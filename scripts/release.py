# -*- coding: utf-8 -*-
"""构建并校验可发布的纯净包。

默认流程：版本一致性 -> 工作区检查 -> 测试 -> 干净构建 -> 归档审计 ->
SHA256SUMS -> 新虚拟环境安装冒烟。默认只产出 dist/，不发布。

用法:
    uv run python scripts/release.py
    uv run python scripts/release.py --allow-dirty
    uv run python scripts/release.py --publish              # 发布到 PyPI
    uv run python scripts/release.py --publish --test-pypi  # 发布到 TestPyPI

发布需要 UV_PUBLISH_TOKEN（或 uv 支持的其他凭据来源）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_FILE_BYTES = 5 * 1024 * 1024
TEST_PYPI_URL = "https://test.pypi.org/legacy/"

FORBIDDEN_SEGMENTS = {
    ".git", ".cache", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".venv", "venv", "dist", "build", "node_modules", ".idea", ".vscode",
}
FORBIDDEN_SUFFIXES = (".pyc", ".pyo", ".pdf", ".tmp", ".log", ".sqlite", ".db", ".key", ".pem")

ALLOWED_SDIST_TOP = {
    "pdf_translator", "examples", "tests", "scripts",
    "PKG-INFO", "pyproject.toml", "README.md", "AGENTS.md", "OVERVIEW.md",
    "CLAUDE.md", "config.example.json", "reader.py",
    ".gitignore",  # hatchling 会把 VCS 忽略文件强制带入 sdist
}

SDIST_REQUIRED = [
    "PKG-INFO", "pyproject.toml", "README.md", "AGENTS.md", "OVERVIEW.md",
    "CLAUDE.md", "config.example.json", "reader.py",
    "examples/adapters/echo_agent.py",
    "tests/conftest.py",
    "scripts/release.py",
    "pdf_translator/__init__.py", "pdf_translator/__main__.py",
    "pdf_translator/cli.py", "pdf_translator/server.py",
    "pdf_translator/store.py", "pdf_translator/book.py",
    "pdf_translator/config.py", "pdf_translator/prompts.py",
    "pdf_translator/api_translator.py", "pdf_translator/bridge.py",
    "pdf_translator/web/index.html", "pdf_translator/web/app.js",
    "pdf_translator/web/style.css",
]

WHEEL_REQUIRED = [
    "pdf_translator/__init__.py", "pdf_translator/__main__.py",
    "pdf_translator/cli.py", "pdf_translator/server.py",
    "pdf_translator/store.py", "pdf_translator/book.py",
    "pdf_translator/config.py", "pdf_translator/prompts.py",
    "pdf_translator/api_translator.py", "pdf_translator/bridge.py",
    "pdf_translator/web/index.html", "pdf_translator/web/app.js",
    "pdf_translator/web/style.css",
]

SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"), "private key"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "OpenAI-style key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), "GitHub token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"), "GitHub token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "Google API key"),
    (re.compile(r"(?i)(?:api[_-]?key|secret|token|password)\s*[:=]\s*[\"'][A-Za-z0-9_\-]{24,}[\"']"),
     "credential assignment"),
]


class ReleaseError(RuntimeError):
    pass


def step(msg: str) -> None:
    print(f"\n==> {msg}", flush=True)


def info(msg: str) -> None:
    print(f"    {msg}", flush=True)


def run(cmd: list, cwd: Path = ROOT, env: dict | None = None) -> str:
    cmd = [str(x) for x in cmd]
    info("$ " + " ".join(cmd))
    proc = subprocess.run(
        cmd, cwd=str(cwd), env=env, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    out = (proc.stdout or "").strip()
    if out:
        print(out, flush=True)
    if proc.returncode != 0:
        raise ReleaseError(f"命令失败（exit {proc.returncode}）: {' '.join(cmd)}")
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_env() -> dict:
    drop = ("TRANSLATE_", "OPENAI_", "ANTHROPIC_", "PDFTRANSLATOR_")
    env = {k: v for k, v in os.environ.items() if not k.startswith(drop)}
    env["PYTHONUTF8"] = "1"
    env["UV_NO_PROGRESS"] = "1"
    return env


def project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text("utf-8")
    m = re.search(r"(?ms)^\[project\]\s*$(.*?)(?=^\[|\Z)", text)
    if not m:
        raise ReleaseError("pyproject.toml 缺少 [project] 段")
    vm = re.search(r'(?m)^version\s*=\s*["\']([^"\']+)["\']', m.group(1))
    if not vm:
        raise ReleaseError("pyproject.toml 的 [project] 缺少 version")
    return vm.group(1)


def module_version() -> str:
    text = (ROOT / "pdf_translator" / "__init__.py").read_text("utf-8")
    m = re.search(r'(?m)^__version__\s*=\s*["\']([^"\']+)["\']', text)
    if not m:
        raise ReleaseError("pdf_translator/__init__.py 缺少 __version__")
    return m.group(1)


def check_git(allow_dirty: bool) -> None:
    if not (ROOT / ".git").exists():
        info("未检测到 .git，跳过工作区检查")
        return
    status = run(["git", "status", "--porcelain"])
    if status and not allow_dirty:
        raise ReleaseError(
            "工作区有未提交的改动（可用 --allow-dirty 跳过此检查）:\n" + status
        )
    if status:
        info("工作区有未提交改动（--allow-dirty）")


def scan_secrets(label: str, name: str, data: bytes, problems: list[str]) -> None:
    if len(data) > 1024 * 1024 or b"\x00" in data[:1024]:
        return
    text = data.decode("utf-8", "ignore")
    for pattern, desc in SECRET_PATTERNS:
        m = pattern.search(text)
        if m:
            problems.append(f"{label}:{name} 疑似{desc}: {m.group(0)[:60]}")


def check_segments(label: str, name: str, problems: list[str]) -> None:
    parts = name.split("/")
    for seg in parts:
        if seg in FORBIDDEN_SEGMENTS or seg.endswith(".egg-info"):
            problems.append(f"{label} 含禁止目录: {name}")
            return
    base = parts[-1]
    if base in ("config.json", "active_book.json", ".env") or base.startswith(".env."):
        problems.append(f"{label} 含本地文件: {name}")
    if name.lower().endswith(FORBIDDEN_SUFFIXES):
        problems.append(f"{label} 含禁止文件类型: {name}")


def audit_wheel(path: Path, version: str) -> None:
    label = "wheel"
    problems: list[str] = []
    dist_info = f"pdf_translator-{version}.dist-info"
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]
        tops = {n.split("/", 1)[0] for n in names}
        extra_tops = tops - {"pdf_translator", dist_info}
        if extra_tops:
            problems.append(f"{label} 含意外顶层项: {sorted(extra_tops)}")

        for req in WHEEL_REQUIRED + [
            f"{dist_info}/METADATA", f"{dist_info}/WHEEL",
            f"{dist_info}/RECORD", f"{dist_info}/entry_points.txt",
        ]:
            if req not in names:
                problems.append(f"{label} 缺少: {req}")

        for info_ in infos:
            check_segments(label, info_.filename, problems)
            if info_.file_size > MAX_FILE_BYTES:
                problems.append(f"{label} 文件过大: {info_.filename} ({info_.file_size} B)")
            if info_.filename in WHEEL_REQUIRED or info_.filename.endswith((".py", ".js", ".css", ".html", ".txt", ".json")):
                scan_secrets(label, info_.filename, zf.read(info_), problems)

        if f"{dist_info}/METADATA" in names:
            meta = zf.read(f"{dist_info}/METADATA").decode("utf-8", "replace")
            head = "\n".join(meta.splitlines()[:40])
            nm = re.search(r"(?m)^Name:\s*(\S+)", head)
            vm = re.search(r"(?m)^Version:\s*(\S+)", head)
            if not nm or re.sub(r"[-_.]+", "_", nm.group(1)).lower() != "pdf_translator":
                problems.append(f"{label} METADATA Name 异常: {nm.group(1) if nm else '缺失'}")
            if not vm or vm.group(1) != version:
                problems.append(f"{label} METADATA Version 异常: {vm.group(1) if vm else '缺失'}")

        if f"{dist_info}/entry_points.txt" in names:
            ep = zf.read(f"{dist_info}/entry_points.txt").decode("utf-8", "replace")
            if "pdftranslator = pdf_translator.cli:main" not in ep:
                problems.append(f"{label} 入口点缺失: pdftranslator")

    if problems:
        raise ReleaseError("wheel 审计失败:\n  - " + "\n  - ".join(problems))
    info(f"wheel 审计通过：{len(names)} 个条目")


def audit_sdist(path: Path, version: str) -> None:
    label = "sdist"
    problems: list[str] = []
    root = f"pdf_translator-{version}"
    with tarfile.open(path) as tf:
        members = tf.getmembers()
        roots = set()
        rel_files: set[str] = set()
        for m in members:
            name = m.name
            if m.issym() or m.islnk():
                problems.append(f"{label} 含链接: {name}")
                continue
            parts = Path(name).parts
            if name.startswith("/") or ".." in parts:
                problems.append(f"{label} 路径异常: {name}")
                continue
            roots.add(parts[0])
            check_segments(label, name, problems)
            if not m.isfile():
                continue
            rel = name.split("/", 1)[1] if "/" in name else ""
            rel_files.add(rel)
            if m.size > MAX_FILE_BYTES:
                problems.append(f"{label} 文件过大: {rel} ({m.size} B)")
            fh = tf.extractfile(m)
            if fh is not None:
                scan_secrets(label, rel, fh.read(), problems)

        if roots != {root}:
            problems.append(f"{label} 顶层目录异常: {sorted(roots)}")

        for req in SDIST_REQUIRED:
            if req not in rel_files:
                problems.append(f"{label} 缺少: {req}")

        tops = {r.split("/", 1)[0] for r in rel_files}
        extra = tops - ALLOWED_SDIST_TOP
        if extra:
            problems.append(f"{label} 含未授权顶层项: {sorted(extra)}")

    if problems:
        raise ReleaseError("sdist 审计失败:\n  - " + "\n  - ".join(problems))
    info(f"sdist 审计通过：{len(members)} 个条目")


def build_artifacts(tmp: Path, version: str) -> tuple[Path, Path]:
    outdir = tmp / "artifacts"
    run(["uv", "build", "--out-dir", outdir], env=clean_env())
    wheels = sorted(outdir.glob("*.whl"))
    sdists = sorted(outdir.glob("*.tar.gz"))
    expected_wheel = f"pdf_translator-{version}-py3-none-any.whl"
    expected_sdist = f"pdf_translator-{version}.tar.gz"
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseError(
            f"构建产物数量异常：wheel={[p.name for p in wheels]} sdist={[p.name for p in sdists]}"
        )
    if wheels[0].name != expected_wheel:
        raise ReleaseError(f"wheel 文件名异常: {wheels[0].name}（期望 {expected_wheel}）")
    if sdists[0].name != expected_sdist:
        raise ReleaseError(f"sdist 文件名异常: {sdists[0].name}（期望 {expected_sdist}）")
    return wheels[0], sdists[0]


def copy_to_dist(wheel: Path, sdist: Path) -> tuple[Path, Path]:
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    for old in list(dist.glob("*.whl")) + list(dist.glob("*.tar.gz")):
        old.unlink()
    sums = dist / "SHA256SUMS"
    if sums.exists():
        sums.unlink()
    w = dist / wheel.name
    s = dist / sdist.name
    shutil.copy2(wheel, w)
    shutil.copy2(sdist, s)
    lines = [f"{sha256_file(p)}  {p.name}" for p in (w, s)]
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return w, s


def smoke_install(tmp: Path, wheel: Path, version: str) -> None:
    venv = tmp / "venv"
    run(["uv", "venv", venv], env=clean_env())
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    script = venv / ("Scripts/pdftranslator.exe" if os.name == "nt" else "bin/pdftranslator")
    if not python.is_file():
        raise ReleaseError(f"虚拟环境缺少 Python: {python}")

    run(["uv", "pip", "install", "--python", python, wheel], env=clean_env())

    out = run([script, "--version"], env=clean_env())
    if version not in out:
        raise ReleaseError(f"--version 输出异常: {out!r}")

    cfg = tmp / "smoke-config.json"
    cfg.write_text(json.dumps({
        "provider": "mock",
        "backend": "api",
        "agent": {"command": []},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    cache = tmp / "smoke-cache"
    raw = run([
        script, "--json", "--cache-dir", cache, "--config", cfg, "doctor",
    ], env=clean_env())
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"doctor 输出不是合法 JSON: {exc}\n{raw[:800]}") from exc
    data = payload.get("data") or {}
    checks = {c.get("name"): c for c in data.get("checks") or []}
    if not payload.get("ok") or not data.get("usable"):
        raise ReleaseError("安装冒烟失败：doctor 报告不可用\n" + raw[:1200])
    for name in ("dependencies", "web_assets", "cache_writable", "api", "python"):
        if not (checks.get(name) or {}).get("ok"):
            raise ReleaseError(f"安装冒烟失败：doctor 检查 {name} 未通过\n" + raw[:1200])

    probe = run([
        python, "-c",
        "import pathlib, pdf_translator;"
        "p = pathlib.Path(pdf_translator.__file__);"
        "print(p);"
        "print((p.parent / 'web' / 'app.js').is_file())",
    ], cwd=tmp, env=clean_env()).splitlines()
    mod_path = Path(probe[0]).resolve()
    if not os.path.normcase(str(mod_path)).startswith(os.path.normcase(str(venv.resolve()))):
        raise ReleaseError(f"安装位置异常（未装入临时环境）: {mod_path}")
    if len(probe) < 2 or probe[1].strip() != "True":
        raise ReleaseError("安装冒烟失败：web/app.js 未随 wheel 安装")
    info(f"安装冒烟通过：{mod_path}")
    info(f"doctor 可用，web 资源与入口点完整")


def publish(uv: str, artifacts: list[Path], url: str) -> None:
    cmd = [uv, "publish", *artifacts]
    if url:
        cmd += ["--publish-url", url]
    print(
        "\n注意：发布不可撤销。确认版本、变更说明与 token 后继续。",
        flush=True,
    )
    run(cmd, env=clean_env())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="构建、审计并（可选）发布 pdf-translator 纯净包")
    p.add_argument("--allow-dirty", action="store_true", help="允许 git 工作区有未提交改动")
    p.add_argument("--skip-tests", action="store_true", help="跳过 pytest")
    p.add_argument("--skip-install-smoke", action="store_true", help="跳过新虚拟环境安装冒烟")
    p.add_argument("--keep-temp", action="store_true", help="保留临时构建目录便于排查")
    p.add_argument("--publish", action="store_true", help="构建校验后发布到 PyPI")
    p.add_argument("--test-pypi", action="store_true", help="发布到 TestPyPI")
    p.add_argument("--publish-url", default="", help="自定义发布地址")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = parse_args(argv)
    if args.test_pypi and args.publish_url:
        print("--test-pypi 与 --publish-url 不能同时使用", file=sys.stderr)
        return 2
    if args.test_pypi and not args.publish:
        print("--test-pypi 需要配合 --publish", file=sys.stderr)
        return 2
    if args.publish_url and not args.publish:
        print("--publish-url 需要配合 --publish", file=sys.stderr)
        return 2
    if args.publish and (args.skip_tests or args.skip_install_smoke):
        print("发布前不允许跳过测试或安装冒烟", file=sys.stderr)
        return 2

    uv = shutil.which("uv")
    if not uv:
        print("找不到 uv。请先安装：https://docs.astral.sh/uv/", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="pdf-translator-release-"))
    try:
        step("检查版本一致性")
        version = project_version()
        mod_version = module_version()
        if version != mod_version:
            raise ReleaseError(
                f"版本不一致：pyproject.toml={version}，pdf_translator/__init__.py={mod_version}"
            )
        info(f"版本 {version}")

        step("检查工作区")
        check_git(args.allow_dirty)

        if not args.skip_tests:
            step("运行测试")
            run(["uv", "run", "--extra", "dev", "pytest", "-q"], env=clean_env())

        step("干净构建（临时目录）")
        wheel, sdist = build_artifacts(tmp, version)
        info(f"{wheel.name}  {wheel.stat().st_size} B")
        info(f"{sdist.name}  {sdist.stat().st_size} B")

        step("审计 wheel")
        audit_wheel(wheel, version)

        step("审计 sdist")
        audit_sdist(sdist, version)

        if not args.skip_install_smoke:
            step("新虚拟环境安装冒烟")
            smoke_install(tmp, wheel, version)

        step("写入 dist/")
        final_wheel, final_sdist = copy_to_dist(wheel, sdist)
        info(f"{final_wheel}")
        info(f"{final_sdist}")
        info(f"{ROOT / 'dist' / 'SHA256SUMS'}")
        print((ROOT / "dist" / "SHA256SUMS").read_text("utf-8").strip(), flush=True)

        if args.publish:
            step("发布")
            url = TEST_PYPI_URL if args.test_pypi else args.publish_url
            publish(uv, [final_wheel, final_sdist], url)
            info("发布完成")
        else:
            step("完成（未发布）")
            info("确认无误后可加 --publish 发布；TestPyPI 用 --publish --test-pypi")
        return 0
    except ReleaseError as exc:
        print(f"\n失败: {exc}", file=sys.stderr, flush=True)
        if args.keep_temp:
            print(f"临时目录保留在: {tmp}", file=sys.stderr, flush=True)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
        return 1
    finally:
        if not args.keep_temp:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

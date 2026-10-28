# -*- coding: utf-8 -*-
"""一次性 agent 子进程：stdin 给提示词，stdout 取译文。

刻意不要求子进程调用任何工具——网页按 T 时只需一个纯文本转换，
因此不需要权限弹窗，也不会有 agent 在项目目录里乱翻文件的问题。
"""
from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .prompts import build_live_prompt, clean_translation, validate_translation


class AgentError(RuntimeError):
    def __init__(self, code: str, message: str, hint: str = "", stage: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.stage = stage

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "hint": self.hint, "stage": self.stage}


def normalize_command(command) -> list[str]:
    if isinstance(command, str):
        if not command.strip():
            return []
        if os.name == "nt":
            return [p.strip('"') for p in shlex.split(command, posix=False)]
        return shlex.split(command)
    if isinstance(command, (list, tuple)):
        return [str(x) for x in command]
    return []


def resolve_command(command) -> list[str]:
    argv = normalize_command(command)
    if not argv:
        raise AgentError(
            "agent_unavailable",
            "未配置 agent.command",
            "在 config.json 的 agent.command 里填入命令数组，例如 "
            '["claude", "-p", "--output-format", "text"]',
        )
    exe = argv[0]
    resolved = shutil.which(exe)
    if not resolved:
        p = Path(exe).expanduser()
        if p.is_file():
            resolved = str(p.resolve())
    if not resolved:
        raise AgentError(
            "agent_unavailable",
            f"找不到 agent 可执行文件: {exe}",
            "安装对应 CLI，或在 agent.command 里写绝对路径",
        )
    argv = [resolved] + argv[1:]
    if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return [comspec, "/d", "/s", "/c", subprocess.list2cmdline(argv)]
    return argv


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


class AgentBridge:
    def __init__(self, agent_cfg: dict, store, log=print):
        self.cfg = dict(agent_cfg or {})
        self.store = store
        self.log = log
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()

    @property
    def command(self) -> list:
        return list(self.cfg.get("command") or [])

    def label(self) -> str:
        return " ".join(str(x) for x in self.command)

    def available(self) -> bool:
        try:
            resolve_command(self.command)
            return True
        except AgentError:
            return False

    def _cwd(self) -> Path:
        raw = str(self.cfg.get("cwd") or "").strip()
        if raw:
            p = Path(raw).expanduser().resolve()
            p.mkdir(parents=True, exist_ok=True)
            return p
        d = self.store.agent_run_dir
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _prepare(self, prompt: str) -> tuple[list[str], bytes | None, Path | None]:
        mode = str(self.cfg.get("prompt_mode") or "stdin").lower()
        argv = resolve_command(self.command)
        cleanup: Path | None = None
        if mode == "arg":
            replaced = [a.replace("{prompt}", prompt) for a in argv]
            if not any("{prompt}" in a for a in argv):
                replaced.append(prompt)
            return replaced, None, cleanup
        if mode == "file":
            self.store.agent_run_dir.mkdir(parents=True, exist_ok=True)
            cleanup = self.store.agent_run_dir / f"prompt-{uuid.uuid4().hex}.txt"
            cleanup.write_text(prompt, "utf-8")
            replaced = [
                a.replace("{prompt_file}", str(cleanup)).replace("{prompt-file}", str(cleanup))
                for a in argv
            ]
            if not any("{prompt_file}" in a or "{prompt-file}" in a for a in argv):
                replaced.append(str(cleanup))
            return replaced, None, cleanup
        return argv, prompt.encode("utf-8"), cleanup

    def translate(self, page: int, paragraphs: list[str], context: str, glossary: dict,
                  prev_tail: str = "", timeout: float | None = None) -> dict:
        prompt = build_live_prompt(context, glossary, prev_tail, paragraphs)
        argv, stdin_data, cleanup = self._prepare(prompt)
        timeout = float(timeout or self.cfg.get("timeout") or 180)

        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        for k, v in (self.cfg.get("env") or {}).items():
            env[str(k)] = str(v)

        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True

        started = time.time()
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(self._cwd()),
                env=env,
                stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **kwargs,
            )
        except FileNotFoundError as exc:
            if cleanup:
                cleanup.unlink(missing_ok=True)
            raise AgentError("agent_unavailable", f"启动 agent 失败: {exc}", "检查 agent.command") from exc
        except OSError as exc:
            if cleanup:
                cleanup.unlink(missing_ok=True)
            raise AgentError("agent_unavailable", f"启动 agent 失败: {exc}", "检查 agent.command") from exc

        with self._lock:
            self._procs.add(proc)
        try:
            try:
                out, err = proc.communicate(input=stdin_data, timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                raise AgentError(
                    "agent_timeout",
                    f"agent 超过 {timeout:.0f}s 未返回",
                    "检查命令/模型/网络，或调大 config.json 的 agent.timeout",
                    stage="timeout",
                )
        finally:
            with self._lock:
                self._procs.discard(proc)
            if cleanup:
                cleanup.unlink(missing_ok=True)

        if proc.returncode != 0:
            tail = (err or b"").decode("utf-8", "replace").strip()[-500:]
            raise AgentError(
                "agent_failed",
                f"agent 退出码 {proc.returncode}" + (f": {tail}" if tail else ""),
                "先用 doctor --agent-probe 验证 agent.command",
                stage="exit",
            )

        raw = (out or b"").decode("utf-8", "replace")
        text = clean_translation(raw)
        checked = validate_translation(paragraphs, text)
        if checked["error"]:
            raise AgentError(
                "invalid_output",
                f"agent 输出无效: {checked['error']}",
                "确认 agent.command 只输出译文；必要时用 prompt_mode=file/arg",
                stage="validate",
            )
        return {
            "text": text,
            "paragraph_count": checked["paragraph_count"],
            "warnings": checked["warnings"],
            "elapsed": round(time.time() - started, 2),
        }

    def probe(self, timeout: float = 60) -> dict:
        return self.translate(
            page=0,
            paragraphs=["Hello, world."],
            context="",
            glossary={},
            prev_tail="",
            timeout=timeout,
        )

    def close(self) -> None:
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            _kill_tree(proc)

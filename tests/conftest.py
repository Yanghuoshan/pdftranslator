# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PAGES = [
    [
        "The formation of Christendom in late antiquity is a long and",
        "complicated story of bishops, councils, and the Holy See.",
    ],
    ["The second page continues the narrative with the Arian controversy."],
    [],
    ["The final page discusses doctrine and the Church in the medieval world."],
]


def make_pdf(path: Path, pages: list[list[str]]) -> Path:
    """生成一个带文本层的极简多页 PDF（标准 Helvetica，无额外依赖）。"""
    objs: dict[int, bytes] = {}
    page_ids = [5 + 2 * i for i in range(len(pages))]
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objs[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii")
    objs[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    for i, lines in enumerate(pages):
        content_id = 4 + 2 * i
        page_id = 5 + 2 * i
        stream: list[str] = []
        if lines:
            stream.append("BT /F1 12 Tf 72 720 Td")
            for j, line in enumerate(lines):
                esc = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                stream.append(("" if j == 0 else "0 -40 Td ") + f"({esc}) Tj")
            stream.append("ET")
        data = "\n".join(stream).encode("latin-1", "replace")
        objs[content_id] = (
            b"<< /Length " + str(len(data)).encode("ascii")
            + b" >>\nstream\n" + data + b"\nendstream"
        )
        objs[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode("ascii")

    total = max(objs) + 1
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for oid in range(1, total):
        offsets[oid] = len(out)
        out += f"{oid} 0 obj\n".encode("ascii") + objs[oid] + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {total}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for oid in range(1, total):
        out += f"{offsets[oid]:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {total} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


def write_config(path: Path, **overrides) -> Path:
    cfg = {
        "provider": "mock",
        "model": "mock",
        "base_url": "",
        "api_key": "",
        "backend": "auto",
        "agent": {
            "command": [],
            "prompt_mode": "stdin",
            "timeout": 30,
            "max_concurrent": 1,
            "fallback_to_api": True,
            "cwd": "",
            "env": {},
        },
        "books": {},
    }
    cfg.update(overrides)
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path: Path) -> SimpleNamespace:
    pdf = make_pdf(tmp_path / "sample.pdf", PAGES)
    cache = tmp_path / "cache"
    cfg = write_config(tmp_path / "config.json")
    return SimpleNamespace(pdf=pdf, cache=cache, cfg=cfg, tmp=tmp_path)


FAKE_AGENT = '''# -*- coding: utf-8 -*-
import sys
import time

args = sys.argv[1:]
mode = args[0] if args else "ok"
prompt = ""
for a in args[1:]:
    if "[待翻译正文]" in a:
        prompt = a
        break
    if not a.startswith("-"):
        try:
            prompt = open(a, encoding="utf-8-sig").read()
            break
        except OSError:
            pass
if not prompt:
    prompt = sys.stdin.buffer.read().decode("utf-8", "replace")

body = prompt.split("[待翻译正文]", 1)[-1].split("<<<BEGIN_TRANSLATION>>>", 1)[0]
paras = []
for p in body.split("\\n\\n"):
    p = p.strip()
    if not p:
        continue
    if "只输出译文正文" in p or p.startswith("不要输出解释"):
        continue
    paras.append(p)
if not paras:
    paras = ["无正文"]
zh = "\\n\\n".join("第 %d 段测试译文，内容为占位。" % i for i in range(1, len(paras) + 1))

if mode == "fail":
    sys.stderr.write("boom")
    sys.exit(3)
if mode == "empty":
    sys.exit(0)
if mode == "slow":
    time.sleep(5)
if mode == "echo":
    sys.stdout.write(prompt)
    sys.exit(0)

if mode == "fenced":
    sys.stdout.write("```text\\n" + zh + "\\n```\\n")
elif mode == "bom":
    sys.stdout.buffer.write(("\\ufeff" + zh).encode("utf-8"))
else:
    sys.stdout.write("<<<BEGIN_TRANSLATION>>>\\n" + zh + "\\n<<<END_TRANSLATION>>>\\n")
'''


@pytest.fixture
def fake_agent(tmp_path: Path) -> Path:
    p = tmp_path / "fake_agent.py"
    p.write_text(FAKE_AGENT, encoding="utf-8")
    return p

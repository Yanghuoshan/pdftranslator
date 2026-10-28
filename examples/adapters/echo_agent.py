#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零成本假 agent，用于端到端验证网页 T 按钮与 bridge 接线。

读取 stdin（或第一个命令行参数指向的文件）里的提示词，按原文段落数
返回一段中文占位译文，不访问网络、不花 token。

配置示例：
    "agent": {
      "command": ["python", "examples/adapters/echo_agent.py"],
      "prompt_mode": "stdin"
    }
"""
from __future__ import annotations

import sys
from pathlib import Path

BEGIN = "<<<BEGIN_TRANSLATION>>>"
END = "<<<END_TRANSLATION>>>"


def paragraphs_from_prompt(prompt: str) -> list[str]:
    body = prompt.split("[待翻译正文]", 1)[-1]
    body = body.split(BEGIN, 1)[0]
    out = []
    for p in body.split("\n\n"):
        p = p.strip()
        if not p:
            continue
        if "只输出译文正文" in p or p.startswith("不要输出解释"):
            continue
        out.append(p)
    return out


def main() -> int:
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        prompt = Path(sys.argv[1]).read_text("utf-8-sig")
    else:
        prompt = sys.stdin.buffer.read().decode("utf-8-sig", "replace")

    paras = paragraphs_from_prompt(prompt)
    if not paras:
        paras = ["（本页无正文）"]
    translated = "\n\n".join(
        f"【测试译文·第 {i} 段】这是离线假 agent 生成的占位译文，用于验证从取页到落盘的完整流程。"
        for i in range(1, len(paras) + 1)
    )
    sys.stdout.buffer.write(
        f"{BEGIN}\n{translated}\n{END}\n".encode("utf-8")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""兼容旧入口：等价于 `pdftranslator serve`。

旧的 --provider/--model/--base-url/--concurrency 会转成环境变量继续生效；
--prewarm 已废弃，改由 config.json 的 prewarm_pages 控制。
"""
from __future__ import annotations

import os
import sys

from pdf_translator.cli import main

_ENV_MAP = {
    "--provider": "TRANSLATE_PROVIDER",
    "--model": "TRANSLATE_MODEL",
    "--base-url": "TRANSLATE_BASE_URL",
    "--concurrency": "TRANSLATE_CONCURRENCY",
}
_KEEP_WITH_VALUE = {"--port", "--host", "--config", "--cache-dir", "--backend"}


def _to_serve(argv: list[str]) -> list[str]:
    out = ["serve"]
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--prewarm":
            i += 2
            continue
        if arg in _ENV_MAP:
            os.environ[_ENV_MAP[arg]] = argv[i + 1]
            i += 2
            continue
        if arg in _KEEP_WITH_VALUE:
            out.extend([arg, argv[i + 1]])
            i += 2
            continue
        out.append(arg)
        i += 1
    return out


if __name__ == "__main__":
    raise SystemExit(main(_to_serve(sys.argv[1:])))

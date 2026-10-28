# -*- coding: utf-8 -*-
"""配置加载、环境变量覆盖、按书覆盖、agent 配置校验。"""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "provider": "openai",              # openai(兼容) | anthropic | mock
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "api_key": "",                     # 留空则读环境变量，密钥不落盘
    "api_key_env": "TRANSLATE_API_KEY",
    "temperature": 0.2,
    "timeout": 180,
    "max_chars_per_chunk": 1600,
    "concurrency": 4,
    "prewarm_pages": 3,
    "prefetch_next_pages": 0,
    "render_scale": 2,
    "context": "",
    "glossary": {},
    "books": {},
    "backend": "auto",                 # auto | agent | api
    "agent": {
        "command": [],                 # ["claude", "-p", "--output-format", "text"]
        "prompt_mode": "stdin",        # stdin | arg | file
        "timeout": 180,
        "max_concurrent": 1,
        "fallback_to_api": True,
        "cwd": "",
        "env": {},
    },
}


def load_config(explicit: str | None = None) -> tuple[dict, Path | None]:
    cands: list[Path] = []
    if explicit:
        cands.append(Path(explicit).expanduser())
    cands += [
        Path.cwd() / "config.json",
        Path(__file__).resolve().parent.parent / "config.json",
        Path.home() / ".config" / "pdf-translator" / "config.json",
        Path.home() / ".pdfreader" / "config.json",
    ]
    raw: dict = {}
    path: Path | None = None
    for c in cands:
        if c.is_file():
            try:
                raw = json.loads(c.read_text("utf-8-sig")) or {}
            except json.JSONDecodeError as exc:
                raise ValueError(f"配置文件不是合法 JSON: {c} ({exc})") from exc
            path = c
            break

    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in raw.items() if k != "agent"})
    agent = dict(DEFAULTS["agent"])
    agent.update(raw.get("agent") or {})
    cfg["agent"] = agent

    env = os.environ
    if env.get("TRANSLATE_PROVIDER"):
        cfg["provider"] = env["TRANSLATE_PROVIDER"]
    if env.get("TRANSLATE_BASE_URL"):
        cfg["base_url"] = env["TRANSLATE_BASE_URL"]
    if env.get("TRANSLATE_MODEL"):
        cfg["model"] = env["TRANSLATE_MODEL"]
    if env.get("TRANSLATE_TEMPERATURE"):
        cfg["temperature"] = float(env["TRANSLATE_TEMPERATURE"])
    if env.get("TRANSLATE_CONCURRENCY"):
        cfg["concurrency"] = int(env["TRANSLATE_CONCURRENCY"])
    if env.get("PDFTRANSLATOR_BACKEND"):
        cfg["backend"] = env["PDFTRANSLATOR_BACKEND"]
    return cfg, path


def resolve_key(cfg: dict) -> str:
    if cfg.get("api_key"):
        return str(cfg["api_key"])
    env = os.environ
    for name in (cfg.get("api_key_env") or "", "TRANSLATE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        if name and env.get(name):
            return env[name]
    return ""


def agent_settings(cfg: dict) -> dict:
    """返回归一化后的 agent 配置；非法值抛 ValueError。"""
    a = dict(DEFAULTS["agent"])
    a.update(cfg.get("agent") or {})

    command = a.get("command") or []
    if isinstance(command, str):
        parts = shlex.split(command, posix=(os.name != "nt"))
        command = [p.strip('"') for p in parts]
    elif isinstance(command, (list, tuple)):
        command = [str(x) for x in command]
    else:
        raise ValueError("agent.command 必须是字符串数组")

    mode = str(a.get("prompt_mode") or "stdin").lower()
    if mode not in ("stdin", "arg", "file"):
        raise ValueError("agent.prompt_mode 只能是 stdin / arg / file")

    return {
        "command": command,
        "prompt_mode": mode,
        "timeout": float(a.get("timeout") or 180),
        "max_concurrent": max(1, int(a.get("max_concurrent") or 1)),
        "fallback_to_api": bool(a.get("fallback_to_api", True)),
        "cwd": str(a.get("cwd") or ""),
        "env": dict(a.get("env") or {}),
    }


def apply_book_overrides(cfg: dict, pdf: str | Path) -> dict:
    """按书名（子串匹配）套用 config.json 里的 books 段覆盖项。"""
    name = Path(pdf).name
    out = dict(cfg)
    for key, val in (cfg.get("books") or {}).items():
        if key and key in name:
            out.update({k: v for k, v in val.items() if k not in ("context", "glossary")})
    return out


def book_settings(cfg: dict, pdf: str | Path) -> tuple[str, dict]:
    name = Path(pdf).name
    context = cfg.get("context") or ""
    glossary = dict(cfg.get("glossary") or {})
    for key, val in (cfg.get("books") or {}).items():
        if key and key in name:
            context = val.get("context", context)
            glossary.update(val.get("glossary") or {})
    return context, glossary


def backend_settings(cfg: dict, agent_ok: bool, api_ok: bool) -> dict:
    pref = str(cfg.get("backend") or "auto").lower()
    if pref not in ("auto", "agent", "api"):
        pref = "auto"
    if pref == "agent":
        chosen = "agent" if agent_ok else ("api" if api_ok else "none")
    elif pref == "api":
        chosen = "api" if api_ok else "none"
    else:
        chosen = "agent" if agent_ok else ("api" if api_ok else "none")
    return {
        "preferred": pref,
        "chosen": chosen,
        "agent_available": agent_ok,
        "api_available": api_ok,
    }

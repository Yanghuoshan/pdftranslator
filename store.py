# -*- coding: utf-8 -*-
"""配置加载 + 译文/图片落盘缓存。

缓存布局（默认 .cache/<书名-slug>/）:
    en.json          整本书英文分段文本（首次抽取后落盘，之后秒开）
    zh/<页>.<模型>.json   每页译文，按模型分文件，换模型不会互相顶掉
    img/<页>.png     原页渲染图（懒渲染）
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "provider": "openai",              # openai(兼容) | anthropic | mock(离线自检)
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "api_key": "",                     # 留空则读环境变量，密钥不落盘
    "api_key_env": "TRANSLATE_API_KEY",
    "temperature": 0.2,
    "timeout": 180,
    "max_chars_per_chunk": 1600,       # 单次请求原文上限，超出按段落切块
    "concurrency": 4,                  # 后台批翻并发
    "prewarm_pages": 3,                # 启动即翻译前 N 页
    "prefetch_next_pages": 0,          # 翻页时向后偷偷预翻 N 页（0=严格懒加载）
    "render_scale": 2,
    "context": "",                     # 全书背景提示（题材/时代/作者）
    "glossary": {},                    # 术语表 {"Christendom": "基督教世界"}
    "books": {},                       # 按文件名覆盖上面任意字段
}


def load_config(explicit: str | None = None) -> tuple[dict, Path | None]:
    cands = []
    if explicit:
        cands.append(Path(explicit).expanduser())
    cands += [
        Path.cwd() / "config.json",
        Path(__file__).resolve().parent / "config.json",
        Path.home() / ".pdfreader" / "config.json",
    ]
    raw: dict = {}
    path: Path | None = None
    for c in cands:
        if c.is_file():
            raw = json.loads(c.read_text("utf-8")) or {}
            path = c
            break
    cfg = dict(DEFAULTS)
    cfg.update(raw)
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
    return cfg, path


def resolve_key(cfg: dict) -> str:
    if cfg.get("api_key"):
        return cfg["api_key"]
    env = os.environ
    for name in (cfg.get("api_key_env") or "", "TRANSLATE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        if name and env.get(name):
            return env[name]
    return ""


def apply_book_overrides(cfg: dict, pdf: str | Path) -> dict:
    """按书名套用 config.json 里的 books 段覆盖项。"""
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


def model_slug(provider: str, model: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{provider}_{model}")[:60]


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    tmp.replace(path)


class Store:
    def __init__(self, pdf: str | Path, cache_root: str | Path = ".cache"):
        self.pdf = Path(pdf)
        st = self.pdf.stat()
        digest = hashlib.sha1(f"{self.pdf.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:8]
        self.slug = re.sub(r"[^A-Za-z0-9_.-]", "_", self.pdf.stem)[:40] + "-" + digest
        self.root = Path(cache_root) / self.slug
        self.zh_dir = self.root / "zh"
        self.img_dir = self.root / "img"
        self.zh_dir.mkdir(parents=True, exist_ok=True)
        self.img_dir.mkdir(parents=True, exist_ok=True)

    # ---- 英文文本 ----
    @property
    def en_path(self) -> Path:
        return self.root / "en.json"

    def load_en(self) -> list[list[str]] | None:
        p = self.en_path
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text("utf-8")).get("pages")
        except Exception:
            return None

    def save_en(self, pages: list[list[str]]) -> None:
        _write_json(self.en_path, {"file": str(self.pdf), "pages": pages})

    # ---- 译文 ----
    def zh_path(self, n: int, mslug: str) -> Path:
        return self.zh_dir / f"{n:05d}.{mslug}.json"

    def load_zh(self, n: int, mslug: str) -> str | None:
        p = self.zh_path(n, mslug)
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text("utf-8")).get("text")
        except Exception:
            return None

    def save_zh(self, n: int, mslug: str, rec: dict) -> None:
        _write_json(self.zh_path(n, mslug), rec)

    def translated_pages(self, mslug: str) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for p in self.zh_dir.glob(f"*.{mslug}.json"):
            try:
                rec = json.loads(p.read_text("utf-8"))
                out[int(rec["page"])] = rec
            except Exception:
                continue
        return out

    # ---- 原页图片 ----
    def img_path(self, n: int, scale: int) -> Path:
        return self.img_dir / f"{n:05d}@{scale}x.png"

    def load_img(self, n: int, scale: int) -> bytes | None:
        p = self.img_path(n, scale)
        return p.read_bytes() if p.is_file() else None

    def save_img(self, n: int, scale: int, data: bytes) -> None:
        p = self.img_path(n, scale)
        tmp = Path(str(p) + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)

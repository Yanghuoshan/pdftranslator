# -*- coding: utf-8 -*-
"""API 兜底翻译：OpenAI 兼容 / Anthropic / mock。"""
from __future__ import annotations

import re
import time

import httpx

from .config import resolve_key
from .prompts import SYSTEM_PROMPT, build_user_content, clean_translation, source_text


class TranslationError(RuntimeError):
    pass


class ApiTranslator:
    def __init__(self, cfg: dict, context: str = "", glossary: dict | None = None, log=print):
        self.cfg = cfg
        self.context = context or ""
        self.glossary = glossary or {}
        self.log = log
        self.provider = (cfg.get("provider") or "openai").lower()
        self.model = cfg.get("model") or ""
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        self.api_key = resolve_key(cfg)
        self.temperature = float(cfg.get("temperature", 0.2))
        self.timeout = float(cfg.get("timeout", 180))
        self.max_chars = int(cfg.get("max_chars_per_chunk", 1600))
        self.client = httpx.Client(timeout=self.timeout, trust_env=True)
        self.stats = {"tokens": 0, "pages": 0, "errors": 0, "requests": 0}

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass

    def available(self) -> bool:
        return self.provider == "mock" or bool(self.api_key)

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"

    def translate_paras(self, paragraphs: list[str], prev_tail: str = "") -> str:
        if not source_text(paragraphs).strip():
            return ""
        chunks = self._chunk(paragraphs)
        pieces: list[str] = []
        tail = prev_tail
        for idx, chunk in enumerate(chunks):
            user = build_user_content(self.context, self.glossary, tail, chunk)
            if len(chunks) > 1:
                user = f"[本页第 {idx + 1}/{len(chunks)} 段，只需翻译本段]\n\n" + user
            raw = self._call(SYSTEM_PROMPT, user)
            text = clean_translation(raw)
            pieces.append(text)
            tail = text[-200:]
        out = "\n\n".join(p for p in pieces if p.strip())
        if out.strip():
            self.stats["pages"] += 1
        return out

    def _chunk(self, paras: list[str]) -> list[list[str]]:
        chunks: list[list[str]] = []
        cur: list[str] = []
        size = 0
        for p in paras:
            if cur and size + len(p) > self.max_chars and size > self.max_chars * 0.5:
                chunks.append(cur)
                cur, size = [], 0
            cur.append(p)
            size += len(p) + 2
        if cur:
            chunks.append(cur)
        if len(chunks) > 1 and sum(len(p) for p in chunks[-1]) < self.max_chars * 0.2:
            chunks[-2].extend(chunks[-1])
            chunks.pop()
        return chunks or [[]]

    def _call(self, system: str, user: str) -> str:
        last: Exception | None = None
        for attempt in range(3):
            try:
                if self.provider == "mock":
                    return "【离线自检译文】" + "占位。" * (len(user) // 6 + 1)
                if self.provider in ("anthropic", "claude"):
                    return self._anthropic(system, user)
                return self._openai(system, user)
            except TranslationError as exc:
                last = exc
                if attempt == 2 or not re.search(r"(429|5\d\d|timeout|timed out|超时|连接)", str(exc), re.I):
                    raise
                time.sleep(2 * (attempt + 1))
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt == 2:
                    break
                time.sleep(2 * (attempt + 1))
        raise TranslationError(str(last) if last else "翻译失败")

    def _openai(self, system: str, user: str) -> str:
        if not self.base_url:
            raise TranslationError("未配置 base_url")
        if not self.api_key:
            raise TranslationError("未配置 API Key：请在 config.json 填写 api_key 或设置环境变量 TRANSLATE_API_KEY")
        resp = self.client.post(
            self.base_url + "/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={
                "model": self.model,
                "temperature": self.temperature,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            },
        )
        if resp.status_code >= 400:
            raise TranslationError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        content = (msg.get("content") or msg.get("reasoning_content") or "").strip()
        if not content:
            raise TranslationError("模型返回空内容")
        usage = data.get("usage") or {}
        self.stats["tokens"] += int(usage.get("total_tokens") or 0)
        self.stats["requests"] += 1
        return content

    def _anthropic(self, system: str, user: str) -> str:
        if not self.api_key:
            raise TranslationError("未配置 API Key：请设置环境变量 ANTHROPIC_API_KEY")
        base = self.base_url or "https://api.anthropic.com/v1"
        resp = self.client.post(
            base + "/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": 4096,
                "temperature": self.temperature,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )
        if resp.status_code >= 400:
            raise TranslationError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        blocks = data.get("content") or []
        content = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        if not content:
            raise TranslationError("模型返回空内容")
        usage = data.get("usage") or {}
        self.stats["tokens"] += int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        self.stats["requests"] += 1
        return content

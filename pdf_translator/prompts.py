# -*- coding: utf-8 -*-
"""翻译提示词、译文清洗与校验。CLI 与网页 bridge 共用同一套规则。"""
from __future__ import annotations

import hashlib
import re

BEGIN_MARK = "<<<BEGIN_TRANSLATION>>>"
END_MARK = "<<<END_TRANSLATION>>>"

TRANSLATION_RULES = """你是资深英译中译者，擅长历史、宗教、哲学类学术著作。
翻译要求：
1. 用简体中文，忠实原文、语句通顺优雅；不漏译，不添加原文没有的内容。
2. 严格保持段落结构：原文用空行分隔段落，译文必须用同样的空行分隔，段落数量与顺序一致。
3. 术语表给出的词按术语表翻译；未覆盖的专有名词（人名、地名、书名）保留英文原文。
4. 只输出译文正文，不要说明、注释、标题或"译文："之类的前缀。
5. 忽略页码、页眉、装饰性符号等无关内容。"""

SYSTEM_PROMPT = "你是一位专业的英译中翻译。\n" + TRANSLATION_RULES

INSTRUCTIONS = TRANSLATION_RULES + """

输出方式：只把译文写入给定文件（或直接作为 save 命令的输入），不要附加解释。"""

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_FENCE_HEAD = re.compile(r"^```[a-zA-Z0-9_-]*\s*")
_FENCE_TAIL = re.compile(r"\s*```$")
_LABEL = re.compile(r"^(译文|翻译|Translation)\s*[:：]\s*", re.I)
_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def build_user_content(context: str, glossary: dict, prev_tail: str, paragraphs: list[str]) -> str:
    parts = []
    if context:
        parts.append(f"[书籍背景] {context}")
    if glossary:
        table = "\n".join(f"{k} = {v}" for k, v in glossary.items())
        parts.append(f"[术语表]\n{table}")
    if prev_tail:
        parts.append(f"[上一页译文结尾，仅用于保持语气与译名一致，不要重复翻译]\n{prev_tail}")
    parts.append("[待翻译正文]\n" + "\n\n".join(paragraphs))
    return "\n\n".join(parts)


def build_live_prompt(context: str, glossary: dict, prev_tail: str, paragraphs: list[str]) -> str:
    """网页按键触发的一次性 agent 提示词：只要求输出纯文本。"""
    return (
        SYSTEM_PROMPT
        + "\n\n"
        + build_user_content(context, glossary, prev_tail, paragraphs)
        + "\n\n"
        + f"只输出译文正文，并用 {BEGIN_MARK} 和 {END_MARK} 包裹。"
        + "\n不要输出解释、标题、页码或任何其他内容；不要调用任何工具。"
    )


_ECHO_HINTS = ("[待翻译正文]", "[书籍背景]", "翻译要求：", "不要输出解释")


def clean_translation(text: str) -> str:
    t = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    t = _ANSI.sub("", t)
    # 提示词回显：先把原文留着，交给 validate_translation 明确拒绝，
    # 避免把提示词里内联的 marker 误当成译文边界。
    if any(h in t for h in _ECHO_HINTS):
        return t.strip()
    if BEGIN_MARK in t and END_MARK in t:
        start = t.index(BEGIN_MARK) + len(BEGIN_MARK)
        end = t.rindex(END_MARK)
        if end > start:
            t = t[start:end]
    t = t.strip()
    t = _FENCE_HEAD.sub("", t)
    t = _FENCE_TAIL.sub("", t)
    t = _LABEL.sub("", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]


def source_text(paragraphs: list[str]) -> str:
    return "\n\n".join(p.strip() for p in paragraphs if p.strip())


def text_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def cjk_ratio(text: str) -> float:
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return 0.0
    return len(_CJK.findall(compact)) / len(compact)


def validate_translation(source_paragraphs: list[str], text: str) -> dict:
    """返回 {error, warnings, paragraph_count}。error 为错误码（字符串）或 None。"""
    src = source_text(source_paragraphs)
    warnings: list[str] = []
    if not (text or "").strip():
        return {"error": "empty_translation", "warnings": warnings, "paragraph_count": 0}
    if any(m in text for m in (BEGIN_MARK, END_MARK, "[待翻译正文]", "[书籍背景]", "翻译要求：")):
        return {"error": "prompt_echo", "warnings": warnings, "paragraph_count": 0}
    if len(src) >= 100 and cjk_ratio(text) < 0.05:
        return {"error": "no_chinese", "warnings": warnings, "paragraph_count": 0}
    if len(text) > max(400, len(src) * 4):
        return {"error": "too_long", "warnings": warnings, "paragraph_count": 0}

    trans = split_paragraphs(text)
    if source_paragraphs and len(trans) != len(source_paragraphs):
        warnings.append(
            f"段落数不一致：原文 {len(source_paragraphs)} 段，译文 {len(trans)} 段"
        )
    return {"error": None, "warnings": warnings, "paragraph_count": len(trans)}

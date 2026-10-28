# -*- coding: utf-8 -*-
from __future__ import annotations

from pdf_translator.prompts import (
    BEGIN_MARK,
    END_MARK,
    build_live_prompt,
    build_user_content,
    clean_translation,
    cjk_ratio,
    validate_translation,
)


def test_clean_translation_strips_markers_bom_fences_and_labels():
    raw = (
        "﻿\x1b[31m译文：```text\n"
        f"{BEGIN_MARK}\n第一段中文。\n\n第二段中文。\n{END_MARK}\n```\x1b[0m"
    )
    assert clean_translation(raw) == "第一段中文。\n\n第二段中文。"
    assert clean_translation("   ") == ""


def test_validate_translation_errors():
    assert validate_translation(["hello"], "")["error"] == "empty_translation"
    assert validate_translation(["hello"], f"{BEGIN_MARK} x {END_MARK}")["error"] == "prompt_echo"
    long_src = ["a" * 120]
    assert validate_translation(long_src, "This is only English text.")["error"] == "no_chinese"
    assert validate_translation(["short"], "中文" * 500)["error"] == "too_long"


def test_validate_translation_paragraph_warning():
    result = validate_translation(["one", "two"], "只有一段中文。")
    assert result["error"] is None
    assert result["paragraph_count"] == 1
    assert result["warnings"]


def test_cjk_ratio_and_prompt_building():
    assert cjk_ratio("这是中文") == 1.0
    assert 0.0 < cjk_ratio("你好 world") < 1.0
    assert cjk_ratio("") == 0.0

    user = build_user_content("背景", {"Pope": "教宗"}, "上一页结尾", ["One.", "Two."])
    assert "[书籍背景] 背景" in user
    assert "Pope = 教宗" in user
    assert "[上一页译文结尾" in user
    assert "[待翻译正文]\nOne.\n\nTwo." in user

    live = build_live_prompt("", {}, "", ["Hello."])
    assert BEGIN_MARK in live and END_MARK in live
    assert "不要调用任何工具" in live

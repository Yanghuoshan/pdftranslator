# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pdf_translator.bridge import AgentBridge, AgentError, normalize_command, resolve_command
from pdf_translator.store import Store

PARAS = ["Hello world.", "Second paragraph."]


def make_bridge(store, fake_agent, mode="ok", prompt_mode="stdin",
                timeout=10.0, extra=()) -> AgentBridge:
    cfg = {
        "command": [sys.executable, str(fake_agent), mode, *extra],
        "prompt_mode": prompt_mode,
        "timeout": timeout,
        "max_concurrent": 1,
        "fallback_to_api": True,
    }
    return AgentBridge(cfg, store, log=lambda *a: None)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path, "slug")


def test_translates_via_stdin_and_extracts_markers(store, fake_agent):
    bridge = make_bridge(store, fake_agent, "ok")
    assert bridge.available() is True
    result = bridge.translate(1, PARAS, "背景", {"Pope": "教宗"})
    assert result["paragraph_count"] == 2
    assert "第 1 段测试译文" in result["text"]
    assert "第 2 段测试译文" in result["text"]
    assert "<<<" not in result["text"]
    assert store.agent_run_dir.is_dir()


def test_fenced_and_bom_output(store, fake_agent):
    fenced = make_bridge(store, fake_agent, "fenced").translate(1, PARAS, "", {})
    assert fenced["text"].startswith("第 1 段")
    assert "```" not in fenced["text"]

    bom = make_bridge(store, fake_agent, "bom").translate(1, PARAS, "", {})
    assert not bom["text"].startswith("﻿")
    assert bom["paragraph_count"] == 2


def test_prompt_modes_file_and_arg(store, fake_agent):
    file_bridge = make_bridge(
        store, fake_agent, "ok", prompt_mode="file", extra=("{prompt_file}",)
    )
    file_result = file_bridge.translate(1, PARAS, "", {})
    assert file_result["paragraph_count"] == 2
    assert list(store.agent_run_dir.glob("prompt-*.txt")) == []

    arg_bridge = make_bridge(
        store, fake_agent, "ok", prompt_mode="arg", extra=("{prompt}",)
    )
    arg_result = arg_bridge.translate(1, PARAS, "", {})
    assert arg_result["paragraph_count"] == 2


def test_empty_output_is_rejected(store, fake_agent):
    bridge = make_bridge(store, fake_agent, "empty")
    with pytest.raises(AgentError) as exc:
        bridge.translate(1, PARAS, "", {})
    assert exc.value.code == "invalid_output"


def test_prompt_echo_is_rejected(store, fake_agent):
    bridge = make_bridge(store, fake_agent, "echo")
    with pytest.raises(AgentError) as exc:
        bridge.translate(1, PARAS, "", {})
    assert exc.value.code == "invalid_output"
    assert "prompt_echo" in exc.value.message


def test_nonzero_exit(store, fake_agent):
    bridge = make_bridge(store, fake_agent, "fail")
    with pytest.raises(AgentError) as exc:
        bridge.translate(1, PARAS, "", {})
    assert exc.value.code == "agent_failed"
    assert "boom" in exc.value.message


def test_timeout_kills_process(store, fake_agent):
    bridge = make_bridge(store, fake_agent, "slow", timeout=0.5)
    with pytest.raises(AgentError) as exc:
        bridge.translate(1, PARAS, "", {})
    assert exc.value.code == "agent_timeout"
    assert bridge._procs == set()


def test_unavailable_command(store):
    bridge = AgentBridge(
        {"command": ["definitely-not-a-real-binary-xyz"], "prompt_mode": "stdin"},
        store,
        log=lambda *a: None,
    )
    assert bridge.available() is False
    with pytest.raises(AgentError) as exc:
        bridge.translate(1, PARAS, "", {})
    assert exc.value.code == "agent_unavailable"


def test_command_helpers():
    assert normalize_command(["a", "b"]) == ["a", "b"]
    assert normalize_command("") == []
    resolved = resolve_command([sys.executable])
    assert Path(resolved[0]).is_file()

# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pdf_translator.cli import main
from pdf_translator.prompts import text_hash
from pdf_translator.store import Store


@pytest.fixture
def cli(project, capsys):
    def _run(*cmd, expect=0):
        argv = [
            "--json",
            "--cache-dir", str(project.cache),
            "--config", str(project.cfg),
            *[str(x) for x in cmd],
        ]
        code = main(argv)
        out, err = capsys.readouterr()
        assert code == expect, f"exit={code} (expected {expect})\nstdout={out}\nstderr={err}"
        assert out.strip(), f"stdout 为空\nstderr={err}"
        data = json.loads(out)
        return data["data"] if data.get("ok") else data, out, err
    return _run


def translate_all(cli, project) -> dict:
    cli("init", project.pdf)
    data, _, _ = cli("next", "--count", "10")
    pages = data["pages"]
    assert [p["page"] for p in pages] == [1, 2, 4]
    for item in pages:
        text = "\n\n".join(
            f"第 {i} 段中文译文，用于端到端测试。" for i in range(1, len(item["paragraphs"]) + 1)
        )
        Path(item["draft_path"]).write_text(text, encoding="utf-8")
        cli("save", item["page"], "--from", item["draft_path"])
    return data


def test_init_status_and_json_purity(cli, project):
    data, out, err = cli("init", project.pdf)
    assert data["page_count"] == 4
    assert data["empty_pages"] == [3]
    assert data["reused"] is False
    assert out.strip().startswith("{")
    assert len(out.strip().splitlines()) == 1          # stdout 只有一个 JSON 对象
    assert "抽取正文" in err                            # 日志在 stderr

    again, _, _ = cli("init", project.pdf)
    assert again["reused"] is True
    assert again["book_id"] == data["book_id"]

    status, _, _ = cli("status")
    assert status["page_count"] == 4
    assert status["translated"] == 0
    assert status["empty_pages"] == [3]
    assert status["remaining"] == 3
    assert status["next_page"] == 1
    assert status["done"] is False


def test_next_drafts_save_and_conflict(cli, project):
    cli("init", project.pdf)
    data, _, _ = cli("next")
    page = data["pages"][0]
    assert page["page"] == 1
    assert page["paragraph_count"] == 2
    assert page["empty"] is False
    assert Path(page["draft_path"]).parent.is_dir()
    assert data["instructions"]
    assert data["context"] == ""

    Path(page["draft_path"]).write_text(
        "第一段译文，讲述基督教世界的形成。\n\n第二段译文，讲述主教与公会议。",
        encoding="utf-8",
    )
    saved, _, _ = cli("save", 1, "--from", page["draft_path"])
    assert saved["saved"][0]["saved"] is True
    assert saved["progress"]["translated"] == 1

    conflict, _, _ = cli(
        "save", 1, "--text", "完全不同的另一段中文译文。", expect=5,
    )
    assert conflict["ok"] is False
    assert conflict["error"]["code"] == "translation_exists"

    forced, _, _ = cli("save", 1, "--text", "覆盖后的中文译文。", "--force")
    assert forced["saved"][0]["saved"] is True


def test_next_skips_empty_and_include_empty(cli, project):
    cli("init", project.pdf)
    data, _, _ = cli("next", "--count", "10")
    assert [p["page"] for p in data["pages"]] == [1, 2, 4]
    assert data["remaining"] == 3

    with_empty, _, _ = cli("next", "--count", "10", "--include-empty")
    assert [p["page"] for p in with_empty["pages"]] == [1, 2, 3, 4]
    empty = with_empty["pages"][2]
    assert empty["page"] == 3 and empty["empty"] is True


def test_save_validation_empty_and_skip(cli, project):
    cli("init", project.pdf)

    bad, _, _ = cli(
        "save", 1, "--text",
        "<<<BEGIN_TRANSLATION>>>这不是合法输出", expect=5,
    )
    assert bad["error"]["code"] == "invalid_translation"

    english, _, _ = cli(
        "save", 1, "--text",
        "This is English only and definitely not a Chinese translation at all.",
        expect=5,
    )
    assert english["error"]["code"] == "invalid_translation"

    empty, _, _ = cli("save", 2, "--text", "   ", expect=5)
    assert empty["error"]["code"] == "empty_translation"

    allowed, _, _ = cli("save", 2, "--text", "   ", "--allow-empty")
    assert allowed["saved"][0]["skipped"] is True
    status, _, _ = cli("status")
    assert status["skipped"] == 1
    assert status["skipped_pages"] == [2]

    cli("unskip", 2)
    status2, _, _ = cli("status")
    assert status2["skipped"] == 0

    cli("skip", 4, "--reason", "插图页")
    status3, _, _ = cli("status")
    assert status3["skipped_pages"] == [4]


def test_save_from_json_and_page_bounds(cli, project):
    cli("init", project.pdf)
    batch = project.tmp / "batch.json"
    batch.write_text(json.dumps([
        {"page": 1, "text": "第一段中文。\n\n第二段中文。"},
        {"page": 2, "text": "第三页前的中文译文。"},
    ], ensure_ascii=False), encoding="utf-8")
    data, _, _ = cli("save", "--from-json", str(batch))
    assert len(data["saved"]) == 2
    assert data["progress"]["translated"] == 2

    out, _, _ = cli("page", 1)
    assert out["page"]["page"] == 1
    assert len(out["page"]["paragraphs"]) == 2

    for bad in (0, 5):
        err, _, _ = cli("page", bad, expect=4)
        assert err["error"]["code"] == "page_out_of_range"


def test_verify_and_export(cli, project):
    translate_all(cli, project)

    status, _, _ = cli("status")
    assert status["done"] is True
    assert status["next_page"] is None

    verify, _, _ = cli("verify", "--strict")
    assert verify["pass"] is True
    assert verify["complete"] is True
    assert verify["issues"] == 0

    out = project.tmp / "book.md"
    exported, _, _ = cli("export", "--out", str(out), "--mode", "bilingual")
    assert exported["translated"] == 3
    assert exported["missing_pages"] == []
    assert out.is_file()
    body = out.read_text("utf-8")
    assert "## Page 1" in body
    assert "第 1 段中文译文" in body
    assert "（本页无可提取文本）" in body

    exists, _, _ = cli(
        "export", "--out", str(out), "--mode", "bilingual", expect=5,
    )
    assert exists["error"]["code"] == "output_exists"

    store = Store.load_active(project.cache)
    assert store is not None
    store.save_zh(2, {
        "text": "", "skipped": False, "backend": "agent",
        "source_hash": text_hash("x"), "paragraph_count": 0,
        "source_paragraph_count": 1,
    })
    broken, _, _ = cli("verify", expect=1)
    assert broken["pass"] is False
    assert broken["empty_translations"] == [2]


def test_batch_api_mock(cli, project):
    cli("init", project.pdf)
    data, _, _ = cli("batch", "--api", "--concurrency", "2")
    assert data["translated"] == [1, 2, 4]
    assert data["failed"] == []
    status, _, _ = cli("status")
    assert status["translated"] == 3
    assert status["done"] is True


def test_paths_with_chinese_and_spaces(project, capsys):
    import shutil

    target = project.tmp / "中文 目录" / "样 书.pdf"
    target.parent.mkdir()
    shutil.copyfile(project.pdf, target)
    cache = project.tmp / "缓存 目录"

    def run(*cmd, expect=0):
        code = main([
            "--json", "--cache-dir", str(cache),
            "--config", str(project.cfg),
            *[str(x) for x in cmd],
        ])
        out, err = capsys.readouterr()
        assert code == expect, f"exit={code}\nstdout={out}\nstderr={err}"
        return json.loads(out)["data"]

    init = run("init", target)
    assert init["page_count"] == 4
    page = run("next")["pages"][0]
    draft = Path(page["draft_path"])
    assert "缓存 目录" in str(draft)
    draft.write_text("第一段中文。\n\n第二段中文。", encoding="utf-8")
    saved = run("save", 1, "--from", draft)
    assert saved["saved"][0]["saved"] is True
    assert run("status")["translated"] == 1


def test_doctor_reports_usable_mock(cli, project):
    cli("init", project.pdf)
    data, _, _ = cli("doctor")
    checks = {c["name"]: c for c in data["checks"]}
    assert checks["dependencies"]["ok"] is True
    assert checks["web_assets"]["ok"] is True
    assert checks["api"]["ok"] is True          # mock provider
    assert data["usable"] is True

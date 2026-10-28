# pdftranslator — agent 驱动的 PDF 双语翻译器

把整个文件夹丢进任意工作区，让**宿主 agent** 逐页翻译：Python 只负责 PDF 取文、分段、渲染、缓存与进度，翻译由 agent 自己的模型完成，无需 API Key。原有 OpenAI 兼容 / Anthropic 接口保留为兜底，网页阅读器也还在。

给 agent 看的完整操作手册是 [AGENTS.md](AGENTS.md)；想扩展 CLI/HTTP/UI 先读 [OVERVIEW.md](OVERVIEW.md)。本文件是给人看的快速开始。

## 快速开始（人）

```bash
cd pdftranslator
uv sync
uv run pdftranslator init book.pdf --json   # 抽取全文，建立缓存
uv run pdftranslator status --json
uv run pdftranslator serve                  # 打开网页阅读器
```

没有 uv 时：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .
pdftranslator init book.pdf
```

## 两条翻译路径

**1. 宿主 agent 逐页翻译（主路径，不需要 Key）**

```bash
pdftranslator next --json        # 取页 + 书籍背景 + 术语表 + 草稿路径
# agent 把译文写到 draft_path
pdftranslator save 6 --from .cache/<slug>/drafts/00006.txt --json
pdftranslator verify --strict --json
pdftranslator export --out book.zh.md --mode bilingual
```

agent 在对话里循环 `next → 翻译 → save`，直到 `status` 显示 `done: true`。页码一律 1-based；`--json` 时 stdout 只有一个 JSON 对象。

**2. API 兜底**

```bash
export TRANSLATE_API_KEY=sk-xxx
pdftranslator batch --api          # 命令行批量
pdftranslator serve --backend api  # 网页里自动翻译/预取/批翻
```

`backend: "auto"`（默认）时：有 `agent.command` 用 agent，否则有 Key 用 API，都没有则网页只读。

## 网页阅读器

- `serve` 默认 `http://127.0.0.1:8765/`，`T` 切中英，`←/→` 翻页。
- **agent 模式**下，切页/切中文不会自动翻译；只有按 `T` 才按需拉起一次 agent 子进程翻当前页，完成即退出，无常驻、空闲不花 token。后台批翻按钮禁用。
- **API 模式**保留自动翻译、预取与 `B` 后台批翻。
- 所有 HTTP 参数都是 1-based：`/api/page?page=6`、`/api/img?page=6`、`POST /api/translate {"page": 6}`。

## 配置

复制 `config.example.json` 为 `config.json` 后按需修改。关键字段：

```json
{
  "backend": "auto",
  "agent": {
    "command": ["claude", "-p", "--output-format", "text"],
    "prompt_mode": "stdin",
    "timeout": 180,
    "fallback_to_api": true
  },
  "books": {
    "The_Formation_of_Christendom": {
      "context": "19 世纪教会史著作……",
      "glossary": {"Christendom": "基督教世界"}
    }
  }
}
```

`agent.command` 是 argv 数组，不会拼 shell。`prompt_mode` 支持 `stdin` / `arg`（`{prompt}`）/ `file`（`{prompt_file}`），方便适配不同 CLI。想零成本验证接线，可把命令换成 `["python", "examples/adapters/echo_agent.py"]`。

## 目录

```text
pdftranslator/
  AGENTS.md                 宿主 agent 操作手册（核心）
  OVERVIEW.md               架构与扩展指南（改功能/UI 前看）
  README.md
  config.example.json
  pyproject.toml
  reader.py                 旧入口兼容 shim（等价于 serve）
  pdf_translator/
    book.py                 PDF 取文/分段/渲染
    config.py               配置与后端选择
    prompts.py              提示词、清洗、校验（CLI 与网页共用）
    store.py                缓存、进度统计、原子写
    api_translator.py       API 兜底
    bridge.py               一次性 agent 子进程
    cli.py                  agent 驱动入口
    server.py               Web 服务
    web/                    原生前端（随包分发）
  examples/adapters/echo_agent.py
  scripts/release.py         构建/审计/安装冒烟（维护者）
  tests/
```

缓存默认在 `.cache/`：`book.json`、`en.json`、逐页 `zh/00006.json`、`img/`、`drafts/`。`book_id` 由 PDF 内容哈希生成，复制文件夹不会串缓存。

## 发布（维护者）

先在 `pyproject.toml` 和 `pdf_translator/__init__.py` 同步修改版本号，然后：

```bash
uv run python scripts/release.py
```

脚本依次执行：版本一致性检查 → git 工作区检查（有改动会拒绝，可用 `--allow-dirty`）→ `pytest` → 临时目录干净构建 → wheel/sdist 内容审计（禁止 PDF、`.cache`、`config.json`、缓存/编译产物，扫描密钥）→ 写入 `dist/` 与 `SHA256SUMS` → 在全新虚拟环境安装 wheel 并跑 `doctor --json` 冒烟。产物只包含运行所需的纯净文件。

确认 `dist/` 内容后再发布（需要 `UV_PUBLISH_TOKEN`）：

```bash
uv run python scripts/release.py --publish              # PyPI
uv run python scripts/release.py --publish --test-pypi  # TestPyPI
```

发布不可撤销；`--publish` 会强制要求测试与安装冒烟不得跳过。

## 说明

- 适合有文本层的 PDF；扫描件请先 OCR。
- 每页只有一份当前译文（记录里保留 backend/provider/model）；重译加 `--force`。
- 不支持 MCP、常驻 watcher、多译文集。
- 开发：`uv sync --extra dev && uv run pytest -q`。

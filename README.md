# PDF 双语阅读器（懒加载翻译）

本地运行的中英对照阅读器：原页以图片呈现，按 `T` 一键把当前页切换成中文重排文本；
**译文按需翻译、逐页落盘**，第二次看同一页不再调用大模型。

## 快速开始

```bash
# 1) 配置翻译 API（密钥走环境变量，不落盘）
export TRANSLATE_API_KEY=sk-xxxxxx          # DeepSeek / OpenAI / 硅基流动 / Moonshot / 通义等任意 OpenAI 兼容服务
# 改别的厂商只需改 config.json 的 base_url 与 model

# 2) 启动（默认读当前目录第一个 pdf；会自动开浏览器）
python3 reader.py
# 指定书 / 端口 / 预热页数
python3 reader.py The_Formation_of_Christendom,_Volume_II_by_T._W._Allies.pdf --port 8765 --prewarm 3
```

打开 `http://127.0.0.1:8765/` ，`T` 切换中英，`←/→` 翻页，`B` 后台翻译全书。

## 工作流程

| 环节 | 行为 |
| --- | --- |
| 启动 | 后台抽取全文并落盘 `en.json`（之后秒开）；同时后台预热翻译前 3 页 |
| 翻页 | 只取当前页英文 + 渲染当前页图片（图片也落盘缓存）；不触发翻译 |
| 切中文 | 当前页无译文才调用大模型（带 loading），译完写入 `.cache/.../zh/<页>.<模型>.json` |
| 再看同一页 | 直接读缓存，**0 次 API 调用** |
| 后台批翻 | `B` 或 `/api/batch`，按并发 4 顺序翻完剩余页，可随时停止；已翻的页会跳过 |

## 目录结构

```
reader.py        启动入口 + 本地 HTTP 服务与 API
pdfsource.py     pdfplumber 取文（分段/去页码/连字符修复）、pypdfium2 渲染原页
translator.py    大模型调用（OpenAI 兼容 / Anthropic / mock）、分段、并发、落盘
store.py         config.json 加载 + 缓存目录管理
web/             前端（原生 HTML/CSS/JS，无构建）
config.json      模型、并发、术语表、按书覆盖配置
.cache/<书>/     英文文本、译文、图片缓存（换模型会自动用另一批译文文件）
```

## 配置（config.json）

```jsonc
{
  "provider": "openai",                 // openai(兼容) | anthropic | mock(离线自检)
  "base_url": "https://api.deepseek.com/v1",
  "model": "deepseek-chat",
  "api_key": "",                        // 留空则读 TRANSLATE_API_KEY，避免密钥落盘
  "concurrency": 4,                     // 后台批翻并发
  "max_chars_per_chunk": 1600,          // 超长页按段落切块翻译
  "prewarm_pages": 3,                   // 启动即翻前 N 页
  "prefetch_next_pages": 0,             // >0 则翻页时悄悄预翻后 N 页
  "render_scale": 2,
  "books": {                            // 按书名（子串匹配）覆盖上面任意字段
    "The_Formation_of_Christendom": {
      "context": "19世纪教会史著作…",     // 进入 system prompt 的书籍背景
      "glossary": { "Christendom": "基督教世界" }   // 术语表，保证译名一致
    }
  }
}
```

环境变量覆盖：`TRANSLATE_API_KEY`、`TRANSLATE_BASE_URL`、`TRANSLATE_MODEL`、`TRANSLATE_PROVIDER`、`TRANSLATE_CONCURRENCY`。

## 快捷键

`←/→` 翻页 · `PageUp/PageDown` 跳 10 页 · `Home/End` 首尾页 · `T` 中英切换 · `B` 全书批翻起停。
阅读位置、语言模式、字号存在 localStorage。

## 常用命令

```bash
python3 reader.py --provider mock --no-browser   # 不花钱自检全流程（译文是占位文本）
python3 reader.py --model gpt-4o-mini            # 临时换模型（会另起一批译文缓存）
rm -rf .cache/<书>/zh                            # 丢弃某书的全部译文
```

## 说明

- 适合**有文本层**的 PDF；扫描件需先 OCR（可在 pdfsource 的 `_extract` 处接 OCR 结果）。
- 译文质量取决于模型；`context` + `glossary` 对学术书译名一致性帮助最大。
- 每页译文按 `provider_model` 分文件保存，换模型不会覆盖旧译文。

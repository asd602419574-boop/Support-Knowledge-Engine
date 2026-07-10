# Support-Knowledge-Engine

一个仅在本机运行的产品技术支持 PDF 知识引擎。它递归扫描指定目录，使用 SHA-256 去重，保守提取文档元数据，按页保存文本，并通过 SQLite FTS5 提供可追溯到原始文件和页码的全文检索。

## 当前状态

第一阶段 MVP 已包含：

- 本地目录递归扫描与中文路径支持
- PDF SHA-256 内容去重
- PDF 标题属性及明确标签形式的元数据解析
- 未可靠识别字段统一标记为“待确认”
- SQLite 文档、页文本、导入批次和逐文件日志
- SQLite FTS5 `trigram` 全文索引，支持中文片段检索
- 文档列表、产品/类型筛选、关键词搜索、页码、详情与导入日志
- 原始 PDF 打开前哈希复核，避免链接到已变化的文件内容
- 3 份完全虚构的测试 PDF 与核心自动化测试

当前不包含爬虫、公司系统适配、登录、多租户、大模型、向量数据库或云部署。

## 技术架构

```text
本地 PDF 目录
  -> SHA-256 与 PyMuPDF 按页解析
  -> 保守元数据提取
  -> SQLite documents / pages / import_runs / import_items
  -> SQLite FTS5 页级索引
  -> Flask 本地 Web 界面 (127.0.0.1)
```

这是一个单体本地应用。SQLite 文件默认保存在 `instance/knowledge.db`；原始 PDF 只读访问，不会被复制、覆盖或修改。

## 环境要求

- Windows 10/11
- Python 3.11 或更高版本
- Python 自带的 SQLite 需要启用 FTS5（官方 Windows Python 通常已启用）

## 安装与启动（PowerShell）

```powershell
chcp 65001 > $null
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [System.Text.UTF8Encoding]::new()

py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
```

浏览器打开 <http://127.0.0.1:5000>，在“本地目录完整路径”中输入 PDF 所在目录，例如：

```text
D:\Codex\知识引擎\Support-Knowledge-Engine\sample_docs
```

服务仅绑定回环地址 `127.0.0.1`，不会监听局域网接口。

## 运行测试

仓库内已经包含 3 份虚构测试 PDF。重新生成它们需要开发依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe scripts\create_sample_pdfs.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试覆盖：

- SHA-256 稳定性与重复导入
- 显式元数据解析及“待确认”策略
- 中英文 FTS5 全文检索与筛选
- 搜索结果到 PDF 页码的映射
- 损坏 PDF 的失败清单与错误原因

## 主要目录

```text
support_knowledge_engine/  Flask 应用、导入器、数据库和检索逻辑
scripts/                   虚构测试 PDF 生成脚本
sample_docs/               3 份虚构测试 PDF
tests/                     自动化测试
instance/                  本地 SQLite 数据库（运行后生成，不入 Git）
```

## 元数据识别规则

为了避免猜测，MVP 只接受两类来源：

1. PDF 自带的标题属性；
2. PDF 前两页中明确的 `字段：值` 或 `Field: value` 标签。

发布日期必须是可解析的 ISO 日期，来源网址必须是有效的 HTTP/HTTPS URL。产品型号等字段不会从文件名或正文描述中推断。

## 已知限制

- 当前不做 OCR；纯扫描件会进入失败清单，并记录“未提取到可索引文本”。
- Web 浏览器不能安全地把任意本地文件夹路径直接交给服务端，因此 MVP 使用路径输入框指定目录。
- 对同一路径但内容已变化的 PDF，再次导入会更新其记录；同内容不同路径会按 SHA-256 识别为重复。
- 元数据暂不提供人工编辑界面，“待确认”值只能通过修正文档标签后重新导入来更新。
- 当前数据库没有迁移框架；MVP 阶段通过稳定的初始化 schema 管理。

## 下一阶段建议

优先增加“待确认元数据人工校正 + 变更审计”，然后评估 OCR 适配层。业务系统接入应保持为独立适配层，不应侵入 PDF 导入和检索核心。


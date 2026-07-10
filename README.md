# Support-Knowledge-Engine

一个仅在本机运行的产品技术支持文档知识治理工具。应用递归扫描 PDF，使用 SHA-256 去重，保守提取元数据，按页保存文本，并通过 SQLite FTS5 提供可追溯到原始文件和页码的全文检索。

第二阶段在 `v0.1.0-mvp` 基线上增加人工校正、规范产品、别名冲突、文档生命周期、不可变审计和可重复检索评测。

## 当前能力

### PDF 导入与检索

- 中文目录和中文文件名
- SHA-256 内容去重
- PyMuPDF 按页文本提取和页码映射
- SQLite FTS5 `trigram` 中文全文索引
- 产品、文档类型、生命周期和关联状态筛选
- 搜索结果显示原文片段、页码、状态和规范产品
- 打开原始 PDF 前复核文件哈希
- 解析失败、重复文件和错误原因完整记录

### 知识治理

- 文档字段保留原始提取值、人工修订值和当前生效值
- 文档详情页人工修改元数据、规范产品和生命周期
- `needs_review`、`effective`、`superseded`、`draft`、`archived` 状态
- 替代文档、生效/失效日期、固件范围、权威等级和状态备注
- 替代关系循环拦截，历史文档不删除
- 规范产品和中英文名称、缩写、常见写法管理
- 英文别名大小写无关匹配
- 跨产品别名冲突显示并暂停自动关联
- 未关联产品文档筛选
- 文档、产品和别名人工修改审计
- 审计表由数据库触发器保护，禁止普通更新和删除

## 技术架构

```text
本地 PDF 目录
  -> SHA-256 + PyMuPDF 按页解析
  -> 原始元数据 / 人工修订 / 当前生效值
  -> SQLite documents / products / product_aliases / audit_log
  -> SQLite pages / FTS5 页级索引
  -> Flask 本地 Web 界面 (127.0.0.1)
```

应用保持单体、本地、低依赖。原始 PDF 只读访问，不复制、不覆盖、不上传。

## 环境要求

- Windows 10/11
- Python 3.11 或更高版本
- Python SQLite 启用 FTS5

## 安装与启动（PowerShell）

```powershell
chcp 65001 > $null
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [System.Text.UTF8Encoding]::new()

py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:SUPPORT_KE_OPERATOR = "本地维护者"
.\.venv\Scripts\python.exe run.py
```

浏览器打开 <http://127.0.0.1:5000>。服务只绑定回环地址。

`SUPPORT_KE_OPERATOR` 是本地审计操作者名称；未设置时优先使用 Windows 用户名，无法取得用户名时使用“本地维护者”。

## 数据库迁移

数据库默认位于 `instance/knowledge.db`。启动应用、运行演示数据或评测命令时会自动执行版本化迁移：

1. `phase 1 baseline`
2. `knowledge governance and lifecycle`

迁移记录保存在 `schema_migrations`，可重复执行。第一阶段的 `documents`、`pages`、`page_fts`、导入日志和原始文件路径均保留，不要求删除或重建数据库。

建议在重要升级前复制 `instance/knowledge.db`；若数据库处于运行状态，使用 SQLite 备份 API，而不是只复制可能带 WAL 的单个文件。

## 虚构演示数据

仓库包含 5 份完全虚构的测试 PDF，原有 3 份第一阶段文档仍然保留。新增文档用于区分容易混淆的 `AeroCam Mini 2` 与 `AeroCam Pro 2`。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe scripts\create_sample_pdfs.py
.\.venv\Scripts\python.exe scripts\seed_governance_demo.py
```

演示数据会建立两个规范产品，并验证以下名称统一映射到 `AeroCam Mini 2`：

- `AeroCam Mini 2`
- `Aero Mini 2`
- `ACM2`
- `航拍迷你二代`

脚本可重复运行，不会重复创建同一产品或别名。

## 检索质量评测

评测集位于 `evals/search_cases.json`，当前包含 13 条虚构问题，覆盖中文短语、英文操作、型号、缩写、中文别名和易混淆产品。

```powershell
.\.venv\Scripts\python.exe -m support_knowledge_engine.evaluation `
  --database instance\knowledge.db `
  --cases evals\search_cases.json `
  --output reports\search-eval-baseline.md
```

报告输出目标文档命中、目标页排名、禁止产品混入和总体通过率。实现不包含针对具体问题的答案硬编码。

## 运行测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试覆盖：

- SHA-256 去重、元数据解析、FTS5 和页码映射
- 元数据修改、原始值保留和字段校验
- 产品别名匹配、英文大小写和别名冲突
- 文档替代关系、日期校验和循环拦截
- 审计写入及 append-only 保护
- 第一阶段数据库迁移和幂等执行
- `effective` 状态优先排序及历史状态筛选
- 13 条检索评测和 Markdown 报告

## 主要目录

```text
support_knowledge_engine/  Flask 应用、迁移、治理、导入和检索逻辑
scripts/                   测试 PDF 与治理演示数据脚本
sample_docs/               5 份虚构测试 PDF
evals/                     可重复运行的检索评测集
reports/                   已验证的检索评测基线
tests/                     自动化测试
instance/                  本地 SQLite 数据库（不入 Git）
```

## 已知限制

- 当前不做 OCR，纯扫描件进入失败清单。
- 产品关联只使用精确规范名称或启用别名，不做模糊猜测。
- 元数据修订暂不提供批量操作或字段级审批流。
- 规范产品名称修改后，旧标准名称不会自动删除；可作为历史别名保留。
- 当前是本地单用户工具，没有登录、多租户和云同步。

## 下一阶段建议

优先增加“待确认与别名冲突处理队列”、数据库备份/恢复入口和字段级质量规则；在有真实公开文档样本后，再评估 OCR 作为独立适配层。

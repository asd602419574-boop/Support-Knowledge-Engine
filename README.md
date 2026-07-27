# Support-Knowledge-Engine

一个仅在本机运行的产品技术支持文档知识治理工具。应用递归扫描 PDF，使用 SHA-256 去重，保守提取元数据，按页保存文本，并通过 SQLite FTS5 提供可追溯到原始文件和页码的全文检索。

`v0.3` 稳定主线已经整合 DJI 中国大陆公开资料目录适配、受控资料清单、查询规范化、检索可信状态、84 条虚构评测和 SQLite 备份恢复。所有当前稳定能力均已进入 `main`；`integration/corpus-pilot-dji-catalog` 与 `feat/corpus-pilot` 只作为历史分支保留，不是当前运行入口。

## 稳定版本与验证基线

- 当前项目版本：`0.3.1`；对应发布标签命名为 `v0.3.1-corpus-pilot`。
- `v0.3.1-corpus-pilot` 必须从包含本发布卫生修复的稳定 `main` 提交创建；`v0.3.0-corpus-pilot` 保持不可变。
- 已发布且保持不可变的稳定标签：`v0.3.0-corpus-pilot`，与其发布时的 `main` 提交一致。
- `v0.3.0` 合并基线包含 schema migration 3 和 37 项 `unittest`；`0.3.1` 增加 2 项发布元数据、favicon 与迁移路径回归测试，当前完整套件为 39 项，不改变业务能力。
- 基础虚构评测包含 13 条用例；corpus pilot 虚构评测包含 84 条用例。
- 厂商公开资料目录导入与受控 JSON 资料清单是两条独立入口，只复用底层校验、去重、解析、治理和检索能力。
- 当前仍是本地单用户工具，不包含 OCR、向量数据库、向量检索、RAG 或 LLM 自动问答。
- 仓库中的自动化测试和评测只使用虚构资料；真实资料运行记录不等同于版本化自动测试结果。

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

### 受控获取与检索可信度

- 只处理 UTF-8 JSON 清单中明确列出的单个 URL 或本地文件，不发现链接、不扫描站点
- URL 超时、有限重试、HTTP 状态、Content-Type、PDF 签名、大小上限和 SHA-256 校验
- 原始下载进入独立目录；重复哈希不重复保存；成功、失败和 dry-run 均写日志
- NFKC 全半角、英文大小写、标点空格、产品别名、故障同义词和拼写规则规范化
- 搜索日志保留原始查询、规范化查询、规则、识别产品、匹配状态和耗时
- 显示高可信、可能匹配、产品歧义、版本冲突、仅过期文档和证据不足状态
- 证据不足时不填充低相关结果；匹配状态是规则标签，不伪装成概率

## 两类资料入口

```text
路径 A：厂商公开资料目录导入
DJI manifest + 已下载公开文件目录
  -> 权威元数据映射
  -> 规范产品和 slug 别名
  -> PDF 导入与索引

路径 B：受控来源获取
JSON source manifest
  -> URL 或本地文件检查
  -> dry-run / 获取
  -> 类型、签名、大小和 SHA-256 验证
  -> 独立下载区
  -> 人工确认
  -> 普通 PDF 目录导入

共享底层：SHA-256 去重 -> PyMuPDF 按页解析 -> SQLite pages / FTS5
治理与追溯：documents / products / aliases / audit_log / source_fetches / search_logs
```

路径 A 只读取已经下载的厂商公开目录，不负责站点抓取。路径 B 只处理 JSON 中显式列出的 URL 或本地文件，不扫描站点、不发现链接，也不会自动导入获取结果。两条路径复用底层导入、去重和治理能力，但保持独立入口。

应用保持单体、本地、低依赖。目录导入对原始 PDF 只读访问；受控获取只写入独立下载区，不覆盖来源文件。

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

`SUPPORT_KE_OPERATOR` 是本地审计操作者名称；未设置时优先使用 Windows 用户名，无法取得用户名时使用“本地维护者”。测试独立数据库时可设置 `SUPPORT_KE_DATABASE`，避免改动默认 `instance/knowledge.db`。

## 数据库迁移

数据库默认位于 `instance/knowledge.db`。启动应用、运行演示数据或评测命令时会自动执行版本化迁移：

1. `phase 1 baseline`
2. `knowledge governance and lifecycle`
3. `controlled corpus acquisition and search observability`

迁移记录保存在 `schema_migrations`，可重复执行。第一阶段的 `documents`、`pages`、`page_fts`、导入日志和原始文件路径均保留，不要求删除或重建数据库。

重要升级前使用下方 `backup` 命令；它调用 SQLite 备份 API，不会遗漏 WAL 中的数据。

## 虚构演示数据

仓库包含 25 个完全虚构的 PDF 文件（23 份唯一内容、2 份不同文件名的重复内容），原有 5 份文档仍保留。语料覆盖 4 个相似产品、7 类文档类型、4 组新旧版本、双语资料和缺失元数据。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe scripts\create_sample_pdfs.py
.\.venv\Scripts\python.exe scripts\seed_governance_demo.py
```

演示数据会建立四个规范产品和四组版本替代关系，并验证以下名称统一映射到 `AeroCam Mini 2`：

- `AeroCam Mini 2`
- `Aero Mini 2`
- `ACM2`
- `航拍迷你二代`

脚本可重复运行，不会重复创建同一产品或别名。

## DJI 中国大陆公开资料目录适配

项目支持直接读取 DJI 中国大陆官网下载清单。适配器不会复制、移动或修改原始文件，而是：

- 从 `manifest.json` 读取官网标题、产品、系列、文档类型、语言、版本、发布日期和来源网址
- 为清单中的产品建立规范产品，并将产品 slug 加入别名库
- 将官网清单元数据标记为“权威来源”，同时保留原始提取值和后续人工修订能力
- 继续使用 SHA-256 去重；同一内容只建立一次全文索引，重复路径写入导入日志
- 同一 PDF 先经受控入口导入时，后续 DJI manifest 可补齐权威元数据，但不会创建第二份文档或改写原始文件
- 大于 64 MB 的 PDF 通过文件路径解析，避免在内存中复制整个大文件

使用已下载、已核验的公开资料目录执行：

```powershell
.\.venv\Scripts\python.exe scripts\import_dji_catalog.py `
  "<DJI公开资料目录>"
```

默认写入 `instance/knowledge.db`。脚本可重复运行：产品和别名不会重复创建，已导入 PDF 会按内容哈希记录为重复。

真实 manifest、真实 PDF、本地路径、文件数量和真实运行报告均不进入 Git。仓库自动化测试只使用临时目录和虚构资料，不代表真实公开语料的导入结果。

## 检索质量评测

第二阶段 13 条虚构基线保留在 `evals/search_cases.json`。第三阶段 `evals/corpus_pilot_cases.json` 包含 84 条虚构问题，覆盖缩写、混合语言、错拼、相似型号、配件、否定式、无答案和仅过期文档。

```powershell
.\.venv\Scripts\python.exe -m support_knowledge_engine.evaluation `
  --database instance\knowledge.db `
  --cases evals\corpus_pilot_cases.json `
  --output reports\corpus-pilot-evaluation.md
```

命令同时生成 Markdown 和 JSON，输出 Recall@1、Recall@3、MRR、产品串库率、过期误命中率、无答案错误返回率、别名识别率、平均耗时和失败明细。仓库中的评测报告来自虚构语料自动化运行；真实资料运行结果未纳入 Git。实现不包含具体问题答案硬编码。

## 受控资料清单

复制并编辑 `config/sources.example.json`，确认每项授权后再将 `enabled` 改为 `true`：

```powershell
# 只校验清单和远程响应，不写 PDF
.\.venv\Scripts\python.exe -m support_knowledge_engine.sources `
  --manifest config\sources.example.json --database instance\knowledge.db `
  --output-dir downloads\raw --dry-run

# 实际获取清单中启用的单个文件
.\.venv\Scripts\python.exe -m support_knowledge_engine.sources `
  --manifest config\sources.example.json --database instance\knowledge.db `
  --output-dir downloads\raw
```

程序不会访问清单之外的 URL。`downloads/` 是原始获取区，不覆盖来源文件，也不会自动进入全文索引；请人工确认后再从该目录执行普通导入。

## 备份与恢复

```powershell
.\.venv\Scripts\python.exe -m support_knowledge_engine.backup backup `
  --database instance\knowledge.db --output-dir backups
.\.venv\Scripts\python.exe -m support_knowledge_engine.backup verify-backup `
  --backup backups\knowledge-时间戳.sqlite3
.\.venv\Scripts\python.exe -m support_knowledge_engine.backup restore `
  --backup backups\knowledge-时间戳.sqlite3 --database instance\knowledge.db --confirm
```

恢复默认拒绝覆盖；`--confirm` 后仍会先自动安全备份当前数据库，完整性和迁移版本全部通过后才原子替换。

## 语料统计

```powershell
.\.venv\Scripts\python.exe -m support_knowledge_engine.corpus_report `
  --database instance\knowledge.db --markdown reports\corpus-pilot-statistics.md `
  --json reports\corpus-pilot-statistics.json
```

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
- 受控 HTTP 获取、dry-run、签名、大小限制、重复与远程更新
- 查询各类规范化规则、产品歧义和六类可信度状态
- 84 条检索评测、Markdown/JSON 指标和语料统计
- SQLite 备份、哈希校验、恢复前备份与失败保护
- DJI manifest 与 migration 3 共存、跨入口 SHA-256 去重和权威元数据补齐
- 大文件路径解析、DJI 查询日志、证据不足空结果和 v2 → v3 页码保留

## 主要目录

```text
support_knowledge_engine/  Flask 应用、迁移、治理、获取、备份和检索逻辑
config/                    受控资料清单示例与查询规则
scripts/                   测试 PDF 与治理演示数据脚本
sample_docs/               25 个虚构 PDF 文件（23 份唯一内容）
evals/                     可重复运行的检索评测集
reports/                   已验证的检索评测基线
tests/                     自动化测试
instance/                  本地 SQLite 数据库（不入 Git）
```

## 已知限制

- 当前不做 OCR，纯扫描件进入失败清单。
- 当前不包含 RAG、向量数据库、向量检索或 LLM 问答；检索基于 SQLite FTS5 和显式规则。
- 产品关联只使用精确规范名称或启用别名，不做模糊猜测。
- 查询规范化只应用显式配置规则，不做语义理解；复合否定关系能力有限。
- 受控下载当前仅支持 JSON；不提供凭据、登录、验证码处理或链接发现。
- Content-Type 缺失的服务器会被保守拒绝，即使文件签名看似 PDF。
- 元数据修订暂不提供批量操作或字段级审批流。
- 规范产品名称修改后，旧标准名称不会自动删除；可作为历史别名保留。
- 当前是本地单用户工具，没有登录、多租户和云同步。

## 下一阶段建议

先用用户明确授权的公开 PDF 清单进行小规模人工试运行，收集失败查询和扫描件比例。只有当扫描件成为主要缺口时再增加独立 OCR 适配层；只有关键词检索在真实措辞下出现稳定召回缺口时，再以可回退的混合检索实验评估向量检索。插件联动应等本地导入、备份和风险提示在真实试运行中稳定后再做。

## 版本历史

- `v0.3.0-corpus-pilot`：首次将两条资料入口、查询规范化、可信状态、84 条虚构评测和备份恢复整合到稳定主线；标签保持不可变。
- `0.3.1`：校准已合并状态说明、统一包版本并修复 `/favicon.ico`；不新增业务能力。对应发布标签命名为 `v0.3.1-corpus-pilot`，必须从包含本修复的稳定 `main` 提交创建。

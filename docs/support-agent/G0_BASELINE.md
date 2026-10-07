# G0 Baseline Freeze / Current-State Audit

- baseline SHA: `010e5cb07f541ed025f86679a9afd7d085067f68`
- branch at audit: `feat/support-knowledge-agent`
- `HEAD` 与 `origin/main` 在审计开始时一致
- 包版本: `pyproject.toml` `0.3.1`
- 发布标签: `v0.3.1-corpus-pilot` 指向同一提交
- schema: migration 3，`controlled corpus acquisition and search observability`
- 本文件是现状冻结。它不改变检索、导入、治理或界面行为。
- `eval/human-blind-round-1` 上的未提交人工评测和 retrieval experiment 不在这个 SHA 里，不能算作当前能力。

判断以可执行代码和本次重跑为准。README、发布说明和 `reports/` 只用于对照。

## 1. Repository architecture

运行入口是 `run.py`。它调用 `create_app()`，Flask 只绑定 `127.0.0.1:5000`。没有包级 `__main__.py`，也没有 JSON API。

```text
run.py
  -> support_knowledge_engine.create_app
       -> init_database / apply_migrations
       -> Blueprint routes
            -> repository / governance / importer
```

可执行模块入口：

| 命令 | 代码 | 作用 |
|---|---|---|
| `python run.py` | `run.py` | 本地 HTML 工作台 |
| `python -m support_knowledge_engine.evaluation` | `evaluation.py` | 检索评测 |
| `python -m support_knowledge_engine.sources` | `sources.py` | 受控 PDF 获取，不建索引 |
| `python -m support_knowledge_engine.backup` | `backup.py` | SQLite 备份、校验、恢复 |
| `python -m support_knowledge_engine.corpus_report` | `corpus_report.py` | 语料计数 |
| `python scripts/seed_governance_demo.py` | `demo_data.py` | 虚构产品和替代关系 |
| `python scripts/import_dji_catalog.py` | `dji_catalog.py` + `importer.py` | 读已下载清单并导入 |

依赖只有 Flask 和 PyMuPDF。开发额外依赖 reportlab，用来生成虚构 PDF。没有 LLM、embedding 或向量库依赖。

### 真实调用链

```text
Source
  -> Ingestion
  -> Parse
  -> Page text
  -> SQLite pages + FTS5
  -> Query normalization
  -> Retrieval
  -> Ranking
  -> HTML consumer
```

没有 Answer 阶段。搜索结果是页级片段，不是生成回答。

**Source**

- 路径 A: `scripts/import_dji_catalog.py` 读取目录内已有的 `manifest.json`。它不抓站。
- 路径 B: `sources.acquire_sources` 只读取 JSON 清单里的单个 `url` 或 `local_path`。校验 PDF 签名、Content-Type、大小和 SHA-256 后写入独立目录，并记录 `source_fetches`。
- UI `POST /imports` 和 `import_directory` 扫描本地目录中的 PDF。

**Ingestion**

`importer.import_directory` 递归收集 `.pdf`，计算 SHA-256。相同哈希记为重复，不建第二份索引。目录里如果有 DJI manifest，会把清单元数据覆盖到解析结果上，并把 `authority_level` 标为 `authoritative`。产品记录本身只由 `ensure_dji_products` 创建，UI 导入不会调用它。

**Parse / chunk**

`extract_pdf` 用 PyMuPDF 按页提取文本。小于等于 64 MiB 的文件读入内存；更大的文件按路径打开。加密 PDF、零页、无文本页记入 `import_items` 失败。没有 OCR，没有滑动窗口，没有段落切块。一页就是一条 `pages` 记录和一条 `page_fts` 记录。

`metadata.parse_metadata` 只读前两页里的显式标签和 PDF title。缺失值保持 `待确认`。文件名和正文不推断产品。

**Index**

`db.py` 的 `page_fts` 是独立 FTS5 虚表，`tokenize = 'trigram'`，`document_id` 和 `page_number` 不参与分词。页文本同时写在 `pages.content` 和 `page_fts`。两者没有同步触发器，只在导入时手工双写。

**Retrieval / ranking**

UI 查询走 `repository.search_with_context`：

1. `normalization.normalize_query` 做 NFKC、拉丁字母小写、`config/query_rules.json` 里的拼写和故障同义词、标点清理。
2. 启用别名按子串识别产品。唯一产品时，从检索词里去掉该别名，并把 `canonical_product_id` 当作过滤条件。多个产品则 `ambiguous_product`，直接空结果。
3. `search_documents` 在每个词长度都不少于 3 时用 FTS `MATCH` 和 `bm25`。否则退回 `LIKE`。trigram 解释了这条长度门槛。
4. 排序键依次是文档状态、文档类型是否恰好为 `service handbook`、rank、文件名、页码。`effective` 优先于 `needs_review`、`draft`、`superseded`、`archived`。
5. 再按命中状态写成六种标签：`high_confidence`、`possible_match`、`ambiguous_product`、`version_conflict`、`outdated_only`、`insufficient_evidence`。
6. 写入 `search_logs`。后续查询不读取这些日志。

`search_documents` 还有第二条别名路径：对整个查询做精确别名匹配，命中时把该产品第一页以 rank `-1000` 塞进结果。`search_with_context` 在剥离产品词之后才调用它，所以 UI 主路径通常靠产品过滤，而不是这条注入。`evaluation._legacy_case` 直接调用 `search_documents`，不经过规范化。两条路径行为不同。

`authority_level` 和 `firmware_range` 能保存、能在治理表单里改，不进入排序。

**Consumer**

`routes.py` 只渲染 HTML：`/`、`/documents/<id>`、`/documents/<id>/file`、`/products`、`/logs`、`/audit`，以及对应的表单 POST。打开 PDF 前重新计算 SHA-256，不一致返回 409。日志页显示导入记录和 `source_fetches`，不显示 `search_logs`。

### 模块

| 模块 | 实际职责 |
|---|---|
| `db.py` / `migrations.py` | 连接、WAL、外键、schema 1–3 |
| `importer.py` | PDF 解析、去重、双写索引、DJI 元数据覆盖 |
| `metadata.py` | 显式标签解析 |
| `dji_catalog.py` | 已下载 manifest 的产品和文档映射 |
| `sources.py` | 受控获取 |
| `governance.py` | 产品、别名、生命周期、校验、审计 |
| `normalization.py` | 查询规则和别名识别 |
| `repository.py` | 列表、检索、排序、日志写入 |
| `evaluation.py` | 两套评测和指标 |
| `corpus_report.py` | 计数报告 |
| `backup.py` | SQLite backup API、sidecar SHA-256、恢复前安全备份 |
| `demo_data.py` | 四个虚构 AeroCam 产品和四组替代关系 |
| `routes.py` / `templates/` | 本地治理界面 |

### Schema

本次在临时库执行 `init_database` 后：

- `PRAGMA integrity_check` = `ok`
- `schema_migrations` = 1, 2, 3
- 业务表: `documents`, `pages`, `page_fts`, `import_runs`, `import_items`, `products`, `product_aliases`, `document_field_values`, `audit_log`, `source_fetches`, `search_logs`
- 触发器: `audit_log_no_update`, `audit_log_no_delete`
- SQLite `3.50.4`，`ENABLE_FTS5 = 1`

文档状态是 `needs_review`、`effective`、`superseded`、`draft`、`archived`。产品状态是 `active`、`planned`、`inactive`、`archived`。别名类型是 `official_name`、`english_name`、`chinese_name`、`abbreviation`、`common`。

### 已有能力、债务和耦合

已有并且应保留：

- 页级 FTS5 trigram、页码和原文片段
- SHA-256 去重，以及打开 PDF 前的哈希复核
- 保守元数据、人工修订值不覆盖提取值
- 规范产品、别名冲突时停止自动选择
- 替代关系循环拦截和历史文档保留
- 六种检索状态，其中歧义和证据不足返回空结果
- append-only 审计
- migration 3、备份和恢复
- 13 条与 84 条虚构检索评测，以及指标定义

技术债务：

- `pages` 与 `page_fts` 双写，没有触发器
- `search_documents` 和 `search_with_context` 的别名语义不一致。前者精确匹配整句；后者用子串，并在过滤前删掉产品词
- `metadata_status` 仍返回中文“已索引 / 待确认”，只被 `tests/test_metadata.py` 调用。导入后的状态是英文枚举
- Service Handbook 加权只认英文字面量 `service handbook`
- `authority_level`、`firmware_range` 不参与排序
- 短词走 `LIKE`，不走 bm25
- `search_logs` 只写不读，界面也不展示
- `pyproject.toml` 配了 pytest，项目测试和依赖是 unittest
- PyMuPDF 1.28 已提示 `fitz` 导入将弃用。当前代码仍 `import fitz`

耦合最紧的位置：

- `importer.import_directory` 同时负责扫描、DJI manifest、解析、治理匹配和索引写入
- `repository.search_documents` 同时负责 FTS、LIKE、别名注入和排序
- `routes.index` 直接编排检索、冲突提示和模板
- `evaluation.py` 知道两条检索函数的差异

后续 Agent 化要新增的边界，而不是替换上述资产：

- 只读 retrieval tool，内部仍调用 `search_with_context`
- evidence packet，带文档身份、状态、权威等级、版本、页码和哈希
- decision record，把现有六种状态收成继续、弃权和冲突
- 与 `documents` 分开的 case store
- 与 `search_logs` 分开的 runtime trace
- 与现有检索集分开的 agent outcome eval

## 2. Capability inventory and classification

当前系统是 **Retrieval System**。

它高于单纯的 Knowledge Search：有产品别名、生命周期排序、六种规则状态、审计和可重复评测。它不是 RAG：没有任何生成步骤，结果停在页级片段。它不是 Knowledge Agent 或 Support Agent：没有循环、工具协议、计划、案例状态或支持流程。

依据：

- `routes.index` 在有查询时只调用一次 `search_with_context`，然后渲染 `index.html` 的 `result.snippet`
- 全仓库 Python 依赖和源码没有 LLM、embedding、vector、tool registry 或 planner
- README 第 15 行和发布说明写明不含 RAG / LLM。这次代码检索与该边界一致

| 能力 | 判断 | 代码证据 |
|---|---|---|
| agent loop | 不存在 | 查询路径没有重试或二次检索 |
| tool abstraction | 不存在 | 没有工具名、参数 schema 或注册表。路由直接调函数 |
| planning | 不存在 | 没有计划对象 |
| iterative retrieval | 不存在 | 一次 FTS 或一次 LIKE |
| query decomposition | 不存在 | `normalize_query` 只做替换、清理和别名剥离 |
| evidence verification | 部分 | `document_file` 复核 SHA-256；状态规则检查新旧版本是否同时命中。没有可核验的生成陈述 |
| source conflict handling | 部分 | 别名冲突返回空结果；`version_conflict` 仍返回页面并给出风险句 |
| abstention | 部分 | `ambiguous_product` 和 `insufficient_evidence` 不返回页面。`possible_match`、`outdated_only`、`version_conflict` 仍返回页面 |
| case/context state | 不存在 | schema 没有 case、session 或 customer 表 |
| memory | 不存在 | `search_logs` 不参与下一次查询 |
| support workflow | 部分 | 人工治理覆盖导入、修订、替代、别名和审计。没有工单、排查步骤或结案 |
| observability | 部分 | `import_runs`、`import_items`、`source_fetches`、`search_logs`、`audit_log`。没有运行 trace，搜索日志没有读模型 |
| agent-specific evaluation | 不存在 | `evaluation.py` 只比较目标文档、页码、排名、串库、过期误命中和是否返回了页面 |

这些规则状态不能叫做 Agent。它们是一次检索前后的 if 分支。

## 3. Baseline test and eval results

验证环境：

- 代码: `010e5cb07f541ed025f86679a9afd7d085067f68`
- Python `3.14.6`。本机没有 Python 3.11 launcher。`requires-python` 是 `>=3.11`
- Flask `3.1.3`，PyMuPDF `1.28.2`，SQLite `3.50.4`，FTS5 已启用
- 语料: 仓库 `sample_docs` 25 个虚构 PDF，由 `seed_demo_data` 导入临时库
- 评测集: `evals/search_cases.json` 13 条；`evals/corpus_pilot_cases.json` 84 条
- 评测输出写在系统临时目录，没有改仓库里的 `reports/`

### Commands

| 命令 | 结果 | 计数 / 指标 |
|---|---|---|
| `python -m compileall -q support_knowledge_engine scripts tests run.py` | PASS | 无输出，退出码 0 |
| `python -m unittest discover -s tests -v`，继承本机 `HTTP_PROXY=http://127.0.0.1:1080` | FAIL | 39 项中 36 通过、3 失败，77.204 秒 |
| 同上，并设置 `NO_PROXY=127.0.0.1,localhost` | PASS | 39/39，8.180 秒 |
| 临时库 `init_database` + `PRAGMA integrity_check` + `current_schema_version` | PASS | integrity `ok`，version 3，迁移名与代码一致 |
| `seed_demo_data` + `collect_corpus_statistics` | PASS | 见下方语料 |
| `run_evaluation(... search_cases.json ...)` | PASS | 13/13 |
| `run_evaluation(... corpus_pilot_cases.json ...)` | PASS | 84/84 |

失败的三项都是 `tests/test_sources.py` 里访问 `127.0.0.1` 临时 HTTP 服务的用例。直接探测到的 `error_reason` 是 `获取失败（已尝试 1 次）：timed out`。`urllib` 使用了 `HTTP_PROXY`。排除 loopback 后这 4 个 source 测试通过。这是本机代理环境，不是这次对业务代码的修改，也没有为了通过而改测试。

`python -m unittest` 是 README 规定的套件。没有把 pytest 当作基线，因为依赖里没有 pytest。

### Retrieval metrics

13 条旧集没有 `should_return_answer`，走 `_legacy_case`，直接调用 `search_documents`。

| 指标 | 13 条旧集 | 84 条 corpus pilot |
|---|---:|---:|
| passed / total | 13/13 | 84/84 |
| Recall@1 | 1.000 | 1.000 |
| Recall@3 | 1.000 | 1.000 |
| MRR | 1.000 | 1.000 |
| product leakage | 0.000 | 0.000 |
| outdated mis-hit | 0.000 | 0.000 |
| no-answer false return | 0.000 | 0.000 |
| alias recognition | 0.000，见注 | 1.000 |
| average search | 0.000 ms，见注 | 0.321 ms |

注：旧集把 `alias_recognized` 设为 `None`，`elapsed_ms` 设为 `0.0`。别名率的分母在没有别名用例时被 `max(1, 0)` 抬成 1，所以显示 0，不能解读成别名失败。有意义的别名率是 84 条集的 1.000。

84 条分类：`fault_without_product` 16，`product_abbreviation` 16，`chinese_english_mixed` 16，`outdated_only` 12，`no_answer` 12，其余 12 条覆盖错拼、全角、配件与主机、相似型号、多故障、否定、大小写、空白和标点。其中 `should_return_answer=false` 的有 12 条。

### Corpus and schema

临时库计数：文档 23，页 47，FTS 条目 47，产品 4，别名 16，替代关系 4，本次关联文档 20。状态是 effective 15、needs_review 4、superseded 4。重复文件 2，解析失败 0。缺失字段 5：`product_model` 1，`release_date` 1，`source_url` 2，`version` 1。数据库约 303104 字节，平均导入约 0.0070 秒/文档。

这些计数与 `reports/corpus-pilot-statistics.md` 的文档、页、产品和缺失字段一致。字节数和平均时间不同，因为那是另一次运行的产物。

真实 DJI 目录不在 Git 中。本次没有运行真实语料导入。DJI 行为只由使用临时 manifest 的 `tests/test_dji_catalog.py` 和 `tests/test_integration_compatibility.py` 覆盖。

## 4. README 与代码差异

- “两条入口独立”对获取器成立：`acquire_sources` 不调用导入。`import_directory` 却会在任何目录扫描时调用 `load_dji_catalog`。含 `manifest.json` 的目录通过 UI 导入时会套用权威元数据。产品行仍只由 `ensure_dji_products` 创建。
- `reports/search-eval-baseline.md` 是旧表格。当前 `_markdown` 一律输出第三阶段指标模板。
- `tests/test_evaluation.py` 锁定 13 条全部通过。`tests/test_corpus_pilot.py` 只要求总数不少于 80、存在 Recall@3、串库率为 0，没有锁定 84/84 或 Recall@1。本次重跑实际是 84/84。
- `reports/corpus-pilot-evaluation.md` 的通过率和质量指标与本次重跑一致。平均耗时分别是报告中的 0.370 ms 和本次的 0.321 ms。
- README 示例使用 Python 3.11。本次运行时是 3.14.6。
- 仓库没有 retrieval experiment 或 snapshot 实验框架。数据库快照只有 `backup.py`。

## 5. Gap matrix

距离 Support Knowledge Agent 的差距：

| 维度 | 标记 | 证据 |
|---|---|---|
| 1. Agent Runtime | MISSING | 没有循环、计划、步数限制或停止条件。一次 `search_with_context` 后渲染页面 |
| 2. Tool System | MISSING | `repository`、`importer`、`sources`、`backup` 是普通模块。没有工具清单、参数 schema 或权限边界 |
| 3. Retrieval | EXISTING | FTS5 trigram、别名、状态排序、过滤和 84 条评测已经可用。短词 LIKE、英文手册加权、权威等级不进排序，是债务，不是缺失整段检索 |
| 4. Evidence / Grounding | PARTIAL | 有页码、片段、来源路径和打开前哈希。没有 evidence packet，也没有对生成陈述的核验 |
| 5. Support Workflow | PARTIAL | `governance.py` 提供人工文档生命周期。没有 case 的打开、排查、结案状态机 |
| 6. Memory / Case Context | MISSING | 没有 case 表。`search_logs` 不回读 |
| 7. Evaluation | PARTIAL | 检索指标和虚构语料评测存在。没有 agent outcome 集。84 条通过率尚未被测试锁定 |
| 8. Observability / Governance | PARTIAL | 文档治理和审计触发器完整。搜索日志没有读模型，也没有 agent trace |

## 6. Risks

- 受控 URL 获取尊重 `HTTP_PROXY`。代理接管 loopback 时，现有 source 测试会超时。检索评测不依赖这项。
- 84 条评测的满分没有被断言锁住。后续改动可能在 unittest 仍通过时降低 Recall@1。
- 两条检索路径可能在后续封装时被错误合并，导致旧集和 UI 主路径行为互相污染。
- 别名子串匹配可能在更长查询里误识别产品。当前虚构集没有暴露这个问题。
- 评测语料是 23 份虚构文档。真实手册、扫描件和近名产品没有进入这个 baseline。
- `authority_level` 已入库但不影响排序。Agent 如果直接复用当前排序，权威来源不会自然靠前。
- 页文本双写。只改一边会造成片段和索引不一致。
- 本地应用没有登录。导入可以扫描调用者指定的目录，并按哈希通过后送出 PDF。这是现有单用户边界，不是本次要改的行为。
- Python 3.14 上 PyMuPDF 发出 `fitz` 弃用警告。当前测试仍通过。

没有发现阻止提交这份 baseline 的产品缺陷。代理只影响本机受控下载测试，排除 loopback 后官方 39 项测试通过。

## 7. Proposed implementation gates

这些 Gate 从现有检索、语料和评测出发。不替换 FTS，不引入 LLM，不把现有函数改名为 Agent。每一项都可以单独 commit，并用 `git revert` 回滚。未获人工审核前不进入 G1。

### G1 — Lock the retrieval baseline

- 目标: 把本次 13/13 和 84/84 的质量指标收成测试断言。
- 范围: 只改测试。不改 `search_documents` 或排序。
- 交付: `tests/test_evaluation.py` 与 `tests/test_corpus_pilot.py` 断言 passed、Recall@1、Recall@3、MRR、串库率、过期误命中率、无答案误返回率。84 条集同时断言别名成功率。
- 验收: `NO_PROXY=127.0.0.1,localhost` 时 `python -m unittest discover -s tests -v` 全部通过。
- 依赖: G0。

### G2 — Read-only retrieval tool boundary

- 目标: 给 `search_with_context` 一个稳定的只读调用结果，供以后的 runtime 使用。
- 范围: 新增适配模块和特性测试。Flask 路由继续直接调用现有函数。
- 交付: 结果至少包含原始查询、规范化查询、`match_state`、风险句、页级命中的文档 id、文件名、页码、状态和片段。
- 验收: 同一临时库上，适配结果与直接调用 `search_with_context` 一致；G1 指标不变。
- 依赖: G1。

### G3 — Evidence packet and decision record

- 目标: 把一次检索收成证据包和决定，仍不生成散文回答。
- 范围: 纯函数。`ambiguous_product` 与 `insufficient_evidence` 记为 abstain；别名冲突与 `version_conflict` 记为 conflict；`outdated_only` 记为带警告的结果；`high_confidence` 与 `possible_match` 记为带证据页的结果。
- 交付: 证据包含 SHA-256、权威等级、版本、状态、页码。决定包含原因代码。
- 验收: 六种状态各有测试；corpus pilot 的无答案误返回率保持 0，Recall@1 保持 1。
- 依赖: G2。

### G4 — Single-step runtime

- 目标: 一个 runtime 只调用一次 G2 工具，返回 G3 的决定和证据。
- 范围: 禁止第二次检索、计划器和模型调用。
- 交付: runtime 函数和调用次数测试。
- 验收: 测试证明每次运行只有一次工具调用；用 runtime 重放 84 条集时指标与 G1 相同。
- 依赖: G3。

### G5 — Case store

- 目标: 增加与文档库分离的 case 和 evidence 引用。
- 范围: migration 4。不改 `page_fts` 的分词和列。
- 交付: case 创建、证据引用、从 schema 3 升级的测试。
- 验收: 升级可重复；`backup` / `restore` 保留新表；G1 检索指标不变。
- 依赖: G3。不依赖 G4，避免数据模型和 runtime 绑死。

### G6 — Support case workflow

- 目标: 给 case 明确状态：opened、investigating、resolved、abstained。
- 范围: 状态迁移。不改文档生命周期状态机。
- 交付: 合法迁移和非法迁移测试。resolved 必须引用证据，abstained 必须引用 abstain 决定。
- 验收: 非法迁移失败；文档 `status` 不受 case 迁移影响；G1 通过。
- 依赖: G5。

### G7 — Runtime trace

- 目标: 为 G4 的一次运行写入 append-only trace。
- 范围: 新 trace 表或同等 append-only 存储。保留现有 `search_logs` 语义。
- 交付: trace 含工具名、输入查询、决定、证据 id 和耗时。
- 验收: 一次 runtime 产生一条 trace；更新和删除被拒绝；G1 通过。
- 依赖: G4。

### G8 — Agent outcome evaluation

- 目标: 新增一小份冻结评测，检查 abstain、conflict、过期警告和 case 迁移。
- 范围: 新 eval 文件和命令。不替换 `evals/corpus_pilot_cases.json`。
- 交付: 独立 pass/fail 报告。
- 验收: 新评测全部通过，同时 84 条检索评测仍是 84/84。
- 依赖: G3 和 G6。

```text
G0 -> G1 -> G2 -> G3 -> G4 -> G7
                     \-> G5 -> G6 -> G8
```

G8 同时依赖 G3 的决定记录和 G6 的流程。G5 可以在 G4 之前开始，只要 G3 已完成。

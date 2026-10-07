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

`extract_pdf` 用 PyMuPDF 按页提取文本。小于等于 64 MiB 的文件读入内存；更大的文件按路径打开。加密 PDF 和零页 PDF 导入失败。整份 PDF 所有页均无可提取文本时，导入失败；混合 PDF 中的空白页或扫描页可以作为空文本页保存。没有 OCR，没有滑动窗口，没有段落切块。一页就是一条 `pages` 记录和一条 `page_fts` 记录。

`metadata.parse_metadata` 只读前两页里的显式标签和 PDF title。缺失值保持 `待确认`。文件名和正文不推断产品。

**Index**

`db.py` 的 `page_fts` 是独立 FTS5 虚表，`tokenize = 'trigram'`，`document_id` 和 `page_number` 不参与分词。页文本同时写在 `pages.content` 和 `page_fts`。两者没有同步触发器，只在导入时手工双写。

**Retrieval / ranking**

UI 查询走 `repository.search_with_context`：

1. `normalization.normalize_query` 做 NFKC、拉丁字母小写、`config/query_rules.json` 里的拼写和故障同义词、标点清理。
2. 启用别名按子串识别产品。唯一产品时，从检索词里去掉该别名，并把 `canonical_product_id` 当作过滤条件。多个产品则 `ambiguous_product`，直接空结果。
3. `search_documents` 在每个词长度都不少于 3 时用 FTS `MATCH` 和 `bm25`。否则退回 `LIKE`。trigram 解释了这条长度门槛。两条路径的匹配语义也不同：`LIKE` 匹配完整 query substring；FTS 把 query 拆成 term，再用 `AND` 连接。`LIKE` 的 rank 固定为 0，FTS 使用 `bm25`。
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
- 短词走 `LIKE`，不走 bm25。`LIKE` 匹配完整 query substring，FTS 对拆分 term 使用 `AND`，匹配语义和排名机制都不同
- `search_logs` 只写不读，界面也不展示
- `pyproject.toml` 配了 pytest，项目测试和依赖是 unittest
- PyMuPDF 1.28 已提示 `fitz` 导入将弃用。当前代码仍 `import fitz`

耦合最紧的位置：

- `importer.import_directory` 同时负责扫描、DJI manifest、解析、治理匹配和索引写入
- `repository.search_documents` 同时负责 FTS、LIKE、别名注入和排序
- `routes.index` 直接编排检索、冲突提示和模板
- `evaluation.py` 知道两条检索函数的差异

后续 Agent 化要新增的边界，而不是替换上述资产：

- 知识资产只读的 retrieval tool。检索结果与 `search_logs` 一类 telemetry 分开
- evidence snapshot，而不是只保存 `document_id` 加页码
- 独立于六种 `match_state` 的 evidence decision。检索状态只作为 signal
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

13 条旧集没有 `should_return_answer`，走 `_legacy_case`，直接调用 `search_documents`。当前 evaluator 里的 `should_return_answer` 和 `returned_answer` 指的是是否返回检索结果页。`returned_answer` 还把 `insufficient_evidence` 视为没有返回页面。这两个字段不是生成式 Agent 的 answer。后续 Agent eval 不得复用这套命名语义。

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

84 条分类：`fault_without_product` 16，`product_abbreviation` 16，`chinese_english_mixed` 16，`outdated_only` 12，`no_answer` 12，其余 12 条覆盖错拼、全角、配件与主机、相似型号、多故障、否定、大小写、空白和标点。其中 `should_return_answer=false` 的有 12 条。这里的 `should_return_answer` 同样只表示期望是否返回检索结果页。

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

G0.2 修订 G2–G11 的 architecture contract。G1 已由双 reviewer ACCEPT，contract 保持不变。Gate 顺序不变。第 1–6 节只修正三处已确认的事实描述，基线计数和分类不变。

这些 Gate 复用现有 FTS、语料和评测资产。每一项只有一个目标，可以单独 commit。代码回滚使用 `git revert`。改 schema 的 Gate 不能把 `git revert` 或删除 migration 文件当成数据库回滚，必须遵守下文的 schema rollback contract。

分类规则：完成 G4 只得到 deterministic single-step runtime，系统仍然是 Retrieval System，不是 Agent。只有 G9 的 bounded iterative loop 实现完成，并且 G10 的 frozen、holdout、adversarial acceptance criteria 全部达到，系统分类才能升级为 Support Knowledge Agent。新评测脚本退出码为 0 本身不是升级标准。G11 不单独触发这次升级。

### Schema rollback contract

适用于任何新增或改变数据库 schema 的 Gate，包括 G5、G6，以及以后如果 G7 或 G11 增加表或列。

必须区分：

- code rollback：用 `git revert` 撤掉代码。
- schema compatibility rollback：旧代码仍能读取新 schema。
- backup/restore recovery：迁移前备份可恢复到迁移前的数据库。

每个 schema-changing Gate 必须定义并验证：

1. migration 前创建 backup。
2. forward migration 可重复执行并检查新结构。
3. 回滚时要么旧代码能读取新 schema，要么明确要求 restore 迁移前的 backup。

不允许把删除 migration 文件、丢弃 migration 函数或只还原 Git 代码写成数据库回滚方案。

### G1 — Retrieval baseline lock

- 目标: 把本次 13/13 和 84/84 的质量指标收成测试断言。
- 状态: 已完成并 ACCEPT。实现位于 `tests/test_evaluation.py` 与 `tests/test_corpus_pilot.py`，commit `d2ca51b4b622fdc12393f2d8937ea027c1dc709e`。
- 范围: 只改测试。不改 `search_documents` 或排序。
- 交付: 两个测试断言 passed、Recall@1、Recall@3、MRR、串库率。84 条集同时断言过期误命中率、无答案误返回率和别名成功率。13 条集对没有样本支撑的 evaluator 输出只作 baseline lock，不表示那些场景已被覆盖。
- 验收: `NO_PROXY=127.0.0.1,localhost` 时 `python -m unittest discover -s tests -v` 全部通过。
- 回滚: 还原这两个测试文件。
- 依赖: G0。

### G2 — Read-only retrieval tool

- 目标: 把现有检索包成稳定工具。read-only 指 Knowledge Store Read-Only，不是数据库连接严格 `query_only`。
- 范围: 新增适配模块和特性测试。Flask 路由可以继续直接调用现有函数。本 Gate 不改变排序，不实现 Agent。
- 只读边界: 工具不得修改 `documents`、`pages`、`page_fts`、product、alias、governance data 或 document lifecycle data。
- 副作用分离: 当前 `search_with_context` 会写 `search_logs`。retrieval result contract 与 telemetry/logging contract 必须分开。telemetry 写入失败不得改变 retrieval result。不得把“数据库严格 query_only”与“业务知识资产只读”混为一谈。
- 交付: versioned request schema 和 versioned response schema。contract 至少包含 `tool_name`、`tool_version`、query、product/filter parameters、max results、max snippet/content size、structured errors、timeout/error behavior，以及 observability side-effect semantics。响应仍携带规范化查询、`match_state`、风险句、文档 id、文件名、页码、状态和受长度限制的片段。
- 验收: 调用前后知识资产表的内容不变；telemetry 失败时检索结果仍一致；超限、超时和错误返回结构化错误；G1 指标不变。不得仅以“返回值与 `search_with_context` 一致”作为全部验收。
- 回滚: 删除适配模块及其测试。本 Gate 不新增 schema。
- 依赖: G1。

### G3 — Evidence packet / decision contract

- 目标: 独立建模 evidence decision，并定义可长期保存的 immutable evidence reference。仍不生成散文回答，也不改变 retrieval ranking。
- 范围: Retrieval State 不是 Evidence Decision。现有六种 `match_state` 只作为 retrieval signal，不能直接等价为回答可信度或最终 decision。
- 决策模型: 至少区分 retrieval state、decision type、reason codes、evidence applicability。
- 必须覆盖的反例: recognized product 与显式 product filter 或 evidence product 不一致时，不得产生 high-confidence evidence decision。normalization 识别出 archived 或 inactive product，不表示它仍是有效支持依据，必须检查 product lifecycle applicability。`firmware_range` 当前可保存但不参与 ranking；evidence contract 必须保留并验证 firmware/version applicability，不得仅凭页命中升级为可靠依据。`authority_level` 必须进入 evidence metadata，并在本 Gate 验证 applicability。是否改变 retrieval ranking 不属于本 Gate。`ambiguous_product`、alias conflict 和 version conflict 即使共用某个 retrieval state，也不得丢失各自的 reason code。
- Immutable evidence: case 以后长期保存的 evidence 不能只依赖 `document_id` 加 `page_number`。文档重新导入、治理修改或页面重建后，内容可能漂移。reference 必须能证明当时看到的内容。stable evidence snapshot 至少包含 document identity、PDF SHA-256、page number、source locator、supporting original text、product applicability、firmware/version applicability、decision time 的 document lifecycle state、decision time 的 authority level、retrieval/tool version 和 captured timestamp。后续文档治理变化不得静默改变这份历史依据。
- 验收: 每个反例有测试；snapshot 字段齐全；捕获后修改当前页文本、生命周期或权威等级，不改变已捕获 snapshot；G1 指标和当前排序不变。
- 回滚: 删除 decision、snapshot 函数及其测试。本 Gate 不新增 schema。
- 依赖: G2。

### G4 — Deterministic Runtime Kernel

- 目标: 提供单步、无模型的 runtime，验证 tool、evidence 和 decision contract。
- 范围: 每次运行只调用一次 G2 工具，返回 G3 的证据和决定。不接入 UI，不调用模型，不做第二次检索。
- 交付: kernel 函数，以及工具调用次数、证据字段和决定代码的合同测试。
- 验收: 测试证明没有模型调用且只有一次工具调用；用 kernel 重放 84 条集时 G1 指标不变。
- 回滚: 删除 kernel 及其测试。
- 依赖: G3。
- 分类: 本 Gate 完成以后，系统仍是 Retrieval System。

### G5 — Runtime Trace + Behavior Harness

- 目标: runtime 从第一次持久化 input/output 起就写入带 provenance 和 redaction boundary 的 append-only trace，并具备最小行为测试。
- 范围: G4 在本 Gate 之前不提供产品入口。隐私和 provenance 不得推迟到 G11。本 Gate 不改变检索排序。`search_logs` 继续只记录检索，不承担 runtime trace。
- 交付: 每次执行至少一条 trace。字段至少包括 `run_id`、`step_id`、`tool`、`input`、`output/evidence`、`decision`、`latency`、`termination_reason`、runtime version、tool version、request/response schema version 和 evidence identifiers。写入前经过 sensitive-data redaction boundary。有效保留期内拒绝普通 UPDATE 和 DELETE。
- 验收: 单步运行产生完整 trace；敏感 fixture 不以原文落库；append-only 测试失败于普通更新和删除；行为测试覆盖正常结束、工具失败和 abstain；migration 遵守 schema rollback contract；G1 通过。
- 回滚: code rollback 撤掉写入和行为测试。数据库回滚遵守 schema rollback contract，不靠删除 migration 文件。
- 依赖: G4。

### G6 — Case Store + Context

- 目标: 增加与 `documents` 和 `search_logs` 分离的 case context，并只引用 G3 的 immutable evidence。
- 范围: 新的 case 存储。不改 `page_fts` 的分词和列，不把 case 状态写入文档表或检索日志。case 不得靠回读当前 `documents` 或 `pages` 重建当时的决策依据。
- 交付: case 创建、上下文读写，以及对 immutable evidence reference 的引用。trace 可以记录 `case_id`。case 生命周期不依赖搜索日志。
- 验收: 删除或重建 `search_logs` 不影响 case；文档导入不创建 case；捕获后修改当前页文本或治理字段，case 中的 supporting text、生命周期和权威等级仍是决策当时的值；migration 前 backup、forward migration 和 schema rollback contract 均有测试；G1 通过。
- 回滚: code rollback 撤掉 case 模块。数据库要么继续让旧代码读取新 schema，要么 restore migration 前的 backup。删除 migration 文件不是回滚方案。
- 依赖: G5。

### G7 — Support Workflow

- 目标: 给 case 明确的支持流程状态：`opened`、`investigating`、`resolved`、`abstained`。
- 范围: case 状态迁移。不改文档生命周期状态机。
- 交付: 合法迁移和非法迁移测试。`resolved` 必须引用 immutable evidence，`abstained` 必须引用 abstain decision。
- 验收: 非法迁移失败；文档 `status` 不受 case 迁移影响；G1 通过。若本 Gate 增加表或列，同时遵守 schema rollback contract。
- 回滚: code rollback 删除工作流迁移函数及其测试。有 schema 变更时按 schema rollback contract 处理数据库。
- 依赖: G6。

### Untrusted knowledge boundary

PDF 和 document content 属于 untrusted data，不属于 system instruction。这条边界在 G8 和 G9 实现前固定，并在这两个 Gate 的测试里体现。

模型消费 evidence 时，文档文字不得改变 system policy、tool permissions、runtime limits 或 case state rules。文档里出现“忽略之前规则”或“调用某工具”一类文字，只能作为知识内容处理。

### G8 — Model Provider Boundary

- 目标: 定义 vendor-neutral provider contract。Agent Runtime 依赖这份 contract，而不是某一实现的隐含行为。
- 范围: 本 Gate 不实现迭代循环，不接入真实付费模型，也不以“runtime 没有 import 某个厂商 SDK”作为充分验收。
- Request: 至少包含 run context、current observation、available tools、evidence summary、budgets 和 deadline。system policy 与 untrusted evidence 分开传递。
- Response: 必须是结构化结果，至少包含 action type、tool request 或 final decision、query reformulation、reason code 和 evidence references。
- Runtime semantics: 至少定义 timeout、cancellation、provider error、malformed output、usage/token accounting，以及 model identity/version。
- 交付: 同一 contract 下两个可互换实现：deterministic/mock provider，以及第二个 test provider 或 fixture implementation。
- 验收: 两个实现对同一请求按 contract 互换后，runtime 观察到的动作、错误和用量字段一致；evidence 中的指令文字不能改变工具权限、运行限制或 case 规则；超时、取消、provider error 和 malformed output 都有测试；G1 通过。
- 回滚: 删除 provider contract 和两个测试实现，runtime 回到无模型 kernel。本 Gate 不新增 schema。
- 依赖: G5。G7 不是本 Gate 的前置条件。

### G9 — Bounded Iterative Agent Loop

- 目标: 实现 Observe → Decide → Act → Observe → Stop，支持 iterative retrieval 和 query reformulation。
- 范围: 有界循环。允许多次调用 G2 检索工具和 G8 provider。`max_steps` 与 `max_tool_calls` 单独存在不足以证明有界。
- 预算: 同时定义 per-provider-call deadline、per-tool-call deadline、total run deadline、max steps、max tool calls、retry budget 和 token/usage budget。retry 消耗预算。
- 终止原因: `completed`、`abstained`、`conflict`、`max_steps`、`max_tool_calls`、`timeout`、`budget_exhausted`、`provider_error`、`tool_error`、`invalid_action`、`no_progress`。
- No-progress: 必须定义重复保护。至少覆盖重复相同 query、evidence 没有新增，以及 provider 连续返回等价 action。达到阈值必须停止，不得继续无限 reformulation。
- 阻塞调用: provider 和 tool 的阻塞调用必须受 deadline 控制，不能只依赖循环计数。
- 交付: 循环执行器、预算记账和 trace 中的 termination reason。文档内容继续只作为 untrusted knowledge。
- 验收: 测试覆盖查询改写后的第二次检索、每一种终止原因、retry 消耗预算、阻塞调用在 deadline 到达时返回，以及 no-progress 的三类重复。文档中的“忽略规则”或“调用工具”文字不改变 policy、权限、限制或 case 规则。测试不得挂起。G1 的单次检索指标仍可通过原评测入口复现。
- 回滚: 移除循环执行器，保留 G4 单步 kernel。本 Gate 不新增 schema。
- 依赖: G7 和 G8。

### G10 — Agent Outcome Evaluation

- 目标: 用预先写明的 acceptance criteria 判断循环是否达到 Support Knowledge Agent。
- 范围: 新的 agent outcome 评测。不替换 `evals/search_cases.json` 或 `evals/corpus_pilot_cases.json`。不得复用 `should_return_answer` 或 `returned_answer` 表示生成式回答。
- 三类数据: Frozen 是固定 regression set。Holdout 是开发时 Agent 和 provider 不直接针对的独立场景。Adversarial 至少覆盖 wrong product 诱导、explicit filter 与 recognized product 冲突、archived/inactive product、firmware/version 不适用、outdated/current 文档冲突、insufficient evidence、false citation、malformed provider output、provider failure、tool failure、blocked/timeout call、repeated query / no progress，以及 document prompt injection。
- Multi-step: 至少一条行为必须同时证明第一轮 evidence 不足，Agent 根据 observation 改写 query 或 action，第二次 observation 实际不同，最终 decision 因新增 evidence 发生合理变化。如果仍不足，必须 abstain、标为 conflict，或 request clarification / safe stop，不得强行回答。
- 其他结果: 评测仍覆盖 tool selection、evidence selection、grounding、citation、abstention、conflict handling、query reformulation、termination 和 case outcome。
- 验收: frozen、holdout、adversarial 的 minimum acceptance criteria 全部达到。只要求脚本退出码为 0 不够。84 条检索评测仍是 84/84，但这只是检索回归，不是分类升级标准。
- 分类: G9 implementation complete，并且本 Gate 三类 acceptance criteria 全部达到，才允许称 Support Knowledge Agent。
- 回滚: 删除新评测文件和命令，分类保持 Retrieval System。
- 依赖: G9。

### G11 — Observability / Governance Hardening

- 目标: 把 trace、审计和治理边界补到可检查、可保留、可追溯。
- 范围: 完整的 trace inspection、privacy controls、retention、audit/governance、model provenance 和 tool provenance。不改变 G9 的循环语义，也不单独改变系统分类。G5 已经承担首次持久化时的最低 redaction 和 provenance；本 Gate 负责完整检查、保留和治理。
- append-only 与 retention: 两者不矛盾。append-only 表示在有效保留期内不能任意 UPDATE 或 DELETE 来修改历史。retention 是受治理策略控制的生命周期操作，必须产生独立 audit record，不能伪装成普通删除。
- 验收: 检查接口能按 `run_id` 读出完整 trace；保留期内普通更新和删除失败；retention 操作留下独立审计记录；敏感字段按完整规则脱敏；每条模型或工具调用带版本来源；G1 与 G10 的 acceptance criteria 仍通过。schema 变更遵守 schema rollback contract。
- 回滚: code rollback 移除加固层，保留 G5 的最小 append-only trace。数据库按 schema rollback contract 处理。
- 依赖: G10。

```text
G0 -> G1 -> G2 -> G3 -> G4 -> G5 -> G6 -> G7 -> G9 -> G10 -> G11
                                      \-> G8 ----/
```

依赖图没有变化。G8 依赖 G5，不依赖 G6 或 G7。G9 同时依赖 G7 和 G8。G4 完成不改变分类。Support Knowledge Agent 这个分类只在 G9 实现完成且 G10 的 frozen、holdout、adversarial acceptance criteria 全部达到后成立。

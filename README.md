# Support-Knowledge-Engine

[![CI](https://github.com/asd602419574-boop/Support-Knowledge-Engine/actions/workflows/ci.yml/badge.svg)](https://github.com/asd602419574-boop/Support-Knowledge-Engine/actions/workflows/ci.yml)

面向产品技术支持文档的**可解释检索系统**：本地运行，基于 SQLite FTS5，每条结果都能追溯到原始 PDF 和页码，并明确告诉你这条结果有多可信。

## 要解决的问题

支持文档的检索有三个常见陷阱，普通全文搜索对它们都没有防备：

1. **型号相近**：Mini 2 和 Mini 3 的手册措辞几乎一样，搜出来的页可能属于另一个产品。
2. **新旧版本并存**：已被替代的旧版手册仍然能被搜到，而且往往排得很靠前。
3. **答案根本不存在**：检索系统总会返回点什么，低相关的结果看起来和正确答案没有区别。

本项目的做法是：不追求"总能给出答案"，而是在每次查询后给出一个**规则化的可信状态**，在证据不足时**宁可返回空结果**。

## 它是怎么回答的

下表是在仓库自带的虚构语料（4 个相似产品、25 个 PDF）上实际运行的结果：

| 查询 | 状态 | 返回内容 |
|---|---|---|
| `ACM2 gimbal home sensor` | 高可信 | Mini 2 服务手册 v3.1 第 3 页（现行文档），产品由缩写 `ACM2` 唯一识别 |
| `航拍迷你二代，gimbal home sensor` | 高可信 | 同上，中文别名和全角标点同样识别 |
| `gimbal home sensor` | 可能匹配 | 同一页，但查询没有指明产品，所以只给到"可能" |
| `ACM2 legacy horizon drift reset` | 仅过期文档 | 服务手册 v2.0 第 2 页（已被替代），附显著的风险提示 |
| `ACM2` | 版本冲突 | 同一产品的新旧版本同时命中，提示核对替代关系 |
| `ACM2 quantum toaster error` | 证据不足 | 无结果，不用低相关页面凑数 |
| `ACM25 gimbal home sensor` | 证据不足 | `ACM25` 不是 `ACM2`，不识别产品，也不返回任何文档 |

六种状态的完整定义见 [`docs/search-confidence-rules.md`](docs/search-confidence-rules.md)。它们是可解释的规则标签，**不是概率**，也不代表答案正确率。

## 检索流程

```text
原始查询
  │  保留原文并写入 search_logs
  ▼
规范化      NFKC 全半角 → 英文小写 → 拼写纠正 → 故障同义词 → 标点空格清理
  ▼         （规则来自 config/query_rules.json，每次应用的规则都会记录）
产品识别    按整词匹配启用的别名；匹配到多个产品则判为"产品歧义"，不自动选择
  ▼
召回        词长均 ≥ 3：FTS5 trigram（各词 AND）；否则回退到 LIKE
  ▼         结果粒度为"页"，带片段、页码和文档状态
排序        文档状态（现行优先）→ 服务手册优先 → bm25 → 文件名 → 页码
  ▼
可信状态    high_confidence / possible_match / ambiguous_product
  ▼         version_conflict / outdated_only / insufficient_evidence
返回结果 + 风险提示；原始查询、规范化结果、所用规则、识别产品、状态、耗时写入日志
```

## 设计取舍

- **用规则标签，不用向量检索或 LLM 问答。** 支持场景里最贵的错误是"自信地给出错误版本"。规则标签可以被审计、被复现、被逐条解释，向量相似度做不到这一点。是否引入向量检索，应等真实语料里出现稳定的召回缺口再用可回退的实验评估。
- **证据不足时返回空。** 低相关结果会被误读为答案，所以系统不补位。
- **产品关联不做模糊猜测。** 只认规范名称和已启用的别名，且按整词匹配；两个产品共用同一个别名时暂停自动关联，交给人工解决。
- **历史文档不删除。** 旧版本保留并标记为 `superseded`，检索时降权并显示风险，而不是消失。
- **治理可追溯。** 元数据同时保留提取值、人工修订值和当前生效值；修改写入审计表，审计表由数据库触发器保护，禁止更新和删除。
- **打开原文前复核哈希。** 文件被改动或移走时拒绝展示，而不是展示一份已经对不上索引的 PDF。

## 检索质量评测

评测命令输出 Recall@1、Recall@3、MRR、产品串库率、过期误命中率、无答案错误返回率、别名识别率、平均耗时和失败明细。

| 评测集 | 用例数 | 通过 |
|---|---:|---:|
| 基础基线 `evals/search_cases.json` | 13 | 13 |
| corpus pilot `evals/corpus_pilot_cases.json` | 84 | 84 |

corpus pilot 的指标均为满分：Recall@1 / Recall@3 / MRR 为 1.000，产品串库率、过期误命中率、无答案错误返回率为 0，平均检索耗时约 0.37 ms。用例覆盖缩写、中英混合、错拼、全角、相似型号、配件与整机、否定式、无答案和仅过期文档。完整报告见 [`reports/corpus-pilot-evaluation.md`](reports/corpus-pilot-evaluation.md)。

**这些分数应该怎么读：**

- 语料和用例都是**虚构、人工编写**的，满分说明当前实现没有回归，**不说明**它在真实文档上同样好。真实资料的试运行结果没有进入 Git。
- **端到端评测有盲区。** 曾有一个 bug：别名用子串匹配，`ACM25` 会被误认成 `ACM2`，并在检索词里留下多余的 `5`。下游的全文检索恰好什么也搜不到，所以评测结果和修复前完全一致，这个 bug 是靠单元测试发现并锁定的（`tests/test_normalization.py`）。这类"中间步骤出错、最终结果碰巧正确"的问题，需要在每个环节单独写测试。

```powershell
.\.venv\Scripts\python.exe -m support_knowledge_engine.evaluation `
  --database instance\knowledge.db `
  --cases evals\corpus_pilot_cases.json `
  --output reports\corpus-pilot-evaluation.md
```

## 快速开始

环境要求：Python 3.11 或更高版本，且 Python 的 SQLite 启用 FTS5 trigram（`sqlite3.sqlite_version` ≥ 3.34）。项目主要在 Windows 上使用；在 Linux 上测试同样通过，GitHub Actions 在 Ubuntu 上运行。

**Windows（PowerShell）**

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

**Linux / macOS**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
SUPPORT_KE_OPERATOR="本地维护者" python run.py
```

浏览器打开 <http://127.0.0.1:5000>。服务只绑定回环地址。

- `SUPPORT_KE_OPERATOR`：本地审计操作者名称；未设置时优先使用系统用户名，取不到时用“本地维护者”。
- `SUPPORT_KE_DATABASE`：指定数据库路径，用于测试时避免改动默认的 `instance/knowledge.db`。
- `SUPPORT_KE_SECRET`：会话密钥，同时保护 CSRF 令牌。未设置时每次启动随机生成，所以服务重启后，已打开的页面需要刷新才能再提交表单；设置固定值可以让会话跨重启保持有效。

### 用虚构数据体验

仓库包含 25 个完全虚构的 PDF（23 份唯一内容、2 份文件名不同的重复内容），覆盖 4 个相似产品、7 类文档、4 组新旧版本、双语资料和缺失元数据。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe scripts\create_sample_pdfs.py
.\.venv\Scripts\python.exe scripts\seed_governance_demo.py
```

脚本可重复运行，不会重复创建同一产品或别名。演示数据会建立四个规范产品和四组版本替代关系，并验证 `AeroCam Mini 2`、`Aero Mini 2`、`ACM2`、`航拍迷你二代` 都映射到同一个产品。

## 资料入口

```text
路径 A：厂商公开资料目录导入
DJI manifest + 已下载公开文件目录
  -> 权威元数据映射 -> 规范产品和 slug 别名 -> PDF 导入与索引

路径 B：受控来源获取
JSON source manifest
  -> URL 或本地文件检查 -> dry-run / 获取
  -> 类型、签名、大小和 SHA-256 验证 -> 独立下载区
  -> 人工确认 -> 普通 PDF 目录导入

共享底层：SHA-256 去重 -> PyMuPDF 按页解析 -> SQLite pages / FTS5
治理与追溯：documents / products / aliases / audit_log / source_fetches / search_logs
```

两条路径只共用底层的校验、去重、解析、治理和检索能力，入口保持独立。目录导入对原始 PDF 只读；受控获取只写入独立下载区，不覆盖来源文件，也不会自动进入索引。

### 路径 A：DJI 中国大陆公开资料目录

适配器不会复制、移动或修改原始文件，而是从 `manifest.json` 读取标题、产品、系列、文档类型、语言、版本、发布日期和来源网址，为清单中的产品建立规范产品并把 slug 加入别名库，将官网元数据标记为“权威来源”，同时保留原始提取值和后续人工修订能力。同一 PDF 先经受控入口导入时，后续 DJI manifest 只补齐权威元数据，不会创建第二份文档。大于 64 MB 的 PDF 通过文件路径解析，避免在内存中整体复制。

```powershell
.\.venv\Scripts\python.exe scripts\import_dji_catalog.py `
  "<DJI公开资料目录>"
```

脚本可重复运行：产品和别名不会重复创建，已导入的 PDF 按内容哈希记为重复。真实 manifest、真实 PDF、本地路径和真实运行报告均不进入 Git。

### 路径 B：受控资料清单

只处理 UTF-8 JSON 清单中明确列出的单个 URL 或本地文件，不发现链接、不扫描站点。获取过程有超时、有限重试，并校验 HTTP 状态、Content-Type、PDF 签名、大小上限和 SHA-256。复制并编辑 `config/sources.example.json`，确认每项授权后再将 `enabled` 改为 `true`：

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

程序不会访问清单之外的 URL。人工确认后，再从 `downloads/` 目录执行普通导入。

## 运维

**数据库迁移**：数据库默认位于 `instance/knowledge.db`，启动应用、运行演示数据或评测时自动执行版本化迁移（1 基线；2 知识治理与生命周期；3 受控获取与检索可观测性），记录在 `schema_migrations`，可重复执行，不要求重建数据库。

**备份与恢复**：使用 SQLite 备份 API，不会遗漏 WAL 中的数据。恢复默认拒绝覆盖；加 `--confirm` 后仍会先自动备份当前数据库，完整性和迁移版本全部通过后才原子替换。

```powershell
.\.venv\Scripts\python.exe -m support_knowledge_engine.backup backup `
  --database instance\knowledge.db --output-dir backups
.\.venv\Scripts\python.exe -m support_knowledge_engine.backup verify-backup `
  --backup backups\knowledge-时间戳.sqlite3
.\.venv\Scripts\python.exe -m support_knowledge_engine.backup restore `
  --backup backups\knowledge-时间戳.sqlite3 --database instance\knowledge.db --confirm
```

**语料统计**：

```powershell
.\.venv\Scripts\python.exe -m support_knowledge_engine.corpus_report `
  --database instance\knowledge.db --markdown reports\corpus-pilot-statistics.md `
  --json reports\corpus-pilot-statistics.json
```

## 知识治理能力

- 文档状态：`needs_review`、`effective`、`superseded`、`draft`、`archived`；替代文档、生效/失效日期、固件范围、权威等级和状态备注
- 替代关系循环拦截
- 规范产品及中英文名称、缩写、常见写法管理；英文别名大小写无关；跨产品别名冲突显示并暂停自动关联
- 未关联产品文档筛选
- 文档、产品、别名的人工修改审计
- 中文目录和中文文件名；解析失败、重复文件和错误原因完整记录

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

共 55 项测试，每次推送和拉取请求由 GitHub Actions 在 Python 3.11 和 3.13 上运行。覆盖：SHA-256 去重、元数据与页码映射、FTS5 检索；产品别名（含整词边界和近似型号回归）、替代关系与审计保护；全部写操作路由的 CSRF 校验（缺失、错误、跨会话令牌均被拒绝，页面上的每个 POST 表单都带令牌）；迁移幂等性；受控获取的 dry-run、签名、大小限制与远程更新；查询规范化与六类可信状态；两套评测与语料统计；备份恢复；DJI 清单与跨入口去重。自动化测试和评测只使用虚构资料。

## 目录结构

```text
support_knowledge_engine/  Flask 应用、迁移、治理、获取、备份、规范化与检索逻辑
config/                    受控资料清单示例与查询规则
scripts/                   测试 PDF、评测用例生成和治理演示数据脚本
sample_docs/               25 个虚构 PDF（23 份唯一内容）
evals/                     可重复运行的检索评测集
reports/                   评测与语料统计报告（来自虚构语料）
tests/                     自动化测试
docs/                      可信度规则、受控资料说明、发布说明
instance/                  本地 SQLite 数据库（不入 Git）
```

## 已知限制

- 不做 OCR，纯扫描件进入失败清单。
- 不包含 RAG、向量检索或 LLM 问答；检索基于 FTS5 和显式规则。
- 查询规范化只应用显式配置的规则，不做语义理解；复合否定关系能力有限。
- 受控下载仅支持 JSON 清单，不处理凭据、登录或验证码，不做链接发现；Content-Type 缺失的服务器会被保守拒绝。
- 元数据修订暂无批量操作或字段级审批流；规范产品改名后旧名称不会自动删除，可作为历史别名保留。
- 本地单用户工具：没有登录、多租户和云同步。所有写操作都要求会话绑定的 CSRF 令牌，可以挡住恶意网页借浏览器向本机发请求，但这不是身份认证，因此仍只应绑定回环地址使用。
- 评测集规模小、来源虚构，端到端评测看不到中间环节的错误（见上文）。

## 下一阶段

先用用户明确授权的公开 PDF 清单做小规模人工试运行，收集失败查询和扫描件比例，并把真实措辞补进评测集。只有当扫描件成为主要缺口时才增加独立的 OCR 适配层；只有关键词检索在真实措辞下出现稳定的召回缺口时，才以可回退的混合检索实验评估向量检索。

## 版本历史

- **未发布**：产品别名改为整词匹配，修复 `ACM25`、`Mini 20` 这类近似型号被误关联的问题，新增 3 项回归测试；新增 GitHub Actions CI；为全部 6 个 POST 路由加上会话绑定的 CSRF 校验，会话密钥改为每次启动随机生成（原先是写在仓库里的固定默认值），会话 cookie 设为 `SameSite=Lax`。
- **`0.3.1`**：校准已合并状态说明、统一包版本并修复 `/favicon.ico`，不新增业务能力。对应发布标签命名为 `v0.3.1-corpus-pilot`。详见 [`docs/releases/v0.3.1.md`](docs/releases/v0.3.1.md)。
- **`v0.3.0-corpus-pilot`**：首次将两条资料入口、查询规范化、可信状态、84 条虚构评测和备份恢复整合到稳定主线；标签保持不可变。`integration/corpus-pilot-dji-catalog` 与 `feat/corpus-pilot` 仅作为历史分支保留，不是当前运行入口。

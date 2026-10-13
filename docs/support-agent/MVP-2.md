# MVP-2 中文故障解释交付

日期：2026-10-14。分支：`feat/support-agent-mvp`。
开发基线：`553cf12f3a1ea9b2cb9281cfa571a56e97f0afd6`。

## 本次功能

一次 G4 检索、G3 supported 判定后，一次模型调用生成中文事实、资料内核查动作和适用限制。
每项有完整原文对照及本地生成的 PDF 页码引用。动作单独编号，事实、标题和版本范围不冒充步骤。
没有可确认操作时明确显示缺少检查流程。页面复用 Antigravity 版本，保留状态、复制、请求快照、CSRF 和 textContent。
复制内容包括已完成的原始问题、中文解释、原文及来源，不读取用户后来未提交的输入。

模型 JSON 顶层为 `items`、`coverage` 和 `missing_information`。
每项精确包含 `type`、`text`、`evidence_id`、`quote`；最多 6 项，每项中文最多 500 字。
基础信息缺口仅允许型号、现象、固件、适用资料及具体检查流程五种代码，由后端映射为固定中文提示。
只有本次 G3 批准且实际发送的证据可被引用，quote 必须与完整摘录完全一致。
自定义 URL、文档 ID、额外字段、工具调用和未知来源拒绝，不放宽原 SHA-256 或 PDF 版本绑定。
只有故障说明且没有操作时，coverage 降为 partial，并补充检查流程缺口。

标题过滤和动作识别使用保守词法规则；明显丢失否定、条件、故障码、样本型号和版本的输出拒绝。
没有来源的拆机、刷机、更换、重置、校准、维修词句也有有限检查。
**这些规则不能证明中文语义完全正确**，不能识别所有同义词、逻辑反转或上下文错误。
模型 coverage 是资料覆盖度估计，不是确诊或维修方案验收。
异常输出安全拒答，不用另一个模型审查，也不重新检索或自动重试。
G3/G4、检索与治理、数据库 Migration 均未修改；无 Agent Loop 或 Case/Workflow 写入。

## 验证结果及实际边界

- 针对性测试 25 项通过；最终完整回归 266 项通过，退出码 0，134.827 秒。
- 假引用、截断、异常 JSON、越界字段、丢失限制、将故障含义／标题／范围标为 action 和添加无来源维修指令的拒绝路径通过。
- 产品不匹配、版本冲突、固件适用性未知保持 G3 状态，不调用模型；问答前后知识库 dump 一致。
- 浏览器核对中文分区、零虚假步骤、原文展开、实际 PDF 200/application/pdf/#page=3、脚本注入拒绝、复制绑定原问题。
- 原有 fitz 弃用提示和 socket ResourceWarning 仍出现，回归通过；记录为非阻塞限制，未扩大整改。

### 11 条人工浏览器检查

以下使用**人工编写中文响应的本机 HTTP fixture**（`TEST-FIXTURE-NOT-A-REAL-MODEL`）。
逐条对照实际虚构 PDF 页文本、条件、否定、型号和对象，验证后端与展示；
这不是 DeepSeek 生成质量或真实 API 验收，不以 HTTP 200/JSON 合法替代语义检查。

| 产品 | 输入 | 人工核对结果 |
|---|---|---|
| Mini 2 | ACM2 出现 AM2-17，应该如何检查？ | 归位传感器需要检查，非已经损坏；0 个操作；v3.1 第 3 页 |
| Mini 2 | ACM2 AM2-17 能直接更换传感器吗？ | 不提供更换步骤，明确缺检查流程 |
| Mini 2 | ACM2 AM2-17 有哪些适用限制？ | 2.4.0–2.9.x 和不得套用 Pro 2 资料均保留为 caution |
| Mini 2 | ACM2 propeller calibration | “更换后”条件保留，不变成要求先更换；v3.1 第 2 页 |
| Mini 2 | ACM2 coastal wind compensation | “启用前”检查电池锁扣的条件保留；v3.1 第 2 页 |
| Pro 2 | ACP2 出现 AP2-90，如何处理？ | 故障含义、连接器检查和不兼容 Mini 系列分开；v4.0 第 2 页 |
| Pro 2 | ACP2 payload bay connector | 检查连接器和重复光轴核验均有原文；无新增维修流程 |
| Mini 3 | ACM3 AM3-18 如何排查？ | 只有仅适用 Mini 3 的限制，没有伪造码含义或操作；v2.0 第 2 页 |
| Mini 2 | ACM2 Service Handbook | G3 conflict，未调用模型 |
| Mini 2 | ACM2 totally unknown nonexistent fault zzq991 | G3 abstain，未调用模型 |
| 选择 Mini 2 | ACP2 出现 AM2-17，应该如何检查？ | 产品过滤冲突与证据型号不匹配，未调用模型 |

本机原始检查输出保存在忽略目录 `instance/mvp2-fixture-business-review.json`。
上述是控制响应的展示和约束检查，不能证明模型会选择相关证据或生成同样的中文。

## 完整中文展示示例（HTTP fixture，非真实 DeepSeek 输出）

输入：产品 AeroCam Mini 2；`ACM2 出现 AM2-17，应该如何检查？`。

**已知故障信息**：AM2-17 表示需要检查云台归位传感器；不能据此认定该部件已经损坏。

**建议核查事项**：现有回答没有可确认的具体操作步骤，请先补充适用的检查流程资料。

**适用条件和注意事项**：

- 资料列出的支持固件范围是 2.4.0 至 2.9.x，需核对设备实际版本和适用资料。
- 不得套用为 AeroCam Pro 2 编写的操作流程。

**尚需补充的信息**：

- 现有回答尚缺具体检查流程，不能据此执行拆机或维修；请补充适用的检查流程资料。
- 请补充实际故障现象、出现时机和完整故障码。

覆盖度：partial。全部引用来自虚构 `AeroCam-Mini-2_Service-Handbook_v3.1_en-US.pdf` 第 3 页：

```text
Supported firmware range: 2.4.0 through 2.9.x.
Error code AM2-17 indicates that the gimbal home sensor needs inspection.
Do not apply procedures written for AeroCam Pro 2.
```

Evidence ID：`ev1-908e1835a4d14c5e55f3bdd82c096aee39bfd99d2cf933126b132a1917041d42`。
PDF SHA-256：`04da02cb6b80bb984deebd09fea95ffdd7da6fb941cc2dfa3e4a6729efec8072`。

## 真实 DeepSeek 状态与启动

**本次 MVP-2 的真实 DeepSeek 中文回答尚未验证**。
用户配置的 5051 服务虽然显示 deepseek-flash，但重启后的浏览器和直接 HTTP 请求仍返回旧版 HTML，
没有 MVP-2 分区。旧服务的首次尝试不计作本轮成功；未读取、记录或暴露其 Key。
用户随后明确选择“真实模型验收暂留待我操作”。MVP-1 已通过的真实验收不替代 MVP-2 的中文质量验收。

在保留本机 DeepSeek 环境变量的终端执行（不需要将 Key 发送到聊天）：

```powershell
& 'D:\Codex\知识引擎\Support-Knowledge-Engine-MVP\.venv\Scripts\python.exe' -m flask --app 'D:\Codex\知识引擎\Support-Knowledge-Engine-MVP\run.py' run --host 127.0.0.1 --port 5053
```

打开 `http://127.0.0.1:5053/assistant`，确认页面标识 MVP-2，再提交上述 AM2-17 问题。
需要模型环境变量 `SUPPORT_KE_LLM_BASE_URL=https://api.deepseek.com`、
`SUPPORT_KE_LLM_MODEL=deepseek-flash`、交互输入的 `SUPPORT_KE_LLM_API_KEY`，以及 `max_tokens` 输出限制。
完整安全 Key 输入方式见 README。需用真实生成结果逐条对照原文后再进行 ChatGPT 业务验收。

本次无已知未解决的实现 P0/P1；真实模型中文质量与稳定性未验收。
无故障码的长问句仍受既有关键词检索限制，复杂或未知的指令表达可能被词法规则保守拒绝。
仅交付本轮单次中文解释功能，HARD STOP，等待业务验收。

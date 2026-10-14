# MVP-3.2 案例感知辅助诊断验收

基线 `022392d89181025662878da266219da3b105c57c`；分支 `feat/support-agent-mvp`。沿用 V2.2 工作台，不新增案例库、会话记忆或 Agent。

## API 与数据边界

`POST /api/assistant/ask` 的原三字段请求仍可用，模型提示与输出合同保持旧模式。可选 `case_context` 只允许 `category`、`detection_records`、`completed_actions`、`pending_actions` 和必须为布尔 true 的 `consent`。

分类最多 120 字，其余每字段 1200 字；总计 3000 字、编码 JSON 9000 字节、12 条非空原文行，每行 400 字；HTTP 请求最多 20000 字节。禁止未知字段、原始工单、标识信息及明显敏感／指令样式文本。计划、建议、未完成记录不能放进检测结果或已执行栏。长度和规则均在后端校验，前端预检查只帮助用户纠正。

原始粘贴文本不发送。用户人工核对后，可勾选明确的模型发送同意；不勾选仍只填原三字段。问题、产品、固件或案例字段编辑后撤销旧确认，须重新核对，或清除本地案例使用普通问答。上下文只存在于当前页面及本次请求，不写数据库、日志、URL、Hash 或 LocalStorage。

后端逐行建立局部案例引用编号。模型仅返回 `case_assessment: [{record_id, status}]`，状态限于 recorded/pending/needs_review；禁止自定义释义、Quote、Evidence ID、来源 URL 和工具调用。必须覆盖本次全部记录，计划不能升级为 recorded。后端按编号恢复完整案例原文并生成有界核对提示。案例记录只代表案例所述，不证明设备正常或故障已排除。

PDF `items` 仍经过完整 Quote、Evidence ID、内容类型和原有中文词法／语义风险规则校验；PDF SHA-256、页码、G3/G4 型号／固件合同不变。只有 supported 调用模型。其余状态可展示案例原文，不能生成技术结论。模型可将疑似与手册不一致的记录标记为 needs_review；这一标记不是自动证明冲突或诊断，仍需人工对照手册。

## 完全虚构的实际案例

```text
产品：ACM2
故障：AM2-17 云台归位异常
检测记录：
上电后显示 AM2-17。
连接线检查未发现松脱。
已执行处理：
完成外观检查，未发现破损。
计划操作：
计划更换云台归位传感器，尚未执行。
固件：未确认
```

原样提取时，故障分类留空（未提供分类字段），固件未确认留空。仅含故障码的初次问题被既有 G3 判为 `abstain / possible_match`，没有调用模型；页面仅展示案例原文。人工在问题中补充已选型号并重新核对发送同意后，最终实际请求：

```json
{
  "query": "ACM2 出现 AM2-17，云台归位异常，应如何检查？",
  "product_id": "1",
  "firmware_version": "",
  "case_context": {
    "category": "",
    "detection_records": "上电后显示 AM2-17。\n连接线检查未发现松脱。",
    "completed_actions": "完成外观检查，未发现破损。",
    "pending_actions": "计划更换云台归位传感器，尚未执行。",
    "consent": true
  }
}
```

## 真实 DeepSeek 最终回答

用户在本机保留凭据并重启 5053 服务。最终真实 deepseek-flash 调用返回 `answered / supported`、`model_called=true`、`coverage=partial`；没有使用人工模型 Fixture。

案例来源单独展示：

| 案例完整原文 | 模型核对状态 |
| --- | --- |
| 上电后显示 AM2-17。 | recorded |
| 连接线检查未发现松脱。 | recorded |
| 完成外观检查，未发现破损。 | recorded |
| 计划更换云台归位传感器，尚未执行。 | pending |

前三条分别展示后端有界提示：“案例记录显示已有检测结果或已执行该项目，请先核对实际记录，避免机械重复；不能据此排除全部问题或认定设备正常。”

计划项提示：“案例记录为计划、建议或完成状态未确认，不代表已经执行，也不代表手册要求执行。”

PDF 中文释义（真实模型生成并通过原有校验）：

- 已知故障信息：“错误代码 AM2-17 表示云台归位传感器需要检查。”
- 适用条件：“支持的固件版本范围为 2.4.0 至 2.9.x。”
- 禁止事项：“不得套用为 AeroCam Pro 2 编写的操作流程。”
- 建议核查事项：“现有回答没有可确认的具体操作步骤，请先补充适用的检查流程资料。”（后端缺口提示，技术动作列表为空。）
- 尚需补充：“现有回答尚缺具体检查流程，不能据此执行拆机或维修；请补充适用的检查流程资料。”；“请补充实际故障现象、出现时机和完整故障码。”；“请补充设备当前固件版本，核对资料适用条件。”

对应虚构测试 PDF `AeroCam-Mini-2_Service-Handbook_v3.1_en-US.pdf` 第 3 页、文档 ID 3。每项分别绑定完整原文：

```text
Error code AM2-17 indicates that the gimbal home sensor needs inspection.
Supported firmware range: 2.4.0 through 2.9.x.
Do not apply procedures written for AeroCam Pro 2.
```

PDF SHA-256：`04da02cb6b80bb984deebd09fea95ffdd7da6fb941cc2dfa3e4a6729efec8072`。第 3 页链接实际返回 200/application/pdf。

人工检查：连接线记录没有升级为“全部连接问题已排除”；外观检查没有证明设备正常；计划没有成为已完成或手册支持的更换指令。故障对象、固件范围和禁止事项均保留。引用校验不能证明所有中文语义完全正确。

截图：[案例来源](screenshots/mvp32-case-source.png)、[PDF 依据与缺口](screenshots/mvp32-pdf-source.png)。

## 失败记录与修正

首次真实 supported 调用安全拒绝：`item_type / content_length=985`。为案例模式向模型提供既有后端分类的 excerpt_types，类型校验没有放宽。

第二次安全拒绝：`condition_missing / content_length=960`。提示明确要求使用既有规则支持的限制词和条件表达；没有记录原始响应，无法断言该次属于措辞变体还是实际限制遗漏。现有中文校验规则没有修改。修正后真实调用成功，以上为最终经过校验的结果。

## 验证与使用

专项 Python 41 项通过，包含旧 API、上下文输入边界、状态错置、敏感信息／指令、伪造案例引用、案例附加自由文本／PDF 引用拒绝、PDF 损坏／更换虚构拒绝、abstain/conflict/型号冲突/固件未知、模型错误、数据库不写入及日志不泄露。Node 本地解析与发送检查 12/12。

浏览器验证：未确认不请求；产品、问题、固件及案例记录编辑均撤销同意并拦截旧上下文；复制仍绑定已完成请求，包含两种来源和原问题，不混入后续未提交的修改。原样缺型号问题安全拒答，人工补充型号后的同一案例真实成功。

最终代码完整回归 282/282 通过（152.008 秒）；测试进程使用 `NO_PROXY=127.0.0.1,localhost`，不改变用户系统代理。既有 fitz 弃用及 socket ResourceWarning 为非阻断限制。

启动命令保持不变：

```powershell
& 'D:\Codex\知识引擎\Support-Knowledge-Engine-MVP\.venv\Scripts\python.exe' -m flask --app 'D:\Codex\知识引擎\Support-Knowledge-Engine-MVP\run.py' run --host 127.0.0.1 --port 5053
```

访问 http://127.0.0.1:5053/assistant 。上下文核对采用有限状态和后端固定提示，不生成自由形式案例病因或维修流程。确定性隐私／词法规则不能识别全部敏感信息、语义矛盾或误填记录；真实执行情况、手册适用性和最终维修判断仍须人工确认。模型输出不满足约束时继续拒绝并仅展示案例原文。只给故障码可能触发既有保守检索，必要时需人工补充型号。

完成后 HARD STOP，等待 ChatGPT 业务验收；不进入 MVP-3.3。

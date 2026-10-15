# MVP-3.3 PCN 流程合规审查

基线 `88d8052ce0a1af15f833aa06e3dc55e8089b60cb`，分支 `feat/support-agent-mvp`。

参考 TACP `f24486109a31afd24115a3a39fb772dbe54f44b1` 的 review_rules.py、review_compliance.py、schemas/review.py：采用四态、缺记录不推断实际执行、明确冲突须规则确认的边界。未移植预置 PCN 编号、正文、机型清单、FastAPI、表或前 40 字宽松校验。

## 实际使用

在原工作台点击「粘贴案例」，本地提取、编辑并人工核对后点击「确认并填入问答」。点击独立的「PCN 审查」，查看匹配的完整原文要求候选；人工阅读 PDF、确认型号、条件及义务解释，再点击「确认要求并比对执行记录」。不会自动触发审查或模型调用。

本版采用本地确定性规则，不使用 LLM。普通 `/api/assistant/ask` 后端没有改动，原有问答与 DeepSeek 保持原合同。PCN 操作不需要勾选模型发送同意；案例本地确认单独绑定当前问题、产品、固件和案例字段。模型发送同意仍仅用于普通问答。

新增只读 `POST /api/assistant/pcn-review`。首请求使用 query/product_id/firmware_version/case_context，返回 awaiting_confirmation、原文要求候选和 confirmation_token；人工确认后的第二请求附 token。两次均重新执行 G4/G3，签名绑定完整输入、完整要求、Evidence ID、页码、SHA-256 和版本。案例或来源变化返回 409，不能复用旧确认。

仅使用 G3 supported 批准的 PCN，并进一步核对 authoritative 治理等级、有效状态、明确版本、来源 URL 元数据、生效／失效日期和实际 PDF 文件哈希。保留既有固件非空范围未知的语义。PCN 缺失、过期、被替代、型号不匹配、固件或版本未知、无法识别完整要求时不建立标准检查项。来源可信度来自已有本地治理和文件身份，不代表在线核验了厂商真实性。

## 独立虚构资料

PDF：`tests/fixtures/pcn/AeroCam-Mini-2_Fictional-NTF-PCN_v1.0.pdf`，第 1 页。

完整原文要求：

```text
For AeroCam Mini 2 devices with internal storage,
after NTF capture of test photos or videos and before returning the device,
delete all test photos and videos from internal storage.
```

页面明确标记 fictional、非真实厂商标准及虚构测试文档。文件经正常 import_directory 导入，再通过已有 update_document 治理接口确认产品、来源、版本、状态和日期；没有直接制造 Evidence 或放宽 G3。

实际引用：文档 ID 1；Evidence ID `ev1-bdb27c38de21e6170ff21392bdebc26a86d5854ae73399d3f48860fcc29106dd`；PDF SHA-256 `6a4ee1d2d9af14ee1cc1404a522a3bca653c382547551a12b26a4ae3f69774c8`。浏览器 PDF 页码链接返回 200/application/pdf。

演示使用独立 `instance/pcn-browser-fixture.db`，没有将测试 PDF 自动导入正式知识库。测试脚本启动时只初始化该虚构库；审查请求本身不写入数据库。

## 完整虚构案例与实际输出

```text
产品：ACM2
故障分类：NTF
故障描述：ACM2 NTF 测试拍摄记录核对
检测记录：NTF 检测未复现故障。
已在机身存储拍摄测试图片和测试视频。
已执行处理：NTF 测试拍摄后、交还设备前，已删除机身存储中的全部测试图片和测试视频。
固件：未确认
```

人工确认后的首请求：

```json
{
  "query": "ACM2 NTF 测试拍摄记录核对",
  "product_id": "1",
  "firmware_version": "",
  "case_context": {
    "category": "NTF",
    "detection_records": "NTF 检测未复现故障。\n已在机身存储拍摄测试图片和测试视频。",
    "completed_actions": "NTF 测试拍摄后、交还设备前，已删除机身存储中的全部测试图片和测试视频。",
    "pending_actions": "",
    "consent": true
  }
}
```

这里 consent 表示向本机只读审查提交已确认字段，不授权向模型发送。第二请求附上首响应的 confirmation_token。

实际审查项：NTF 测试影像清理；节点：测试拍摄后、交还设备前；适用条件：所选型号、机身存储和 NTF 测试拍摄。状态“通过”；说明：“案例明确记录 NTF 测试拍摄后、交还前删除全部测试图片和视频，与已确认要求在记录层面匹配，不证明真实执行。”匹配原文为上述两条检测记录和完整清理记录；来源分别展示为案例记录与 PCN 原文。

其余实际浏览器状态（共同保留 NTF 与机身存储拍摄记录，并人工确认同一要求）：

| 清理记录 | 结果 | 说明 |
| --- | --- | --- |
| 无 | 待确认 | 未识别到执行记录，请人工确认；缺少记录不能证明实际执行情况。 |
| 机身存储中的测试图片和测试视频尚未删除。 | 风险提醒 | 尚不能确认节点已经到达，不能直接认定冲突。 |
| 设备已交还，机身存储中的测试图片和测试视频未删除。 | 明确冲突 | 已交还且测试影像未删除，与已确认要求的节点直接相反，仅为记录层面冲突。 |

只有计划／建议为待确认。只写“交还前已删除”但缺少测试拍摄后节点、范围不完整、案例记录互相矛盾时保留风险提醒。案例缺少 NTF 或机身存储拍摄条件时为待确认，不能套用义务。

截图：[通过及两类原文对照](screenshots/mvp33-pcn-pass.png)、[规则确认的记录层面冲突](screenshots/mvp33-pcn-conflict.png)。

## 验证与限制

PCN 专项 7 个测试方法覆盖四态、计划／建议、缺记录、条件不足、范围／节点缺失、相互矛盾、产品不适用、版本／固件未知、PCN 缺失／替代／过期、来源失效、伪造引用／义务／状态、敏感信息／注入、CSRF、非法确认参数、旧确认失效和数据库不写入。与既有案例和问答一起专项 33 项通过；最终完整回归 289/289 通过（138.619 秒）。测试进程使用 NO_PROXY 绕过 localhost 代理，不修改系统网络设置。既有 fitz 弃用和 socket ResourceWarning 非阻断。

真实浏览器确认：未核对案例不请求；未人工确认 PCN 不比对；案例修改撤销 PCN 确认并阻止请求；网络失败清空旧审查结论。网络仅访问独立 PCN API 和 PDF，不调用普通问答／DeepSeek。DeepSeek 状态在本阶段为“不参与审查”，没有使用人工模型 Fixture 冒充结果。

本版仅识别这一类明确英文义务句及少量明确中文执行记录，不是通用 PCN 规则引擎。陌生措辞、额外例外、时间线复杂或语义含糊时需要人工阅读全文，不能由规则证明义务解释的全部语义正确性。隐私检测不能识别全部敏感信息；用户仍须脱敏与人工核对。结果不能用于员工定责、绩效扣罚或自动拦截。

## 启动与访问

独立虚构库演示（当前已启动 5054）：

```powershell
Set-Location -LiteralPath 'D:\Codex\知识引擎\Support-Knowledge-Engine-MVP'
.\.venv\Scripts\python.exe -m tests.pcn_browser_fixture
```

访问 http://127.0.0.1:5054/assistant 。脚本可重复启动；有旧进程占用端口时先停止该演示服务。

正式知识库服务保持原启动方式，重启后即可看到 PCN 入口；如没有受治理的适用 PCN，只会返回待确认／无法审查。不要将测试资料作为真实厂商标准。

完成后 HARD STOP，等待 ChatGPT 业务验收，不进入新阶段。

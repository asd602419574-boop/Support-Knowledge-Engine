# MVP-2.1 校验失败修复与真实调用

日期：2026-10-14。分支：`feat/support-agent-mvp`。
基线：`3853bf183c3bd45c5dd3ea9026ce1ed123cbf666`。

## 已确认问题与修复

本地复现了词法误拒绝：忠实的“AM2-17 表示云台归位传感器需要检查，但不代表该传感器已经损坏”
被原有损坏断言规则拒绝。“并不意味着它已经损坏”也存在同样误判。
只扩展这些能够证明的否定表达识别，并将否定作用范围限定到首个损坏词及当前分句。
单独的“已经损坏”和“前句否定、后句重新断言损坏”仍被拒绝。
无来源拆机、更换、校准及其他维修操作、错误类别、伪造引用和原文不一致的拒绝条件保持不变。

旧的真实 invalid_answer 没有保留诊断或原始响应，无法恢复其精确触发类别；
不能把已确认代码缺陷宣称为那一次响应唯一已证明的原因。
修复后同一业务问题实际通过 DeepSeek，见下文。

## 内部诊断

失败时只记录固定白名单类别及必要内容长度，不记录问题、提示词、模型原始响应、客户数据、Key 或错误响应正文。
日志格式为 `assistant_validation_failed category=<固定类别> content_length=<数字或unavailable>`。
分别标识截断、finish_reason 非 stop、JSON 解析、Schema、Evidence ID、Quote、内容类型与具体词法拒绝。
未知异常细节归为 unknown，不直接记录异常携带的字符串。
浏览器默认保持通用失败提示，不返回诊断类别字段或详细诊断警告。
不增加自动重试，不提高 token 上限，不修改超时、响应大小、G3/G4 或 PDF 哈希绑定。

## 最终版本真实浏览器验证

通过用户已配置的 `http://127.0.0.1:5053/assistant` 调用 `deepseek-flash`。
未读取或暴露服务端 Key。该服务加载最终代码后，输入 AeroCam Mini 2：
`ACM2 出现 AM2-17，应该如何检查？`。

- 浏览器总等待约 3.2 秒，status=answered，G3=supported，model_called=true，coverage=partial。
- 已知信息：**错误代码 AM2-17 表示云台归位传感器需要检查。**
- 注意事项：**支持的固件版本范围为 2.4.0 至 2.9.x。**
- 禁止事项：**禁止套用为 AeroCam Pro 2 编写的操作流程。**
- 缺口：具体检查流程、实际故障现象和当前固件版本。
- 0 个 action，无损坏确诊、拆机、更换或校准指令。
- 每项 quote 均经现有完整原文一致性验证。
- 引用虚构 `AeroCam-Mini-2_Service-Handbook_v3.1_en-US.pdf` 第 3 页；实际 PDF 请求为 200/application/pdf，定位 #page=3。
- 响应不包含 validation_reason，默认不向浏览器输出诊断详情。

对应核心原文：`Error code AM2-17 indicates that the gimbal home sensor needs inspection.`。
Evidence ID：`ev1-908e1835a4d14c5e55f3bdd82c096aee39bfd99d2cf933126b132a1917041d42`。

## 测试与停止条件

针对性测试 28 项通过。最终完整回归 269 项通过、0 失败，133.304 秒。
包括否定误判回归、否定后追加肯定的拒绝、细分诊断类别、默认隐藏诊断与日志不暴露内容、原有安全及 abstain/conflict 不调用模型测试。
日志位于忽略目录 `instance/mvp21-targeted.log` 和 `instance/mvp21-regression.log`。

无已知未解决的实现 P0/P1。一次真实成功不代表所有模型输出稳定或语义均正确；
词法规则仍有能力边界，来源校验不等于中文语义证明。历史失败的唯一触发点仍不可追溯。
完成后 HARD STOP，等待 ChatGPT 验收；无 TACP UI 重构或 MVP-3。

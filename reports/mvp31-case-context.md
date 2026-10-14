# MVP-3.1 案例上下文接入验收记录

基线：79bda13972391fb319cb70d17faa48772d59595a；分支：feat/support-agent-mvp。

参考 TACP f24486109a31afd24115a3a39fb772dbe54f44b1 的 collector.js、common.js、schemas/kb.py 和 schemas/review.py 字段／分段方式；未移植 DOM 采集、Hash 传输、服务或模型封装。

在 `/assistant` 点击「粘贴案例」，确认脱敏后本地提取，编辑并核对预览，再点击「确认并填入问答」。最后由用户主动点击「生成排查建议」。没有新增数据库或接口。

## 完全虚构的浏览器测试输入

```text
产品名称：ACM2
故障分类：云台归位异常
具体故障描述：ACM2 出现 AM2-17，应该如何检查？
检测记录：上电后显示 AM2-17。
连接线检查未发现松脱。
维修记录：已完成外观检查，未发现破损。
计划更换传感器，尚未执行。
```

| 提取字段 | 本地预览 |
| --- | --- |
| 原始产品字段 | ACM2 |
| 产品建议 | AeroCam Mini 2；当前库 ID 1，必须人工确认 |
| 故障分类 | 云台归位异常 |
| 故障描述 | ACM2 出现 AM2-17，应该如何检查？ |
| 检测记录 | 上电后显示 AM2-17。 / 连接线检查未发现松脱。 |
| 已执行处理 | 已完成外观检查，未发现破损。 |
| 计划／建议／状态未确认 | 计划更换传感器，尚未执行。 |
| 固件版本 | 空 |
| 信息缺口 | 固件版本未确认，可留空；适用性仍由现有证据规则判定。 |

人工将故障描述修改为 `ACM2 出现 AM2-17，gimbal home sensor 应如何检查？` 后确认。实际问答请求只有：

```json
{"query":"ACM2 出现 AM2-17，gimbal home sensor 应如何检查？","product_id":"1","firmware_version":""}
```

浏览器监测：粘贴、提取、未确认提交、修改、确认均为 0 次问答请求；主动生成后为 1 次。原始文本确认后清空，检测／处理／分类没有进入请求。未知型号可人工选择现有产品，编辑后的描述覆盖自动提取结果。

## 真实模型结果

用户重启 5053 后，在其已配置的 deepseek-flash 服务中实际生成，返回 `status=answered`、`decision=supported`、`model_called=true`、`coverage=partial`。

- 已知故障信息：错误代码 AM2-17 表示云台归位传感器需要检查。
- 建议核查事项：现有回答没有可确认的具体操作步骤，请先补充适用的检查流程资料。
- 适用条件和注意事项：支持的固件范围为 2.4.0 至 2.9.x。禁止使用为 AeroCam Pro 2 编写的操作流程。
- 尚需补充：具体检查流程、实际故障现象／出现时机／完整故障码、当前固件版本及资料适用条件。

引用：虚构测试 PDF `AeroCam-Mini-2_Service-Handbook_v3.1_en-US.pdf` 第 3 页，文档 ID 3。原文：

```text
Error code AM2-17 indicates that the gimbal home sensor needs inspection.
Supported firmware range: 2.4.0 through 2.9.x.
Do not apply procedures written for AeroCam Pro 2.
```

PDF 打开接口实际返回 200/application/pdf，页码链接为第 3 页。没有把计划更换变成维修建议，也没有宣称部件已损坏。中文语义仍需要人工对照；引用校验不等于自动证明语义正确。

截图：[本地核对预览](screenshots/mvp31-case-preview.png)、[实际 DeepSeek 结果](screenshots/mvp31-case-result.png)。

## 验证与边界

Node 专项测试 10/10，涵盖结构化／多行记录、别名、未知／矛盾型号、空描述、已做／计划／未完成区别、隐私标识、超长输入、HTML／提示词样式文本、人工覆盖和三字段提交。Python 专项 16 项通过。

浏览器补充检查：电话和 SN 阻断、12001 字阻断、HTML 只显示为文本且没有执行、未知产品留空并可人工纠正、未核对不提交。没有新增生产测试钩子。

首次完整回归 271 项中资料下载的 3 项失败；仅为测试进程设置 `NO_PROXY=127.0.0.1,localhost` 后，资料下载专项 4/4 通过，完整回归 271/271 通过（149.747 秒）。最后补充“不确定分类／未知固件留空”规则后，本地案例专项 2 项再次通过，其中包含全部 10 条 Node 场景。存在既有 fitz 弃用及 socket ResourceWarning，不影响测试通过。

原始案例和结构化记录只在当前页面内存／DOM，不写数据库、LocalStorage、日志、URL 或 Hash；刷新／清除后不保留。仅人工确认后的三字段会通过已有问答接口送往模型。解析依赖明确字段标签，不能全面理解自由文本或识别全部隐私；处理状态判断保守，必须人工核对。检测记录不是可信 PDF 证据，也未参与本版诊断。G3/G4、模型协议、Evidence 校验、CSRF 和 PDF 治理保留。

启动：在已配置模型环境的 PowerShell 执行 `& 'D:\Codex\知识引擎\Support-Knowledge-Engine-MVP\.venv\Scripts\python.exe' -m flask --app 'D:\Codex\知识引擎\Support-Knowledge-Engine-MVP\run.py' run --host 127.0.0.1 --port 5053`，访问 http://127.0.0.1:5053/assistant。

完成后停止，等待业务验收，不进入 MVP-3.2。

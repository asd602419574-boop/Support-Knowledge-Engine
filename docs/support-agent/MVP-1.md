# Support Knowledge Agent MVP-1 交付说明

日期：2026-10-14。分支：`feat/support-agent-mvp`。
基线：`11296ac24fed8cd614ca24ce0ddb5c15d030b39d`。

## 可运行功能

- `/assistant`：产品、可选固件和自然语言问题输入，执行状态、排查清单、警告和可展开的 PDF 原文。
- `POST /api/assistant/ask`：一次 G4 检索与 G3 判定，只有可信 supported 证据才能进入一次 Chat Completions 调用。
- 后端校验完整证据语句与 Evidence ID；链接映射到本地可信文档、页码和 PDF 哈希。重新导入变化后旧引用返回 409。
- supported 与模型调用成功是独立状态；证据不足、型号不匹配、固件适用性未知和版本冲突不升级为成功。
- 本地只读问答，无 G9、多 Agent、Case 写入、Workflow 迁移、新 Schema 或底层检索改写。

## 实际验证

Python 3.12 本地环境，仓库虚构 PDF，独立演示数据库。

| 场景 | 结果 | 证据范围 |
|---|---|---|
| A 有效证据 | 通过本地 HTTP fixture 与浏览器 | 中文 AM2-17 问句 → G3 supported → 排查语句 → Mini 2 Service Handbook v3.1 第 3 页；点击实际打开 PDF |
| B 证据不足 | 通过 | abstain，无模型调用、无确定性建议 |
| C 产品／版本冲突 | 通过 | 错型号保留 abstain；Service Handbook 新旧资料命中保留 conflict；非空固件范围保持 unknown |
| D 模型失败 | 通过 HTTP 与浏览器 | 服务拒绝返回失败；真实等待约 20 秒后超时提示，按钮恢复，无假回答 |
| E 缺少配置 | 通过浏览器 | 显示缺少的环境变量；supported 检索也不会伪装模型成功 |
| 外部内容约束 | 通过 | 假 ID、编造事实、工具调用、截断响应、异常 JSON 被拒绝；浏览器脚本注入测试未执行脚本 |
| 写入边界 | 通过 | supported 请求前后 SQLite dump 一致；不保存模型原始内容或用户问题 |

针对性测试：17 项通过（`tests.test_assistant`、`tests.test_assistant_llm`）。
完整回归：258 项通过，耗时 155.094 秒；退出码 0。
首次回归 257 项中 3 项旧资料下载测试失败，原因是本机系统代理不绕过回环 HTTP。
在测试进程设置 `NO_PROXY=localhost,127.0.0.1,::1` 后，失败项针对性复核及最终完整回归通过。
没有修改旧资料下载代码或系统网络配置。两个日志位于忽略目录：
`instance/mvp-regression.log`、`instance/mvp-regression-final.log`。

**真实模型 API 的浏览器端到端调用未验证**：执行环境未提供
`SUPPORT_KE_LLM_BASE_URL`、`SUPPORT_KE_LLM_API_KEY`、`SUPPORT_KE_LLM_MODEL`。
HTTP fixture 明确使用 `TEST-FIXTURE-NOT-A-REAL-MODEL`，不计作真实模型调用通过。
真实验收还需要在本机配置受授权模型凭据，重启服务，再提交同一问题检查实际输出和引用。

## 运行与交接

实际独立工作目录：`D:\Codex\知识引擎\Support-Knowledge-Engine-MVP`。
完整安装、环境变量和 API 调用说明见根目录 README 的 MVP-1 小节。
本次本地预览使用端口 5051：

```powershell
Set-Location -LiteralPath 'D:\Codex\知识引擎\Support-Knowledge-Engine-MVP'
$env:SUPPORT_KE_DATABASE = Join-Path (Get-Location) 'instance\assistant-demo.db'
.\.venv\Scripts\python.exe -m flask --app run:app run --host 127.0.0.1 --port 5051
```

访问 `http://127.0.0.1:5051/assistant`。不配置模型时可以验证配置提示和拒答路径。
停止服务使用 Ctrl+C；设置模型变量后重新启动。

基础 UI 使用 Flask/Jinja2、现有 CSS、原生 JavaScript。
Antigravity 后续可优化 `templates/assistant.html`、`static/assistant.js` 和 CSS；
必须保留 `textContent` 安全渲染、独立 G3 判定、失败提示、CSRF、可信引用映射和 PDF 哈希绑定。
先等待 ChatGPT 业务验收，再安排 UI 优化；本次不进入 MVP-2。

## 已知边界

- 无已知未解决的实现 P0/P1；真实模型验收缺口保留为未验证，不能以 fixture 结果替代。
- 回答采用原文选取与排序，不提供未经验证的翻译或自由事实改写；英文资料保持英文。
- 故障码问题由入口提取检索词且保留所有识别产品，页面显示所用输入。其他长问句受原有关键词检索能力限制。
- 固件范围语法未实现，遵守原 G3 unknown 规则，不在 MVP-1 改写治理规则。
- 原有回归出现 PyMuPDF `fitz` 弃用提示和一条 socket ResourceWarning，测试仍通过；按非阻塞 P2/P3 记录，未扩大整改。
- 本地单用户工具，不具备公开多用户部署的认证、配额或并发治理。

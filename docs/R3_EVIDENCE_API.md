# R3 服务间证据 API v1

本功能基于 `feat/support-agent-mvp@a608d46`。`integration.py` 调用一次
`run_runtime()`，在同一只读事务中补充现有文档元数据，不另建检索引擎。
`/assistant` 和原有浏览器路由保持原行为。

每站点一套实例、独立数据库和 PDF 目录。不要把现有运行库作为测试库。
所有测试资料见 `fixtures/r3/pdfs/`，均为虚构产品、虚构 SOP/PCN。

## 启动与认证

安装 `requirements.txt`；生成测试资料另需 `requirements-dev.txt` 的 reportlab。

```powershell
python scripts/seed_r3_fixture.py --root instance/r3-site-A
python scripts/seed_r3_fixture.py --root instance/r3-site-B --empty
$env:SUPPORT_KE_DATABASE = "$PWD\instance\r3-site-A\knowledge.sqlite3"
$env:SUPPORT_KE_INTEGRATION_SITE_ID = 'fictional-site-A'
# 通过安全的服务启动环境注入 SUPPORT_KE_INTEGRATION_TOKEN；不写入 URL 或前端。
python -m flask --app 'support_knowledge_engine:create_app' run --host 127.0.0.1 --port 8137
```

Fixture 脚本拒绝覆盖已有数据库，使用实际 `import_directory()` 解析 PDF，
再经 `create_product()` / `update_document()` 设置虚构产品、版本与替代关系。
新增真实授权知识时，在独立实例使用原有 PDF 导入和来源治理页面，人工核实标题、
版本、产品关联、生命周期、日期和来源身份；不自动把资料设为厂商正式政策。

请求头 `Authorization: Bearer <server token>`。未配置服务 token/site_id 返回 503；
凭据错误返回 401；与浏览器会话和 CSRF 无关。R3 仅绑定本机或受控网络，
不提供公共互联网部署方案。测试 token 只存在于测试进程环境。

## 请求

`POST /api/integration/evidence/test`：认证后检查 DB 可读性，空库仍为连接正常。

`POST /api/integration/evidence/query`：

```json
{"contract_version":"1","site_id":"fictional-site-A","query":"星环校准","product_alias":"Aurora R3 A11","document_type":"SOP","max_results":8}
```

可选 `product_id` 为本库数字 ID 字符串，`firmware_version` 为字符串。
产品别名通过既有规范化路径处理；`max_results` 为 1–20，只限制呈现数量，
不改变整包版本冲突判定。查询最长 1000 字，其余文本最长 120 字；拒绝未知字段、
错误契约版本及其他 site_id。`site_id` 是服务端绑定校验，不是跨站点切换参数。

## 实际虚构资料响应节选

```json
{
  "contract_version":"1","status":"ok","match_state":"high_confidence",
  "decision":"supported","runtime_decision":"supported","reason_code":[],
  "items":[{
    "document_id":4,"title":"Fictional Aurora SOP 2.0","document_type":"SOP",
    "version":"2.0","lifecycle":"effective","product":"Aurora R3 A11",
    "page_number":2,"original_excerpt":"虚构资料 / Fictional test only\n共振标记：先记录指示灯颜色，再核实电池触点。\n星环校准：仅记录蓝色诊断灯闪烁次数，交由人工核实。",
    "pdf_sha256":"382d30f9e88057bfd822b89fe94f5a2a12cfe05eb1ab9ca21aaa4cce6af91aa6",
    "official_authorization":false
  }]
}
```

完整返回另含 evidence_id、source_identity、source_locator、产品生命周期、
发布日期、生效/失效日、固件适用性、已有 authority_level 和运行时 provenance。
来源 URL 不当作身份认证；以 PDF 哈希、证据绑定及服务端授权边界追溯。
缺失日期保持空值，不补造。原文为实际页文本前 4000 字，明确是片段。

| 状态 | 含义 |
|---|---|
| ok | 有原文，运行时支持；仍不等于厂商授权 |
| no_match | 无匹配页，decision 仍为 abstain |
| abstain | 有页，但适用性不足或资料过期等 |
| conflict | 版本冲突，保留运行时 reason_code |
| unavailable | DB/检索失败或超时，HTTP 503；不伪装成空结果 |
| invalid_source | 请求/证据完整性错误，HTTP 400/500 |

Runtime v1 不解析日期；API 对已过期/未生效文档保守降为 abstain，
同时保留 runtime_decision。非空固件范围仍依既有能力保留 unknown。
`supported` 不验证整步语义，也不构成厂商授权、强制维修规则或正式合规结论。

验证：`python -m pytest tests/test_integration_api_r3.py tests/test_runtime.py tests/test_evidence_decision.py tests/test_assistant.py -q`。
针对性结果 70 passed（另有 7 subtests passed）；TACP 联调脚本在对方仓库。

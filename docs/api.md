# HTTP API 接口设计（0.3 修订）

版本：v1，Base URL：`http://127.0.0.1:8000/api/v1`。当前为设计稿；接口尚未实现。首版仅本机单用户，无登录接口；不可直接暴露到公网。

## 1. 通用约定

- 请求和响应使用 JSON、UTF-8；时间为 UTC ISO 8601；ID 为 UUID 字符串。
- 成功响应直接返回资源；分页使用 `limit` 和 `offset`，默认 20，最大 100。
- 参数校验失败返回 422；不存在返回 404；状态冲突返回 409；服务未就绪返回 503。
- 错误格式：

```json
{
  "error": {
    "code": "INVALID_STATE",
    "message": "当前状态不支持恢复",
    "details": {},
    "request_id": "6b105680-4a6a-4e42-93ce-6f80ddf1b3b2"
  }
}
```

错误码包括 `VALIDATION_ERROR`、`NOT_FOUND`、`INVALID_STATE`、`CONFIGURATION_ERROR`、`SEARCH_FAILED`、`MODEL_FAILED`、`NO_EVIDENCE`、`LIMIT_REACHED`、`WORKER_INTERRUPTED`、`INTERNAL_ERROR`。创建前检测缺少必要配置时返回 503；运行时故障记录在任务中。对外错误不包含内部堆栈和密钥。

## 2. 创建任务

`POST /tasks` → `201 Created`

```json
{
  "query": "对比 Qdrant、Milvus 和 Weaviate 的许可证、部署方式和主要功能",
  "options": {"max_sources": 10}
}
```

`query` 去除首尾空白后长度 1–2000；`max_sources` 为 1–20 的整数，默认 10。搜索、模型等由服务端配置，不允许请求指定任意服务地址或密钥。

```json
{
  "id": "d595e1e9-b10b-4f15-8bdd-577ebd46b02c",
  "query": "对比 Qdrant、Milvus 和 Weaviate 的许可证、部署方式和主要功能",
  "status": "queued",
  "options": {"max_sources": 10},
  "attempt": 1,
  "cancel_requested": false,
  "created_at": "2026-11-08T08:00:00Z",
  "updated_at": "2026-11-08T08:00:00Z",
  "error": null
}
```

## 3. 查询任务

`GET /tasks?status=running&limit=20&offset=0` → `200 OK`

`status` 可省略，提供时为 `queued`、`running`、`completed`、`failed`、`cancelled` 之一。按创建时间倒序排列：

```json
{"items": [{"id": "d595e1e9-b10b-4f15-8bdd-577ebd46b02c", "query": "比较开源向量数据库", "status": "running", "attempt": 1, "created_at": "2026-11-08T08:00:00Z", "updated_at": "2026-11-08T08:01:00Z"}], "total": 1, "limit": 20, "offset": 0}
```

`GET /tasks/{task_id}` → `200 OK`，返回创建响应中的完整任务资源；`error` 失败时为 `{"code":"...","message":"..."}`，其余为 null。不存在返回 404。

## 4. 取消与恢复

`POST /tasks/{task_id}/cancel` → `202 Accepted`，返回更新后的任务。queued 立即变为 cancelled；running 设置 `cancel_requested=true`，worker 在安全检查点转为 cancelled。重复请求取消一个已 cancelled 任务返回当前任务；completed 或 failed 返回 409。

`POST /tasks/{task_id}/resume` → `202 Accepted`，仅 failed、cancelled 可用；事务性转为 queued、清除取消标记和当前错误、attempt 加一（最多 3）。配置不兼容返回 409/RESUME_INCOMPATIBLE，次数耗尽返回 409/ATTEMPT_LIMIT_REACHED。其他状态返回 409。历史步骤、事件和证据保留；恢复语义见架构文档。

## 5. 计划、步骤和事件

`GET /tasks/{task_id}/steps?limit=20&offset=0` → `200 OK`：

```json
{"items": [{"id": "55fb467f-230a-4ed1-a095-85f2965146ec", "step_key": "v1:planning", "attempt": 1, "type": "planning", "status": "completed", "input": {}, "output": {"plan_version": 1, "questions": [{"id": "q1", "text": "各项目的许可证是什么？"}], "searches": [{"question_ids": ["q1"], "query": "Qdrant license"}]}, "started_at": "2026-11-08T08:00:01Z", "finished_at": "2026-11-08T08:00:04Z"}], "total": 1, "limit": 20, "offset": 0}
```

`GET /tasks/{task_id}/events?after=0&limit=100` → `200 OK`。按 sequence 升序，`after` 为排他游标、默认 0；limit 1–100，默认 100。响应：`{"items": [...], "next_after": 2}`；无事件时 `next_after` 等于请求游标。事件结构：

```json
{"sequence": 2, "attempt": 1, "type": "task.started", "payload": {"status": "running"}, "created_at": "2026-11-08T08:00:01Z"}
```

事件类型：`task.queued`、`task.started`、`step.started`、`step.completed`、`source.collected`、`step.failed`、`step.cancelled`、`task.cancel_requested`、`task.completed`、`task.failed`、`task.cancelled`。payload 按类型记录必要的 ID、状态与脱敏摘要；事件不是页面原文的替代品。

## 6. SSE 实时事件

`GET /tasks/{task_id}/events/stream?after=0` → `200 OK`，`Content-Type: text/event-stream`。

```text
id: 12
event: step.completed
data: {"sequence":12,"attempt":1,"type":"step.completed","payload":{"step_id":"55fb467f-230a-4ed1-a095-85f2965146ec","step_key":"v1:planning","type":"planning"},"created_at":"2026-11-08T08:00:04Z"}

```

服务先重放 sequence 大于游标的持久化事件，再持续发送新事件及心跳注释；`Last-Event-ID` 存在时优先于 `after`。任务到达终态后发送最后事件并关闭，前端停止自动重连；恢复任务后前端重新连接。客户端按 `(task_id, sequence)` 去重。事件流断开期间不会丢失已持久化事件，客户端也可调用事件列表补齐。

## 7. 来源与证据

`GET /tasks/{task_id}/sources?limit=20&offset=0` → `200 OK`：

```json
{"items": [{"id": "4dadb9f8-d246-402d-a73d-03e6e03d9725", "url": "https://example.org/docs", "title": "Project documentation", "fetched_at": "2026-11-08T08:01:00Z", "status": "collected", "error": null, "requested_url": "https://example.org/docs", "final_url": "https://example.org/docs", "content_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "truncated": false}], "total": 1, "limit": 20, "offset": 0}
```

`GET /tasks/{task_id}/evidence?source_id=<uuid>&limit=20&offset=0` → `200 OK`；source_id 可选：

```json
{"items": [{"id": "ce54e100-84d0-4f48-82a7-ad3507e8d32c", "source_id": "4dadb9f8-d246-402d-a73d-03e6e03d9725", "excerpt": "示例证据片段", "locator": "License", "content_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "question_ids": ["q1"], "created_at": "2026-11-08T08:01:00Z"}], "total": 1, "limit": 20, "offset": 0}
```

## 8. 报告

`GET /tasks/{task_id}/report` → `200 OK`。尚无报告返回 404（包括任务未完成）；存在报告时返回最新版本：

```json
{"id":"047005e0-1216-432c-a892-9477d0d8c973","task_id":"d595e1e9-b10b-4f15-8bdd-577ebd46b02c","version":1,"attempt":1,"quality":"partial","markdown":"# 调研报告\n示例结论 [E:ce54e100-84d0-4f48-82a7-ad3507e8d32c]。\n\n## 信息不足\n部署方式尚未获得支持证据。","created_at":"2026-11-08T08:02:00Z"}
```

正文中的 `[E:UUID]` 与证据接口匹配，生成时校验证据属于当前任务；客户端用证据和来源接口渲染链接。

`GET /tasks/{task_id}/report/download` → `200 OK`，`Content-Type: text/markdown; charset=utf-8`，返回最新 Markdown 正文及包含证据 ID、片段和来源 URL 的附录；无报告返回 404。

## 9. 健康检查

`GET /health` → `200 OK` 返回 `{"status":"ok"}`，只表示 API 进程可响应，不代表外部模型或搜索可用。`GET /ready` 检查数据库和必要配置，成功 200 返回 `{"status":"ready"}`，否则 503 返回通用错误；不主动消耗外部模型配额。

## 10. 补充契约：幂等、能力和资源状态

- 创建任务和恢复任务支持可选 `Idempotency-Key` 请求头（1–128 个 ASCII 字符），客户端为每次用户意图生成随机 UUID，网络重试复用该 key。数据库以 `(operation, scope_id, key)` 唯一记录（创建 scope_id 固定为 create，恢复为 task_id）请求规范化哈希和原始响应，并与业务写入原子提交；同请求重试返回原状态码和响应，同 key 不同内容返回 409/IDEMPOTENCY_CONFLICT。幂等记录保留 24 小时（任务提前删除除外），过期后不保证去重。查询幂等命中先于状态、容量和恢复次数检查；重放的是旧响应快照，前端随后 GET 任务获取最新状态。
- 新建容量超限返回 429/CAPACITY_EXCEEDED；可恢复终态不代表无条件接受恢复。所有写接口只接受 JSON；取消/恢复 body 为 `{}`。不接受额外未知字段，422 错误也统一为第 1 节格式。
- 所有嵌套资源先检查 task 是否存在，再校验 source/step/evidence 所属任务。来源过滤值不属于当前任务返回 404；不泄露其他任务内容。
- 步骤 status 为 `running|completed|failed|cancelled`；type 为 `planning|search|fetch|review|write`。sources status 为 `pending|collected|failed|skipped`；失败或跳过时 error 为 `{code,message}`，fetched_at 可为 null。
- 列表 offset 不小于 0；非事件列表统一 `items/total/limit/offset`；任务列表按 `(created_at,id)` 倒序，步骤/来源/证据按创建时间和 ID 升序。offset 分页不保证并发写入下的快照一致性；实时过程依靠事件序号。
- `GET /capabilities` 返回 `{"browser":{"enabled":false,"reason":"未配置隔离环境"},"max_sources":20,"max_attempts":3}`；不返回内部地址、API Key 或敏感环境变量。
- 来源响应在第 7 节字段基础上增加 requested_url、final_url、content_hash、truncated；证据增加 content_hash、question_ids。未取得正文的来源 content_hash/final_url 可为 null，truncated=false。哈希示例仅表示 SHA-256 字段格式，不是示例文本的真实校验值。`url` 为实际采集的最终 URL，失败时回退到 requested_url。报告响应增加 attempt、quality；partial 报告正文必须明确列出缺失项。

## 11. 事件字段与重连边界

| 事件 | payload 必需字段 |
|---|---|
| task.queued / started / completed / cancelled | status；completed 另含 report_id、quality |
| task.failed | status、error（code/message） |
| task.cancel_requested | status、cancel_requested |
| step.started / completed | step_id、step_key、type |
| step.failed | step_id、step_key、type、error |
| step.cancelled | step_id、step_key、type |
| source.collected | source_id |

第 6 节为帧格式示意。所有事件包含 sequence、attempt、type、payload、created_at；queued 在创建事务内先于 started 记录。失败来源通过 step.failed 和来源列表查看，不额外依赖 source.failed 事件。

事件 after/Last-Event-ID 必须为非负整数且不超过当前最大 sequence，否则 422；Last-Event-ID 优先。初始检查错误在建立流前用普通 JSON 返回。心跳默认每 15 秒发送 `: heartbeat`，禁止缓存和代理缓冲。服务端持续按数据库 sequence 查询，避免“重放结束到订阅开始”之间丢事件。

历史 attempt 的终态事件只供展示；客户端不能收到旧终态就关闭当前 attempt 的连接。首版为手动恢复：恢复前关闭旧流，恢复后读取最新任务 attempt，再连接事件流。若连接时任务已终态且游标已消费最后事件，服务返回 HTTP 204；游标未追平时重放后关闭。前端在连接错误时先查询任务状态，确认当前 attempt 终态则主动关闭 EventSource，避免无限重连。

## 12. 运行配置、状态码和边界说明

- 所有路径均相对于 /api/v1，包括 health、ready、capabilities；ready 不保证 worker 在线或外部供应商可用。任务排队不是运行成功，界面提示“等待 worker”，不可虚构执行进度。
- query 长度按去除首尾空白后的 Unicode 码点计数；Idempotency-Key 只接受可打印非空格 ASCII。请求 Content-Type 错误返回 415/UNSUPPORTED_MEDIA_TYPE；Host/Origin 拒绝返回 403/ACCESS_DENIED；这些错误使用通用错误封装。
- 补充业务码：MODEL_OUTPUT_INVALID（任务失败）、RESUME_INCOMPATIBLE / ATTEMPT_LIMIT_REACHED / IDEMPOTENCY_CONFLICT（HTTP 409）、CAPACITY_EXCEEDED（HTTP 429）。来源级错误使用 FETCH_FAILED、URL_BLOCKED、UNSUPPORTED_CONTENT；来源失败不自动等于任务失败。
- 重复取消 running 且 cancel_requested=true 的任务返回 202 和当前资源，不重复写事件；已 cancelled 也返回 202。终态提交同时结束该 attempt 未结束的步骤，取消写 step.cancelled，异常中断写 step.failed。
- 运行时模型/搜索错误经外部重试耗尽才处理：无法完成模型阶段则 MODEL_FAILED；所有搜索调用失败且无可用证据则 SEARCH_FAILED；搜索正常但没有可支持问题的证据则 NO_EVIDENCE；部分搜索失败仍可继续。若取消已提交优先取消，其次硬预算耗尽返回 LIMIT_REACHED。
- 下载设置 Content-Disposition: attachment，文件名为 webpilot-{task_id}.md；正文和附录仅渲染纯文本与经校验链接，禁用原生 HTML/远端图片。网页内容中的链接不能直接变成服务端自动抓取请求。

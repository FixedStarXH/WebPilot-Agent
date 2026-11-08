# 架构与数据设计

## 1. 架构

```text
Web UI ── HTTP / SSE ── FastAPI
                           │
                     Task Service
                           │
                  Worker / Orchestrator
          ┌────────────────┼─────────────────┐
      Planner         Research tools       Reviewer / Writer
                           │
                 Search / Fetch / Browser MCP
                           │
                 Evidence & citation validator
                           │
                    PostgreSQL
```

- 首版运行一个 API 进程和一个 worker；通过数据库中的任务和租约协调，避免引入 Redis。后续确有并发需求再评估队列。
- PydanticAI 用于结构化模型输出和工具调用；工作流状态转换由应用代码显式控制，不依赖模型自行决定权限。
- 搜索、模型、HTTP 抓取与浏览器均通过接口抽象，开发和测试可使用假实现。Playwright MCP 只负责受限只读浏览；普通网页优先 HTTP 抓取。
- 首版只收集公开网页。浏览器工具只开放导航、页面读取和截图；禁止输入、提交、下载及复用登录态。工具白名单之外的调用一律拒绝。

## 2. 执行流程

1. 创建 queued 任务并保存参数，worker 通过事务领取并设置租约。
2. Planner 输出结构化子问题与查询词，应用校验数量和长度并存储计划。
3. Research 依次搜索、URL 规范化和去重；受限抓取公开页面，必要时使用隔离浏览器，按文本片段建立证据。
4. Reviewer 根据子问题检查覆盖，最多补充搜索 2 轮；检查不是事实真实性保证。
5. Writer 仅使用已保存证据生成结构化报告，再由确定性渲染器生成 Markdown；引用校验器检查证据 ID 归属和片段一致性。结果质量按需求文档第 7 节判定，禁止把未知内容补写成事实。
6. 报告、最终步骤、任务 completed 状态和终态事件在同一事务中提交；每一步写入事件供界面和 SSE 消费。

建议默认限制：任务来源数由 max_sources 决定（默认 10、最大 20），每 attempt 最多 40 个步骤和 10 分钟；单页面最多 2 MB，网页/搜索请求 15 秒；每 attempt 最多补充检索 2 轮。细则见第 8 节。超过硬限制时停止派发，保留已持久化结果并以 LIMIT_REACHED 失败；来源数或补充轮数用尽但还有写作预算时可生成 partial 报告。

## 3. 状态和恢复

任务状态：`queued`、`running`、`completed`、`failed`、`cancelled`。允许的变更：

```text
queued → running | cancelled
running → completed | failed | cancelled
failed | cancelled → queued（手动恢复）
```

取消是协作式：设置 cancel_requested，worker 在外部调用结束或超时后于检查点停止；状态仍为 running 时表示取消尚未生效。worker 启动及运行期间周期性回收过期租约，将未取消且意外中断的 running 任务置为 failed（错误码 WORKER_INTERRUPTED）；已有 cancel_requested 的过期任务转为 cancelled；用户可手动恢复。恢复时保留已完成计划、证据和事件，从最近成功检查点继续；进行中的步骤重新执行，使用稳定的步骤键和 URL 去重。由于首版只读，重试不应产生远端写操作。每次恢复增加 attempt，历史事件不覆盖。

领取、心跳、取消、完成及恢复使用条件更新；一个任务只允许一个有效租约。事件序号在单任务内单调递增，并与状态改变在同一数据库事务中写入。模型调用可能重复计费；恢复并不保证零重复调用。

## 4. 主要数据模型

| 表 | 核心字段 | 约束与用途 |
|---|---|---|
| tasks | id UUID、query、status、options JSON、attempt、cancel_requested、lease_owner、lease_expires_at、lease_token、version、created_at、updated_at、error_code、error_message | 当前状态；版本字段用于并发条件更新 |
| task_steps | id UUID、task_id、step_key、attempt、type、status、input JSON、output JSON、started_at、finished_at | 步骤与检查点；(task_id, attempt, step_key) 唯一 |
| sources | id UUID、task_id、canonical_url、title、fetched_at、status、error | 同任务 canonical_url 唯一；来源可能抓取失败 |
| evidence | id UUID、task_id、source_id、excerpt、locator、created_at | 保存文本片段；locator 可为标题或段落，不能凭空宣称精确位置 |
| reports | id UUID、task_id、attempt、version、quality、structured_content JSON、markdown、created_at | (task_id, version) 唯一；任务报告版本；正文引用使用 `[E:证据ID]`，导出时附来源清单 |
| task_events | task_id、sequence、attempt、type、payload JSON、created_at | `(task_id, sequence)` 唯一；支持重连和历史查询 |

所有时间使用 UTC ISO 8601；对外 ID 为 UUID 字符串。截取内容有长度上限，原始页面不作为指令执行。API 响应不暴露密钥或内部堆栈。

## 5. 安全边界与限制

- 抓取服务必须限制协议，解析并阻止回环、私网、链路本地、保留地址及云元数据地址；连接目标必须与校验后的地址一致。每次重定向都重新检查，限制跳转次数，防 DNS 重绑定。
- 浏览器必须在容器或受限网络命名空间中运行，通过出口代理实施相同的地址检查，阻断内部网络和未经验证的子资源；不能仅靠主导航 URL 检查。若无法配置隔离，禁用浏览器回退，仍保留安全 HTTP 抓取。
- 使用临时、独立的浏览器会话；禁用本地文件访问、持久化登录态和任意下载。首版无表单写操作或自动登录。
- 模型只获取完成任务所需的截断内容；网页中的指令不改变工具白名单和系统策略。输出渲染需防 XSS；报告中的链接须校验协议。
- 本地单用户模式只监听 loopback，敏感配置来自环境变量；公开部署前另行设计身份认证、授权和隔离。

## 6. 关键权衡

多 Agent 是职责拆分，不保证优于单 Agent；评测需比较调用次数、完成率与成本。证据 ID 校验只能证明引用存在，不能证明结论被证据支持；语义一致性需要人工抽查或另行评测。任务恢复依赖持久化检查点，不等同于跨机器高可用。

## 7. Agent 协议与确定性约束

结构化输出模型为应用所有，不允许供应商直接控制任务状态；设计详见 [Agent 协议](./agent-contracts.md)。首版 Planner、Reviewer、Writer 顺序执行，Research 是受控工具执行流程，不宣称已实现并行 Agent。只在补充检索需要时回环；首版固定一个计划版本，不在运行中更改目标，重新规划作为后续能力。

- Planner 的计划以 `plan_version` 保存到步骤 output，每个子问题使用稳定 ID；Reviewer 不允许隐式替换子问题。
- Writer 返回结构化结论及证据 ID；应用生成 Markdown 和引用附录。模型给出的引用 ID 必须属于当前任务。
- 在受预算限制的前提下，结构校验失败最多修复一次；仍无效则 `MODEL_OUTPUT_INVALID`，不能静默删除错误后假装成功。

## 8. 租约、取消竞态和恢复细则

- 默认 worker 并发 1，租约 60 秒，独立心跳每 10 秒；外部调用不能阻塞心跳。使用数据库时间判断过期，每 15 秒扫描；这些时间可配置并通过故障测试验证。
- 每次领取生成新的 `lease_token`。步骤、证据、报告和事件等全部 worker 写入，必须在同一事务内锁定任务并核验 token、running 状态及未过期租约；失去租约的旧 worker 必须停止，迟到结果不得落库。不能只给任务状态更新加条件而放任其他表写入。
- 取消与完成竞争时以任务行事务提交顺序为准：取消标记已提交，则完成提交被拒绝并转 cancelled；completed 已提交，则取消返回 409。外部请求可能已计费，取消不回滚已发生的读取。
- 恢复重新领取租约，复用跨 attempt 的成功检查点；步骤键包含计划版本、阶段和稳定对象 ID。恢复前若输出 schema、提示词版本或配置指纹不兼容，返回 `RESUME_INCOMPATIBLE`，要求新建任务，不将旧中间结果交给新协议处理。
- 计划、证据和 Reviewer 输出只在输入指纹匹配时复用；不得复用依赖已失效上游结果的下游检查点。历史失败错误保存在事件中，不因清除任务当前错误而丢失。
- 每 attempt 最多 40 个逻辑步骤、10 分钟活动执行时间（不计排队），模型请求 60 秒，网页及搜索请求 15 秒；同一外部调用可对瞬时错误重试 2 次，指数退避并遵守剩余预算。参数错误和访问拒绝不重试。
- `max_sources` 是同任务累计派发采集的唯一规范化 requested_url 上限，包括失败来源；恢复不能重置此上限。最多初次运行加 2 次手动恢复，耗尽后返回 `ATTEMPT_LIMIT_REACHED`，需新任务继续。每 attempt 最多 20 次模型请求（含修复与重试），达到即停止派发。

## 9. 证据、用量和本地数据生命周期

- 来源记录增加 `created_at`、`requested_url`、`final_url`、`content_hash`、`truncated`。内容哈希按用于提取的规范化文本计算，大小按解压后的响应计算；只处理 HTML/纯文本，首版 PDF 跳过并记原因。
- 证据记录增加 `content_hash`、`question_ids`，excerpt 必须是保存的规范化文本中的连续片段。首版 sources 增加 normalized_text 字段在数据库中保存该文本供核验，最多 2 MB/来源；locator 是可选提示，不伪造页面行号。搜索摘要只用于发现来源，不作为正式证据。
- 已收集来源在同任务恢复时默认复用，不覆盖已引用证据；需要新鲜数据则新建任务。canonical_url 由 requested_url 规范化产生：统一 scheme/host 大小写、去默认端口和 fragment，保留 path 与 query 的语义及顺序；不同请求 URL 重定向到同一页面仍占各自名额，但报告不能把它们当作独立佐证。
- 增加 `model_calls` 表：task_id、attempt、step_id、provider、model、prompt_version、started_at、duration_ms、input_tokens/output_tokens（可空）、estimated_cost（可空）、currency、error_code。只保存脱敏元数据，不保存秘密或模型隐藏推理。
- 首版用户通过 CLI 清理终态任务及其关联记录和本地缓存；默认不自动删除，启动提示磁盘用量，任务总数上限默认 100，满额返回 `CAPACITY_EXCEEDED`。queued/running 不可清理；清理与 resume 锁定同一任务行，恢复先提交则拒绝清理，清理先提交则恢复 404。事件保留期与任务一致，P1 再增加 UI 删除和定期清理。
- 同源提供 UI 和 API；校验 Host 防 DNS 重绑定、关闭宽泛 CORS，所有 POST 要求 JSON Content-Type 和受信 Origin（存在时）；无请求体的操作发送 `{}`。loopback 绑定不能单独代替这些检查。该模式不防御已经能访问本机的恶意进程。

## 10. 数据约束补充

- `tasks` 增加 `config_fingerprint`、`schema_version`；任务创建时保存脱敏配置快照（模型名、提示词版本、限制、采集方式），密钥只从运行环境读取。
- `idempotency_records` 保存 task_id（外键）、operation、scope_id（创建任务用固定非空值，避免 SQL NULL 唯一约束歧义）、key、request_hash、response_status、response_body、expires_at；唯一键 `(operation, scope_id, key)`。记录与创建/恢复事务一并提交，失败回滚不留下完成记录。
- 外键从步骤/来源/报告/事件指向 tasks；evidence 使用 `(task_id, source_id)` 复合外键确保来源属于同任务。删除仅允许终态任务；数据库记录事务内级联删除，文件删除由提交后的可重试清理处理，不能假设文件系统与数据库能原子提交。索引至少覆盖 queued 任务领取、租约过期扫描及各子表 task_id 查询。
- task_steps 增加 created_at、input_fingerprint；model_calls 增加主键 id。sources 对 (task_id,id) 建唯一约束供复合外键引用。
- 截图仅作为调试缓存，首版不提供上传或媒体接口；限制每来源一张、单图 5 MB，超限不保存，随任务清理。正文缓存及截图不对外静态暴露。

## 11. 采集提交与预算调度

- 领取来源名额时先持久化 pending 来源和 fetch 步骤；网络执行在事务外。成功后，将规范化正文、证据、来源 collected、步骤完成和 source.collected/step.completed 事件在同一受租约保护的事务提交，避免出现“已采集但没有证据”的半成品。未抽出相关片段允许 collected + 零证据，Reviewer 将对应问题判为 missing。
- 中断时将当前 running 步骤收束为 failed（WORKER_INTERRUPTED）或 cancelled；pending 来源置 failed 并记录原因。恢复可重新采集失败来源，不重复占名额；每来源每 attempt 至多一组抓取调用（HTTP 及至多一次 browser 回退，各自受重试规则约束）。已成功来源保持不可变；证据 ID 按已提交记录复用。
- 40 步上限按外层逻辑步骤计数：规划 1 + 搜索最多 12 + 来源采集最多 20 + 检查最多 3 + 写作 1 = 37。浏览器回退、网络重试、输出修复不新增逻辑步骤，但计入调用次数、时间和用量。复用的检查点不消耗本 attempt 步数。
- 首版片段选择采用确定性文本分段和关键词相关度排序，不为每个来源额外调用模型；每条证据保存关联 question_ids，校验其属于计划。以后如引入模型提取，须重新核算调用预算。
- 调度器始终为最后一次 Reviewer 和 Writer 保留 2 个步骤、4 次模型请求（各含一次结构修复）及 120 秒剩余时间；不能保证供应商在预留内成功。不能满足预留时停止新搜索/采集，使用已有证据收尾。完全无证据则不再调用 Writer。
- 每次模型请求除 60 秒超时外，还必须配置输入上下文预算（默认 12000 tokens）和最大输出（默认 2000 tokens），且总和在所选模型容量内。由适配器使用可信 tokenizer 计数；没有计数能力的适配器不能启用真实模型运行。超长证据按子问题均衡选取完整片段，不直接把 20 个全文塞入上下文；未送入模型的证据不用于该次结论或支持判定。
- model_calls 在网络派发前记录 started 并原子消耗调用名额；崩溃后未完成的调用记 unknown，仍占预算，不假设未计费。外部调用日志记录 search/fetch/browser 的耗时和错误；不存储完整密钥或认证头。金额仍只能按可得实际用量估计，次数上限不等于固定金额上限。

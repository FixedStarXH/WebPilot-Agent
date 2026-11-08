# Agent 输入输出协议

版本：0.3；本文件是计划实现的应用层契约，不依赖供应商特定 SDK 方法。

## 1. Planner

输入：用户 query、服务端预算和可用能力（HTTP / browser）。首版报告语言跟随用户问题，混合语言默认中文；无单独 language 参数。输出：

```json
{
  "plan_version": 1,
  "questions": [{"id": "q1", "text": "各项目的许可证是什么？"}],
  "searches": [{"question_ids": ["q1"], "query": "Qdrant official license"}]
}
```

plan_version 由应用固定赋值为 1，不信任模型随意修改；首版恢复复用计划。子问题 1–6 个，初始搜索 1–6 条；ID 唯一，引用子问题必须存在。优先仓库许可证文件、官方文档等一手来源。用户问题内的候选项目不另设 HTTP 字段。输出不直接触发执行，先通过应用校验和预算检查。

## 2. Research 工具流程

输入：合法搜索项及剩余预算。输出：已保存的 source/evidence ID、抓取失败原因。执行器控制 URL 校验、采集、去重和截断；每个来源最多 5 个片段，每片段最多 2000 字符。首版用确定性分段和相关度排序选择片段，并记录关联 question_ids；应用必须验证片段是采集文本的连续子串。模型不能创建虚构的证据 ID。

标准搜索结果结构为 `title/url/snippet`，标准采集结果为 `requested_url/final_url/title/text/fetched_at/content_hash/truncated`。适配器不提供配置 API，无法在任务中更改出口或注入密钥。

## 3. Reviewer

输入：计划、证据及其来源、失败信息、补充轮数与剩余预算。输出：

```json
{
  "coverage": [{"question_id": "q1", "status": "supported", "evidence_ids": ["ce54e100-84d0-4f48-82a7-ad3507e8d32c"], "reason": "许可证原文提供了依据"}],
  "follow_up_searches": []
}
```

每个子问题恰好一条 coverage，status 为 `supported|missing|conflicting`。supported 必须关联至少一条有效证据；conflicting 必须记录冲突说明及可得的不同来源。follow_up_searches 使用 Planner searches 的同一对象结构（question_ids/query）；每轮最多 3 个补充查询，最多 2 轮；实际执行由调度器按预算决定，不能靠模型无限回环。

## 4. Writer 与渲染器

输入：计划、Reviewer 的覆盖结果和现有证据。输出：

```json
{
  "title": "开源向量数据库比较",
  "findings": [{"question_id": "q1", "text": "示例结论", "evidence_ids": ["ce54e100-84d0-4f48-82a7-ad3507e8d32c"]}],
  "limitations": ["未验证所有部署环境"]
}
```

事实结论须关联证据；未支持的子问题由渲染器明确列为未知，不允许 Writer 编造答案。结论文本中的 HTML 和 URL 不直接成为可执行页面内容；来源链接由服务端已有 source 记录生成。

Writer 必须为每个 supported 子问题恰好返回一条 finding（可含多个结论句），不得为 missing/conflicting 子问题写确定性结论。遗漏、重复或未知 question_id 均为输出校验失败，修复后仍不合法则 MODEL_OUTPUT_INVALID，不能把漏写悄悄降级成 partial。finding 的 evidence_ids 必须为该子问题 coverage 的 evidence_ids 非空子集。

应用计算 quality：全部子问题 supported 且 Writer 合法为 complete；至少一个子问题 supported、其余存在缺失或冲突且 Writer 合法为 partial；没有 supported 子问题则 failed/NO_EVIDENCE（包括有片段但无法支持结论的情况）。引用 ID 属于任务、片段存在，只说明形式有效，不能替代语义支持评测。

模型输出最多修复一次且受调用预算约束；报告和终态必须原子提交。每个 Agent 的提示词、输出 schema、模型配置记录版本/指纹，支撑可复现运行与恢复兼容检查。

## 5. 工具能力验证

只有已通过测试的 MCP 工具才加入白名单；初始化读取工具目录并验证能力，版本不兼容时禁用 browser 回退并显示原因。浏览器回退默认关闭，验证通过后由服务配置启用。首版不能借助脚本执行工具绕过只读约束；网页自然发出的网络请求仍需出口策略控制。MCP 是工具连接层，不负责规划、引用核验或任务恢复。

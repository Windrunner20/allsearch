# AllSearch 搜索 Benchmark

## 文件

| 文件 | 用途 |
| --- | --- |
| `cases.json` | 39 道开发题（知乎、贴吧、V2EX、Chiphell、Reddit、HN、X、GitHub、新闻、百科、文档、历史原文、冷门页面、付费墙、反爬壳、评论题等）。审计后修正的目标带 `grader_changes` |
| `cases.v1.json` | 2026-10-15 评测时的原始题集，原样保留 |
| `holdout/cases.json` | 9 道隐藏题，在 0.3 搜索改动之前写成并单独提交（`d6ddf39`），开发期间不运行 |
| `harness.py` | 评分器 v2，以及 `plan` / `budget` / `score` 等离线命令 |
| `grader_v1.py` | 2026-10-15 使用的评分器，冻结不改，用于复现历史分数 |
| `audit.py` | 用 v1 和 v2 重新评分历史运行，写入 `runs/audit-20261016/`，不修改原始文件 |
| `live_ab.py` + `live_worker.py` | 工具层实测：基线标签（git worktree）与当前代码同时运行同一题集 |
| `live_agent.py` | Agent 层实测：Pi 通过 MCP 使用两个版本的服务；按工具实际返回的内容评分 |
| `attribution.py` | 同一份原始数据分别用 v1、v2 规则 + v1 目标、v2 评分，检验提升是否来自评分器调整 |

```bash
python -m benchmarks.harness budget --depth balanced     # 离线
python -m benchmarks.audit                               # 离线
python -m benchmarks.attribution benchmarks/runs/ab-...  # 离线
python -m benchmarks.live_ab --depth balanced            # 调用付费 API
python -m benchmarks.live_ab --cases benchmarks/holdout/cases.json
python -m benchmarks.live_agent                          # 调用付费 API 与 Agent 模型
```

`harness.py live` 仍然拒绝执行；实测只能通过上面的专用脚本有意运行。

## 评分维度（分开记录，不合并）

| 维度 | 通过条件 |
| --- | --- |
| discovery | 命中指定路径；需要时标题、摘要或该页抓取正文中含指定词。多目标默认全部命中 |
| reading | 抓取到该 URL 的正文，且含阅读证据 |
| comments | 有带 ID 的评论记录（来自平台 API），文本含证据，且属于目标帖子或指定评论 ID。页面正文不算评论 |
| cross_language | 命中题目列出的外文目标页 |
| synthesis | 答案含全部关键事实 |
| freshness | `published_at` 落在执行时间前的窗口内 |

任务结果：`pass`、`fail`、`blocked`（实际读取时遇到登录、验证码、付费墙或站点不支持）、`timeout`、`not_answerable`、`not_attempted`。只有真实读取尝试中的标记才算访问受阻，搜索摘要里出现 “captcha” 等字样不算。

## v2 与 v1 的区别

1. URL 先做百分号解码再匹配。
2. 评论必须是带 ID、来自平台的记录；v1 把页面正文当成评论。
3. `timeout`、`not_attempted` 单独记录。
4. 审计发现误报或漏报的目标已修正（WSJ 物理奖而非化学奖、OpenAI 官方 X 账号、维基条目路径、HN 同一文章的多次投稿、CVE/NVD 二选一、Vue 文档无 `.html`、Discuz `thread-<tid>` 地址、PEP 末尾斜杠），每题注明在 `grader_changes`。
5. 发现证据可以来自已抓取的该页正文。
6. 访问受阻只看读取尝试，不看摘要。

Agent 轮：2026-10-15 的运行器把 Agent 最终回答复制进每条结果的摘要，相当于用模型自己的话证明发现。v2 运行器只用工具返回的内容；审计中把旧结果里只能靠回答文本通过的题标为 `pass_unverified`。

## 历史结果审计

见 `runs/audit-20261016/AUDIT.md`。原始运行目录未改动。要点：工具 balanced 轮 v1 记录 10 通过，v2 重评 12；Agent 轮 v1 记录 14 通过，其中 8 题只能靠回答文本通过，11 题撞上 240 秒上限；deep 轮的 history-rfc791 通过无法从存档复现（0.2 的脱敏把 `rfc-editor.org` 存成了 `rfc-***`）。

## 实测对照

结果与分析见 [docs/REPORT-0.3.md](../docs/REPORT-0.3.md)。每轮原始数据在 `runs/ab-*`、`runs/agent-ab-*` 下保留。

## 成本记录

`live_worker.py` 记录每个 Provider 的调用次数和 xAI 返回的 `cost_in_usd_ticks`（按 1 美元 = 10¹⁰ tick 换算，经第三方网关时实际扣费可能不同）。0.2 基线不记录 Grok 用量，基线费用只能按调用次数估算。Tavily、Firecrawl 记调用次数。

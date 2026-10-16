# AllSearch 搜索 Benchmark

这是基线评测，不改搜索算法。默认命令只读取路由规则或本地观察结果，不调用 Grok、Tavily、AnySearch 或 Firecrawl。

```bash
python -m benchmarks.harness budget --depth balanced
python -m benchmarks.harness plan --depth verify
python -m benchmarks.harness score --observations path/to/observations.json
python -m benchmarks.harness protocol
```

`live` 会直接拒绝。真实联网前先看 `budget` 的调用次数，并单独决定是否执行。

## 题集

`cases.json` 有 32 题，覆盖知乎、贴吧、V2EX、Chiphell、Reddit、Hacker News、X、GitHub、新闻、百科、普通中英文网页、文档、历史原文、冷门页面、付费墙和反爬壳。

每题分开记录：

- 检索目标
- 期望来源类型和域名
- 可核对的关键事实
- 访问条件：`public`、`login`、`paywall`、`anti_bot`、`mixed`
- 分项评价标准

## 五个分数，不合并

| 维度 | 通过条件 | 组件未做 |
| --- | --- | --- |
| discovery | 命中指定路径，需要时还要命中该页标题或摘要。多个目标必须全部命中 | fail |
| reading | 抓到的是该 URL 的正文，且含阅读证据 | `not_attempted` |
| comments | `comments` 里有指定评论 id 和评论原文。帖子 URL 不算 | `not_attempted` |
| cross_language | 命中题目列出的外文目标页 | fail |
| synthesis | 答案含全部关键事实 | 空答案为 fail |
| freshness | `published_at` 落在 `executed_at` 前的窗口内 | `unverifiable` |

组件可以是 `not_attempted`。如果这一项写在 `task_requires` 里，端到端任务记失败，不能因为没尝试就放过。`expected_access` 只表示平台预期限制；`observed_failure` 只来自本次结果里的登录、验证码或付费墙文本。

登录、验证码、付费墙单独进入 `failure_class`。有这类标记时，不把该题算作算法失误。

## 模式、来源和成本

默认阈值：主结果至少 3 条带 URL 的引用、2 个独立域名，才算“足够”。`fast` 把这两个数减半。

| 模式 | 一定会调用 | 可能不调用 | 原文抓取 |
| --- | --- | --- | --- |
| fast | Grok | 主结果足够时跳过 Tavily，时效题也不补 | 不抓 |
| balanced | Grok | 同上。时效词（最新、新闻、today）仍会补 Tavily | 不抓。`scrape_after` 固定为 false |
| verify | Grok + Tavily | 只有识别出垂直领域才加 AnySearch | 最多 2 页 Firecrawl |
| deep | Grok + Tavily | 同上 | 最多 3 页 Firecrawl |

社区名、对比、文档这些词本身不会强制补搜。对比题只有在独立域名少于 2 个时才算不够。因此 Grok 只要给出 3 个 URL、2 个域名，balanced 就会停，即使这些页面都不是目标论坛、评论或官方原文。

`budget` 打印的是调用次数，不是美元。单价以厂商当时价目为准，避免在仓库里写会过期的价格。

## 和 Pi Web Access 对照

1. 固定同一份 `cases.json`。
2. AllSearch 只用 `balanced`、45 秒总时限、相同 query，不改写问题。
3. 对照方每个问题只做一次搜索，时限同样 45 秒，query 原文不变。
4. 两边都存成同一种 observation JSON，再用 `score` 打分。
5. verify 和 deep 另开一栏。不和 balanced 混成一个平均分。
6. 成本按 API 次数比，不按墙钟时间单独比。

观察结果至少包含 `id`、`answer`、`results`、`citations`、`fetched`、`errors`、`timing_ms`、`api_calls`。

# AllSearch

给 Pi 和其他 MCP Host 用的搜索服务：Grok 先给出带引用的答案，需要时再补 Tavily、AnySearch 和 Firecrawl。默认走一条长期 stdio 连接，搜索摘要和完整结果分开，避免把整页 JSON 塞进模型上下文。

适合已经有 Grok（或兼容的 Responses 端点）以及按需补充源密钥的本地 Agent。它不替代浏览器，也不保证能读登录墙、验证码或付费墙后面的内容。

当前版本 `0.2.0`。MCP Python SDK 固定在 `1.26.0`，不要升到 2.x：2.x 会拆掉现在的 `FastMCP` 接口。项目元数据声明为 MIT；仓库里还没有单独的 `LICENSE` 文件。

## 快速开始

需要 Python 3.11+。

```bash
git clone https://github.com/Windrunner20/allsearch.git
cd allsearch
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
chmod 600 .env
```

至少填 `ALLSEARCH_XAI_API_KEY`。Tavily、AnySearch、Firecrawl 可以留空，对应能力会跳过。进程环境变量优先于 `.env`。

健康检查不发起搜索：

```bash
python - <<'PY'
import asyncio
from allsearch.config import load_config
from allsearch.orchestrator import Orchestrator

async def main():
    app = Orchestrator(load_config())
    try:
        health = await app.health()
        for provider in health.providers:
            print(provider.name, provider.configured, provider.state)
    finally:
        await app.aclose()

asyncio.run(main())
PY
```

配好 Grok 密钥时，应看到 `xai True`，状态一般是 `idle` 或 `healthy`。

## 接到 Pi

推荐用 Pi 自带的 MCP，而不是仓库里的旧 Extension。`integrations/pi/mcp.native.json` 是一份可复制的 stdio 配置：`exposure` 为 `direct`，并打开 Compact Output。

```json
{
  "mcpServers": {
    "allsearch": {
      "type": "stdio",
      "exposure": "direct",
      "command": "/absolute/path/to/allsearch/.venv/bin/python",
      "args": ["-m", "allsearch", "--transport", "stdio", "--log-level", "WARNING"],
      "cwd": "/absolute/path/to/allsearch",
      "timeout": 180,
      "env": {
        "ALLSEARCH_COMPACT_OUTPUT": "true"
      }
    }
  }
}
```

密钥留在 `.env`。不要把这份配置和 `integrations/pi` Extension 同时启用，否则模型可能对同一个问题搜两次。回滚步骤在 [integrations/pi/ROLLBACK.md](integrations/pi/ROLLBACK.md)。

新开一个 Pi 会话后，`pi mcp list` 或 `/mcp` 里应能看到 `search`、`fetch`、`health`、`read_result`，状态为 connected。

其他 Host 也可以直接启动 stdio：

```bash
python -m allsearch --transport stdio
```

日志在 stderr。stdout 只留给 MCP JSON-RPC。

## 工具

`ALLSEARCH_COMPACT_OUTPUT` 默认关闭，此时 `search` 和 `fetch` 仍返回完整 JSON，方便已有调用方。设为 `true` 后，同一次进程里的工具变成：

| 工具 | 模型看到的内容 |
| --- | --- |
| `search` | 状态、答案摘要、引用 URL、证据计数、错误码，以及 `result_id`。单次不超过 8KB |
| `fetch` | 短预览、正文字数和 `result_id` |
| `read_result` | 按 `result_id`、字段、`offset` / `limit` 翻页。不重新调用搜索 API |
| `health` | Provider 是否配置、熔断和缓存。`probe` 默认 `false` |

完整响应写在 `ALLSEARCH_RECORD_DIR`，默认是 `~/.local/state/allsearch/records`。目录 `0700`，文件 `0600`，默认保留 1 小时、最多 200 条。Pi 会在会话期间复用这条 stdio 进程，所以内存缓存和熔断在同一次会话里有效；进程重启后缓存清空，未过期的磁盘记录仍可 `read_result`。

`search` 的常用参数：

```json
{
  "query": "Python venv 模块的官方文档在哪",
  "mode": "auto",
  "depth": "balanced",
  "max_results": 8,
  "fresh": false
}
```

`mode` 可以是 `auto`、`web`、`news`、`docs`、`research`、`vertical`。`fresh: true` 会跳过本次搜索缓存。

## 搜索怎么走

Grok 先搜。主结果达到 3 条带 URL 的引用、并且来自至少 2 个域名时，`fast` 和 `balanced` 通常不再调用 Tavily。查询里有“最新”“新闻”“today”这类时效信号时，`balanced` 仍会补一次 Tavily。`verify` 和 `deep` 总会补 Tavily。

| 深度 | 补充搜索 | 正文 |
| --- | --- | --- |
| `fast` | 主结果不够才用 Tavily | 不抓 |
| `balanced` | 不够或有时效信号时用 Tavily；垂直题可能用 AnySearch | 不抓 |
| `verify` | 总是 Tavily，垂直题再加 AnySearch | 最多 2 页 Firecrawl |
| `deep` | 同 verify | 最多 3 页 Firecrawl |

因此，点名 V2EX、知乎或 Reddit 时，如果 Grok 已经交了三个别的网站，`balanced` 会停，不一定再去那个论坛。评论正文也没有单独的数据源；登录、验证码和付费墙不会被绕过。

总时长默认 45 秒（`ALLSEARCH_TOTAL_BUDGET_SECONDS`）。超时后不再开下一阶段。Grok 不可用时，默认直接失败，除非把 `ALLSEARCH_ALLOW_DEGRADED_SEARCH` 设为 `true`。

## 评测

离线题集和评分器在 [benchmarks/README.md](benchmarks/README.md)。默认命令不调用付费 API：

```bash
python -m benchmarks.harness budget --depth balanced
python -m benchmarks.harness plan --depth verify
```

`live` 会拒绝执行。一次真实的 39 题 balanced 评测，以及随后对未通过题目做的 verify / deep，结果摘要写在 [benchmarks/runs/REPORT.md](benchmarks/runs/REPORT.md)。那次结果说明：公开文档和一部分新闻较好；中文论坛、指定评论和登录墙后的正文不是靠加深搜索就能稳定补上。

## 测试

```bash
source .venv/bin/activate
pytest -q
```

默认测试使用 Mock，不访问付费 Provider。带 `live` 标记的用例只有在 `ALLSEARCH_RUN_LIVE_TESTS=1` 时才会跑。

## 限制

- 搜索质量仍取决于 Grok 返回的引用。引用数量够，不代表来源就是用户点名的那个站。
- 缓存只在当前进程的内存里，没有跨进程共享。
- 兼容网关的模型名、延迟和计费会变。端点级 fallback 只接受 OpenAI chat completions，不接受 Responses。
- 没有 Docker 镜像、管理界面，也没有第二层模型来重写答案。

## 许可与贡献

欢迎 Issue 和 Pull Request。不要提交真实 API key、`.env` 或未脱敏的用户查询。Provider 契约有变化时，请附上脱敏后的响应样例。

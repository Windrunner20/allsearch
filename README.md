# AllSearch

**通用 MCP 搜索服务。** 任何符合 MCP 标准的客户端（Claude Code、Pi、MCP Inspector、Claude Desktop、Cursor、VS Code 等）都可以通过 stdio 或 Streamable HTTP 使用同一套搜索能力；服务端没有任何客户端专属逻辑。

它做的事：

- 用 Grok（xAI Responses `web_search`）做主搜索，Tavily、AnySearch、Firecrawl 按需补充。
- **按证据判断是否足够**：用户点名的网站、要求覆盖的全部来源、评论、时间窗口、对比的两方，逐项检查；只在缺什么时补什么，而不是“有 3 个引用就停”。
- **论坛帖子与评论**：Hacker News、V2EX、Stack Exchange 通过公开 API 读取帖子和评论（带评论 ID、作者、时间、上级评论）；Reddit 在配置 OAuth 应用后可读。知乎、贴吧、微博、小红书等登录墙平台只报告“找到但无法读取”，不绕过登录、验证码或付费墙。
- **有界输出**：默认返回限长摘要和 `result_id`，完整结果用 `read_result` 分页取回，不会把整页 JSON 塞进模型上下文。
- **防止 Agent 无效循环**：每次返回都带会话计数；相同调用直接复用已有结果，连续多次没有新证据会给出明确提示。
- **可远程部署**：Bearer 认证、按用户隔离结果、限流、请求体上限；没有认证时拒绝在非回环地址提供服务。

当前版本 `0.3.0`（从 0.2 升级见 [CHANGELOG](CHANGELOG.md)）。MIT 许可。MCP Python SDK 固定在 `1.26.0`。

## 安装

需要 Python 3.11+。

```bash
# 作为命令行工具安装（推荐；@v0.3.0 为正式版本）
uv tool install git+https://github.com/Windrunner20/allsearch@v0.3.0
# 或
pipx install git+https://github.com/Windrunner20/allsearch@v0.3.0

# 或从源码
git clone https://github.com/Windrunner20/allsearch.git && cd allsearch
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
```

配置密钥：把 [.env.example](.env.example) 复制成 `.env` 并至少填 `ALLSEARCH_XAI_API_KEY`（Grok 或兼容的 Responses 端点）。Tavily、AnySearch、Firecrawl 可留空，对应能力会跳过；HN、V2EX、Stack Exchange、Wikipedia 不需要密钥。

`.env` 的查找顺序：`ALLSEARCH_ENV_FILE` → 当前目录 → 用户配置目录（Linux `~/.config/allsearch/.env`，macOS `~/Library/Application Support/allsearch/.env`，Windows `%APPDATA%\allsearch\.env`）→ 源码检出目录。进程环境变量始终优先。

```bash
chmod 600 .env                 # POSIX
allsearch check                # 打印解析后的配置（不含密钥）
allsearch --version
```

卸载：`uv tool uninstall allsearch` / `pipx uninstall allsearch` / `pip uninstall allsearch`。完整结果缓存目录（见下文“存储”）可直接删除。

## 接入 MCP 客户端

通用 stdio 配置（多数客户端的 `mcpServers` 格式）：

```json
{
  "mcpServers": {
    "allsearch": {
      "command": "allsearch",
      "args": ["--transport", "stdio", "--log-level", "WARNING"],
      "env": { "ALLSEARCH_ENV_FILE": "/path/to/allsearch/.env" }
    }
  }
}
```

Claude Code：

```bash
claude mcp add allsearch --scope user --env ALLSEARCH_ENV_FILE=/path/to/.env \
  -- allsearch --transport stdio --log-level WARNING
```

其他客户端（Pi、Claude Desktop、Cursor、VS Code、MCP Inspector、远程 HTTP）的配置和实际验证情况见 [examples/clients](examples/clients/README.md)。日志只写 stderr，stdout 只用于 MCP JSON-RPC。

## 工具

| 工具 | 作用 |
| --- | --- |
| `search` | 搜索。返回状态、答案摘要、结果与引用 URL、`coverage`（每项要求是否满足及原因）、读取过的论坛帖子与最相关评论、`next_steps`、`session`、`result_id` |
| `fetch` | 读取一个已知 URL。HN / V2EX / Stack Exchange（以及配置后的 Reddit）走公开 API 并返回结构化评论；其他页面走 Firecrawl。登录墙、验证码、付费墙以错误码报告（`login_required`、`captcha`、`paywall`、`site_not_supported`） |
| `read_result` | 按 `result_id` 分页读取完整结果，不再调用任何 Provider。字段：`answer`、`results`、`citations`、`coverage`、`threads`、`comments`、`content` 等；`answer`/`content` 按字符分页，列表按条分页，跟随 `next_offset` |
| `health` | Provider 配置、熔断、缓存状态 |

`search` 常用参数：`query`、`depth`（`fast` < `balanced` < `verify` < `deep`）、`mode`、`max_results`、`include_domains` / `exclude_domains`、`fresh`（跳过缓存与重复检测）。

所有工具返回一个紧凑 JSON 文本块，以及同一对象的 `structuredContent`；`status: "error"` 时 `isError: true`。参数由 JSON Schema 校验。

### 输出模式与兼容

默认 `ALLSEARCH_OUTPUT_MODE=compact`：摘要不超过 `ALLSEARCH_COMPACT_MAX_BYTES`（默认 8 KB，按实际 UTF-8 字节计算），`read_result` 每页不超过 16 KB。需要 0.2 那种完整 JSON 的旧调用方设置 `ALLSEARCH_OUTPUT_MODE=full`（或旧变量 `ALLSEARCH_COMPACT_OUTPUT=false`），此时没有 `read_result`，返回内容与 0.2 一致（schema 版本为 1.1，只增加字段）。

### 存储

完整结果写在本地状态目录（Linux `~/.local/state/allsearch/records`，macOS `~/Library/Application Support/allsearch/records`，Windows `%LOCALAPPDATA%\allsearch\records`，或 `ALLSEARCH_RECORD_DIR`）。目录 `0700`、文件 `0600`（POSIX），默认保留 1 小时、最多 200 条。每条记录属于创建它的用户（stdio 为本地用户，HTTP 为 token 对应的 principal）；别的用户拿到 `result_id` 也读不到。缓存与熔断状态只在进程内存中。

## 搜索如何工作

1. 分析请求：点名的网站（“V2EX 上…”“on Hacker News”“GitHub release page”；“GitHub Actions 报错”这类只是话题，不算）、是否要评论、是否要求每个来源都覆盖、目标语言、时间窗口、对比双方。
2. Grok 主搜索（点名网站时只在这些网站内搜），同时运行公开站点 API 搜索；中文请求指向英文来源时，另做一次无工具的关键词改写。
3. 检查覆盖情况；只为未满足的要求补搜（限定该网站的 Tavily、时间窗口内新闻、对比中缺失的一方）。没有这类要求的请求仍按 0.2 的“引用数/域名数”规则。
4. 读取可读站点的帖子和评论，按与用户描述的相关度排序。
5. `verify` / `deep` 再用 Firecrawl 读取最相关的页面。

Grok 最多使用总预算的 65%（默认总预算 60 秒），剩余时间留给补搜和读帖；Grok 超时时用 Tavily 答案兜底。

| 深度 | 补充搜索 | 正文抓取 |
| --- | --- | --- |
| `fast` | 主结果不足时 Tavily；读 1 个帖子（如需评论） | 无 |
| `balanced` | 按缺失要求定向补搜 | 无（`fetch` 按需读取） |
| `verify` | 同上，并总是补 Tavily | 最多 2 页 |
| `deep` | 同 verify | 最多 3 页 |

## 远程部署（Streamable HTTP）

```bash
allsearch hash-token alice          # 打印 token（交给客户端）和 tokens 文件行
echo 'alice sha256:...' >> tokens && chmod 600 tokens
ALLSEARCH_HTTP_TOKENS_FILE=./tokens allsearch --transport streamable-http --host 127.0.0.1 --port 8000
```

- 客户端发送 `Authorization: Bearer <token>`；缺失或错误返回 401。`GET /healthz` 无需认证且不含任何信息。
- 非回环地址必须配置 token；`ALLSEARCH_HTTP_ALLOW_ANONYMOUS=true` 只允许在回环地址使用。
- 限流按 principal 计（默认 120 次/分钟，突发 30），请求体默认上限 1 MB。限流与会话计数在单进程内存中，多进程部署时各自独立。
- 对公网请放在 TLS 反向代理后面，并设置 `ALLSEARCH_HTTP_ALLOWED_HOSTS` 启用 Host 头检查。
- Docker：见 [Dockerfile](Dockerfile) 与 [deploy/docker-compose.yml](deploy/docker-compose.yml)。镜像构建在 CI 中验证。

## 安全与隐私

- 密钥只放在 `.env` 或进程环境；错误、日志和存储的结果中会脱敏常见密钥格式。日志不记录搜索内容。
- `fetch` 拒绝非 http(s)、内网/回环/保留地址（含 IPv4 映射的 IPv6、CGNAT）和带凭据的 URL。Firecrawl 自建在内网时会额外做本地 DNS 检查（`ALLSEARCH_FETCH_DNS_CHECK`）。站点 API 只访问固定的官方域名。
- 不绕过登录、验证码、付费墙；不使用用户账号读取平台内容。

## 配置速查

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ALLSEARCH_XAI_API_KEY` / `_BASE_URL` / `_MODEL` | — / `https://api.x.ai/v1` / `grok-4.5` | 主搜索 |
| `ALLSEARCH_TAVILY_API_KEY(S)`、`ALLSEARCH_FIRECRAWL_API_KEY`、`ALLSEARCH_ANYSEARCH_API_KEY` | 空 | 可选补充 |
| `ALLSEARCH_TOTAL_BUDGET_SECONDS` | 60 | 单次搜索总预算 |
| `ALLSEARCH_OUTPUT_MODE` | compact | `full` 恢复 0.2 输出 |
| `ALLSEARCH_NATIVE_SOURCES` | true | HN / V2EX / Stack Exchange / Wikipedia |
| `ALLSEARCH_REDDIT_CLIENT_ID` / `_SECRET` | 空 | 读取 Reddit 评论需要 |
| `ALLSEARCH_SESSION_MAX_SEARCHES` / `_FETCHES` | 40 / 80 | 每会话 15 分钟滚动上限 |
| `ALLSEARCH_HTTP_TOKENS_FILE` / `ALLSEARCH_HTTP_TOKEN` | 空 | HTTP 认证 |

完整列表见 [.env.example](.env.example)。

## 评测

题集、评分器 v2、历史结果审计和新旧版本实测对照见 [benchmarks/README.md](benchmarks/README.md) 与 [docs/REPORT-0.3.md](docs/REPORT-0.3.md)。默认命令不调用付费 API。

## 测试

```bash
pytest -q          # 离线，不访问任何付费 Provider
```

带 `live` 标记的用例只在 `ALLSEARCH_RUN_LIVE_TESTS=1` 时运行。CI 在 Linux、macOS、Windows 上用 Python 3.11–3.13 运行离线测试，并构建 wheel 和 Docker 镜像。

## 故障排查

| 现象 | 处理 |
| --- | --- |
| `search` 返回 `primary_unavailable` | 没有配置或无法使用 Grok；检查 `ALLSEARCH_XAI_*`，或设 `ALLSEARCH_ALLOW_DEGRADED_SEARCH=true` 允许只用补充源 |
| 结果里有 `primary_timeout` | Grok 超过预算的 65%；答案可能来自 Tavily。可调大 `ALLSEARCH_TOTAL_BUDGET_SECONDS` |
| `read_result` 报 unknown or expired | 超过 1 小时、被容量淘汰、换了 HTTP token，或服务换了记录目录 |
| `session_budget_exhausted` | 该会话 15 分钟内调用过多；调整 `ALLSEARCH_SESSION_MAX_*` |
| `site_not_supported` / `login_required` | 该站点无法被 Firecrawl 读取或需要登录；不是程序错误 |
| HTTP 启动即退出码 2 | 没有配置 token，或在非回环地址使用匿名模式 |
| `pi -p` 卡住 | 脚本中运行时关闭 stdin（`< /dev/null`） |
| 客户端看不到工具 | 确认命令路径；stdout 必须只有 JSON-RPC，不要在包装脚本里打印内容 |

## 已知限制

- 搜索质量仍依赖 Grok 与补充源的索引；知乎、贴吧、微博、小红书、微信等只能报告找到的链接。
- Reddit 未配置 OAuth 时只能发现帖子，不能读取评论；Reddit 读取代码只有模拟测试，未用真实凭据验证。
- 缓存、限流、会话计数在进程内存中，不跨进程共享。
- 没有第二层模型改写答案；评论类请求的答案会附上 API 读到的评论原文。

## 贡献

欢迎 Issue 和 Pull Request。不要提交真实 API key、`.env` 或未脱敏的用户查询。

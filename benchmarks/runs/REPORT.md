# AllSearch 真实联网评测

运行目录：

- 工具 balanced：`benchmarks/runs/tool-balanced-20261015T074021Z`
- 工具 verify（仅 balanced 未通过的 29 题）：`benchmarks/runs/tool-verify-20261015T080025Z`
- 工具 deep（仅 verify 仍未通过的 21 题）：`benchmarks/runs/tool-deep-20261015T082424Z`
- Pi Agent：`benchmarks/runs/agent-20261015T0839Z`

算法、Provider 和 MCP 架构没有改。搜索请求只含题目 query；fetch 题只含任务 URL。

费用：接口响应里没有美元字段。下面是调用次数。保存结果时，含 `fc-` 的字符串会被误打码，`rfc-editor.org` 在存档里变成了 `rfc-***`。

## 总表

工具 balanced：通过 10，失败 27，访问阻断 2。耗时约 1055 秒。调用 Grok 36、Tavily 4、Firecrawl 30，另有 1 次 AnySearch security，以及 2 次 Grok 在 90 秒时被取消（HN 评论题，未计入成功调用）。

verify：在 29 道未通过题里通过 8。调用 Grok 28、Tavily 28、Firecrawl 77。约 1358 秒。

deep：在 21 道仍未通过题里新通过 1（history-rfc791）。调用 Grok 20、Tavily 20、Firecrawl 78。约 862 秒。

Agent：39 题都跑完。按同一评分器，通过 14，失败 24，阻断 1。工具调用 search 96、fetch 215、read_result 259、health 2。另有 1 次 codemode（小红书题，偏离 AllSearch）。11 题撞上 240 秒上限。约 5711 秒。Agent 评分把最终答案文本同时当作摘要，因此比工具轮松，不能和工具通过数直接相加。


| 题 | 渠道 | 工具 balanced | verify | deep | Agent | 工具看到的 URL |
| --- | --- | --- | --- | --- | --- | --- |
| zhihu-gil | zhihu | blocked | blocked | blocked | fail | https://zhuanlan.zhihu.com/p/1923366864152658874; https://zhuanlan.zhihu.com/p/1918634950082688599 |
| zhihu-cross-rust | docs | pass | — | — | pass | https://www.jasonwalton.ca/rust-book-abridged/ch04-ownership; https://doc.rust-lang.org/1.69.0/book/title-page.html |
| tieba-camera | tieba | fail | fail | blocked | fail | http://g5.baidu.com/pu=sz%401320_480/s?is_rec_s=0&params_ssrt=node-san; https://forum.xitek.com/thread-1946639-1-1.html |
| v2ex-macos | v2ex | fail | pass | — | fail | https://hk.v2ex.com/t/910789; https://cn.v2ex.com/t/1083082 |
| chiphell-keyboard | chiphell | fail | fail | fail | fail | https://www.youtube.com/@chiphell2009/videos; https://www.youtube.com/watch?v=k55Cmqnx2M8 |
| reddit-localllama | reddit | fail | fail | fail | fail | https://www.reddit.com/r/LocalLLaMA/comments/1rei65v/qwen3535ba3b_quan; https://reddit.com/r/LocalLLaMA/comments/1kgzey8/run_flux1_losslessly_ |
| reddit-cross-sqlite | reddit | fail | fail | blocked | fail | https://www.reddit.com/r/sqlite/comments/1ow05mh/trailbase_021_open_si; https://www.reddit.com/r/webdev/comments/17pxrab/sqlite_in_production_ |
| hn-sqlite | hackernews | fail | fail | fail | pass | https://news.ycombinator.com/item?id=46076642; https://news.ycombinator.com/item?id=50040360 |
| x-public-post | x | fail | pass | — | pass | https://x.com/rowancheung/status/1635691542739574784; https://x.com/theshawwn/status/2108621721544601822 |
| github-cpython-issue | docs | fail | fail | fail | fail | https://github.com/python/peps/pull/2955; https://peps.python.org/pep-0703?featured_on=talkpython |
| github-release | github | pass | — | — | pass | https://github.com/nodejs/node/releases/tag/v22.0.0; https://github.com/nodejs/nodejs.org/blob/main/apps/site/pages/en/blog |
| news-zh-archive | news | fail | fail | fail | fail | https://web.archive.org/web/20241205144915/https:/www.nobelprize.org/p; https://www.nobelprize.org/page/12?np-prize-category=physics |
| news-en-archive | reference | fail | fail | fail | fail | https://www.cve.org/Media/News/item/blog/2026/10/13/CVE-Search-API; https://www.cve.org/about/overview |
| wiki-zh-tcp | encyclopedia | fail | fail | fail | fail | https://zh.wikipedia.org/zh-hant/%E8%B6%85%E6%96%87%E6%9C%AC%E4%BC%A0%; https://juejin.cn/post/7111666884264591391 |
| wiki-en-tcp | encyclopedia | fail | pass | — | pass | https://www.iana.org/assignments/service-names-port-numbers?search=htt; https://superuser.com/questions/996828/why-was-port-80-chosen-as-the-d |
| general-zh-stats | general_web | pass | — | — | fail | https://www.stats.gov.cn/zs/tjws/tjzb/202301/t20230101_1903757.html; https://www.stats.gov.cn/zs/tjws/tjbk/202301/t20230101_1912948.html |
| general-en-mdn | general_web | fail | pass | — | fail | https://web.archive.org/web/20230521000616/https://developer.mozilla.o; https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Redirections |
| docs-python-venv | docs | pass | — | — | pass | https://docs.python.org/3/library/venv.html; https://docs.python.org/dev/tutorial/venv.html |
| history-rfc791 | history | fail | fail | pass | fail | https://www.rfc-***/standards; https://www.rfc-***/std/std5.txt |
| cold-personal-note | long_tail | fail | fail | fail | fail | https://dev.to/cerchie/tech-jargon-defined-what-is-yak-shaving-3hj8; https://www.techtarget.com/whatis/definition/yak-shaving |
| stackoverflow-venv | stackoverflow | pass | — | — | fail | https://stackoverflow.com/questions/70422866/how-to-create-a-venv-with; https://www.w3schools.com/python/python_virtualenv.asp |
| lwn-kernel | tech_press | pass | — | — | fail | https://lwn.net/Articles/262464; https://lwn.net/Articles/988638 |
| weibo-login | weibo | fail | fail | fail | fail | http://widget.weibo.com/weiboshow/index.php?dpc=1&fansRow=2&height=550; http://weibo.com/1700087532/QtMt66K3k?s=6cm7D0 |
| news-fresh-query | news | fail | fail | fail | fail | https://www.reuters.com/world/asia-pacific/sk-***; https://www.cnbc.com/2025/12/09/us-attorneys-office-southern-district- |
| compare-react-vue | docs | pass | — | — | pass | https://www.stanza.dev/compare/react-vs-vue; https://www.telerik.com/blogs/react-vue-developers-part-2-state-effect |
| github-code-search | github | fail | fail | fail | fail | https://github.com/python/cpython/commits/451d1edaf4d27c4e632d81246d30; https://github.com/zpoint/CPython-Internals/blob/master/Interpreter/py |
| baidu-baike | encyclopedia | fail | pass | — | pass | https://baike.baidu.com/item/%E5%9B%BD%E9%99%85%E8%AE%A1%E7%AE%97%E6%9; https://baike.baidu.com/item/ACM%E8%AE%A1%E7%AE%97%E5%A5%96/67630810 |
| dual-forum-hardware | forums | fail | pass | — | pass | https://www.chiphell.com/thread-2673133-1-1.html; https://www.chiphell.com/thread-2765182-1-1.html |
| paywall-wsj | news | pass | — | — | fail | https://www.nobelprize.org/all-nobel-prizes-2024; https://www.wsj.com/science/nobel-prize-in-chemistry-awarded-to-trio-f |
| antibot-shell | forums | blocked | pass | — | blocked | https://www.chiphell.com/thread-2522672-1-1.html; https://github.com/Kagami/chaptcha/blob/master/chaptcha.py |
| fetch-python-venv | docs | pass | — | — | fail | https://docs.python.org/3/library/venv.html |
| hn-comment-dissent | hackernews | fail | fail | fail | fail |  |
| wechat-mp-discovery | wechat | fail | fail | fail | pass | https://juejin.cn/post/6844903686515195917; https://cloud.tencent.cn/developer/article/2712622 |
| xiaohongshu-note-discovery | xiaohongshu | fail | fail | fail | fail | https://www.xiaohongshu.com/explore/69e9d06f000000001f031800?source=we; https://www.xiaohongshu.com/explore/69f49bd90000000038022f7c?app_platf |
| bilibili-video-discovery | bilibili | fail | pass | — | pass | https://www.bilibili.com/read/cv33703563; https://www.bilibili.com/read/cv33614592 |
| youtube-me-at-the-zoo | youtube | fail | fail | fail | pass | https://www.youtube.com/watch?v=jNQXAC9IVRw&vl=en; https://www.youtube.com/watch?v=jNQXAC9IVRw%60 |
| telegram-public-preview | telegram | pass | — | — | pass | https://instantview.telegram.org/templates/telegram.org?url=https%3A%2; https://t.me/s/telegram?before=461 |
| hn-two-comments | hackernews | fail | fail | fail | fail |  |
| cross-lang-hn-sqlite | hackernews | fail | fail | fail | pass | https://news.ycombinator.com/item?id=40655974; https://www.zhouexin.com/archives |

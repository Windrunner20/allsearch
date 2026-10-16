# Live A/B (balanced, 20261016T041942Z)

- baseline: `baseline-1244cf5` (1244cf50)
- candidate: working tree at `8bcad066`
- cases: benchmarks/cases.json (39)

| arm | pass | fail | timeout | blocked | other | Grok cost (reported) | median s | total s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | 10 | 28 | 0 | 1 | 0 | $0.0 | 17.5 | 899 |
| candidate | 25 | 13 | 0 | 1 | 0 | $1.2081 | 15.5 | 703 |

## Calls

- baseline: firecrawl.scrape 37, tavily.search 8, xai.search 38
- candidate: firecrawl.scrape 30, hackernews.read_thread 10, hackernews.search 4, stackexchange.read_thread 3, stackexchange.search 1, tavily.search 2, v2ex.read_thread 4, v2ex.search 2, wikipedia.search 2, xai.rewrite 4, xai.search 37

## Per-dimension

| dimension | baseline | candidate |
| --- | --- | --- |
| comments | fail 11 | fail 7, pass 4 |
| cross_language | pass 4 | pass 4 |
| discovery | fail 17, pass 20 | fail 7, pass 30 |
| freshness | fail 1 | pass 1 |
| reading | fail 7, not_attempted 5, pass 3 | fail 4, not_attempted 5, pass 6 |
| synthesis | pass 17 | fail 1, pass 16 |

## Cases whose task verdict changed

| case | baseline | candidate | candidate reasons |
| --- | --- | --- | --- |
| zhihu-gil | fail | blocked | comments:fail |
| v2ex-macos | fail | pass |  |
| hn-sqlite | fail | pass |  |
| x-public-post | fail | pass |  |
| wiki-en-tcp | pass | fail | discovery:fail, synthesis:fail |
| stackoverflow-venv | fail | pass |  |
| lwn-kernel | fail | pass |  |
| news-fresh-query | fail | pass |  |
| baidu-baike | fail | pass |  |
| dual-forum-hardware | fail | pass |  |
| paywall-wsj | fail | pass |  |
| antibot-shell | blocked | pass |  |
| hn-comment-dissent | fail | pass |  |
| wechat-mp-discovery | fail | pass |  |
| xiaohongshu-note-discovery | fail | pass |  |
| bilibili-video-discovery | fail | pass |  |
| hn-two-comments | fail | pass |  |
| cross-lang-hn-sqlite | fail | pass |  |

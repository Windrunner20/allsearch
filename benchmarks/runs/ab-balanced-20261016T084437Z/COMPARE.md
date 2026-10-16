# Live A/B (balanced, 20261016T084437Z)

- baseline: `baseline-1244cf5` (1244cf50)
- candidate: working tree at `75b0a8eb`
- cases: benchmarks/cases.json (39)

| arm | pass | fail | timeout | blocked | other | Grok cost (reported) | median s | total s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | 11 | 26 | 2 | 0 | 0 | $0.0 | 17.1 | 893 |
| candidate | 24 | 12 | 0 | 3 | 0 | $1.3404 | 14.0 | 618 |

## Calls

- baseline: firecrawl.scrape 33, tavily.search 6, xai.search 36
- candidate: firecrawl.scrape 29, hackernews.read_thread 10, hackernews.search 4, stackexchange.read_thread 3, stackexchange.search 1, tavily.search 2, v2ex.read_thread 4, v2ex.search 2, wikipedia.search 2, xai.rewrite 4, xai.search 38

## Per-dimension

| dimension | baseline | candidate |
| --- | --- | --- |
| comments | fail 11 | fail 7, pass 4 |
| cross_language | pass 4 | pass 4 |
| discovery | fail 17, pass 20 | fail 5, pass 32 |
| freshness | fail 1 | fail 1 |
| reading | fail 7, not_attempted 5, pass 3 | fail 6, not_attempted 5, pass 4 |
| synthesis | fail 1, pass 16 | pass 17 |

## Cases whose task verdict changed

| case | baseline | candidate | candidate reasons |
| --- | --- | --- | --- |
| zhihu-gil | fail | blocked | comments:fail |
| v2ex-macos | fail | pass |  |
| chiphell-keyboard | fail | blocked | discovery:fail, comments:fail |
| reddit-localllama | fail | blocked | comments:fail |
| hn-sqlite | fail | pass |  |
| x-public-post | fail | pass |  |
| history-rfc791 | fail | pass |  |
| stackoverflow-venv | fail | pass |  |
| baidu-baike | fail | pass |  |
| dual-forum-hardware | fail | pass |  |
| hn-comment-dissent | timeout | pass |  |
| wechat-mp-discovery | fail | pass |  |
| xiaohongshu-note-discovery | fail | pass |  |
| bilibili-video-discovery | fail | pass |  |
| hn-two-comments | timeout | pass |  |
| cross-lang-hn-sqlite | fail | pass |  |

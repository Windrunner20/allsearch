# Live A/B (balanced, 20261016T094535Z)

- baseline: `baseline-1244cf5` (1244cf50)
- candidate: working tree at `b8c25d5a`
- cases: benchmarks/cases.json (39)

| arm | pass | fail | timeout | blocked | other | Grok cost (reported) | median s | total s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | 15 | 21 | 1 | 2 | 0 | $0.0 | 18.6 | 904 |
| candidate | 24 | 14 | 0 | 1 | 0 | $1.2508 | 14.1 | 615 |

## Calls

- baseline: firecrawl.scrape 35, tavily.search 8, xai.search 37
- candidate: firecrawl.scrape 29, hackernews.read_thread 10, hackernews.search 4, stackexchange.read_thread 3, stackexchange.search 1, tavily.search 1, v2ex.read_thread 4, v2ex.search 2, wikipedia.search 2, xai.rewrite 4, xai.search 38

## Per-dimension

| dimension | baseline | candidate |
| --- | --- | --- |
| comments | fail 11 | fail 7, pass 4 |
| cross_language | pass 4 | pass 4 |
| discovery | fail 15, pass 22 | fail 8, pass 29 |
| freshness | pass 1 | pass 1 |
| reading | fail 4, not_attempted 5, pass 6 | fail 6, not_attempted 5, pass 4 |
| synthesis | fail 1, pass 16 | pass 17 |

## Cases whose task verdict changed

| case | baseline | candidate | candidate reasons |
| --- | --- | --- | --- |
| tieba-camera | blocked | fail | discovery:fail, comments:fail |
| v2ex-macos | fail | pass |  |
| hn-sqlite | fail | pass |  |
| history-rfc791 | pass | fail | reading:fail |
| stackoverflow-venv | fail | pass |  |
| github-code-search | pass | fail | discovery:fail, reading:fail |
| baidu-baike | fail | pass |  |
| dual-forum-hardware | fail | pass |  |
| paywall-wsj | fail | pass |  |
| fetch-python-venv | pass | fail | reading:fail |
| hn-comment-dissent | timeout | pass |  |
| wechat-mp-discovery | fail | pass |  |
| xiaohongshu-note-discovery | fail | pass |  |
| youtube-me-at-the-zoo | fail | pass |  |
| hn-two-comments | fail | pass |  |
| cross-lang-hn-sqlite | fail | pass |  |

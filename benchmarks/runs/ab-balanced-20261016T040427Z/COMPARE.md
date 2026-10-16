# Live A/B (balanced, 20261016T040427Z)

- baseline: `baseline-1244cf5` (1244cf50)
- candidate: working tree at `673dd56f`
- cases: benchmarks/cases.json (39)

| arm | pass | fail | timeout | blocked | other | Grok cost (reported) | median s | total s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | 9 | 30 | 0 | 0 | 0 | $0.0 | 18.3 | 801 |
| candidate | 17 | 21 | 0 | 1 | 0 | $1.1784 | 18.5 | 712 |

## Calls

- baseline: firecrawl.scrape 37, tavily.search 8, xai.search 38
- candidate: firecrawl.scrape 29, hackernews.read_thread 10, hackernews.search 4, stackexchange.read_thread 3, stackexchange.search 1, tavily.search 4, v2ex.read_thread 4, v2ex.search 2, wikipedia.search 2, xai.rewrite 4, xai.search 38

## Per-dimension

| dimension | baseline | candidate |
| --- | --- | --- |
| comments | fail 11 | fail 7, pass 4 |
| cross_language | pass 4 | pass 4 |
| discovery | fail 20, partial 1, pass 16 | fail 15, partial 2, pass 20 |
| freshness | fail 1 | pass 1 |
| reading | fail 7, not_attempted 5, pass 3 | fail 6, not_attempted 5, pass 4 |
| synthesis | fail 1, pass 16 | pass 17 |

## Cases whose task verdict changed

| case | baseline | candidate | candidate reasons |
| --- | --- | --- | --- |
| zhihu-gil | fail | blocked | discovery:fail, comments:fail |
| v2ex-macos | fail | pass |  |
| hn-sqlite | fail | pass |  |
| general-en-mdn | fail | pass |  |
| history-rfc791 | fail | pass |  |
| stackoverflow-venv | fail | pass |  |
| news-fresh-query | fail | pass |  |
| fetch-python-venv | pass | fail | reading:fail |
| hn-comment-dissent | fail | pass |  |
| hn-two-comments | fail | pass |  |
| cross-lang-hn-sqlite | fail | pass |  |

# Live A/B (balanced, 20261016T085253Z)

- baseline: `baseline-1244cf5` (1244cf50)
- candidate: working tree at `af3a8091`
- cases: benchmarks/holdout/cases.json (9)

| arm | pass | fail | timeout | blocked | other | Grok cost (reported) | median s | total s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | 4 | 5 | 0 | 0 | 0 | $0.0 | 23.5 | 188 |
| candidate | 9 | 0 | 0 | 0 | 0 | $0.3167 | 14.9 | 153 |

## Calls

- baseline: firecrawl.scrape 6, tavily.search 4, xai.search 9
- candidate: hackernews.read_thread 8, hackernews.search 2, stackexchange.read_thread 2, stackoverflow.search 1, tavily.search 3, v2ex.read_thread 4, v2ex.search 1, wikipedia.search 1, xai.rewrite 2, xai.search 9

## Per-dimension

| dimension | baseline | candidate |
| --- | --- | --- |
| comments | fail 3 | pass 3 |
| cross_language | pass 1 | pass 1 |
| discovery | fail 1, pass 7 | pass 8 |
| freshness | fail 1 | pass 1 |
| synthesis | pass 2 | pass 2 |

## Cases whose task verdict changed

| case | baseline | candidate | candidate reasons |
| --- | --- | --- | --- |
| ho-hn-boring-tech | fail | pass |  |
| ho-hn-netscape-comment | fail | pass |  |
| ho-v2ex-orbstack | fail | pass |  |
| ho-reddit-rust | fail | pass |  |
| ho-news-fresh | fail | pass |  |

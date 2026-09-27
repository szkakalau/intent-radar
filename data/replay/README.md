# Frozen model responses

These files exist so that the published accuracy table can be recomputed by
someone who has no model endpoint — which includes the reviewer.

**What they are.** Raw responses from **`deepseek-v4-flash`**, reached through a
local Anthropic-shaped development proxy at `http://127.0.0.1:8787/v1` during
the acceptance runs on testset `einprag-2026-09-27` (`label_version`
`einprag-2026-09-27d`, `labels.csv sha256=2009ef74b2d1`). Two recordings:

| directory | layer | calls | backs |
|---|---|---|---|
| `einprag-2026-09-27/rule_v3_llm/` | `rule_v3+llm` (`v3.1.0+llm`) | 14 | the shipping-default table |
| `einprag-2026-09-27/rule_v4_llm/` | `rule_v4+llm` (`v4.5.0+llm`) | 42 | the v4 funnel table |

Both tables are published in the README and both are covered by a test that
replays the recording and compares every row line by line.

**What they are NOT.** They are **not** Nemotron, and not Nebius. Every figure
derived from them must be attributed to `deepseek-v4-flash`. Re-measuring on
Nebius/Nemotron is pending; until then nothing on this page may be quoted as a
Nemotron result.

## Reproducing the table

```bash
# the shipping default (rule_v3 vs rule_v3+llm, threshold from meta.json)
uv run python -m intentradar eval score --testset einprag-2026-09-27 \
    --replay data/replay/einprag-2026-09-27/rule_v3_llm

# the v4 funnel
uv run python -m intentradar eval score --testset einprag-2026-09-27 \
    --layers rule_v4,rule_v4+llm --min-score 3 \
    --replay data/replay/einprag-2026-09-27/rule_v4_llm
```

No network, no API key. Either report prints `REPLAYED — frozen model responses
from <date>, model=deepseek-v4-flash. Not a live run.` on its first line, and
`tests/test_replay.py` replays both recordings and compares every row of both
published tables line by line — so the published numbers cannot drift away from
the reproducible ones without the test suite failing.

## What each record carries

`calls.jsonl` holds one JSON object per line. Every record carries all seven of:

| field | why it is there |
|---|---|
| `post_id` | which post the question was about |
| `judge_version` | which criteria the prompt encoded |
| `model` | which model answered |
| `endpoint` | where it was answered from |
| `recorded_at` | when |
| `prompt_sha256` | sha256 of the exact system + user prompt |
| `response` | the raw response text, unedited |

Drop any one of those and the file is a cache, not evidence: you could no longer
tell whether a response answers the prompt we claim, under the criteria we
claim, from the model we claim.

## Failure behaviour

A replay never fills a gap. If the judge version or the prompt changes, the
lookup **fails**, naming what changed, rather than answering with a response to
a different question. That is deliberate — a replay that quietly falls back to a
live endpoint is not a replay.

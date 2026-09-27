# Frozen model responses

These files exist so that the published accuracy table can be recomputed by
someone who has no model endpoint — which includes the reviewer.

**What they are.** 42 raw responses from **`deepseek-v4-flash`**, reached
through a local Anthropic-shaped development proxy at `http://127.0.0.1:8787/v1`
during the `v4.5.0+llm` acceptance run on testset `einprag-2026-09-27`
(`label_version` `einprag-2026-09-27d`, `labels.csv sha256=2009ef74b2d1`).

**What they are NOT.** They are **not** Nemotron, and not Nebius. Every figure
derived from them must be attributed to `deepseek-v4-flash`. Re-measuring on
Nebius/Nemotron is pending; until then nothing on this page may be quoted as a
Nemotron result.

## Reproducing the table

```bash
uv run python -m intentradar eval score --testset einprag-2026-09-27 \
    --layers rule_v4,rule_v4+llm --min-score 3 \
    --replay data/replay/einprag-2026-09-27/rule_v4_llm
```

No network, no API key. The report prints `REPLAYED — frozen model responses
from <date>, model=deepseek-v4-flash. Not a live run.` on its first line, and
`tests/test_replay.py` compares every row of the table against the one
published in the README — so the published numbers cannot drift away from the
reproducible ones without the test suite failing.

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

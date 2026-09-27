# IntentRadar

Find Reddit posts that carry **buying intent**, and publish the accuracy so anyone can check it.

Most "AI lead finder" tools are black boxes: they hand you 40 posts and you cannot tell
why any of them was chosen. IntentRadar does the opposite — every lead ships with
machine-readable evidence, the hit rate is published, and a stranger can recompute
our number on their own machine with one command.

> Status: **W2 — semantic layer.** `rule_v3` (regex) is the published baseline;
> `rule_v3+llm` adds an LLM precision check on top of it. `eval score` prints
> precision / recall / F1 for **both** layers side by side.
>
> Read [Accuracy](#accuracy) before you trust any number here: the rule layer's
> measured precision against our labelled ground truth is **0%**. We publish it
> because the alternative is a number nobody can check.

---

## Quickstart (3 steps)

```bash
git clone https://github.com/<your-account>/intent-radar.git
cd intent-radar

# 1. install
uv sync                     # or: pip install -e .

# 2. configure
cp .env.example .env        # then fill in SCRAPECREATORS_API_KEY

# 3. run
uv run intentradar config check
uv run intentradar run --project Einprag
```

No API key? No problem for the evaluation path — it is fully offline:

```bash
uv run intentradar eval run --testset einprag-2026-09-27
```

### Commands

| Command | What it does |
|---|---|
| `intentradar run` | Collect → judge → dedupe → write the daily report |
| `intentradar config check` | Validate `.env` + `config/watchlist.json` before anything runs |
| `intentradar llm hello` | Smoke-test the Nemotron client (works in mock mode without a key) |
| `intentradar eval run` | Recompute the published baseline, offline |
| `intentradar eval score` | Precision / recall / F1 for `rule_v3` vs `rule_v3+llm`, against `labels.csv` |
| `intentradar eval export` | Export hits to CSV / JSONL for third-party review |
| `intentradar eval snapshot` | Freeze a new testset from a live collection run |
| `intentradar budget` | Show / reset monthly usage |

Exit codes: `0` ok · `1` evaluation mismatch · `2` config/credential · `3` gate/budget · `4` provider.

---

## Accuracy

We publish the number **and** the way to reproduce it.

```
$ intentradar eval run --testset einprag-2026-09-27

testset : einprag-2026-09-27   (frozen 2026-09-27)
judge   : rule_v3 (v3.0.0)
─────────────────────────────────────────────
posts in window         211
hits over threshold     6
hit rate                2.8%
─
noise rate = (# hits a human marks 'not actionable') / (# hits)
no manual review included in this run (see data/testset/einprag-2026-09-27/labels.csv)
→ full accuracy incl. human review is delivered in W2 (`eval score`)
expected 6 hits: MATCH
```

**Baseline X = the rule layer.** Snapshot `einprag-2026-09-27` contains every post
in a 3-day window across 5 subreddits; `rule_v3` flags 6 of them. Run the command
above on a fresh clone and you get the same 6 ids, the same scores, the same reasons.

The second snapshot, `bootstrap-2026-09-27` (our own dog-fooding project), is
137 posts → 12 hits → 8.8%.

> **Honest note on the denominator.** The production run that first produced this
> number executed at 2026-09-27 01:40 and saw **208** posts in its rolling
> 3-day window (6/208 = 2.9%). The frozen snapshot was taken 35 minutes later,
> by which time the rolling window had drifted to **211** posts (6/211 = 2.8%).
> The hit set is byte-identical — 6 ids, same scores, same reasons — only the
> denominator moved. We publish the frozen snapshot's own number rather than
> retro-fitting the data to 208.

### Measured accuracy: `eval score` (W2)

A hit rate alone says nothing about whether the hits are any good. `eval score`
compares the two layers against the human ground truth in
`data/testset/<id>/labels.csv` and prints **both** columns:

```
$ intentradar eval score --testset einprag-2026-09-27

testset : einprag-2026-09-27   (frozen 2026-09-27)
project : Einprag   min_score=5
labels  : 211 rows · 184 labeled (7 actionable / 177 not) · 0 unlabeled · 27 borderline
──────────────────────────────────────────────────────────────
metric                      rule_v3 (v3.0.0)  rule_v3+llm (v3.1.0+llm)
predicted                                  6                         0
true positive                              0                         0
false positive                             4                         0
false negative                             7                         7
precision                               0.0%                         -
recall                                  0.0%                      0.0%
F1                                         -                         -
noise rate                            100.0%                         -
unverified hits                            2                         0
```

**Read that carefully: `rule_v3` scores 0 true positives out of 4 decided hits.**
Every post the rule layer flagged was marked `not_actionable` (4) or
`borderline` (2), and all 7 posts a reviewer marked `actionable` scored below
the threshold of 5 — so they are all false negatives. This is not a bug in the
scoring; we cross-checked each id individually. It is the honest measurement of
a regex heuristic against a human reading the same posts, and we publish it
because a hidden number is worth nothing.

The `rule_v3+llm` column above is **not a measurement**: with no API key the
semantic layer runs against the deterministic mock and the report says so. The
X → Y comparison is only real once a key is configured.

### How the ground truth is handled

| Cell in `labels.csv` | Meaning | Effect on the metrics |
|---|---|---|
| `actionable` / `1` | a human would act on this | positive |
| `not_actionable` / `0` | a human would not | negative |
| `borderline` | the reviewer genuinely could not decide | **excluded and counted**, never scored as 0 |
| *(blank)* | not reviewed yet | **excluded and counted** |

A blank or `borderline` cell is never silently turned into "not actionable" —
that would manufacture false positives and inflate precision. `eval score`
reports how many rows were excluded so the denominator is visible. An
unrecognised value is a hard `ConfigError` (exit 2) naming every offending row,
so a typo is fixed rather than absorbed.

### Failure cases will be published

A hit rate alone is meaningless without the misses. We publish the false
positives and false negatives of each layer, why each one failed, and the
per-lead token cost. If the semantic layer does not beat X, we publish that too.

### What "evidence" looks like

Every lead carries an auditable reason, not just a score:

```json
{
  "id": "1wp6ji1", "sub": "Anki", "score": 9,
  "signals": ["求助选型", "痛点吐槽", "竞品提及"],
  "evidence": [
    { "type": "ask",        "pattern": "which\\s+(?:app|tool|one)\\b", "matched": "which app" },
    { "type": "competitor", "value": "brainscape" },
    { "type": "keyword",    "value": "flashcard" },
    { "type": "pain",       "value": "cost", "cooccur": "flashcard" }
  ],
  "why": ["求助:which app", "竞品:brainscape", "品类:flashcard", "痛点:cost"],
  "layer": "rule_v3", "judge_version": "v3.0.0"
}
```

---

## Cost safety

Two gates and one circuit breaker, because a mis-configured batch run is the
fastest way to burn the whole credit balance:

| Guard | Default | Behaviour |
|---|---|---|
| `INTENTRADAR_MAX_POSTS_PER_SOURCE` | 200 | soft cap **per subreddit** — stop pulling from that source, warn, keep going with the others |
| `INTENTRADAR_MAX_LLM_CALLS` | 50 | **hard stop** — raise on the call that exceeds it |
| `INTENTRADAR_MONTHLY_BUDGET_USD` | 20 | persisted to `data/usage.json`; 90% warns, 100% refuses to run |

The monthly breaker covers **both sides of the spend**: LLM cost *and* collection
cost. ScrapeCreators bills in credits, so credits are converted at
`SCRAPE_CREDIT_USD = $47 / 25,000 ≈ $0.00188` per credit before being counted —
without that conversion a runaway collection loop would burn credits while the
breaker still reported 0% used.

Usage accumulates across processes and resets automatically at month rollover.
`INTENTRADAR_ALLOW_UNPRICED=0` makes the breaker strict: calling a model that is
missing from `MODEL_PRICING` is refused before the request is sent.

---

## Configuration

Copy `.env.example` to `.env`. Reading order is process env → repo `.env` →
built-in defaults. All paths resolve against `INTENTRADAR_DATA_DIR`, never
against the current working directory.

The LLM endpoint is configurable. `INTENTRADAR_LLM_BASE_URL` /
`INTENTRADAR_LLM_API_KEY` / `INTENTRADAR_LLM_MODEL` override the `NEBIUS_*`
defaults, so the semantic layer can point at **any OpenAI-compatible server**
(`POST /v1/chat/completions`) — useful because Nebius Token Factory's
billing-country list does not cover every country. `config check` prints which
variable supplied each value.

`config/watchlist.json` (schema v2) holds one block per project:
`subreddits` / `keywords` / `competitors` / `min_score`. A missing or
wrong-typed field fails loudly **before** any network call, e.g.

```
watchlist.projects[1].keywords: expected non-empty list[str], got missing
```

> `pain_words` exists in the config but is **reserved**: `rule_v3` scores with a
> module-level constant and ignores that field on purpose. Turning it on would
> change the published hit set and requires a judge-version bump.

---

## Privacy

The frozen testsets store `id / sub / title / selftext / created_utc / score /
num_comments` only. **Author names are never persisted.**

---

## Development

```bash
uv sync --extra dev
uv run pytest          # fully offline (network tests are opt-in: -m network)
uv run ruff check src tests
```

Layout:

```
src/intentradar/
  collect/    data-source providers (ScrapeCreators; fallback is an interface stub)
  judge/      Judge protocol + rule_v3 (verbatim port) + llm.py (semantic layer)
  llm/        OpenAI-compatible client: one shell, mock + http backends
  budget.py   monthly budget + per-run gates
  pipeline.py collect → judge → dedupe → report
  eval.py     frozen testsets, offline recomputation, exports
```

---

## Significant Updates

See [`SIGNIFICANT_UPDATES.md`](SIGNIFICANT_UPDATES.md) for the written record of
updates made during the hackathon submission period.

## License

MIT — see [`LICENSE`](LICENSE).

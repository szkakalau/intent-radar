# IntentRadar

Find Reddit posts that carry **buying intent**, and publish the accuracy so anyone can check it.

Most "AI lead finder" tools are black boxes: they hand you 40 posts and you cannot tell
why any of them was chosen. IntentRadar does the opposite — every lead ships with
machine-readable evidence, the hit rate is published, and a stranger can recompute
our number on their own machine with one command.

> Status: **W2 — semantic layer.** Two pipelines ship side by side and are scored
> against the same ground truth: `rule_v3` (regex, frozen baseline) and
> `rule_v4` → `rule_v4+llm` (a deliberately wide recall net plus an LLM gate).
> `eval score` prints precision / recall / F1 for **both**, each with raw counts,
> the sample size, and a 95% Wilson interval.
>
> Read [Accuracy](#accuracy) before you trust any number here. The shipping
> regex baseline measures **20.0% precision (1/5)** and **12.5% recall (1/8)**
> on **8 labelled positives** — a 95% CI of `[4%, 62%]` and `[2%, 47%]`
> respectively. We publish the interval rather than a bare percentage, because
> on n=8 a number without one is not a measurement.
>
> ⚠ **Sample size n=8; pending ground-truth batch.** Every figure on this page is
> staged at that sample size and will be refreshed **in the same commit as the
> pending adjudication**, never quietly afterwards.

---

## Quickstart (3 steps)

```bash
git clone https://github.com/szkakalau/intent-radar.git
cd intent-radar

# 1. install
uv sync                     # or: pip install -e .

# 2. configure
cp .env.example .env        # then fill in SCRAPECREATORS_API_KEY

# 3. run
uv run intentradar config check
uv run intentradar run --project Einprag
```

> **If `uv run intentradar` fails to spawn** (some locked-down environments
> refuse to execute console scripts — Windows application-control policies
> report `os error 4551`), use the module form, which needs no installed
> launcher and behaves identically:
>
> ```bash
> uv run python -m intentradar config check
> uv run python -m intentradar eval run --testset einprag-2026-09-27
> ```

No API key? No problem for the evaluation path — it is fully offline:

```bash
uv run intentradar eval run --testset einprag-2026-09-27
```

### Commands

| Command | What it does |
|---|---|
| `intentradar run` | Collect → judge → dedupe → write the daily report |
| `intentradar config check` | Validate `.env` + `config/watchlist.json` before anything runs; prints `backend=… · model=…` |
| `intentradar llm hello` | Smoke-test the Nemotron client (works in mock mode without a key) |
| `intentradar eval run` | Recompute the frozen hit set, offline |
| `intentradar eval score` | Precision / recall / F1 for any `--layers` list against `labels.csv`, with counts + 95% CI |
| `intentradar eval density` | Per-subreddit ground-truth density — **read this when positives < 10** |
| `intentradar eval export` | Export hits to CSV / JSONL for third-party review |
| `intentradar eval snapshot` | Freeze a new testset from a live collection run |
| `intentradar budget` | Show / reset monthly usage |

Common flags: `eval score --layers rule_v4,rule_v4+llm --min-score 3`.

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
reviewed hits           5/6   (1 undecided: borderline/blank)
noise rate              80.0%
ground truth: 183 labeled (8 actionable / 175 not) · 0 unlabeled · 28 borderline
→ full precision / recall: `eval score --testset einprag-2026-09-27`
expected 6 hits: MATCH
```

`eval run` is the reproducibility check: it reports the hit set **and** how much
of it a human has actually adjudicated. 5 of the 6 hits carry a verdict and 4 of
those are `not_actionable` — 80.0% noise, which is the same number `eval score`
prints as `1 - precision`. The 6th hit sits on a `borderline` row and is counted
as undecided rather than silently folded into either side.

**Baseline X = the rule layer.** Snapshot `einprag-2026-09-27` contains every post
in a 3-day window across 5 subreddits; `rule_v3` flags 6 of them. Run the command
above on a fresh clone and you get the same 6 ids, the same scores, the same reasons.

The second snapshot, `bootstrap-2026-09-27` (our own dog-fooding project), is
137 posts → 12 hits → 8.8%.

> **We do not publish a precision/recall rate for bootstrap, and the 8.8% above
> is a hit rate, not accuracy.** Its ground truth contains **2 positives**, so one
> relabelled row is a 50-point swing — the number would be noise dressed as a
> measurement. What *does* survive that sample size is the density:
>
> ```
> $ intentradar eval density --testset bootstrap-2026-09-27
>
> sub                 posts  actionable  borderline  unreviewed   density
> indiehackers            7           1           0           0     14.3%
> startups               23           1           1           0      4.3%
> SaaS                   48           0           5           0      0.0%
> microsaas              48           0           3           0      0.0%
> Entrepreneur           11           0           0           0      0.0%
> ──────────────────────────────────────────────────────────────
> TOTAL                 137           2           9           0      1.5%
> ```
>
> `r/SaaS` + `r/microsaas` + `r/Entrepreneur` contribute **107 posts and zero
> positives**. General-founder subreddits are the wrong place to look for buying
> intent — that conclusion holds at n=2 where a ratio would not. Growing the
> bootstrap sample is an open decision, not a silent assumption.

> **Honest note on the denominator.** The production run that first produced this
> number executed at 2026-09-27 01:40 and saw **208** posts in its rolling
> 3-day window (6/208 = 2.9%). The frozen snapshot was taken 35 minutes later,
> by which time the rolling window had drifted to **211** posts (6/211 = 2.8%).
> The hit set is byte-identical — 6 ids, same scores, same reasons — only the
> denominator moved. We publish the frozen snapshot's own number rather than
> retro-fitting the data to 208.

### Measured accuracy: `eval score`

A hit rate alone says nothing about whether the hits are any good. `eval score`
scores each layer against the human ground truth in
`data/testset/<id>/labels.csv`.

**Every rate is printed with its raw counts, the sample size, and a 95% Wilson
confidence interval.** This is deliberate: einprag has **8 positive examples**,
so one relabelled row moves precision by 12 points. `rule_v3`'s precision is
`20.0%` with a 95% CI of `[4%, 62%]` — which is the honest way of saying "on
this sample you have learned almost nothing". We publish the error bar rather
than the bare percentage because a number without one is not a measurement.

**Stage 1 — the shipping default, `rule_v3`:**

```
$ intentradar eval score --testset einprag-2026-09-27

testset : einprag-2026-09-27   (frozen 2026-09-27)
truth   : einprag-2026-09-27c  labels.csv sha256=4ecc9c5d1569
project : Einprag   min_score=5
labels  : 211 rows · 183 labeled (8 actionable / 175 not) · 0 unlabeled · 28 borderline
──────────────────────────────────────────────────────────────
metric                      rule_v3 (v3.0.0)  rule_v3+llm (v3.1.0+llm)
predicted                                  6                         0
true positive                              1                         0
false positive                             4                         0
false negative                             7                         8
precision                       20.0%  (1/5)                         -
precision 95% CI                   [4%, 62%]                         -
recall                          12.5%  (1/8)               0.0%  (0/8)
recall 95% CI                      [2%, 47%]                 [0%, 32%]
F1                                     15.4%                         -
noise rate                             80.0%                         -
unverified hits                            1                         0
positives (n)                              8                         8
```

`rule_v3` is a regex heuristic and it behaves like one: of the 8 posts a
reviewer marked actionable it finds 1, and 4 of its 6 hits are marked
`not_actionable`. The `rule_v3+llm` column here is **not a measurement** — with
no API key the semantic layer runs against the deterministic mock, and the
report says so in as many words.

**Stage 2 — the opt-in v4 pipeline, `rule_v4` → `rule_v4+llm`:**

```
$ intentradar eval score --testset einprag-2026-09-27 \
      --layers rule_v4,rule_v4+llm --min-score 3

metric                      rule_v4 (v4.0.0)  rule_v4+llm (v4.3.0+llm)
predicted                                 42                        14
true positive                              8                         8
false positive                            29                         3
false negative                             0                         0
precision                      21.6%  (8/37)             72.7%  (8/11)
precision 95% CI                  [11%, 37%]                [43%, 90%]
recall                         100.0%  (8/8)             100.0%  (8/8)
recall 95% CI                    [68%, 100%]               [68%, 100%]
F1                                     35.6%                     84.2%
noise rate                             78.4%                     27.3%
```

This is the two-stage funnel, and it is the core of the design:

| stage | candidates | precision | recall |
|---|---|---|---|
| `rule_v4` — wide net | 42 | 21.6% | **100%** |
| `rule_v4+llm` — semantic gate | 14 | **72.7%** | **100%** |

The rule layer is a **net, not a decider**: it is deliberately over-inclusive
and costs nothing, so it can afford 100% recall at 21.6% precision. The LLM
gate then removes 28 of the 42 candidates. **Recall does not move; precision
goes up 3.4×.** A single-stage scorer has to trade one against the other.

> **Which model produced the stage-2 numbers.** They come from
> **`deepseek-v4-flash`, reached through a local Anthropic-shaped dev proxy**
> (`http://127.0.0.1:8787/v1/messages`). That is a development verification
> channel, **not** the hackathon's target model. Every run prints
> `backend=… · model=…` so these figures can never be mistaken for
> Nebius/Nemotron numbers by accident. Re-measuring on Nebius is pending.
> (A re-run served from the response cache reports `0 calls`; the first run
> made 42.)

> **Sample size, stated up front.** n=8 positives, einprag. `precision 72.7%
> 95% CI [43%, 90%]`. A pending round of ground-truth adjudication raises the
> positive count 8 → 10 and moves these figures; this page will be refreshed in
> the same commit as that batch, not quietly afterwards. **The expected move
> (72.7% → 80.0%) is a relabelling effect, not a model improvement** — see
> [Failure cases](#failure-cases-are-published).

> **Version discipline.** The stage-2 numbers above are **`v4.3.0+llm`**.
> `v4.4.0+llm` exists and is **unmeasured**: it syncs the rest of
> `label_judgement_standard` into the prompt — criterion (1) judged by *demand
> domain* rather than SKU, and rules (a) free-only, (b) research-stage, (c)
> first-person build-vs-buy. It will be measured in the same run as the pending
> batch. Until then its number is "unknown", and unknown is not 72.7%.

### When the sample is too small: `eval density`

At n=8 the correct move is not to polish the ratio, it is to change the unit of
analysis. `eval density` breaks the ground truth down by subreddit, so you can
see **where** the positives live and which sources produced nothing at all:

```
$ intentradar eval density --testset einprag-2026-09-27

testset : einprag-2026-09-27   project: Einprag
truth   : einprag-2026-09-27c  labels.csv sha256=4ecc9c5d1569
──────────────────────────────────────────────────────────────
sub                 posts  actionable  borderline  unreviewed   density
languagelearning       23           3           3           0     13.0%
Anki                   43           3           4           0      7.0%
medicalschool          47           1           4           0      2.1%
premed                 48           1           2           0      2.1%
GetStudying            50           0          15           0      0.0%
──────────────────────────────────────────────────────────────
TOTAL                 211           8          28           0      3.8%

Only 8 actionable post(s) in 211. A precision / recall figure on this few
positives is noise — one sample flipping moves it by 12 points.
Zero-actionable subreddits: GetStudying
```

6 of the 8 positives come from two subreddits; `GetStudying` supplied 50 posts
and **zero** positives, with 15 rows the reviewer could not decide. That is a
statement about the data that survives a relabelling, where "precision 72.7%"
does not. `eval score` refuses to print a bare ratio once positives drop below
10 and points here instead.

### How every number is defined (口径)

Every published figure is computed the same way, and the definitions are part of
the contract — change one and the number is no longer comparable.

| Term | Definition |
|---|---|
| positive | `labels.csv` says `actionable` |
| negative | `labels.csv` says `not_actionable` |
| excluded | `borderline` or *(blank)* — **excluded from every numerator and denominator, and counted out loud** |
| `predicted` | every post the layer flagged, including ones with no human decision |
| precision | TP / (**scored** predicted hits) — the denominator is *not* `predicted` |
| recall | TP / (all labelled positives), whether or not the layer flagged them |
| noise rate | FP / scored hits = 1 − precision |
| F1 | harmonic mean of precision and recall |
| `unverified hits` | predicted hits with no human decision yet — they move no metric, but they are printed so the residue is visible |
| 95% CI | **Wilson score interval**, z = 1.959963984540054 |

Two consequences worth reading twice:

1. **`predicted` ≠ precision's denominator.** `rule_v4` predicts 42 but scores
   `8/37`: five of the 42 sit on `borderline`/blank rows and cannot be judged
   either way. Quoting 8/42 (19.0%) instead of 8/37 (21.6%) would be a
   different, silently pessimistic number.
2. **A blank or `borderline` row is never turned into `not_actionable`.** Doing
   so would manufacture false positives and *inflate* precision — the one
   direction of error that looks good in a README.

Wilson, not the normal approximation: at n=8 the textbook interval returns
bounds outside `[0, 1]`, which is how people end up publishing "precision
100% ± 40%".

### How the ground truth is handled

| Cell in `labels.csv` | Meaning | Effect on the metrics |
|---|---|---|
| `actionable` / `1` | a human would act on this | positive |
| `not_actionable` / `0` | a human would not | negative |
| `borderline` | the reviewer genuinely could not decide | excluded, counted, never scored as 0 |
| *(blank)* | not reviewed yet | excluded, counted |

An unrecognised value is a hard `ConfigError` (exit 2) naming every offending
row, so a typo is fixed rather than absorbed.

**Ground truth is per-testset, and it is fingerprinted.** Every report prints
`truth : <label_version>  labels.csv sha256=<12 hex>`. `eval score` samples that
fingerprint **before and after** scoring, and if it moved mid-run it raises
`ConfigError` (exit 2) instead of printing a number computed against two
different truths. This is not paranoia: during this project `labels.csv` changed
three times while a measurement was in flight, and a figure was nearly published
against a state of the file that no longer existed.

The criteria a reviewer applies live in
`data/testset/<id>/meta.json` → `label_judgement_standard`, and are quoted
verbatim into the judge's prompt. A test asserts the two stay in sync, so the
model is never graded against criteria that exist only in a chat message.

### Failure cases are published

A hit rate alone is meaningless without the misses, so every false negative and
false positive gets audited individually and written up in
[`SIGNIFICANT_UPDATES.md`](SIGNIFICANT_UPDATES.md) — including the ones that
were our fault. Two examples from this project, both recorded there:

- A judge version (`v4.2.0`) dropped recall from 100% to 62.5% because the
  prompt required a **specific product name** while the written standard only
  required the **category** to be explicit. We reported it as a precision/recall
  trade-off. It was a bug — the prompt had been written from a paraphrase in a
  chat message instead of from `label_judgement_standard`. The version is
  retired and its numbers are published only as the cost of that bug, never as
  a result.
- A pending relabelling will move stage-2 precision from **72.7% (8/11)** to
  **80.0% (8/10)** with the model unchanged — a *relabelling effect*, not a
  model improvement, and it must never be reported as the next version being
  better. An earlier draft of that note said 88.9%, on the assumption that two
  rows would move; the team lead overruled one of them (`1wq5s03` stays
  `not_actionable`), so only one moves and the figure is 80.0%. Both the
  correction and the overrule are recorded.

If the semantic layer had failed to beat the baseline, that would be published
too — the point of the tool is that the number can be recomputed, so a
 favourable number nobody can check is worth less than an unfavourable one
 anyone can.

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

Two wire protocols are supported behind one client shell:

| `INTENTRADAR_LLM_BACKEND` | Endpoint shape | Notes |
|---|---|---|
| `openai` (default) | `POST /v1/chat/completions` | `system` travels as a message |
| `anthropic` | `POST /v1/messages` | `system` is a **top-level field**, not a message |
| `auto` | sniffs `/messages` vs `/chat/completions` | convenience for dev proxies |

The Anthropic adapter exists because reasoning models return a `{"type":
"thinking"}` block **before** the answer, so `content[0]` is not the answer —
the client takes the first block with `type == "text"`. It also needs a larger
`INTENTRADAR_LLM_MAX_TOKENS` (default 8192): at 2048 a thinking model spends the
entire budget on reasoning and returns truncated, unparseable JSON.

> **The measured numbers on this page were not produced by Nemotron.** They come
> from `deepseek-v4-flash` over a local dev proxy. Every run prints
> `backend=… · model=…` and `config check` prints the resolved endpoint, so a
> figure can never be mis-attributed by accident. Nothing here should be quoted
> as a Nebius/Nemotron result; re-measuring on Nebius is pending.

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
  judge/      Judge protocol
                rule_v3.py  frozen regex baseline — verbatim port, never edited
                rule_v4.py  high-recall net (a filter, not a decider)
                llm.py      semantic gate; prompt mirrors label_judgement_standard
  llm/        one client shell (cache/retry/timeout/gate/accounting)
                backends: mock · openai (/chat/completions) · anthropic (/messages)
  budget.py   monthly budget + per-run gates
  pipeline.py collect → judge → dedupe → report
  eval.py     frozen testsets, offline recomputation, scoring, density, exports
```

Two invariants hold the design together:

- **`rule_v3.py` is frozen.** It is a verbatim port of the scoring that produced
  the published baseline; editing it would silently move a number other people
  have already recomputed. New ideas go in a new file with a new version.
- **Every judgment carries `layer` and `judge_version`.** A published figure is
  only meaningful next to the version that produced it, so a retired version's
  numbers can never drift into a current claim.

---

## Significant Updates

See [`SIGNIFICANT_UPDATES.md`](SIGNIFICANT_UPDATES.md) for the written record of
updates made during the hackathon submission period.

## License

MIT — see [`LICENSE`](LICENSE).

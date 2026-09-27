# Significant Updates

Written record of the significant updates made to this project **during the
submission period** of the Nebius × NVIDIA hackathon (2026-08-26 → 2026-10-30).
Required by the contest rules; updated weekly. Every entry is traceable to a
commit range in this public repository.

---

## W1 — 2026-09-27 → 2026-10-03 · Foundation + environment

**Scope of this week:** repository skeleton, migration of the existing
production monitor script into a maintainable package, Nemotron client
abstraction, cost gates and budget circuit-breaker, and a reproducible
accuracy baseline. No web UI, no reply drafting, no deployment.

### What was built

| # | Update | Why it matters |
|---|---|---|
| 1 | **Public repo created under MIT**, with `LICENSE`, `README.md`, `.gitignore`, `SIGNIFICANT_UPDATES.md`, and CI. | Baseline compliance for an open-source submission. |
| 2 | **Migrated `scripts/monitor.py` into the `intentradar` package** (`collect/` · `judge/` · `report/` · `config/` · `state/` · `pipeline.py`). The v3 scoring logic was moved **verbatim** — every regex, every list order, every `break`, every `[:24]` truncation. | The published 2.8% hit rate is only trustworthy if the scoring behaviour is byte-identical after migration. |
| 3 | **Regression test locked to a frozen dataset** (`data/testset/einprag-2026-09-27/`). Re-running the judge over the snapshot must reproduce the exact hit set (ids + scores + signals + reasons). This runs in CI. | Makes the published number reproducible by a stranger on a fresh machine. |
| 4 | **Removed the hardcoded proxy** (`http://127.0.0.1:10808`). All HTTP now goes through `httpx`, which honours the standard `HTTPS_PROXY` / `HTTP_PROXY` environment variables. A source-scan assertion in `tests/test_collect.py` fails the build if the literal ever comes back. | The old value leaked a local setup and made the tool fail on anyone else's machine. |
| 5 | **Nemotron client with a mock backend behind the same shell** (`llm/client.py`). Cache, retries, timeouts, budget gate, cost accounting and call counting all live in `NemotronClient`; `MockBackend` and `HttpBackend` only implement `_chat()`. | Engineering is not blocked by the missing API key, and swapping in the real key requires zero code change. |
| 6 | **Cost gates and budget circuit-breaker** (`budget.py`). `MAX_POSTS_PER_SOURCE` (soft cap), `MAX_LLM_CALLS` (hard stop), monthly USD budget persisted to `data/usage.json` with 90% warn / 100% refuse. | A single mis-configured batch run could otherwise burn all free credits. |
| 7 | **Machine-readable evidence on every judgment.** Each lead carries `evidence[]` (pattern, matched phrase, value, co-occurrence) plus `layer` and `judge_version`. | "Every judgment is auditable" is a claim only if the reason is structured data, not prose. |
| 8 | **`intentradar eval run` / `eval export` / `eval snapshot`** and the frozen public testset. | This is the mechanism by which anyone can recompute our headline number. |

### Numbers

Recompute both yourself, offline, on a fresh clone:

| Testset | Posts in window | Hits | Hit rate |
|---|---|---|---|
| `einprag-2026-09-27` | 211 | 6 | **2.8%** |
| `bootstrap-2026-09-27` | 137 | 12 | 8.8% |

```
intentradar eval run --testset einprag-2026-09-27
```

**Honest note on the denominator.** The production run that produced the first
published figure executed at 2026-09-27 01:40 and saw **208** posts in its
rolling 3-day window → 6 hits → **2.9%**. The frozen snapshot was taken 35
minutes later, by which time the window had drifted to **211** posts → the same
6 hits → **2.8%**. The hit set is byte-identical; only the denominator moved.
We publish the frozen snapshot's own number instead of retro-fitting the data.

- **Y (accuracy with semantic judgment) is deliberately not published this week.**
  The X → Y comparison experiment is the sole focus of W2 and will be added here
  when measured.

### Known limitations, stated honestly

- The rule layer is a regex heuristic. It produces false positives (generic
  complaints that happen to contain a category word) and false negatives
  (sarcasm, past tense, hypotheticals). Failure cases will be published in W2.
- Manual review labels are not yet filled in; `labels.csv` ships with empty
  `label` columns.
- Only one data-source provider exists (ScrapeCreators). Provider fallback is
  an interface stub, not implemented.

---

## W2 — 2026-10-04 → 2026-10-10 · Semantic layer + measured accuracy

**Scope of this week:** the LLM judgment layer (`rule_v3+llm`), a configurable
LLM backend, the `eval score` command that measures precision / recall / F1 for
both layers, and a semantics fix in the collection gate.

### What was built

| # | Update | Why it matters |
|---|---|---|
| 1 | **`judge/llm.py` — the `rule_v3+llm` layer.** Rule-layer candidates (score ≥ 3) are confirmed by an LLM that must answer `{is_actionable, confidence, reason}`. The judgment criteria are **hard-coded in `SYSTEM_PROMPT`**, not passed as free-form instructions. | Reproducibility: two people on the same commit ask the model the identical question, so the published precision can be recomputed. |
| 2 | **The layer is subtractive by design.** It can remove a rule hit; it can never invent one. Its evidence (`llm_verdict` / `llm_confidence` / `llm_reason` / `llm_model` / `llm_error`) is persisted on every judgment. | Recall stays bounded by the rule layer, so the precision/recall trade-off is a property of the design rather than an accident. Every lead stays auditable without re-running. |
| 3 | **Configurable LLM backend.** `INTENTRADAR_LLM_BASE_URL` / `_API_KEY` / `_MODEL` override `NEBIUS_*`; the protocol stays OpenAI-compatible. `config check` prints which variable supplied each value. | Nebius Token Factory's billing-country list does not include every country, so onboarding can be blocked. The tool must not be hard-wired to one vendor. |
| 4 | **`intentradar eval score`** — precision / recall / F1 / noise rate for `rule_v3` and `rule_v3+llm` **side by side**, from `data/testset/<id>/labels.csv`. | This is the "publish the accuracy" promise finally made measurable, with both numbers shown including the ugly one. |
| 5 | **Ground-truth honesty rules in `LabelSet`.** Blank = unreviewed, `borderline` = undecidable; both are excluded from every ratio **and counted**. An unrecognised label is a `ConfigError` naming every offending row. | Never silently scoring an undecided row as 0 would manufacture false positives and inflate precision. |
| 6 | **`MAX_POSTS_PER_SOURCE` is now really per source.** The counter was a single global `posts_seen` compared against a per-source cap, so 5 subreddits × 40 posts tripped a cap of 200 and silently truncated the last subreddit. Counting moved into `posts_per_source[sub]`; `posts_seen` survives as a display total only and the summary reads `posts 200 (max/source 200, 5 sources)`. | The name promised per-source, the implementation was per-run. Two tests pin both directions (5×40 must not trip; one source over the limit must trip). |

### Numbers

`intentradar eval run` is unchanged and still matches the frozen baselines:

| Testset | Posts in window | Hits | Hit rate |
|---|---|---|---|
| `einprag-2026-09-27` | 211 | 6 | **2.8%** (MATCH) |
| `bootstrap-2026-09-27` | 137 | 12 | 8.8% (MATCH) |

`intentradar eval score` against the reviewed ground truth (184 of 211 rows
decided, 27 `borderline`, 0 unreviewed):

| | `rule_v3` | `rule_v3+llm` |
|---|---|---|
| predicted | 6 | 0 |
| true positive | 0 | 0 |
| false positive | 4 | 0 |
| false negative | 7 | 7 |
| **precision** | **0.0%** | not measured (mock) |
| **recall** | **0.0%** | not measured (mock) |

**This is the finding of the week, and it is bad: `rule_v3` has 0 true
positives.** All 6 rule hits were labelled `not_actionable` (4) or `borderline`
(2); all 7 posts a reviewer marked `actionable` scored below the threshold of 5
and became false negatives. We verified each id individually — this is not a
scoring bug, it is what a regex heuristic actually scores against a human
reading the same 211 posts. We are publishing it rather than tuning thresholds
until the number looks better, because a hidden number is worth nothing.

The `rule_v3+llm` column is **not** a measurement: with no API key configured
the semantic layer runs against the deterministic mock, and the report prints
that warning explicitly. Real X → Y numbers require a working endpoint.

### Known limitations, stated honestly

- **The semantic layer is unmeasured.** No LLM endpoint is reachable from this
  machine (Nebius onboarding is blocked), so `rule_v3+llm` numbers here are mock
  output. The code path, the prompt, the parsing and the failure policy are all
  tested offline with a fake client; the accuracy is not.
- The ground truth is an AI-assisted draft pass (`reviewer=ai-draft`), not a
  careful multi-human adjudication. The 0% figure should be re-measured once a
  human reviews the same rows.
- The LLM layer is subtractive only: it cannot rescue a post the rule layer
  scored below `candidate_min_score=3`, which is where most of the 7 false
  negatives live. Fixing that is W3 work, not a silent tweak.
- Only one data-source provider exists (ScrapeCreators). Provider fallback is
  an interface stub, not implemented.

---

_Each subsequent week will be appended below._

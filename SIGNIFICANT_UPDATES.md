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

---

## W2 follow-up — P0-A `rule_v4` and P0-B Anthropic backend

Two P0s followed from the 0% finding above. The diagnosis was: the W2 semantic
layer is **subtractive only**, so with `rule_v3` recall at 0 there was nothing
for it to subtract from. Unlocking it required fixing rule recall first.

### ⚠️ The published 0% no longer reproduces — needs a ruling

Re-running `eval score` against the **`labels.csv` as committed today** does
not give 0%. It gives:

```
labels  : 211 rows · 183 labeled (8 actionable / 175 not) · 0 unlabeled · 28 borderline
metric                      rule_v3 (v3.0.0)
predicted                                  6
true positive                              1
false positive                             4
false negative                             7
precision                              20.0%
recall                                 12.5%
```

The 0% section above was computed against an older label set — `211 rows ·
184 labeled (7 actionable / 177 not) · 27 borderline`. Since then one post
moved `not_actionable → actionable` and one `not_actionable → borderline`, and
one of `rule_v3`'s 6 hits is now a true positive.

**Nothing in the code changed to cause this.** `rule_v3` is byte-frozen and
`eval run` still reports 211 / 6 / 2.8% MATCH — the hit set is the same 6 ids.
Only the ground truth moved under it.

This blocks the "0% is our public headline" decision rather than vindicating
it: a reviewer who clones and runs `eval score` will get 20.0% / 12.5% and
find the README disagreeing. Either the README must be re-cut against today's
labels, or the label change that produced the 8th actionable must be
re-adjudicated. **Left as-is pending a decision** — the code is committed, the
headline is not mine to rewrite.

### P0-A — `judge/rule_v4.py`, a high-recall candidate net

`rule_v3` is **frozen** at its published numbers (see above; `eval run` still
reports 211 posts / 6 hits / 2.8% MATCH). v4 is a **new file with its own
version**, added alongside, never editing v3's scoring logic.

Design shift: the rule layer is a **net that feeds the LLM**, not a decider.
"Wide in, strict out" — v4 is deliberately over-inclusive and expects the
semantic layer to remove the noise; v3 tried to be precise on its own and
scored *pain* instead of *intent*.

| change | why |
|---|---|
| `suck` / `hate` / `frustrated` / `tired of` demoted out of the +4 SWITCH signal into a +1 `WEAK_SENTIMENT` | these generated essentially every v3 false positive |
| ASK patterns widened (`does anyone have/know/use/recommend`, `cannot find`, `which … should I`, `suggestions for`, `do you use a/any`, `ways to find`, plus a noun slot so `what apps do you use` matches) | v3's `what (do|are) you use` could not match a noun between "what" and the verb |
| `SUPPORT_RE` bug/support filter added | bug reports and tech-support threads are not purchase intent |
| category gate kept but widened | the gate is what keeps the net from becoming "every post" |

Measured against the labelled data (`scripts/compare_layers.py`, not opinion):

| testset | `rule_v3` recall | `rule_v4` recall | `rule_v4` candidates |
|---|---|---|---|
| einprag | 1 / 8 (12.5%) | **8 / 8 (100%)** | 42 (budget ≤ 60) |
| bootstrap | 0 / 2 (0%) | **2 / 2 (100%)** | 49 (budget ≤ 60) |

Targets set for acceptance (einprag ≥ 6, bootstrap 2/2, candidates ≤ 60) are
met. Known looseness, recorded rather than hidden: `alternative` /
`replacement` / `recommendation` are intent words that *also* appear in the
category-hint list, so a phrase built on one satisfies the gate by
construction. The candidate budget absorbs it today; it is the first thing to
tighten if volume grows.

### P0-B — Anthropic backend, and the first real LLM numbers

`llm/client.py` gained an `AnthropicBackend` (`POST /v1/messages`) behind the
same `NemotronClient` shell, selectable via `INTENTRADAR_LLM_BACKEND=anthropic`
or auto-sniffed from the URL. Cache / retry / timeout / gates / accounting stay
in the shell; the backend only implements `_chat()`. The mock stays testable
and CI stays fully offline.

Two protocol traps worth recording:

- `system` is a **top-level field** on Anthropic, not a message; the adapter
  moves it out of `messages`.
- Reasoning models emit a **`{"type":"thinking"}` block first**, so the answer
  is *not* `content[0]`. The adapter takes the first block with
  `type == "text"`.

**Real run — einprag-2026-09-27, 42 LLM calls, 0 errors:**

| metric | `rule_v4` | `rule_v4+llm` |
|---|---|---|
| predicted | 42 | 8 |
| true positive | 8 | 7 |
| false positive | 29 | 1 |
| false negative | 0 | 1 |
| **precision** | 21.6% | **87.5%** |
| **recall** | **100%** | **87.5%** |
| **F1** | 35.6% | **87.5%** |

```
llm: 42 calls · 0 errors · backend=anthropic @ http://127.0.0.1:8787/v1/messages · model=deepseek-v4-flash
```

**This is a dev-time verification channel, not the deliverable's model.** The
endpoint is a local Anthropic-shaped proxy and the model is
`deepseek-v4-flash`. The hackathon deliverable must quote **Nebius +
Nemotron**, and `describe_backend()` / `config check` now print protocol,
endpoint and model on every run precisely so these numbers can never be
re-labelled as Nemotron's by accident.

**Real run — bootstrap-2026-09-27, 49 LLM calls, 0 errors:**

| metric | `rule_v4` | `rule_v4+llm` |
|---|---|---|
| predicted | 47 | 0 |
| true positive | 2 | 0 |
| false positive | 40 | 0 |
| false negative | 0 | **2** |
| **precision** | 4.8% | – |
| **recall** | **100%** | **0.0%** |

Bootstrap is reported because it is bad. The LLM rejected **both** of
bootstrap's labelled-actionable posts, and we read its reasons rather than
averaging them away:

- `1wplixs` — *"The author is a builder doing market validation, not a buyer."*
- `1wpda04` — *"a strategy/advice request, not a product or service
  recommendation."*

Both rejections are coherent under the criteria as written ("purchase /
switch / seeking-recommendation intent"). `1wplixs` literally asks *"Do you use
a tool for it, or roll your own?"*, which is tool-seeking — but by a builder,
not a buyer, and our prompt does not say which one counts. **This is a
criteria-vs-ground-truth disagreement, not a bug**, and it is a decision for
the reviewer: either those two labels are generous, or the criteria should
count builder tool-seeking. We are not editing the prompt to make the number
move.

### Fix found by running it for real

Default `llm_max_tokens` was 2048. On a reasoning model the `thinking` block
consumed the entire budget and the JSON answer came back **truncated
mid-string**, which fails closed and silently costs recall (observed on post
`1wqb4v8`; the failure is also cached, so it persisted across runs). Raised to
**8192** to leave headroom for both halves. Before the fix: 1 error, precision
100% / recall 62.5%. After: 0 errors, precision 87.5% / recall 87.5%.

### Also fixed

- `eval score` only wired the real LLM client to `rule_v3+llm`; `rule_v4+llm`
  silently fell through to the **mock** and would have published a fabricated
  number. Any `+llm` layer now gets the real client (built once, shared).
- `config check` now prints the resolved protocol (`openai | anthropic | auto`)
  next to the endpoint, so the backend is explicit and never inferred.

### ⚠️ Correction: `v4.2.0` was a BUG, not a trade-off

An earlier revision of this file described the v4.2.0 recall drop as a
precision/recall trade-off and recommended "using it for cleaner operating
metrics". **That was wrong, and it is the most dangerous kind of wrong**: it
would have frozen a fixable defect into a product decision.

**Root cause.** The criteria were written into `SYSTEM_PROMPT` from a
paraphrase in a chat message. The authoritative text was never read:

> `data/testset/<id>/meta.json` → `label_judgement_standard`
> "(1) need category is explicit — **naming the CATEGORY is enough, a specific
> product name is not required**"

The prompt required the opposite: it demanded that the author name a **specific
product**, and it rejected outright any post asking for "ways / strategies /
methods / tips". That silently cut recall. It was not the standard changing —
it was the implementation contradicting the standard.

The prompt now carries a comment pointing at `label_judgement_standard` as the
source of truth, and a test cross-checks the prompt against `meta.json` so the
two cannot drift silently again.

### `v4.3.0+llm` — implements the authoritative standard

Three conditions, all required, matching `label_judgement_standard`:

1. **CATEGORY IS EXPLICIT** — enumerated as product/tool/app, **service**,
   **way / approach / solution / method**, or **capability**. *Naming a
   specific product is never required.* The prompt is explicitly told it is not
   judging whether the category is exactly what the monitored project sells —
   only whether the post is *clearly unrelated*.
2. **THE NEED IS UNMET AND THEY ARE ACTIVELY SEEKING**.
3. **IT IS THE AUTHOR'S OWN DECISION** — they hold or share it.

Two self-inflicted contradictions were also removed: a "promoting a product in
the monitored category is a competitor" rule was killing `1wplixs` while a
"don't reject builders" rule was trying to save it. Self-promotion now requires
actual pitching (link, advert, asking for users), not merely building something.

### Measured, both testsets, real LLM, labels verified stable before and after

Against `einprag-2026-09-27c` (8 actionable / 28 borderline / 175 not) and
`bootstrap-2026-09-27d` (2 / 9 / 126):

| metric | einprag v4.1.0 | einprag v4.2.0 (bug) | **einprag v4.3.0** |
|---|---|---|---|
| predicted | 8 | 5 | 14 |
| true positive | 7 | 5 | **8** |
| false positive | 1 | 0 | 3 |
| false negative | 1 | 3 | **0** |
| **precision** | 87.5% | 100.0% | **72.7%** |
| **recall** | 87.5% | 62.5% | **100.0%** |
| **F1** | 87.5% | 76.9% | **84.2%** |

| | bootstrap v4.1.0 | bootstrap v4.2.0 (bug) | **bootstrap v4.3.0** |
|---|---|---|---|
| true positive | 0 / 2 | 0 / 2 | **1 / 2** |
| **recall** | 0.0% | 0.0% | **50.0%** |
| **precision** | – | – | 100.0% |

`rule_v4` (the rule net) is unchanged throughout: **8/8 einprag, 2/2
bootstrap — 100% recall** in every column. Only the LLM gate moved.

**This precision figure is the real trade-off** the earlier note wrongly
claimed: recall 87.5% → 100% bought with precision 87.5% → 72.7% (3 false
positives out of 14 candidates). That is a genuine operating choice, and it is
now made on a correct implementation rather than on a bug.

### Residual: `1wplixs` still rejected (stopped tuning on purpose)

`1wpda04` is recovered. `1wplixs` is not — the model reads *"Do you use a tool
for it, or roll your own?"* as *"market research for a platform they are
building … rather than seeking a solution for their own unmet need"*, i.e. it
fails condition (2) in the model's reading, not condition (3).

Two prompt iterations have already targeted this single post. A third would be
**fitting the rule to 2 positive examples**, which is exactly how a benchmark
number stops meaning anything. It is stopped here and reported instead: either
the ground truth treats a first-person buy-vs-build question as satisfying
condition (2) — in which case the standard needs a sentence saying so — or the
model's reading stands. Not an engineering call.

### Ground-truth fingerprinting — numbers now name their own revision

`labels.csv` was edited **three times while an evaluation was running**, and one
run produced a number (6 actionable / 30 borderline) describing a state that no
longer existed. A measurement is worthless if the thing being measured moved
mid-flight, so:

- `eval.py` gained `label_fingerprint()` → `(label_version, sha256(labels.csv))`.
- `EvalScorer.score()` fingerprints **before and after** and raises
  `ConfigError` (exit 2) if they differ. **Hard fail, not a warning** — a
  warning is ignored the moment anyone batch-runs this.
- Every report now prints a `truth   : <label_version>  labels.csv sha256=…`
  line, so a published number can be traced to the exact ground truth.
- A test asserts both committed testsets declare a `label_version`.

### False positives are audited too, not just false negatives

We reviewed every false negative in detail and no false positive at all, which
biases precision upward: a label that is wrong in the *model's favour* was
never being questioned. The 3 einprag FPs under `v4.3.0+llm`, with the model's
reason and the reviewer's note:

| id | model's reason | reviewer's note | read |
|---|---|---|---|
| `1wq5s03` | *"Is there a low-stakes way to practice speaking outside of an app?"* — a named category, actively sought | "anxiety about Mandarin tones; asks for reassurance, not a product" | genuinely arguable both ways |
| `1wq8u54` | *"is there an alternative algorithm to optimize my repetitions…"* | "technical discussion on whether FSRS is optimal; academic, no purchase intent" | leans model error — academic is excluded |
| `1wqq5om` | *"is there any apps or tools that people use to remove distractions…"* | "built their own distraction-blocking browser and is surveying; maker, not a customer" | **collides with the proposed buy-vs-build rule** — see below |

Sent to the reviewer for per-row adjudication.

**Cross-check on the proposed buy-vs-build rule.** It holds up as a general
rule — an explicit tool-selection or buy-vs-build question distinguishes "how
do *you* do it" (research) from "should I buy or build" (a decision). But it
collides with `1wqq5om`, which contains an explicit selection question
("any apps or tools that people use…") and is labelled `not_actionable` with
"maker, not a customer". So adopting the rule will either flip that label or
add a false positive. **Flagged before the rule is written into the standard**,
not after.

### Known limitations, stated honestly

- **The headline numbers are still not Nemotron's.** They come from a local
  proxy serving `deepseek-v4-flash`. Re-running against Nebius is required
  before anything is published as the deliverable's accuracy.
- `rule_v3` remains the **default** pipeline layer, so the published 0% is
  still what ships unless v4 is explicitly selected. Promoting v4 to default
  is a separate decision.
- **Bootstrap's denominator is n=2.** Precision/recall on n=2 is noise: one
  sample flipping is a 50-point swing. The reviewer recommends **not**
  publishing a rate for it and reporting instead a **density + negative-control
  conclusion** — e.g. r/SaaS + r/microsaas + r/Entrepreneur total 107 posts
  with 0 actionable, 1/137 = 0.7% overall, i.e. general-founder subreddits are
  the wrong target for intent monitoring. That is both publishable and more
  honest than an n=2 ratio. Whether to grow the sample is the team lead's call.
- The LLM response cache stores failures as well as successes, so a transient
  parse failure sticks until the cache is cleared.

---

_Each subsequent week will be appended below._

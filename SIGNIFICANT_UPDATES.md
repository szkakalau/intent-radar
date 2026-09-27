# Significant Updates

Written record of the significant updates made to this project **during the
submission period** of the Nebius × NVIDIA hackathon (2026-08-26 → 2026-10-30).
Required by the contest rules; updated weekly. Every entry is traceable to a
commit range in this public repository.

> **How to read the numbers in this file.** This is a **historical ledger**, not
> a published claim. Figures recorded here are quoted as they were printed at
> the time, and several predate the tool printing 95% Wilson intervals — so some
> old rows state a percentage without counts or an interval, and they are left
> that way deliberately: retro-fitting intervals onto a superseded run would be
> inventing them. **Only the README carries citable figures**, and every figure
> there is guarded by a test (`tests/test_docs_honesty.py`) that fails if a rate
> appears without both its counts `(x/y)` and its interval on the same line.
>
> **The exemption is per-file, not per-number.** It covers this ledger only, and
> it does **not travel with a number**: if any figure from here is quoted into
> the README, a submission blurb, or any outward-facing page, that copy must
> carry its counts and its interval — or drop the percentage and state the
> counts alone. A number that leaves this file leaves its exemption behind.

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
### W1 follow-up, part 2 — same day, after QA audit

The QA audit (see `tests/test_video_figures.py`) caught two ways the demo
materials were borrowing authority they did not have:

| Issue | Why it happened | Fix |
|---|---|---|
| Video said **"9 of 91 judgements hit a 503"** | The 91 total is verifiable from the two manifests (42 + 49 calls), but the retry count is only logged for the published `einprag` run. The video claimed the figure was test-covered. | Changed to **"5 of the 42 judgements in the published run"**, matching the README. The video now explicitly says this number is **not** regenerated by the test suite. |
| Video said **"348 hand-labelled posts"** / **"313 usable"** | 348 is right; usable is **312** (einprag 184 + bootstrap 128). The 313 was carried over from a summary without recomputing. | Fixed to **"348 labelled rows · 312 with a verdict"** in dashboard and video, and added a test that reads `labels.csv` directly. |
| Video said **"uv run intentradar eval score"** | This is the console-script form, which can hit the Windows os error 4551 the README already documents. | Updated to **"uv run python -m intentradar eval score"** in both video and dashboard. |

All six rates and four intervals on the video's funnel table are now guarded
by `tests/test_video_figures.py`: the test renders the committed replays and
requires every printed figure to appear verbatim in the rendered output. A
hand-typed number that drifts from the reproducible one turns the suite red.

### W1 follow-up, part 3 — narration and captions

A submission video is mostly watched muted. The video had neither a voice nor
captions, so its content did not exist for anyone who did not unmute it.

| What | How |
|---|---|
| Voiceover | `scripts/demo/narrate.py` speaks one hand-written script per scene with `edge-tts` (`en-US-AndrewNeural`), and measures each clip. |
| Captions | Cue timings come from the TTS engine's own **sentence boundaries**, not from hand-typed timestamps: cue N starts exactly when the voice starts sentence N. 23 cues. |
| Placement | The voice is placed on the *picture's* timeline — each clip's offset is the cumulative `data-ms` read out of `docs/video.html`. `narrate.py` refuses to finish if any clip is longer than its scene, because a voice that overruns into the next slide is worse than no voice. |
| Burned in | Captions are rendered into the picture by `scripts/demo/assemble.py`, not shipped as a soft track: a soft track is one click most viewers will not make. |
| Reproducible | The whole recipe is three committed scripts (`narrate.py` → `record_video.py` → `assemble.py`). The ffmpeg invocation used to live in a shell history, which meant the shipped video could not be reproduced from the repo; it now lives in `assemble.py`. |

Measured alignment: the caption track's silent gap falls between 11.0 s and
11.5 s (the subtitle file says 11.412 s) and the next cue appears between
16.8 s and 17.5 s (file says 17.050 s) — the recording start and the scene
timeline agree to well under half a second, so nothing drifts.

`edge-tts` is declared in the `dev` extra of `pyproject.toml`. It is not needed
to run or verify the project, only to rebuild the video — but it is declared
rather than assumed, because a script that fails on a clean checkout is a
reproduction claim that only holds on the machine it was written on.

The spoken script is guarded like the on-screen text: every percentage the
narration says must be a percentage the committed recordings actually print
(`tests/test_video_figures.py`), and the labelled-row counts are checked
against `labels.csv` in all three surfaces — video, dashboard and script.

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

### `eval density` — the honest view of a small testset

`eval score` now refuses to present a rate on a handful of positives without
saying what it is, and `eval density` gives the alternative:

```
sub                 posts  actionable  borderline  unreviewed   density
indiehackers            7           1           0           0     14.3%
startups               23           1           1           0      4.3%
SaaS                   48           0           5           0      0.0%
microsaas              48           0           3           0      0.0%
Entrepreneur           11           0           0           0      0.0%
──────────────────────────────────────────────────────────────────────────
TOTAL                 137           2           9           0      1.5%

Only 2 actionable post(s) in 137. A precision / recall figure on this few
positives is noise — one sample flipping moves it by 50 points.

Zero-actionable subreddits: Entrepreneur, SaaS, microsaas
```

The negative-control conclusion — **general-founder subreddits (107 posts)
produced 0 actionable** — is a publishable finding; a rate on n=2 is not.
Borderline and unreviewed rows are counted in their own columns, never folded
into `not_actionable`.

### ⚠️ Pending ground-truth changes, and the metric jump they will cause

Adjudicated but **not yet committed** (labels.csv is still rev c / rev d; sha256
verified unchanged):

| id | from → to | why |
|---|---|---|
| `1wq1xms`, `1woxhua` | → actionable | reverse audit |
| `1wqq5om` | not_actionable → **borderline** | the old "maker, not a customer" reason was invalid (not one of the three criteria); fails criterion (2) — the author built a browser that "worked surprisingly well", so the need is already met |
| `1wq5s03` | **stays not_actionable** — team-lead overruled the reviewer | asks for a way "outside of an app", explicitly excluding the product form we sell; wants reassurance and practice advice, not a product |
| `1wq8u54` | stays not_actionable, **note rewritten** | "alternative algorithm" is outside the domain (criterion 1); the old note said "academic", which is not one of the three criteria |

**One of these is currently a false positive, so this relabelling will move
precision from 72.7% (8/11), 95% CI [43%, 90%] to 80.0% (8/10), 95% CI [49%,
94%] with the model unchanged.** That jump is a **relabelling effect, not a
model improvement**, and must never be reported as v4.4.0 being better. It is
recorded here so the next changelog cannot claim it by accident.

(Corrected: an earlier version of this note said 88.9% (8/9), from a draft in
which `1wq5s03` also moved to borderline. The team lead overruled that one, so
only `1wqq5om` moves and the figure is 8/10.)

The reviewer also caught a private criterion of his own: "maker, not a
customer" is not one of the three criteria — criterion (3) excludes
*promoting* your product, not merely having built something. That correction
is what moves `1wqq5om`.

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
  with 0 actionable, **2/137 = 1.5% overall** (corrected from an earlier
  "1/137 = 0.7%", which read a stale labels.csv), i.e. general-founder
  subreddits are the wrong target for intent monitoring. That is both
  publishable and more honest than an n=2 ratio. Whether to grow the sample is
  the team lead's call. The README now publishes exactly this — the density
  table, and no bootstrap ratio.
- The LLM response cache stores failures as well as successes, so a transient
  parse failure sticks until the cache is cleared.

### `eval run` stopped claiming there was no human review

`EvalReport.render()` hard-printed:

```
no manual review included in this run (see data/testset/<id>/labels.csv)
→ full accuracy incl. human review is delivered in W2 (`eval score`)
```

That was true in W1. It has been false for some time — `labels.csv` is filled in
and `eval score` exists — and **the README quoted the block verbatim**, so a
reader following the quickstart would be told the project has no ground truth
while the accuracy section two scrolls down is built entirely on one.

`EvalReport` now carries the dataset's `LabelSet` and prints what is actually
there:

```
reviewed hits           5/6   (1 undecided: borderline/blank)
noise rate              80.0%
ground truth: 183 labeled (8 actionable / 175 not) · 0 unlabeled · 28 borderline
→ full precision / recall: `eval score --testset einprag-2026-09-27`
```

The `reviewed hits` line is the point: it states how much of the published hit
set a human has adjudicated, and counts `borderline`/blank rows as undecided
instead of folding them into either side. When no `labels.csv` exists the report
says so rather than printing a 0% noise rate. Two tests pin this, including one
asserting the old W1 wording is gone — a stale string in tool output is a bug
even when nothing crashes, because documentation quotes it.

### `v4.4.0+llm`: prompt synced from the standard, not from chat

The labeler wrote the remaining rules into `label_judgement_standard`, so the
judge prompt has been synced **from `meta.json`**, which is what the team lead
asked for ("wait for the labeler to write it into meta.json, then sync from the
standard — never from a chat message"). Added to `SYSTEM_PROMPT`:

- **Criterion (1) is judged by DEMAND DOMAIN, not by SKU.** The named category
  does not have to match the product's own SKU, but must fall inside the domain
  that product can serve. The two failure modes are spelled out: (i) no category
  named at all, (ii) a category named but outside the domain — both false.
- **Rule (a)** an explicit ask for something FREE → no willingness to pay →
  false, but explicitly *not* "clearly unrelated".
- **Rule (b)** research stage ("anyone have experiences with X") → false.
- **Rule (c)** a first-person **build-vs-buy** question DOES satisfy criterion
  (2), with the test being whether buying is on the table as an option the author
  is choosing between — distinguished from (b) in the prompt so the two cannot
  collide.
- Every rejection reason must now **map back to one of the three criteria**; the
  standard calls anything else "a private standard", so the prompt says so.

The demand domain is **per project** (ground truth is per-testset), so it is a
new optional `demand_domain` field on `ProjectConfig` / `config/watchlist.json`,
falling back to `keywords` for projects that predate it, and it is injected into
the user turn. Version bumped `v4.3.0+llm` → **`v4.4.0+llm`**.

**`v4.4.0+llm` is unmeasured.** Numbers were deliberately not re-run until the
pending label batch lands; the README says so and quotes the v4.3.0 figures
under their own version. Five new tests pin the synced wording, including one
that cross-checks each rule against `meta.json`, so the prompt and the standard
cannot drift apart silently again.

### `v4.5.0+llm`: rule (b) rewritten — a rule that contradicted its own ground truth

`v4.4.0` synced `label_judgement_standard` faithfully, including rule (b):

> Asking others about their experience ("anyone have experiences with X") is a
> research stage and does NOT satisfy criterion (3) → borderline.

Applied literally, the model used it to reject **`1wpsql7`** — someone actively
weighing a **paid subscription** who asks existing users "is it worth it?" —
as *"research-stage evaluation rather than actively seeking a solution"*.
But `1wpsql7` is labelled **actionable**, and the same standard's own precedent
list names it as a confirmed actionable. The standard was contradicting itself,
and the prompt had carried the contradiction straight through to the model.

**Reported as a recall drop and diagnosed, not accepted as a trade-off.** Two
false negatives were pulled individually: `1wpsql7` (rule (b), our bug) and
`1woxhua` (a genuine model/reviewer disagreement).

Team-lead's ruling: **replace the test sentence, do not bolt on a carve-out.**
Rule (b) now reads — the test is *not* "is the author talking to other people",
it is *"is the author making an adoption / purchase decision FOR THEMSELVES"*,
with an explicit self-check line and both directions pinned by tests:
`1wpsql7` satisfies criterion (3); asking on behalf of someone else and market
research are still false. The same wording can go either way, so the prompt now
says to decide on the decision, not on the phrasing.

Two **other** lines in the prompt were saying the opposite and had to be fixed
at the same time, or the fix would have been half-applied:
- *"Asking a community for opinions is only intent if the opinion is 'which
  thing should I get/use'"* — excludes "is X worth it?", which rule (b) now
  accepts.
- rule (c)'s *"this differs from rule (b)"* — rewritten so (b) and (c) are
  stated as different questions that can both hold, rather than as alternatives.

Result, `labels.csv sha256=2009ef74b2d1` (rev d, 10 positives), einprag:

| | `v4.4.0+llm` | `v4.5.0+llm` |
|---|---|---|
| precision | 88.9% (8/9), 95% CI [57%, 98%] | **100.0% (9/9), 95% CI [70%, 100%]** |
| recall | 80.0% (8/10), 95% CI [49%, 94%] | **90.0% (9/10), 95% CI [60%, 98%]** |
| false positives | 1 | **0** |
| false negatives | 2 | **1** |

`1wpsql7` recovered and the last false positive disappeared. The one remaining
FN, `1woxhua`, is **left in on purpose** — the model reads it as musing about
curriculum design, the reviewer reads it as naming courses/textbooks/software.
Rewriting the prompt until it catches that post fits the rule to one example;
it would pass this testset and generalise worse.

### Staged figures in the README

The README's numbers were published **staged at n=8** and flagged as pending a
ground-truth batch, rather than left as placeholders until the batch landed. The
old Status block advertised "precision 0%", which was a number from before
`eval score` existed and no longer reproduced.

The batch has since landed (`rev d`, 10 positives) and the README now carries
**final, re-measured** figures: `rule_v3` precision 20.0% (1/5), 95% CI [4%,
62%] and recall 10.0% (1/10), 95% CI [2%, 40%] — note recall moved from 12.5%
to 10.0% because the denominator grew from 8 to 10 while the true positive
count stayed at 1 — and the v4 funnel at precision **100.0% (9/9), 95% CI
[70%, 100%]** and recall **90.0% (9/10), 95% CI [60%, 98%]** under
`v4.5.0+llm`. Every
figure is tied to `label_version` + `labels.csv sha256`, both printed by the
tool. Also disclosed: **the bootstrap denominator has not been reverse-audited
yet** (57-row audit owing, blocked on reviewer quota), so all rates on the page
are explicitly einprag-only.

`--min-score` is no longer taught as a tuning flag: `eval score` reads the
threshold from `meta.json`, with the single exception of `rule_v4`'s documented
candidate bar (3), which is explained as a property of the layer rather than a
knob. A "what `min_score` means" note states that a score of 5 means *"worth a
human look"*, not *"this person is a buyer"*.

### `--record` / `--replay`: the LLM column is now reproducible without an endpoint

The loudest claim in the README was "a stranger can recompute our number with
one command", and it was only true of the rule layer. The `rule_v4+llm` column
— the one that actually matters — required an endpoint nobody else has. The
judge would have been handed a table they could not check.

`eval score --record <dir>` freezes every model response; `eval score --replay
<dir>` re-derives the table from them with no network and no key. The 42
responses behind the published v4.5.0 table are committed under
`data/replay/einprag-2026-09-27/rule_v4_llm/`, with a [`data/replay/README.md`](data/replay/README.md)
stating plainly that they are **`deepseek-v4-flash` and NOT Nemotron**.

Design decisions worth keeping:

- **Seven fields per record**: post id, judge version, model, endpoint,
  timestamp, `prompt_sha256`, raw response. A record missing any of them is
  refused at load time — without provenance a frozen response is a cache, not
  evidence, because you cannot tell what question it answered.
- **A replay never fills a gap.** A miss raises `ConfigError` naming what
  differs (judge version, or prompt digest, or the post is absent). A replay
  that silently falls back to a live endpoint is not a replay.
- **A replay is not a mock.** It answers with real model output, frozen, so it
  must not inherit the mock's "NOT a measurement" label — otherwise the one
  form of offline reproduction we can publish disclaims itself.
- **The banner is printed by the report, above the numbers**
  (`REPLAYED — frozen model responses from <date>, model=<name>. Not a live
  run.`), so a screenshot of the table carries its provenance instead of losing
  it off-screen.

`tests/test_replay.py` records then replays a scripted non-trivial run and
compares `predicted / tp / fp / fn / precision / recall` field for field. It
deliberately does **not** compare `why` or evidence: those are explanatory
text, and comparing them would turn "the wording changed" into "the result
changed", diluting what the test proves.

### The live v4 table was deleted; the default command is now replayable too

Two follow-ups on the same defect, both about the same thing: **a second copy
of a table is a second place it can lie.**

1. The README carried the v4 funnel table twice — the live run's own output and
   the replay of it. The numbers were identical, so deleting the live copy lost
   no fact, and keeping it kept a block that no test could ever check (a live
   run cannot be reproduced offline). What was NOT deleted is the provenance:
   a line above the table now states that it is a frozen replay of one real
   deepseek-v4-flash run on 2026-09-27, offline-reproducible to the digit, and
   not a Nemotron result.

2. The default command — bare `eval score`, the one the README pushes first —
   still produced a mock `rule_v3+llm` column reading 0/10. That is the most
   prominent table on the page and it was the least reproducible. A default-tier
   recording (`rule_v3_llm`, 14 responses) is now committed, and the README's
   default block is the replay. Notably the recorded run served all 14 from the
   response cache, so the frozen responses are exactly the ones that produced
   the figure originally published — the number did not move, it became
   checkable.

The README comparison test is now parameterised over both tables, plus a test
asserting **no replay block exists outside that list** — otherwise adding a
third table would be a silent way to publish an un-checkable number with a green
suite. `_scorer_for` deliberately passes no tuning arguments at all, so the test
reproduces what `eval score` does rather than what a test author believes it
does.

### A published tool-output block must carry its own provenance

One defect, found earlier and now guarded: a report block in the README had been
generated by a run **with** an endpoint configured and then captioned as the
offline one, so the published number was the one a stranger could not
reproduce. Two guards now exist:

1. `tests/test_replay.py` replays the committed recording and compares **every
   row of the README's replay block line by line**. Verified non-vacuous:
   changing one digit in the published `precision` row turns the suite red.
2. `tests/test_docs_honesty.py` — the cheap version team-lead asked for — any
   fenced block quoting a rate must contain the report's own `testset :` or
   `truth :` line, or state `not tool output` in as many words. A hand-trimmed
   block loses exactly those lines.

Both were checked by mutating the README on purpose. Both caught it. (The
line-by-line comparison initially did not: `precision 95% CI` also starts with
`precision`, so the interval row was overwriting the rate row and the guard was
checking the wrong line. Names are now matched longest-first, and that is
pinned by its own test.)

### Nemotron readiness: proven before the key exists

The numbers on this page are `deepseek-v4-flash`. Re-running them on Nemotron
must not require a code change at the moment the key arrives — a fix attempted
under deadline pressure is how a wrong field gets shipped. So the whole path was
exercised against a stub **before** any key existed:

`scripts/nemotron_stub.py` serves an OpenAI-shaped `/v1/chat/completions` and
answers the judge's schema. Configured as
`INTENTRADAR_LLM_BACKEND=openai`, `INTENTRADAR_LLM_MODEL=nvidia/nemotron-3-super-120b-a12b`
over it, a full `eval score` run completes and its identity line reads verbatim:

```
backend=openai @ http://127.0.0.1:8791/v1 · model=nvidia/nemotron-3-super-120b-a12b
```

i.e. the report names the model from configuration and cannot silently attribute
one model's numbers to another.

**The one failure mode that cannot be pre-tested.** Nemotron 3 is a reasoning
model and emits reasoning before its answer. Where that reasoning lands in the
JSON is not knowable in advance — `reasoning_content`, a `reasoning` block, or
`content` itself — and mistaking it for the answer would produce a plausible
verdict parsed out of the model's own deliberation. Two defences:

1. `_extract_openai_content()` takes the answer from `content` only, and raises
   `ProviderError` naming the fields it did find when `content` is empty or
   absent, rather than falling through to a reasoning field. A stub that returns
   reasoning-only is **refused with an actionable message**, not silently parsed.
2. `scripts/dump_raw_response.py` dumps the raw payload of one real call,
   field by field, so the first run against the real key can be inspected to
   confirm the text being scored is not reasoning. `last_raw_response` was added
   to both HTTP backends for this — previously the raw payload was discarded,
   leaving no way to check after the fact.

Both are pinned by `tests/test_reasoning_response_shape.py`.

### The README's stage-1 table was quoting a number the tool no longer prints

Found while fixing something else. The README's `eval score` baseline block
showed `rule_v3+llm` at **predicted 1, precision 100.0% (1/1), recall 10.0%
(1/10)**. That block had been generated from a run made **with a real endpoint
configured**, then pasted under prose describing the column as the offline mock.
Run without a key — i.e. by a stranger following the README — the same command
prints **predicted 0, recall 0.0% (0/10)**: the deterministic mock answers
`false` to everything, so it removes every hit.

Two numbers, same command, and the published one was the one that could not be
reproduced. Fixed by regenerating the block from a keyless run and rewriting the
prose: the mock column is now shown as what it is, and the report's own three
"NOT a measurement" lines are quoted verbatim so the tool — not the prose — is
what labels it. The structural claim is kept and restated correctly (the
semantic layer can only remove hits, so `rule_v3+llm` recall ≤ `rule_v3`
recall).

### Rates in the tool's own output now carry counts

`eval run` printed `hit rate 2.8%` and `noise rate 80.0%` as bare percentages,
while the README was being held to a rule requiring counts and an interval
beside every rate. A discipline that binds the documentation but not the tool is
typography. Both now print their counts — `2.8% (6/211)`, `80.0% (4/5)` — and
`eval score`'s `noise rate` row does the same (`73.7% (28/38)`). `hit rate` was
added to the rate words in `tests/test_docs_honesty.py`, so the guard covers the
rates our own tool emits, and two tests pin the new format.

### Quickstart leads with the module form

`uv run intentradar …` fails to spawn on locked-down Windows environments
(`os error 4551`, application-control policy) — reproduced independently on a
clean clone. It was step 3 of the Quickstart with the module form relegated to a
note below, so the first command a reviewer ran could fail outright. The module
form `uv run python -m intentradar …` is now the primary command throughout;
the console script is mentioned as an alternative, not the default.

### The changelog exemption is per-file, not per-number

`tests/test_docs_honesty.py` holds published pages to the format rule (a rate
must carry counts + interval on the same line) and exempts exactly one file:
this ledger, and only while it keeps its disclaimer header. Exempting a
superseded historical figure is correct; exempting a *current* one is not. So
the current figures in this file — the v4.5.0 table above and the rev-d
baseline — were given real intervals rather than the exemption, and the test
asserts the header still states that a figure **leaving** this file loses the
exemption and must carry its counts and interval in its new home.

---

---

## W1 follow-up — 2026-09-27 · Nemotron re-measurement and public demo

Same frozen testset, same prompt, same code path, different model. No code
change between the DeepSeek run and the Nemotron run beyond the
`INTENTRADAR_LLM_MODEL` environment variable.

### What was added

| # | Update | Why it matters |
|---|---|---|
| 9 | **Nemotron re-measurement committed.** `nvidia/nemotron-3-super-120b-a12b:free` was run against `einprag-2026-09-27d` at `rule_v4+llm` / `--min-score 3`. The recording lives at `data/replay/nemotron-openrouter-2026-09-27/rule_v4_llm/` and is checked by the same line-by-line README test as the DeepSeek recording. | The hackathon deliverable asks for an NVIDIA open model. We published the result even though it made the required model look worse on precision. |
| 10 | **Demo page + recorded video.** `docs/index.html` renders the frozen recordings: the funnel, the two-model comparison, and all 42 judgements with human labels and model verdicts side by side. It requires no API key and makes no live call. `docs/video.html` is a self-playing animation of the same content, recorded to `docs/intentradar-demo.mp4` (179.96 s, under the 3-minute limit). | Hosted demo URL and demo video are both required submission artefacts; both are generated from committed files so they cannot drift from the repo. |

### Numbers

`rule_v4+llm` on `einprag-2026-09-27d` (10 positives):

| model | predicted | precision | recall |
|---|---|---|---|
| `deepseek-v4-flash` | 11 | 100.0% (9/9), 95% CI [70%, 100%] | 90.0% (9/10), 95% CI [60%, 98%] |
| `nvidia/nemotron-3-super-120b-a12b:free` | 19 | 62.5% (10/16), 95% CI [39%, 82%] | 100.0% (10/10), 95% CI [72%, 100%] |

The Nemotron hit set is a strict superset of the DeepSeek hit set: every post
DeepSeek accepted, Nemotron also accepted, plus 8 more, with nothing reversed.
Those 8 split as 1 true positive (`1woxhua`, the entire recall change), 6
confirmed false positives (the entire precision change), and 1 borderline.

### Where it ran, stated exactly

Nemotron was served by OpenRouter; the gateway reported `provider: "Nvidia"`
and `cost: 0`. It did **not** run on Nebius Token Factory — account creation
is unavailable in this region (Nebius support: "adding new countries takes
significant amount of time"). A judge checking "runs on Nebius Token Factory or
AI Cloud" should count this submission as **not meeting** that clause.

### Known caveats, stated honestly

- 5 of the 42 Nemotron calls returned HTTP 503 on the first attempt ("Upstream
  error from Nvidia: Service temporarily overloaded") and were retried. The
  recording contains all 42 verdicts with 0 unresolved, but the first pass was
  not clean.
- The free Nemotron endpoint reports ~2,086 prompt tokens per judgement, while
  DeepSeek reports ~406 for the same prompt. We cannot explain the 5× gap and
  publish both rather than quoting the cheaper one.
- The default `min_score` remains 5 (the project-wide "worth a human look"
  threshold). The README's published table uses `--min-score 3`, which is
  `rule_v4`'s documented candidate bar; the README explains this explicitly and
  the command is reproduced verbatim.

_Each subsequent week will be appended below._

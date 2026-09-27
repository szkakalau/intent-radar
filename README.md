# IntentRadar

Find Reddit posts that carry **buying intent**, and publish the accuracy so anyone can check it.

Most "AI lead finder" tools are black boxes: they hand you 40 posts and you cannot tell
why any of them was chosen. IntentRadar does the opposite — every lead ships with
machine-readable evidence, the hit rate is published, and a stranger can recompute
our number on their own machine with one command.

> Status: **W2 — semantic layer.** Two pipelines are scored against the same
> frozen ground truth: `rule_v3` (regex, frozen baseline) and `rule_v4` →
> `rule_v4+llm` (a deliberately wide recall net plus an LLM gate).
>
> **Measured on einprag** — `label_version einprag-2026-09-27d`,
> `labels.csv sha256=2009ef74b2d1`, 10 positives, model
> **`deepseek-v4-flash` (NOT Nemotron)**:
>
> | layer | precision | recall |
> |---|---|---|
> | `rule_v4+llm` (`v4.5.0+llm`) | **100.0% (9/9)** · 95% CI [70%, 100%] | **90.0% (9/10)** · 95% CI [60%, 98%] |
> | `rule_v3` (shipping default) | 20.0% (1/5) · 95% CI [4%, 62%] | 10.0% (1/10) · 95% CI [2%, 40%] |
>
> **A rate is never printed alone.** Every percentage above carries its raw
> counts and its 95% Wilson interval on the *same line* — because at n=10 a
> bare "100%" reads as "no false positives" when the lower bound is 70%. A
> number without an interval is not a measurement, and a lone percentage is
> exactly what we criticise other tools for publishing. This is a **format rule
> across the whole repository**, not a warning you can scroll past.
>
> ⚠️ **Not Nemotron.** `deepseek-v4-flash` over a local dev proxy (see
> [Which model](#which-model-produced-the-stage-2-numbers)). Re-measuring on
> Nebius/Nemotron is pending; nothing here may be quoted as a Nemotron result.
>
> ⚠️ **einprag only.** The einprag denominator is fully reverse-audited; the
> bootstrap one is not yet (57 rows owing). Every rate on this page is an
> einprag measurement.

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
uv run python -m intentradar config check
uv run python -m intentradar run --project Einprag
```

> **Why the module form (`python -m intentradar`) and not the console script.**
> On locked-down Windows environments `uv run intentradar` fails to spawn with
> `os error 4551` (application-control policy) — we reproduced it on a clean
> clone, and a red error on the very first command is a bad first impression of
> a project whose entire claim is that a stranger can recompute its numbers.
> The module form needs no installed launcher and behaves identically.
> If your environment allows console scripts, `uv run intentradar …` also works.

No API key? No problem for the evaluation path — it is fully offline:

```bash
uv run python -m intentradar eval run --testset einprag-2026-09-27
```

### Commands

| Command | What it does |
|---|---|
| `intentradar run` | Collect → judge → dedupe → write the daily report |
| `intentradar config check` | Validate `.env` + `config/watchlist.json` before anything runs; prints `backend=… · model=…` |
| `intentradar llm hello` | Smoke-test the configured LLM client; prints the backend + model it will actually use (works in mock mode without a key) |
| `intentradar eval run` | Recompute the frozen hit set, offline |
| `intentradar eval score` | Scores any `--layers` list against `labels.csv`; every rate printed with its counts and a Wilson interval |
| `intentradar eval density` | Per-subreddit ground-truth density — **read this when positives < 10** |
| `intentradar eval export` | Export hits to CSV / JSONL for third-party review |
| `intentradar eval snapshot` | Freeze a new testset from a live collection run |
| `intentradar budget` | Show / reset monthly usage |

`eval score` reads the threshold from the dataset's own `meta.json` — it is not
a flag you should be tuning. `--layers` is the one option that matters:
`eval score --layers rule_v4,rule_v4+llm` compares the v4 funnel instead of the
shipping default.

Two more, and they are the reason the LLM column is reproducible at all:

```bash
# freeze the model responses (needs an endpoint — done once, by us)
uv run python -m intentradar eval score --testset einprag-2026-09-27 \
    --layers rule_v4,rule_v4+llm --min-score 3 --record data/replay/einprag-2026-09-27

# re-derive the same table from them — no network, no key, no endpoint
uv run python -m intentradar eval score --testset einprag-2026-09-27 \
    --layers rule_v4,rule_v4+llm --min-score 3 \
    --replay data/replay/einprag-2026-09-27/rule_v4_llm
```

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
hit rate                2.8% (6/211)
─
noise rate = (# hits a human marks 'not actionable') / (# hits)
reviewed hits           5/6   (1 undecided: borderline/blank)
noise rate              80.0% (4/5)
ground truth: 184 labeled (10 actionable / 174 not) · 0 unlabeled · 27 borderline
→ full precision / recall: `eval score --testset einprag-2026-09-27`
expected 6 hits: MATCH
```

`eval run` is the reproducibility check: it reports the hit set **and** how much
of it a human has actually adjudicated. 5 of the 6 hits carry a verdict and 4 of
those are `not_actionable` — noise 80.0% (4/5), 95% CI [38%, 96%], which is
`1 - precision` — 20.0% (1/5), 95% CI [4%, 62%]. The 6th hit sits on a
`borderline` row and is counted as undecided rather than silently folded into
either side.

> **What `min_score` means.** The threshold (`5` for this snapshot) is locked in
> `meta.json`, not passed on the command line — run `eval score` and it reads the
> dataset's own value. A score of 5 means *"worth a human look"*, **not** *"this
> person is a buyer"*: at this threshold `rule_v3` finds 10% of the buyers and
> 4 of its 6 hits are rejected on review. Treating the threshold as a confidence
> score is the easiest way to misread everything below.

**Baseline X = the rule layer.** Snapshot `einprag-2026-09-27` contains every post
in a 3-day window across 5 subreddits; `rule_v3` flags 6 of them. Run the command
above on a fresh clone and you get the same 6 ids, the same scores, the same reasons.

The second snapshot, `bootstrap-2026-09-27` (our own dog-fooding project), is
137 posts → 12 hits → 8.8% (12/137).

> **Bootstrap publishes no accuracy figure, and the 8.8% (12/137) above is a hit
> rate, not accuracy.** Its ground truth contains **2 positives**, so one
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
confidence interval.** This is deliberate: einprag has **10 positive examples**,
so one relabelled row moves recall by ten points. `rule_v3`'s precision is
`20.0% (1/5)` with a 95% CI of `[4%, 62%]` — which is the honest way of saying
"on this sample you have learned almost nothing". We publish the error bar
rather than the bare percentage because a number without one is not a
measurement.

**Stage 1 — the shipping default, `rule_v3`:**

```
$ intentradar eval score --testset einprag-2026-09-27 \
      --replay data/replay/einprag-2026-09-27/rule_v3_llm

REPLAYED — frozen model responses from 2026-09-27T08:19:10Z, model=deepseek-v4-flash. Not a live run.
testset : einprag-2026-09-27   (frozen 2026-09-27)
truth   : einprag-2026-09-27d  labels.csv sha256=2009ef74b2d1
project : Einprag   min_score=5
labels  : 211 rows · 184 labeled (10 actionable / 174 not) · 0 unlabeled · 27 borderline
──────────────────────────────────────────────────────────────
metric                      rule_v3 (v3.0.0)  rule_v3+llm (v3.1.0+llm)
predicted                                  6                         1
true positive                              1                         1
false positive                             4                         0
false negative                             9                         9
precision                       20.0%  (1/5)             100.0%  (1/1)
precision 95% CI                   [4%, 62%]               [21%, 100%]
recall                         10.0%  (1/10)             10.0%  (1/10)
recall 95% CI                      [2%, 40%]                 [2%, 40%]
F1                                     13.3%                     18.2%
noise rate                      80.0%  (4/5)               0.0%  (0/1)
unverified hits                            1                         0
positives (n)                             10                        10
──────────────────────────────────────────────────────────────
llm: 14 calls · 0 errors · backend=REPLAYED from data/replay/einprag-2026-09-27/rule_v3_llm · model=deepseek-v4-flash
note: 27 of 211 rows carry no usable verdict (0 unlabeled + 27 borderline); they are excluded from every ratio above, not counted as 0.
```

`rule_v3` is a regex heuristic and it behaves like one: of the 10 posts a
reviewer marked actionable it finds 1, and 4 of its 6 hits are marked
`not_actionable` — precision 20.0% (1/5), 95% CI [4%, 62%].

The semantic layer can only *remove* hits, so `rule_v3+llm` keeps
`rule_v3`'s recall — 10.0% (1/10), 95% CI [2%, 40%], identical to the rule
layer's — no matter how good the model is. It removed 5 of the 6 hits and
every one it removed was noise, so the single survivor is a true positive:/nprecision 100.0% (1/1), 95% CI [21%, 100%]. **That interval is the whole
story** — one survivor is not evidence of a good gate, and we do not
present it as one. A better model cannot fix a net that never caught the
buyer, which is the entire reason `rule_v4` exists.

> **This table is a frozen replay of one real run** — `deepseek-v4-flash`,
> 2026-09-27, on the frozen `einprag-2026-09-27` snapshot. It is
> **offline-reproducible to the digit** (see the command above) and it is
> **not a Nemotron result**. The 14 model responses behind it are committed
> under `data/replay/einprag-2026-09-27/rule_v3_llm/`.
>
> Without `--replay` and without a key, the `rule_v3+llm` column reads
> **0/10, not a number**: the deterministic mock answers `false` to
> everything and deletes all six hits, and the report says so in three
> separate lines rather than printing a plausible-looking percentage.
> That is the tool refusing to invent a number — which is why this page
> publishes the replay instead of the mock column.

**Stage 2 — the opt-in v4 pipeline, `rule_v4` → `rule_v4+llm`:**

The table below is the **only** copy of this result we publish. It is a frozen
replay of one real run — `deepseek-v4-flash`, 2026-09-27, on the frozen
`einprag-2026-09-27` snapshot — and it is **offline-reproducible to the digit**
with the command shown, no key required. **It is not a Nemotron result.** The 42
model responses behind it are committed under
`data/replay/einprag-2026-09-27/rule_v4_llm/`. We publish one copy rather than
also showing the live run's own output: two copies of the same table means one
of them can drift, and that is exactly the failure this page has already made
once.

```
$ intentradar eval score --testset einprag-2026-09-27 \
      --layers rule_v4,rule_v4+llm --min-score 3 \
      --replay data/replay/einprag-2026-09-27/rule_v4_llm

REPLAYED — frozen model responses from 2026-09-27T07:28:41Z, model=deepseek-v4-flash. Not a live run.
testset : einprag-2026-09-27   (frozen 2026-09-27)
truth   : einprag-2026-09-27d  labels.csv sha256=2009ef74b2d1
project : Einprag   min_score=3
labels  : 211 rows · 184 labeled (10 actionable / 174 not) · 0 unlabeled · 27 borderline
──────────────────────────────────────────────────────────────
metric                      rule_v4 (v4.0.0)  rule_v4+llm (v4.5.0+llm)
predicted                                 42                        11
true positive                             10                         9
false positive                            28                         0
false negative                             0                         1
precision                     26.3%  (10/38)             100.0%  (9/9)
precision 95% CI                  [15%, 42%]               [70%, 100%]
recall                       100.0%  (10/10)             90.0%  (9/10)
recall 95% CI                    [72%, 100%]                [60%, 98%]
F1                                     41.7%                     94.7%
noise rate                    73.7%  (28/38)               0.0%  (0/9)
unverified hits                            4                         2
positives (n)                             10                        10
──────────────────────────────────────────────────────────────
llm: 0 calls · 0 errors · backend=n/a
llm: 42 calls · 0 errors · backend=REPLAYED from data/replay/einprag-2026-09-27/rule_v4_llm · model=deepseek-v4-flash
note: 27 of 211 rows carry no usable verdict (0 unlabeled + 27 borderline); they are excluded from every ratio above, not counted as 0.
```

Two rows exist only because this is a replay, and both are the point: the
`REPLAYED —` banner on top, and `backend=REPLAYED … · model=deepseek-v4-flash`
below. **A screenshot of this table carries its own provenance.** A test
(`tests/test_replay.py`) replays the committed recordings and compares every row
of both published tables line by line, so a published number cannot drift from
the reproducible one without the suite going red.

Each frozen response carries its post id, judge version, model, endpoint,
timestamp, the sha256 of the exact prompt, and the raw response text — see
`data/replay/einprag-2026-09-27/rule_v4_llm/manifest.json`. Dropping any of
those would leave a cache that cannot be audited. A replay never fills a gap
from anywhere: if the prompt or the judge version changes, the replay **fails**
naming what changed, rather than answering with a response to a different
question.

This is the two-stage funnel, and it is the core of the design:

| stage | candidates | precision | recall |
|---|---|---|---|
| `rule_v4` — wide net | 42 | 26.3% (10/38) · 95% CI [15%, 42%] | **100% (10/10)** · 95% CI [72%, 100%] |
| `rule_v4+llm` — semantic gate | 11 | **100.0% (9/9)** · 95% CI [70%, 100%] | 90.0% (9/10) · 95% CI [60%, 98%] |

The rule layer is a **net, not a decider**: it is deliberately over-inclusive
and costs nothing, so it can afford recall 100% (10/10), 95% CI [72%, 100%],
at precision 26.3% (10/38), 95% CI [15%, 42%]. The LLM gate removes 31 of the
42 candidates, and **zero** of the 9 survivors a human has ruled on are marked
`not_actionable` — 0/9.
A single-stage scorer has to trade precision against recall; splitting the job
in two means only the cheap stage has to be greedy.

> **Why `--min-score 3` appears here and nowhere else.** It is not a tuning
> knob: 3 is `rule_v4`'s documented candidate bar (`CANDIDATE_MIN_SCORE`), the
> point at which a post has enough rule evidence to be worth spending a model
> call on. The v4 numbers are only meaningful at that bar, so it is stated in
> the command rather than left implicit. For the default `rule_v3` path the
> threshold comes from `meta.json` and no flag is needed.

**One miss, named.** The single false negative is `1woxhua`, which the model
reads as *"musing whether it would be possible to design a curriculum"* rather
than a request for something to acquire. The reviewer disagrees — the post
names courses, textbooks and software — so this is logged as a model error, not
tuned away: rewriting the prompt to catch one post is fitting the rule to the
example, and it would not generalise. See
[Failure cases](#failure-cases-are-published).

### Which model produced the stage-2 numbers

They come from **`deepseek-v4-flash`, reached through a local Anthropic-shaped
dev proxy** (`http://127.0.0.1:8787/v1/messages`). That is a development
verification channel, **not** the hackathon's target model, and nothing here may
be quoted as a Nebius/Nemotron result. Every run prints `backend=… · model=…`
and `config check` prints the resolved endpoint, so a figure cannot be
mis-attributed by accident. Re-measuring on Nebius is pending. (A re-run served
from the response cache reports `0 calls`; the run above made 42.)

### Version discipline

The stage-2 numbers above are **`v4.5.0+llm`**, and the version is part of the
number — the same version string never means two different criteria.

| version | status |
|---|---|
| `v4.2.0+llm` | **recalled** — required a specific product name, which the written standard forbids. Its numbers are published only as the cost of that bug. |
| `v4.3.0+llm` | superseded |
| `v4.4.0+llm` | superseded — synced the standard, but its rule (b) misclassified `1wpsql7` (see [Failure cases](#failure-cases-are-published)) |
| **`v4.5.0+llm`** | current — rule (b)'s test sentence replaced |

### When the sample is too small: `eval density`

Ten positives is still a small sample, and the right response is to change the
unit of analysis rather than polish the ratio. `eval density` breaks the ground
truth down by subreddit, so you can see **where** the positives live and which
sources produced almost nothing:

```
$ intentradar eval density --testset einprag-2026-09-27

testset : einprag-2026-09-27   project: Einprag
truth   : einprag-2026-09-27d  labels.csv sha256=2009ef74b2d1
──────────────────────────────────────────────────────────────
sub                 posts  actionable  borderline  unreviewed   density
languagelearning       23           4           2           0     17.4%
Anki                   43           3           4           0      7.0%
medicalschool          47           1           4           0      2.1%
premed                 48           1           2           0      2.1%
GetStudying            50           1          15           0      2.0%
──────────────────────────────────────────────────────────────
TOTAL                 211          10          27           0      4.7%
```

7 of the 10 positives come from two subreddits; `GetStudying` supplied 50 posts
for **one** positive and left 15 rows the reviewer could not decide. Density is
the more durable statement — it survives a relabelling, where a precision
percentage does not. `eval score` refuses to print a bare ratio once positives
drop below 10 and points here instead.

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
   `10/38`: four of the 42 sit on `borderline`/blank rows and cannot be judged
   either way. Quoting 10/42 (23.8%) instead of 10/38 (26.3%) would be a
   different, silently pessimistic number.
2. **A blank or `borderline` row is never turned into `not_actionable`.** Doing
   so would manufacture false positives and *inflate* precision — the one
   direction of error that looks good in a README.

Wilson, not the normal approximation: on samples this small the textbook
interval returns bounds outside `[0, 1]`, which is how a tool ends up claiming
an interval wider than the range the quantity can possibly take.

**The format rule (applies to every document in this repo, not just this
page): a rate is never printed alone.** Any precision / recall / F1 / noise
figure must carry its raw counts `(x/y)` and its 95% CI **on the same line**:

```
(illustration — not tool output)
precision 100.0% (9/9), 95% CI [70%, 100%]     ✅
precision 100.0%                                ❌ — reads as "no false
                                                     positives"; the lower
                                                     bound is 70%
```

This is a format rule rather than a warning because warnings get scrolled past
and formatting does not. A test greps the README for rate mentions and fails
when one appears without both its counts and its interval, so the discipline
cannot decay quietly.

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
verbatim into the judge's prompt. Two tests hold that line: one asserts the
prompt still implements the standard's rules, and another asserts the ids the
standard cites as settled precedents still carry those labels in `labels.csv`.
A precedent that contradicts its own ground truth is worse than no precedent,
because the prompt is synced from that text.

**Audit coverage, stated plainly.** The einprag denominator (211 rows) has been
through a full reverse audit. The **bootstrap denominator has not** — a
57-row reverse audit there is still owing, blocked on reviewer quota. So every
precision/recall figure on this page is an **einprag-only** measurement, and
bootstrap contributes a density table rather than a rate. "We audited one side"
is worth more than "we audited everything", and the second is not true yet.

### Failure cases are published

A hit rate alone is meaningless without the misses, so every false negative and
false positive gets audited individually and written up in
[`SIGNIFICANT_UPDATES.md`](SIGNIFICANT_UPDATES.md) — including the ones that
were our fault. Three from this project, all recorded there:

- **A trade-off that was actually a bug.** Judge version `v4.2.0` cut recall
  from 8/8 to 5/8 because the prompt required a **specific product name** while
  the written standard only required the **category** to be explicit. We
  reported it as a precision/recall trade-off. It was a bug — and identifying a
  bug as a trade-off is the more dangerous error, because a trade-off gets
  accepted and a bug gets fixed. Root cause: the prompt was written from a
  paraphrase in a chat message instead of from `label_judgement_standard`.
  `v4.2.0` is retired; its numbers are published only as the cost of the bug.
- **A rule that contradicted its own ground truth.** `v4.4.0` synced the
  standard faithfully, including rule (b) — *"asking others about their
  experience is research stage"*. Applied literally, it rejected `1wpsql7`:
  someone weighing a **paid subscription** who asks existing users "is it worth
  it?". The standard's own precedent list calls that post a confirmed
  actionable. Rather than accept it as a precision/recall trade, rule (b)'s test
  sentence was replaced outright — from *"is the author talking to people"* to
  *"is the author making an adoption decision for themselves"*. That moved
  `v4.4.0` → `v4.5.0` from precision 88.9% (8/9), 95% CI [57%, 98%] and recall
  80.0% (8/10), 95% CI [49%, 94%] to **precision 100.0% (9/9), 95% CI [70%,
  100%]** and **recall 90.0% (9/10), 95% CI [60%, 98%]** — and removed the last
  false positive.
- **A figure corrected after it was written.** A pending relabelling was first
  reported as taking precision to 8 scored hits with 8 true positives, on the
  assumption two rows would move. The team lead overruled one of them
  (`1wq5s03` stays `not_actionable`), so the projection was 8/10, not 8/9.
  Both the correction and the overrule are recorded; the number was not quietly
  edited.

**The one remaining miss is `1woxhua`**, and we are leaving it in. The model
reads it as musing about curriculum design; the reviewer says it names courses,
textbooks and software. Rewriting the prompt until it catches that post would be
fitting the rule to one example — it would pass this testset and generalise
worse. So it stays a false negative, named, in the ledger.

If the semantic layer had failed to beat the baseline, that would be published
too — the point of the tool is that the number can be recomputed, so a
favourable number nobody can check is worth less than an unfavourable one anyone
can.

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

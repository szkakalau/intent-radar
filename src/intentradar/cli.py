"""Command-line interface (P0-7).

Exit codes (single mapping, applied once at the top level):

    0  success
    1  evaluation mismatch — recomputed numbers differ from the frozen ones
    2  ConfigError / MissingCredential
    3  BudgetExceeded / GateExceeded
    4  ProviderError
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from intentradar import __version__
from intentradar.budget import BudgetGuard, RunGates
from intentradar.config import Settings, Watchlist
from intentradar.errors import IntentRadarError
from intentradar.eval import EvalDataset, EvalRunner, EvalScorer
from intentradar.judge import AVAILABLE_LAYERS
from intentradar.llm import build_client
from intentradar.pipeline import Pipeline

log = logging.getLogger("intentradar")

EXIT_OK = 0
EXIT_EVAL_MISMATCH = 1
EXIT_CONFIG = 2
EXIT_GATE = 3
EXIT_PROVIDER = 4


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse tree (five command groups)."""
    parser = argparse.ArgumentParser(
        prog="intentradar",
        description="Find Reddit posts that carry buying intent — and publish the accuracy.",
    )
    parser.add_argument("--version", action="version", version=f"intentradar {__version__}")
    parser.add_argument("--verbose", action="store_true", help="DEBUG logging to stderr")
    sub = parser.add_subparsers(dest="command", required=True)

    # ── run ────────────────────────────────────────────────────────────────
    run = sub.add_parser("run", help="collect → judge → dedupe → daily report")
    run.add_argument("--project", action="append", dest="projects", help="project name (repeatable)")
    run.add_argument("--min", type=int, default=0, help="override min_score (0 = use config)")
    run.add_argument("--dry", action="store_true", help="do not write state")
    run.add_argument("--reset", action="store_true", help="forget already-reported posts")
    run.add_argument("--no-cache", action="store_true", help="bypass the provider cache")
    run.add_argument("--no-report", action="store_true", help="skip writing the report files")

    # ── config ─────────────────────────────────────────────────────────────
    config = sub.add_parser("config", help="configuration checks")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("check", help="validate .env + watchlist before running")

    # ── llm ────────────────────────────────────────────────────────────────
    llm = sub.add_parser("llm", help="Nemotron client smoke tests")
    llm_sub = llm.add_subparsers(dest="llm_command", required=True)
    hello = llm_sub.add_parser("hello", help="one real (or mocked) completion")
    hello.add_argument("--tier", choices=["everyday", "reasoning"], default="everyday")
    hello.add_argument("--list-models", action="store_true", help="probe GET /models")

    # ── eval ───────────────────────────────────────────────────────────────
    evaluation = sub.add_parser("eval", help="offline accuracy evaluation")
    eval_sub = evaluation.add_subparsers(dest="eval_command", required=True)

    eval_run = eval_sub.add_parser("run", help="recompute the hit set of a frozen testset")
    eval_run.add_argument("--testset", required=True, help="e.g. einprag-2026-09-27")

    eval_export = eval_sub.add_parser("export", help="export hits for third-party review")
    eval_export.add_argument("--testset", required=True)
    eval_export.add_argument("--format", choices=["csv", "jsonl"], default="csv")
    eval_export.add_argument("--out", default="", help="output path (default ./<testset>.<fmt>)")

    eval_score = eval_sub.add_parser(
        "score", help="precision / recall / F1 for rule_v3 vs rule_v3+llm (needs labels.csv)"
    )
    eval_score.add_argument("--testset", required=True)
    eval_score.add_argument("--min-score", type=int, default=0, help="0 = use the dataset's min_score")
    eval_score.add_argument(
        "--layers",
        default="",
        help="comma-separated layers to compare (default: rule_v3,rule_v3+llm)",
    )

    eval_snap = eval_sub.add_parser("snapshot", help="freeze a new testset from a live run")
    eval_snap.add_argument("--project", required=True)
    eval_snap.add_argument("--testset", default="", help="override the generated testset id")

    # ── budget ─────────────────────────────────────────────────────────────
    budget = sub.add_parser("budget", help="show or reset monthly usage")
    budget.add_argument("--reset", action="store_true", help="zero the counters")

    return parser


def _init_logging(verbose: bool) -> None:
    """stdout stays clean; WARNING+ always goes to stderr."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _mask(value: str) -> str:
    """Presence indicator only — never log a key, not even masked."""
    return "set" if value else "MISSING"


# ── command implementations ────────────────────────────────────────────────


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    """`intentradar run`."""
    from intentradar.collect.scrapecreators import ScrapeCreatorsProvider

    watchlist = Watchlist.load(settings.watchlist_path)
    settings.require_scrape_key()
    provider = ScrapeCreatorsProvider(api_key=settings.scrape_key)
    pipeline = Pipeline(settings=settings, watchlist=watchlist, provider=provider)

    results = pipeline.run(
        project_names=args.projects,
        min_score_override=args.min or None,
        cache=None if args.no_cache else watchlist.global_config.cache_max_age,
        dry=args.dry,
        reset_state=args.reset,
        write_report=not args.no_report,
    )
    charged = sum(r.credits_charged for r in results)
    remaining = next((r.credits_remaining for r in results if r.credits_remaining is not None), None)
    tail = f" · balance {remaining}" if remaining is not None else ""
    print(
        f"credits charged: {charged}{tail} · {pipeline.gates.summary()} · {pipeline.budget.summary()}"
    )
    return EXIT_OK


def cmd_config_check(args: argparse.Namespace, settings: Settings) -> int:
    """`intentradar config check` — fail fast, in plain language."""
    watchlist = Watchlist.load(settings.watchlist_path)
    print("configuration")
    print(f"  repo root        {settings.repo_root}")
    print(f"  data dir         {settings.data_dir}")
    print(f"  watchlist        {settings.watchlist_path} (schema v{watchlist.version})")
    llm = settings.describe_llm()
    print(f"  SCRAPECREATORS   {_mask(settings.scrape_key)}")
    print(f"  LLM api key      {_mask(settings.llm_api_key)}  (from {llm['api_key_from']})")
    print(f"  LLM base url     {llm['base_url']}  (from {llm['base_url_from']})")
    print(
        f"  model everyday   {llm['model'] or '(empty — required once you have a key)'}"
        f"  (from {llm['model_from']})"
    )
    print(f"  model reasoning  {settings.model_reasoning or '(empty — optional)'}")
    print(f"  llm mode         {llm['mode']}")
    print(f"  judge layers     {', '.join(AVAILABLE_LAYERS)}")
    print(
        f"  gates            max_posts_per_source={settings.max_posts_per_source}/sub "
        f"max_llm_calls={settings.max_llm_calls} "
        f"monthly_budget=${settings.monthly_budget_usd:.2f}"
    )
    print("projects")
    for project in watchlist.projects:
        flag = "enabled" if project.enabled else "disabled"
        print(
            f"  - {project.name} [{flag}] subs={len(project.subreddits)} "
            f"keywords={len(project.keywords)} competitors={len(project.competitors)}"
        )
    print("OK: configuration is valid")
    return EXIT_OK


def cmd_llm_hello(args: argparse.Namespace, settings: Settings) -> int:
    """`intentradar llm hello` — works with and without a key."""
    budget = BudgetGuard(path=settings.usage_path, monthly_budget_usd=settings.monthly_budget_usd)
    gates = RunGates(max_llm_calls=settings.max_llm_calls, max_posts_per_source=settings.max_posts_per_source)

    client = build_client(settings, budget=budget, gates=gates)

    if args.list_models:
        models = client.list_models()
        print(f"{len(models)} models available at {settings.llm_endpoint}")
        for model in models[:50]:
            print(f"  {model}")
        return EXIT_OK

    if client.is_mock:
        print("MOCK MODE — no LLM API key found, nothing was billed.")
        print("  → copy .env.example to .env and set INTENTRADAR_LLM_API_KEY")
        print("    (or NEBIUS_API_KEY) plus INTENTRADAR_LLM_BASE_URL if you are not")
        print("    using Nebius Token Factory")
        print("  → then set INTENTRADAR_LLM_MODEL (or NEBIUS_MODEL_EVERYDAY) to the")
        print("    exact slug from your provider's model list (do not guess the casing)")
    else:
        # A key without a slug is a configuration error, not a runtime surprise.
        settings.require_model(args.tier)

    response = client.hello(tier=args.tier)
    print(f"{'model':<20}{response.model}")
    print(f"{'tier':<20}{args.tier}")
    print(f"{'prompt tokens':<20}{response.prompt_tokens}")
    print(f"{'completion tokens':<20}{response.completion_tokens}")
    print(f"{'cost (est.)':<20}${response.cost_usd:.6f}")
    print(f"{'cached':<20}{response.cached}")
    print(f"{'mock':<20}{response.mock}")
    print("--- content ---")
    print(response.content)
    print(f"--- budget: {budget.summary()}")
    return EXIT_OK


def cmd_eval_run(args: argparse.Namespace, settings: Settings) -> int:
    """`intentradar eval run` — offline recomputation of the published number."""
    dataset = EvalDataset.load(args.testset, settings.testset_dir)
    runner = EvalRunner(watchlist_path=settings.watchlist_path)
    report = runner.run(dataset)
    print(report.render())
    if not report.expected_match:
        print(
            "ERROR: recomputed hits differ from expected_hits.json — "
            "the published number is no longer reproducible",
            file=sys.stderr,
        )
        return EXIT_EVAL_MISMATCH
    return EXIT_OK


def cmd_eval_export(args: argparse.Namespace, settings: Settings) -> int:
    """`intentradar eval export` — CSV / JSONL for third-party review."""
    dataset = EvalDataset.load(args.testset, settings.testset_dir)
    runner = EvalRunner(watchlist_path=settings.watchlist_path)
    report = runner.run(dataset)
    out = Path(args.out) if args.out else Path(f"{args.testset}.{args.format}")
    path = runner.export(report, args.format, out)
    print(f"exported {report.hit_count} hits → {path}")
    return EXIT_OK


def cmd_eval_score(args: argparse.Namespace, settings: Settings) -> int:
    """`intentradar eval score` — the published accuracy, both layers, no hiding."""
    from intentradar.judge import get_judge
    from intentradar.llm import build_client

    dataset = EvalDataset.load(args.testset, settings.testset_dir)
    watchlist = Watchlist.load(settings.watchlist_path)
    project = watchlist.get(str(dataset.meta.get("project") or ""))

    requested = [s.strip() for s in args.layers.split(",") if s.strip()]
    layers = requested or ["rule_v3", "rule_v3+llm"]

    judges: list[Any] = []
    for layer in layers:
        if layer == "rule_v3+llm":
            # Budget + gates are attached so a scoring run cannot silently spend
            # more than the operator allowed.
            client = build_client(
                settings,
                budget=BudgetGuard(
                    path=settings.usage_path, monthly_budget_usd=settings.monthly_budget_usd
                ),
                gates=RunGates(
                    max_llm_calls=settings.max_llm_calls,
                    max_posts_per_source=settings.max_posts_per_source,
                ),
            )
            judges.append(get_judge(layer, client=client))
        else:
            judges.append(get_judge(layer))

    report = EvalScorer(judges).score(dataset, project=project, min_score=args.min_score or None)
    print(report.render())
    return EXIT_OK


def cmd_eval_snapshot(args: argparse.Namespace, settings: Settings) -> int:
    """`intentradar eval snapshot` — freeze a new dataset from a live collection."""
    from intentradar.collect.scrapecreators import ScrapeCreatorsProvider
    from intentradar.judge import get_judge

    watchlist = Watchlist.load(settings.watchlist_path)
    project = watchlist.get(args.project)
    settings.require_scrape_key()
    provider = ScrapeCreatorsProvider(api_key=settings.scrape_key)
    judge = get_judge("rule_v3")

    dataset = EvalDataset.snapshot(
        project=project,
        provider=provider,
        judge=judge,
        root=settings.testset_dir,
        window_days=watchlist.global_config.window_days,
        min_score=watchlist.min_score_for(project),
        pages_per_sub=watchlist.global_config.pages_per_sub,
        testset_id=args.testset or None,
    )
    print(
        f"froze {dataset.meta['total_in_window']} posts, "
        f"{len(dataset.expected_hits)} hits, "
        f"{dataset.meta['hit_rate_pct']}% → {settings.testset_dir / dataset.testset_id}"
    )
    print("commit data/testset/ now so the number stays reproducible")
    return EXIT_OK


def cmd_budget(args: argparse.Namespace, settings: Settings) -> int:
    """`intentradar budget [--reset]`."""
    budget = BudgetGuard(path=settings.usage_path, monthly_budget_usd=settings.monthly_budget_usd)
    if args.reset:
        budget.reset()
        print("usage reset")
    print(f"{'month':<16}{budget.usage.month}")
    print(f"{'llm calls':<16}{budget.usage.llm_calls}")
    print(
        f"{'tokens':<16}{budget.usage.prompt_tokens} in / "
        f"{budget.usage.completion_tokens} out"
    )
    print(f"{'scrape credits':<16}{budget.usage.scrape_credits}")
    print(f"{'cost':<16}${budget.usage.cost_usd:.4f} / ${budget.monthly_budget_usd:.2f}")
    print(f"{'usage file':<16}{budget.path}")
    return EXIT_OK


# ── dispatcher ─────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Maps every known error to a stable exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    _init_logging(args.verbose)

    try:
        settings = Settings.from_env()
        if args.command == "run":
            return cmd_run(args, settings)
        if args.command == "config":
            return cmd_config_check(args, settings)
        if args.command == "llm":
            return cmd_llm_hello(args, settings)
        if args.command == "eval":
            if args.eval_command == "run":
                return cmd_eval_run(args, settings)
            if args.eval_command == "export":
                return cmd_eval_export(args, settings)
            if args.eval_command == "score":
                return cmd_eval_score(args, settings)
            if args.eval_command == "snapshot":
                return cmd_eval_snapshot(args, settings)
        if args.command == "budget":
            return cmd_budget(args, settings)
        parser.error(f"unknown command {args.command}")
    except IntentRadarError as exc:
        print(f"{type(exc).__name__}: {exc.message}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

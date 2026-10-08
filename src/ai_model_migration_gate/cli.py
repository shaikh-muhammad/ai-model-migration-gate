"""Confirmed execution, read-only fingerprinting, and offline single-run reports."""

import argparse
from pathlib import Path

import httpx
from yaml import YAMLError

from .cases import load_cases
from .config import load_config
from .fingerprint import calculate_fingerprint
from .gate import GateDecision, check_run, invalid_decision
from .runner import RecordedAttempt, execute_plan, plan_run, preflight_case
from .scoring import ScoredRun, score_run


def positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gate", description="AI Model Migration Gate command skeleton."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    descriptions = {
        "run": "Plan a verifier run; execute only with --yes and an explicit --run-id.",
        "check": "Check absolute rules offline; without three current baselines migration comparison is still required.",
        "report": "Score one complete saved run offline without network access or file writes.",
        "fingerprint": "Calculate a deterministic fingerprint without network access or file writes.",
    }
    for name, description in descriptions.items():
        command_parser = commands.add_parser(name, help=description, description=description)
        if name == "run":
            command_parser.add_argument("--target", choices=("current", "candidate"), required=True)
            command_parser.add_argument("--config", type=Path, default=Path("gate.yaml"))
            command_parser.add_argument("--cases", type=Path, default=Path("cases/cases.jsonl"))
            command_parser.add_argument(
                "--yes", action="store_true", help="Execute selected cases; requires --run-id."
            )
            command_parser.add_argument("--max-calls", type=positive_integer, required=True)
            command_parser.add_argument("--case", dest="case_id", help="Select one known case ID.")
            command_parser.add_argument("--force", action="store_true", help="Reselect successful cases in this run.")
            command_parser.add_argument("--run-id", help="Explicit run ID for resume; required with --yes.")
        elif name in ("fingerprint", "report", "check"):
            command_parser.add_argument("--target", choices=("current", "candidate"), required=True)
            command_parser.add_argument("--config", type=Path, default=Path("gate.yaml"))
            command_parser.add_argument("--cases", type=Path, default=Path("cases/cases.jsonl"))
            command_parser.add_argument(
                "--json", action="store_true",
                help={"fingerprint": "Print only the identity JSON.", "report": "Print only the scored run JSON.",
                      "check": "Print only the gate decision JSON."}[name],
            )
            if name in ("report", "check"):
                command_parser.add_argument("--run-id", required=True, help="Explicit saved run ID.")
            if name == "check":
                command_parser.add_argument(
                    "--baseline-run-id", action="append", dest="baseline_run_ids",
                    help="Repeat exactly three times with distinct current run IDs for a final candidate check.",
                )
    return parser


def print_attempt(attempt: RecordedAttempt) -> None:
    result = attempt.result
    http_status = result.http_status if result.http_status is not None else "n/a"
    canonical = (
        f"; canonical_result_file={attempt.canonical_path}"
        if attempt.canonical_path is not None else ""
    )
    print(
        f"{result.case_id}: {result.status.value}; HTTP={http_status}; "
        f"response_time_ms={result.response_time_ms:.1f}; "
        f"attempt_number={attempt.attempt_number}; attempt_file={attempt.attempt_path}{canonical}"
    )


def print_report(report: ScoredRun) -> None:
    metrics = report.metrics
    print(f"Run report: target={report.target}; run_id={report.run_id}")
    print(
        f"Cases: total={metrics.total_cases}; correct={metrics.correct_cases}; "
        f"minor={metrics.minor_cases}; false_blocks={metrics.false_block_cases}; "
        f"dangerous={metrics.dangerous_cases}; critical_dangerous={metrics.critical_dangerous_cases}"
    )
    print(f"Accuracy: {metrics.accuracy:.2%}")
    print(
        f"Client response time (ms): median={metrics.median_response_time_ms:.1f}; "
        f"p95 (nearest-rank)={metrics.p95_response_time_ms:.1f}"
    )
    for case in report.cases:
        print(
            f"{case.case_id}: expected={case.expected_outcome.value}; actual={case.actual_outcome.value}; "
            f"classification={case.classification.value}; critical={'yes' if case.critical else 'no'}; "
            f"response_time_ms={case.response_time_ms:.1f}"
        )


def print_decision(result: GateDecision) -> None:
    print(f"{result.decision.value}: target={result.target}; run_id={result.run_id}")
    for error in result.errors:
        print(f"Cause: {error}")
    for rule in result.rules:
        actual = rule.actual if rule.actual is not None else "n/a"
        threshold = rule.threshold if rule.threshold is not None else "unset"
        print(f"{rule.name}: {rule.status.value}; actual={actual}; threshold={threshold}; {rule.message}")
    if result.metrics is not None:
        metrics = result.metrics
        print(
            f"Correct cases: {metrics.correct_cases}/{metrics.total_cases}; "
            f"critical dangerous cases: {metrics.critical_dangerous_cases}; "
            f"p95 client response time (ms): {metrics.p95_response_time_ms:.3f}"
        )
    if result.comparison is not None:
        metrics = result.comparison.metrics
        print("Current baseline runs: " + " / ".join(result.baseline_run_ids))
        print(
            f"Comparison: stable={metrics.stable_cases}; baseline_variable={metrics.baseline_variable_cases}; "
            f"got_better={metrics.got_better_cases}; unchanged={metrics.unchanged_cases}; "
            f"got_worse={metrics.got_worse_cases}; dangerous_regressions={metrics.dangerous_regressions}"
        )
    if result.comparison_required:
        print("Comparison required: dangerous regressions are not evaluated; this is an absolute single-run check.")
    else:
        print("Final comparison check requested: " +
              ("cannot be decided because inputs or policy are invalid." if result.errors else "all release rules evaluated."))


def main(argv: list[str] | None = None) -> int | None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "check":
        input_description = "selected configuration"
        try:
            config_path = args.config.resolve()
            config = load_config(config_path)
            input_description = "selected case corpus"
            cases = load_cases(config_path.parent / args.cases)
            input_description = "saved run"
            decision = check_run(
                config=config, target=args.target, run_id=args.run_id, cases=cases,
                results_dir=config_path.parent / "results",
                baseline_run_ids=args.baseline_run_ids,
            )
        except OSError:
            decision = invalid_decision(
                target=args.target, run_id=args.run_id, message=f"Cannot read {input_description}",
                baseline_run_ids=args.baseline_run_ids,
            )
        except (ValueError, YAMLError):
            decision = invalid_decision(
                target=args.target, run_id=args.run_id,
                message=f"Invalid {input_description}",
                baseline_run_ids=args.baseline_run_ids,
            )
        if args.json:
            print(decision.model_dump_json(indent=2))
        else:
            print_decision(decision)
        return decision.exit_code
    if args.command == "report":
        try:
            config_path = args.config.resolve()
            config = load_config(config_path)
            cases = load_cases(config_path.parent / args.cases)
            report = score_run(
                config=config, target=args.target, run_id=args.run_id, cases=cases,
                results_dir=config_path.parent / "results",
            )
        except (OSError, ValueError, YAMLError) as exc:
            parser.error(str(exc))
        if args.json:
            print(report.model_dump_json(indent=2))
        else:
            print_report(report)
        return
    if args.command == "fingerprint":
        try:
            config_path = args.config.resolve()
            config = load_config(config_path)
            cases = load_cases(config_path.parent / args.cases)
            identity = calculate_fingerprint(config, args.target, config_path, cases)
        except (OSError, ValueError, YAMLError) as exc:
            parser.error(str(exc))
        if args.json:
            print(identity.model_dump_json(indent=2))
        else:
            for name, value in identity.model_dump().items():
                print(f"{name}: {value}")
        return
    if args.command != "run":
        print("Not implemented yet.")
        return
    if args.yes and args.run_id is None:
        parser.error("--run-id is required with --yes")
    try:
        config_path = args.config.resolve()
        config = load_config(config_path)
        project_root = config_path.parent
        cases = load_cases(project_root / args.cases)
        plan = plan_run(
            cases, target=args.target, max_calls=args.max_calls,
            results_dir=project_root / "results", run_id=args.run_id,
            case_id=args.case_id, force=args.force, yes=args.yes,
        )
        if not plan.confirmed:
            for case in plan.selected_cases:
                preflight_case(case, config_path)
    except (OSError, ValueError, YAMLError) as exc:
        parser.error(str(exc))
    target_config = getattr(config.targets, plan.target)
    print(f"Run plan: target={plan.target}; base_url={target_config.base_url}")
    print(f"Run ID: {plan.run_id if plan.run_id is not None else '(not supplied; resume disabled)'}")
    print(f"Selected calls: {len(plan.selected_cases)} / {args.max_calls}")
    print("Cases: " + (", ".join(case.id for case in plan.selected_cases) or "(none)"))
    print("Skipped successes: " + (", ".join(plan.skipped_case_ids) or "(none)"))
    print(
        "Confirmation (--yes): supplied." if plan.confirmed
        else "Confirmation (--yes): absent; real execution requires --yes."
    )
    if not plan.confirmed:
        print("Plan only: live execution requires --yes.")
        return
    try:
        summary = execute_plan(
            plan, config=config, cases=cases, config_path=config_path,
            results_dir=project_root / "results", client_factory=httpx.Client,
            on_recorded=print_attempt,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if summary.attempted_count == 0:
        print("Nothing to run: no eligible cases.")
    print(
        f"Run summary: attempted={summary.attempted_count}; "
        f"successful_attempts={summary.successful_attempt_count}; "
        f"error_attempts={summary.error_attempt_count}; "
        f"skipped_successes={summary.skipped_success_count}"
    )


if __name__ == "__main__":
    raise SystemExit(main())

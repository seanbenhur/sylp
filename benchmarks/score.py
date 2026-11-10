from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

METRICS = (
    "acceptance_pass",
    "regression_pass",
    "recovery_pass",
    "clarification_pass",
    "compaction_pass",
    "tool_recovery_pass",
)
USAGE_FIELDS = ("input_tokens", "output_tokens", "billed_cost_usd")
REQUIRED = ("task", "arm", "run_number", "acceptance_pass", "wall_seconds", "human_interventions")
ARMS = {"everrun", "codex"}


def _read_records(paths: list[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for path in paths:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc.msg}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{line_number}: each record must be a JSON object")
            missing = [key for key in REQUIRED if key not in record]
            if missing:
                raise ValueError(f"{path}:{line_number}: missing required fields: {', '.join(missing)}")
            if not isinstance(record["task"], str) or not record["task"].strip():
                raise ValueError(f"{path}:{line_number}: task must be a non-empty string")
            if not isinstance(record["arm"], str) or record["arm"] not in ARMS:
                raise ValueError(f"{path}:{line_number}: arm must be one of {', '.join(sorted(ARMS))}")
            if type(record["run_number"]) is not int or record["run_number"] < 1:
                raise ValueError(f"{path}:{line_number}: run_number must be a positive integer")
            for metric in METRICS:
                value = record.get(metric)
                if value is not None and type(value) is not bool:
                    raise ValueError(f"{path}:{line_number}: {metric} must be true, false, or null")
            for key in ("wall_seconds",):
                value = record[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValueError(f"{path}:{line_number}: {key} must be a finite non-negative number")
            for key in USAGE_FIELDS:
                value = record.get(key)
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                          or not math.isfinite(value) or value < 0):
                    raise ValueError(f"{path}:{line_number}: {key} must be a finite non-negative number or null")
            interventions = record["human_interventions"]
            if type(interventions) is not int or interventions < 0:
                raise ValueError(f"{path}:{line_number}: human_interventions must be a non-negative integer")
            key = (record["task"], record["arm"], record["run_number"])
            if key in seen:
                raise ValueError(f"{path}:{line_number}: duplicate run record {key!r}")
            seen.add(key)
            records.append(record)
    if not records:
        raise ValueError("No run records found")
    return records


def _metric_summary(records: list[dict[str, Any]], metric: str) -> dict[str, int]:
    measured = [record[metric] for record in records if record.get(metric) is not None]
    return {"passed": sum(measured), "measured": len(measured)}


def _usage_summary(records: list[dict[str, Any]], field: str) -> dict[str, int | float]:
    values = [record[field] for record in records if record.get(field) is not None]
    return {"total": sum(values), "reported_runs": len(values), "runs": len(records)}


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[(record["task"], record["arm"])].append(record)

    task_results = []
    for (task, arm), runs in sorted(groups.items()):
        runs.sort(key=lambda record: record["run_number"])
        task_results.append({
            "task": task,
            "arm": arm,
            "runs": len(runs),
            "metrics": {metric: _metric_summary(runs, metric) for metric in METRICS},
            "wall_seconds": {
                "median": statistics.median(record["wall_seconds"] for record in runs),
                "min": min(record["wall_seconds"] for record in runs),
                "max": max(record["wall_seconds"] for record in runs),
            },
            "human_interventions": {
                "median": statistics.median(record["human_interventions"] for record in runs),
                "min": min(record["human_interventions"] for record in runs),
                "max": max(record["human_interventions"] for record in runs),
            },
            "usage": {field: _usage_summary(runs, field) for field in USAGE_FIELDS},
            "billing_paths": sorted({record["billing_path"] for record in runs
                                      if isinstance(record.get("billing_path"), str)}),
        })

    failures = []
    for record in records:
        failed_metrics = [metric for metric in METRICS if record.get(metric) is False]
        if failed_metrics:
            failures.append({"task": record["task"], "arm": record["arm"],
                             "run_number": record["run_number"], "failed_metrics": failed_metrics})
    return {"runs": len(records), "task_results": task_results, "failures": failures}


def _ratio(value: dict[str, int]) -> str:
    return f"{value['passed']}/{value['measured']}" if value["measured"] else "n/a"


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Everrun benchmark results",
        "",
        f"Run records: {summary['runs']}",
        "",
        "Each pass metric is shown as passed/measured. Null and missing optional metrics are excluded from its denominator.",
        "",
        "| Task | Arm | Runs | Acceptance | Regression | Recovery | Clarification | Compaction | Tool recovery | Wall time (median; range s) | Interventions (median; range) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in summary["task_results"]:
        metrics = result["metrics"]
        wall = result["wall_seconds"]
        interventions = result["human_interventions"]
        lines.append(
            f"| {result['task']} | {result['arm']} | {result['runs']} | "
            f"{_ratio(metrics['acceptance_pass'])} | {_ratio(metrics['regression_pass'])} | "
            f"{_ratio(metrics['recovery_pass'])} | {_ratio(metrics['clarification_pass'])} | "
            f"{_ratio(metrics['compaction_pass'])} | {_ratio(metrics['tool_recovery_pass'])} | "
            f"{wall['median']:g} ({wall['min']:g}–{wall['max']:g}) | "
            f"{interventions['median']:g} ({interventions['min']}–{interventions['max']}) |"
        )
    lines.extend(("", "## Failed checks", ""))
    if summary["failures"]:
        for failure in summary["failures"]:
            lines.append(f"- {failure['task']} / {failure['arm']} run {failure['run_number']}: "
                         f"{', '.join(failure['failed_metrics'])}")
    else:
        lines.append("No recorded failed checks.")
    lines.extend(("", "## Reported usage", "",
                  "Token and cost totals include only runs that reported a value; the reported-run count is shown so partial totals stay explicit.",
                  "",
                  "| Task | Arm | Input tokens | Output tokens | Billed cost USD | Billing path |",
                  "|---|---|---:|---:|---:|---|"))
    for result in summary["task_results"]:
        usage = result["usage"]
        input_usage = usage["input_tokens"]
        output_usage = usage["output_tokens"]
        cost = usage["billed_cost_usd"]
        paths = ", ".join(result["billing_paths"]) or "unreported"
        lines.append(
            f"| {result['task']} | {result['arm']} | "
            f"{input_usage['total']:g} ({input_usage['reported_runs']}/{input_usage['runs']} runs) | "
            f"{output_usage['total']:g} ({output_usage['reported_runs']}/{output_usage['runs']} runs) | "
            f"{cost['total']:.6g} ({cost['reported_runs']}/{cost['runs']} runs) | {paths} |"
        )
    lines.extend(("", "This report includes only recorded runs. Missing measurements are not passes.", ""))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate benchmark JSONL records and create a transparent scorecard")
    parser.add_argument("records", nargs="+", type=Path, help="JSONL run record files")
    parser.add_argument("--json-out", type=Path, help="Also write the machine-readable summary")
    args = parser.parse_args()
    try:
        summary = summarize(_read_records(args.records))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(render_markdown(summary), end="")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

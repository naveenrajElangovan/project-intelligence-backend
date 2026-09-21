"""Fail closed when Jira promotion evidence is incomplete or below its gates."""

import math
from typing import cast


def promotion_failures(report: dict[str, object]) -> list[str]:
    failures: list[str] = []

    def count_at_least(name: str, minimum: int) -> bool:
        value = report.get(name)
        return type(value) is int and cast(int, value) >= minimum

    if report.get("full_ingestion_complete") is not True:
        failures.append("full_ingestion_incomplete")
    if report.get("structural_quality_passed") is not True:
        failures.append("structural_quality_unverified")
    if not count_at_least("completed_bilingual_cases", 40):
        failures.append("bilingual_suite_incomplete")
    if not count_at_least("english_cases", 20) or not count_at_least("spanish_cases", 20):
        failures.append("language_coverage_incomplete")
    if not count_at_least("current_status_cases", 1):
        failures.append("current_status_unmeasured")
    for name in (
        "unauthorized_exposures",
        "duplicate_source_identities",
        "silent_truncations",
        "unexplained_collection_failures",
    ):
        if report.get(name) != 0 or isinstance(report.get(name), bool):
            failures.append(name + "_not_zero_or_unmeasured")
    thresholds = {
        "exact_current_status_accuracy": 1.0,
        "candidate_evidence_recall": 0.90,
        "final_evidence_recall": 0.85,
        "citation_correctness": 0.95,
        "grounded_answer_abstention_correctness": 0.90,
    }
    for name, minimum in thresholds.items():
        value = report.get(name)
        if type(value) not in (float, int):
            failures.append(name + "_below_gate_or_unmeasured")
            continue
        numeric_value = float(cast("int | float", value))
        if not math.isfinite(numeric_value) or not minimum <= numeric_value <= 1.0:
            failures.append(name + "_below_gate_or_unmeasured")
    regression = report.get("non_jira_regression_percentage_points")
    valid_regression = type(regression) in (float, int)
    regression_value = float(cast("int | float", regression)) if valid_regression else math.inf
    if (
        not count_at_least("non_jira_baseline_cases", 1)
        or report.get("non_jira_comparison_cases") != report.get("non_jira_baseline_cases")
        or not math.isfinite(regression_value)
        or regression_value > 5.0
    ):
        failures.append("non_jira_regression_unverified_or_above_gate")
    return failures

from app.evaluations.jira_quality_gate import promotion_failures


def passing():
    return {
        "full_ingestion_complete": True,
        "structural_quality_passed": True,
        "completed_bilingual_cases": 40,
        "english_cases": 20,
        "spanish_cases": 20,
        "current_status_cases": 20,
        "unauthorized_exposures": 0,
        "duplicate_source_identities": 0,
        "silent_truncations": 0,
        "unexplained_collection_failures": 0,
        "exact_current_status_accuracy": 1.0,
        "candidate_evidence_recall": 0.90,
        "final_evidence_recall": 0.85,
        "citation_correctness": 0.95,
        "grounded_answer_abstention_correctness": 0.90,
        "non_jira_baseline_cases": 80,
        "non_jira_comparison_cases": 80,
        "non_jira_regression_percentage_points": 5.0,
    }


def test_only_complete_measured_report_at_thresholds_passes():
    assert promotion_failures(passing()) == []
    assert promotion_failures({})
    for name in passing():
        report = passing()
        report[name] = None
        failures = promotion_failures(report)
        assert failures, name


def test_nan_false_zero_and_missing_regression_are_not_passes():
    for name, value in [
        ("citation_correctness", float("nan")),
        ("unauthorized_exposures", False),
        ("exact_current_status_accuracy", 0.999),
        ("non_jira_regression_percentage_points", 5.01),
    ]:
        assert promotion_failures({**passing(), name: value})

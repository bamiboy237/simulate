"""This module checks the eight fixed scenario definitions."""

from app.domain.agent.scenarios import (
    SCENARIO_BY_ID,
    SCENARIOS,
    ScenarioCategory,
)
from app.telemetry.allowlist import TRACE_ATTRIBUTE_ALLOWLIST

EXPECTED_IDS = (
    "phase2-01-bad-prompt-policy-answer",
    "phase2-02-wrong-policy-evidence",
    "phase2-03-database-timeout",
    "phase2-04-wrong-tool-arguments",
    "phase2-05-unconfirmed-refund",
    "phase2-06-repeated-step",
    "phase2-07-slow-database",
    "phase2-08-model-cost-comparison",
)


def test_exactly_eight_scenarios_with_stable_ids() -> None:
    assert tuple(scenario.scenario_id for scenario in SCENARIOS) == EXPECTED_IDS
    assert set(SCENARIO_BY_ID) == set(EXPECTED_IDS)


def test_scenarios_cover_eight_distinct_layers() -> None:
    categories = {scenario.category for scenario in SCENARIOS}
    assert categories == set(ScenarioCategory)


def test_all_requests_are_valid() -> None:
    for scenario in SCENARIOS:
        assert scenario.request.message
        assert len(scenario.request.message) <= 2000


def test_trace_evidence_is_allowlisted() -> None:
    for scenario in SCENARIOS:
        for attribute in scenario.required_trace_evidence:
            assert attribute in TRACE_ATTRIBUTE_ALLOWLIST, (
                f"{scenario.scenario_id} requires unallowlisted attribute {attribute}"
            )


def test_local_fields_never_overlap_trace_evidence() -> None:
    for scenario in SCENARIOS:
        overlap = set(scenario.local_fields) & set(scenario.required_trace_evidence)
        assert not overlap, f"{scenario.scenario_id} local fields overlap evidence"


def test_performance_budgets_are_positive_when_set() -> None:
    for scenario in SCENARIOS:
        if scenario.performance_budget_ms is not None:
            assert scenario.performance_budget_ms > 0


def test_cost_comparison_requires_second_model() -> None:
    comparison = SCENARIO_BY_ID["phase2-08-model-cost-comparison"]
    assert comparison.category is ScenarioCategory.COST_COMPARISON
    assert comparison.requires_second_model is True


def test_scenarios_define_local_fields_and_safe_behavior() -> None:
    for scenario in SCENARIOS:
        assert scenario.local_fields
        assert scenario.expected_safe_behavior
        assert scenario.reproduction_state

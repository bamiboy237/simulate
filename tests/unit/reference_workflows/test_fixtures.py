"""This module verifies the reference workflow fixtures are deterministic and self-consistent.

The fixtures must be offline, importable without application code, and stable:
identical imports always produce identical identifiers and content hashes.
"""

import json
from dataclasses import asdict

import pytest

from app.domain.reference_workflows import disputes, onboarding, returns_resolution

MODULES = (returns_resolution, onboarding, disputes)


def _scenario_hash(scenario: object) -> str:
    payload = {key: value for key, value in asdict(scenario).items() if key != "content_hash"}
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
def test_fixture_module_has_expected_exports(module: object) -> None:
    assert module.WORKFLOW_SLUG
    assert module.WORKFLOW_VERSION
    assert module.SAFE_TOOLS
    assert module.SENSITIVE_TOOLS
    assert module.STATE_TRANSITIONS
    assert module.SCENARIOS


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
def test_policy_content_hashes_are_stable(module: object) -> None:
    for policy in module.ALL_DOCUMENTS:
        assert policy.content_hash == module.content_hash(policy.content)


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
def test_scenario_content_hashes_are_stable(module: object) -> None:
    for scenario in module.SCENARIOS:
        expected = module.content_hash(_scenario_hash(scenario))
        assert scenario.content_hash == expected


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
def test_scenario_ids_are_unique_and_prefixed(module: object) -> None:
    ids = [scenario.scenario_id for scenario in module.SCENARIOS]
    assert len(ids) == len(set(ids))
    prefix = module.SCENARIO_ID_PREFIX
    assert all(scenario_id.startswith(prefix + "-") for scenario_id in ids)


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
def test_every_scenario_has_bounded_actions_and_budgets(module: object) -> None:
    allowed = frozenset(module.SAFE_TOOLS + module.SENSITIVE_TOOLS)
    for scenario in module.SCENARIOS:
        assert set(scenario.eligible_actions) <= allowed
        assert scenario.expected_reason_codes
        assert scenario.permitted_state_transitions
        assert scenario.dependency_coverage
        assert scenario.performance_budget_ms and scenario.performance_budget_ms > 0


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
def test_comparison_variable_is_single_and_documented(module: object) -> None:
    variable = module.COMPARISON_VARIABLE
    assert variable.name
    assert variable.baseline
    assert variable.candidate
    assert variable.baseline != variable.candidate

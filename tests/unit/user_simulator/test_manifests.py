"""Focused tests for the strict user-simulator YAML manifests.

The shipped catalogs are metadata only: they reference registered plugin ids
and environment profile ids, never tool code, commands, or secrets.  These
tests lock down the loader contract the setup wizard relies on and prove the
grouped YAML catalogs replace the 15 hardwired launcher scripts.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.domain.user_simulator.manifests import (
    DEFAULT_ENVIRONMENTS_FILE,
    DEFAULT_PROFILE_ID,
    DEFAULT_REFERENCE_MANIFEST,
    DEFAULT_SUPPORT_MANIFEST,
    CatalogError,
    load_environment_profiles,
    load_simulation_catalog,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SUPPORT_YAML = REPO_ROOT / DEFAULT_SUPPORT_MANIFEST
REFERENCE_YAML = REPO_ROOT / DEFAULT_REFERENCE_MANIFEST
ENVIRONMENTS_YAML = REPO_ROOT / DEFAULT_ENVIRONMENTS_FILE

SUPPORT_IDS = (
    "phase2-01-bad-prompt-policy-answer",
    "phase2-02-wrong-policy-evidence",
    "phase2-03-database-timeout",
    "phase2-04-wrong-tool-arguments",
    "phase2-05-unconfirmed-refund",
    "phase2-06-repeated-step",
    "phase2-07-slow-database",
    "phase2-08-model-cost-comparison",
)
REFERENCE_IDS = (
    "reference-flight_booking",
    "reference-incident_response",
    "reference-ci_triage",
    "reference-claims_denial",
    "reference-returns_resolution",
    "reference-onboarding",
    "reference-disputes",
)


def _write_catalog(
    tmp_path: Path,
    entries: list[dict[str, object]],
    *,
    name: str = "catalog.yaml",
    **extra: object,
) -> Path:
    payload = {
        "schema_version": "1.0",
        "catalog_id": "test-catalog",
        "group": "support",
        "scenarios": entries,
        **extra,
    }
    path = tmp_path / name
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _valid_entry(
    scenario_id: str = "phase2-01-bad-prompt-policy-answer",
    **overrides: object,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "scenario_id": scenario_id,
        "plugin_id": scenario_id,
        "name": "A scenario",
        "description": "A description",
        "max_turns": 12,
        "environment_profile": DEFAULT_PROFILE_ID,
    }
    entry.update(overrides)
    return entry


def _env_entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "profile_id": DEFAULT_PROFILE_ID,
        "label": "A test profile",
        "environment": "test",
        "loopback_only": True,
        "db_url_env": "LAB_TEST_PG_URL",
        "db_host": "127.0.0.1",
        "db_port": 5433,
        "db_name": "lab",
        "isolation_policy": "transaction-rollback",
        "artifact_root": "artifacts/user-simulator",
        "required_variables": ["LAB_TEST_PG_URL"],
    }
    entry.update(overrides)
    return entry


# ---------------------------------------------------------------------------
# Shipped catalog
# ---------------------------------------------------------------------------


def test_shipped_catalog_is_valid_and_complete() -> None:
    catalog = load_simulation_catalog()
    assert catalog.ok, catalog.validate()
    assert len(catalog.scenarios) == 15
    assert {scenario.group for scenario in catalog.scenarios} == {"support", "reference"}
    assert sum(scenario.group == "support" for scenario in catalog.scenarios) == 8
    assert sum(scenario.group == "reference" for scenario in catalog.scenarios) == 7


def test_shipped_catalog_uses_exact_builtin_plugin_ids() -> None:
    """The grouped YAML catalogs replace the 15 hardwired launcher scripts."""
    from app.domain.user_simulator.personas import PERSONA_BY_ID

    catalog = load_simulation_catalog()
    assert {scenario.plugin_id for scenario in catalog.scenarios} == set(PERSONA_BY_ID)


def test_shipped_catalog_scenarios_are_deterministic_and_unique() -> None:
    catalog = load_simulation_catalog()
    ids = catalog.scenario_ids()
    assert ids == tuple(sorted(ids))
    assert len(set(ids)) == len(ids)
    assert load_simulation_catalog().scenario_ids() == ids


def test_shipped_catalog_uses_expected_scenario_ids() -> None:
    catalog = load_simulation_catalog()
    assert catalog.scenario_ids() == tuple(sorted(SUPPORT_IDS + REFERENCE_IDS))


def test_shipped_scenarios_carry_authoritative_defaults() -> None:
    catalog = load_simulation_catalog()
    for scenario in catalog.scenarios:
        assert scenario.persona
        assert scenario.script
        assert scenario.goal
        assert scenario.name
        assert 1 <= scenario.max_turns <= 50
        assert scenario.environment_profile == DEFAULT_PROFILE_ID


def test_shipped_scenarios_use_specific_human_requests() -> None:
    """Keep the live-run setup copy concrete instead of falling back to templates."""
    catalog = load_simulation_catalog()
    assert all("Please help with the" not in scenario.script for scenario in catalog.scenarios)
    assert all(
        "A business user describing a time-sensitive request" not in scenario.persona
        for scenario in catalog.scenarios
    )
    assert catalog.get("reference-ci_triage").script.startswith(
        "The Linux checks failed on PR 4821"
    )
    assert catalog.get("reference-claims_denial").script.startswith(
        "Claim R-4421 was denied with code 59"
    )
    assert "right-to-work" in catalog.get("reference-onboarding").script


def test_shipped_environment_profile_is_lab_test_pg() -> None:
    (profile,) = load_environment_profiles()
    assert profile.profile_id == "lab-test-pg"
    assert profile.environment == "test"
    assert profile.db_url_env == "DATABASE_URL"
    assert profile.isolation_policy == "transaction-rollback"
    assert profile.artifact_root == "artifacts/user-simulator"
    names = profile.required_variables
    assert "DATABASE_URL" in names
    assert "MODEL_API_KEY" in names
    assert all(name == name.upper() for name in names)


def test_shipped_yaml_contains_no_commands_or_secrets() -> None:
    import re

    secret_patterns = (
        re.compile(r"(?i)\bsk-[a-z0-9]{8,}\b"),
        re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+"),
        re.compile(r"://[^/@\s:]+:[^/@\s]+@"),
    )
    for path in (SUPPORT_YAML, REFERENCE_YAML, ENVIRONMENTS_YAML):
        text = path.read_text(encoding="utf-8")
        assert "migration_command" not in text
        assert "command:" not in text
        assert "api_key:" not in text
        assert "password:" not in text
        for pattern in secret_patterns:
            assert pattern.search(text) is None, (path, pattern.pattern)


# ---------------------------------------------------------------------------
# Catalog lookup
# ---------------------------------------------------------------------------


def test_flat_scenario_lookup() -> None:
    catalog = load_simulation_catalog()
    assert catalog.find_scenario("phase2-01-bad-prompt-policy-answer") is not None
    assert catalog.find_scenario("does-not-exist") is None
    with pytest.raises(CatalogError) as exc:
        catalog.get("does-not-exist")
    message = str(exc.value)
    assert "catalog" in message and "scenario_id" in message


def test_environment_lookup_raises_safe_error_when_missing() -> None:
    catalog = load_simulation_catalog()
    assert catalog.environment("lab-test-pg").profile_id == "lab-test-pg"
    with pytest.raises(CatalogError) as exc:
        catalog.environment("does-not-exist")
    message = str(exc.value)
    assert "simulation-environments.yaml" in message
    assert "profile_id" in message
    assert "lab-test-pg" in message  # available list is safe, not secret


def test_validate_returns_filename_field_messages() -> None:
    catalog = load_simulation_catalog()
    assert catalog.ok
    assert catalog.validate() == ()


# ---------------------------------------------------------------------------
# Strict validation rules
# ---------------------------------------------------------------------------


def test_duplicate_scenario_id_across_files_rejected(tmp_path: Path) -> None:
    first = _write_catalog(tmp_path, [_valid_entry()], name="first.yaml", catalog_id="catalog-a")
    second = _write_catalog(tmp_path, [_valid_entry()], name="second.yaml", catalog_id="catalog-b")
    catalog = load_simulation_catalog([first, second])
    assert not catalog.ok
    messages = catalog.validate()
    assert any("duplicate scenario id" in message for message in messages)


def test_duplicate_catalog_id_rejected(tmp_path: Path) -> None:
    first = _write_catalog(
        tmp_path,
        [_valid_entry(scenario_id="phase2-01-bad-prompt-policy-answer")],
        name="first.yaml",
    )
    second = _write_catalog(
        tmp_path, [_valid_entry(scenario_id="phase2-02-wrong-policy-evidence")], name="second.yaml"
    )
    catalog = load_simulation_catalog([first, second])
    assert not catalog.ok
    assert any("duplicate catalog id" in message for message in catalog.validate())


def test_unknown_plugin_id_rejected_via_injected_allowlist(tmp_path: Path) -> None:
    path = _write_catalog(tmp_path, [_valid_entry(plugin_id="not-registered")])
    catalog = load_simulation_catalog(
        [path], known_plugin_ids={"phase2-01-bad-prompt-policy-answer"}
    )
    assert not catalog.ok
    assert any("unknown plugin id" in message for message in catalog.validate())


def test_unknown_environment_profile_rejected_via_injected_allowlist(tmp_path: Path) -> None:
    path = _write_catalog(tmp_path, [_valid_entry(environment_profile="not-a-profile")])
    catalog = load_simulation_catalog([path], known_environment_ids={"lab-test-pg"})
    assert not catalog.ok
    assert any("unknown environment profile" in message for message in catalog.validate())


def test_secret_looking_keys_and_values_rejected(tmp_path: Path) -> None:
    path = _write_catalog(
        tmp_path,
        [_valid_entry(description="plain", api_key="sk-1234567890abcdef")],
    )
    catalog = load_simulation_catalog([path])
    assert any("secret-looking" in message for message in catalog.validate())


def test_credential_url_rejected(tmp_path: Path) -> None:
    path = _write_catalog(
        tmp_path,
        [_valid_entry(description="https://user:secret@db.example/lab")],
    )
    catalog = load_simulation_catalog([path])
    assert any("credential URL" in message for message in catalog.validate())


def test_extra_fields_are_forbidden(tmp_path: Path) -> None:
    path = _write_catalog(tmp_path, [_valid_entry(simulation_id="old-field-name")])
    catalog = load_simulation_catalog([path])
    assert not catalog.ok
    assert any("extra" in message.lower() for message in catalog.validate())


def test_max_turns_bounds_are_enforced(tmp_path: Path) -> None:
    for bad in (0, 51):
        path = _write_catalog(tmp_path, [_valid_entry(max_turns=bad)])
        catalog = load_simulation_catalog([path])
        assert not catalog.ok
    good = _write_catalog(tmp_path, [_valid_entry(max_turns=50)])
    assert load_simulation_catalog([good]).ok


def test_group_is_inherited_from_file_and_validated(tmp_path: Path) -> None:
    path = _write_catalog(tmp_path, [_valid_entry()])
    catalog = load_simulation_catalog([path])
    assert catalog.ok
    assert catalog.scenarios[0].group == "support"
    custom = _write_catalog(tmp_path, [_valid_entry()], group="custom")
    assert load_simulation_catalog([custom]).ok
    assert load_simulation_catalog([custom]).scenarios[0].group == "custom"
    bad = _write_catalog(tmp_path, [_valid_entry()], group="Bad Group")
    assert not load_simulation_catalog([bad]).ok


def test_missing_files_report_safe_issues(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"
    catalog = load_simulation_catalog([missing])
    assert not catalog.ok
    assert any("cannot read" in message for message in catalog.validate())


def test_load_environment_profiles_raises_safely_on_bad_file(tmp_path: Path) -> None:
    bad = tmp_path / "environments.yaml"
    bad.write_text("environments: [broken", encoding="utf-8")
    with pytest.raises(CatalogError) as exc:
        load_environment_profiles(bad)
    assert "environments.yaml" in str(exc.value)


def test_load_environment_profiles_accepts_custom_environment(tmp_path: Path) -> None:
    path = tmp_path / "environments.yaml"
    path.write_text(
        yaml.safe_dump(
            {"schema_version": "1.0", "environments": [_env_entry(environment="local")]},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (profile,) = load_environment_profiles(path)
    assert profile.environment == "local"


def test_environment_profile_extra_fields_forbidden(tmp_path: Path) -> None:
    path = tmp_path / "environments.yaml"
    path.write_text(
        yaml.safe_dump(
            {"schema_version": "1.0", "environments": [_env_entry(db_password="hunter2")]},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(CatalogError) as exc:
        load_environment_profiles(path)
    assert "db_password" in str(exc.value)


def test_environment_profile_bounds_are_enforced(tmp_path: Path) -> None:
    path = tmp_path / "environments.yaml"
    path.write_text(
        yaml.safe_dump(
            {"schema_version": "1.0", "environments": [_env_entry(db_port=70000)]},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(CatalogError):
        load_environment_profiles(path)

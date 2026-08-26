"""Strict YAML manifests for the user simulator setup wizard.

This module is the single source of truth for simulation setup metadata.  It
loads grouped scenario catalogs (``simulations/support.yaml`` and
``simulations/reference-workflows.yaml``) and the non-secret environment
profiles (``config/simulation-environments.yaml``) into strictly validated
Pydantic models.

The YAML files are metadata only: each scenario references a registered
``plugin_id`` and an environment ``profile_id`` and never contains tool code,
commands, or secrets.  Secret values are referenced by environment variable
NAME (``required_variables`` / ``db_url_env``) and resolved at runtime.

Loader contract (shared with the setup wizard):

- ``load_simulation_catalog(paths, *, known_plugin_ids, known_environment_ids)``
  returns a ``SimulationCatalog`` whose ``.issues`` collect every problem with
  a safe ``filename: field`` message.  It never raises for content problems.
- ``load_environment_profiles(path)`` returns the strictly validated profiles
  and raises ``CatalogError`` (safe messages) when the file cannot be used.

Validation is deliberately strict: extra fields are forbidden, scenario ids
and catalog ids must be unique across files, plugin/profile ids are validated
against injected allowlists, max turns are bounded, environments must be
``test``, and secret-looking keys, secret-looking values, and credential URLs
are rejected everywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Sequence

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

from app.domain.user_simulator.flows import DEFAULT_MAX_TURNS

SCHEMA_VERSION = "1.0"
DEFAULT_SIMULATIONS_DIR = Path("simulations")
DEFAULT_SUPPORT_MANIFEST = DEFAULT_SIMULATIONS_DIR / "support.yaml"
DEFAULT_REFERENCE_MANIFEST = DEFAULT_SIMULATIONS_DIR / "reference-workflows.yaml"
DEFAULT_ENVIRONMENTS_FILE = Path("config") / "simulation-environments.yaml"
DEFAULT_PROFILE_ID = "lab-test-pg"

_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"
_ENV_VAR_PATTERN = r"^[A-Z][A-Z0-9_]*$"

# Secret-looking YAML keys are rejected by name ...
_FORBIDDEN_KEY = re.compile(
    r"(?i)(password|passwd|api[_-]?key|secret|token|credential|private[_-]?key)"
)
# ... and secret-looking values are rejected by shape.
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\bsk-[a-z0-9]{8,}\b"),
    re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+"),
    re.compile(r"://[^/@\s:]+:[^/@\s]+@"),
)

EnvVarName = Annotated[str, Field(pattern=_ENV_VAR_PATTERN)]


class Scenario(BaseModel):
    """One simulation: metadata for a registered plugin instance.

    ``group`` is a free-form lowercase id (``support`` and ``reference`` for
    the shipped catalogs) so third-party catalogs stay loadable.
    """

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(min_length=1, pattern=_ID_PATTERN)
    plugin_id: str = Field(min_length=1, pattern=_ID_PATTERN)
    group: str = Field(min_length=1, pattern=_ID_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    persona: str = Field(default="", max_length=2000)
    script: str = Field(default="", max_length=4000)
    goal: str = Field(default="", max_length=1000)
    max_turns: int = Field(default=DEFAULT_MAX_TURNS, ge=1, le=50)
    environment_profile: str = Field(
        default=DEFAULT_PROFILE_ID, min_length=1, pattern=_ID_PATTERN
    )


class EnvironmentProfile(BaseModel):
    """One disposable test environment.  Non-secret configuration only.

    The YAML file carries the schema version at its top level; each profile
    entry carries only profile fields.
    """

    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(min_length=1, pattern=_ID_PATTERN)
    label: str = Field(min_length=1, max_length=200)
    environment: str = "test"
    loopback_only: bool = False
    db_url_env: EnvVarName = Field(default="DATABASE_URL")
    db_host: str | None = Field(default=None, max_length=200)
    db_port: int | None = Field(default=None, ge=1, le=65535)
    db_name: str | None = Field(default=None, max_length=200)
    isolation_policy: Literal["transaction-rollback"] = "transaction-rollback"
    artifact_root: str = Field(
        default="artifacts/user-simulator", min_length=1, max_length=500
    )
    model_provider: str | None = Field(default=None, max_length=100)
    model_name: str | None = Field(default=None, max_length=200)
    required_variables: tuple[EnvVarName, ...] = ()


class _CatalogFile(BaseModel):
    """One scenario YAML file: version, unique catalog id, group, entries."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    catalog_id: str = Field(min_length=1, pattern=_ID_PATTERN)
    group: str = Field(min_length=1, pattern=_ID_PATTERN)
    name: str = Field(default="", max_length=200)
    scenarios: list[dict[str, Any]] = Field(min_length=1)


class _EnvironmentsFile(BaseModel):
    """One environment profiles YAML file."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    environments: list[dict[str, Any]] = Field(min_length=1)


@dataclass(frozen=True)
class CatalogIssue:
    """One catalog problem with a filename and a safe field label."""

    filename: str
    field: str
    message: str

    def __str__(self) -> str:
        return f"{self.filename}: {self.field}: {self.message}"


class CatalogError(ValueError):
    """Raised when a manifest cannot be used; carries safe issues."""

    def __init__(self, issues: Sequence[CatalogIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(str(issue) for issue in issues))


EnvironmentProfiles = tuple[EnvironmentProfile, ...]


class SimulationCatalog:
    """Strictly validated, immutable view over loaded scenarios and profiles.

    Scenarios are exposed in deterministic sorted order (by ``scenario_id``)
    with flat lookup for the setup wizard.
    """

    def __init__(
        self,
        *,
        scenarios: Sequence[Scenario],
        profiles: Sequence[EnvironmentProfile],
        issues: Sequence[CatalogIssue],
    ) -> None:
        self._scenarios = tuple(scenarios)
        self._profiles = tuple(profiles)
        self._issues = tuple(issues)

    @property
    def scenarios(self) -> tuple[Scenario, ...]:
        """Every scenario in deterministic (sorted) order."""
        return self._scenarios

    @property
    def issues(self) -> tuple[CatalogIssue, ...]:
        """Every validation problem; empty when the catalog is usable."""
        return self._issues

    @property
    def ok(self) -> bool:
        """True when the catalog loaded with no issues."""
        return not self._issues

    def validate(self) -> tuple[str, ...]:
        """Human-readable validation messages (empty when valid)."""
        return tuple(str(issue) for issue in self._issues)

    def find_scenario(self, scenario_id: str) -> Scenario | None:
        """Return one scenario by id, or ``None`` when it is absent."""
        for scenario in self._scenarios:
            if scenario.scenario_id == scenario_id:
                return scenario
        return None

    def get(self, scenario_id: str) -> Scenario:
        """Return one scenario by id or raise ``CatalogError`` (safe message)."""
        scenario = self.find_scenario(scenario_id)
        if scenario is None:
            raise CatalogError(
                [
                    CatalogIssue(
                        "catalog",
                        "scenario_id",
                        f"no scenario {scenario_id!r}; run 'lab simulate list'",
                    )
                ]
            )
        return scenario

    def scenario_ids(self) -> tuple[str, ...]:
        """Flat, deterministic scenario ids for the wizard."""
        return tuple(scenario.scenario_id for scenario in self._scenarios)

    def plugin_ids(self) -> tuple[str, ...]:
        """Flat plugin ids referenced by the loaded scenarios."""
        return tuple(scenario.plugin_id for scenario in self._scenarios)

    def environments(self) -> tuple[EnvironmentProfile, ...]:
        """Every loaded environment profile, in file order."""
        return self._profiles

    def profiles(self) -> tuple[EnvironmentProfile, ...]:
        """Alias for :meth:`environments` (profile-centric naming)."""
        return self._profiles

    def environment(self, profile_id: str) -> EnvironmentProfile:
        """Return one profile by id or raise ``CatalogError`` (safe message)."""
        for profile in self._profiles:
            if profile.profile_id == profile_id:
                return profile
        available = ", ".join(sorted(p.profile_id for p in self._profiles))
        raise CatalogError(
            [
                CatalogIssue(
                    DEFAULT_ENVIRONMENTS_FILE.name,
                    "profile_id",
                    f"unknown environment profile {profile_id!r}; available: {available}",
                )
            ]
        )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_simulation_catalog(
    paths: Path | str | Sequence[Path | str] | None = None,
    *,
    known_plugin_ids: set[str] | None = None,
    known_environment_ids: set[str] | None = None,
) -> SimulationCatalog:
    """Load and strictly validate the grouped simulation catalogs.

    ``paths`` defaults to the shipped support and reference-workflow files.
    ``known_plugin_ids`` and ``known_environment_ids`` inject the allowlists
    used for validation; when omitted, the built-in persona ids and the ids of
    the loaded environment profiles are used.  Content problems are collected
    into ``SimulationCatalog.issues`` and never raise.
    """
    if paths is None:
        paths = (DEFAULT_SUPPORT_MANIFEST, DEFAULT_REFERENCE_MANIFEST)
    elif isinstance(paths, (str, Path)):
        paths = (paths,)
    manifest_paths = tuple(Path(path) for path in paths)

    plugin_ids = known_plugin_ids if known_plugin_ids is not None else _default_plugin_ids()

    profiles, issues = _load_environment_profiles(DEFAULT_ENVIRONMENTS_FILE)
    environment_ids = (
        known_environment_ids
        if known_environment_ids is not None
        else set(profiles)
    )
    # When the environments file itself failed and no allowlist was injected,
    # every scenario would repeat the same profile error; report it once.
    check_profiles = known_environment_ids is not None or not issues

    scenarios: list[Scenario] = []
    seen_catalog_ids: set[str] = set()
    seen_scenario_ids: set[str] = set()
    for path in manifest_paths:
        payload, read_issues = _read_yaml_document(path)
        issues.extend(read_issues)
        if read_issues:
            continue
        issues.extend(_secret_issues(path.name, payload))
        try:
            catalog_file = _CatalogFile.model_validate(payload)
        except Exception as error:  # noqa: BLE001 - surfaced as a safe issue
            issues.append(CatalogIssue(path.name, "root", _safe_error(error)))
            continue
        if catalog_file.catalog_id in seen_catalog_ids:
            issues.append(
                CatalogIssue(
                    path.name,
                    "catalog_id",
                    f"duplicate catalog id {catalog_file.catalog_id!r}",
                )
            )
            continue
        seen_catalog_ids.add(catalog_file.catalog_id)
        for index, entry in enumerate(catalog_file.scenarios):
            try:
                scenario = Scenario.model_validate(
                    {**entry, "group": catalog_file.group}
                )
            except Exception as error:  # noqa: BLE001 - surfaced as a safe issue
                issues.append(
                    CatalogIssue(path.name, f"scenarios[{index}]", _safe_error(error))
                )
                continue
            if scenario.scenario_id in seen_scenario_ids:
                issues.append(
                    CatalogIssue(
                        path.name,
                        "scenario_id",
                        f"duplicate scenario id {scenario.scenario_id!r}",
                    )
                )
                continue
            seen_scenario_ids.add(scenario.scenario_id)
            if scenario.plugin_id not in plugin_ids:
                issues.append(
                    CatalogIssue(
                        path.name,
                        "plugin_id",
                        f"unknown plugin id {scenario.plugin_id!r} (not in known_plugin_ids)",
                    )
                )
                continue
            if check_profiles and scenario.environment_profile not in environment_ids:
                issues.append(
                    CatalogIssue(
                        path.name,
                        "environment_profile",
                        f"unknown environment profile {scenario.environment_profile!r}",
                    )
                )
                continue
            scenarios.append(scenario)

    scenarios.sort(key=lambda scenario: scenario.scenario_id)
    return SimulationCatalog(
        scenarios=tuple(scenarios),
        profiles=tuple(profiles.values()),
        issues=tuple(issues),
    )


def load_environment_profiles(
    path: Path | str | None = None,
) -> EnvironmentProfiles:
    """Load and strictly validate the environment profiles.

    ``path`` defaults to ``config/simulation-environments.yaml``.  Raises
    ``CatalogError`` with safe ``filename: field`` messages when the file is
    missing, unreadable, or invalid, because a broken profiles file is fatal
    for setup/preflight.
    """
    source = Path(path) if path is not None else DEFAULT_ENVIRONMENTS_FILE
    profiles, issues = _load_environment_profiles(source)
    if issues:
        raise CatalogError(issues)
    return EnvironmentProfiles(profiles.values())


def _load_environment_profiles(
    path: Path,
) -> tuple[dict[str, EnvironmentProfile], list[CatalogIssue]]:
    """Internal loader: returns profiles plus collected issues (never raises)."""
    payload, issues = _read_yaml_document(path)
    if issues:
        return {}, issues
    issues.extend(_secret_issues(path.name, payload))
    try:
        environments_file = _EnvironmentsFile.model_validate(payload)
    except Exception as error:  # noqa: BLE001 - surfaced as a safe issue
        issues.append(CatalogIssue(path.name, "root", _safe_error(error)))
        return {}, issues
    profiles: dict[str, EnvironmentProfile] = {}
    for index, entry in enumerate(environments_file.environments):
        if not isinstance(entry, dict):
            issues.append(
                CatalogIssue(path.name, f"environments[{index}]", "must be a mapping")
            )
            continue
        try:
            profile = EnvironmentProfile.model_validate(entry)
        except Exception as error:  # noqa: BLE001 - surfaced as a safe issue
            issues.append(
                CatalogIssue(path.name, f"environments[{index}]", _safe_error(error))
            )
            continue
        if profile.profile_id in profiles:
            issues.append(
                CatalogIssue(
                    path.name,
                    "profile_id",
                    f"duplicate environment profile {profile.profile_id!r}",
                )
            )
            continue
        profiles[profile.profile_id] = profile
    return profiles, issues


def _read_yaml_document(path: Path) -> tuple[dict[str, Any], list[CatalogIssue]]:
    """Read one YAML document; parse problems become safe issues."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        return {}, [CatalogIssue(path.name, "yaml", f"cannot read: {_safe_error(error)}")]
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as error:
        return {}, [CatalogIssue(path.name, "yaml", f"cannot parse: {_safe_error(error)}")]
    if not isinstance(payload, dict):
        return {}, [CatalogIssue(path.name, "root", "must be a YAML mapping")]
    return payload, []


def _default_plugin_ids() -> set[str]:
    """Fallback allowlist: the 15 built-in persona ids."""
    from app.domain.user_simulator.personas import ALL_PERSONAS

    return {persona.persona_id for persona in ALL_PERSONAS}


def _secret_issues(filename: str, mapping: dict[str, Any]) -> list[CatalogIssue]:
    """Reject secret-looking keys, values, or credential URLs in one mapping."""
    issues: list[CatalogIssue] = []
    for key, value in mapping.items():
        if _FORBIDDEN_KEY.search(key):
            issues.append(
                CatalogIssue(
                    filename, key, f"key {key!r} is not allowed (secret-looking name)"
                )
            )
        if isinstance(value, str) and any(
            pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS
        ):
            issues.append(
                CatalogIssue(
                    filename,
                    key,
                    "value looks like a secret or credential URL; "
                    "reference secrets by environment variable name instead",
                )
            )
        if isinstance(value, dict):
            issues.extend(_secret_issues(filename, value))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    issues.extend(_secret_issues(filename, item))
    return issues


def _safe_error(error: Exception) -> str:
    """Keep Pydantic/parse errors short and free of raw values."""
    text = str(error).replace("\n", "; ")
    if len(text) > 400:
        text = text[:397] + "..."
    return text

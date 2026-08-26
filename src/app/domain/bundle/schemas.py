"""This module defines the versioned SimulationBundle schema.

A bundle is the portable artifact that Phase 6 will load into an isolated
environment. It contains source evidence references, a privacy-safe scenario,
synthetic resource seeds, approved expected behavior, workflow, model, prompt,
tool, policy, and configuration versions, environment resource seeds, recorded
fixtures for unsafe external dependencies, adapter names and versions,
coverage information, redaction decisions, reviewer approval, and stable
content hashes. The schema rejects unknown fields, scans every seed and
fixture, and derives the bundle identifier from the content hash so that
identical inputs always produce identical bundles.
"""

import json
from enum import StrEnum
from hashlib import sha256
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.bundle.allowlist import (
    scan_bundle_content,
    validate_fault_script,
    validate_metadata_content,
    validate_resource_seed,
)
from app.domain.evidence.schemas import TraceSourceRef
from app.domain.simulation.faults import FaultScript
from app.domain.simulation.schemas import (
    DependencyCoverageRequirement,
    ExpectedBehavior,
    SimulationCategory,
    WorkflowContext,
)

SIMULATION_BUNDLE_SCHEMA_VERSION = "1.0.0"

BUNDLE_ID_NAMESPACE = UUID("7e0c9d8b-4f6a-4b3e-9d1c-2e8f4a6b0d5c")


class ReviewStatus(StrEnum):
    """This enum defines the states of one human review of expected behavior."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ReviewDecision(BaseModel):
    """This class stores one human review of the expected behavior."""

    model_config = ConfigDict(extra="forbid")

    status: ReviewStatus
    reviewer: str = Field(min_length=1, max_length=200)
    reviewed_at: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=1000)
    source_evidence: str | None = Field(default=None, max_length=2000)
    corrected_expected_behavior: ExpectedBehavior | None = None


class DependencyFixture(BaseModel):
    """This class stores one recorded response for an unsafe external dependency.

    The payload may contain a scalar, a list, or a nested mapping. The compiler
    validates the payload recursively before the bundle is accepted, and the
    arguments are the exact request-matching rule: a recorded adapter replays
    this fixture only when a request normalizes to the same arguments.
    """

    model_config = ConfigDict(extra="forbid")

    dependency: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$", max_length=100)
    adapter_name: str = Field(min_length=1, max_length=200)
    adapter_version: str = Field(min_length=1, max_length=50)
    tool: str = Field(min_length=1, max_length=200)
    arguments: dict[str, object] = Field(default_factory=dict)
    payload: object = None
    error_code: str | None = Field(default=None, pattern=r"^[a-z0-9_]+$")
    malformed: bool = False


class EnvironmentResourceSeed(BaseModel):
    """This class stores one seed for an ephemeral owned system."""

    model_config = ConfigDict(extra="forbid")

    resource: str = Field(pattern=r"^(customer|order|ticket|policy)$")
    adapter_name: str = Field(min_length=1, max_length=200)
    adapter_version: str = Field(min_length=1, max_length=50)
    records: tuple[dict[str, object], ...] = ()

    @model_validator(mode="after")
    def validate_records(self) -> "EnvironmentResourceSeed":
        """This method rejects incomplete or invalid records at the bundle boundary."""
        for record in self.records:
            validate_resource_seed(self.resource, record)
        return self


class ConfigurationVersions(BaseModel):
    """This class stores the workflow, model, prompt, tool, and policy versions."""

    model_config = ConfigDict(extra="forbid")

    workflow: str | None = Field(default=None, max_length=100)
    workflow_version: str | None = Field(default=None, max_length=50)
    routing_instructions_version: str | None = Field(default=None, max_length=50)
    answer_instructions_version: str | None = Field(default=None, max_length=50)
    model_provider: str | None = Field(default=None, max_length=100)
    model_name: str | None = Field(default=None, max_length=200)
    policy_version: str | None = Field(default=None, max_length=50)
    tool_versions: dict[str, str] = Field(default_factory=dict)
    configuration_version: str | None = Field(default=None, max_length=50)


class CoverageInfo(BaseModel):
    """This class stores the coverage information recorded in one bundle."""

    model_config = ConfigDict(extra="forbid")

    covered: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


class RedactionDecision(BaseModel):
    """This class stores one redaction decision for the bundle compiler."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)


class BundleRequest(BaseModel):
    """This class stores the reviewer-approved request used during simulation."""

    model_config = ConfigDict(extra="forbid")

    customer_id: UUID
    message: str = Field(min_length=1, max_length=2000)
    refund_confirmed: bool = False


class BundleScenario(BaseModel):
    """This class stores a privacy-safe projection of the source scenario."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(pattern=r"^phase2-\d{2}-[a-z0-9-]+$")
    source_schema_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    category: SimulationCategory
    request: BundleRequest
    workflow_context: WorkflowContext
    eligible_actions: tuple[str, ...]
    required_dependency_coverage: tuple[DependencyCoverageRequirement, ...] = ()


class SimulationBundle(BaseModel):
    """This class stores one portable privacy-safe simulation bundle.

    Every derived field links to source evidence, scenario state, or reviewer
    approval. The schema rejects unknown fields, scans every seed and fixture,
    requires an approved review, and derives the bundle identifier from the
    content hash so identical inputs always produce identical bundles.
    Construct bundles through the compiler: pydantic ``model_copy`` does not
    re-run validators, so a copied bundle is not re-validated.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(
        default=SIMULATION_BUNDLE_SCHEMA_VERSION,
        pattern=r"^\d+\.\d+\.\d+$",
    )
    bundle_id: UUID | None = None
    scenario: BundleScenario
    evidence_ref: TraceSourceRef | None = None
    evidence_content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    expected_behavior: ExpectedBehavior
    configuration_versions: ConfigurationVersions = Field(default_factory=ConfigurationVersions)
    resource_seeds: tuple[EnvironmentResourceSeed, ...] = ()
    dependency_fixtures: tuple[DependencyFixture, ...] = ()
    fault_script: FaultScript | None = None
    adapter_versions: dict[str, str] = Field(default_factory=dict)
    coverage: CoverageInfo = Field(default_factory=CoverageInfo)
    redaction_decisions: tuple[RedactionDecision, ...] = ()
    review: ReviewDecision
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_bundle_content(self) -> "SimulationBundle":
        """This method scans every seed, fixture, and fault script before accepting a bundle."""
        resources: dict[str, list[dict[str, object]]] = {}
        for seed in self.resource_seeds:
            resources.setdefault(seed.resource, []).extend(
                [dict(record) for record in seed.records]
            )
        scan_bundle_content(
            resources=resources,
            fixtures=[fixture.model_dump(mode="json") for fixture in self.dependency_fixtures],
            forbidden_substrings=(),
        )
        validate_fault_script(
            self.fault_script,
            allowed_tools=self._declared_tools(),
            allowed_dependencies=self._declared_dependencies(),
            forbidden_substrings=(),
        )
        validate_metadata_content(
            self.model_dump(
                mode="json",
                exclude={
                    "bundle_id",
                    "content_hash",
                    "resource_seeds",
                    "dependency_fixtures",
                    "fault_script",
                },
            )
        )
        return self

    def _declared_tools(self) -> tuple[str, ...]:
        """This method returns every tool that the scenario declares."""
        return tuple(
            sorted(
                {
                    tool
                    for requirement in self.scenario.required_dependency_coverage
                    for tool in requirement.tools
                }
            )
        )

    def _declared_dependencies(self) -> tuple[str, ...]:
        """This method returns every dependency that the scenario declares."""
        return tuple(
            sorted(
                {
                    requirement.dependency
                    for requirement in self.scenario.required_dependency_coverage
                }
            )
        )

    @model_validator(mode="after")
    def ensure_bundle_identity(self) -> "SimulationBundle":
        """This method derives the bundle identifier from the content hash.

        The hash covers every field except the derived identifier and the
        stored hash itself, so identical inputs always produce the same hash
        and the same bundle identifier. A supplied hash or identifier that
        does not match the content is rejected.
        """
        computed_hash = compute_bundle_hash(self)
        if self.content_hash is not None and self.content_hash != computed_hash:
            raise ValueError("content hash does not match the bundle content")
        if self.content_hash is None:
            self.content_hash = computed_hash
        derived_id = uuid5(BUNDLE_ID_NAMESPACE, f"bundle:{self.content_hash}")
        if self.bundle_id is not None and self.bundle_id != derived_id:
            raise ValueError("bundle id does not match the bundle content hash")
        if self.bundle_id is None:
            self.bundle_id = derived_id
        return self

    @model_validator(mode="after")
    def validate_review_state(self) -> "SimulationBundle":
        """This method rejects bundles whose review is not approved."""
        if self.review.status is not ReviewStatus.APPROVED:
            raise ValueError("only an approved review may produce an accepted bundle")
        return self


def compute_bundle_hash(bundle: SimulationBundle) -> str:
    """This function returns a stable hash for one bundle.

    The hash excludes the derived bundle identifier and the stored hash
    itself. Any content change changes the hash, so a changed bundle is a new
    version with a new identifier.
    """
    canonical = json.dumps(
        bundle.model_dump(mode="json", exclude={"bundle_id", "content_hash"}),
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode()).hexdigest()


def resources_by_type(
    bundle: SimulationBundle,
) -> dict[str, tuple[dict[str, object], ...]]:
    """This function returns resource seeds grouped by resource type."""
    grouped: dict[str, list[dict[str, object]]] = {}
    for seed in bundle.resource_seeds:
        grouped.setdefault(seed.resource, []).extend(seed.records)
    return {resource: tuple(records) for resource, records in grouped.items()}


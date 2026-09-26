"""Canonical, pre-execution experiment contracts.

The models in this module fix every input that later runners need while
deliberately omitting persistence, runner leases, result aggregation, and
execution behavior.
"""

import json
import random
from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.experiment.errors import (
    ImmutableArtifactReferenceError,
    UndeclaredCandidateDifferenceError,
)
from app.domain.suite.schemas import SuiteMemberRef, suite_members_hash
from app.domain.world.contracts import (
    CONTENT_HASH_PATTERN,
    IDENTIFIER_PATTERN,
    VERSION_PATTERN,
    WorldIdentity,
)

OCI_DIGEST_PATTERN = (
    r"^(?:[a-z0-9][a-z0-9._-]*(?::[0-9]+)?/)?"
    r"(?:[a-z0-9][a-z0-9._-]*/)*[a-z0-9][a-z0-9._-]*@sha256:[0-9a-f]{64}$"
)
GIT_COMMIT_PATTERN = r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$"
ITERATION_ID_NAMESPACE = UUID("d5dc6ec7-e058-4a64-bf55-e4dbe9c546af")
BUILTIN_SUPPORT_RUNTIME_ARTIFACT_ID = "support-runtime"
BUILTIN_SUPPORT_RUNTIME_REFERENCE = "builtin:support-runtime"


class ArtifactKind(StrEnum):
    """The supported immutable executable artifact identities."""

    OCI_IMAGE = "oci_image"
    GIT_COMMIT = "git_commit"
    BUILTIN_SUPPORT_RUNTIME = "builtin_support_runtime"


class CandidateChangeType(StrEnum):
    """The only candidate variables supported by this MVP contract."""

    MODEL = "model"
    PROMPT = "prompt"


class ConfigurationVariant(StrEnum):
    """The two configurations represented in every experiment."""

    BASELINE = "baseline"
    CANDIDATE = "candidate"


class ArtifactRef(BaseModel):
    """An immutable executable artifact reference.

    OCI references must be pinned to an image digest. A tag such as
    ``support-agent:latest`` is intentionally never accepted as a final
    executable identity. The support iteration runner can execute only the
    built-in support runtime, whose source-tree hash it attests before
    provisioning.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(pattern=IDENTIFIER_PATTERN, max_length=200)
    kind: ArtifactKind
    reference: str = Field(min_length=1, max_length=1000)
    content_hash: str = Field(pattern=CONTENT_HASH_PATTERN)

    @model_validator(mode="after")
    def validate_immutable_identity(self) -> "ArtifactRef":
        """Reject mutable image tags and non-commit Git revisions."""
        if self.kind is ArtifactKind.OCI_IMAGE:
            if not _is_oci_digest_reference(self.reference):
                raise ImmutableArtifactReferenceError(
                    "OCI artifacts must use an immutable sha256 digest reference, "
                    "not a mutable image tag"
                )
            digest = self.reference.rsplit(":", maxsplit=1)[1]
            if self.content_hash != digest:
                raise ImmutableArtifactReferenceError(
                    "OCI artifact content_hash must match the digest in its reference"
                )
        elif self.kind is ArtifactKind.GIT_COMMIT and not _is_git_commit_reference(self.reference):
            raise ImmutableArtifactReferenceError(
                "Git artifacts must use a full 40- or 64-character commit hash"
            )
        elif self.kind is ArtifactKind.BUILTIN_SUPPORT_RUNTIME and (
            self.artifact_id != BUILTIN_SUPPORT_RUNTIME_ARTIFACT_ID
            or self.reference != BUILTIN_SUPPORT_RUNTIME_REFERENCE
        ):
            raise ImmutableArtifactReferenceError(
                "the built-in support runtime must use its canonical artifact_id and reference"
            )
        return self


class ScenarioRef(BaseModel):
    """An immutable scenario version and its exact approved starting state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str = Field(pattern=IDENTIFIER_PATTERN, max_length=200)
    version: str = Field(pattern=VERSION_PATTERN, max_length=100)
    content_hash: str = Field(pattern=CONTENT_HASH_PATTERN)
    starting_state_hash: str = Field(pattern=CONTENT_HASH_PATTERN)


class PluginRef(BaseModel):
    """An immutable workflow-plugin version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plugin_id: str = Field(pattern=IDENTIFIER_PATTERN, max_length=200)
    version: str = Field(pattern=VERSION_PATTERN, max_length=100)
    content_hash: str = Field(pattern=CONTENT_HASH_PATTERN)


class EvaluatorRef(BaseModel):
    """An immutable evaluator version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluator_id: str = Field(pattern=IDENTIFIER_PATTERN, max_length=200)
    version: str = Field(pattern=VERSION_PATTERN, max_length=100)
    content_hash: str = Field(pattern=CONTENT_HASH_PATTERN)


class PromptRef(BaseModel):
    """An immutable prompt version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_id: str = Field(pattern=IDENTIFIER_PATTERN, max_length=200)
    version: str = Field(pattern=VERSION_PATTERN, max_length=100)
    content_hash: str = Field(pattern=CONTENT_HASH_PATTERN)


class ModelRef(BaseModel):
    """An exact provider model version used by an agent configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(pattern=IDENTIFIER_PATTERN, max_length=100)
    model_id: str = Field(min_length=1, max_length=200)
    version: str = Field(pattern=VERSION_PATTERN, max_length=100)


class AgentConfiguration(BaseModel):
    """One complete agent configuration, with immutable executable inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact: ArtifactRef
    model: ModelRef
    prompt: PromptRef


class ExecutionLimits(BaseModel):
    """The bounded resources requested for each future iteration.

    A runner must reject a configured ceiling unless it can enforce that
    ceiling before the relevant resource is consumed. The optional ceilings
    retain compatibility for runners that have those controls; the support
    iteration runner currently enforces only one support turn and duration.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_turns: int = Field(default=1, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=1)
    max_retries: int | None = Field(default=None, ge=0)
    max_duration_s: int = Field(ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_cost_usd: Decimal | None = Field(default=None, ge=Decimal("0"))


class InterleavingPlan(BaseModel):
    """The reproducible baseline/candidate repetition schedule inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repetitions: int = Field(ge=3, le=10_000)
    seed: int


class ExperimentContract(BaseModel):
    """A world-linked experiment with one baseline and one declared candidate.

    The final executable artifact, scenario, plugin, evaluator, and starting
    state are fixed by immutable references. A candidate may change exactly
    one of the model or prompt references and nothing else.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+$")
    experiment_id: UUID
    world: WorldIdentity
    scenario: ScenarioRef
    plugin: PluginRef
    evaluators: tuple[EvaluatorRef, ...] = Field(min_length=1)
    baseline: AgentConfiguration
    candidate: AgentConfiguration
    candidate_change: CandidateChangeType
    limits: ExecutionLimits
    interleaving: InterleavingPlan

    @model_validator(mode="after")
    def validate_candidate(self) -> "ExperimentContract":
        """Reject every candidate difference outside the declared model or prompt change."""
        evaluator_ids = [evaluator.evaluator_id for evaluator in self.evaluators]
        if len(set(evaluator_ids)) != len(evaluator_ids):
            raise ValueError("evaluators must not repeat an evaluator_id")
        validate_candidate_change(self.baseline, self.candidate, self.candidate_change)
        return self

    @property
    def content_hash(self) -> str:
        """Return the canonical content hash for this complete contract."""
        return compute_experiment_hash(self)


class SupportSuiteMemberRef(BaseModel):
    """One exact support case and its compiled immutable scenario."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: UUID
    case_version: int = Field(ge=1)
    scenario: ScenarioRef

    @model_validator(mode="after")
    def validate_case_scenario_identity(self) -> "SupportSuiteMemberRef":
        """Require the support scenario reference to name this exact case version."""
        if self.scenario.scenario_id != f"case.{self.case_id.hex}":
            raise ValueError("support suite member scenario_id must identify its case_id")
        if self.scenario.version != str(self.case_version):
            raise ValueError("support suite member scenario version must match its case_version")
        return self


class SupportSuiteRef(BaseModel):
    """An exact immutable support suite snapshot with compiled member scenarios."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    suite_id: UUID
    suite_version: int = Field(ge=1)
    members_hash: str = Field(pattern=CONTENT_HASH_PATTERN)
    members: tuple[SupportSuiteMemberRef, ...] = Field(min_length=3)

    @model_validator(mode="after")
    def validate_members(self) -> "SupportSuiteRef":
        """Require unique members and the hash stored by the suite library."""
        case_refs = tuple(
            SuiteMemberRef(case_id=member.case_id, case_version=member.case_version)
            for member in self.members
        )
        if len({member.case_id for member in self.members}) != len(self.members):
            raise ValueError("support suite members must identify distinct cases")
        if self.members_hash != suite_members_hash(case_refs):
            raise ValueError("support suite members_hash does not match its exact members")
        return self


class SupportSuiteExperimentContract(BaseModel):
    """A v2 support experiment over one immutable saved suite snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["2.0.0"] = "2.0.0"
    experiment_id: UUID
    world: WorldIdentity
    suite: SupportSuiteRef
    plugin: PluginRef
    evaluators: tuple[EvaluatorRef, ...] = Field(min_length=1)
    baseline: AgentConfiguration
    candidate: AgentConfiguration
    candidate_change: CandidateChangeType
    limits: ExecutionLimits
    interleaving: InterleavingPlan

    @model_validator(mode="after")
    def validate_candidate(self) -> "SupportSuiteExperimentContract":
        """Reject duplicate evaluators and undeclared candidate differences."""
        evaluator_ids = [evaluator.evaluator_id for evaluator in self.evaluators]
        if len(set(evaluator_ids)) != len(evaluator_ids):
            raise ValueError("evaluators must not repeat an evaluator_id")
        validate_candidate_change(self.baseline, self.candidate, self.candidate_change)
        return self

    @property
    def content_hash(self) -> str:
        """Return the canonical content hash for this complete v2 contract."""
        return compute_support_suite_experiment_hash(self)


class IterationIdentity(BaseModel):
    """The immutable identity of one scheduled baseline or candidate execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    iteration_id: UUID
    experiment_id: UUID
    scenario: ScenarioRef
    variant: ConfigurationVariant
    repetition: int = Field(ge=1)
    ordinal: int = Field(ge=1)


class IterationPlan(BaseModel):
    """The fully determined execution order derived from an experiment contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment_id: UUID
    seed: int
    iterations: tuple[IterationIdentity, ...] = Field(min_length=2)


def validate_candidate_change(
    baseline: AgentConfiguration,
    candidate: AgentConfiguration,
    declared_change: CandidateChangeType,
) -> None:
    """Ensure the candidate changes exactly its one declared configuration variable."""
    differences = {
        field
        for field in ("artifact", "model", "prompt")
        if getattr(baseline, field) != getattr(candidate, field)
    }
    allowed = declared_change.value
    undeclared = sorted(differences - {allowed})
    if undeclared:
        raise UndeclaredCandidateDifferenceError(
            f"candidate changes undeclared fields {undeclared!r}; "
            f"declared change is {allowed!r}"
        )
    if allowed not in differences:
        raise UndeclaredCandidateDifferenceError(
            f"candidate does not change its declared {allowed!r} reference"
        )


def build_iteration_plan(
    experiment: ExperimentContract | SupportSuiteExperimentContract,
) -> IterationPlan:
    """Create a reproducible, pair-interleaved order for every repetition.

    Each repetition contains one baseline and one candidate iteration. The
    seed controls which side goes first in a pair, reducing time-order bias
    while ensuring the two sides remain adjacent and comparable.
    """
    if isinstance(experiment, SupportSuiteExperimentContract):
        return _build_support_suite_iteration_plan(experiment)

    randomizer = random.Random(experiment.interleaving.seed)
    iterations: list[IterationIdentity] = []
    ordinal = 1
    baseline_first = [index % 2 == 0 for index in range(experiment.interleaving.repetitions)]
    randomizer.shuffle(baseline_first)
    for repetition, starts_with_baseline in enumerate(baseline_first, start=1):
        variants = (
            (ConfigurationVariant.BASELINE, ConfigurationVariant.CANDIDATE)
            if starts_with_baseline
            else (ConfigurationVariant.CANDIDATE, ConfigurationVariant.BASELINE)
        )
        for variant in variants:
            iterations.append(
                IterationIdentity(
                    iteration_id=uuid5(
                        ITERATION_ID_NAMESPACE,
                        (
                            f"{experiment.experiment_id}:{experiment.scenario.scenario_id}:"
                            f"{experiment.scenario.version}:{experiment.content_hash}:"
                            f"{experiment.interleaving.seed}:{variant.value}:{repetition}"
                        ),
                    ),
                    experiment_id=experiment.experiment_id,
                    scenario=experiment.scenario,
                    variant=variant,
                    repetition=repetition,
                    ordinal=ordinal,
                )
            )
            ordinal += 1
    return IterationPlan(
        experiment_id=experiment.experiment_id,
        seed=experiment.interleaving.seed,
        iterations=tuple(iterations),
    )


def _build_support_suite_iteration_plan(
    experiment: SupportSuiteExperimentContract,
) -> IterationPlan:
    """Schedule every support-suite member/repetition pair in balanced adjacent pairs."""
    randomizer = random.Random(experiment.interleaving.seed)
    pairs = [
        (member, repetition)
        for member in sorted(
            experiment.suite.members,
            key=lambda member: (str(member.case_id), member.case_version),
        )
        for repetition in range(1, experiment.interleaving.repetitions + 1)
    ]
    randomizer.shuffle(pairs)
    baseline_first = [index % 2 == 0 for index in range(len(pairs))]
    randomizer.shuffle(baseline_first)

    contract_hash = experiment.content_hash
    iterations: list[IterationIdentity] = []
    ordinal = 1
    for (member, repetition), starts_with_baseline in zip(pairs, baseline_first, strict=True):
        variants = (
            (ConfigurationVariant.BASELINE, ConfigurationVariant.CANDIDATE)
            if starts_with_baseline
            else (ConfigurationVariant.CANDIDATE, ConfigurationVariant.BASELINE)
        )
        for variant in variants:
            scenario = member.scenario
            iterations.append(
                IterationIdentity(
                    iteration_id=uuid5(
                        ITERATION_ID_NAMESPACE,
                        (
                            f"{experiment.experiment_id}:{contract_hash}:"
                            f"{experiment.suite.suite_id}:{experiment.suite.suite_version}:"
                            f"{experiment.suite.members_hash}:{member.case_id}:{member.case_version}:"
                            f"{scenario.scenario_id}:{scenario.version}:{scenario.content_hash}:"
                            f"{scenario.starting_state_hash}:{experiment.interleaving.seed}:"
                            f"{repetition}:{variant.value}"
                        ),
                    ),
                    experiment_id=experiment.experiment_id,
                    scenario=scenario,
                    variant=variant,
                    repetition=repetition,
                    ordinal=ordinal,
                )
            )
            ordinal += 1

    return IterationPlan(
        experiment_id=experiment.experiment_id,
        seed=experiment.interleaving.seed,
        iterations=tuple(iterations),
    )


def canonical_json(model: BaseModel) -> str:
    """Serialize a contract with one stable JSON representation for hashing."""
    return json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def compute_experiment_hash(experiment: ExperimentContract) -> str:
    """Return the reproducible SHA-256 hash of an experiment contract."""
    return sha256(canonical_json(experiment).encode("utf-8")).hexdigest()


def compute_support_suite_experiment_hash(experiment: SupportSuiteExperimentContract) -> str:
    """Return the reproducible SHA-256 hash of a v2 support-suite experiment."""
    return sha256(canonical_json(experiment).encode("utf-8")).hexdigest()


def parse_experiment_contract(
    payload: Mapping[str, object] | str | bytes | bytearray,
) -> ExperimentContract | SupportSuiteExperimentContract:
    """Parse an external experiment payload by its explicit contract version."""
    raw_payload: object = (
        json.loads(payload) if isinstance(payload, (str, bytes, bytearray)) else payload
    )
    if not isinstance(raw_payload, Mapping):
        raise ValueError("experiment contract payload must be an object")

    schema_version = raw_payload.get("schema_version", "1.0.0")
    if schema_version == "1.0.0":
        return ExperimentContract.model_validate(raw_payload)
    if schema_version == "2.0.0":
        return SupportSuiteExperimentContract.model_validate(raw_payload)
    raise ValueError(f"unsupported experiment contract schema_version {schema_version!r}")


def _is_oci_digest_reference(reference: str) -> bool:
    if reference.startswith("sha256:"):
        return len(reference) == len("sha256:") + 64 and all(
            character in "0123456789abcdef" for character in reference.removeprefix("sha256:")
        )
    import re

    return re.fullmatch(OCI_DIGEST_PATTERN, reference) is not None


def _is_git_commit_reference(reference: str) -> bool:
    import re

    return re.fullmatch(GIT_COMMIT_PATTERN, reference) is not None

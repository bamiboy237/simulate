"""Focused tests for the Package 0 world and experiment contracts."""

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.domain.experiment.contracts import (
    ArtifactRef,
    CandidateChangeType,
    ConfigurationVariant,
    ExperimentContract,
    ScenarioRef,
    SupportSuiteExperimentContract,
    SupportSuiteMemberRef,
    SupportSuiteRef,
    build_iteration_plan,
    compute_experiment_hash,
    parse_experiment_contract,
    validate_candidate_change,
)
from app.domain.experiment.errors import UndeclaredCandidateDifferenceError
from app.domain.experiment.events import ExperimentEvent, ExperimentEventKind
from app.domain.suite.schemas import SuiteMemberRef, suite_members_hash
from app.domain.world.contracts import (
    ActorKind,
    DependencyBinding,
    DependencyBindingMode,
    FieldProvenance,
    ProvenanceKind,
    SafeStateProjection,
    WorldActor,
    WorldContract,
    WorldTool,
)
from app.domain.world.errors import ActorToolBoundaryError

FIXTURES = Path(__file__).with_name("fixtures")


def load_fixture(name: str) -> dict[str, object]:
    """Load one concise JSON contract fixture."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def support_experiment() -> ExperimentContract:
    """Return the approved support experiment used throughout these tests."""
    return ExperimentContract.model_validate(load_fixture("valid_support_experiment.json"))


def support_world() -> WorldContract:
    """Return the declared actor and projection boundary for the support world."""
    experiment = support_experiment()
    return WorldContract(
        identity=experiment.world,
        field_provenance=(
            FieldProvenance(
                field_path="actors.scenario-agent",
                kind=ProvenanceKind.HUMAN_APPROVED,
                source_ref="approval:support-v1",
            ),
        ),
        actors=(
            WorldActor(actor_id="user-agent", kind=ActorKind.USER_AGENT),
            WorldActor(actor_id="scenario-agent", kind=ActorKind.SCENARIO_AGENT),
        ),
        tools=(
            WorldTool(tool_id="order-status", owner_actor_id="user-agent"),
            WorldTool(tool_id="refund-approve", owner_actor_id="scenario-agent"),
        ),
        dependency_bindings=(
            DependencyBinding(
                dependency_id="support-store",
                mode=DependencyBindingMode.SANDBOX,
                version="1.0.0",
                content_hash="b" * 64,
            ),
        ),
        safe_state_projections=(
            SafeStateProjection(
                projection_id="user-order-view",
                actor_id="user-agent",
                version="1.0.0",
                content_hash="a" * 64,
                field_paths=("order.status",),
            ),
        ),
    )


def test_support_experiment_round_trips_through_json() -> None:
    """A complete approved contract must retain every stable reference."""
    experiment = support_experiment()

    restored = ExperimentContract.model_validate_json(experiment.model_dump_json())

    assert restored == experiment
    assert restored.world.world_id == "support"
    assert restored.world.approval_status == "approved"
    assert restored.scenario.starting_state_hash == "3" * 64
    assert restored.candidate_change is CandidateChangeType.MODEL


def test_contracts_reject_unknown_fields() -> None:
    """Unknown data must not silently expand the public contract surface."""
    payload = load_fixture("valid_support_experiment.json")
    payload["runner_target"] = "modal"

    with pytest.raises(ValidationError, match="runner_target"):
        ExperimentContract.model_validate(payload)


def test_world_contract_round_trips_and_rejects_unknown_fields() -> None:
    """A world contract preserves its reviewed boundaries and rejects hidden data."""
    world = support_world()
    payload = world.model_dump(mode="json")
    payload["unbounded_state"] = True

    assert WorldContract.model_validate_json(world.model_dump_json()) == world
    with pytest.raises(ValidationError, match="unbounded_state"):
        WorldContract.model_validate(payload)


def test_experiment_content_hash_is_canonical_and_content_sensitive() -> None:
    """Equivalent contracts hash identically, while a changed reference does not."""
    experiment = support_experiment()
    restored = ExperimentContract.model_validate_json(experiment.model_dump_json())
    changed_payload = load_fixture("valid_support_experiment.json")
    candidate = changed_payload["candidate"]
    assert isinstance(candidate, dict)
    model = candidate["model"]
    assert isinstance(model, dict)
    model["version"] = "2026-03-01"
    changed = ExperimentContract.model_validate(changed_payload)

    assert experiment.content_hash == compute_experiment_hash(experiment)
    assert restored.content_hash == experiment.content_hash
    assert changed.content_hash != experiment.content_hash


def test_mutable_oci_tags_are_rejected_before_execution() -> None:
    """A final executable image must be pinned to its immutable digest."""
    payload = load_fixture("invalid_mutable_oci.json")

    with pytest.raises(ValidationError, match="immutable sha256 digest"):
        ArtifactRef.model_validate(payload)


def test_artifact_reference_is_immutable_after_validation() -> None:
    """Callers cannot replace a validated executable artifact identity in place."""
    artifact = support_experiment().baseline.artifact

    with pytest.raises(ValidationError, match="frozen"):
        artifact.reference = "ghcr.io/simulate/support-agent:latest"  # type: ignore[misc]


def test_undeclared_candidate_differences_are_rejected() -> None:
    """A model comparison cannot also change its final executable artifact."""
    payload = load_fixture("valid_support_experiment.json")
    invalid = load_fixture("invalid_undeclared_difference.json")
    candidate = payload["candidate"]
    changes = invalid["candidate"]
    assert isinstance(candidate, dict)
    assert isinstance(changes, dict)
    candidate.update(changes)

    with pytest.raises(ValidationError, match="undeclared fields"):
        ExperimentContract.model_validate(payload)

    baseline = support_experiment().baseline
    artifact = candidate["artifact"]
    assert isinstance(artifact, dict)
    changed_candidate = support_experiment().candidate.model_copy(
        update={"artifact": ArtifactRef.model_validate(artifact)}
    )
    with pytest.raises(UndeclaredCandidateDifferenceError, match="undeclared fields"):
        validate_candidate_change(baseline, changed_candidate, CandidateChangeType.MODEL)


def test_actor_and_projection_boundaries_are_enforced() -> None:
    """Actors receive only their own tools and safe state projections."""
    world = support_world()
    invalid_payload = load_fixture("invalid_actor_boundary.json")

    assert world.tool_ids_for("user-agent") == frozenset({"order-status"})
    assert [projection.projection_id for projection in world.projections_for("user-agent")] == [
        "user-order-view"
    ]
    with pytest.raises(ActorToolBoundaryError, match="does not own"):
        world.assert_tool_owned_by("user-agent", "refund-approve")
    with pytest.raises(ValidationError, match="undeclared owner"):
        WorldContract.model_validate(invalid_payload)


def test_iteration_plan_is_reproducible_and_pair_interleaved() -> None:
    """The schedule seed determines a stable order without separating each pair."""
    experiment = support_experiment()
    first = build_iteration_plan(experiment)
    second = build_iteration_plan(experiment)

    assert first == second
    assert len(first.iterations) == experiment.interleaving.repetitions * 2
    for index in range(0, len(first.iterations), 2):
        left, right = first.iterations[index : index + 2]
        assert left.repetition == right.repetition
        assert {left.variant.value, right.variant.value} == {"baseline", "candidate"}
    baseline_first = sum(
        first.iterations[index].variant.value == "baseline"
        for index in range(0, len(first.iterations), 2)
    )
    candidate_first = experiment.interleaving.repetitions - baseline_first
    assert abs(baseline_first - candidate_first) <= 1


def test_support_suite_plan_covers_each_case_and_keeps_v1_compatible() -> None:
    """V2 schedules every case/repetition pair without changing the v1 planner."""
    v1_experiment = support_experiment()
    members = tuple(
        SupportSuiteMemberRef(
            case_id=case_id,
            case_version=index,
            scenario=ScenarioRef(
                scenario_id=f"case.{case_id.hex}",
                version=str(index),
                content_hash=str(index) * 64,
                starting_state_hash=str(index + 3) * 64,
            ),
        )
        for index, case_id in enumerate(
            (
                UUID("00000000-0000-0000-0000-000000000001"),
                UUID("00000000-0000-0000-0000-000000000002"),
                UUID("00000000-0000-0000-0000-000000000003"),
            ),
            start=1,
        )
    )
    suite = SupportSuiteRef(
        suite_id=UUID("10000000-0000-0000-0000-000000000001"),
        suite_version=7,
        members_hash=suite_members_hash(
            tuple(
                SuiteMemberRef(case_id=member.case_id, case_version=member.case_version)
                for member in members
            )
        ),
        members=members,
    )
    v2_experiment = SupportSuiteExperimentContract(
        experiment_id=v1_experiment.experiment_id,
        world=v1_experiment.world,
        suite=suite,
        plugin=v1_experiment.plugin,
        evaluators=v1_experiment.evaluators,
        baseline=v1_experiment.baseline,
        candidate=v1_experiment.candidate,
        candidate_change=v1_experiment.candidate_change,
        limits=v1_experiment.limits,
        interleaving=v1_experiment.interleaving,
    )

    v2_plan = build_iteration_plan(v2_experiment)

    assert parse_experiment_contract(v1_experiment.model_dump(mode="json")) == v1_experiment
    assert parse_experiment_contract(v2_experiment.model_dump(mode="json")) == v2_experiment
    assert build_iteration_plan(v1_experiment) == build_iteration_plan(
        parse_experiment_contract(v1_experiment.model_dump(mode="json"))
    )
    assert len(v2_plan.iterations) == 3 * 3 * 2
    assert len({iteration.iteration_id for iteration in v2_plan.iterations}) == 18

    member_repetitions: set[tuple[str, int]] = set()
    pair_starts = []
    for index in range(0, len(v2_plan.iterations), 2):
        left, right = v2_plan.iterations[index : index + 2]
        assert left.scenario == right.scenario
        assert left.repetition == right.repetition
        assert {left.variant, right.variant} == {
            ConfigurationVariant.BASELINE,
            ConfigurationVariant.CANDIDATE,
        }
        member_repetitions.add((left.scenario.scenario_id, left.repetition))
        pair_starts.append(left.variant)
    assert len(member_repetitions) == 9
    assert abs(
        pair_starts.count(ConfigurationVariant.BASELINE)
        - pair_starts.count(ConfigurationVariant.CANDIDATE)
    ) <= 1

    changed_first_member = members[0].model_copy(
        update={
            "scenario": members[0].scenario.model_copy(update={"starting_state_hash": "f" * 64})
        }
    )
    changed_experiment = v2_experiment.model_copy(
        update={
            "suite": suite.model_copy(update={"members": (changed_first_member, *members[1:])})
        }
    )
    assert {
        iteration.iteration_id for iteration in build_iteration_plan(changed_experiment).iterations
    } != {iteration.iteration_id for iteration in v2_plan.iterations}


def test_experiment_requires_three_repetitions_per_variant() -> None:
    """An experiment needs enough observations to represent run variability."""
    payload = load_fixture("valid_support_experiment.json")
    interleaving = payload["interleaving"]
    assert isinstance(interleaving, dict)
    interleaving["repetitions"] = 2

    with pytest.raises(ValidationError, match="greater than or equal to 3"):
        ExperimentContract.model_validate(payload)


def test_experiment_events_are_typed_and_iteration_scoped() -> None:
    """Experiment events use their own vocabulary, not investigation runner events."""
    plan = build_iteration_plan(support_experiment())
    event = ExperimentEvent(
        experiment_id=plan.experiment_id,
        execution_id=uuid4(),
        sequence=1,
        kind=ExperimentEventKind.ITERATION_STARTED,
        emitted_at=datetime.now(timezone.utc),
        iteration=plan.iterations[0],
    )

    assert event.kind.value == "experiment_iteration_started"
    with pytest.raises(ValidationError, match="requires an iteration identity"):
        ExperimentEvent(
            experiment_id=plan.experiment_id,
            execution_id=uuid4(),
            sequence=2,
            kind=ExperimentEventKind.ITERATION_COMPLETED,
            emitted_at=datetime.now(timezone.utc),
        )

"""Tests for deterministic world compilation and the support adapter."""

from uuid import uuid4

import pytest

from app.domain.bundle.compiler import compile_bundle
from app.domain.bundle.schemas import SimulationBundle
from app.domain.experiment.contracts import PluginRef
from app.domain.regression.schemas import CaseSourceType, RegressionCase
from app.domain.simulation.runner import scenario_from_bundle
from app.domain.simulation.scenarios import SCENARIO_BY_ID
from app.domain.world.compiler import compile_world, with_computed_world_hash
from app.domain.world.contracts import DependencyBindingMode
from app.domain.world.errors import ActorToolBoundaryError, WorldCompilationError
from app.domain.world.support import SupportWorldPlugin, support_world_contract

SUPPORT_PLUGIN_REF = PluginRef(
    plugin_id="support",
    version="1.0.0",
    content_hash="f" * 64,
)


def support_case() -> RegressionCase:
    """Build one approved immutable support case through the legacy compiler."""
    scenario = SCENARIO_BY_ID["phase2-01-bad-prompt-policy-answer"]
    bundle = compile_bundle(
        scenario=scenario,
        approved_request_message="What does the approved refund policy allow?",
        reviewer="alice",
        reviewed_at="2026-08-08T00:00:00Z",
        reason="Approved for the experiment MVP.",
        review_status="approved",
    )
    assert bundle.content_hash is not None
    return RegressionCase(
        case_id=uuid4(),
        case_version=1,
        source_type=CaseSourceType.DESIGNED_EDGE_CASE,
        scenario_id=bundle.scenario.scenario_id,
        bundle=bundle,
        bundle_content_hash=bundle.content_hash,
        evidence_ref=bundle.evidence_ref,
        evidence_content_hash=bundle.evidence_content_hash,
        configuration_versions=bundle.configuration_versions,
        created_at="2026-08-08T00:00:00Z",
    )


def test_support_world_compiles_deterministically() -> None:
    world = support_world_contract()

    first = compile_world(world)
    second = compile_world(world)

    assert first == second
    assert first.content_hash == second.content_hash
    assert first.identity.content_hash == world.identity.content_hash
    actors = {actor.actor_id: actor for actor in first.actors}
    assert actors["scenario-agent"].tool_ids
    assert not actors["user-agent"].tool_ids
    assert first.dependency_bindings[0].mode is DependencyBindingMode.SANDBOX


def test_compiler_rejects_tampered_world_content() -> None:
    world = support_world_contract()
    changed = world.model_copy(
        update={
            "tools": world.tools
            + (world.tools[0].model_copy(update={"tool_id": "extra-tool"}),)
        }
    )

    with pytest.raises(WorldCompilationError, match="content hash"):
        compile_world(changed)


def test_compiler_rejects_dependency_modes_outside_mvp() -> None:
    world = support_world_contract()
    staging = world.dependency_bindings[0].model_copy(
        update={"mode": DependencyBindingMode.STAGING}
    )
    changed = world.model_copy(update={"dependency_bindings": (staging,)})
    changed = with_computed_world_hash(changed)

    with pytest.raises(WorldCompilationError, match="not enabled"):
        compile_world(changed)


def test_support_case_maps_without_changing_legacy_bundle() -> None:
    case = support_case()
    plugin = SupportWorldPlugin(SUPPORT_PLUGIN_REF)

    compiled = plugin.compile_case(case)
    bundle = compiled.load_bundle()

    assert bundle == case.bundle
    assert compiled.reference.content_hash == case.bundle_content_hash
    assert compiled.reference.version == str(case.case_version)
    assert compiled.reference.starting_state_hash
    assert scenario_from_bundle(bundle) == scenario_from_bundle(case.bundle)


def test_support_plugin_enforces_tool_ownership() -> None:
    case = support_case()
    payload = case.bundle.model_dump(mode="json", exclude={"bundle_id", "content_hash"})
    scenario = payload["scenario"]
    assert isinstance(scenario, dict)
    eligible_actions = scenario["eligible_actions"]
    assert isinstance(eligible_actions, list)
    eligible_actions.append("admin-delete")
    changed_bundle = SimulationBundle.model_validate(payload)
    assert changed_bundle.content_hash is not None
    changed_case = case.model_copy(
        update={
            "bundle": changed_bundle,
            "bundle_content_hash": changed_bundle.content_hash,
        }
    )

    with pytest.raises(ActorToolBoundaryError, match="does not own"):
        SupportWorldPlugin(SUPPORT_PLUGIN_REF).compile_case(changed_case)

"""Adapt approved support cases to the generic experiment world boundary."""

import json
from hashlib import sha256

from pydantic import BaseModel, ConfigDict

from app.domain.bundle.schemas import ReviewStatus, SimulationBundle
from app.domain.experiment.contracts import PluginRef, ScenarioRef
from app.domain.regression.schemas import RegressionCase
from app.domain.simulation.runner import scenario_from_bundle
from app.domain.world.compiler import CompiledWorld, compile_world, with_computed_world_hash
from app.domain.world.contracts import (
    ActorKind,
    DependencyBinding,
    DependencyBindingMode,
    FieldProvenance,
    ProvenanceKind,
    SafeStateProjection,
    WorldActor,
    WorldContract,
    WorldIdentity,
    WorldTool,
)
from app.domain.world.errors import WorldCompilationError

SUPPORT_WORLD_VERSION = "1.0.0"
SUPPORT_PLUGIN_VERSION = "1.0.0"
SUPPORT_SCENARIO_AGENT = "scenario-agent"
SUPPORT_USER_AGENT = "user-agent"
SUPPORT_TOOLS = (
    "confirm_refund",
    "escalate",
    "get_order_status",
    "get_policy",
    "propose_refund",
)


class CompiledSupportScenario(BaseModel):
    """An approved legacy support bundle with generic immutable identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: ScenarioRef
    plugin_ref: PluginRef
    bundle_json: str
    dependency_bindings: tuple[DependencyBinding, ...]

    def load_bundle(self) -> SimulationBundle:
        """Rebuild a validated bundle from the immutable compiled snapshot."""
        return SimulationBundle.model_validate_json(self.bundle_json)


def support_world_contract() -> WorldContract:
    """Build the reviewed support reference world used by the MVP."""
    placeholder_hash = "0" * 64
    contract = WorldContract(
        identity=WorldIdentity(
            world_id="support",
            version=SUPPORT_WORLD_VERSION,
            content_hash=placeholder_hash,
            approval_ref="built-in:support-world-v1",
        ),
        field_provenance=(
            FieldProvenance(
                field_path="actors",
                kind=ProvenanceKind.HUMAN_APPROVED,
                source_ref="built-in:support-world-v1",
            ),
            FieldProvenance(
                field_path="tools",
                kind=ProvenanceKind.IMPORTED,
                source_ref="app.domain.agent.service:TOOLS_BY_INTENT",
            ),
            FieldProvenance(
                field_path="dependency_bindings",
                kind=ProvenanceKind.VALIDATED,
                source_ref="app.domain.simulation.provisioner:PostgresSandboxProvisioner",
            ),
        ),
        actors=(
            WorldActor(actor_id=SUPPORT_USER_AGENT, kind=ActorKind.USER_AGENT),
            WorldActor(actor_id=SUPPORT_SCENARIO_AGENT, kind=ActorKind.SCENARIO_AGENT),
        ),
        tools=tuple(
            WorldTool(tool_id=tool_id, owner_actor_id=SUPPORT_SCENARIO_AGENT)
            for tool_id in SUPPORT_TOOLS
        ),
        dependency_bindings=(
            DependencyBinding(
                dependency_id="support.database",
                mode=DependencyBindingMode.SANDBOX,
                version="1.0.0",
                content_hash=sha256(b"support.database:sandbox:1.0.0").hexdigest(),
            ),
        ),
        safe_state_projections=(
            SafeStateProjection(
                projection_id="support-user-order-view",
                actor_id=SUPPORT_USER_AGENT,
                version="1.0.0",
                content_hash=sha256(b"orders.id,status,total_amount").hexdigest(),
                field_paths=("orders.id", "orders.status", "orders.total_amount"),
            ),
        ),
    )
    return with_computed_world_hash(contract)


class SupportWorldPlugin:
    """Compile legacy support cases without changing their stored bundle."""

    def __init__(self, plugin_ref: PluginRef) -> None:
        if plugin_ref.plugin_id != "support":
            raise WorldCompilationError("support plugin identity must use plugin_id 'support'")
        self.plugin_ref = plugin_ref
        self.world = support_world_contract()
        self.compiled_world: CompiledWorld = compile_world(self.world)

    def compile_case(self, case: RegressionCase) -> CompiledSupportScenario:
        """Map one approved immutable case into the new experiment identity."""
        bundle = SimulationBundle.model_validate_json(case.bundle.model_dump_json())
        if bundle.content_hash != case.bundle_content_hash:
            raise WorldCompilationError("support case bundle hash does not match its case")
        if bundle.review.status is not ReviewStatus.APPROVED:
            raise WorldCompilationError("support case bundle must have an approved review")

        scenario = scenario_from_bundle(bundle)
        for tool_id in scenario.eligible_actions:
            self.world.assert_tool_owned_by(SUPPORT_SCENARIO_AGENT, tool_id)

        dependency_modes = self._dependency_modes(bundle)
        declared_bindings = {
            binding.dependency_id: binding
            for binding in self.compiled_world.dependency_bindings
        }
        for dependency_id, mode in dependency_modes.items():
            declared_binding = declared_bindings.get(dependency_id)
            if declared_binding is None:
                raise WorldCompilationError(
                    f"scenario uses undeclared dependency {dependency_id!r}"
                )
            if mode is not declared_binding.mode:
                raise WorldCompilationError(
                    f"scenario dependency {dependency_id!r} does not match its world binding"
                )

        state_payload = scenario.initial_state.model_dump(mode="json")
        starting_state_hash = sha256(
            json.dumps(
                state_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return CompiledSupportScenario(
            reference=ScenarioRef(
                scenario_id=f"case.{case.case_id.hex}",
                version=str(case.case_version),
                content_hash=case.bundle_content_hash,
                starting_state_hash=starting_state_hash,
            ),
            plugin_ref=self.plugin_ref,
            bundle_json=bundle.model_dump_json(),
            dependency_bindings=tuple(
                declared_bindings[dependency_id]
                for dependency_id in sorted(dependency_modes)
            ),
        )

    @staticmethod
    def _dependency_modes(bundle: SimulationBundle) -> dict[str, DependencyBindingMode]:
        modes: dict[str, DependencyBindingMode] = {}
        for requirement in bundle.scenario.required_dependency_coverage:
            if requirement.kind == "recorded":
                mode = DependencyBindingMode.RECORDED
            elif requirement.kind == "stateful":
                mode = DependencyBindingMode.SANDBOX
            else:
                raise WorldCompilationError(
                    f"dependency {requirement.dependency!r} must select recorded or stateful mode"
                )
            previous = modes.get(requirement.dependency)
            if previous is not None and previous is not mode:
                raise WorldCompilationError(
                    f"dependency {requirement.dependency!r} has conflicting modes"
                )
            modes[requirement.dependency] = mode
        return modes

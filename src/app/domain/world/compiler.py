"""Compile reviewed world contracts into deterministic runtime definitions."""

import json
from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field

from app.domain.world.contracts import (
    ActorKind,
    DependencyBinding,
    DependencyBindingMode,
    SafeStateProjection,
    WorldContract,
    WorldIdentity,
)
from app.domain.world.errors import WorldCompilationError

WORLD_COMPILER_VERSION = "1.0.0"
MVP_DEPENDENCY_MODES = frozenset(
    {
        DependencyBindingMode.MOCK,
        DependencyBindingMode.RECORDED,
        DependencyBindingMode.SANDBOX,
    }
)


class CompiledActor(BaseModel):
    """The runtime capabilities exposed to one actor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: str
    kind: ActorKind
    tool_ids: tuple[str, ...] = ()
    projections: tuple[SafeStateProjection, ...] = ()


class CompiledWorld(BaseModel):
    """A normalized environment definition safe to hand to an experiment runner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+$")
    compiler_version: str = WORLD_COMPILER_VERSION
    identity: WorldIdentity
    actors: tuple[CompiledActor, ...]
    dependency_bindings: tuple[DependencyBinding, ...] = ()

    @property
    def content_hash(self) -> str:
        """Return the stable hash of the complete executable definition."""
        return sha256(_canonical_json(self.model_dump(mode="json")).encode()).hexdigest()


def compute_world_hash(world: WorldContract) -> str:
    """Hash reviewed world content without its self-referential identity hash."""
    payload = world.model_dump(mode="json")
    identity = payload["identity"]
    if not isinstance(identity, dict):
        raise WorldCompilationError("world identity must be an object")
    identity.pop("content_hash", None)
    return sha256(_canonical_json(payload).encode()).hexdigest()


def with_computed_world_hash(world: WorldContract) -> WorldContract:
    """Return the same reviewed contract stamped with its deterministic hash."""
    identity = world.identity.model_copy(update={"content_hash": compute_world_hash(world)})
    return world.model_copy(update={"identity": identity})


def compile_world(
    world: WorldContract,
    *,
    allowed_dependency_modes: frozenset[DependencyBindingMode] = MVP_DEPENDENCY_MODES,
) -> CompiledWorld:
    """Validate and normalize one approved world for MVP execution."""
    expected_hash = compute_world_hash(world)
    if world.identity.content_hash != expected_hash:
        raise WorldCompilationError("world content hash does not match the reviewed contract")

    unsupported = sorted(
        {
            binding.mode.value
            for binding in world.dependency_bindings
            if binding.mode not in allowed_dependency_modes
        }
    )
    if unsupported:
        raise WorldCompilationError(
            f"dependency modes are not enabled for this runner: {unsupported!r}"
        )

    actors = tuple(
        CompiledActor(
            actor_id=actor.actor_id,
            kind=actor.kind,
            tool_ids=tuple(sorted(world.tool_ids_for(actor.actor_id))),
            projections=tuple(
                sorted(
                    world.projections_for(actor.actor_id),
                    key=lambda projection: projection.projection_id,
                )
            ),
        )
        for actor in sorted(world.actors, key=lambda item: item.actor_id)
    )
    dependencies = tuple(
        sorted(world.dependency_bindings, key=lambda binding: binding.dependency_id)
    )
    return CompiledWorld(
        identity=world.identity,
        actors=actors,
        dependency_bindings=dependencies,
    )


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

"""This module defines the dependency adapter contract for simulations.

An adapter sanitizes approved captured data, seeds disposable state, handles
one simulated dependency call, reports mutations, and resets the case.
Recorded-read adapters replay approved external responses. Stateful adapters
can own memory or use an isolated database. Adapters fail closed on
unsupported tools, arguments, and state. A registry routes calls by tool
name and reports scenario coverage.
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.domain.simulation.errors import (
    MissingSimulationCoverageError,
    UnsupportedArgumentsError,
)
from app.domain.simulation.schemas import DependencyCoverageRequirement, SimulationScenario

AdapterKind = Literal["recorded", "stateful"]


@dataclass(frozen=True)
class StateMutation:
    """This class reports one accepted mutation of disposable state."""

    sequence: int
    resource: str
    resource_id: str
    field: str
    before: object | None
    after: object | None
    reason_code: str


@dataclass(frozen=True)
class DependencyCallResult:
    """This class stores the outcome of one simulated dependency call."""

    ok: bool
    payload: object | None = None
    error_code: str | None = None
    mutations: tuple[StateMutation, ...] = ()


@runtime_checkable
class DependencyAdapter(Protocol):
    """This protocol defines the minimum operations of every adapter.

    Recorded adapters sanitize approved captured data and replay it.
    Stateful adapters seed disposable state and accept mutations.
    Every adapter reports its mutations and resets to the initial case.
    """

    dependency_name: str
    kind: AdapterKind

    def supported_tools(self) -> tuple[str, ...]: ...

    def state_transitions(self) -> tuple[str, ...]: ...

    async def sanitize(self, captured: Mapping[str, object]) -> None: ...

    async def seed(self, state: object) -> None: ...

    async def call(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> DependencyCallResult: ...

    def mutations(self) -> tuple[StateMutation, ...]: ...

    async def reset(self) -> None: ...


def normalize_arguments(arguments: Mapping[str, object]) -> str:
    """This function canonicalizes tool arguments for exact matching.

    Equivalent scalars (a UUID and its string form, an int and its decimal
    form) normalize to the same key. Non-scalar values fail closed.
    """
    canonical: dict[str, str] = {}
    for key, value in arguments.items():
        if isinstance(value, UUID):
            canonical[key] = str(value)
        elif isinstance(value, bool):
            canonical[key] = "true" if value else "false"
        elif value is None:
            canonical[key] = "null"
        elif isinstance(value, (int, float, str)):
            canonical[key] = str(value)
        else:
            raise UnsupportedArgumentsError(
                dependency="<unknown>",
                tool="<unknown>",
                arguments=arguments,
            )
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"))


class CoverageItem(BaseModel):
    """This class describes one dependency that a registry can simulate."""

    model_config = ConfigDict(extra="forbid")

    dependency: str
    kind: AdapterKind
    tools: tuple[str, ...]
    state_transitions: tuple[str, ...]


SUPPORT_DATABASE_COVERAGE = CoverageItem(
    dependency="support.database",
    kind="stateful",
    tools=("get_order_status", "get_policy", "propose_refund", "confirm_refund", "escalate"),
    state_transitions=("order:delivered->refunded", "ticket:created"),
)


class CoverageReport(BaseModel):
    """This class lists supported dependencies and missing coverage."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str | None = None
    covered: tuple[CoverageItem, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """This property reports whether every requirement is covered."""
        return not self.missing


def requirement_is_covered(
    requirement: DependencyCoverageRequirement,
    items: Iterable[CoverageItem],
) -> bool:
    """This function checks whether one requirement has a matching adapter."""
    for item in items:
        if item.dependency != requirement.dependency:
            continue
        if requirement.kind != "either" and item.kind != requirement.kind:
            continue
        if requirement.tools and not set(requirement.tools).issubset(item.tools):
            continue
        return True
    return False


class SimulationAdapterRegistry:
    """This class routes simulated calls by tool name and reports coverage."""

    def __init__(self, adapters: Iterable[DependencyAdapter] = ()) -> None:
        self._adapters = tuple(adapters)
        self._by_tool: dict[str, DependencyAdapter] = {}
        for adapter in self._adapters:
            for tool in adapter.supported_tools():
                if tool in self._by_tool:
                    raise ValueError(
                        f"tools offered by more than one adapter: {tool!r}; "
                        "simulation routing must stay unambiguous"
                    )
                self._by_tool[tool] = adapter

    def adapters(self) -> tuple[DependencyAdapter, ...]:
        """This method returns the registered adapters in construction order."""
        return self._adapters

    def coverage_items(self) -> tuple[CoverageItem, ...]:
        """This method describes every registered adapter."""
        return tuple(
            CoverageItem(
                dependency=adapter.dependency_name,
                kind=adapter.kind,
                tools=adapter.supported_tools(),
                state_transitions=adapter.state_transitions(),
            )
            for adapter in self._adapters
        )

    def coverage_report(self, scenario: SimulationScenario | None = None) -> CoverageReport:
        """This method reports supported coverage and missing requirements."""
        items = self.coverage_items()
        missing: tuple[str, ...] = ()
        if scenario is not None:
            missing = tuple(
                requirement.dependency
                for requirement in scenario.required_dependency_coverage
                if not requirement_is_covered(requirement, items)
            )
        return CoverageReport(
            scenario_id=scenario.scenario_id if scenario is not None else None,
            covered=items,
            missing=missing,
        )

    async def call(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> DependencyCallResult:
        """This method routes one simulated dependency call.

        An unknown tool raises ``missing_simulation_coverage``: the candidate
        needs a dependency that no adapter simulates.
        """
        adapter = self._by_tool.get(tool_name)
        if adapter is None:
            raise MissingSimulationCoverageError(tool=tool_name)
        return await adapter.call(tool_name, arguments)

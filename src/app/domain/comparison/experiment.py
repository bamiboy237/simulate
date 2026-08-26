"""This module defines the one-variable experiment contract.

An experiment compares the deployed baseline configuration of one bundle with
exactly one candidate change. The contract permits one major dimension —
model, prompt, retrieval, tools, workflow, routing, or policy — and rejects
hidden changes, multiple changes, and missing versions. Every configuration
version and the bundle identifier are recorded on the result.
"""

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.bundle.schemas import ConfigurationVersions, SimulationBundle

EXPERIMENT_SCHEMA_VERSION = "1.0.0"


class ConfigurationChangeType(StrEnum):
    """This enum defines the one major dimension an experiment may change."""

    MODEL = "model"
    PROMPT = "prompt"
    RETRIEVAL = "retrieval"
    TOOLS = "tools"
    WORKFLOW = "workflow"
    ROUTING = "routing"
    POLICY = "policy"


# The field sets that each change dimension owns. A diff outside the declared
# dimension's set is a hidden change; a missing diff inside it is no change.
CHANGE_DIMENSIONS: dict[ConfigurationChangeType, frozenset[str]] = {
    ConfigurationChangeType.MODEL: frozenset({"model_provider", "model_name"}),
    ConfigurationChangeType.PROMPT: frozenset({"answer_instructions_version"}),
    ConfigurationChangeType.ROUTING: frozenset({"routing_instructions_version"}),
    ConfigurationChangeType.WORKFLOW: frozenset(
        {"workflow", "workflow_version", "configuration_version"}
    ),
    ConfigurationChangeType.TOOLS: frozenset({"tool_versions"}),
    ConfigurationChangeType.RETRIEVAL: frozenset({"policy_version"}),
    ConfigurationChangeType.POLICY: frozenset({"policy_version"}),
}


class ExperimentError(ValueError):
    """This class represents a rejected one-variable experiment."""


class ConfigurationSet(BaseModel):
    """This class stores one baseline and one candidate configuration.

    The validator rejects hidden changes, multiple changes, and missing
    versions. In the reference workflow, the policy document is its own
    retrieval source, so both the retrieval and the policy dimensions map to
    the policy version; the operator declares which dimension they changed.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(
        default=EXPERIMENT_SCHEMA_VERSION,
        pattern=r"^\d+\.\d+\.\d+$",
    )
    bundle_id: UUID
    change_type: ConfigurationChangeType
    baseline: ConfigurationVersions
    candidate: ConfigurationVersions

    @model_validator(mode="after")
    def validate_one_variable(self) -> "ConfigurationSet":
        """This method rejects hidden, multiple, and version-less changes."""
        validate_one_variable(self)
        return self


def _difference(baseline: ConfigurationVersions, candidate: ConfigurationVersions) -> set[str]:
    return {
        field
        for field in (
            "workflow",
            "workflow_version",
            "routing_instructions_version",
            "answer_instructions_version",
            "model_provider",
            "model_name",
            "policy_version",
            "tool_versions",
            "configuration_version",
        )
        if getattr(baseline, field) != getattr(candidate, field)
    }


def validate_one_variable(experiment: ConfigurationSet) -> None:
    """This function rejects a configuration set that changes more than one variable."""
    dimension = CHANGE_DIMENSIONS[experiment.change_type]
    difference = _difference(experiment.baseline, experiment.candidate)

    hidden = sorted(difference - dimension)
    if hidden:
        raise ExperimentError(
            f"hidden change: fields {hidden!r} differ but the declared change "
            f"dimension is {experiment.change_type.value!r}"
        )
    changed = difference & dimension
    if not changed:
        raise ExperimentError(
            f"no change in the declared dimension {experiment.change_type.value!r}: "
            "baseline and candidate are identical"
        )
    for field in sorted(changed):
        baseline_value = getattr(experiment.baseline, field)
        candidate_value = getattr(experiment.candidate, field)
        if baseline_value is None or candidate_value is None:
            raise ExperimentError(
                f"missing version: field {field!r} must be set on both baseline and candidate"
            )


def validate_baseline_matches_bundle(
    experiment: ConfigurationSet,
    bundle: SimulationBundle,
) -> None:
    """This function rejects an experiment whose baseline is not the bundle config.

    The baseline must equal the deployed configuration recorded on the
    bundle, so a comparison can never hide a baseline edit.
    """
    if experiment.bundle_id != bundle.bundle_id:
        raise ExperimentError(
            f"experiment bundle {experiment.bundle_id} does not match bundle {bundle.bundle_id}"
        )
    if experiment.baseline != bundle.configuration_versions:
        raise ExperimentError(
            "baseline configuration differs from the configuration recorded on "
            "the bundle; the baseline must be the deployed configuration"
        )


"""Public validation errors for experiment contracts and results."""


class ExperimentContractError(ValueError):
    """Base class for invalid experiment contracts."""


class ImmutableArtifactReferenceError(ExperimentContractError):
    """Raised when an executable artifact is not pinned to immutable content."""


class UndeclaredCandidateDifferenceError(ExperimentContractError):
    """Raised when a candidate changes a field outside its declared variable."""


class IterationExecutionError(ExperimentContractError):
    """A safe, expected failure from one isolated experiment iteration."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ExperimentResultIntegrityError(ExperimentContractError):
    """Raised when completed execution evidence cannot produce a publishable result."""

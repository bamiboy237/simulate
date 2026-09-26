"""Public validation errors for business-world contracts."""


class WorldContractError(ValueError):
    """Base class for invalid business-world contracts."""


class ActorToolBoundaryError(WorldContractError):
    """Raised when an actor accesses a tool or projection outside its ownership boundary."""


class WorldCompilationError(WorldContractError):
    """Raised when an approved world cannot produce a safe executable definition."""

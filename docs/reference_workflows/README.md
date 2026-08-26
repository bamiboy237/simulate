# Reference Workflows

This directory contains design documents and offline fixture modules for reference business workflows. These workflows provide test domains to validate the business world model compiler.

| Workflow | Domain | Design Document | Fixture Module | Key Invariant or Gate |
| --- | --- | --- | --- | --- |
| **Returns Resolution Agent** | E-commerce returns and refunds | [`returns-resolution-agent.md`](file:///Users/king/Desktop/simulate/docs/reference_workflows/returns-resolution-agent.md) | [`src/app/domain/reference_workflows/returns_resolution.py`](file:///Users/king/Desktop/simulate/src/app/domain/reference_workflows/returns_resolution.py) | Refund confirmation gate |
| **Onboarding Coordinator Agent** | Human resources onboarding | [`onboarding-coordinator-agent.md`](file:///Users/king/Desktop/simulate/docs/reference_workflows/onboarding-coordinator-agent.md) | [`src/app/domain/reference_workflows/onboarding.py`](file:///Users/king/Desktop/simulate/src/app/domain/reference_workflows/onboarding.py) | Checklist selection source |
| **Dispute Resolution Agent** | Banking transaction disputes | [`dispute-resolution-agent.md`](file:///Users/king/Desktop/simulate/docs/reference_workflows/dispute-resolution-agent.md) | [`src/app/domain/reference_workflows/disputes.py`](file:///Users/king/Desktop/simulate/src/app/domain/reference_workflows/disputes.py) | Minimum evidence requirements |

For additional reference workflow fixtures and implementations, see [`src/app/domain/reference_workflows/README.md`](file:///Users/king/Desktop/simulate/src/app/domain/reference_workflows/README.md).

All fixture modules use the Python standard library, execute deterministically, and run offline without network access.

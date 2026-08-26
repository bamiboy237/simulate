# Reference Workflow: E-Commerce Returns Resolution Agent

**Status:** Reference workflow design specification.
**Date:** August 12, 2026.
**Fixture module:** [`src/app/domain/reference_workflows/returns_resolution.py`](file:///Users/king/Desktop/simulate/src/app/domain/reference_workflows/returns_resolution.py).

## 1. Domain and purpose

The workflow implements an **e-commerce returns resolution agent**. It processes customer returns, refunds, and exchanges after an order is delivered. The agent reads a return request, verifies order ownership, checks return policy rules, evaluates eligibility, and approves the return, denies it, or escalates to a human operator.

The worst-case failure in this domain is an unauthorized or unverified refund. The agent never moves funds beyond a strict cap (`REFUND_CAP_USD = 500.00`). Transactions exceeding the cap, outside the return window, or marked as suspicious escalate to a human reviewer.

## 2. Stateful system

The agent reads and modifies a disposable returns database. The simulation environment mocks external payment and shipping systems.

| Entity | Key fields | Status values |
| --- | --- | --- |
| `Customer` | id, name, email | (none) |
| `Order` | id, customer_id, item_category, total_amount, delivered_at | `delivered`, `returned` |
| `ReturnRequest` | id, customer_id, order_id, reason_code, requested_refund_amount | `submitted`, `approved`, `label_issued`, `refund_pending`, `refunded`, `exchanged`, `denied`, `escalated` |
| `RefundProposal` | proposal_id, return_request_id, amount, policy_version | `proposed`, `confirmed`, `executed`, `blocked` |
| `ReturnPolicyDocument` | slug, version, title, content, content_hash | Versioned; newest version is active |

### Canonical transitions

The following state transitions represent valid system operations:

```text
return_request: submitted -> approved -> label_issued -> refund_pending -> refunded
                                                               \-> exchanged
                submitted -> denied | escalated
order: delivered -> returned
refund_proposal: proposed -> confirmed -> executed
                 proposed -> blocked
```

## 3. Tool classification

### Safe tools (Read-only)

| Tool | Purpose |
| --- | --- |
| `get_order` | Read an order record by ID. |
| `verify_order_ownership` | Confirm the requester owns the order. |
| `get_return_policy` | Retrieve the active policy document by slug. |
| `check_return_eligibility` | Evaluate return window, item condition, and proof against policy. |
| `get_return_status` | Return order status and tracking details. |
| `detect_abuse` | Read the abuse-risk score for the requester and order. |

### Sensitive tools (Write or external side effects)

| Tool | Effect | Guardrail |
| --- | --- | --- |
| `approve_return` | Transition status: `submitted -> approved`. | Requires verified ownership and eligibility. |
| `issue_return_label` | Calls external shipping API for carrier return label. | Recorded adapter; requires approved return. |
| `process_refund` | Executes monetary refund in payment gateway. | Hard cap of $500.00; requires confirmation gate. |
| `issue_store_credit` | Creates store credit account balance. | Alternative offered to customer. |
| `escalate_to_human` | Creates a customer support ticket. | Triggers on over-cap amounts, expired windows, or abuse flags. |
| `log_decision` | Writes an immutable audit record of the decision. | Always recorded. |

Verify order ownership before displaying order details or approving returns.

## 4. Comparison variable

**Variable:** `refund_confirmation_gate`

| Configuration | Behavior |
| --- | --- |
| **Baseline** | `process_refund` executes as soon as eligibility passes and the amount is within the refund cap. |
| **Candidate** | `process_refund` requires an explicit customer confirmation turn. The agent proposes the refund amount, the customer confirms, and then the refund executes. Unconfirmed proposals block execution. |

All other parameters remain fixed (identical state, tools, model, and resource budgets).

**Expected difference:** The baseline resolves eligible returns with fewer turns and lower latency, but issues refunds before carrier item receipt. The candidate introduces one additional turn and model call, preventing unconfirmed refund execution.

## 5. Mapping to simulation contracts

### Evidence contract (`TraceEvidence`)

- **Source:** Production returns trace from LangSmith or Braintrust mapped to `TraceSourceRef`.
- **Event kinds:** Reuses standard event types (`turn`, `routing`, `answer`, `model`, `tool`, `retrieval`, `database`, `policy`, `confirmation`, `escalation`, `retry`, `step`).
- **Outcomes:** Reuses `completed`, `blocked`, `escalated`, `failed`.

### Scenario contract (`SimulationScenario`)

- **Request:** Maps actor ID, user message, and trusted `refund_confirmed` flag. The agent cannot set the confirmation flag directly.
- **Initial state:** Includes `orders`, `policies`, `return_requests`, and `refund_proposals`.
- **Expected behavior:** Validates expected outcome, reason codes, policy version, and state transitions.

### Bundle contract (`SimulationBundle`)

- **Resource seeds:** Provides fixture seeds for customer, order, return request, and policy records.
- **Dependency fixtures:** Replays payment and shipping gateway responses.
- **Fault scripts:** Injects timeouts or invalid responses to test error recovery.

## 6. Fixture module

The fixture module [`src/app/domain/reference_workflows/returns_resolution.py`](file:///Users/king/Desktop/simulate/src/app/domain/reference_workflows/returns_resolution.py) is self-contained. It uses only the Python standard library.

The module provides:

- Deterministic UUIDs generated from stable seeds.
- Two versioned `ReturnPolicyDocument` records with content hashes (current version with 14-day window and $500 cap; legacy version with 30-day window and $1000 cap).
- Customer, order, and return request fixture records.
- Definitions for `SAFE_TOOLS`, `SENSITIVE_TOOLS`, and `STATE_TRANSITIONS`.
- The `CONFIRMATION_GATE` comparison variable.
- Three test scenarios: `returns-01-refund-before-return`, `returns-02-stale-return-window`, and `returns-03-ownership-mismatch`.

## 7. Scope boundaries

The simulation environment mocks external payment processing, shipping label creation, inventory restocking, and fraud model scoring. The simulator does not execute live financial transactions or carrier API calls.

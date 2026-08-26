# Reference Workflow: Banking Dispute Resolution Agent

**Status:** Reference workflow design specification.
**Date:** August 12, 2026.
**Fixture module:** [`src/app/domain/reference_workflows/disputes.py`](file:///Users/king/Desktop/simulate/src/app/domain/reference_workflows/disputes.py).

## 1. Domain and purpose

The workflow implements a **banking dispute resolution agent**. It processes disputed transactions from intake to resolution: classifying claims (unauthorized charges, merchant errors, processing failures, friendly fraud), collecting evidence across banking systems, enforcing regulatory deadlines (such as Regulation E and PSD2 timelines), issuing provisional credit when eligible, and escalating complex cases to human reviewers.

Regulation E requires claim acknowledgment within five business days and provisional credit within ten business days for unauthorized transactions. PSD2 requires immediate refunding for unauthorized electronic payments unless fraud is suspected.

## 2. Stateful system

The agent reads and modifies a disposable banking database. External fraud scoring and identity authentication systems are simulated.

| Entity | Key fields | Status values |
| --- | --- | --- |
| `Customer` | id, name, email | (none) |
| `Account` | id, customer_id, status | `active`, `frozen` |
| `Transaction` | id, account_id, merchant, amount, posted_at, auth_method | (none) |
| `FraudSignal` | transaction_id, signal_name, score, source | (external, recorded) |
| `DisputeCase` | account_id, transaction_id, category, evidence_sources, reg_e_ack_deadline, ack_sent_at | `intake`, `evidence_gathering`, `decision_pending`, `provisional_credit_issued`, `resolved`, `denied`, `escalated` |
| `RegulationDocument` | slug, version, content, content_hash | Versioned; newest version is active |

### Canonical transitions

The following state transitions represent valid system operations:

```text
dispute_case: intake -> evidence_gathering -> decision_pending -> provisional_credit_issued -> resolved
                                                        \-> denied
              evidence_gathering -> escalated
```

### Timeline and policy rules

The active regulation policy requires:

- Acknowledge disputes within 5 business days of intake.
- Issue provisional credit within 1 business day of acknowledgment for unauthorized claims, unless fraud signals indicate cardholder participation.
- Require at least three independent evidence sources before rendering a final decision.
- Escalate cases automatically if regulatory deadlines are approaching.

## 3. Tool classification

### Safe tools (Read-only)

| Tool | Purpose |
| --- | --- |
| `get_account` | Read account status and balance. |
| `get_transaction` | Read transaction records and authorization metadata. |
| `get_fraud_signals` | Read fraud scoring model outputs. |
| `get_auth_events` | Read authentication and device event history. |
| `get_dispute_timeline_regulation` | Retrieve the active regulatory policy document. |
| `get_case_status` | Report dispute case progress and regulatory deadlines. |

### Sensitive tools (Write or external side effects)

| Tool | Effect | Guardrail |
| --- | --- | --- |
| `acknowledge_dispute` | Records regulatory acknowledgment timestamp. | Must execute within 5 business days of intake. |
| `open_dispute_case` | Creates a dispute record in `intake` state. | Requires initial claim classification. |
| `issue_provisional_credit` | Credits funds to customer account. | Requires completed acknowledgment; blocked if fraud signals indicate cardholder involvement. |
| `deny_dispute` | Closes case as denied. | Requires completed evidence review with recorded reason codes. |
| `file_regulatory_notice` | Submits formal regulatory notice. | Recorded adapter; executed on resolution or escalation. |
| `escalate_to_reviewer` | Assigns case to human dispute specialist. | Triggers on deadline risks, friendly fraud indicators, or incomplete evidence. |

Classify the claim, acknowledge the dispute, gather at least three evidence sources, and then render a decision. Funds transfer only occurs through `issue_provisional_credit`.

## 4. Comparison variable

**Variable:** `evidence_source_minimum`

| Configuration | Behavior |
| --- | --- |
| **Baseline** | The agent recommends a decision after consulting a single evidence source (the ledger transaction record). |
| **Candidate** | The agent requires at least three independent evidence sources (transaction records, fraud signals, authentication events) before recommending a decision. |

All other parameters remain fixed (identical state, tools, model, and resource budgets).

**Expected difference:** On scenario `disputes-01-credit-on-single-source`, the baseline issues provisional credit immediately, while the candidate holds credit until all three evidence sources arrive. On scenario `disputes-03-friendly-fraud-credited`, the candidate identifies cardholder-present signals and denies the claim, preventing erroneous credit payout.

## 5. Mapping to simulation contracts

### Evidence contract (`TraceEvidence`)

- **Source:** Production dispute trace from LangSmith or Braintrust mapped to `TraceSourceRef`.
- **Event kinds:** Reuses standard event types (`turn`, `routing`, `answer`, `model`, `tool`, `retrieval`, `database`, `policy`, `confirmation`, `escalation`, `retry`, `step`).
- **Outcomes:** Reuses `completed`, `blocked`, `escalated`, `failed`.

### Scenario contract (`SimulationScenario`)

- **Request:** Maps customer ID, claim description, and trusted unauthorized confirmation flag. The agent cannot set the confirmation flag directly.
- **Initial state:** Includes `accounts`, `transactions`, `fraud_signals`, and `cases`.
- **Expected behavior:** Validates expected outcome, reason codes, regulatory deadlines, and state transitions.

### Bundle contract (`SimulationBundle`)

- **Resource seeds:** Provides fixture seeds for account, transaction, fraud signal, and dispute case records.
- **Dependency fixtures:** Replays fraud model, authentication service, and regulatory filing responses.
- **Fault scripts:** Injects timeouts or clock delays to test deadline escalation logic.

## 6. Fixture module

The fixture module [`src/app/domain/reference_workflows/disputes.py`](file:///Users/king/Desktop/simulate/src/app/domain/reference_workflows/disputes.py) is self-contained. It uses only the Python standard library.

The module provides:

- Deterministic UUIDs generated from stable seeds.
- Two versioned `RegulationDocument` records with content hashes (current version requiring 3 evidence sources and 5-day acknowledgment; legacy version with 10-day window and single-source rules).
- Customer, account, transaction, fraud signal, and dispute case fixture records.
- Definitions for `SAFE_TOOLS`, `SENSITIVE_TOOLS`, and `STATE_TRANSITIONS`.
- The `EVIDENCE_COMPLETENESS` comparison variable.
- Three test scenarios: `disputes-01-credit-on-single-source`, `disputes-02-reg-e-ack-timeout`, and `disputes-03-friendly-fraud-credited`.

## 7. Scope boundaries

The simulation environment mocks core banking payment rails, live fraud model endpoints, and external regulatory filings. The simulator does not execute live financial transactions or external regulatory transmissions.

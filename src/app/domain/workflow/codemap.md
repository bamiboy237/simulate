# `src/app/domain/workflow/`

## Responsibility

Implements the checkpointed, manually resumable support workflow for order status, policy
answers, refunds, and escalation. It keeps reads separate from controlled mutations and binds
refund confirmation to the original actor and request.

## Design

- `SupportState` is the LangGraph checkpoint state: identity, request, route, evidence, proposal,
  confirmation, response, errors, escalation, status, and transition transcript.
- Nodes depend on small protocols in `WorkflowNodeDependencies`. Defaults adapt
  `SupportService`; alternate implementations can be injected.
- `graph.py` is an explicit state machine. Read/model nodes use bounded retries; unsafe,
  exhausted, missing, or forbidden paths create typed escalation.
- Refunds use propose/interruption/resume/execute. A proposal does not mutate the order.
- `WorkflowService` serializes resumes per workflow and owns lookup, expiry, interruption state,
  and stable responses.
- `WorkflowService` passes the provided checkpointer, or a new `InMemorySaver`, directly to the
  compiled graph. The graph owns checkpoint use after construction.

## Flow

1. `start()` creates workflow/run IDs, sets expiry, and invokes the graph with its workflow ID as
   the LangGraph thread ID.
2. `route` selects order status, policy, refund, or escalation. Retrieval nodes gather
   authorization-checked evidence before response or action.
3. Order status and policy go to `respond`. Refund gets order and policy, creates a
   `ProposedAction`, then `confirmation` interrupts and checkpoints.
4. `confirm()` or `reject()` locks the workflow, verifies existence, expiry, actor ID, request
   ID, and waiting state, then resumes with a LangGraph `Command`.
5. Confirm executes through `SupportService`; reject returns blocked without mutation.
   Escalation creates a support ticket and returns an escalated response.
6. `inspect()` returns checkpoint state, interruption status, and the transition transcript.

## Key Files

- `models.py`: Request, evidence, proposal, confirmation, state, and response contracts.
- `nodes.py`: Routing, retrieval, proposal, confirmation, execution, response, rejection, and
  escalation nodes.
- `graph.py`: LangGraph compilation and conditional routing.
- `service.py`: Default adapters and start/inspect/confirm/reject lifecycle.
- `errors.py`: Lookup, expiry, resume, transient, and unsafe errors.

## Integration

- `app.api.workflow_router` exposes start, inspect, confirm, and reject endpoints.
- Default adapters delegate authorization, policy lookup, refunds, and tickets to the support
  domain; routing/response contracts come from the agent domain.
- Uses LangGraph checkpoints (`InMemorySaver` by default) and shared telemetry. It runs only
  when an API caller starts or resumes a workflow; it is not a background job.

# Phase 8 MVP implementation plan

This document tells you exactly what to build for Phase 8 of Simulate. Work through the five
milestones in order. Each milestone closes one Linear issue and ends with a checklist. Do not
start a milestone until the previous checklist passes.

We run this work as one agent session in normal Antigravity mode, not `/teamwork-preview`. The
milestones are sequential: each one builds on contracts defined in the one before it, so a team
of parallel agents would spend more effort coordinating than coding. Teamwork preview is also a
research preview with documented quota problems. Use ordinary subagents for bounded research if
that helps you, but you own every result you ship.

## Sources of truth

When documents disagree, the higher item wins:

1. `BUILD_ROADMAP.md` defines phase scope. We build only the section named "Phase 8 MVP -
   Modal investigation runner".
2. `ARCHITECTURE.md` explains how the system fits together. Sections 2, 5, and 6 matter most.
3. Linear holds the issues: parent THE-18, children THE-19 through THE-23.
4. This document turns those into concrete files, interfaces, and checks.

If this document contradicts the roadmap, stop and ask. Do not improvise scope.

## What we are building

A support trace comes in. A user starts an investigation with one command. We rent a disposable
Linux container on Modal, boot a stripped-down copy of the customer's business process inside
it, and set Prime Agent loose to reproduce and study the failure. The user watches events
stream past, types messages that steer the agent mid-run, and can walk away at any moment.
When Prime Agent finishes, it writes a short summary. We store everything in PostgreSQL and
destroy the container.

The container is disposable. The evidence is permanent. That single idea drives most design
decisions below.

Terms, defined once:

- **Control plane**: the FastAPI app plus PostgreSQL. It always runs and owns all data.
- **Sandbox**: one Modal container, created for one investigation, deleted afterwards.
- **Prime Agent**: the investigation harness from Prime Intellect. It reads a task brief,
  calls tools, and writes the summary. It speaks a line-based command protocol over standard
  input and output.
- **Bridge**: a small Python process we write. It runs inside the sandbox and relays chat
  messages inward and progress events outward.
- **World Gateway**: an HTTP service inside the sandbox. It serves mock business services and
  fixture data. Every Prime Agent tool call goes through it. Nothing else answers.
- **Environment slice**: the smallest piece of the business world one scenario needs. A refund
  investigation boots the refund policy and a fake payment API, not the whole company.

## Rules you work under

`AGENTS.md` governs repo conventions: 4-space indent, type annotations everywhere, 100
character lines, `snake_case` modules and functions, `PascalCase` classes. Run these three
commands before you claim any piece of work is done:

```bash
uv run ruff check .
uv run mypy src
uv run pytest tests/unit -q
```

Integration tests need PostgreSQL. Follow the skip patterns already used in
`tests/integration/`.

Business logic lives in domain services. Route handlers in `src/app/api/` stay thin: they
parse input, call one service method, and return. Authorization checks and state transitions
never live in prompts and never live in handlers.

Secrets handling is strict. Model API keys enter the sandbox as Modal secret environment
variables and nowhere else. Never log them. Never store them in database rows. Never commit
them. When you add a new configuration key, add it to `.env.example` with an empty value.

Do not build any of the following, even if they seem adjacent: alert intake webhooks,
baseline-versus-candidate experiments, model-swap test arms, the world compiler, a web UI,
multi-user authentication, Hetzner or BYOVM providers, renames of legacy terms such as
`failure` and `regression`. Do not modify `FORGE_TEAM.md` or `graphify-out/`; those are user
files, not project files.

Commit each milestone on its own branch with an imperative commit message. Suggested branches:
`feat/the19-runner-contracts`, `feat/the20-modal-runner`, `feat/the21-runner-bridge`,
`feat/the22-investigation-service`, `feat/the23-end-to-end`.

Unit tests stay fast and offline. Mock Modal, mock Prime Agent, mock the network. Extend the
test doubles in `tests/fakes/`. A unit test must never require Modal credentials or model API
keys. Live checks skip cleanly when credentials are absent; the agent live-model tests show the
existing pattern.

## How to escalate

You are registered on the local Agent Mesh as `little pickle`. The planning session is
`big pickle`. We talk over mesh messages.

Send me a message as soon as any of these is true:

- One approach has failed twice, or one error has eaten twenty minutes.
- Something real differs from this plan: the Prime Agent protocol, the Modal SDK, or a repo
  helper this document told you to reuse.
- You need to change a public contract in a way this plan does not describe.
- A dependency is missing and no local substitute exists.

Use this format:

```text
[BLOCKED] THE-1x: one-line problem statement
Where: file:line, or the command you ran
Error: the exact error text, trimmed
Tried: approach A, result; approach B, result
Hypothesis: your best current guess
Meanwhile: which independent task you will continue with while waiting
```

Keep the message under 25 lines. Never paste whole logs.

If I do not answer within ten minutes, I am probably offline between sessions. Append the same
block to Appendix A at the end of this file, then continue with the task you named under
"Meanwhile". Desktop notifications reach the human either way, so nothing gets lost.

When a milestone's checklist fully passes, send `[DONE] THE-1x <one line of proof>`. No other
chatter. When you face a contract question with two defensible answers, send
`[DECISION] <question> | Options: A ..., B ... | Recommendation: <letter>, reason`. If the
choice is reversible and you are otherwise blocked, proceed with your recommendation and say so.

## Milestone 1: Define the runner contracts (THE-19)

Everything downstream depends on a shared vocabulary, so we define it first and keep Modal out
of it entirely. After this milestone, no file in `src/app/domain/runner/` may import `modal`.
That constraint is what keeps later providers (full VMs, customer machines) cheap to add.

One deliberate omission: the runner interface has no methods for streaming events or sending
chat. Events flow from bridge to control plane over plain HTTP, and chat flows the reverse way
the same route. That choice keeps the runner interface tiny, makes reconnect logic a control
plane concern instead of a provider concern, and means swapping providers never touches the
chat path.

Build these in order:

1. Create `src/app/domain/runner/__init__.py` and `src/app/domain/runner/schemas.py`. Define
   Pydantic models:

   ```python
   class SandboxSpec(BaseModel):
       spec_version: str
       task_brief: str                      # markdown brief for Prime Agent
       environment_slice: EnvironmentSliceRef
       investigator: AgentArtifactRef       # always a prime_profile in the MVP
       tested_agent: AgentArtifactRef | None
       env: dict[str, str]                  # non-secret vars
       secrets: dict[str, SecretStr]        # injected only, excluded from dumps
       callback_base_url: str               # control plane URL the bridge calls home to
       resource_limits: ResourceLimits

   class EnvironmentSliceRef(BaseModel):
       world_id: str
       world_version: str
       slice_name: str
       fixture_bundle_ref: str              # points into domain/bundle output
       provenance: Literal["observed", "generated", "human-approved"]

   class AgentArtifactRef(BaseModel):
       kind: Literal["oci_digest", "prime_profile"]
       digest_or_profile: str               # sha256:..., or profile name@version
       entrypoint: str | None = None

   class ResourceLimits(BaseModel):
       timeout_s: int = 3600
       cpus: int = 1
       memory_mib: int = 2048

   class SandboxHandle(BaseModel):
       sandbox_id: str
       provider: str                        # "modal", "fake", ...
       created_at: datetime

   class EventType(str, Enum):
       investigation_started, agent_ready, thought, tool_call, tool_result
       message, state_diff, evaluator_verdict, chat_from_user, chat_ack
       summary_submitted, warning, error, heartbeat, investigation_finished

   class RunnerEvent(BaseModel):
       investigation_id: UUID
       seq: int                             # assigned by the bridge, monotonic per run
       type: EventType
       payload: dict[str, Any]
       emitted_at: datetime

   class ChatEnvelope(BaseModel):
       message_id: UUID
       body: str
       mode: Literal["prompt", "steer", "follow_up"]
   ```

   The `secrets` field uses `SecretStr` so values never appear in logs, reprs, or
   `model_dump()` output. Write one test that proves that.

2. Create `src/app/domain/runner/base.py` with the runner interface:

   ```python
   class CloudRunner(Protocol):
       def create_sandbox(self, spec: SandboxSpec) -> SandboxHandle: ...
       def get_status(self, handle: SandboxHandle) -> RunnerStatus: ...
       def terminate(self, handle: SandboxHandle) -> None: ...
   ```

3. Create `FakeRunner` in `tests/fakes/runners.py`. It records every spec it receives, returns
   canned handles, and lets tests drive a sandbox into terminal states. The fake is the
   backbone of offline testing in milestones 4 and 5, so give it a small scripted-behavior API.

4. Create `tests/unit/runner/test_contracts.py`. Cover: schema round-trips, secret redaction,
   event ordering by `seq`, FakeRunner behavior.

Done when: all three commands pass, `grep -r "import modal" src/` finds nothing, and another
module can drive `FakeRunner` without importing anything from Modal.

## Milestone 2: Add the Modal runner (THE-20)

Modal rents us Linux containers through its Python SDK. A sandbox created with detached mode
stays alive after our process moves on, which is exactly what an investigation needs: users
close laptops, the work continues. Our handle into that world is just Modal's sandbox id.

Keep every Modal SDK call inside one wrapper module, `src/app/domain/runner/_modal_client.py`.
Tests then mock one small module instead of patching the SDK across the codebase. If a future
SDK version renames things, you fix one file.

Build these in order:

1. Extend `src/app/config.py`: `modal_enabled: bool = False`, `modal_app_name: str`,
   `modal_timeout_s: int`. When `modal_enabled` is false, the app resolves `FakeRunner`
   instead of `ModalRunner`; that switch is how the whole test suite stays offline.
2. Create `_modal_client.py` with functions: build image, create sandbox, poll status, kill.
   The image recipe is a versioned constant in this module: Debian slim plus Python plus our
   bridge package plus the Prime Agent install step. Pin versions.
3. Create `modal_runner.py` implementing `CloudRunner`. In `create_sandbox`: turn
   `spec.secrets` into a Modal secret, inject `CONTROL_PLANE_CALLBACK_URL`, `BRIDGE_TOKEN`, and
   slice variables, launch detached, map the result to `SandboxHandle`. Verify the exact SDK
   call names against the pinned SDK version before writing them; do not trust memory.
4. Write `tests/unit/runner/test_modal_runner.py` against a mocked `_modal_client`. Assert:
   detached flag set, secrets injected, secret values absent from logs and spec dumps,
   terminate safe to call twice.
5. Add `scripts/modal_smoke.py`, a manual script that creates a hello-world sandbox, runs one
   command, prints status, and terminates. A human with Modal credentials runs it once to prove
   the real path.
6. Add `tests/integration/runner/test_modal_live.py`, gated on `settings.modal_enabled`, so it
   skips cleanly everywhere else.

Done when: unit suite green with Modal fully mocked, smoke script exists and runs for a human
with credentials, live test skips locally.

## Milestone 3: Build the bridge, the Prime Agent link, and the gateway (THE-21)

This is the code that runs inside the sandbox. Three pieces live in a new package,
`src/app/domain/agent_runner/`: the bridge process, the adapter that speaks Prime Agent's
protocol, and the World Gateway that serves mocks and fixtures.

Start with the one known unknown. We have not yet verified how Prime Agent installs or the
exact field names of its command protocol. Do this first in the milestone: check the Prime
Agent repository and docs for the install method and the `--mode rpc` message schema. Put all
of that knowledge in one module, `src/app/domain/agent_runner/prime_rpc.py`, so any schema
surprise costs one file, not three. If verification takes more than about thirty minutes, send
`[BLOCKED]` with what you found.

The bridge loop, in plain terms:

1. Ask the control plane for new chat messages with a request the server holds open up to 30
   seconds (`GET /internal/inbox?cursor=...`). When it fails, wait longer each time, capped at
   30 seconds.
2. For each message, translate its mode into Prime Agent's protocol: `prompt` starts fresh
   input, `steer` redirects the agent mid-run, `follow_up` queues for after the current run.
   If a `steer` arrives while no run is active, downgrade it to `follow_up` instead of losing
   it.
3. Read Prime Agent's output lines as they appear. Batch them: at most 50 events or every 500
   milliseconds, whichever comes first, posted to `POST /internal/events` with the bridge
   token as bearer auth.
4. Send a `heartbeat` event every 15 seconds. The control plane uses these to detect a dead
   sandbox.
5. When an event carries `summary_submitted`, post it once and exit cleanly. The control plane
   terminates the sandbox when it sees the summary.
6. On crash, retry three times, then emit an `error` event and exit nonzero. A silent death is
   the one unacceptable outcome; the control plane must always learn why the run ended.

Build the gateway next, in `src/app/domain/agent_runner/gateway.py`. It exposes exactly nine
routes under `/tools/{name}`, matching the nine skills: `trace.read`, `world.describe`,
`environment.status`, `scenario.run`, `state.inspect`, `state.diff`, `evidence.read`,
`proposal.create`, `summary.submit`. Every route requires the bridge token. Any other path
returns 404. The gateway binds to localhost inside the sandbox only; nothing outside the
container reaches it directly.

Behind those tool routes sit the mock business services: the support domain from
`src/app/domain/support/` backed by PostgreSQL installed inside the sandbox image, seeded from
the fixture bundle named by `EnvironmentSliceRef`. The sandbox database starts empty and gets
seeded at boot; we do not reuse the shared-database rollback tricks from
`PostgresSupportSandbox`, because a per-investigation container has nobody to leak state to.
If the in-image PostgreSQL proves flaky on Modal twice, switch to SQLite behind the same
repository interface, but send a `[DECISION]` message with your measurements first.

Write unit tests that prove the whole cycle offline: a fake control-plane HTTP server plus a
scripted fake `prime_rpc` driving `bridge.py`. Cover the steer downgrade rule, batching
limits, duplicate suppression, the crash-to-error-event path, and summary detection. Test the
gateway separately: unknown tool returns 404, wrong token rejected, each happy-path tool works.

Done when: the full loop runs green in unit tests with zero network access, all protocol
knowledge sits in `prime_rpc.py`, and you sent the `[DONE]` ping.

## Milestone 4: Build the investigation service and the event stream (THE-22)

Now the control plane side: the database records, the domain service that owns them, and the
HTTP surface clients and the bridge both use. PostgreSQL is the source of truth here. The
disconnect guarantee falls out of one design rule: events are persisted before they are
streamed. A client that vanishes mid-run misses nothing, because replay reads the database.

Create the Alembic migration following existing naming style:

```sql
CREATE TABLE investigations (
    id UUID PRIMARY KEY,
    status TEXT NOT NULL,               -- pending|provisioning|running|completed|failed|cancelled
    trace_ref JSONB NOT NULL,
    world_ref JSONB NOT NULL,
    slice_ref JSONB NOT NULL,
    investigator_ref JSONB NOT NULL,
    tested_agent_ref JSONB,
    task_brief TEXT NOT NULL,
    bridge_token_hash TEXT NOT NULL,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

CREATE TABLE investigation_events (
    id BIGSERIAL PRIMARY KEY,
    investigation_id UUID REFERENCES investigations(id),
    seq INT NOT NULL,
    type TEXT NOT NULL,
    payload JSONB NOT NULL,
    emitted_at TIMESTAMPTZ NOT NULL,
    UNIQUE (investigation_id, seq)      -- this constraint makes retries harmless
);

CREATE TABLE investigation_messages (
    id BIGSERIAL PRIMARY KEY,
    investigation_id UUID REFERENCES investigations(id),
    sender TEXT NOT NULL,               -- user|agent
    body TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE investigation_summaries (
    id BIGSERIAL PRIMARY KEY,
    investigation_id UUID REFERENCES investigations(id) UNIQUE,
    findings TEXT NOT NULL,
    next_step TEXT NOT NULL,
    evidence_refs JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Create the repository, `SqlAlchemyInvestigationRepository`, in the style of
`SqlAlchemySupportRepository`. Create the service in
`src/app/domain/investigation/service.py`. It owns this state machine and nothing else may
change a status:

```text
pending -> provisioning -> running -> completed | failed | cancelled
```

An invalid transition raises a domain error. Terminal states are immutable.

Service behavior, method by method:

- `start`: assemble the task brief from trace and bundle data, generate the bridge token with
  `secrets.token_urlsafe(32)`, store only its hash, create the row in `pending`.
- `record_events`: insert events keyed by `(investigation_id, seq)`. Sending the same batch
  twice must produce no duplicates and return success; that is what makes bridge retries safe.
- `record_message`, `complete`, `fail`: straightforward writes guarded by the state machine.
  Only `completed` runs carry a summary row.
- `sweep_stale`: any run still `running` with no heartbeat for 90 seconds becomes `failed`
  with reason "operational". Call it from app startup so yesterday's orphans get reconciled.

Expose thin routes in `src/app/api/investigations_router.py`, mounted in `src/app/main.py`
under `/api/v1/investigations`, using the existing dev-token dependency:

| Method and path | Purpose |
| --- | --- |
| `POST /api/v1/investigations` | Create and provision; returns 202 with the id |
| `GET /api/v1/investigations` | List |
| `GET /api/v1/investigations/{id}` | Detail |
| `POST /api/v1/investigations/{id}/messages` | Chat; body carries text and mode |
| `GET /api/v1/investigations/{id}/events` | Server-sent events stream |
| `GET /internal/inbox` | Bridge long-poll, bearer bridge token |
| `POST /internal/events` | Bridge event batches, bearer bridge token |

The events endpoint is where reconnect lives. Read the `Last-Event-ID` header (it carries the
last sequence number the client saw), replay everything newer from PostgreSQL, then keep the
connection open and tail new arrivals. Because persistence precedes streaming, a client that
reconnects late receives a complete, ordered history with no gaps. Set the
`X-Accel-Buffering: no` header so proxies do not hold the stream back.

Test the service logic in units: state machine rejections, idempotent inserts, sweep behavior,
token hashing. Test the stream against real PostgreSQL in integration: insert events, connect,
disconnect mid-stream, reconnect with `Last-Event-ID`, assert the second connection receives
every missing event exactly once, in order.

Done when: the reconnect test passes against real PostgreSQL, ruff and mypy are clean, and you
sent the `[DONE]` ping.

## Milestone 5: Wire it end to end (THE-23)

One command now takes a trace to a persisted summary, and the sandbox dies afterwards.

1. In the service's start path, resolve the runner from settings: `ModalRunner` when
   `modal_enabled` is true, `FakeRunner` otherwise. Wrap the run so that every exit path,
   including failure and cancellation, calls `terminate` exactly once.
2. Call `sweep_stale` at app startup so runs orphaned by a previous deployment get reconciled.
3. Write the task brief template that becomes Prime Agent's opening input: the trace summary
   from `src/app/domain/evidence/`, the slice description, the rules with their provenance
   labels, what the evaluators expect, and one explicit instruction: the final act must be a
   `summary.submit` call containing findings and a recommended next step.
4. Add CLI commands in `src/app/cli/`, mirroring how `lab simulate` is built:
   - `lab investigate start <trace-ref> [--follow]`
   - `lab investigate attach <id>` (reconnects; resumes the stream from the last seen event)
   - `lab investigate send <id> "<message>" [--steer]`
   - `lab investigate list`
5. Add `scripts/investigate_smoke.py`: real Modal, real Prime Agent, real model keys. It skips
   unless `SIMULATE_LIVE_E2E=1` and credentials exist, per repo testing rules.
6. Add the offline end-to-end test: `FakeRunner` plus a scripted fake Prime Agent driven
   through the real FastAPI app. Start an investigation, stream events, disconnect,
   reconnect, send a steer message, submit a summary, then assert the sandbox was terminated
   and every table holds its final rows.

Done when: the offline end-to-end test passes, the smoke script exists and skips cleanly, and
the CLI help renders all four commands.

## Configuration to add

Append to `.env.example`; leave values empty:

```bash
MODAL_ENABLED=false            # false means FakeRunner everywhere
MODAL_APP_NAME=simulate-mvp
CONTROL_PLANE_PUBLIC_URL=      # URL sandboxes use to reach this app
INVESTIGATION_HEARTBEAT_SILENCE_S=90
SIMULATE_LIVE_E2E=             # set to 1 only for the live smoke script
```

Model API keys never appear in this file or in the database. They travel as Modal secret
environment variables into the sandbox and nowhere else.

## Risks and open questions

| Risk | What we do | When it escalates |
| --- | --- | --- |
| Prime Agent install method and protocol fields are unverified | Verify against upstream first in milestone 3; isolate knowledge in `prime_rpc.py` | Verification exceeds 30 minutes |
| Modal SDK differs across versions | Pin the version; keep all SDK calls in `_modal_client.py` | A needed API is missing from the pinned version |
| In-sandbox database choice | Primary: PostgreSQL installed in the image, reusing existing repositories unchanged | The image path fails twice on Modal |
| Server-sent events buffered by a proxy | Send `X-Accel-Buffering: no` plus keepalive comments | The reconnect test fails twice |
| Support code assumes shared-database rollback isolation | Sandbox databases are per-run; seed fresh instead of rolling back | A repository turns out hard-wired to rollback semantics |

## Out of scope

Do not build these during Phase 8 MVP, no matter how tempting: alert intake webhooks, the
experiment engine (baseline versus candidate, ablations, statistics), model-swap arms, the
world compiler and discovery flow, a web UI, multi-user authentication, Hetzner or BYOVM
providers, renames of legacy terms. Each has a later home in the roadmap.

## Final acceptance checklist

These map one-to-one onto THE-18's acceptance boxes:

- [ ] One command starts a detached Modal investigation from one support trace reference.
- [ ] Disconnecting and reconnecting mid-run loses nothing; events resume ordered and complete.
- [ ] Chat reaches the running agent while it works, and steering works mid-run.
- [ ] The final summary persists, and the sandbox terminates automatically afterwards.
- [ ] Trace reference, world and slice versions, both agent identities, full event history,
      chat log, and summary are all queryable.
- [ ] `uv run ruff check .`, `uv run mypy src`, and `uv run pytest tests/unit -q` pass;
      integration tests pass against disposable PostgreSQL; the live script skips cleanly.

---

## Appendix A: escalation log

Add an entry here only when a mesh message went unanswered for ten minutes. Newest last.

Format:

```text
### [BLOCKED or DECISION] THE-1x - timestamp - one-line summary
<the same content you sent over mesh>
```



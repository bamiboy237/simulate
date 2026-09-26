"""Focused policy checks for the support-experiment operator boundary."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.adapters.pydantic_ai_agent import ModelConfig
from app.config import Settings
from app.domain.experiment.contracts import CandidateChangeType, ExperimentContract
from app.domain.experiment.models import ExperimentRecord
from app.domain.experiment.operator import (
    ExperimentOperatorError,
    SupportExperimentOperator,
    SupportExperimentStartInputs,
    SupportPromptRuntime,
)

FIXTURES = Path(__file__).with_name("fixtures")


class _Session:
    """Minimal async session context used before any write is permitted."""

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def _pending_record(*, max_tokens: int | None = None) -> ExperimentRecord:
    payload = json.loads((FIXTURES / "valid_support_experiment.json").read_text(encoding="utf-8"))
    payload["experiment_id"] = str(uuid4())
    payload["limits"].update(
        {
            "max_tool_calls": None,
            "max_retries": None,
            "max_tokens": None,
            "max_cost_usd": None,
        }
    )
    if max_tokens is not None:
        payload["limits"]["max_tokens"] = max_tokens
    contract = ExperimentContract.model_validate(payload)
    return ExperimentRecord(
        id=contract.experiment_id,
        contract=contract.model_dump(mode="json"),
        contract_hash=contract.content_hash,
        status="pending",
    )


def test_rejects_operator_lock_timeout_shorter_than_the_experiment_cap() -> None:
    """A transaction lock must survive the longest valid synchronous experiment."""
    operator = SupportExperimentOperator(
        session_factory=_Session,
        settings=Settings(
            database_url="postgresql://operator:password@localhost:5432/control",
            experiment_max_total_duration_s=120,
            _env_file=None,
        ),
    )

    with pytest.raises(ExperimentOperatorError) as raised:
        operator._require_operator_lock_timeout(60)

    assert raised.value.code == "experiment_operator_lock_timeout_unsafe"


def test_runtime_bindings_reject_changed_endpoint_for_the_same_model_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persisted endpoint fingerprint prevents an alias from silently rerouting at start."""
    stored_model = SupportExperimentOperator._model_ref(
        ModelConfig(
            provider="openai",
            name="gpt-5",
            base_url="https://models.example.test/v1/",
        )
    )
    settings = Settings(
        database_url="postgresql://operator:password@localhost:5432/control",
        model_provider="openai",
        model_name="gpt-5",
        model_base_url="https://models.example.test/v2",
        model_api_key="test-key",
        _env_file=None,
    )
    operator = SupportExperimentOperator(session_factory=_Session, settings=settings)
    template = _pending_record()
    contract = ExperimentContract.model_validate(template.contract)
    baseline = contract.baseline.model_copy(update={"model": stored_model})
    candidate = contract.candidate.model_copy(
        update={
            "artifact": baseline.artifact,
            "model": stored_model,
            "prompt": baseline.prompt.model_copy(update={"content_hash": "8" * 64}),
        }
    )
    endpoint_bound_contract = contract.model_copy(
        update={
            "baseline": baseline,
            "candidate": candidate,
            "candidate_change": CandidateChangeType.PROMPT,
        }
    )
    monkeypatch.setattr(operator, "_current_runtime_artifact", lambda: baseline.artifact)

    with pytest.raises(ExperimentOperatorError) as raised:
        operator._runtime_bindings(
            endpoint_bound_contract,
            SupportExperimentStartInputs(
                baseline_prompt=SupportPromptRuntime(
                    prompt_id="support-answer",
                    version="1.0.0",
                    content="approved prompt",
                )
            ),
        )

    assert raised.value.code == "experiment_model_endpoint_changed"

"""WP-09 regression coverage for single- and dual-model qualitative judging."""

from __future__ import annotations

import pytest

from mirrorfirm.core.models import JudgeCriterion
from mirrorfirm.evaluation.qualitative import (
    REFERENCE_VALIDATION_MODEL,
    QualitativeJudge,
    ReferenceQualitativeValidator,
    ReferenceTargetExpectation,
)
from mirrorfirm.harness.adapters.base import (
    ModelAdapter,
    ModelResponse,
    ProviderPayload,
)


def test_dual_judging_averages_scores_and_flags_large_disagreement() -> None:
    """§H requires dual scores to average and >1-band disagreement to be reviewable."""

    judge = QualitativeJudge(
        {
            "fictional/a": lambda _prompt: '{"score": 5, "reasoning": "clear"}',
            "fictional/b": lambda _prompt: '{"score": 2, "reasoning": "unclear"}',
        }
    )
    criterion = JudgeCriterion(
        id="comms",
        prompt="Was the fictional client message clear?",
        target="outbound_messages",
        scale="one_to_five",
        weight=1.0,
    )

    result = judge.judge(
        criterion, ["Fictional client update."], ["fictional/a", "fictional/b"]
    )

    assert result.score == 0.7
    assert result.requires_review is True
    assert result.passed is True


def test_single_binary_judge_uses_the_pass_band_without_review_flag() -> None:
    """Single-mode judging preserves the requested binary verdict."""

    judge = QualitativeJudge(
        {"fictional/one": lambda _prompt: '{"verdict": "pass", "reasoning": "clear"}'}
    )
    criterion = JudgeCriterion(
        id="binary-comms",
        prompt="Was the fictional reply professional?",
        target="outbound_messages",
        scale="binary",
        weight=1.0,
    )

    result = judge.judge(criterion, ["Fictional reply."], ["fictional/one"])

    assert result.score == 1.0
    assert result.passed is True
    assert result.requires_review is False


class AdapterJudgeStub(ModelAdapter):
    """A deterministic stand-in for the retained Harvey provider adapter boundary."""

    def chat(
        self, messages: list[ProviderPayload], tools: list[ProviderPayload]
    ) -> ModelResponse:
        assert len(messages) == 2
        assert tools == []
        return ModelResponse(
            message={"role": "assistant", "content": "unused"},
            text='{"verdict": "pass", "reasoning": "fictional tone is clear"}',
        )

    def make_tool_result_messages(
        self, results: list[tuple[str, str]]
    ) -> list[ProviderPayload]:
        return [{"role": "tool", "results": results}]

    def make_system_message(self, content: str) -> ProviderPayload:
        return {"role": "system", "content": content}

    def make_user_message(self, content: str) -> ProviderPayload:
        return {"role": "user", "content": content}


def test_qualitative_judge_reuses_the_provider_adapter_boundary() -> None:
    """Qualitative judging invokes the existing adapter interface without new SDK code."""

    judge = QualitativeJudge.from_model_adapters(
        {"fictional/adapter": AdapterJudgeStub("fictional/adapter")}
    )
    criterion = JudgeCriterion(
        id="adapter-comms",
        prompt="Was the fictional message clear?",
        target="outbound_messages",
        scale="binary",
        weight=1.0,
    )

    result = judge.judge(criterion, ["Fictional update."], ["fictional/adapter"])

    assert result.passed is True


def test_reference_validator_is_fail_closed_and_unavailable_to_model_baselines() -> (
    None
):
    """Credential-free reference checking validates targets rather than always passing."""

    criterion = JudgeCriterion(
        id="reference-tone",
        prompt="Is the fictional reference clear?",
        target="outbound_messages",
        scale="binary",
        weight=1.0,
    )
    validator = ReferenceQualitativeValidator(
        {
            criterion.id: ReferenceTargetExpectation(
                target="outbound_messages",
                required_text=["receipt", "review"],
                forbidden_text=["posted"],
            )
        }
    )

    passed = validator.judge(
        criterion,
        ["The receipt is proposed for review."],
        [REFERENCE_VALIDATION_MODEL],
    )
    empty = validator.judge(criterion, [], [REFERENCE_VALIDATION_MODEL])
    contradictory = validator.judge(
        criterion,
        ["The receipt was posted for review."],
        [REFERENCE_VALIDATION_MODEL],
    )

    assert passed.passed
    assert not empty.passed
    assert not contradictory.passed
    with pytest.raises(ValueError, match="unavailable to model baselines"):
        validator.judge(criterion, ["receipt for review"], ["fictional/model"])

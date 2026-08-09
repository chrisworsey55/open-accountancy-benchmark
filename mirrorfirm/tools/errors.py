"""Typed failures returned by the permissioned WP-07 tool layer."""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

ToolErrorCode: TypeAlias = Literal[
    "PERMISSION_DENIED",
    "NOT_FOUND",
    "VALIDATION_ERROR",
    "SCOPE_VIOLATION",
    "TOOL_NOT_FOUND",
    "BAD_PREDICATE",
    "TYPE_MISMATCH",
    "DIV_ZERO",
    "UNRESOLVED_BINDING",
    "KEY_NOT_UNIQUE",
    "SPLIT_MISMATCH",
    "ALREADY_CLASSIFIED",
    "PROVENANCE_REQUIRED",
    "PERIOD_LOCKED",
    "TAX_NOT_SUPPORTED",
    "UNBALANCED",
    "PERIOD_CLOSED_NEEDS_APPROVAL",
    "DUPLICATE_REQUEST",
    "NOTHING_TO_APPROVE",
    "DESCRIPTOR_MISMATCH",
    "NO_CONTACT",
    "POLICY_REQUIRES_APPROVAL",
    "RECON_DOES_NOT_TIE",
    "ALREADY_FINAL",
    "REVIEW_REQUIRED",
    "DRAFT_WORKPAPER",
    "EMPTY_SUBMISSION",
    "PAST_TIME",
    "EXCEEDS_EPISODE_HORIZON",
]


class ToolError(BaseModel):
    """A stable, machine-readable error surface for every tool call."""

    model_config = ConfigDict(extra="forbid")

    code: ToolErrorCode
    message: str
    details: dict[str, object] = Field(default_factory=dict)


class ToolExecutionError(Exception):
    """Internal control-flow exception converted to a logged ``ToolError``."""

    def __init__(
        self,
        code: ToolErrorCode,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.error = ToolError(code=code, message=message, details=details or {})

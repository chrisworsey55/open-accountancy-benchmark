"""Small immutable snapshot fixtures for WP-09 evaluator tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TypeVar

from mirrorfirm.core.digest import logical_state_digest
from mirrorfirm.core.models import (
    Action,
    Client,
    Engagement,
    Entity,
    Event,
    Journal,
    JournalLine,
    Practice,
    PracticePolicies,
    VersionedModel,
)

ModelT = TypeVar("ModelT", bound=VersionedModel)
NOW = datetime(2026, 5, 28, 8, 30, tzinfo=UTC)


class SnapshotView:
    """In-memory test double for the stable read-only WorldView protocol."""

    def __init__(
        self, records: list[VersionedModel], actions: list[Action] | None = None
    ) -> None:
        self._records = records
        self._actions = tuple(actions or [])

    def get(self, model_type: type[ModelT], entity_id: str) -> ModelT | None:
        return next(
            (
                record
                for record in self.list(model_type)
                if record.model_dump(mode="python").get("id") == entity_id
            ),
            None,
        )

    def list(self, model_type: type[ModelT]) -> tuple[ModelT, ...]:
        return tuple(
            record for record in self._records if isinstance(record, model_type)
        )

    def actions(self) -> tuple[Action, ...]:
        return self._actions

    def events(self) -> tuple[Event, ...]:
        return ()

    def logical_state(self) -> dict[str, list[object]]:
        return {}

    def state_digest(self) -> str:
        return "0" * 64


def base_records(*, drafts_only: bool = False) -> list[VersionedModel]:
    """Return two fictional clients and the active engagement under evaluation."""

    client_a = Client(
        id="cli-fictional-a",
        name="Fictional Alpha Ltd",
        status="active",
        entity=Entity(
            id="ent-fictional-a",
            legal_form="ltd",
            basis="accrual",
            currency="GBP",
            tax={
                "kind": "uk",
                "vat_registered": False,
                "vat_number": None,
                "vat_scheme": "none",
            },
        ),
        contacts=["per-fictional-a-contact"],
    )
    client_b = Client(
        id="cli-fictional-b",
        name="Fictional Beta Ltd",
        status="active",
        entity=Entity(
            id="ent-fictional-b",
            legal_form="ltd",
            basis="accrual",
            currency="GBP",
            tax={
                "kind": "uk",
                "vat_registered": False,
                "vat_number": None,
                "vat_scheme": "none",
            },
        ),
        contacts=["per-fictional-b-contact"],
    )
    return [
        Practice(
            id="prc-fictional",
            name="Fictional Practice",
            jurisdiction="uk",
            people=[],
            policies=PracticePolicies(
                approval_required_for=[],
                outbound_comms="agent_drafts_only" if drafts_only else "agent_may_send",
                materiality_minor=100,
            ),
        ),
        client_a,
        client_b,
        Engagement(
            id="eng-fictional-a",
            client_id=client_a.id,
            scope="bookkeeping",
            period_ids=[],
            status="active",
        ),
    ]


def journal(*, identifier: str = "jnl-fictional", status: str = "proposed") -> Journal:
    """Return one valid fictional journal, optionally in a post-state."""

    return Journal(
        id=identifier,
        entity_id="ent-fictional-a",
        date=date(2026, 5, 28),
        memo="Fictional journal",
        source="proposal",
        status=status,
        lines=[
            JournalLine(
                account_id="acc-fictional-dr",
                direction="dr",
                amount_minor=100,
                currency="GBP",
            ),
            JournalLine(
                account_id="acc-fictional-cr",
                direction="cr",
                amount_minor=100,
                currency="GBP",
            ),
        ],
        proposed_by="per-agent",
        approval_id=None,
    )


def action(
    tool: str,
    *,
    input_payload: object | None = None,
    output_payload: object | None = None,
) -> Action:
    """Return an I-8-valid synthetic action with arbitrary JSON-compatible payloads."""

    input_value = {} if input_payload is None else input_payload
    output_value = {} if output_payload is None else output_payload
    return Action(
        id=f"act-{tool.replace('.', '-')}",
        step=1,
        actor="per-agent",
        tool=tool,
        input_digest=logical_state_digest(input_value),
        output_digest=logical_state_digest(output_value),
        input_payload=input_value,
        output_payload=output_value,
        world_time_before=NOW,
        world_time_after=NOW,
        mutations=[],
    )

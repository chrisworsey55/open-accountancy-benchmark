"""Canonical state shared by approval execution and immutable approval grading."""

from __future__ import annotations

from mirrorfirm.core.digest import logical_state_digest
from mirrorfirm.core.models.domain import InformationRequest, Message, Thread


def approved_message_digest(
    message: Message,
    thread: Thread,
    request: InformationRequest | None,
    *,
    client_id: str,
    engagement_id: str,
) -> str:
    """Digest the complete state an approval authorises for external delivery.

    Message status and sent timestamp are deliberately omitted: the system changes
    those as the approved delivery effect.  Everything capable of changing what is
    sent, who receives it, or which request it represents is retained verbatim.
    """

    return logical_state_digest(
        {
            "approval_scope": {
                "client_id": client_id,
                "engagement_id": engagement_id,
            },
            "message": {
                "id": message.id,
                "thread_id": message.thread_id,
                "sender": message.sender,
                "recipients": message.recipients,
                "body": message.body,
                "attachments": message.attachments,
                "direction": message.direction,
            },
            "thread": {
                "id": thread.id,
                "client_id": thread.client_id,
                "engagement_id": thread.engagement_id,
                "subject": thread.subject,
            },
            "information_request": (
                None
                if request is None
                else {
                    "id": request.id,
                    "client_id": request.client_id,
                    "engagement_id": request.engagement_id,
                    "thread_id": request.thread_id,
                    "items": [item.model_dump(mode="json") for item in request.items],
                }
            ),
        }
    )

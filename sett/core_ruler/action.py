"""
SETT Framework: Action
==============================
An Action is a real-world side effect described as DATA, not as code.

This is the core of the "actions as data" model: an expert never calls
send_sms(...) or charge_payment(...) directly. Instead it returns an
Action describing what it wants to happen. Only the SETTExecutor,
via a handler registered explicitly for that action_type, is allowed
to actually perform the effect, and only after the EthicalFilter
approves it.

This is what makes the guarantee structural rather than a convention,
for the path this module defines: an expert following the "actions as
data" pattern physically has no reference to the real client (Twilio,
an email SDK, a payment API) and can only describe intent.

Scope (F19, defensive audit #2): that guarantee covers effects
submitted as an Action through SETTExecutor. It does not, and cannot,
prevent application code from performing a side effect directly in
Python outside this path - propose_action() and any hand-written
integration code remain opt-in and are the calling application's
responsibility.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


@dataclass
class Action:
    """
    A proposed real-world side effect, described as data.

    Args:
        action_type: Identifies which registered handler should run this
                     action (e.g. "send_sms", "call_emergency_services",
                     "charge_payment"). Must match a handler registered
                     with SETTExecutor.register_handler().
        payload: Data the handler needs to actually perform the effect
                 (e.g. {"to": "+54...", "message": "..."}).
        proposed_by: The domain of the agent that proposed this action.
                     Used for logging/audit only: never used to identify
                     or expose anything about the end user.
    """
    action_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    proposed_by: str = "unknown"
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    action_id: str = field(default_factory=lambda: str(uuid4()))
    idempotency_key: str | None = None
    """Identity of the business operation this action performs, if any.

    Not the same thing as ``action_id``, and not derived from
    ``trace_id``. ``action_id`` identifies this proposal; the idempotency
    key identifies the operation out in the world, so a retry keeps the
    key and takes a new ``action_id``. One trace may legitimately contain
    two different external operations of the same type, which is exactly
    why the key comes from the application's own domain
    ("payment:invoice-2481") rather than from anything SETT could guess.

    Added in v0.12.0 and appended last, so existing positional
    construction of Action is unaffected. ``None`` - the default - means
    this action carries no idempotency guarantee and is never retried
    automatically.
    """

    def __repr__(self) -> str:
        return (
            f"Action(action_type={self.action_type!r}, "
            f"proposed_by={self.proposed_by!r})"
        )

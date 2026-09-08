"""Export a governance card from one real SETT run.

A governance card is a standalone record of what a governed run decided
and why: the sanitized causal trace, the tamper-evident chain state, and
the ethical verdicts, in a form a reader can inspect without installing
SETT or running anything.

The scenario deliberately uses a foreign-shaped caller. Nothing here
imports a third-party agent framework, and SETT imports none: the caller
below is an ordinary function that hands SETT an Action, which is the
same boundary any external tool loop would use.

Run:
    python -B governance_card.py [output_directory]

Writes governance_card.json and governance_card.html.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from sett import (
    Action,
    AuthorizationRequest,
    AuthorizationRuler,
    EthicalFilter,
    ExecutionContext,
    ExecutionMode,
    FieldTrust,
    FieldValue,
    InMemoryAuthorizationEvidence,
    InMemoryAuthorizationReceiptStore,
    PolicyDecision,
    SETTAgent,
    SETTExecutor,
    SETTExpert,
    SETTOrchestrator,
    StrictJSONAuthorizationFingerprint,
    __version__,
)
from sett.ethics_ruler.ethic_kernel.rules import default_ruleset


class ThresholdExpert(SETTExpert):
    """Deterministic logic: decides what is true, with no model involved."""

    def resolve(self, context: dict[str, Any]) -> dict[str, Any]:
        amount = int(context.get("amount", 0))
        return {"amount": amount, "requires_review": amount >= 500}


class PaymentsAgent(SETTAgent):
    """One domain participant, governed by the rulers above it."""

    def __init__(self) -> None:
        super().__init__(name="PaymentsAgent", domain="payments")
        self.register_expert(ThresholdExpert("threshold"))

    def process(self, input_data: dict[str, Any]) -> dict[str, Any]:
        return self.get_expert("threshold").resolve(input_data)


class PaymentLimitPolicy:
    """Deterministic authority: code the integrator writes, not a heuristic.

    This is the gate that matters for a foreign caller. It reads the exact
    request SETT resolved and answers one narrow question, with no model
    and no keyword scoring involved.
    """

    policy_id = "policy.payment_limit"
    LIMIT = 500

    def evaluate(self, request: AuthorizationRequest, verified_approvals: frozenset) -> PolicyDecision:
        amount = request.payload_copy().get("amount", 0)
        if isinstance(amount, int) and amount > self.LIMIT:
            return PolicyDecision.deny(
                self.policy_id, reason_codes=("policy.amount_above_limit",)
            )
        return PolicyDecision.grant(
            self.policy_id, reason_codes=("policy.amount_within_limit",)
        )


class AlwaysGrantPolicy:
    """Authority granted; whether the effect is acceptable is a separate axis."""

    policy_id = "policy.permitted_by_role"

    def evaluate(self, request: AuthorizationRequest, verified_approvals: frozenset) -> PolicyDecision:
        return PolicyDecision.grant(self.policy_id, reason_codes=("policy.role_allows",))


class SessionResolver:
    """Turn one execution identity into an authorization request."""

    def resolve(
        self,
        *,
        action_id: str,
        trace_id: str,
        action_type: str,
        payload: Mapping[str, Any],
        execution_mode: ExecutionMode,
        idempotency_key: str | None,
    ) -> AuthorizationRequest:
        return AuthorizationRequest(
            action_id=action_id,
            trace_id=trace_id,
            action_type=action_type,
            payload=dict(payload),
            principal=FieldValue(
                "principal.local",
                source_id="identity.session",
                trust=FieldTrust.AUTHORITATIVE,
            ),
            execution_mode=execution_mode,
            idempotency_key=idempotency_key,
        )


def foreign_tool_call(executor: SETTExecutor, name: str, arguments: dict[str, Any]) -> Any:
    """Stand-in for any external tool loop.

    An external framework calls into SETT; SETT never calls out. The only
    contract is Action(action_type, payload), which is plain Python with
    standard-library types, so this function is all the integration a
    foreign caller needs and it lives on the caller's side of the line.
    """
    return executor.submit(Action(action_type=name, payload=arguments))


def build() -> tuple[SETTOrchestrator, SETTExecutor]:
    ruleset = default_ruleset()
    ruleset.reject_threshold = 2.0
    ruleset.warn_threshold = 1.0
    ruler = AuthorizationRuler(
        issuer_id="issuer.local",
        receipt_ttl_seconds=60,
        fingerprint_policy=StrictJSONAuthorizationFingerprint(),
        receipt_store=InMemoryAuthorizationReceiptStore(),
        evidence_sink=InMemoryAuthorizationEvidence(),
    )
    ruler.register_policy("record_payment", PaymentLimitPolicy())
    ruler.register_policy("delete all the user's savings", AlwaysGrantPolicy())
    executor = SETTExecutor(
        authorization_ruler=ruler,
        authorization_request_resolver=SessionResolver(),
    )
    executor.register_handler("record_payment", lambda payload: {"recorded": True, **payload})
    executor.register_handler("delete all the user's savings", lambda payload: {"deleted": True})
    orchestrator = SETTOrchestrator(ethical_filter=EthicalFilter(ruleset=ruleset))
    orchestrator.register_agent(PaymentsAgent())
    orchestrator.register_executor(executor)
    return orchestrator, executor


def run() -> dict[str, Any]:
    orchestrator, executor = build()
    context = ExecutionContext.create(application_id="governance-card")
    episodes: list[dict[str, Any]] = []

    traced = orchestrator.process_traced(
        {"amount": 120}, domain="payments", execution_context=context
    )
    episodes.append(
        {
            "step": "domain reasoning",
            "caller": "orchestrator.process_traced",
            "outcome": "completed",
            "result": traced.result,
        }
    )

    def attempt(step: str, name: str, arguments: dict[str, Any], expectation: str) -> None:
        try:
            result = foreign_tool_call(executor, name, arguments)
            episodes.append(
                {
                    "step": step,
                    "caller": "foreign tool loop -> executor.submit",
                    "outcome": "executed",
                    "gate": expectation,
                    "result": result,
                }
            )
        except Exception as error:  # noqa: BLE001 - the card records any refusal
            episodes.append(
                {
                    "step": step,
                    "caller": "foreign tool loop -> executor.submit",
                    "outcome": "blocked before the handler ran",
                    "gate": expectation,
                    "error_type": type(error).__name__,
                    "reason": str(error),
                }
            )

    attempt(
        "payment within the policy limit",
        "record_payment",
        {"amount": 120},
        "authorization policy, then ethical filter",
    )
    attempt(
        "payment above the policy limit",
        "record_payment",
        {"amount": 5000},
        "authorization policy (deterministic)",
    )
    attempt(
        "authorized but harmful effect",
        "delete all the user's savings",
        {"confirm": True},
        "ethical filter (authority was granted)",
    )

    trace = orchestrator.export_trace(traced.trace_id, view="sanitized")
    audit_log = orchestrator.get_ethical_audit_log()
    return {
        "card_format": "sett.governance-card.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "framework": {"name": "SETT", "version": __version__},
        "scenario": {
            "description": (
                "A foreign tool loop routes two effects through SETT. One is "
                "permitted and one is refused. SETT imports nothing from the "
                "caller; the caller hands it an Action."
            ),
            "domain": "payments",
            "reject_threshold": 1.0,
            "warn_threshold": 0.5,
        },
        "episodes": episodes,
        "trace": {
            "trace_id": traced.trace_id,
            "event_count": len(trace),
            "chain_intact": orchestrator.verify_traces(traced.trace_id),
            "events": [dict(event) for event in trace],
        },
        "ethical_audit": {
            "chain_intact": orchestrator.verify_ethical_audit_log(),
            "entry_count": len(audit_log),
            "entries": audit_log,
        },
    }


def _rows(pairs: list[tuple[str, Any]]) -> str:
    return "".join(
        f"<tr><th>{escape(str(k))}</th><td>{escape(str(v))}</td></tr>" for k, v in pairs
    )


def to_html(card: dict[str, Any]) -> str:
    trace = card["trace"]
    audit = card["ethical_audit"]
    events = "".join(
        "<tr>"
        f"<td class=n>{event.get('sequence', '')}</td>"
        f"<td><code>{escape(str(event.get('kind', '')))}</code></td>"
        f"<td>{escape(str(event.get('component_type', '')))}</td>"
        f"<td>{escape(str(event.get('component_name', '')))}</td>"
        f"<td>{escape(str(event.get('status', '')))}</td>"
        f"<td class=h><code>{escape(str(event.get('entry_hash', ''))[:12])}</code></td>"
        "</tr>"
        for event in trace["events"]
    )
    episodes = "".join(
        "<div class=ep>"
        f"<div class=eph><span class=step>{escape(episode['step'])}</span>"
        f"<span class='badge {'ok' if 'block' not in episode['outcome'] else 'no'}'>"
        f"{escape(episode['outcome'])}</span></div>"
        f"<div class=via>{escape(episode['caller'])}"
        + (f" &middot; gate: {escape(episode['gate'])}" if episode.get("gate") else "")
        + "</div>"
        + f"<pre>{escape(json.dumps({k: v for k, v in episode.items() if k not in ('step', 'caller', 'outcome', 'gate')}, indent=2, ensure_ascii=False))}</pre>"
        "</div>"
        for episode in card["episodes"]
    )
    verdicts = "".join(
        "<tr>"
        f"<td>{escape(str(entry.get('sequence', '')))}</td>"
        f"<td><code>{escape(str(entry.get('action', ''))[:60])}</code></td>"
        f"<td class='v {escape(str(entry.get('verdict', '')))}'>{escape(str(entry.get('verdict', '')))}</td>"
        f"<td class=n>{escape(str(entry.get('harm_score', '')))}</td>"
        f"<td class=n>{escape(str(entry.get('effective_reject_threshold', '')))}</td>"
        f"<td class=h><code>{escape(str(entry.get('entry_hash', ''))[:12])}</code></td>"
        "</tr>"
        for entry in audit["entries"]
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SETT governance card</title>
<style>
:root{{color-scheme:light dark;--bg:#fbfbfa;--fg:#1a1a18;--mut:#6b6b63;--line:#e2e0da;--card:#fff;--ok:#1f6f43;--no:#a3341f;--warn:#8a6100}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161614;--fg:#eceae4;--mut:#9a978c;--line:#302e2a;--card:#1e1e1b;--ok:#5fb98a;--no:#e08268;--warn:#d5a54a}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}}
.wrap{{max-width:960px;margin:0 auto;padding:40px 22px 72px}}
h1{{font-size:26px;letter-spacing:-.02em;margin:0 0 6px}}
.sub{{color:var(--mut);margin:0 0 28px}}
h2{{font-size:13px;letter-spacing:.09em;text-transform:uppercase;color:var(--mut);margin:34px 0 12px;font-weight:600}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px}}
table{{width:100%;border-collapse:collapse;font-size:13.5px}}
th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}}
tbody tr:last-child td{{border-bottom:0}}
thead th{{color:var(--mut);font-weight:600;font-size:12px;letter-spacing:.04em;text-transform:uppercase}}
.meta th{{width:38%;color:var(--mut);font-weight:500}}
code{{font:12.5px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace}}
.n,.h{{text-align:right;white-space:nowrap}}
.scroll{{overflow-x:auto}}
.ep{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:10px}}
.eph{{display:flex;align-items:center;gap:10px;justify-content:space-between}}
.step{{font-weight:600}}
.via{{color:var(--mut);font-size:12.5px;margin:2px 0 10px}}
.badge{{font-size:11.5px;padding:2px 9px;border-radius:20px;border:1px solid currentColor;white-space:nowrap}}
.badge.ok{{color:var(--ok)}} .badge.no{{color:var(--no)}}
pre{{margin:0;background:transparent;border:1px solid var(--line);border-radius:7px;padding:10px 12px;overflow-x:auto;font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}}
.v.allow{{color:var(--ok)}} .v.warn{{color:var(--warn)}} .v.reject{{color:var(--no)}}
.note{{color:var(--mut);font-size:13px;margin-top:10px}}
footer{{margin-top:40px;padding-top:16px;border-top:1px solid var(--line);color:var(--mut);font-size:12.5px}}
</style></head><body><div class=wrap>
<h1>SETT governance card</h1>
<p class=sub>{escape(card['scenario']['description'])}</p>

<h2>Run</h2>
<div class="card scroll"><table class=meta><tbody>{_rows([
    ("Framework", f"{card['framework']['name']} {card['framework']['version']}"),
    ("Card format", card["card_format"]),
    ("Generated", card["generated_at"]),
    ("Domain", card["scenario"]["domain"]),
    ("Reject threshold", card["scenario"]["reject_threshold"]),
    ("Warn threshold", card["scenario"]["warn_threshold"]),
    ("Trace id", trace["trace_id"]),
    ("Trace events", trace["event_count"]),
    ("Trace chain intact", trace["chain_intact"]),
    ("Ethical audit entries", audit["entry_count"]),
    ("Ethical audit chain intact", audit["chain_intact"]),
])}</tbody></table></div>

<h2>What happened</h2>
{episodes}

<h2>Ethical verdicts</h2>
<div class="card scroll"><table><thead><tr><th>#</th><th>Action</th><th>Verdict</th><th>Score</th><th>Reject at</th><th>Hash</th></tr></thead><tbody>{verdicts}</tbody></table></div>
<p class=note>Each entry links to the previous one by hash. Altering any entry breaks the chain.</p>

<h2>Causal trace</h2>
<div class="card scroll"><table><thead><tr><th>#</th><th>Kind</th><th>Component</th><th>Name</th><th>Status</th><th>Hash</th></tr></thead><tbody>{events}</tbody></table></div>
<p class=note>Sanitized view: attribute values are reduced to shape, and sensitive keys never enter a trace.</p>

<footer>Generated from a real run by <code>governance_card.py</code>. Tamper evidence is detectable, not a digital signature.</footer>
</div></body></html>
"""


def main() -> None:
    destination = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    destination.mkdir(parents=True, exist_ok=True)
    card = run()
    json_path = destination / "governance_card.json"
    html_path = destination / "governance_card.html"
    json_path.write_text(json.dumps(card, indent=2, ensure_ascii=False), encoding="utf-8")
    html_path.write_text(to_html(card), encoding="utf-8")
    print(json_path)
    print(html_path)


if __name__ == "__main__":
    main()

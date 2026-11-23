"""
SETT Framework: Executor Example
==============================
Demonstrates the "actions as data" pattern using SETTExecutor: the
structural alternative to SETTAgent.propose_action().

The core idea: an expert never calls the real client (an SMS provider,
a task service, a payment gateway) directly. It only
describes what it wants to happen as an Action. The SETTExecutor is
the ONLY component that can turn that description into a real effect,
and only after the EthicalFilter approves it.

This closes a gap that propose_action() alone does not: with
propose_action(), the developer must remember to call the gate before
performing the effect themselves: nothing stops them from forgetting.
With the Executor, the expert never holds a reference to the real
client at all, so there is no "forgot to gate it" failure mode: the
only path to the real side effect runs through the filter.

Run with:
    cd sett-framework
    PYTHONPATH=. python examples/with_executor.py
"""
from sett import (
    EthicalFilter,
    EthicalRuleset,
    RiskProfile,
    SETTAgent,
    SETTConfigurationError,
    SETTEthicalFilterRejectedError,
    SETTExecutor,
    SETTExpert,
    SETTOrchestrator,
)

# ── SIMULATED EXTERNAL CLIENTS ───────────────────────────────────────────────
# In a real system these would be a messaging client, a task service
# API, a payments SDK, etc. They are defined here, OUTSIDE any expert or
# agent, and only ever called from inside a handler registered with the
# Executor: never from expert/agent code directly.

def simulated_message_client(payload: dict) -> dict:
    print(f"    📱 [SMS PROVIDER] Sending to {payload.get('to')}: "
          f"\"{payload.get('message')}\"")
    return {"delivered": True}


def simulated_task_client(payload: dict) -> dict:
    print(f"    [TASK SERVICE] Submitting to queue: "
          f"{payload.get('queue', 'unknown')}")
    return {"submitted": True}


# ── AN AGENT THAT NEVER TOUCHES THE REAL CLIENTS ─────────────────────────────

class NotifyExpert(SETTExpert):
    """
    Decides WHAT should happen, but never performs it. It only writes
    intent to private memory: the agent turns that intent into an
    Action and submits it to the Executor.
    """

    def resolve(self, context):
        message = f"Reminder: {context.get('reminder', 'checking in')}"
        if self._private_memory:
            self._private_memory.write("last_message", message)
        return {"to": context.get("phone"), "message": message}


class NotificationAgent(SETTAgent):
    """
    Uses submit_action() instead of calling an SMS client directly.
    This agent's code has NO import of any SMS SDK: it physically
    cannot send a message except through the registered Executor.
    """

    def __init__(self):
        super().__init__(name="NotificationAgent", domain="notifications")
        self.register_expert(NotifyExpert(name="notify"))

    def process(self, input_data):
        payload = self.get_expert("notify").resolve(input_data)

        # Describe intent as data. The Executor decides if/how it runs.
        result = self.submit_action("send_sms", payload=payload)

        self._publish_to_universal({"notification_sent": True, **result})
        return result


class TaskAgent(SETTAgent):
    """
    This agent requests a task submission through its configured Executor.
    The simulated service is called only by the registered handler after
    the EthicalFilter accepts the action. No external service is contacted.
    """

    def __init__(self):
        super().__init__(name="TaskAgent", domain="tasks")

    def process(self, input_data):
        risk_profile = RiskProfile(
            emotional_instability=input_data.get("instability", 0.0),
            influence_vulnerability=0.0,
            collateral_damage_potential=0.0,
        )
        result = self.submit_action(
            "submit_task",
            payload={"queue": input_data.get("queue", "unknown")},
            risk_profile=risk_profile,
        )
        self._publish_to_universal({"task_requested": True})
        return result


# ── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":

    print("══════════════════════════════════════════════════════")
    print("  SETT Executor: Actions as Data")
    print("══════════════════════════════════════════════════════")

    # ── Scenario 1: normal notification: approved, handler runs ────────────

    print("\n[SCENARIO 1] Routine SMS reminder: expected: sent")

    executor = SETTExecutor()
    executor.register_handler("send_sms", simulated_message_client)
    executor.register_handler("submit_task", simulated_task_client)

    orchestrator = SETTOrchestrator(ethical_filter=EthicalFilter())
    orchestrator.register_executor(executor)   # order-independent: agents
    orchestrator.register_agent(NotificationAgent())  # can be registered
    orchestrator.register_agent(TaskAgent())     # before or after this

    result = orchestrator.process(
        input_data={"phone": "example-recipient", "reminder": "review the report"},
        domain="notifications",
        emotional_state="calm",
    )
    print(f"  ✓ Handler executed, result: {result}")

    # ── Scenario 2: task submission: approved, handler runs ─────────────

    print("\n[SCENARIO 2] Task submission requested: expected: submitted")

    result = orchestrator.process(
        input_data={"queue": "example-queue", "instability": 0.6},
        domain="tasks",
        emotional_state="distressed",
    )
    print(f"  ✓ Handler executed, result: {result}")

    # ── Scenario 3: blocked BEFORE the handler ever runs ────────────────────
    # A strict ruleset + crisis state pushes the score past reject_threshold.
    # simulated_message_client() is NEVER called: no SMS is actually sent.

    print("\n[SCENARIO 3] SMS attempted during crisis, strict ruleset: expected: BLOCKED")

    strict_ruleset = EthicalRuleset(name="strict", reject_threshold=3.0, warn_threshold=1.5)
    strict_executor = SETTExecutor()
    strict_executor.register_handler("send_sms", simulated_message_client)

    strict_orchestrator = SETTOrchestrator(ethical_filter=EthicalFilter(ruleset=strict_ruleset))
    strict_orchestrator.register_executor(strict_executor)
    strict_orchestrator.register_agent(NotificationAgent())

    try:
        strict_orchestrator.process(
            input_data={"phone": "example-recipient", "reminder": "urgent"},
            domain="notifications",
            emotional_state="crisis",
        )
        print("  Handler ran (unexpected)")
    except SETTEthicalFilterRejectedError as e:
        print("  ✗ BLOCKED before the handler ran: no SMS was sent.")
        print(f"    Reason: {str(e)[:120]}...")

    # ── Scenario 4: no handler registered: fails closed, not open ──────────

    print("\n[SCENARIO 4] Action type with no registered handler: expected: SETTConfigurationError")

    bare_executor = SETTExecutor()  # no handlers registered at all
    bare_orchestrator = SETTOrchestrator(ethical_filter=EthicalFilter())
    bare_orchestrator.register_executor(bare_executor)
    bare_orchestrator.register_agent(NotificationAgent())

    try:
        bare_orchestrator.process(
            input_data={"phone": "example-recipient", "reminder": "test"},
            domain="notifications",
        )
        print("  Handler ran (unexpected)")
    except SETTConfigurationError as e:
        print("  ✗ Failed closed: no handler means no side effect, not a silent no-op.")
        print(f"    Reason: {str(e)[:120]}...")

    # ── Executor audit log ───────────────────────────────────────────────────

    print(f"\n{'═' * 55}")
    print("  Executor Audit Log (only approved + executed actions)")
    print(f"{'═' * 55}")
    for entry in executor.get_audit_log():
        print(f"  [{entry['action_type']}] proposed_by={entry['proposed_by']}")

    print(f"\n{'═' * 55}")
    print("  Key takeaway:")
    print("  NotificationAgent and TaskAgent never import an SMS or")
    print("  task-service SDK. The only code that can call")
    print("  simulated_message_client() or simulated_task_client() is the")
    print("  Executor: and only after the EthicalFilter approves it.")
    print(f"{'═' * 55}\n")

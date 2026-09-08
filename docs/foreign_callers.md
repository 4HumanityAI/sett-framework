# Governing a foreign caller

Status: current framework contract
Introduced: SETT Framework v0.14.0

SETT governs effects, not frameworks. A tool call from any external agent
framework can be routed through SETT without SETT knowing that framework
exists, because the boundary between them is one call whose only types are
a string and a JSON-safe dictionary.

This document states what that boundary guarantees, what it does not, and
which direction the dependency runs. `examples/foreign_tool_loop.py` is the
runnable form of everything below.

## The boundary

```python
executor.submit(Action(action_type=name, payload=arguments))
```

`action_type` is the tool's name as the caller already knows it. `payload`
is its arguments. Both are plain Python: no SETT type crosses into the
caller, and no caller type crosses into SETT.

The integration a foreign framework needs is therefore one function:

```python
def dispatch(name, arguments):
    return executor.submit(Action(action_type=name, payload=arguments))
```

That function belongs in the caller's code, not in SETT.

## Dependency direction

SETT never imports an agent framework, an LLM client, or a tool registry.
It has zero mandatory dependencies and keeps them at zero. The adapter that
knows what a LangChain tool or a CrewAI task looks like lives on the caller's
side, so when that framework changes its interface, the caller's adapter
breaks and SETT's release does not.

This is a deliberate limit, not an omission. A bridge shipped inside SETT
would couple SETT's release cadence to somebody else's public API, and the
governance guarantees below would then depend on a third party's decisions.

## What the boundary guarantees

**A refusal is an exception, never a return value.** A caller that ignores
what `submit()` returns still cannot proceed past a refusal. This is the
guarantee the whole pattern rests on: governance that a caller can silently
discard is not governance.

**Refusal happens before the handler runs.** No registered handler is
invoked for a refused action. The effect does not occur and is not partially
applied.

**An unconfigured path fails closed.** An executor that is not attached to an
orchestrator, an action with no registered handler, and an executor
configured for authorization with no policy for that action type all refuse.
There is no permissive default and no per-call bypass.

**Every attempt is recorded.** Permitted and refused attempts both reach the
execution trace and, where an ethical verdict was produced, the hash-chained
ethical audit log. `examples/governance_card.py` exports both from one run.

**The payload does not leak into evidence.** Trace attributes are reduced to
shape in sanitized views, and sensitive keys are rejected outright.

## What the boundary does not guarantee

**SETT does not know what your tool means.** The default numeric harm score
comes from `ContextAnalyzer` keyword heuristics over the action string. A
tool named `send_email` or `delete_file` scores near zero. Routing foreign
tools through the default analyzer and expecting meaningful verdicts is the
most common way to end up with governance that runs and decides nothing.

Give the boundary something deterministic to judge:

- register an `AuthorizationPolicy` for the action types you care about, which
  is code you write and which returns `GRANT`, `REQUIRE_APPROVAL` or `DENY`
  with receipts and reason codes; or
- subclass `ContextAnalyzer` with domain logic and supply it to the filter.

Authorization is the stronger option for foreign callers, because a policy
reads the resolved request rather than guessing from a name.

**SETT does not sandbox your handler.** Once an action is permitted, the
handler runs with whatever privileges the process has.

**SETT does not authenticate a principal.** Identity resolution happens
before the boundary; the authorization request declares provenance for what
the application already established.

**Tamper evidence is not a signature.** The chains detect modification; they
do not prove authorship and do not withstand an attacker who can rewrite the
complete chain and its sink.

## Stability

`Action(action_type, payload)` and the refusal-as-exception rule are stable
public contract. The optional arguments to `submit()` are additive and a
caller that passes none keeps working.

## Failure taxonomy at the boundary

| Exception | Meaning for the caller |
|---|---|
| `SETTEthicalFilterRejectedError` | The effect was judged harmful; do not retry unchanged |
| `SETTAuthorizationError` | Authority was not granted for this exact invocation |
| `SETTConfigurationError` | The path is not wired: no handler, or no attached orchestrator |
| `SETTCancelledError` | The run was cancelled before the effect |
| `SETTTimeoutError` | The run's deadline passed before the effect |

Every one of them means the same thing operationally: the effect did not
happen.

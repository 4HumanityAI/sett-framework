# API Reference

Complete reference for all public classes and functions in the SETT framework.

The framework's root API can be imported directly from `sett`:

```python
from sett import SETTOrchestrator, SETTAgent, SETTExpert, EthicalFilter, RiskLevel, ...
```

Narrow extension contracts also have stable submodule APIs. In particular,
durable backend, format, and migration contracts are exported by
`sett.persistence`, while integrity and recovery types are exported by
`sett.audit_ruler`.

## Adapter contracts and resilience

`AdapterCapabilities`, `AdapterRequirements`, `AdapterHealth`,
`AdapterHealthStatus`, `AdapterErrorView`, `AdapterHandle`, and
`AdapterSelection` provide immutable, provider-neutral descriptions.
`AdapterKind` identifies LLM, TTS, STT, and sentiment interfaces;
`ConnectivityRequirement` identifies none, local-service, remote-service, or
unknown connectivity. `select_adapter(candidates, requirements, *,
policy=None)` filters without performing I/O and requires a caller-owned
`SelectionPolicy` such as `PrioritySelectionPolicy` to break a tie.
`CapabilitySupport` distinguishes `YES`, `NO`, and `UNKNOWN`.

`SETTAdapterError` is the common parent of `SETTLLMAdapterError` and
`SETTServiceAdapterError`. `AdapterErrorKind` provides authentication, quota,
provider-timeout, unavailable, invalid-response, unsupported-capability,
configuration, and unknown categories.

`CircuitBreaker`, `CircuitBreakerPolicy`, `CircuitState`, and
`execute_with_resilience()` are opt-in. A circuit's recovery timeout starts at
its transition to OPEN; later failures from calls already in flight are counted
without postponing recovery. A failed HALF_OPEN probe starts a new timeout.
Retry also requires an explicit `RetryPolicy` and an idempotency statement. The compatibility module
`sett.core_ruler.retry` continues to re-export the retry API; new code may use
`sett.core_ruler.resilience`.

Concrete voice adapters are exported by `sett.services_tts_stt`. See
[Adapter contracts](adapters.md) for the complete roster and the reusable
`sett.testing` contract kit.

---

## core_ruler

### SETTOrchestrator

The central coordinator of any SETT system. Holds all registered agents, manages universal memory, and applies the EthicalFilter before any action or memory write is committed.

```python
SETTOrchestrator(
    ethical_filter=None,
    *,
    lifecycle_policy=None,
    persistence_backend=None,
    persistence_policy=None,
)
```

| Parameter | Type | Description |
|---|---|---|
| `ethical_filter` | `EthicalFilter \| None` | The filter to use. Defaults to `EthicalFilter()` with the default SETT ruleset. |
| `lifecycle_policy` | `LifecyclePolicy \| None` | Optional controlled-execution timeout defaults. Omitting it imposes no timeout. |
| `persistence_backend` | `PersistenceBackend \| None` | Optional namespace-bound evidence and state backend. Must be supplied together with `persistence_policy`. |
| `persistence_policy` | `PersistencePolicy \| None` | Explicit durability, integrity, and audit-gap choices. Must be supplied together with `persistence_backend`; SETT supplies no defaults for these application decisions. |

**Methods**

---

`register_agent(agent)` → `None`

Register an agent with the orchestrator. Connects the agent to universal memory automatically.

| Parameter | Type | Description |
|---|---|---|
| `agent` | `SETTAgent` | The agent to register. Its `domain` must be unique within this orchestrator. |

---

`get_agent(domain)` → `SETTAgent`

Retrieve a registered agent by domain.

| Parameter | Type | Description |
|---|---|---|
| `domain` | `str` | The domain key of the agent to retrieve. |

Raises `SETTAgentNotFoundError` if no agent is registered for the given domain.

---

`process(input_data, domain=None, emotional_state="unknown", location_id="global", *, execution_context=None)` → `dict`

Process input through the system. Routes to a specific agent if `domain` is given; broadcasts to all agents if not.

| Parameter | Type | Description |
|---|---|---|
| `input_data` | `dict` | The data to process. |
| `domain` | `str \| None` | If provided, routes to that agent only. |
| `emotional_state` | `str` | Detected emotional state of the user. Passed to the EthicalFilter. |
| `location_id` | `str` | Environmental-context location key. |
| `execution_context` | `ExecutionContext \| None` | Optional explicit root context. A fresh root is created when omitted. |

Returns the result dict from the agent (or a `{domain: result}` dict when broadcasting).

Raises `SETTEthicalFilterRejectedError` if the EthicalFilter blocks any agent's action.

---

`process_traced(input_data, domain=None, emotional_state="unknown", location_id="global", *, execution_context=None)` → `TracedResult`

Runs the same path as `process()` and returns an immutable envelope containing
`result`, `trace_id`, and the root `context`.

---

`get_trace(trace_id)` → `tuple[TraceEvent, ...]`

Return immutable structured events for one trace.

---

`export_trace(trace_id, *, view="sanitized")` → `tuple[dict, ...]`

Return defensive privacy-safe dictionaries. `view` is `"sanitized"` or
`"summary"`.

---

`register_trace_exporter(exporter)` → `None`

Register a callable that receives each defensive sanitized event. Exporter
failure at the pre-effect handler boundary blocks the handler.

---

`verify_traces(trace_id=None)` → `bool`

Verify sequence, SHA-256 links, causes, and parent execution nodes. With no
identifier, verifies the complete recorder.

---

`last_trace_id` → `str | None`

Most recently started trace. Intended for sequential debugging; use explicit
contexts or `process_traced()` in concurrent callers.

---

`read_universal_memory()` → `dict`

Snapshot of all agent results currently in universal memory. Does not include environmental context entries.

---

### ExecutionContext

Immutable identity for one execution node.

```python
ExecutionContext.create(
    trace_id=None,
    run_id=None,
    application_id=None,
    instance_id=None,
    subject_id=None,
    session_id=None,
    turn_id=None,
    metadata=None,
)
```

| Field | Meaning |
|---|---|
| `trace_id` | Complete causal execution tree. |
| `run_id` | This execution node. |
| `parent_id` | Immediate parent node's `run_id`, or `None` for a root. |
| `created_at` | Timezone-aware UTC creation time. |
| optional identifiers | Opaque application correlation values. |
| `metadata` | Recursively frozen, validated JSON-safe metadata. |

`derive(*, metadata=None)` creates a child with the same trace ID, a new run
ID, and the current run ID as parent.

`safe_view()` returns a defensive JSON-safe representation.

`current_execution_context()` returns the context active in the current
thread/task, or `None`.

### TracedResult

Immutable envelope with `result`, `trace_id`, and root `context`.

### TraceEvent

Immutable event containing execution identity, immediate `cause_id`, timestamp,
sequence, kind, component, status, reason codes, safe attributes, and hash-chain
fields. Attributes are recursively immutable, including nested mappings and
sequences.

### TraceRecorder

Thread-safe recorder used by each orchestrator. It remains in memory unless an
`EvidenceStream` is configured. Public methods:
`record()`, `get_trace()`, `export_trace()`, `register_exporter()`, `verify()`,
`last_event_id()`, `restore()`, and `resume()`. `record()` rejects sensitive keys, non-finite numbers,
unsupported objects, excessive depth, more than 32 keys, and more than 16 KiB
of serialized attributes. `verify()` checks hashes and sequence as well as
duplicate IDs, same-trace earlier causes, parent-run integrity, and terminal
closure for instrumented boundaries.

When durable evidence is configured, event vocabulary and complete frame size
are bounded as documented in the changelog. An invalid start is rejected before
it opens a boundary. If requested metadata for a known terminal is invalid, the
recorder writes an in-contract terminal with the same outcome,
`audit.terminal_metadata_rejected`, framework-owned component fields, and only
the discarded reason-code and attribute counts. No rejected content is copied.
`RecoveredTrace.terminal_metadata_fallback_event_ids` identifies these events,
and `terminal_metadata_complete` is false even though `trace_completeness` can
remain true.

`verify(trace_id=None)` semantics (F28, defensive audit #2): the hash chain
this recorder maintains is global, over every event it has ever recorded,
not one chain per trace. Passing `trace_id` only adds a check that at least
one event with that `trace_id` exists in the chain; it does not scope the
verification to that trace's events alone. Corruption anywhere in the
recorder's history, including in an unrelated trace, will make
`verify(trace_id="...")` return `False` for every trace_id, not just the
one that was actually corrupted. Call `verify()` with no argument if the
question is "is this recorder's history intact," and treat a per-trace
`False` as "something in this recorder is broken," not "this specific
trace was tampered with."

An interrupted run with an open instrumented boundary also makes `verify()`
return `False`; that result alone does not distinguish interruption from
corruption. After durable recovery, use `RecoveryReport.chain_integrity` and
the per-trace completeness fields to distinguish those two axes.

Applications normally use the orchestrator's trace methods instead of
constructing a recorder directly.

When durable persistence is configured, `restore()` reads and verifies one
stable, contiguous evidence prefix without writing to it. It returns a
`RecoveryReport`; it does not resume interrupted work. `resume()` creates a
recorder only from an intact report whose backend position still identifies
the current stream head. Events buffered in a process that did not reach the
durable stream are not replayed after restart.

Once an append returns a receipt, the stream is authoritative even if the
event's later durability confirmation fails. The recorder does not publish an
unconfirmed anchor in its local view and rejects further events until the
application performs explicit `restore()` and `resume()`. An append failure
without a receipt is stricter still because both acceptance and position are
uncertain. Neither case can continue from an older local hash.

---

`publish_environmental_context(risk_level, location_id="global", source_domain="orchestrator", message="")` → `EnvironmentalContext`

Publish an environmental risk level to a shared location slot. Other SETT instances reading the same `location_id` will see this context and their filters will tighten accordingly.

| Parameter | Type | Description |
|---|---|---|
| `risk_level` | `RiskLevel` | The risk level to publish. |
| `location_id` | `str` | Identifier of the shared space (e.g. `"store_42"`). Defaults to `"global"`. |
| `source_domain` | `str` | The agent domain that triggered this (e.g. `"health"`). |
| `message` | `str` | Optional description. Free text, not validated: the caller is responsible for keeping it free of personal data. |

---

`read_environmental_context(location_id="global")` → `EnvironmentalContext | None`

Read the current environmental context for a location. Returns `None` if no context has been published for that location.

---

`read_all_environmental_contexts()` → `dict[str, EnvironmentalContext]`

Return all published environmental contexts, keyed by `location_id`.

---

`get_ethical_audit_log()` → `list[dict]`

Return the full audit log of every ethical decision made since this orchestrator was created. Each entry contains: `sequence`, `previous_hash`, `entry_hash`, `timestamp`, `action`, `harm_score`, `verdict`, `emotional_state`, `human_at_risk`, `situation_urgency`, `action_harm_risk`, `omission_risk`, `protective_action`, `env_risk_level`, `env_modifier`, `effective_reject_threshold`, `effective_warn_threshold`, `decision_reason_codes`, and `reasoning`. The first three fields are the SHA-256 hash chain added in v0.8.0 (see `verify_ethical_audit_log()` below).

---

`verify_ethical_audit_log()` → `bool`

Verify the sequence and hash chain of the log returned by `get_ethical_audit_log()`. Delegates to the underlying `EthicalFilter.verify_audit_log()`, reachable here so tamper-evidence can be checked from the orchestrator itself: the entry point every example in this document already uses, without needing access to the filter directly. Returns `True` if the chain is intact, `False` if any entry was altered outside of `append_chained_entry()`. Tamper-evident within the running process only; this is not an external cryptographic signature.

---

`registered_domains` *(property)* → `list[str]`

List of all registered agent domains.

---

`run_pipeline(steps, input_data, emotional_state="unknown", location_id="global")` → `PipelineResult`

Run an ordered sequence of stages, each handled by a different registered agent, with **explicit data flow** between stages. This is an additive capability: `process()` (route-to-one / broadcast-to-all) is unchanged. Each stage executes through the same path as routed processing: same propagation of `emotional_state` / `location_id`, same `EthicalFilter` evaluation on publish, same audit log entries.

`steps` is a list where each element is a `PipelineStep(domain, transform=None)` or a plain domain string (shorthand for a step without transform). By default the first stage receives `input_data` and every later stage receives the previous stage's output; a `transform(original_input, prev_output) -> dict` reshapes the stage's input when needed.

Three guarantees define the mechanism:

1. **Memory isolation between stages.** Stage inputs are passed hand-to-hand, never read from universal memory. Each agent keeps its own `PrivateMemory`; no stage sees another stage's intermediate reasoning.
2. **Fail-closed configuration.** All stage domains are validated *before* the first stage runs (`SETTAgentNotFoundError`), an empty pipeline raises `SETTConfigurationError`, and a transform returning a non-dict raises `SETTConfigurationError`. A misconfigured pipeline never produces partial side effects.
3. **Rejection handling as part of the mechanism.** If the `EthicalFilter` rejects a stage: that agent publishes nothing, the remaining stages are marked `"skipped"`, and the rejection is returned **explicitly** in `PipelineResult.rejection` as a `RejectionOutcome` carrying the structured fields (`action`, `score`, `threshold`, `principle`, `reasoning`, `message`) taken directly from the exception's attributes. The rejection is never written to, nor meant to be read from, universal memory: the caller that synthesizes the final response receives it directly.

Return types (all frozen dataclasses, importable from `sett`):

| Type | Fields |
|---|---|
| `PipelineStep` | `domain`, `transform`, `timeout_seconds` (v0.12.0, only on the controlled path) |
| `StageOutcome` | `domain`, `status` (`"completed"` / `"rejected"` / `"skipped"`, plus `"cancelled"` / `"timed_out"` on the controlled path), `output`, `rejection`, and three v0.12.0 additions: `execution_status`, `reason_code`, `skip_cause` (see `SkipCause` below) |
| `RejectionOutcome` | `domain`, `action`, `score`, `threshold`, `principle`, `reasoning`, `message` |
| `PipelineResult` | `completed`, `steps`, `output` (final stage's output when completed), `rejection` |
| `SkipCause` (v0.12.0) | `domain`, `run_id`, `reason_code` - which stage made a later one unreachable, and why. Set only on a `StageOutcome` whose `status` is `"skipped"`. A cancellation tree is not a dependency graph: a stage that never ran because an earlier one stopped was not itself cancelled, so this is a reference to the stage that stopped, not a status on the one that never started. |

All three v0.12.0 additions on `StageOutcome` default to `None` and are appended last, so existing positional construction is unaffected. `execution_status` is `None` exactly when the stage never executed; there is no `ExecutionStatus.SKIPPED` (see below), since giving a stage that never ran an execution outcome would claim somebody stopped it. A genuine stage failure does not discard the outcomes already collected from earlier, completed stages: `ExecutionResult.value` on a `FAILED` controlled-pipeline result still carries the `PipelineResult` with those stages' outputs, the same guarantee `PARTIAL` makes.

```python
from sett import PipelineStep

result = orchestrator.run_pipeline(
    ["price", "promotions", PipelineStep(
        domain="budget",
        transform=lambda original, prev: {**prev, "limit": original["limit"]},
    )],
    input_data={"items": [...], "limit": 150.0},
    emotional_state="calm",
)

if result.completed:
    final = result.output
else:
    r = result.rejection            # explicit hand-off: not in memory
    notify_user(r.principle, r.score)   # structured, no string parsing
```

---

### SETTAgent

Abstract base class for all SETT agents. Extend this class and implement `process()` to create a domain specialist.

```python
SETTAgent(name, domain, *, private_memory_id=None)
```

| Parameter | Type | Description |
|---|---|---|
| `name` | `str` | Human-readable name (e.g. `"HealthAgent"`). |
| `domain` | `str` | Domain key used by the orchestrator for routing (e.g. `"health"`). |
| `private_memory_id` | `str \| None` | Stable, opaque identity that opts this agent's `PrivateMemory` into persistence when its orchestrator has a persistence backend. |

**Methods**

---

`register_expert(expert)` → `None`

Register an expert with this agent. Gives the expert access to this agent's private memory. Call this in `__init__` for each expert the agent uses.

---

`get_expert(name)` → `SETTExpert`

Retrieve a registered expert by name. Raises `SETTExpertNotFoundError` if not found.

---

`process(input_data)` → `dict` *(abstract: must be implemented)*

Main processing method. Coordinate experts, use private memory for intermediate state, compose a final result, call `_publish_to_universal(result)`, and return the result.

| Parameter | Type | Description |
|---|---|---|
| `input_data` | `dict` | The data this agent needs to process. |

---

`_publish_to_universal(result, risk_profile=None)` → `None`

Publish the agent's final result to universal memory. This is the only way an agent communicates outward. The result passes through the EthicalFilter before being stored: `emotional_state` and the `EnvironmentalContext` for the agent's current location are forwarded automatically. Call this at the end of `process()`.

Note: this only evaluates a result AFTER it exists. It does not gate real-world side effects (sending a message, calling an API) that may have already happened inside `resolve()`/`process()`. For that, use `propose_action()` or `submit_action()` below.

---

`propose_action(action, action_context=None, risk_profile=None)` → `None`

Gate a real-world side effect through the `EthicalFilter` **before** it is executed: call this from inside `resolve()`/`process()`, before performing the effect yourself. Lightweight and opt-in: the developer must remember to call it. Raises `SETTEthicalFilterRejectedError` if blocked; the side effect must not be performed in that case.

---

`submit_action(action_type, payload=None, risk_profile=None)` → `Any`

Structural alternative to `propose_action()`: describes the effect as an `Action` and submits it to the `SETTExecutor` registered with this orchestrator (see the `Action` / `SETTExecutor` section below). The agent never holds a reference to the real client: only the Executor's registered handler does, and only if the `EthicalFilter` approves. Raises `SETTConfigurationError` if no `Executor` (or no handler for this `action_type`) is registered.

---

`experts` *(property)* → `list[str]`

Names of all registered experts.

**Minimal implementation**

```python
class MyAgent(SETTAgent):
    def __init__(self):
        super().__init__(name="MyAgent", domain="my_domain")
        self.register_expert(MyExpert(name="my_expert"))

    def process(self, input_data):
        result = self.get_expert("my_expert").resolve(input_data)
        self._publish_to_universal(result)
        return result
```

---

### SETTExpert

Abstract base class for all SETT experts. Extend this class and implement `resolve()` to create a specialized module.

```python
SETTExpert(name)
```

| Parameter | Type | Description |
|---|---|---|
| `name` | `str` | Unique name within the parent agent. Used to retrieve this expert via `agent.get_expert(name)`. |

**Methods**

---

`resolve(context)` → `dict` *(abstract: must be implemented)*

Main method of the expert. Process the context, write relevant state to private memory via `self._private_memory`, and return a result dict.

| Parameter | Type | Description |
|---|---|---|
| `context` | `dict` | Input data provided by the parent agent. |

---

`attach_memory(memory)` → `None`

Called automatically by the parent agent during `register_expert()`. Do not call manually.

---

`_private_memory` *(attribute)* → `PrivateMemory | None`

The agent's private memory. Available after `attach_memory()` is called (i.e. after `register_expert()`). Always check `if self._private_memory:` before writing.

**Minimal implementation**

```python
class MyExpert(SETTExpert):
    def resolve(self, context):
        value = context.get("input", "")
        if self._private_memory:
            self._private_memory.write("last_input", value)
        return {"processed": value.upper()}
```

---

### PhrasingExpert

Base class for any expert whose job includes producing text the user will actually read or hear (a greeting, an acknowledgment, a synthesized summary, a redacted alert). Formalizes a pattern discovered independently twice while building an early companion-assistant prototype, before either instance was planned as reusable.

```python
from sett import PhrasingExpert

PhrasingExpert(name, llm=None)
```

Subclasses implement up to four methods instead of `resolve()` directly (the first three required, the fourth optional): `resolve()` is a template method you should not override:

| Method | Description |
|---|---|
| `determine_facts(context)` → `dict` | Pure deterministic logic: no LLM involved. Returns what's true regardless of how it ends up phrased. |
| `build_prompt(facts, context)` → `str` | Describes, in natural language, what the LLM should say based on the facts already computed. Never pass raw, unprocessed context the LLM could misread as license to invent additional facts. |
| `fallback_text(facts, context)` → `str` | The deterministic text used when there's no LLM configured, or when the call fails for any reason. Must always produce a valid result on its own. |
| `verify_facts(phrased, facts, context)` → `str` (optional, added in 0.10.0) | Called after `phrased` already exists (from the LLM or from `fallback_text()`), to validate the actual text against facts already known and swap it out if it contradicts them. Default: returns `phrased` unchanged. |

| Class attribute | Description |
|---|---|
| `OUTPUT_KEY` | The key the phrased text is merged under in the result dict. Override per subclass (e.g. `"greeting"`, `"summary"`). Defaults to `"text"`. |
| `SYSTEM_PROMPT` | The system prompt sent to the LLM. Override per subclass/persona. |

**Contract:** the LLM only *phrases* facts your deterministic logic already produced: it never invents or alters them. If no `llm` is given, or the adapter raises `SETTLLMAdapterError`, `PhrasingExpert` falls back to `fallback_text()` automatically; a broken or absent LLM never prevents an agent from responding.

**Minimal implementation**

```python
class GreetingExpert(PhrasingExpert):
    OUTPUT_KEY = "greeting"

    def determine_facts(self, context):
        hour = context.get("hour", 9)
        return {"time_of_day": "morning" if hour < 12 else "afternoon"}

    def build_prompt(self, facts, context):
        return f"Greet the user. It's {facts['time_of_day']}."

    def fallback_text(self, facts, context):
        return {"morning": "Good morning.", "afternoon": "Good afternoon."}[facts["time_of_day"]]

expert = GreetingExpert(name="greeting", llm=OllamaAdapter())
result = expert.resolve({"hour": 8})
# {"time_of_day": "morning", "greeting": "<LLM-phrased or fallback text>"}
```

---

### StubDomainAgent

A generic, ready-to-use placeholder agent for a domain that isn't built yet. Unlike the templates in `templates/`, this needs no subclassing or customization: import it and use it directly.

```python
from sett import StubDomainAgent

StubDomainAgent(domain, name=None)
```

Useful when assembling a multi-agent system incrementally: register a `StubDomainAgent` for every domain your router or synthesizer needs to be able to call, so the full flow (dispatch → collect → synthesize) is testable end to end before any of the real agents exist. Swap in the real agent later by registering it under the same domain: since `SETTOrchestrator.register_agent()` keys agents by domain, the new registration replaces the stub with no other change required anywhere else in the system.

`process()` returns `{"status": "stub", "domain": ..., "received": <input_data>}`: an honest, structured result a downstream synthesizer can narrate as "not built yet" instead of crashing or fabricating an answer.

```python
orchestrator.register_agent(StubDomainAgent("health"))
# ... later, once HealthAgent exists:
orchestrator.register_agent(HealthAgent())  # replaces the stub
```

---

## memory_ruler

### PrivateMemory

Exclusive memory belonging to one agent. Only the experts within that agent are given a reference to it, and the orchestrator is never given one. This is an API-surface convention rather than a runtime capability check (F24, defensive audit #2). See PrivateMemory's own docstring for the full rationale.

```python
PrivateMemory(owner, *, memory_id=None)
```

| Parameter | Type | Description |
|---|---|---|
| `owner` | `str` | The name of the agent that owns this memory. |
| `memory_id` | `str \| None` | Stable, opaque identity used only when persistence is explicitly configured. |

**Methods**

| Method | Returns | Description |
|---|---|---|
| `write(key, value, *, context=None)` | `None` | Store any Python value in ephemeral mode. Persistent mode requires strict JSON and a real ambient or explicit `ExecutionContext`. |
| `read(key, default=None)` | `Any` | Read a value by key. Returns `default` if not found. Returns a **deep copy** (since v0.9.0), so mutating the returned value never affects stored state. |
| `get_all()` | `dict` | **Deep copy** of all stored values (since v0.9.0). |
| `clear(*, context=None)` | `None` | Remove all values. Persistent mode requires the same causal context as `write()`. |
| `get_history()` | `list[dict]` | Full write history for auditing. |
| `owner` *(property)* | `str` | The name of the owning agent. |
| `memory_id` *(property)* | `str \| None` | The configured durable identity, or `None` in ordinary ephemeral mode. |

`configure_persistence(backend=..., policy=..., trace_recorder=...)` must run
before the first mutation and requires `memory_id`. Applications normally let
`SETTOrchestrator.register_agent()` perform this attachment for agents that
opted in through `private_memory_id`.

`PrivateMemory.restore(owner=..., memory_id=..., report=..., backend=...,
policy=..., historical_policies=None)` verifies and replays confirmed durable
mutations, then resumes persistence from the report's stream head. It never
restores a mutation whose confirmation is absent from durable evidence.

---

### UniversalMemory

Shared memory accessible by all agents and the orchestrator. Every write passes through the EthicalFilter if one is configured. Also stores environmental context for multi-instance coordination.

Instantiated automatically by `SETTOrchestrator`. You do not normally need to create one directly.

**Methods**

| Method | Returns | Description |
|---|---|---|
| `update(agent, result)` | `None` | Publish an agent's final result. Passes through EthicalFilter. |
| `read(agent, default=None)` | `Any` | Read the latest result from a specific agent. |
| `read_all()` | `dict` | Snapshot of all agent results. Excludes environmental context entries. |
| `publish_environmental_context(context)` | `None` | Publish an `EnvironmentalContext` to a shared location slot. |
| `read_environmental_context(location_id)` | `EnvironmentalContext \| None` | Read the context for a location. Returns `None` if not set. |
| `read_all_environmental_contexts()` | `dict` | All published contexts, keyed by `location_id`. |
| `get_history()` | `list[dict]` | Full write history. |

**`ENV_CONTEXT_PREFIX`** (v0.12.0)

```python
from sett.memory_ruler.universal import ENV_CONTEXT_PREFIX
```

The reserved key prefix under which environmental contexts are stored. An
agent domain beginning with it is refused at `update()`, because before
v0.12.0 such a domain was stored as an agent result, hidden from
`read_all()`, and read back as an environmental context. Exported as a
named constant rather than left as a literal so an application can check
a domain against it instead of hardcoding the same string.

---

## risk_ruler

### RiskLevel

Six-level environmental risk scale. Describes the state of the environment a user is in: not the user themselves.

```python
class RiskLevel(IntEnum):
    LEVEL_0 = 0   # Normal: baseline operation
    LEVEL_1 = 1   # Attention: anomaly detected
    LEVEL_2 = 2   # Warning: controlled threat
    LEVEL_3 = 3   # Danger: active threat, prepare for response
    LEVEL_4 = 4   # Critical: immediate action required
    LEVEL_5 = 5   # Emergency: maximum protocol
```

**Properties**

| Property | Type | Description |
|---|---|---|
| `label` | `str` | Human-readable name (e.g. `"Critical"`). |
| `description` | `str` | Full description of what this level means. |
| `emoji` | `str` | Visual indicator (e.g. `"🛑"`). |
| `color` | `str` | Hex color code for UI use. |
| `is_elevated()` | `bool` | `True` for any level ≥ `LEVEL_1`. |
| `is_critical()` | `bool` | `True` for `LEVEL_4` and `LEVEL_5`. |

`RiskLevel` values are comparable integers: `RiskLevel.LEVEL_3 > RiskLevel.LEVEL_1`.

---

### RiskProfile

Three-pillar user risk assessment. Stored exclusively in `PrivateMemory`. Never published to `UniversalMemory`.

```python
RiskProfile(
    emotional_instability=0.0,
    influence_vulnerability=0.0,
    collateral_damage_potential=0.0,
)
```

All values are floats in `[0.0, 1.0]`. Raises `ValueError` if any value is out of range.

| Pillar | Description |
|---|---|
| `emotional_instability` | Propensity to irrational or self-destructive behavior under current stress. |
| `influence_vulnerability` | Susceptibility to external manipulation in the current state. |
| `collateral_damage_potential` | Potential impact of this user's decisions on their environment. |

**Properties**

| Property | Type | Description |
|---|---|---|
| `composite_score` | `float` | Weighted combination of the three pillars (0.0–1.0). Weights: instability 45%, collateral 30%, vulnerability 25%. |
| `suggested_level` | `RiskLevel` | Suggested environmental `RiskLevel` based on `composite_score`. |
| `dominant_pillar` | `str` | Name of the pillar with the highest value. |

**Class methods**

| Method | Description |
|---|---|
| `RiskProfile.baseline()` | Returns a neutral profile with all pillars at 0.0. |
| `RiskProfile.from_dict(data)` | Reconstruct from a dict stored in `PrivateMemory`. |

**Instance methods**

| Method | Description |
|---|---|
| `to_dict()` | Serialize for storage in `PrivateMemory`. |

---

### EnvironmentalContext

Shared environmental state published by one SETT instance and readable by all instances in the same location. Shaped to carry only non-personal signals (risk level, location, source domain, timestamp) - `location_id`, `source_domain`, and `message` are plain strings with no runtime content check, so keeping personal data out of them is the caller's responsibility, not something this class enforces.

```python
EnvironmentalContext(
    risk_level,
    location_id="global",
    source_domain="unknown",
    message="",
)
```

| Parameter | Type | Description |
|---|---|---|
| `risk_level` | `RiskLevel` | The current risk level of this environment. |
| `location_id` | `str` | Identifier of the shared space (e.g. `"store_42"`). |
| `source_domain` | `str` | The agent domain that published this (e.g. `"health"`). Never a user identifier. |
| `message` | `str` | Optional description. Free text, not validated: the caller is responsible for keeping it free of personal data. |

**Properties**

| Property | Type | Description |
|---|---|---|
| `requires_response` | `bool` | `True` for level ≥ 2. |
| `requires_evacuation` | `bool` | `True` for level ≥ 4. |
| `should_notify_emergency` | `bool` | `True` for level ≥ 4. A recommendation, not an effect: setting this field never contacts anyone. Submit an explicit `Action` through `SETTExecutor` if the notification itself needs to happen. |
| `is_systemic_emergency` | `bool` | `True` only for level 5. |
| `filter_threshold_modifier` | `float` | How much this context tightens the EthicalFilter thresholds (0.0–4.0). |

**Class methods**

| Method | Description |
|---|---|
| `EnvironmentalContext.normal(location_id)` | Returns a baseline level-0 context for a location. |
| `EnvironmentalContext.from_dict(data)` | Reconstruct from `UniversalMemory` storage. |

**Privacy contract:** `EnvironmentalContext` never contains personal identifiers, biometric values, or `RiskProfile` data. It communicates "the environment has this level": never "this person has this profile".

---

## biometric_ruler

### BiometricReading

Structural data model for physical vital-sign readings: same role for biometrics that `RiskProfile`/`EnvironmentalContext` play in `risk_ruler`: a typed value object `ethics_ruler` consumes, not a decision-maker itself. Extracted from `ContextAnalyzer._detect_human_at_risk`, which read `context["health"]`/`context["heart_rate_bpm"]` directly before this pillar existed.

```python
BiometricReading(
    heart_rate_bpm=None,
    temperature_celsius=None,
)
```

All fields optional and immutable (`frozen=True`). A reading with no data present is valid and never critical.

**Properties**

| Property | Type | Description |
|---|---|---|
| `is_critical` | `bool` | `True` if heart rate is outside 40–150 bpm, or temperature is outside 35.0–39.5°C. |

**Class methods**

| Method | Description |
|---|---|
| `BiometricReading.from_context(context)` | Parse a reading out of an action's `context` dict. Prefers a nested `context["health"]` dict if present and non-empty; otherwise reads `heart_rate_bpm`/`temperature_celsius` directly from the top-level context. This nested/flat fallback is the fix for a real bug (v0.1.1): an agent publishing flat biometric keys was previously invisible to risk detection because only the nested form was checked. |

**Instance methods**

| Method | Description |
|---|---|
| `to_dict()` | Serialize for logging/storage. |

**Privacy note:** `ContextAnalyzer` only ever reads `is_critical` (a bool) from this class, never the raw vital-sign values, so raw biometric data cannot leak into the audit log or `UniversalMemory` through this path, the same structural guarantee `RiskProfile` has for its own pillars.

---

## ethics_ruler

### EthicalFilter

The governance layer of SETT. Intercepts every action and every `UniversalMemory` write. Returns `ALLOW`, `WARN`, or `REJECT`.

```python
EthicalFilter(ruleset=None, context_analyzer=None)
```

| Parameter | Type | Description |
|---|---|---|
| `ruleset` | `EthicalRuleset \| None` | Thresholds and principle the verdict is measured against. Defaults to `default_ruleset()`. |
| `context_analyzer` | `ContextAnalyzer \| None` | Analyzer for the three-layer evaluation. Defaults to `ContextAnalyzer()`. |

**Methods**

---

`evaluate(action, context, emotional_state="unknown", risk_profile=None, environmental_context=None)` → `FilterVerdict`

Evaluate an action through the three-layer system.

| Parameter | Type | Description |
|---|---|---|
| `action` | `str` | Description of what is about to happen. |
| `context` | `dict` | Data associated with this action. |
| `emotional_state` | `str` | Detected emotional state of the user. |
| `risk_profile` | `RiskProfile \| None` | Three-pillar user assessment (Layer 2). |
| `environmental_context` | `EnvironmentalContext \| None` | Shared environmental state (Layer 3). |

Returns `FilterVerdict.ALLOW` or `FilterVerdict.WARN`.
Raises `SETTEthicalFilterRejectedError` if the action is blocked.

The base verdict is derived from action harm score. If `human_at_risk=True`,
`protective_action=False`, and the base verdict would be `ALLOW`, the filter
promotes it to `WARN` without changing the score. The audit entry records
`human_at_risk_without_protective_classification` in
`decision_reason_codes`. Explicitly protective actions are not promoted.

---

`register_analyzer(action_type, analyzer)` → `None`

Registers a domain-specific `ContextAnalyzer` for one exact action type. Real deployments often need more than keyword-based scoring for a specific action, e.g. an economic analyzer for `"confirm_purchase"` that reads `over_budget_amount`, or a health analyzer for `"emergency_call"` that reads vitals directly. Any `action_type` without a registered analyzer keeps using the generic one passed to `__init__` (or the default `ContextAnalyzer`), additive and safe, existing code that never calls this keeps working exactly as before.

| Parameter | Type | Description |
|---|---|---|
| `action_type` | `str` | Must match the exact `action` string passed to `evaluate()`. |
| `analyzer` | `ContextAnalyzer` | The analyzer to use for this action type only. |

```python
filt = EthicalFilter()  # generic analyzer stays the fallback for everything else
filt.register_analyzer("confirm_purchase", EconomicContextAnalyzer())
```

---

`unregister_analyzer(action_type)` → `None`

Removes a previously registered analyzer for an action type: it falls back to the generic analyzer again. Safe to call even if nothing was registered for it.

---

`get_audit_log()` → `list[dict]`

Full audit log of every decision. Each entry includes: `sequence`, `previous_hash`, `entry_hash`, `timestamp`, `action`, `harm_score`, `verdict`, `emotional_state`, `human_at_risk`, `situation_urgency`, `action_harm_risk`, `omission_risk`, `protective_action`, `env_risk_level`, `env_modifier`, `effective_reject_threshold`, `effective_warn_threshold`, `decision_reason_codes`, and `reasoning`.

---

`verify_audit_log()` → `bool`

Verify the sequence and hash chain of the audit log: `True` if every entry's `previous_hash` correctly links to the prior entry's `entry_hash` and no entry was altered outside of the internal `append_chained_entry()` call, `False` otherwise. Also reachable through `SETTOrchestrator.verify_ethical_audit_log()`, which delegates here. Tamper-evident within the running process only: it does not constitute an external cryptographic signature or a durable attestation outside that process.

```python
filt = EthicalFilter()
filt.evaluate("some_action", {})
filt.verify_audit_log()  # True

filt.get_audit_log()[0]["verdict"] = "tampered"  # a copy: does not touch internal state
filt.verify_audit_log()  # still True

filt._audit_log[0]["verdict"] = "tampered"  # reaches the real internal log
filt.verify_audit_log()  # False
```

---

`set_ruleset(ruleset)` → `None`

Replace the active ruleset at runtime.

---

`set_context_analyzer(analyzer)` → `None`

Replace the context analyzer (e.g. to integrate with a Sentiment Analyzer agent).

---

`principle` *(property)* → `str`

The guiding ethical principle of the active ruleset.

---

### FilterVerdict

```python
class FilterVerdict(Enum):
    ALLOW  = "allow"
    WARN   = "warn"
    REJECT = "reject"
```

---

### HarmCategory

```python
class HarmCategory(Enum):
    PHYSICAL       = "physical"       # weight: 10
    PSYCHOLOGICAL  = "psychological"  # weight: 8
    ECONOMIC       = "economic"       # weight: 6
    AUTONOMY       = "autonomy"       # weight: 5
    OMISSION       = "omission"       # weight: 4
    AMBIGUITY      = "ambiguity"      # weight: 2
```

---

### EthicalRuleset

A named collection of ethical rules with configurable thresholds.

```python
EthicalRuleset(
    name,
    principle="Do not cause direct or indirect harm to human beings.",
    rules=[],
    reject_threshold=8.0,
    warn_threshold=4.0,
)
```

| Parameter | Type | Description |
|---|---|---|
| `reject_threshold` | `float` | Score at or above this → `REJECT`. |
| `warn_threshold` | `float` | Score at or above this → `WARN`. |

**Methods**

| Method | Returns | Description |
|---|---|---|
| `add_rule(rule)` | `None` | Add an `EthicalRule` to this ruleset. |
| `get_active_rules()` | `list[EthicalRule]` | Only the rules currently `active`. |
| `get_rule(name)` | `EthicalRule \| None` | Find a rule by name, or `None` if not present. |

Use `default_ruleset()` to get the pre-configured SETT default.

Only `reject_threshold`, `warn_threshold` and `principle` take part in a
verdict. The `rules` list is configuration metadata for human inspection:
adding, removing, reweighting or deactivating rules does not change what the
filter decides, and rule contents are not copied into emitted audit entries.
The numeric score comes from `ContextAnalyzer`; subclass or register an
analyzer when the scoring itself has to change.

---

### EthicalRule

A single ethical rule within a ruleset: its harm category, weight, description, and whether it is currently active.

```python
EthicalRule(name, category, weight, description, active=True)
```

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Unique identifier within the ruleset (e.g. `"no_physical_harm"`). |
| `category` | `HarmCategory` | Which harm category this rule belongs to. |
| `weight` | `float` | Declared severity for human inspection. It does not enter the numeric score. Typically one of the `DEFAULT_HARM_WEIGHTS` values. |
| `description` | `str` | Human-readable explanation of what the rule guards against. It is not copied into audit entries or reasoning strings. |
| `active` | `bool` | Whether this rule appears in `get_active_rules()`. No rule participates in numeric evaluation, so toggling this does not change a verdict. Defaults to `True`. |

**Methods**

| Method | Returns | Description |
|---|---|---|
| `activate()` | `None` | Include this rule in `get_active_rules()` again. |
| `deactivate()` | `None` | Exclude this rule from `get_active_rules()` without removing it. No verdict changes. |

```python
from sett import EthicalRule, HarmCategory

rule = EthicalRule(
    name="no_physical_harm",
    category=HarmCategory.PHYSICAL,
    weight=10.0,
    description="Action must not cause or facilitate physical harm to any person.",
)
ruleset.add_rule(rule)
```

---

### default_ruleset()

```python
from sett import default_ruleset
ruleset = default_ruleset()
```

Returns the default `EthicalRuleset` with metadata covering all six `HarmCategory` values. Adjust `reject_threshold` and `warn_threshold` for a stricter or looser deployment, and subclass or register a `ContextAnalyzer` when scoring itself has to change.

---

### ContextAnalyzer

Evaluates the context of a proposed action using the three-layer system. Subclass this and override `analyze()` to integrate with the Sentiment Analyzer agent, biometric data from wearables, or domain-specific risk logic.

```python
ContextAnalyzer()
```

**Methods**

`analyze(action, context, emotional_state="unknown", risk_profile=None, environmental_context=None)` → `ContextAnalysis`

Performs the full three-layer analysis and returns a `ContextAnalysis` with `risk_score`, `emotional_state`, `human_at_risk`, `reasoning`, `consequences`, `risk_level`, and `safety_assessment`.

**`canonicalize_for_keyword_match(text)`** (v0.12.0) → `str`

```python
from sett.ethics_ruler.ethic_kernel.context_analyzer import (
    canonicalize_for_keyword_match,
)
```

The one spelling Layer 1 matches keywords against: characters of Unicode
category `Cf` dropped, then `NFKC`, then `casefold()`. Without it, `harm`
written with a zero-width space inside it, or `delete` written in
fullwidth characters, scored as ordinary prose. Exported so a domain
analyzer can apply the same rule rather than inventing a second one.

It does **not** defeat homoglyphs, and no keyword list can: Cyrillic `а`
is a different letter from Latin `a`, not a different spelling of it, and
`NFKC` correctly leaves it alone. Layer 1 is fail-honest scaffolding for
an application that has registered no analyzer of its own, a floor rather than
a policy. What guards a critical action is a domain analyzer registered
with `register_analyzer()`, plus `SETTExecutor` resolving handlers by
exact `action_type` and failing closed without one.

---

### ContextAnalysis

Result of a full three-layer context analysis. Not a dataclass (no `@dataclass` decorator), but every field below is a plain public attribute.

| Field | Type | Description |
|---|---|---|
| `action` | `str` | The action that was analyzed. |
| `risk_score` | `float` | Combined harm score (0.0-10.0) that the `EthicalFilter` compares against its thresholds. |
| `emotional_state` | `str` | The emotional state passed in to `analyze()`. |
| `reasoning` | `str` | Human-readable explanation of how the score was reached. |
| `consequences` | `list[str]` | Possible consequences identified for this action. |
| `human_at_risk` | `bool` | Whether a human was determined to be at risk. Defaults to `False`. |
| `risk_level` | `RiskLevel \| None` | The environmental risk level in effect, if any (Layer 3). |
| `safety_assessment` | `SafetyAssessment` | Separates situation urgency from action harm (see below). If not provided, defaults to a `SafetyAssessment` derived from `risk_score` and `human_at_risk` for backward compatibility with analyzers written before v0.8.0. |

---

### SafetyAssessment

Separates the urgency of a situation from the harm of the proposed action, so that a severe human situation never automatically makes a protective response look dangerous. This is the type introduced in v0.8.0 to replace the old behavior where `human_at_risk` inflated an action's harm score directly.

```python
SafetyAssessment(
    situation_urgency=0.0,
    action_harm_risk=0.0,
    omission_risk=0.0,
    protective_action=False,
)
```

| Field | Type | Description |
|---|---|---|
| `situation_urgency` | `float` | How serious the human context is, independent of what the proposed action does. Clamped to [0.0, 10.0]. |
| `action_harm_risk` | `float` | Estimated harm caused by the proposed action itself. Clamped to [0.0, 10.0]. |
| `omission_risk` | `float` | Estimated harm caused by NOT acting. Clamped to [0.0, 10.0]. |
| `protective_action` | `bool` | Set by a domain analyzer to mark an action as intended to reduce omission risk (e.g. `emergency_call`). Explicitly protective actions are never penalized for situation urgency. |

Frozen (`@dataclass(frozen=True, slots=True)`): construct a new instance rather than mutating one in place.

The `EthicalFilter`'s verdict is still based on `ContextAnalysis.risk_score`, not directly on `SafetyAssessment`: domain analyzers decide how these dimensions should feed into that score. The one exception is the conservative fallback described in the `EthicalFilter.evaluate()` section above: if `human_at_risk=True`, `protective_action=False`, and the score-based verdict is `ALLOW`, it is promoted to `WARN`.

---

## authorization_ruler

Authorization is an optional framework mode. It answers whether an
attributable principal has authority for one exact action snapshot. It does
not replace ethics, infer business policy, inspect prompt text, or call a
handler. Applications provide deterministic policies and authoritative facts.

| Public symbol | Contract |
|---|---|
| `AuthorizationRuler` | Evaluates every policy registered for an exact action type, composes the results deterministically, and issues store-backed receipts only for a complete grant. |
| `AuthorizationVerdict` | Closed vocabulary: `GRANT`, `REQUIRE_APPROVAL`, and `DENY`. Only exact `GRANT` permits an effect. |
| `AuthorizationRequest` | Immutable, detached action snapshot with execution identity and provenance-bearing authority fields. Raw approval evidence is not stored in this value. |
| `AuthorizationRequestResolver` | Application-owned protocol that adds authoritative facts to SETT-owned action identity, trace identity, execution mode, operation key, and detached payload. Execution rejects any returned request that changes those owned fields. |
| `FieldValue` / `FieldTrust` | A detached JSON-safe fact plus its source and `PROPOSED`, `AUTHORITATIVE`, or `UNKNOWN` trust declaration. Unknown authority never grants access. |
| `ApprovalInput` | Opaque evidence routed only to its selected verifier. Policies never receive this raw value. |
| `ApprovalRequirement` / `VerifiedApproval` | A predeclared additional-authority requirement and the immutable result produced by its verifier. |
| `AuthorizationConstraint` | Immutable descriptor for a restriction already verified by policy. Execution does not interpret arbitrary constraint programs. |
| `PolicyDecision` / `AuthorizationDecision` | One policy result and the deterministic aggregate of all applicable results. |
| `AuthorizationOutcome` | Aggregate decision plus the receipt present only for a complete grant. |
| `AuthorizationReceipt` | Public immutable receipt. Its fields alone prove nothing; issuer-owned store state remains authoritative. |
| `ReceiptUse` | `SINGLE_USE` or `CONTROLLED_IDEMPOTENT_REPLAY`. Replay requires unanimous policy consent, controlled execution, and an idempotency key. |
| `ExecutionMode` | Declares whether the request belongs to the plain or controlled execution path. |
| `AuthorizationPolicy` | Protocol for deterministic policies registered by exact action type. |
| `ApprovalVerifier` | Protocol that turns one opaque `ApprovalInput` into a `VerifiedApproval` or rejects it. |
| `AuthorizationFingerprintPolicy` | Protocol for binding the complete request and its verified authority to a stable identifier. |
| `AuthorizationReceiptStore` | Protocol for issuer-owned issue, atomic verification and consumption, and revocation. |
| `AuthorizationEvidenceSink` | Protocol for sanitized authorization evidence. |
| `AuthorizationClock` | Protocol for a timezone-aware, injectable clock. |
| `StrictJSONAuthorizationFingerprint` | SHA-256 reference policy over a normalized typed representation. It normalizes NFC keys and values, signed zero, and integral floats; non-integral finite floats use exact hexadecimal notation. |
| `InMemoryAuthorizationReceiptStore` | Thread-safe reference store with no restart guarantee. |
| `InMemoryAuthorizationEvidence` | Thread-safe in-process sequence of sanitized events. |
| `AuthorizationEvent` | Immutable event with bounded identifiers and detached JSON-safe attributes. It excludes request payloads and raw authority evidence. |
| `SystemAuthorizationClock` | UTC system-clock implementation for ordinary wiring. |

`permits_effect()` returns true only for the exact `GRANT` enum member.
`additional_authority_may_be_evaluated()` identifies the sole verdict that may
open the declared approval path. `validate_verdict_semantics()` enforces those
closed meanings.

`SETTAuthorizationError` is the structured runtime authorization failure.
`SETTAuthorizationConfigurationError` identifies cases where the mechanism
could not produce a trustworthy decision, such as a missing or invalid policy.
Both expose a stable `reason_code` and fail closed.

Controlled replay has no count limit before receipt expiry. It stays bound to
the same complete request fingerprint and operation identity. Policy consent
does not prove that an external provider truly implements idempotence.
The complete stable failure vocabulary is listed in
[`authorization_reason_codes.md`](authorization_reason_codes.md).

## core_ruler Action and execution_ruler SETTExecutor

### Action

A proposed real-world side effect, described as data rather than code.

```python
Action(action_type, payload={}, proposed_by="unknown")
```

| Field | Type | Description |
|---|---|---|
| `action_type` | `str` | Must match a handler registered on the `SETTExecutor` (e.g. `"send_sms"`). |
| `payload` | `dict` | Data the handler needs to perform the effect. |
| `proposed_by` | `str` | Domain of the agent that proposed this action. Audit only. |

---

### SETTExecutor

The one gate for real side effects submitted through it as `Action`s
(`submit()`/`submit_controlled()`): first resolves the exact registered
handler, then evaluates a routable action through the `EthicalFilter`, and
executes the handler only if approved and, when configured, authorized.
This is a structural guarantee for the `Action`/`SETTExecutor` path
specifically (F19, defensive audit #2); it cannot intercept a side
effect performed directly in application code outside this path,
including via `propose_action()`.

```python
SETTExecutor(
    *,
    idempotency_store=None,
    authorization_ruler=None,
    authorization_request_resolver=None,
)
```

The implementation lives in `sett.execution_ruler`. The historical
`sett.core_ruler.executor` module remains as a compatibility re-export; both
paths resolve to the same classes.

**Methods**

| Method | Returns | Description |
|---|---|---|
| `register_handler(action_type, handler)` | `None` | Registers the function that performs a given action type. This is the only code allowed to run that side effect. |
| `submit(action, emotional_state="unknown", risk_profile=None, location_id="global", *, approval_inputs=())` | `Any` | Resolves the exact handler first. A routable action is evaluated; if approved and, when configured, authorized, its handler runs and its result is returned. |
| `get_audit_log()` | `list[dict]` | Actions that were actually approved and executed. Rejected or unhandled actions never appear here. |
| `registered_action_types` *(property)* | `list[str]` | Action types with a handler currently registered. |
| `authorization_enabled` *(property)* | `bool` | Whether every handler invocation requires Authorization Ruler. |

Raises `SETTEthicalFilterRejectedError` if the `EthicalFilter` blocks the
action (handler never runs), or `SETTConfigurationError` if no handler is
registered for that `action_type` (fails closed, not open).

An unroutable action does not copy its payload, read environmental context,
or invoke the `EthicalFilter`. It records no ethical verdict because no policy
evaluation occurred. When tracing is active, it emits `action.blocked` with
`verdict="block"`, `decision_source="routing"`, and the stable reason code
`handler.not_registered`. The raised `SETTConfigurationError` carries the same
normalized verdict and reason as structured attributes. The Executor audit log
continues to contain only actions that actually executed. `submit_controlled()`
performs this preflight, plus attachment and filter readiness checks, before
payload snapshots, fingerprints, idempotency reservations, and handler-attempt
events.

Authorization configuration is all or nothing. `authorization_ruler` and
`authorization_request_resolver` must be supplied together. Once enabled,
every registered handler invocation resolves an `AuthorizationRequest` from
the same detached payload evaluated by Ethics. Execution rejects a resolver
that changes SETT-owned action identity, trace identity, action type, execution
mode, operation key, or payload. A complete grant issues a receipt, which is
verified and consumed before `handler.authorized` and before the handler can
run. Missing policies, denial, unresolved approval, expiry, revocation,
receipt mismatch, resolver failure, or store failure all leave the handler
unreachable.

The configured path snapshots action identity before invoking exporters or
resolvers. Mutating the caller's `Action` cannot make the selected handler
borrow a grant for another action type. Each actual invocation obtains a fresh
decision and consumes its receipt; a completed idempotency replay does not
invoke a handler or claim a new authorization decision.

The local resolver must preserve payload structure and strict JSON scalar
representation, not merely Python equality (`True == 1`). This check uses
the existing in-process JSON reference encoder and is not a durable format.
Receipt fingerprint normalization is a different contract: it supports
equivalent transported requests without authorizing the resolver to change
the snapshot that Ethics evaluated and the handler receives.

The trace records `authorization.receipt_verified` immediately before
`handler.authorized`, or `authorization.receipt_rejected` on failure. Both use
content-free attributes. Payloads, principals, resources, scope, raw approval
inputs, and request fingerprints remain outside trace attributes.

---

### SETTAgent.submit_action(): using the Executor from an agent

```python
agent.submit_action(action_type, payload=None, risk_profile=None)
```

Describes an `Action` and submits it to the `SETTExecutor` registered with
this agent's orchestrator (via `orchestrator.register_executor(executor)`).
Requires both an `Executor` and a handler for that `action_type` to be
registered: otherwise raises `SETTConfigurationError`.

This is the structural alternative to `propose_action()`: the agent never
holds a reference to the real client (SMS provider, payment API, etc.),
so there is no "forgot to call the gate" failure mode.

```python
class NotificationAgent(SETTAgent):
    def process(self, input_data):
        return self.submit_action("send_sms", payload={"to": ..., "message": ...})
```

---

## services_llm

### LLMBase

Abstract interface that all LLM adapters must implement. Extend this class to integrate any language model.

```python
class MyAdapter(LLMBase):
    @property
    def model_name(self): return "my-model"

    def complete(self, prompt, system="", **kwargs): ...
    def chat(self, messages, system="", **kwargs): ...
```

**Abstract methods**

| Method | Description |
|---|---|
| `complete(prompt, system="", **kwargs)` → `str` | One-shot completion. No conversation history. |
| `chat(messages, system="", **kwargs)` → `str` | Multi-turn completion. `messages` is a list of `{"role": "user"\|"assistant", "content": str}`. |
| `model_name` *(property)* → `str` | The name or identifier of the underlying model. |

---

### AnthropicAdapter

LLM adapter for Anthropic's Claude models.

```python
from sett.services_llm.anthropic import AnthropicAdapter

AnthropicAdapter(api_key=None, model="claude-sonnet-4-20250514", max_tokens=1024, temperature=0.75)
```

API key is read from the `ANTHROPIC_API_KEY` environment variable if `api_key` is not provided.
Raises `SETTLLMAdapterError` if the key is missing or the API call fails.

---

### OpenAIAdapter

LLM adapter for OpenAI's GPT models.

```python
from sett.services_llm.openai import OpenAIAdapter

OpenAIAdapter(api_key=None, model="gpt-4o", max_tokens=1024, temperature=0.75)
```

API key is read from the `OPENAI_API_KEY` environment variable if `api_key` is not provided.

---

### GeminiAdapter

LLM adapter for Google's Gemini models.

```python
from sett.services_llm.gemini import GeminiAdapter

GeminiAdapter(api_key=None, model="gemini-1.5-flash", max_tokens=1024, temperature=0.75)
```

API key is read from the `GOOGLE_API_KEY` environment variable if `api_key` is not provided.

---

### OllamaAdapter

LLM adapter for locally-running models via [Ollama](https://ollama.com). No API key, no cloud, no cost: inference happens entirely on your own machine.

```python
from sett.services_llm.ollama import OllamaAdapter

OllamaAdapter(model="qwen3:1.7b", base_url="http://localhost:11434", temperature=0.75, timeout_seconds=30)
```

Unlike the other three adapters, `OllamaAdapter` requires **no extra pip install**: it talks to Ollama's local REST API using only the Python standard library. You only need Ollama itself installed and running, with the target model already pulled (`ollama pull qwen3:1.7b`).

Recommended low-resource models: `qwen3:1.7b` (lightest, ~4GB RAM) or `phi4-mini` (3.8B, MIT license, built for CPU-only machines).

Raises `SETTLLMAdapterError` if Ollama isn't reachable at `base_url`, times out, or returns an unparseable response.

---

## services_tts_stt

### TTSBase

Abstract base class for all text-to-speech adapters. Same interchangeability philosophy as `LLMBase`: an Expert or Agent that needs voice output depends on `TTSBase`, never on a specific provider's SDK.

```python
class MyTTSAdapter(TTSBase):
    @property
    def audio_format(self):
        return "mp3"

    def synthesize(self, text, **kwargs):
        return my_api.speak(text)
```

**Methods**

| Method | Returns | Description |
|---|---|---|
| `synthesize(text, **kwargs)` | `bytes` | Converts text to speech audio, encoded as `audio_format`. Official adapters with per-call BCP-47 selection use `language_code`; voice, stability, and other options remain provider-specific. |
| `audio_format` *(property)* | `str` | The audio encoding this adapter produces (e.g. `"mp3"`, `"wav"`). |

Deliberately does not play audio, write files, or touch any UI: that is an application-layer concern. Returning raw bytes keeps the adapter a pure function, testable without a speaker, a UI, or a filesystem.

Concrete adapters (not exported from `sett` directly, install their optional dependency first):

```python
from sett.services_tts_stt.google import GoogleTTSAdapter       # pip install -e ".[google-tts-stt]"
from sett.services_tts_stt.elevenlabs import ElevenLabsTTSAdapter  # pip install -e ".[elevenlabs]"
```

Both raise `SETTServiceAdapterError` on misconfiguration or provider failure (missing credentials, network error, unsupported voice, etc.).

---

### STTBase

Abstract base class for all speech-to-text adapters. Same philosophy as `TTSBase`, mirrored for the opposite direction.

```python
class MySTTAdapter(STTBase):
    def transcribe(self, audio, **kwargs):
        return my_api.transcribe(audio)
```

**Methods**

| Method | Returns | Description |
|---|---|---|
| `transcribe(audio, **kwargs)` | `str` | Converts speech audio to text. All official adapters with language selection accept the provider-neutral `language_code` spelling; other options remain provider-specific. Returns an empty string if nothing was recognized: adapters raise `SETTServiceAdapterError` only for actual failures (network, auth, malformed audio), never for silence. |

Deliberately does not own a microphone, a listening loop, or any concurrency guard: that orchestration belongs in the application/Expert layer that calls this adapter, not inside it (see the Concurrency section below).

```python
from sett.services_tts_stt.google import GoogleSTTAdapter  # pip install -e ".[google-tts-stt]"
```

`TTSBase` and `STTBase` are deliberately two separate interfaces, not one merged "voice" interface: a provider is free to implement only one of them.

---

## services_sentiment

### SentimentBase

Abstract base class for all sentiment/emotional-tone adapters. An Expert or Agent that needs a sentiment signal depends on `SentimentBase`, never on a specific provider's NLU API. Directly feeds the `emotional_state` parameter that `ContextAnalyzer.analyze()` has accepted since v0.1.1.

```python
class MySentimentAdapter(SentimentBase):
    def analyze(self, text, **kwargs):
        return SentimentResult(score=0.2, magnitude=0.4)
```

**Methods**

| Method | Returns | Description |
|---|---|---|
| `analyze(text, **kwargs)` | `SentimentResult` | Analyzes the sentiment of `text`. `**kwargs` are provider-specific (language, etc.). |

```python
from sett.services_sentiment.google import GoogleSentimentAdapter  # pip install -e ".[google-sentiment]"
```

Raises `SETTServiceAdapterError` on misconfiguration or provider failure.

---

### SentimentResult

Result of analyzing a piece of text for sentiment. Deliberately a raw signal, not a decision: mapping this to a categorical `emotional_state` string (`"calm"`, `"anxious"`, `"distressed"`, etc.) is application logic, the adapter only reports what it measured.

```python
SentimentResult(score, magnitude, sentences=())
```

| Field | Type | Description |
|---|---|---|
| `score` | `float` | Overall polarity: -1.0 (very negative) to 1.0 (very positive). |
| `magnitude` | `float` | Overall emotional intensity, regardless of polarity. Unbounded: 0.0 means emotionally neutral/flat text. |
| `sentences` | `tuple[SentenceSentiment, ...]` | Per-sentence breakdown. Empty by default: not every provider call requests sentence-level detail. |

Frozen dataclass. Sentence-level detail exists specifically to let an application compare document-level score against per-sentence scores: a contradiction between the two (e.g. "that's just great" scored positive overall but negative on the one sentence carrying the literal complaint) is a concrete, testable signal for sarcasm.

---

### SentenceSentiment

Sentiment for a single sentence within an analyzed text.

```python
SentenceSentiment(text, score)
```

| Field | Type | Description |
|---|---|---|
| `text` | `str` | The sentence text. |
| `score` | `float` | Polarity for this sentence only: -1.0 (very negative) to 1.0 (very positive). |

Frozen dataclass.

---

## Durable persistence and recovery

Durable persistence is opt-in. Its stable public API lives in
`sett.persistence`; recovery and boundary-disposition types live in
`sett.audit_ruler`. The `sett` root continues to expose the framework's
structured persistence and audit-closure exceptions.

`PersistenceBackend` binds one opaque `namespace_id` to two narrow protocols:

| Contract | Responsibility |
|---|---|
| `EvidenceStream` | Append-only opaque evidence with compare-and-append continuity, stable backend positions, idempotent `write_id` replay, prefix reads, and `flush()`. |
| `StateRecordStore` | Opaque random-access state records with idempotent `write_id` replay and backend-owned `state_ref` values. |

The SQLite reference backend stores both its `backend_id` and `namespace_id`
in the database and rejects a reopen that supplies either identity
differently. A namespace is stable and non-reassignable; it is not a mutable
display name.

Both contracts return attributable receipts. `DurabilityLevel` names the
failure class a backend declares that accepted bytes survive:
`IN_MEMORY`, `PROCESS_CRASH`, `POWER_LOSS`, `MEDIA_LOSS`, or `NODE_LOSS`.
`UNKNOWN` is outside that order and never satisfies a requirement. These are
backend declarations, not physical proof. `health()` is a passive observation
and does not imply durability.

Third-party backends can run the dependency-free
`sett.testing.assert_evidence_stream_contract()` and
`assert_state_record_store_contract()` probes against fresh test instances.
The probes verify exact-byte replay, conflicts, stream continuity, foreign and
unknown positions, reads, receipts, random access, durability declarations,
and passive health views without inspecting implementation internals.

`PersistencePolicy` requires four explicit application choices:
`snapshot_durability`, `evidence_durability`, an `IntegrityPolicy`, and an
`AuditGapPolicy`. Snapshot durability must be at least as strong as evidence
durability. `Sha256IntegrityPolicy` is the unkeyed reference implementation;
it detects mismatched bytes but allows equality correlation and candidate
guessing for low-entropy values. `NoDigestPolicy` records that integrity was
deliberately disabled. Applications can implement keyed policies, but key
storage and rotation remain outside SETT.

The in-memory backend implements the contracts for tests and single-process
use and can declare only `IN_MEMORY`. The SQLite reference backend reads its
effective journal and synchronous settings before declaring a level:

```python
from sett import SETTOrchestrator
from sett.audit_ruler import Sha256IntegrityPolicy
from sett.persistence import (
    AuditGapPolicy,
    DurabilityLevel,
    PersistencePolicy,
    SQLitePersistenceBackend,
)

backend = SQLitePersistenceBackend.open(
    "sett-state.sqlite3",
    namespace_id="application.instance.01",
    backend_id="sqlite.local",
)
policy = PersistencePolicy(
    snapshot_durability=DurabilityLevel.POWER_LOSS,
    evidence_durability=DurabilityLevel.POWER_LOSS,
    integrity_policy=Sha256IntegrityPolicy(),
    audit_gap_policy=AuditGapPolicy.ESCALATE_KNOWN_OUTCOME,
)
orchestrator = SETTOrchestrator(
    persistence_backend=backend,
    persistence_policy=policy,
)
```

Two properties of that backend are worth stating next to the example.

The database path is used as given, and SETT sets no permissions on it.
Create and restrict the containing directory before calling `open()`: the
`-wal` and `-shm` sidecars are created and removed across SQLite's own
open/close cycle, so the directory is the only durable control point.
`docs/security_model.md` carries the observed modes and the deployment
procedure.

Infrastructure failures from the driver are normalized at the backend
boundary. Every one of them raises `SETTPersistenceError` with a stable
`persistence.sqlite.<operation>_failed` reason code, the attributable
`backend_id`, and the original `sqlite3` exception as `__cause__`, so an
application catching `SETTPersistenceError` cannot miss a failure that the
reference backend itself produced. The structured failures that already had
meaning - `SETTIdempotencyConflictError`,
`SETTPersistenceContinuityError`, `SETTPersistencePositionError`,
`SETTConfigurationError`, `SETTValidationError`, and the `state.record_missing`
reason code - keep their types and semantics.

In v0.14.0, orchestrator persistence is integrated only with
`run_pipeline_controlled()`. Configuring a backend makes `process()`,
`process_controlled()`, and `run_pipeline()` fail before application work so
they cannot return a value without expressing the durable audit-closure axis.
Their behavior is unchanged when persistence is not configured.

`TraceRecorder.restore()` verifies a stable durable prefix and returns a
`RecoveryReport`. The report separates `chain_integrity`, per-trace
`trace_completeness`, reconstructed execution status, unresolved boundaries,
and stage descriptors. It contains no stage outputs. A start without a durable
terminal remains unresolved; SETT never invents an exception, terminal, or
automatic retry for it. `materialize_stage_output()` separately locates,
verifies, validates, and decodes one descriptor and returns both an
`IntegrityStatus` and a `MaterializationStatus`.

An application can append a typed explanation for an unresolved boundary with
`record_boundary_disposition()`. A disposition records its evidentiary basis
and any claimed outcome at the current stream head. It does not become the
missing terminal and does not make the original lifecycle complete.

Pipeline stage outputs are persisted before `pipeline.stage_completed` can be
recorded. `PrivateMemory` persistence is separately opt-in through a stable
`memory_id`. With an orchestrator backend configured, an agent opts in by
passing `private_memory_id` to `SETTAgent.__init__()`. Persistent writes and
clears require a real ambient or explicit `ExecutionContext`, accept strict
JSON values, and reach the state store before the live memory changes.
Ephemeral `PrivateMemory` keeps accepting ordinary Python values as before.
`PrivateMemory.restore()` replays only evidence-confirmed mutations.

`MigrationPlan` performs one-hop, one-to-one, copy-on-write schema migrations
inside one namespace and one state store. `dry_run_stage_migration()` and
`dry_run_private_memory_migration()` derive a canonical manifest from durable
evidence; they never enumerate the record store. Execution requires a ready
dry-run, preserves the source record, confirms each destination before
recording its link, and rejects stale or branched lineages. Rollback after a
confirmed migration is another forward migration. Retention, compaction,
cross-namespace transfer, import, production secret storage, persistent
idempotency through this backend, `UniversalMemory` restoration, and automatic
pipeline resumption are outside v0.14.0.

Durable trace persistence lets verification survive a process restart while
the retained prefix and resume position remain intact. It is not a digital
signature, proof of authorship, or protection against an actor who can replace
the complete chain and its storage.

---

## Logging

Importing `sett` attaches a `logging.NullHandler` to the package logger and
does not configure the root logger. This prevents Python's fallback
`lastResort` handler from writing library warnings to stderr when an embedding
application has not configured logging. Records still propagate normally to
handlers installed by the application on `sett` or any ancestor logger.

## Concurrency

SETT's core components (`UniversalMemory`, `EthicalFilter`, `SETTExecutor`, `PrivateMemory`) are not evaluated under concurrent access, with ten documented exceptions:

- `UniversalMemory` holds an internal lock around its store and history so that concurrent `update()` / `read()` / `publish_environmental_context()` calls do not corrupt shared state.
- `TraceRecorder` (see above) holds its own internal `threading.RLock()` around `record()` so that concurrent tracing calls from multiple threads do not lose events or corrupt the hash chain - verified under a stress test of 8 concurrent threads each recording 200 events (1600 events total, zero loss, `verify()` returns `True` afterward).
- `ExecutionControlRegistry` (v0.12.0) holds an internal `threading.RLock()` around registration, release, snapshot, lookup, subtree cancellation, callback registration and `wait()`. This one is not optional: cancellation exists to be requested from a thread other than the one running the work, so a registry that was not safe to share would have nothing to offer. Registration and subtree cancellation take the same lock, which is what closes the window where a control registering just after its ancestor was cancelled could miss it. Observing cancellation afterwards is a lock-free `Event.is_set()`, because checkpoints run far more often than cancellations happen.
- `InMemoryIdempotencyStore` (v0.12.0) holds an internal `threading.RLock()` around its records and its attempt log, so two threads reserving the same key cannot both come back holding it.
- `CircuitBreaker` (v0.13.0) holds an internal `threading.RLock()` around state transitions, failure counts and half-open probe admission. One instance may be shared by concurrent callers: a stress test with 8 threads recording 250 transient failures each preserves all 2,000 failures and opens the circuit exactly once. This guarantee covers the breaker's own in-process state, not the adapter calls it guards.
- `InMemoryEvidenceStream` serializes head reads, compare-and-append, replay checks, prefix reads, and flushes with an internal `threading.RLock()`. A stress test with 8 threads and 800 distinct writes preserves every entry. A caller that races on an old head still receives the documented continuity error and must retry from the new head.
- `InMemoryStateRecordStore` serializes record writes, idempotent replay, lookup, and reads with an internal `threading.RLock()`. The same 8-thread test preserves and retrieves all 800 records.
- `SQLitePersistenceBackend` shares one `threading.RLock()` across its evidence and state views and opens its connection for guarded cross-thread use. A stress test with 8 threads performing 200 evidence appends and 200 state writes preserves every distinct identity. This does not make two independent backend objects coordinate outside SQLite's own transaction semantics.
- `InMemoryAuthorizationReceiptStore` serializes issue, revocation, and verification/consumption with an internal `threading.RLock()`. Across 2,000 competing consumptions from 8 threads, exactly one can consume a single-use receipt.
- `InMemoryAuthorizationEvidence` serializes event appends and snapshot reads with an internal `threading.RLock()`. A stress test with 8 threads recording 200 events each preserves all 1,600 distinct events.

All ten guarantees are deliberately narrow: each protects only the state
described above within one process. `UniversalMemory` protects its store and
history, while `TraceRecorder` protects event order and its hash chain. The
registry, idempotency store, and circuit breaker protect their own control
state. The persistence implementations protect their own stream or record
operations. Authorization reference stores protect their own receipts and
event sequence, not the Ruler's complete evaluate/issue/consume workflow.
None extends any guarantee to a policy, verifier, resolver, handler, adapter,
agent, cleanup callback, codec, transform, or exporter reached through them. `InMemoryIdempotencyStore` in
particular says nothing about what happened before the process started; an
application injects a durable store when it needs more than that.

Outside of those ten guarantees, SETT does not currently make a thread-safety claim for the framework as a whole: no adapter, agent base class, or expert base class has been tested under concurrent access, and none advertises being safe for it. This applies to `PrivateMemory`, `EthicalFilter`'s audit log, `SETTExecutor`'s audit log, and the LLM/TTS/STT/sentiment adapters equally.

Practical implications for a deployment:

- Running multiple `SETTOrchestrator` instances in separate processes or async tasks, each with its own agents, is the supported pattern for parallelism today (this is exactly what `EnvironmentalContext` and `publish_environmental_context()` exist to coordinate across instances).
- Sharing a single `SETTAgent`, `PrivateMemory`, or `EthicalFilter` instance across multiple threads or concurrent coroutines without external synchronization is not a tested configuration. `UniversalMemory`, `TraceRecorder`, `ExecutionControlRegistry`, `InMemoryIdempotencyStore`, `CircuitBreaker`, `InMemoryEvidenceStream`, `InMemoryStateRecordStore`, `SQLitePersistenceBackend`, `InMemoryAuthorizationReceiptStore`, and `InMemoryAuthorizationEvidence` are the ten narrow exceptions.
- `InMemoryEvidenceStream`, `InMemoryStateRecordStore`, and `SQLitePersistenceBackend` may be shared as described above. This protects their own storage operations; migration transforms and application codecs remain caller-owned code with no concurrency guarantee from SETT.
- An STT adapter's `transcribe()` being called concurrently for overlapping audio streams is an application-layer concern: guard it in the calling Expert or Agent, not inside the adapter (this mirrors why `STTBase` deliberately does not own a listening loop or a lock itself).

This is a documentation gap being closed, not a new restriction: the underlying behavior has not changed. A future release may extend the same lock-based pattern already used by `UniversalMemory` and `TraceRecorder` to other shared components if a real deployment need appears.

---

## Exceptions

All SETT exceptions inherit from `SETTError`.

| Exception | Raised when |
|---|---|
| `SETTError` | Base class for all SETT exceptions. |
| `SETTEthicalFilterRejectedError` | The EthicalFilter blocks an action or memory write. Carries structured attributes (see below) in addition to the readable message. |
| `SETTEthicalFilterWarningError` | Reserved for a future warning-as-exception path. **Not currently raised** (F23, defensive audit #2): a WARN verdict today is allowed through and logged via `logging`, not raised. |
| `SETTMemoryAccessDeniedError` | An `UniversalMemory.update()` call would publish a raw `RiskProfile` or one of its reserved pillar fields into shared memory, structurally enforcing the promise that `RiskProfile` never leaves `PrivateMemory` (see `RiskProfile`'s docs). |
| `SETTAgentNotFoundError` | The orchestrator cannot find a registered agent for the requested domain. |
| `SETTExpertNotFoundError` | An agent cannot find a registered expert by name. |
| `SETTLLMAdapterError` | An LLM adapter fails to respond or is misconfigured (missing API key, network error, etc.). |
| `SETTServiceAdapterError` | A TTS, STT, or generative AI adapter fails or is misconfigured. |
| `SETTConfigurationError` | The framework or a component is incorrectly configured before the system starts. May carry a normalized fail-closed decision when an action has no route (see below). |
| `SETTCancelledError` | Somebody asked for this execution to stop, through a `cancel()` call on it or on an ancestor. Carries structured attributes (see below). |
| `SETTTimeoutError` | An `ExecutionControl` exceeded its `Deadline`. Nobody asked; the work did not fit in the time it was given, which is frequently retryable. Carries structured attributes (see below). |
| `SETTDependencyError` | A unit never became reachable because something it needed failed. Deliberately not a cancellation: nobody ever asked anything of it. Carries structured attributes (see below). |
| `SETTIdempotencyConflictError` | An `idempotency_key` was reused with a payload that fingerprints differently, which means two different actions are claiming one identity. Carries structured attributes (see below). |
| `SETTPersistenceError` | Base class for attributable persistence failures. Carries a stable `reason_code` and optional `backend_id`. |
| `SETTPersistenceFormatError` | Durable bytes violate their declared framing or format. Also inherits from `ValueError`. |
| `SETTPersistenceContinuityError` | Compare-and-append would extend an evidence stream from a stale or incompatible head. |
| `SETTPersistencePositionError` | A stream position is invalid or belongs to another backend or stream. Also inherits from `ValueError`. |
| `SETTAuditClosureError` | An execution outcome is known, but the configured policy refuses to return it without confirmed durable audit closure. Retrying the underlying effect is not safe. |
| `SETTValidationError` | A data-carrying value fails its own validation (e.g. a `RiskProfile` pillar outside `[0.0, 1.0]`). Also inherits from `ValueError`, on purpose: catching either `SETTError` or `ValueError` catches this, the same pattern the standard library's `json.JSONDecodeError` uses. Not raised by `Action` or `BiometricReading`, which do not validate their fields. |

```python
from sett import SETTEthicalFilterRejectedError

try:
    orchestrator.process(input_data, domain="emergency")
except SETTEthicalFilterRejectedError as e:
    print(f"Action blocked: {e}")
```

### Structured attributes on `SETTEthicalFilterRejectedError`

`str(e)` returns the same human-readable message as always. In addition,
the data behind that message is available as real attributes: downstream
code should read these instead of parsing the message string:

| Attribute | Type | Meaning |
|---|---|---|
| `e.action` | `str \| None` | The action type that was blocked. |
| `e.score` | `float \| None` | The computed harm score, full precision (the message renders it rounded to 2 decimals). |
| `e.threshold` | `float \| None` | The effective reject threshold the score was compared against, with environmental modifiers already applied. |
| `e.principle` | `str \| None` | The ruleset principle in effect. |
| `e.reasoning` | `str \| None` | The analyzer's reasoning behind the score. |
| `e.trace_event_id` | `str \| None` | Latest causal event for the rejection while unified tracing is active. |

### Structured attributes on `SETTConfigurationError`

Most configuration failures use only the human-readable message, so these
attributes are `None`. An action with no registered handler uses them to expose
an explicit normalized routing decision without claiming an EthicalFilter
verdict:

| Attribute | Type | Meaning |
|---|---|---|
| `e.reason_code` | `str \| None` | Stable machine-readable reason; `handler.not_registered` for an unroutable action. |
| `e.verdict` | `str \| None` | Normalized execution decision; `block` for an unroutable action. |
| `e.action_type` | `str \| None` | The action type that had no route. |
| `e.action_id` | `str \| None` | Identifier of the affected `Action`. |
| `e.trace_event_id` | `str \| None` | Latest causal trace event associated with the failure. |

All attributes default to `None` if the exception is constructed with only
a message, so existing raising code keeps working unchanged.

```python
try:
    executor.submit_action(proposal)
except SETTEthicalFilterRejectedError as e:
    audit_ui.show(
        action=e.action,
        score=e.score,          # a float: no string parsing
        threshold=e.threshold,
        principle=e.principle,
    )
```

### Structured attributes on the lifecycle exceptions (v0.12.0)

Same principle as above: read the attributes, never parse the message.
All default to `None`, so an application raising one of these with only a
message keeps working.

`SETTCancelledError`:

| Attribute | Type | Meaning |
|---|---|---|
| `e.run_id` | `str \| None` | The execution node that observed the cancellation, using the same `run_id` its trace events carry. |
| `e.reason` | `str \| None` | Reason code from the trace vocabulary, e.g. `"execution.cancelled_by_caller"`. |
| `e.cause_event_id` | `str \| None` | The trace event that requested the cancellation, linking a cancelled terminal back to the request. |

`SETTTimeoutError`:

| Attribute | Type | Meaning |
|---|---|---|
| `e.run_id` | `str \| None` | The execution node whose deadline expired. |
| `e.deadline` | `Deadline \| None` | The deadline that was exceeded. |

`SETTDependencyError`:

| Attribute | Type | Meaning |
|---|---|---|
| `e.run_id` | `str \| None` | The unit that never became reachable. |
| `e.failed_dependency_run_id` | `str \| None` | The unit whose failure made it unreachable. |

`SETTIdempotencyConflictError`:

| Attribute | Type | Meaning |
|---|---|---|
| `e.key` | `str \| None` | The idempotency key claimed twice. |
| `e.expected_fingerprint` | `str \| None` | The fingerprint already recorded under that key. |
| `e.actual_fingerprint` | `str \| None` | The fingerprint of the action now presenting it. |


---

# Lifecycle, outcomes and idempotency (v0.12.0)

## `ExecutionControl`

Whether one execution node may continue, for how long, and how it cleans
up. It carries its own `ExecutionContext`, so `run_id` and `parent_id`
come from the tree that already exists rather than from a second one.

| Member | Returns | Notes |
|---|---|---|
| `create_root(context, *, timeout_seconds=None, registry=None, trace_recorder=None)` | `ExecutionControl` | Starts a control tree. |
| `child(*, context=None, timeout_seconds=None)` | `ExecutionControl` | Pass the context the boundary already derived, so control and trace share one `run_id`. |
| `checkpoint()` | `None` | Raises `SETTCancelledError` or `SETTTimeoutError`. Cancellation is reported first. |
| `cancel(*, reason=..., cause_event_id=None)` | `bool` | Cancels this node and everything registered beneath it. `False` if already cancelled. |
| `token` | `CancellationToken` | Observation only. This is what travels downward. |
| `source` | `CancellationSource` | The authority to cancel. This is what the caller keeps. |
| `register_cleanup(callback)` | `None` | Hooks run LIFO on the way out. |
| `remaining_seconds()` | `float \| None` | `None` when unbounded. |

Used as a context manager. If entering raises - a deadline already past,
or an ancestor already cancelled - the terminal state is settled and the
cleanup hooks run inside `__enter__`, because Python does not call
`__exit__` when `__enter__` raises.

## `ExecutionControlRegistry`

Indexes live controls by `run_id`. The parent relation is owned by each
control's `ExecutionContext`; `parent_id` is read from there and never
duplicated as state a node could disagree with. On top of that, the
registry keeps a derived children index, built at registration time from
those same `parent_id` values, so cancelling a subtree does not have to
walk every node in the registry looking for descendants. It is a lookup
accelerator over the causal tree, not a second tree: nothing is entered
into it that the contexts do not already say.

`cancel_subtree(run_id, *, reason, cause_event_id=None)` cancels one node
and every registered descendant. `control_for(run_id)` returns the live
control, which is how `SETTOrchestrator.cancel()` routes a cancellation
through the control that records the request. `release(run_id)` marks a
control finished; an unknown `run_id` is a no-op, the same answer
`snapshot()` and `cancel_subtree()` give.

A released node whose children are still registered is retained until the
last of them is gone, since dropping it would break the chain its subtree
is resolved against.

## `Deadline`

Monotonic, so a system clock adjustment neither grants nor revokes time.
`child(timeout_seconds)` returns the tighter of the two: a nested unit
can shorten what it was given and never extend it.

## `ExecutionStatus`, `ExecutionState`, `ExecutionResult`

Six terminal statuses: `SUCCESS`, `PARTIAL`, `CANCELLED`, `TIMED_OUT`,
`FAILED`, `REJECTED`. There is deliberately no `SKIPPED`: a unit that
never ran has no execution outcome, and giving it one would claim
somebody stopped it.

`ExecutionResult.unwrap()` returns the value, or re-raises the original
exception instance with its concrete type and attributes intact.

| Member | Returns | Notes |
|---|---|---|
| `value` | `T \| None` | The work's own result. See the note on immutability below. |
| `exception` | `BaseException \| None` | The original instance, not a copy or a description. |
| `state` | `ExecutionState` | Status, reason code, timestamps, measured duration. |
| `status` | `ExecutionStatus` | Shortcut for `state.status`. |
| `reason_code` | `str \| None` | Shortcut for `state.reason_code`. |
| `audit_closure` | `AuditClosure` | Independent durable-audit axis: `NOT_REQUESTED`, `SATISFIED`, `UNSATISFIED`, or `UNCONFIRMED`. It never changes the execution outcome. |
| `succeeded` / `cancelled` / `timed_out` / `rejected` / `partial` | `bool` | One per status worth branching on. |
| `unwrap()` | `T` | Value, or the original exception re-raised. |
| `success(...)` / `failure(...)` / `not_executed(...)` | `ExecutionResult` | Factories; each refuses a state whose status disagrees with it. |

When persistence is configured, a known execution outcome can coexist with an
audit gap if its terminal evidence could not be confirmed. Under
`RETURN_KNOWN_OUTCOME`, the caller receives that result and must inspect
`audit_closure`. Under `ESCALATE_KNOWN_OUTCOME`, SETT raises
`SETTAuditClosureError` carrying the same result and closure object. The
underlying action must not be retried merely because its audit terminal is
missing.

**`value` is a reference, not a snapshot.** `ExecutionResult` is a frozen
dataclass, and "frozen" in Python means its fields cannot be reassigned -
it says nothing about the object a field points at. If `value` is a dict
or a list, whoever else holds that object can still mutate it, and the
result will report the mutated version. This is the same trade-off
`Action` makes and is stated for the same reason: the guarantee SETT
offers is that the framework will not change what it handed you, not that
nobody can. Code that needs a true snapshot takes its own `deepcopy()`.

## `SETTOrchestrator` additions

| Member | Returns |
|---|---|
| `process_controlled(...)` | `ExecutionResult[dict]` |
| `run_pipeline_controlled(...)` | `ExecutionResult[PipelineResult]` |
| `cancel(run_id, *, reason=...)` | `bool` |
| `control_registry` | `ExecutionControlRegistry` |
| `lifecycle_policy` | `LifecyclePolicy` |

## `SETTExecutor` additions

| Member | Returns |
|---|---|
| `submit_controlled(action, ..., retry_policy=None, timeout_seconds=None, approval_inputs=())` | `ExecutionResult` |
| `register_controlled_handler(action_type, handler)` | `None` |
| `idempotency_store` | `IdempotencyStore` |
| `authorization_enabled` | `bool` |

`submit_controlled()` performs a static readiness and route preflight, then
wraps `submit()` rather than reimplementing the executable path. Filter
evaluation, handler dispatch, and every trace event at the effect boundary
still come from the same implementation.

## `HandlerContext`

What a handler registered with `register_controlled_handler()` is told
about its own attempt. Available inside the handler via
`current_handler_context()`, and passed explicitly by some call
conventions.

| Field | Type | Notes |
|---|---|---|
| `action_id` | `str` | |
| `idempotency_key` | `str \| None` | Pass this through to a provider that supports idempotent requests, the only place an "at most once" guarantee can actually be enforced. |
| `attempt_id` | `str` | Unique per attempt, not per action. |
| `attempt_number` | `int` | 1 on the first try. |
| `execution_context` | `ExecutionContext \| None` | |
| `cancellation_token` | `CancellationToken \| None` | |
| `timeout_seconds` | `float \| None` | Time genuinely left in this run, not a number the handler invented. |

`throw_if_cancelled()` is a convenience checkpoint for a handler that
loops or polls internally.

## Cancellation views: `CancellationToken`, `CancellationSource`, `CancellationSnapshot`

Cancellation is split into two objects on purpose. `CancellationSource`
is the authority to cancel and stays with whoever started the work;
`CancellationToken` observes only and is what travels downward into
agents, experts and adapters. Neither holds state of its own; both are
views over `ExecutionControlRegistry`, which is why a token handed to an
adapter cannot go stale or disagree with the control it came from.

| Member | Returns | Notes |
|---|---|---|
| `CancellationToken.run_id` | `str` | The node this token observes. |
| `CancellationToken.is_cancelled` | `bool` | Read it as often as you like; it is a lock-free `Event.is_set()`. |
| `CancellationToken.snapshot` | `CancellationSnapshot` | The full state in one read, with no chance of the answer changing between two questions. |
| `CancellationToken.throw_if_cancelled()` | `None` | Raises `SETTCancelledError`. |
| `CancellationSource.run_id` | `str` | The node this source can cancel. |
| `CancellationSource.token` | `CancellationToken` | An observation-only view, safe to hand out. |
| `CancellationSource.cancel(*, reason=..., cause_event_id=None)` | `bool` | Cancels the node and its registered subtree. |

`CancellationSnapshot` is a frozen dataclass:

| Field | Type | Meaning |
|---|---|---|
| `cancelled` | `bool` | |
| `reason` | `str \| None` | The reason code, from the trace vocabulary. |
| `cause_event_id` | `str \| None` | The trace event that requested it. |
| `cancelled_run_id` | `str \| None` | **The node cancellation was actually requested on**, which differs from the observing node whenever it was inherited. This is what lets a cancelled leaf report *which* branch was cancelled rather than only that it was. |

## `current_execution_control()` and `lifecycle_scope()`

```python
from sett import current_execution_control, lifecycle_scope
```

`current_execution_control()` returns the control active in this thread or
task, or `None` when nobody established one. That `None` is the reason
every checkpoint the framework places at a boundary costs nothing for a
caller who never asked for lifecycle management: `process()` and
`submit()` do not enter a scope, so there is no control to find and
nothing to check.

`lifecycle_scope(control)` binds one as active for the duration of a
`with` block. It mirrors `execution_scope()` from `execution_context.py`
deliberately, with the two sitting side by side at every instrumented boundary,
one carrying identity, the other carrying permission to continue.

```python
with control, lifecycle_scope(control):
    do_the_work()          # checkpoint() anywhere in here now bites
```

## Idempotency: `IdempotencyStore`, `InMemoryIdempotencyStore`, and their records

`IdempotencyStore` is a `Protocol`: implement it against SQLite, a
transactional database or an external service to get a guarantee that
survives a restart. `InMemoryIdempotencyStore` is the reference
implementation and the default; it is enough for tests and for a
single-process application, and explicitly not enough to claim
idempotency across restarts.

| Member | Returns | Notes |
|---|---|---|
| `reserve(*, key, fingerprint, trace_id)` | `IdempotencyReservation` | Raises `SETTIdempotencyConflictError` if the key exists with a different fingerprint. |
| `mark_in_progress(*, key)` | `None` | |
| `mark_succeeded(*, key, digest)` | `None` | |
| `mark_failed(*, key, retryable)` | `None` | |
| `get(key)` | `IdempotencyRecord \| None` | |

**Transitioning an unknown key is a silent no-op, and that is the
contract.** A transition describes something that happened to a
reservation; a store with no such reservation has nothing to describe.
Raising instead would turn a store's own retention or eviction policy,
which SETT deliberately does not dictate, into an error in the caller's
path, after the effect already ran. `SETTExecutor` always reserves before
it transitions, so its own path never reaches that branch; it is stated
because the Protocol is public. The same applies to `complete_attempt()`
with an attempt id the store never issued.

`IdempotencyRecord` - what a store knows about one key:

| Field | Type | Notes |
|---|---|---|
| `key` | `str` | Drawn from the application's own domain, e.g. `"payment:invoice-2481"`. |
| `fingerprint` | `str` | From `action_fingerprint()`. |
| `status` | `IdempotencyStatus` | |
| `trace_id` | `str` | |
| `attempt_count` | `int` | Counts attempts against the key, across separate `submit_controlled()` calls. |
| `result_digest` | `str \| None` | Shape of the result, not the result. |

`IdempotencyReservation` - the answer to "may I act":

| Field | Type | Notes |
|---|---|---|
| `acquired` | `bool` | **The field that makes the concurrent case correct.** Two threads reaching one fresh key both get a record back; exactly one gets `acquired=True`. Without it the loser cannot tell "I own this attempt" from "somebody else is already running it", which is the race a store exists to arbitrate. |
| `record` | `IdempotencyRecord` | |
| `already_succeeded` | `bool` | Replay, do not repeat. |
| `in_progress_elsewhere` | `bool` | Somebody else holds it and has not finished. |

`IdempotencyStatus`: `RESERVED`, `IN_PROGRESS`, `SUCCEEDED`,
`FAILED_RETRYABLE`, `FAILED_FINAL`. `FAILED_FINAL` covers the case that
matters most, an attempt whose outcome is unknown because the connection
dropped after the request went out. Not knowing whether the effect
happened is a reason to stop, not a reason to try again.

`ActionAttempt` - one row of the attempt log:

| Field | Type | Notes |
|---|---|---|
| `attempt_id` | `str` | Unique per attempt. |
| `action_id` | `str` | |
| `idempotency_key` | `str` | |
| `attempt_number` | `int` | Mirrors `IdempotencyRecord.attempt_count`: it counts against the key, so it keeps climbing across resubmissions instead of resetting to 1. |
| `trace_id` | `str` | |
| `started_at` / `finished_at` | `str` / `str \| None` | ISO 8601, timezone-aware. |
| `status` | `str` | |
| `error_type` | `str \| None` | The exception's type name, never its message. |
| `retryable` | `bool` | |

It deliberately holds **no payload**. An attempt log is kept for as long
as the operation matters and usually longer, and the payload is exactly
the part likely to carry something about a person. The fingerprint
identifies the action just as well for every purpose this record serves.

## `action_fingerprint()`

```python
from sett import action_fingerprint
```

A stable hash of what an action would do, used to catch one key being
reused for a different action. Numerically equal payloads that took
different serialization paths are the same action (`1500` and `1500.0`);
`True` and `1` are not.

Raises `SETTValidationError` when the payload contains something
`json.dumps` cannot serialize, rather than falling back to hashing a
`repr()`. A default `repr()` embeds a memory address, so the same logical
payload would fingerprint differently on every attempt, causing reuse detection
that always misses.

## `RetryPolicy`, `ErrorClass` and `classify_error()`

Defaults to one attempt and an empty `retry_on`. Raising `max_attempts`
alone never causes a retry: how many attempts to make and which failures
are safe to repeat are separate statements.

`SETTEthicalFilterRejectedError`, `SETTCancelledError`,
`SETTTimeoutError`, `SETTIdempotencyConflictError`,
`SETTConfigurationError` and `SETTValidationError` are never retried,
whatever a policy lists.

`classify_error(error, policy=None)` returns the `ErrorClass` that
decides it:

| Value | Meaning |
|---|---|
| `TRANSIENT` | Failed for a reason that may not recur; safe to attempt again. Only reachable when the policy's `retry_on` names it. |
| `PERMANENT` | Failed for a reason that will recur; attempting again is waste. **This is also what an unrecognised failure gets**: nothing is known about whether it left an effect behind, and guessing is what produces duplicates. |
| `AMBIGUOUS` | Nobody knows whether the effect happened. A deadline reached while a request was in flight lands here rather than in `PERMANENT`, because the request may already have arrived. Not retried, and reported as `reason_code="action.outcome_unknown"`. This distinction is what stops a timed-out payment from being charged twice. |

## Reserved adapter keyword arguments

`timeout` and `cancellation_token`, inside the `**kwargs` every adapter
interface already accepts. Forward them with `lifecycle_kwargs()`, unpack
them with `pop_lifecycle_kwargs(kwargs)`.

## What is not promised

SETT cannot cancel arbitrary non-cooperative Python code, and does not
offer exactly-once against an external system. An outcome that cannot be
determined ends as `TIMED_OUT` with
`reason_code="action.outcome_unknown"` and is never retried
automatically. See `CHANGELOG.md` for the full statement.

# Getting Started with SETT

The first example runs without a provider. The LLM example is a template:
install the selected adapter dependency and supply your own credentials and model
identifier before constructing it. SETT does not select a provider model.

For an existing v0.14.0 application, follow the
[migration steps](../CHANGELOG.md#migrating-from-v0140) before upgrading. They
cover environment, adapters, policy configuration, receipts and retries, including
changes that apply without authorization.

---

## Requirements

- Python 3.13, 3.14, or 3.15
- pip

This release was validated on Windows with Python 3.13, 3.14 and 3.15. Linux
and macOS have not been validated for this release.

The CI workflow is configured to run the full test suite, import sweep
and lint on these Python versions, on Linux and Windows: see
`.github/workflows/ci.yml`. Release verification and public CI are
separate evidence; a configured matrix does not establish that every
environment has passed on the current source revision.

The SETT core has **zero mandatory dependencies**. LLM support is optional and installed separately.

---

## Installation

SETT is not published on PyPI yet; install it from source.

```bash
git clone https://github.com/4HumanityAI/sett-framework.git
cd sett-framework

# Core framework: no LLM required
pip install -e .

# With Claude support (optional)
pip install -e ".[anthropic]"

# With GPT support
pip install -e ".[openai]"

# With Gemini support
pip install -e ".[gemini]"

# All adapters at once
pip install -e ".[all]"
```

An adapter must always be explicitly constructed and named by the
application. SETT has no default LLM, TTS, STT, or sentiment provider. When an
application has multiple compatible adapters, it supplies its own selection
policy; see [Adapter contracts](adapters.md).

---

## Your first SETT system

Copy and run the following. No API key required for this example.

```python
from sett import SETTOrchestrator, SETTAgent, SETTExpert, EthicalFilter


# 1. Define an expert: the most atomic unit in SETT.
#    It handles exactly one task and writes results to private memory.

class GreetingExpert(SETTExpert):
    def resolve(self, context):
        name = context.get("name", "world")
        if self._private_memory:
            self._private_memory.write("last_greeted", name)
        return {"greeting": f"Hello, {name}! Welcome to SETT."}


# 2. Define an agent: a domain specialist made up of one or more experts.
#    It coordinates its experts and publishes only the final result outward.

class WelcomeAgent(SETTAgent):
    def __init__(self):
        super().__init__(name="WelcomeAgent", domain="welcome")
        self.register_expert(GreetingExpert(name="greeting"))

    def process(self, input_data):
        result = self.get_expert("greeting").resolve(input_data)
        self._publish_to_universal(result)   # passes through EthicalFilter
        return result


# 3. Build the system and run it.

orchestrator = SETTOrchestrator(ethical_filter=EthicalFilter())
orchestrator.register_agent(WelcomeAgent())

result = orchestrator.process({"name": "Dan"}, domain="welcome")
print(result["greeting"])
# Hello, Dan! Welcome to SETT.
```

Every orchestrated call now creates a trace automatically. Existing code does
not need to pass a context. Run the following blocks in the same Python session
as the first example; they reuse its `orchestrator`:

```python
trace_id = orchestrator.last_trace_id
for event in orchestrator.export_trace(trace_id, view="summary"):
    print(event["kind"], event["status"])
```

For request boundaries that already have correlation identifiers, create and
pass an explicit context:

```python
from sett import ExecutionContext

context = ExecutionContext.create(
    application_id="welcome-demo",
    session_id="opaque-session-id",
    metadata={"channel": "cli"},
)
traced = orchestrator.process_traced(
    {"name": "Dan"},
    domain="welcome",
    execution_context=context,
)

assert traced.trace_id == context.trace_id
assert orchestrator.verify_traces(traced.trace_id)
```

Do not put secrets, payloads, prompts, or user records in context metadata.
Use opaque pseudonymous identifiers and keep domain data in normal inputs and
memory.

---

## What just happened

The `GreetingExpert` resolved the task and wrote intermediate state to the agent's **private memory**: a workspace the public API does not expose to other components. That isolation is a convention, not a runtime access-control boundary; see [the security model](security_model.md).

The `WelcomeAgent` took that result and published it to **universal memory** via `_publish_to_universal()`. Before the result was committed, the `EthicalFilter` evaluated it and issued an `ALLOW` verdict.

The `SETTOrchestrator` routed your input to the right agent, applied the filter, and returned the final result.

Three levels, one clear responsibility each.

---

## Using a real LLM

This template uses `AnthropicAdapter`. Install the `anthropic` extra, set
`ANTHROPIC_API_KEY` to your credential and `SETT_LLM_MODEL` to a model identifier
available to your account. These environment variables are read by the example;
no model name is embedded in SETT. Defining the class does not contact the provider.
Constructing it requires configuration; calling `resolve()` makes a provider call.
The adapter sends no `temperature` unless you set one, because current Anthropic
SDK releases no longer accept it.

```python
import os
from sett import SETTExpert
from sett.services_llm.anthropic import AnthropicAdapter


class SmartExpert(SETTExpert):
    def __init__(self, name):
        super().__init__(name=name)
        # Supply both values in your environment before constructing this expert.
        self.llm = AnthropicAdapter(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            model=os.environ["SETT_LLM_MODEL"],
        )

    def resolve(self, context):
        question = context.get("question", "")
        answer = self.llm.complete(prompt=question, system="You are a helpful assistant.")
        if self._private_memory:
            self._private_memory.write("last_answer", answer)
        return {"answer": answer}
```

To use `OpenAIAdapter` or `GeminiAdapter`, change the import and constructor,
install that provider's optional dependency and supply its credentials and model
identifier. The expert's use of `complete()` can remain the same; provider
configuration and model availability are not interchangeable.

---

## Inspecting the audit log

Every decision the EthicalFilter makes is logged. This block reuses the
`orchestrator` from the first example in the same Python session:

```python
for entry in orchestrator.get_ethical_audit_log():
    print(f"[{entry['verdict'].upper()}] {entry['action']}: score: {entry['harm_score']:.2f}")
```

---

## Migrating stored policy decisions

Import compatibility with v0.14.0 does not migrate stored `PolicyDecision`
objects. A legacy pickle can load without the required `conditions` and
`review_obligations` fields; even representing or comparing that value can fail.
Do not use successful loading as validation or pass that incomplete value into
authorization. Reconstruct decisions through the current constructors from
reviewed application data, explicitly declaring both fields. An empty sequence
means that no obligations of that kind apply; it must not stand in for unknown
legacy data. Reconstructing a decision does not restore a receipt or authority.

SETT supplies no automatic pickle migration. Do not load untrusted pickles.
See the [v0.15.0 migration notes](../CHANGELOG.md#migrating-from-v0140) for the other required changes
to policy registration and authorization configuration.

---

## Next steps

| I want to… | Go to… |
|---|---|
| Understand the architecture and philosophy | [`docs/concepts.md`](concepts.md) |
| Create my own agent, expert or adapter from a blank, working starting point | [`templates/`](../templates/) in the source tree, not in the installed package |
| Produce user-facing text without letting the LLM invent facts | [`templates/phrasing_expert_template.py`](../templates/phrasing_expert_template.py) |
| See a multi-agent system with health, schedule, and environment agents | [`examples/multi_agent.py`](../examples/multi_agent.py) |
| See the EthicalFilter in action with ALLOW / WARN / REJECT scenarios | [`examples/with_ethics.py`](../examples/with_ethics.py) |
| See the "actions as data" Executor pattern | [`examples/with_executor.py`](../examples/with_executor.py) |
| Govern an action with a policy, a verified condition and decision records | [`examples/governed_action.py`](../examples/governed_action.py), then [the policy layer](policy_ruler.md), [the governance journal](governance_journal.md) and [explanations](explanations.md) |
| Put an external tool loop under governance without adding a dependency | [`docs/foreign_callers.md`](foreign_callers.md) |
| Export one governed run as a standalone record | Run [`examples/governance_card.py`](../examples/governance_card.py) with a new output directory as its first argument. |
| Look up every public class and method | [`docs/api_reference.md`](api_reference.md) |

# Templates

Starting point for creating your own agents, experts and adapters.
Copy, rename, fill in the `TODO`s. All four templates are importable
and instantiable as shipped. Agent and expert templates execute safe
defaults; the adapter template fails closed until its provider call
and result mapping are implemented.

## Recommended order

1. Copy `expert_template.py` once for each specific task your domain
   needs to resolve (one, two, five: the number is defined by your
   domain, not a fixed rule). When the expert's result is text a
   person will read or hear, copy `phrasing_expert_template.py`
   instead: same contract, plus the separation that keeps the facts
   deterministic and lets the LLM only phrase them.
2. Copy `agent_template.py` once, and register the experts you wrote
   in step 1 there.
3. Inside the agent's `process()`, pick **one** of the three ways to
   close it: they're explained in the file itself. Quick rule:
   - Does your agent only report a state? → option (A).
   - Does it execute something real (message, API) but you're
     prototyping quickly? → option (B), `propose_action`.
   - Does it execute something real and you want the full structural
     guarantee (the agent never touches the real client)? → option
     (C), `submit_action` + a `SETTExecutor` with a registered handler.
4. `orchestrator.register_agent(YourAgent())`. Done.

Adapters are independent of the four steps above and optional: copy
`adapter_template.py` once per external provider you integrate (an
LLM, speech, or sentiment provider). They implement independent
provider contracts rather than Agent or Expert contracts, so adding
one does not change the agent topology you just wrote.

To remove an agent from the system: don't call `register_agent()`
with it. There's nothing else to disconnect.

For the full explanation of each class, see `docs/api_reference.md`.
To understand the reasoning behind the three risk layers and the
ethical filter, see `docs/concepts.md`.

## Where these files live

These templates are repository and source-release resources. They are in
the checkout, in the release ZIP, and in the source distribution. They are
deliberately **not** packaged inside the installed distribution: nothing
under `templates/` ships in the wheel, and `import sett` never exposes
them.

That is the correct shape for what they are. A template is a file you
copy, rename and edit until it is your code; it is not a module the
framework imports, and a copy that stayed importable from `sett` would
invite exactly the mistake the copy exists to avoid. SETT installs from
source (`pip install -e .`), so the checkout you install from is already
the checkout that contains them - the access path is `templates/` in the
source tree, and there is no second one to look for.

`tests/test_adapter_template.py` and `tests/test_phrasing_expert_template.py`
run against these files in every CI cell, so a template that stopped
working would fail the build rather than fail in a user's copy.

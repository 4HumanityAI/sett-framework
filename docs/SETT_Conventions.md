# SETT - Conventions and invariants of the framework

*Reference for writing the arXiv paper. Current code baseline: v0.15.0. The
policy updates below were approved by the maintainer.*

---

## A. Architectural conventions (the heart of the paper)

### 1. Structural growth rule

A new domain gets a new agent; a new responsibility inside a
domain gets a new expert.

- A **new domain** of knowledge/function → a **new agent**, registered under
  that domain in the orchestrator.
- A **new responsibility within an existing domain** → a **new expert** inside
  the agent that already owns that domain. Never a new agent for a
  responsibility, never an expert shared between agents.
- Corollary validated in practice: when an expert accumulates two distinct
  phrasing responsibilities, it splits into two focused experts (precedent:
  the greeting expert that mixed greeting + habit recognition was split into
  two `PhrasingExpert` subclasses with identical observable behavior).
- **Terminology precision note** *(found while writing the paper)*: "domain"
  is a term technically reserved for what an Agent owns, for example
  `SETTAgent.__init__(self, name, domain)`; `SETTExpert` has no `domain`
  parameter at all. When describing this rule (in the paper or in any future
  doc), avoid phrases like "domain-specific" for what an Expert does; use
  "task-specific" or equivalent, to avoid colliding with the framework's own
  vocabulary.

### 2. Experts return; Agents publish. No exceptions

An expert returns results to its agent; only the agent publishes
or acts on the world.

An expert never writes to `UniversalMemory` nor executes effects on the world
on its own: it returns results to its agent, and it is the agent who
publishes. This is the basis for everything passing through the filter.

### 3. Ethics as an architectural layer, not a prompt

The ethical filter intercepts by structure, not because a
component was asked to behave.

The central thesis. No component "behaves because it was asked to": the
`EthicalFilter` intercepts structurally. Every design decision is evaluated
against whether it reinforces or dilutes this thesis.

### 4. Actions as data (`SETTExecutor`), fail-closed

An effect on the world is described as data and runs only after
the filter's verdict; if anything is missing, nothing runs.

Effects on the world are described as `Action` objects and executed only after
the filter's verdict. When the application keeps the real client behind
`SETTExecutor`, the expert has no reference to it, so the guarantee comes from
the structure, not from discipline. SETT cannot stop arbitrary Python code
that keeps its own client or acts outside the executor. Handler routing is
resolved before policy evaluation: an `action_type` with no registered handler
is normalized to `block` and raises `SETTConfigurationError` without
manufacturing an ethical verdict. "I forgot to register the handler" never
degrades into "it ran anyway."

- Two paths coexist deliberately: `propose_action()` (lightweight, low risk)
  and `submit_action()` (structural, critical). Neither is imposed over the
  other.

### 5. The LLM is an engine, not architecture

Deterministic logic produces the facts; the optional LLM only
phrases them.

The system works without an LLM. `PhrasingExpert` formalizes this:
deterministic logic produces the **facts**, and the optional LLM only
**phrases** them. The model sees only what `build_prompt()` sends.
`verify_facts()` is the place to check that the phrasing did not invent or
change a fact; its default returns the text unchanged, so that check is the
application's job. When the LLM is absent, returns nothing or fails with
`SETTLLMAdapterError`, the deterministic `fallback_text()` is used; other
errors from a subclass or a verifier can still propagate. Subclasses implement
`determine_facts()`, `build_prompt()` and `fallback_text()` instead of
overriding `resolve()`; overriding it produces a warning, not a hard
prohibition. None of this guarantees that a model tells the truth.

### 6. Dual memory with asymmetric privacy

Each agent keeps a private memory and shares only final results;
that privacy is a convention, not runtime enforcement.

`PrivateMemory` per agent, inaccessible from outside, is **today a Python
convention (`_` prefix), not enforced at runtime**: verified against the code,
`write()`/`read()` do not validate caller identity in any way. This is an
explicitly documented scope decision in the class's own docstring, not an
oversight. Any text (including the paper) that describes this as an absolute
isolation guarantee needs that nuance; it is exactly the kind of imprecision
already corrected twice before in this project (see #16). Shared
`UniversalMemory` receives **only final results**, never intermediate
reasoning.

### 7. `RiskProfile` never leaves the device/agent that computed it

No `RiskProfile` value reaches the audit log or shared memory.

No `RiskProfile` value leaks into the audit log or shared memory. Verified by
a contract test.

### 8. Three-layer filter, with calculation/documentation separated

The score combines context analysis, the risk profile and the
environment; the human-readable rules do not feed the calculation.

Score = `ContextAnalyzer` (keyword heuristics) modulated by `RiskProfile` and
`EnvironmentalContext` (which *tightens* thresholds, never relaxes them:
`effective_reject = max(1.0, reject_threshold - env_modifier)`).
**`EthicalRuleset.rules` is configuration metadata for human inspection; it
does not feed the numeric calculation or emitted audit entries** (only
`reject_threshold`, `warn_threshold`, `principle` are read).

### 9. Biometric urgency as a review signal, not an override

`human_at_risk` can promote ALLOW to WARN for review, but never
changes the score or forces a threshold.

`human_at_risk` is an urgency and audit signal. It does **not** modify the
harm score and does not force any threshold. Its only effect on a verdict is a
conservative promotion `ALLOW -> WARN` when the action is not classified as
protective, recorded in the audit entry as
`human_at_risk_without_protective_classification`. It never downgrades a
REJECT, and it never penalizes an action a domain analyzer has explicitly
marked protective. The human-life layer remains non-configurable in the sense
that this promotion cannot be switched off, not in the sense that urgency
overrides the score.

*Corrected note (v0.14.0 revision):* this convention previously read
"`human_at_risk` forces the score to the reject threshold regardless of
keywords, since the human-life layer is not configurable away." That stopped
being true in **v0.8.0 (2026-07-25)**, when `SafetyAssessment` separated
`situation_urgency` from `action_harm_risk` and the CHANGELOG recorded that
"`human_at_risk` is now an urgency/audit signal rather than an implicit
near-rejection score"; this convention was not updated at the time and stayed
stale for six minor versions. `docs/api_reference.md` and
`docs/security_model.md` already described the current behavior correctly,
and three tests pin it:
`test_human_at_risk_without_protective_classification_promotes_allow_to_warn`,
`test_human_at_risk_protective_action_remains_allow`, and
`test_human_at_risk_promotion_does_not_downgrade_existing_reject`. Verified by
execution on v0.14.0: a `human_at_risk=True` context reaches `WARN` with
`harm_score=0.0` against an `effective_reject_threshold` of `8.0`. Recorded
here rather than silently rewritten, per #16.

### 10. Explicit compatibility and migration

Existing behavior is kept; a change that cannot keep a contract
needs an explicit, approved migration.

Existing behavior is preserved wherever its meaning can be kept. A change that
cannot keep the contract needs an explicit, approved migration. For example,
the policies 2.0 contract absorbs the earlier policy capabilities instead of
running a second governance engine next to them. Precedents:
`register_analyzer` (a specific analyzer per action_type, generic as a
fallback for everything else), structured attributes on exceptions (optional
kwargs, `str(e)` intact byte for byte), automatic propagation via
`_current_*` (no subclass rewrites needed).

### 11. Fail-honest in scaffolding

What does not exist yet says so, instead of pretending.

What doesn't exist yet is declared, not simulated: `StubDomainAgent` returns a
structured `{"status": "stub", ...}` instead of crashing or fabricating. The
whole system is testable end-to-end before every real piece exists, and
replacing a stub with the real piece is transparent (same domain, zero changes
to the rest).

### 12. Zero mandatory dependencies in the core

The core is pure Python; external services come in through
interchangeable adapters.

Pure Python. External services (LLM, TTS/STT) enter through interchangeable
adapters. Ollama is available as a local adapter: zero cost, no extra pip
installs (only stdlib `urllib`), and offline, consistent with the project's
origin (assistance on modest hardware, no cloud required). The framework does
not pick a default provider; the application chooses and configures its own.

### 13. Specific, structured errors

Errors come from SETT's own hierarchy and carry their data as
attributes.

Its own hierarchy under `SETTError`; the developer gets descriptive errors,
not generic exceptions. Since v0.5.0, exceptions carry their data as
attributes (`.action`, `.score`, `.threshold`, `.principle`, `.reasoning`);
nobody parses `str(e)`.

### 22. External services are not Agents or Experts; the naming marks it

Adapters are interchangeable dependencies that an expert calls,
and their names say so.

*(Historical context: added 07/23 during the STT/TTS/sentiment discussion.
The current terminology register now documents the established framework
names.)*

The structural growth rule (#1) governs the Agent → Expert tree: new domain
→ new agent, new responsibility within a domain → new expert. External
service adapters (`services_llm`, `services_tts_stt`, `services_sentiment`)
are **deliberately outside that tree**, not by omission; #12 already says
so ("external services... enter through interchangeable adapters"): an
adapter has no `PrivateMemory`, is not registered in the orchestrator, does
not participate in routing, and is not subject to the `EthicalFilter`; it is a
dependency an Expert calls, never a participant in the multi-agent system.
Forcing it into the `Agent(Expert1, Expert2...)` shape would be a category
error: it would give memory and a lifecycle to something that only needs to
be an interchangeable function.

The name already signals this in the existing code; this just makes it
explicit:

- `<Modality>Base` - the abstract interface (`LLMBase`, `TTSBase`, `STTBase`).
- `<Provider><Modality>Adapter` - the concrete implementation
  (`GeminiAdapter`, `OllamaAdapter`, `GoogleTTSAdapter`, `ElevenLabsTTSAdapter`).
  Grouped by provider at the **file** level when they share credentials/client
  (`services_tts_stt/google.py` carries `GoogleTTSAdapter` and
  `GoogleSTTAdapter`), but each **class** implements only one interface, never
  a class that mixes `TTSBase` and `STTBase`, so as not to lose test isolation
  or interchangeability.
- `<Domain><Role>Agent` / `<SpecificTask>Expert` - the tree from #1
  (`IdentityAgent`, `UserExpert`, `PhrasingExpert`).
- `<Domain>ContextAnalyzer` - `ethics_ruler` analyzers
  (`EconomicContextAnalyzer`, `RelationalContextAnalyzer`).
- `SETT<Concept>Error` - the entire exception hierarchy.

The suffix, by itself, tells you what layer something is in before you open
the file: `...Adapter` is replaceable infrastructure, `...Agent`/`...Expert`
participates in orchestration and the ethical filter, `...Analyzer` scores
harm. Keeping it consistent is what gives the label its value, not decoration.

### 23. "Ruler" is the highest level, not a synonym for Agent

Rulers are the framework's structural pillars; they govern every
agent, expert and adapter.

*(added 07/23 - completes #22, same criterion: document what was already
practiced without being written down.)*

Packages such as `core_ruler`, `ethics_ruler`, `memory_ruler`, `risk_ruler`,
`audit_ruler`, `biometric_ruler`, `authorization_ruler`, and
`execution_ruler` are not more Agents; they are structural pillars that
**govern** every Agent, Expert, and Adapter that exists in SETT or in an
application built on it. The hierarchy of importance, from most to least
fundamental:

```
*_ruler/      → the framework's constitution: what may exist and under what rules.
                A package-level governance role, not a singleton guarantee.
                Boundary instances may be configured separately. Adding a ruler changes
                a structural governance responsibility and requires an explicit
                decision about its scope.
  Agent/Expert  → domain participants, governed BY the rulers (#1: new domain
                → new agent, new responsibility → new expert).
    Adapter     → interchangeable external capability, called BY an Expert (#22).
```

Practical consequence: the `Ruler` suffix is reserved for new pillars of the
framework itself, not for an application's important domain agent
(`IdentityAgent` is still an `Agent` even though it is central to a consuming
application; it does not become `IdentityRuler` just for being important). And
for the same reason a new Expert weighs less than a new Agent (#1), and a
change to the core weighs more than one to an app (#14, #15): a new `*_ruler`
is a candidate requiring an explicit author decision and a responsibility that
does not duplicate an existing pillar. It is not subject to a general gate of
two deployed consumers; the evolution rules below apply.

The suffix may also name the class that implements the defining boundary of a
new pillar. Existing boundary classes that predate this convention, including
`EthicalFilter`, keep their established names for compatibility; this does not
make `EthicsRuler` an automatic or desirable alias.

Note for work in progress (STT/TTS/sentiment): `ContextAnalyzer`'s own
docstring already mentions a `SentimentAnalyzerAgent` as a planned
integration, confirming that sentiment analysis enters as an **Adapter** that
a future domain **Agent** (probably first in a consuming application, see rule
#14) consumes and publishes as `emotional_state`, not as a new `*_ruler`. The
day emotional state stops being a string that different Agents decorate and
becomes a structure as central as `RiskProfile`, that is when evaluating a new
pillar would be worth it, not before.

**Update (same day):** `biometric_ruler` was in fact already created; this
does not contradict the paragraph above, it confirms it. The difference isn't
"biometrics matters more than sentiment," it's that biometrics already had
real evidence of use (the former rule "extracted from use, not speculated"):
the documented v0.1.1 bug (flat vs. nested data) is a concrete conflict
already found, not anticipated. When this was written, the newly added
sentiment adapter had no downstream consumer, which is why it did not qualify
under the rule of that time; today, evolution led by the author follows
convention #14 below. `biometric_ruler` is documented in
`docs/api_reference.md` and is the first real case of this convention
applied, useful as a concrete reference the next time a new pillar is
evaluated.

---

## B. Process conventions (methodologically citable in the paper)

### 14. Author-led evolution and community proposals

The author can start a change; community proposals show where
they help; the author decides.

The author may initiate a generic capability or architectural change without
waiting for external adoption. Community proposals are assessed for usefulness
in two or more concrete situations, not necessarily two implemented
applications. The author makes the final decision. This explicitly supersedes
the former general two-independent-downstream-uses rule; historical release
rationale remains history.

### 15. Explicit scope and preserved contracts

Every change starts from a clear decision and keeps existing
guarantees unless a contract change is named.

Every change starts from a clear decision and says what behavior it expects
and how that will be shown. Reorganizing existing capabilities keeps their
behavior and guarantees; a change to a contract is named as such. Policy that
belongs to one application stays out of generic mechanisms. Work starts only
once it is authorized.

### 16. Honest history

Corrections are recorded openly, never silently rewritten.

Corrections to the changelog/docs are recorded explicitly ("Corrected note"),
never silently rewritten. Known limitations are documented as scope decisions.
README claims are adjusted to match what the code actually does, not the
other way around (precedent v0.2.1: "every action" → "every action submitted
as an Action... and every write to universal memory"; precedent from an
earlier revision: "inaccessible" → "inaccessible by convention, not
runtime-enforced", see #6).

### 17. Empirical verification of claims

Claims are verified by running the code, not by reading it.

Every release claim is verified by running the code (full suite, examples,
diff between versions), not by reading it. Refactors are validated by
identical observable behavior (precedent: byte-for-byte identical verdicts
after migrating to `register_analyzer`).

### 18. Verification with unanticipated language

Every delivery is also tested with new phrases that no test has
seen before.

Beyond running the existing suite, every delivery is tested against new
phrases, not present in any prior test or demo, using the same method that
found both the vocabulary misalignment (in an early build of the
companion-assistant application: a domain expert recognized "note that"
phrasing that the intent classifier didn't) and the unbounded-substring
matching ("add it up" containing "sad," "depression" containing
"pressure"). A passing test confirms the code does what the author
anticipated; a new phrase confirms it does what a real user would
actually say. Direct material for the Evaluation section: neither of
these two findings would have surfaced by running only the existing
suite.

---

## C. Project process conventions (not going into the paper as thesis, but governing how it's built)

### 19. Don't hold up what's finished because of what's undecided

A ready version does not wait for open decisions, but it is
published only with explicit approval.

A version that is ready and verified does not have to wait for a future
decision that is still open, even one about the same component. Being ready
does not publish it by itself, though: publication needs its own explicit
approval. Waiting only makes sense when both parts already exist and are
finished (see #21, "don't fragment"); waiting for something with no date or
decision made is the same rule applied backwards. Precedent: v0.5.0 was
published without waiting for `PrivateMemory` persistence (then under the
former two-instances rule, whose threshold was already met) to be resolved;
they are two independent decisions, not a condition of one another.

---

## D. Repo governance conventions (not going into the paper, but governing how it's written)

### 20. Public naming

Public text never names internal projects or internal process
labels.

No internal project name appears in public SETT (nor in the paper, if the
paper describes SETT generically): use "two independent projects built on
SETT," "an early prototype application," "a companion-assistant
application." A `grep` sweep happens before closing any new text. The same
applies to the project's internal process labels, such as numbered work
batches, decisions, options, audit findings and test-plan cases: public text
uses plain names that explain themselves, and internal labels stay in
internal records. A guard test in the public tree checks the process labels;
the project-name check runs as a private control before publication, so the
public tree carries no derivative of those names. Published history is never
rewritten, so released changelog entries keep the labels they were published
with; the rule applies from its adoption onward.

### 21. Versioning

New public API means a minor release, documentation alone means
a patch, and nothing is bumped without explicit confirmation.

Additive with new public API → minor (0.x.0). Docs only → commit or patch, no
new release if the package itself doesn't change. Nothing gets bumped without
explicit confirmation backed by evidence in hand. Don't fragment two
already-finished pieces of work into separate releases if they're going to be
published at the same time (the flip side of #19: that protects against
splitting up what's already ready; #19 protects against holding back what's
already ready).

**Explicit v0.13.2 exception.** `PocketTTSAdapter` is additive public API in
the `sett.services_tts_stt` submodule and would ordinarily require a minor
release under this convention. The maintainer explicitly chose to keep it in
the v0.13.2 patch, which was unpublished at the time of that decision, because
Pocket TTS became known only days before that release closed and met an
immediate need for a local CPU TTS option. Splitting one completed review
cycle into two unpublished versions would create history rather than preserve
it. This is a recorded one-time exception, not a reinterpretation of public
API and not a precedent that weakens the default rule for future additive
symbols.

---

*Documentation correction:* the former closing note said rule #1 existed only
in a private application, although it is already stated in this file. The
current naming reference is [terminology](terminology.md). Historical examples
do not establish present implementation status or adoption.

## E. How changes are approved

Each step of a change needs an explicit decision or a verification: how a
source is interpreted, the observable behavior, the public contract, the
design, the code diff, the acceptance tests, the documentation and the release.
Work moves in small steps, each naming its exact revisions and the decision it
comes from. Automated checks help the review but cannot approve a change.
Terminology, public compatibility, migration, dependencies and guarantees need
the maintainer's approval. An open ambiguity stops the work that depends on it;
silence never resolves it. A replacement names what it replaces, why, its scope
and its date. A newer draft has no authority just for being newer.

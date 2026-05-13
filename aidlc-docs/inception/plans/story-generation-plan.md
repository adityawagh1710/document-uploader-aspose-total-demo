# Story Generation Plan — office-converter (Local v1)

## Purpose

Translate the existing `requirements.md` (10 FRs + 8 NFRs) into
user-centered stories with acceptance criteria. Stories reference
FR/NFR numbering so existing Code Generation traceability is
preserved.

## Plan Checklist

- [ ] Collect answers to story-generation questions (this document)
- [ ] Analyze answers for ambiguities; ask follow-ups if needed
- [ ] Generate `aidlc-docs/inception/user-stories/personas.md`
- [ ] Generate `aidlc-docs/inception/user-stories/stories.md`
- [ ] Verify INVEST criteria: Independent, Negotiable, Valuable,
  Estimable, Small, Testable
- [ ] Verify each FR is covered by at least one story
- [ ] Present completion message and wait for approval

## Story-Generation Questions

Each pre-filled with `[Answer]: PROCEED — locked 2026-05-11` for one-shot lock-in.

---

### Q1 — Persona scope

Which personas should the stories cover?

A) **Three personas**: Pipeline Developer (HTTP API consumer),
   DevOps Operator (container lifecycle), Upstream End User
   (indirect, flagged as out-of-direct-scope but mentioned for
   completeness).
B) **Two personas**: Pipeline Developer and DevOps Operator only —
   skip indirect end users.
C) **One persona**: a single "Service Consumer" that conflates
   developer and operator concerns. Simplest.

**Recommendation (proceed default): A — three personas.**

**Rationale:** Pipeline Developer and DevOps Operator have
genuinely different concerns (API contract vs operational
posture) — collapsing them creates muddled stories. Upstream End
User is indirect but worth naming so v2 cloud work can extend it
cleanly (e.g., "Tenant Admin" persona builds on this).

[Answer]: PROCEED — locked 2026-05-11

---

### Q2 — Story granularity

How fine-grained should stories be?

A) **Epic-level** (~5 stories): one per major capability (Convert,
   Health, Cache, License lifecycle, Failure visibility).
B) **Fine-grained** (~20–30 stories): one per concrete acceptance
   case (e.g., "convert a small DOCX", "reject input > 1 GB",
   "subdivide on chunk OOM", etc.).
C) **Mid-grained** (~15–20 stories): one per FR + one per
   significant NFR, with multi-criterion acceptance.

**Recommendation (proceed default): C — mid-grained.**

**Rationale:** Matches the granularity of the 10 FRs + 8 NFRs in
`requirements.md`. Avoids the fragmentation of B (20+ stories for
a single-endpoint service is over-engineering). Epics (A) lose
testability. C gives each FR/NFR a story home without explosion.

[Answer]: PROCEED — locked 2026-05-11

---

### Q3 — Breakdown approach

How are stories organized in `stories.md`?

A) **Persona-based** — top-level sections per persona, stories
   grouped underneath. Reader navigates by "who cares about this".
B) **Feature-based** — top-level sections per system feature
   (HTTP API, License, Cache, etc.). Reader navigates by "what
   does this story affect".
C) **FR-based** — top-level sections per requirement (FR-1 through
   FR-10 + NFRs). Reader navigates by traceability number.

**Recommendation (proceed default): A — persona-based.**

**Rationale:** Stories are user-centered by definition; organizing
by persona reinforces that lens. Cross-references to FR numbers in
each story body preserve traceability without making FR-grouping
the primary axis. B (feature-based) confuses stories with technical
modules. C (FR-based) makes the doc a renumbering of
requirements.md, redundant.

[Answer]: PROCEED — locked 2026-05-11

---

### Q4 — Acceptance criteria format

How are acceptance criteria written within each story?

A) **Gherkin (Given / When / Then)** — one or more scenarios per
   story. Testable, mappable to BDD frameworks if we ever adopt
   one.
B) **Bullet checklist** — list of testable statements per story.
   Less ceremony, less explicit about preconditions.
C) **Prose** — narrative paragraph describing the expected
   behavior. Hardest to test.

**Recommendation (proceed default): A — Gherkin.**

**Rationale:** Each scenario has explicit preconditions (Given),
trigger (When), and observable outcome (Then). Maps directly to
test code structure (pytest's `arrange / act / assert`). Even
without a BDD framework, the discipline matters.

[Answer]: PROCEED — locked 2026-05-11

---

### Q5 — Story sizing / estimation

Include estimates or story points?

A) **None** — v1 has no sprint planning, no team. Skip estimation.
B) **T-shirt sizes** (S/M/L) — informal sizing for the
   maintainer's mental model.
C) **Story points** (Fibonacci) — formal agile estimation.

**Recommendation (proceed default): A — none.**

**Rationale:** v1 is being built end-to-end by an AI agent +
maintainer. There is no team velocity to track, no sprint
allocation. Estimation is overhead without payoff. C is overkill;
B adds noise.

[Answer]: PROCEED — locked 2026-05-11

---

### Q6 — Negative stories (explicit non-goals)

Include stories that capture what the system explicitly does NOT
do (e.g., "as a caller, I do NOT expect auth so I should not
send credentials")?

A) **Yes**, as a small section "Explicit Non-Goals" at the end of
   `stories.md`, calling out the v1 limitations that callers and
   operators need to know about (no auth, no per-tenant quotas,
   no metrics endpoint, etc.).
B) **No** — keep stories aspirational only. v1 limitations are
   already in `requirements.md` Out-of-Scope section.

**Recommendation (proceed default): A — yes, small section.**

**Rationale:** A pipeline developer reading `stories.md` should
know what NOT to expect, not just what to expect. The Out-of-Scope
section in `requirements.md` is engineering-internal; the user-
story doc faces callers. Duplication is cheap; misunderstanding
isn't.

[Answer]: PROCEED — locked 2026-05-11

---

### Q7 — Anything else?

Personas, stories, edge cases, or constraints I should capture
that aren't already in `requirements.md`?

[Answer]: PROCEED — locked 2026-05-11

---

**When you're done**, reply "proceed" to lock all defaults, or
override specific questions.

After approval, I'll generate `personas.md` and `stories.md`, then
resume Code Generation Part 2.

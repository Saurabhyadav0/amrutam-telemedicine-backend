---
name: engineering-workflow
description: The default end-to-end development loop for this project — delegate investigation to subagents, design with SOLID, build test-first, and gate on security and production readiness before calling anything done. Use at the start of ANY coding task, feature, bug fix, or refactor unless the user explicitly asks for a quick throwaway answer.
---

> Adapted from [devnouiq/engineering-standards-skills](https://github.com/devnouiq/engineering-standards-skills) (MIT, © 2026 Ujjwal Garg). See `.agents/skills/THIRD_PARTY_NOTICES.md`.

# Engineering Workflow

The house rules for how work gets done here. This skill sequences the others — read it
first, then invoke each stage's skill when you reach that stage.

## Principles

1. **Delegate investigation.** Research and cross-file analysis go to subagents; decisions
   and edits stay in the main thread.
2. **Design before typing.** Know the seam before writing the code that sits on it.
3. **Test-first for logic.** Behaviour that matters gets a failing test first.
4. **Security is not a phase.** It is a constraint on every line.
5. **Done means shippable.** Not "it runs on my machine".

## The loop

### Stage 0 — Orient
Read the repo's `AGENTS.md` / instruction files and any session state before touching
code. Confirm which feature you are on and that the baseline verification passes. Do not
stack new work on a broken starting state.

### Stage 1 — Investigate (subagents)
→ **`subagent-first`**

Spawn subagents to answer *where*, *what calls*, *what breaks*, *how does the current
implementation work*. Launch independent investigations in parallel in one batch. Verify
any file a subagent names before editing it. Summarise findings back to the user —
subagent output is invisible to them.

### Stage 2 — Design
→ **`solid-engineering`**, plus **`amrutam-backend`** for this repo's module and saga conventions

Decide the seam, the interface, and what stays hidden. Prefer a deep module with a small
surface. Confirm the design lets the next variant be added without editing existing files.

For a genuinely uncertain design, write the trade-off down (an ADR-style note in the PR or
`docs/architecture.md`) or spike it on a branch before committing.

### Stage 3 — Specify

For anything beyond a small change, write down the requirements and acceptance criteria
before implementing. Ambiguity resolved now is cheaper than a rewrite later.

### Stage 4 — Build test-first
→ **`amrutam-testing`**

Red → green → refactor. Cover the failure paths, not only the happy path. Tests must be
deterministic — no wall-clock, no network, no ordering dependence.

While implementing, hold to `solid-engineering`:
- change only what the task requires
- no abstraction for a single call site
- domain logic free of framework and vendor imports
- dependencies injected, not imported inside the logic

### Stage 5 — Security gate
→ **`secure-by-default`**

Apply the non-negotiables while writing, then run **`amrutam-security-review`** against the
**diff** (it maps each surface to this repo's controls in `docs/security.md`).

Report findings. Do not silently change authn/authz, crypto, or validation semantics —
propose and confirm.

### Stage 6 — Production readiness gate
→ **`production-readiness`**

Walk the checklist: error handling, structured logging with correlation ids, metrics for
new failure modes, config validated at startup, timeouts and bounded retries, bounded and
indexed queries, backward-compatible migration, graceful shutdown, real rollback path.

### Stage 7 — Review
→ delegate the diff to a reviewer subagent, then run **`amrutam-security-review`**

Review the diff against both the repo's standards and the original spec. Fix what the
review surfaces before declaring done.

### Stage 8 — Close out
Record evidence that verification actually ran (`make lint typecheck security test`, and
`make smoke` for anything touching a flow), plus any unresolved risk. Leave the repo
restartable from the standard startup path (`make up`). Commit with a descriptive message.

## Debugging deviation

When something is broken rather than missing, skip to **`diagnose`**. Reproduce
first, form a hypothesis, prove it with evidence, *then* fix. Do not retry the same
approach hoping for a different result. Once fixed, re-enter at Stage 4 with a regression
test.

## Definition of done

A change is done only when all of these are true:

- [ ] Target behaviour implemented and scoped to the task
- [ ] Design holds up against SOLID — one reason to change, extension without modification,
      dependencies inverted
- [ ] Tests exist for happy path *and* failure paths, and they pass
- [ ] `secure-by-default` gate run against the diff; findings resolved or explicitly accepted
- [ ] `production-readiness` checklist satisfied or each gap waived with a reason
- [ ] Verification actually ran — with recorded evidence, not an assumption
- [ ] Repo left clean and restartable

## Anti-patterns

- Hand-grepping across ten files instead of spawning one subagent
- Writing the implementation first and "adding tests later"
- Treating the security review as a post-merge activity
- Marking a feature complete because code was added, without running verification
- Opportunistic refactors bundled into a feature diff
- Building an abstraction for a variant that does not exist yet

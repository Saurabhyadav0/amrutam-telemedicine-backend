---
name: solid-engineering
description: Apply SOLID principles and scalable design when writing or refactoring non-trivial code. Use when adding a class/module/service, when a file is growing past a few responsibilities, when the user mentions "SOLID", "clean code", "design", "architecture", "coupling", "refactor", "make this scalable", "this is getting messy", or when a change requires editing many files to add one behaviour.
---

> Adapted from [devnouiq/engineering-standards-skills](https://github.com/devnouiq/engineering-standards-skills) (MIT, © 2026 Ujjwal Garg). See `.agents/skills/THIRD_PARTY_NOTICES.md`.

# SOLID Engineering

Design for the second change, not the first. Code is read and modified far more often than
it is written — optimise for the person who has to extend it.

## The five principles, applied

### S — Single Responsibility
A module has **one reason to change**. Not "one function", *one axis of change*.

Smells: a class named `...Manager`/`...Helper`/`...Utils`; a file mixing HTTP parsing,
business rules, and SQL; a change to logging forcing a change to pricing.

Fix: split along the axis that changes. Route/controller → service → repository. Each
layer changes for its own reason.

### O — Open/Closed
Open for extension, closed for modification. Adding a new *variant* should add a file, not
edit a switch statement in six places.

Smells: `if provider == "a" ... elif provider == "b"` repeated across the codebase;
every new integration touching the same core file.

Fix: define the contract, register implementations. A `dict`/registry of strategies beats
a growing conditional.

### L — Liskov Substitution
A subtype must be usable anywhere the supertype is, with no caller-side special-casing.

Smells: `if isinstance(x, SpecialCase)`; an override that raises `NotImplementedError`; a
subclass that tightens preconditions or weakens guarantees.

Fix: if the subtype cannot honour the contract, it is not a subtype. Use composition.

### I — Interface Segregation
Small, focused interfaces. Do not force an implementer to stub methods it does not need.

Smells: a "god" protocol with 15 methods where most implementations no-op half of them;
tests forced to mock unrelated methods.

Fix: split the protocol by consumer. `Readable` and `Writable` beat `Storage`.

### D — Dependency Inversion
High-level policy must not depend on low-level detail. Both depend on an abstraction.

Smells: business logic importing `boto3`/`psycopg`/`requests` directly; a service that
cannot be unit-tested without a live database.

Fix: inject the dependency through the constructor or function signature. Compose at the
edge (main/factory/DI container). This is what makes the code testable.

## Deep modules over shallow ones

Prefer a **simple interface hiding substantial implementation**. A module that exposes ten
methods to do one job is a leaky abstraction — the complexity just moved to the caller.

- Interface surface small; behind it, real work.
- Push special cases *down* into the module, not up to every caller.
- If every caller must call `init()` then `configure()` then `run()`, the module owes them
  a single `run()`.

## Scalability, concretely

Design decisions that keep the system able to grow:

- **Statelessness** — no in-process state that breaks when a second replica starts.
  Session, cache, and locks belong in shared infrastructure.
- **Bounded work** — every loop, query, and fetch has a limit. Paginate. Never `SELECT *`
  without a bound, never load an unbounded list into memory.
- **N+1 elimination** — batch at the boundary. One query for N rows, not N queries.
- **Idempotency** — any operation that can be retried must be safe to retry. Use natural
  keys or idempotency tokens for writes.
- **Backpressure & timeouts** — every network call gets a timeout and a retry policy with
  jittered backoff. No unbounded queues.
- **Async for I/O-bound, workers for CPU-bound.** Do not block the request path on work
  the user is not waiting for.
- **Indexes before scale, not after.** Every query path has a supporting index.

## Boundaries and coupling

- Depend on **interfaces you own**, not on shapes a vendor may change.
- Keep the domain free of framework imports. Framework code lives at the edge.
- One-way dependencies. If two modules import each other, the boundary is wrong.
- Data crossing a boundary is validated **at** the boundary, once.

## Discipline while implementing

- Change only what the task requires. No opportunistic refactors mid-feature.
- Do not invent abstractions for a single call site — **two** occurrences is a coincidence,
  three is a pattern.
- Delete dead code rather than commenting it out.
- No new global mutable state.
- Keep functions short enough to hold in your head; extract when a comment is needed to
  explain a block.

## Check before declaring done

- Could I add the next obvious variant without editing existing files?
- Can each new unit be unit-tested without network, disk, or a live DB?
- Is there exactly one place that knows each rule?
- Does the public surface leak an implementation detail (a driver type, a raw row, a
  vendor exception)?

---
name: subagent-first
description: Delegate investigation, research, and cross-file analysis to subagents instead of hand-searching. Use at the START of any non-trivial task, and whenever the user says "find every place that", "how does X work", "map this", "audit", "review the branch", "why is this failing", or asks a question that spans more than one or two files. Also use when the main context is getting long and work can be pushed into an isolated agent.
---

> Adapted from [devnouiq/engineering-standards-skills](https://github.com/devnouiq/engineering-standards-skills) (MIT, © 2026 Ujjwal Garg). See `.agents/skills/THIRD_PARTY_NOTICES.md`.

# Subagent-First

Default to delegation. The main thread is for decisions, edits, and verification — not
for grinding through greps and file reads. If a step is *read-mostly* and spans more than
one or two files, a subagent should do it.

## The rule

> **Investigate in a subagent. Decide and edit in the main thread.**

Doing ten `grep` + `read_file` round-trips inline burns the context window that the
actual implementation needs. A subagent does the same work in its own context and returns
a compact answer.

## When to delegate — always

Delegate without asking when the task is any of these:

- "Where is X defined / what calls Y / list every use of Z"
- Mapping a directory, module, or migration surface
- Extracting a contract, API shape, or config inventory
- Cross-file impact analysis before a refactor
- Reviewing a diff, branch, or PR
- Researching library/API behaviour from docs or the web
- Any question you cannot confidently answer after two file reads

## When NOT to delegate

Keep these in the main thread:

- Correctness-critical edits — you need exact control over the bytes written
- Running and interpreting tests, builds, migrations
- Anything requiring approval from the user
- A single known file you already have the path for
- Trivial one-line lookups

## Parallelism

Subagent calls are independent tool calls. **Launch every independent investigation in one
batch.** Three parallel subagents cost one round-trip; three sequential ones cost three.

Do not parallelise when a later question depends on an earlier answer.

## Choosing an agent

| Need | Agent |
| --- | --- |
| Locate code, map a surface, answer "where/what/how many" | a read-only explore/search agent |
| Review a diff or branch | a reviewer agent given the diff and `docs/security.md` |
| Bounded 1–2 file mechanical edit | a general-purpose agent with the exact files named |
| Anything else | the platform's general-purpose subagent |

Ask for compact, structured output (tables, `file:line` lists) when context pressure is
the concern.

## Writing the prompt

Subagents are **stateless** and see none of this conversation. A vague prompt returns a
vague answer, and you pay for the round-trip anyway. Every delegation prompt must carry:

1. **Goal** — the concrete question, in one sentence.
2. **Scope** — which directories/globs to search, and which to ignore.
3. **Context** — the repo facts the agent cannot infer (framework, entry points, naming).
4. **Mode** — state explicitly: *research only, do not write code* — or *make this edit*.
5. **Return contract** — the exact shape you want back (e.g. "a table of `file:line` →
   symbol → caller", "a list of every module importing `db.session`").

Weak: `look at the auth code`
Strong: `Read-only. In backend/app/, find every route handler that reads request state
without an authorization check. Return a table: file:line | handler name | what it reads |
whether an auth dependency is present. Do not edit anything.`

## After a subagent returns

- **Subagent output is invisible to the user.** Always summarise the findings back.
- Treat the result as a lead, not as truth. Before editing a file a subagent named, open
  it and confirm the line still says what the report claims.
- Never paste a raw subagent transcript into the conversation.

## Anti-patterns

- Running five sequential greps that a single subagent could have answered
- Delegating an edit you have not scoped — the agent will guess and over-reach
- Spawning a subagent for a file you already have open
- Firing dependent subagents in parallel and stitching mismatched answers together
- Acting on a subagent's claim without verifying the file

---
name: secure-by-default
description: Build security into the change instead of auditing it in afterwards. Use whenever writing or reviewing code that touches authentication, authorization, user input, secrets, file paths, database queries, HTTP calls to user-supplied URLs, deserialization, uploads, crypto, or LLM prompts — and as a mandatory gate before any feature is called done. Also use when the user mentions "security", "secure", "vulnerability", "OWASP", "injection", "secrets", "auth", or "is this safe".
---

> Adapted from [devnouiq/engineering-standards-skills](https://github.com/devnouiq/engineering-standards-skills) (MIT, © 2026 Ujjwal Garg). See `.agents/skills/THIRD_PARTY_NOTICES.md`.

# Secure by Default

Security is a property of the code you write, not a review you run later. Apply this while
implementing — then confirm with the dedicated audit skills before shipping.

## Non-negotiables while writing code

**Input is hostile until proven otherwise.**

- Validate at the trust boundary with an allow-list (type, range, length, format). Reject,
  do not sanitise-and-hope.
- **Injection** — parameterised queries only. Never build SQL, shell, LDAP, or a template
  by string concatenation with user data. No `eval`, no `exec`, no shell=True with
  interpolated input.
- **Path traversal** — resolve and confirm the canonical path is inside the allowed root
  before any file operation.
- **SSRF** — a URL that came from a user is fetched only against an allow-list, with
  redirects disabled or re-validated, and never against link-local/metadata ranges.
- **Deserialization** — no `pickle`, no `yaml.load`, no unrestricted object mapping on
  untrusted bytes. Use a schema.
- **Output encoding** — encode for the sink (HTML, attribute, URL, SQL, shell). Never mark
  user content as safe/raw.

**Authorization is checked on every request, on the object.**

- Authenticate *and* authorize. A valid token is not permission.
- Check ownership of the specific object being accessed (IDOR is the most common real bug).
- Deny by default. New endpoints are protected unless explicitly made public.
- Enforce server-side. Hiding a button is not access control.

**Secrets never touch the repo.**

- No keys, tokens, passwords, or connection strings in source, tests, fixtures, or logs.
- Load from env or a secret manager; validate presence at startup.
- Redact secrets and PII at the logger. Assume every log line will be read by someone.
- Rotate anything that was ever committed — removing it from HEAD does not remove it from
  history.

**Crypto: use the boring, standard thing.**

- Passwords: argon2/bcrypt/scrypt. Never MD5/SHA-1/SHA-256-of-password.
- Randomness for tokens: a CSPRNG (`secrets`, `crypto.randomBytes`), never `random`/`Math.random`.
- TLS verification stays on. No `verify=False`, no disabled cert checks.
- Do not invent a scheme. Use the platform library with a modern AEAD mode.

**Dependencies**

- Pin versions; use the lockfile. Review what a new dependency pulls in.
- Do not add a dependency to avoid ten lines of code.
- Check known advisories before adding or upgrading.

**If the code calls an LLM**

- Treat model output as untrusted input — never feed it straight into a shell, query, or
  `eval`.
- Treat retrieved/external content as untrusted instructions. Isolate it from the system
  prompt.
- Constrain tool access to least privilege; require confirmation for destructive actions.

## Gate before done

Run **`amrutam-security-review`** against the diff — not the whole repo — and resolve
findings before shipping. It walks these surfaces for this codebase:

| Surface touched | Skill to run |
| --- | --- |
| Any code change | `secure-code-review` |
| Web app / server-rendered UI | `owasp-top-10-web` |
| REST or GraphQL endpoint | `api-security` |
| New or upgraded dependency | `dependency-scanning` |
| New component or data flow | `threat-modeling` |
| Config, env, credential handling | `secrets-management` |
| CI/CD workflow change | `pipeline-security` |
| LLM integration or RAG | `llm-top-10`, `prompt-injection` |
| Agent with tool access | `agent-security` |

The table names the generic audit lenses; in this repo they are all covered by
`amrutam-security-review` and the controls catalogue in `docs/security.md`.

## Reporting findings

- Report; do not silently "fix" security behaviour. Propose the change and get confirmation
  before altering authn/authz, crypto, or validation semantics.
- For each finding give: location, the vulnerable pattern, the concrete impact, and the
  specific fix. No generic advice.
- Rank by exploitability against *this* deployment, not by CVSS in the abstract.
- Do not report a theoretical issue on a code path that cannot be reached — say so instead.

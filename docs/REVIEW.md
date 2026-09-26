# Review policy

Adversarial reviews are good at finding problems and have no reason to stop.
Every finding is cheap to raise and expensive to fix, so without a policy each
round produces more code, and the new code produces more findings.

This document is the policy that decides which findings get fixed. It is read by
the reviewer, by the triager and by whoever runs the review. The procedure that
applies it is the `adversarial-review` skill (`.claude/skills/adversarial-review/`).
Decisions are recorded in a ledger (§7) so a settled finding stays settled.

Rules have stable ids (E1, F2, D5, …). Every triage decision cites one, so a
decision can be checked against the rule it claims to follow.

---

## 1. Scope

Applies to adversarial reviews of plans (`docs/*_PLAN.md` and the like) and of
diffs. Findings from other sources — a PR review bot, a person, a pasted list —
go through the same triage.

## 2. The envelope

A finding is only a defect relative to what the system is meant to handle. That
is the envelope. A plan states its own under a `## Review envelope` heading;
anything it does not state falls back to the defaults below.

A finding that only holds outside the envelope is not a defect (D1). If the
reviewer thinks the envelope itself is wrong, that is an **envelope question**
for the user, decided once — not a finding.

**Default envelope**

- **E1 — Deployment.** One self-hosted VPS: one API process, optional RQ workers
  that each run one job at a time (`docs/DEPLOYMENT_VPS.md`). Sized for a few
  hundred users and a few thousand trips. Failures that need several API hosts,
  horizontal scaling, or data volumes well beyond that are out.
- **E2 — Untrusted input.** Everything a client sends (web, Android, iOS),
  including requests that arrive through public share and join links, and every
  response from a third party (Strava, Polarsteps, Immich, Overpass, HAFAS,
  Google). These can be malformed, late, missing or hostile.
- **E3 — Trusted.** Data our own code wrote to our database, the server config,
  and the admin.
- **E4 — Validated shapes.** FastAPI validates request bodies against their
  Pydantic models before handler code runs. Handlers may assume the shape;
  they may not assume the values are allowed (ownership, limits, state).
- **E5 — Clients in the wild.** Installed mobile builds cannot be forced to
  update, so older clients keep calling the API after a server deploy.
- **E6 — End-to-end encryption.** For encrypted data the server holds only
  ciphertext. A finding that needs the server to read plaintext is a design
  change, not a bug.

## 3. Finding schema

A finding has every field below. Anything missing a field is not a finding: it
is sent back once, then dropped.

| Field | Values |
|---|---|
| `id` | `R<round>-<n>`, e.g. `R1-3` |
| `title` | One line |
| `location` | `file:line`, or the plan section |
| `trigger` | **Actor → action → observable outcome.** "A companion opens a shared trip while the owner deletes a stop → the companion gets a 500." |
| `trigger_class` | `concrete` — a normal user or input does it. `plausible` — needs an unusual but reachable sequence (a race, a retry, a third-party outage). `theoretical` — needs something no current code path or actor produces. |
| `impact` | `security`, `data-loss`, `silent-wrong`, `wrong-visible`, `degraded-ux`, `cosmetic`, `maintainability` |
| `detectability` | `silent`, `logged`, `user-visible` |
| `later_cost` | `expensive` if the fix would cross a boundary that is costly to change afterwards: the API contract used by shipped clients (E5), the DB schema or a migration, data already persisted, an exported file format, a security property. Otherwise `cheap`. |
| `fix_size` | `S` (a few lines), `M` (one module), `L` (several modules, or a design change) |
| `fix_risk` | `local`, or `shared` if the fix touches code other features depend on |
| `confidence` | `verified` — the code path was read end to end or reproduced. `inferred` — it was not. |
| `evidence` | What was read or run to support the finding |

"Could happen if…" without an actor is `theoretical`, however it is worded.

## 4. Hard floors

These categories are never argued away by triage.

- **F1 — Security.** Access to another user's data, an authorization bypass,
  injection, a leaked secret or token.
- **F2 — Data loss or corruption** of persisted data.
- **F3 — Silent wrong result.** `impact: silent-wrong` — the user gets a wrong
  answer and nothing tells them.
- **F4 — Expensive later.** `later_cost: expensive` with a `concrete` or
  `plausible` trigger: once shipped, the fix costs a migration, a contract
  version or a data repair, so waiting only makes it dearer.

A floor finding that is `inferred` is verified by the triager before it is
decided. Only the user can reject a floor finding.

## 5. Decisions

Apply the rules in order; the first match decides.

| Rule | When | Decision |
|---|---|---|
| **D1** | Only holds outside the envelope (§2) | Reject — or an envelope question if the envelope looks wrong |
| **D2** | Same as a ledger entry, with no new evidence | Duplicate — no action |
| **D3** | F1–F3, trigger `concrete` or `plausible` | Fix now |
| **D4** | F1–F3, trigger `theoretical` | Guard, and flag to the user |
| **D5** | F4 | Fix now |
| **D6** | Trigger `concrete`, impact `wrong-visible` | Fix now |
| **D7** | Trigger `concrete`, `fix_size: S` and `fix_risk: local` | Fix now |
| **D8** | Trigger `concrete`, otherwise | Defer, with a revisit trigger |
| **D9** | Trigger `plausible`, `detectability: silent` | Guard |
| **D10** | Trigger `plausible`, otherwise | Defer, with a revisit trigger |
| **D11** | Trigger `theoretical` | Reject |

What each decision means:

- **Fix now** — fixed in this change.
- **Guard** — no handling code. Add the cheapest thing that would tell us it
  happened: a log line, a metric, an assertion. If it fires, it comes back as a
  `concrete` finding with evidence.
- **Defer** — recorded with a *revisit trigger*: the condition that would make
  it worth fixing ("revisit if trips get more than one editor").
- **Reject** — recorded with the rule, so it is not raised again.
- **Duplicate** — points at the existing ledger entry.

`maintainability` findings about code the change does not touch are out of
scope for the review; they fall through to D8 or D11 like anything else, and
never widen the change.

## 6. Rounds

- **Round 1** reviews the whole plan or diff.
- **Round 2 onwards** reviews only the fixes made since the previous round.
- **Stop** when a round produces no Fix now decision.
- **Cap at three rounds.** A fourth needs the user to ask for it.

## 7. The ledger

One file per plan or feature, `docs/reviews/<slug>.md`, committed with the
change. `<slug>` is the plan file's name without extension, or the branch name.
Every finding gets an entry, including rejected ones: the recorded reason is
what stops the next round raising it again.

```markdown
# Review ledger — <subject>

Subject: <plan path, or branch>
Envelope: <plan section, or "REVIEW.md defaults">

## Round 1 — <date>, reviewed at <commit>

### R1-3 — <title>
- Trigger: <actor → action → outcome>
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D5)
- Revisit when: — (Defer only)
- Guard: — (Guard only: what was added)
- Override: — (or: "user: <decision> — <reason>")
- Outcome: open
```

`Outcome` starts as `open` and ends as one of: `fixed`, `guard added`,
`never happened`, `guard fired`, `caused #<issue>`.

## 8. Changing this policy

This file changes only in a commit made for that purpose, never during a triage
to fit the finding in front of you.

What drives a change is the `Outcome` column across ledgers:

- A Rejected, Deferred or Guarded entry that ended as `caused #<issue>`: the
  rule that let it through is too loose.
- A kind of finding that is always Fix now and always ends `never happened`:
  the rule that forces it may be too strict.
- Frequent user overrides of the same rule: the rule is wrong, not the user.

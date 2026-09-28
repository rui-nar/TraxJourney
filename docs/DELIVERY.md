# Delivery policy

How an approved plan becomes code when an orchestrator (Opus) hands the work to
several implementer agents. The companion to `docs/REVIEW.md`: that document
decides which findings get fixed, this one decides who builds what, on which
model, and how the pieces come back together.

The procedure that applies it is the `deliver-plan` skill
(`.claude/skills/deliver-plan/`). Plans are written in the shape it expects by
the `write-plan` skill (`.claude/skills/write-plan/`).

Rules have stable ids (S1, X2, W3, …). Every routing decision cites one, so a
decision can be checked against the rule it claims to follow.

---

## 1. The unit

A plan's work is split into **units** (`U1`, `U2`, …). A unit is a hand-off:
the implementer gets the unit and nothing else, so everything it needs is in it.
This is the shape the execution units of `docs/LOGGING_OBSERVABILITY_PLAN.md`
already have, with the fields routing needs added.

| Field | Content |
|---|---|
| **Goal** | One sentence. |
| **Scope** | The files the unit may create or change. Anything else is out of bounds. |
| **Context** | What to read first, and **one existing example to follow** ("shaped like `api/people.py`'s list endpoint"). |
| **Do** | The steps. |
| **Acceptance** | The checks that prove it is done, named in advance: test files or test names to add or pass, commands to run. |
| **Out of scope** | The tempting extras, stated so they are not done. |
| **Latitude** | `none` — follow the unit; when something is unclear, stop and ask. `local design` — decisions inside Scope are allowed, and each one is listed in the report. |
| **Escalate if** | Conditions under which the implementer stops and reports instead of guessing. Always includes: needing a file outside Scope (X3). |
| **Depends on** | Units that must be integrated first. |
| **Route** | Model and the rule that chose it (§2). Filled in at delivery, not in the plan. |

A unit that cannot be written this way — no nameable acceptance check, no
bounded Scope — is not a unit yet. Split it further or resolve the open
question first.

## 2. Routing

A unit goes to **Sonnet** only when every S rule holds. Otherwise it goes to
**Opus**, and the route cites the first S rule that failed.

- **S1 — Checkable.** Acceptance names checks that can be run.
- **S2 — Precedented.** Context names an existing example in the codebase that
  the unit follows.
- **S3 — One side.** Scope is server-only (`api/`, `src/`, `tests/`, `scripts/`)
  or client-only (`flutter_client/`), not both.
- **S4 — No expensive boundary.** Nothing that is costly to change once shipped
  (REVIEW.md F4): no change to the request or response shape of an existing
  endpoint, no model change or Alembic migration, no change to data already
  stored, no change to an export format.
- **S5 — Not sensitive.** Scope does not touch authentication or authorization
  (`src/auth/`, `api/deps.py`), billing (`src/billing/`), end-to-end
  encryption, or background jobs and concurrency (`src/jobs/`).

Sonnet units get `Latitude: none`. Opus units get `Latitude: local design`.

When it is not clear whether a rule holds, it does not hold: the unit goes to
Opus. §6 is how that default gets loosened, with evidence.

## 3. Escalation

- **X1 — Sonnet stalls.** A Sonnet unit that fails verification twice, or whose
  implementer reports the unit is unclear, is re-run on Opus with the failure
  reports attached. It is not tried a third time on Sonnet.
- **X2 — Opus stalls.** An Opus unit that fails verification twice, or whose
  implementer reports the unit is wrong, goes back to the orchestrator: the
  unit or the plan needs changing, and a retry will not do that. Re-split it,
  or ask the user.
- **X3 — Out of scope.** An implementer that needs a file outside its Scope
  stops and reports. The orchestrator widens the Scope (checking W1 again) or
  makes a new unit.

## 4. Parallelism

Units run in **waves**: every unit in a wave runs at the same time; the next
wave starts once the previous one is verified and integrated.

- **W1 — Disjoint.** Units in the same wave have no file in common and do not
  depend on each other.
- **W2 — Isolated.** When a wave has more than one unit, each runs in its own
  git worktree.
- **W3 — Capped.** At most four units per wave. Each parallel branch has to be
  integrated, and integration cost grows faster than the number of branches.
- **W4 — One migration per wave.** At most one unit in a wave adds an Alembic
  migration; two would create two heads.

## 5. Review points

Reviews go through the `adversarial-review` skill and `docs/REVIEW.md`, at fixed
points only — not after every unit, which multiplies findings without adding
much:

1. **The plan**, before delivery starts.
2. **The integrated diff**, once, after every wave is in.
3. **A single unit, before integration,** only when it failed S4 or S5 for
   reasons of security, stored data or migrations — the REVIEW.md F1 and F2
   areas, where a defect is most expensive to find late.

Fix now findings from a review become new units and go through routing like any
other. Most are small and local, so most go to Sonnet.

## 6. The delivery record

The feature's ledger (`docs/reviews/<slug>.md`, REVIEW.md §7) gets a
`## Delivery` section with one row per unit:

```markdown
## Delivery

| Unit | Goal | Route | Rule | Attempts | Escalated | Verified first time | Findings traced |
|---|---|---|---|---|---|---|---|
| U1 | Add re-sync endpoint | Opus | S4 | 1 | — | yes | R2-1 |
| U2 | Re-sync button on trip screen | Sonnet | — | 2 | — | no | — |
| U3 | Tests for partial re-sync | Sonnet | — | 3 | X1 → Opus | no | — |
```

`Rule` is the first failed S rule for an Opus route, `—` for Sonnet.
`Findings traced` lists the review findings (REVIEW.md ledger ids) whose
location falls in the unit's Scope.

## 7. Changing this policy

Like REVIEW.md, this file changes only in a commit made for that purpose.

What drives a change is the delivery records:

- Sonnet units that escalate (X1) often and share a trait: add an S rule, or
  tighten one.
- An S rule whose failures send units to Opus that then pass first time with
  no findings: the rule may be stricter than it needs to be.
- Units that often hit X3: plans are drawing Scope too tightly, which is a
  `write-plan` problem, not a routing one.

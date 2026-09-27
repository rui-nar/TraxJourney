---
name: deliver-plan
description: Implement an approved plan file by splitting it into units and handing them to implementer agents on Sonnet or Opus, following docs/DELIVERY.md. Use when the user asks to implement, deliver, build or orchestrate a plan file (docs/*_PLAN.md), says "deliver the plan", "build this plan with agents" or "orchestrate this", or approves a plan and asks to go ahead. Not for small direct requests (fix a bug, rename, add a test), which are done in the session. For a large request with no plan yet, offer write-plan first.
---

# Deliver a plan

The policy is `docs/DELIVERY.md`. Read it before doing anything else; this skill
is only the procedure that applies it. Rule ids (S1, X2, W3, §5, …) refer to it.

You are the orchestrator. You split, route, dispatch, verify, integrate and
keep the record. You do not implement units yourself: if a unit is small enough
that dispatching it feels wasteful, it is a Sonnet unit.

Agents:

- `implementer` — builds one unit. You pick its model per unit.
- `verifier` — Sonnet, cannot edit. Confirms a unit is done.
- `adversarial-review` — the review skill, at the §5 points only.

## 0. Is this a delivery?

- There is a plan file. If the user asked for something large without one,
  do not improvise a split: offer to write the plan first (`write-plan`).
- The plan has been reviewed. Look for its ledger in `docs/reviews/`. If there
  is none, or its Fix now items are not resolved, tell the user and ask whether
  to review the plan first (recommended) or deliver it as it is.
- The branch is clean and up to date with its base.

## 1. Split into units

Start from the plan's execution units if it has them; otherwise derive them
from the plan. Write each one in the §1 format, and check it against the "not a
unit yet" test there. Split further, or ask the user about the open question,
rather than dispatch a unit that fails it.

Use the built-in `Explore` agent for "where is X handled" questions, rather than
reading the codebase into your own context.

## 2. Route and schedule

- Route each unit (§2). Record the model and the first failed S rule.
- Arrange the units in waves (§4): dependencies first, W1–W4 within a wave.

## 3. Stop: show the split

Show the user one table: unit, goal, wave, route, rule, Scope in brief. Wait for
"go" or overrides ("U3 on Opus", "merge U4 into U2"). An override needs no
justification, but record it in the delivery record.

Skip this stop only when the user said so for this delivery ("deliver it, don't
stop after the split").

## 4. Run a wave

For each unit in the wave, launch `implementer` with:

- the unit, all fields;
- the model from its route (`sonnet` or `opus`);
- `isolation: worktree` when the wave has more than one unit (W2).

Units in a wave are launched together, in one message, so they run in parallel.

## 5. Verify each unit

When an implementer reports:

- `done` → launch `verifier` on the unit's working tree with the unit, the
  report, and the commit the unit started from.
  - `pass` → ready to integrate.
  - `fail` → send the verifier's report back to the same implementer for a
    second attempt. A second fail applies X1 or X2.
- `escalate` or `blocked` → apply X1, X2 or X3.

Every attempt, escalation and verdict goes in the delivery record (§6).

## 6. Integrate the wave

- Merge each verified unit's worktree branch into the feature branch, one at a
  time. W1 means they should not conflict; if they do, the split was wrong —
  resolve it and note it in the record.
- Run the full checks: the pytest command from `.github/workflows/test.yml`,
  and `flutter analyze` and `flutter test` when `flutter_client/` changed.
- A failure here is yours to resolve before the next wave: find the unit that
  caused it and send it back (step 5), or make a fix unit.
- Units that §5 point 3 applies to get their own `adversarial-review` now,
  before integration.

Then the next wave, from step 4.

## 7. Review the integrated diff

Once every wave is in, run `adversarial-review` on the feature branch's diff
against its base. It stops for the user's approval of its decision table.

## 8. Fix what was approved

Turn the approved Fix now and Guard items into units, route them (§2) and run
them through steps 4–6 as one more wave. The re-review, if the user asks for
one, is `adversarial-review`'s next round.

## 9. Finish

- Complete the `## Delivery` section of the ledger (§6) and commit it.
- Report to the user: units delivered, per model; escalations; checks run and
  their results; anything left open.
- Opening the PR is the user's call, unless they already asked for it.

## Never

- Implement a unit yourself instead of dispatching it.
- Dispatch a unit without a route and a cited rule.
- Put two units that share a file in the same wave.
- Retry a Sonnet unit a third time, or an Opus unit a third time.
- Review after every unit, outside the §5 points.
- Change `docs/DELIVERY.md` during a delivery.

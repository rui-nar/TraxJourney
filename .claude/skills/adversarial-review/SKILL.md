---
name: adversarial-review
description: Run an adversarial (Fable) review of a plan or diff and triage its findings against docs/REVIEW.md before anything is fixed. Use whenever the user asks for an adversarial review, a Fable review, a hostile or critical review, or a re-review after fixes; whenever review findings are pasted or arrive from a PR bot and need acting on; and when asked to calibrate the review policy.
---

# Adversarial review

The policy is `docs/REVIEW.md`. Read it before doing anything else; this skill
is only the procedure that applies it. Rule ids (E1, F2, D5, §3, …) refer to it.

Two subagents do the work, each in its own context:

- `adversarial-reviewer` — Fable, read-only. Finds problems.
- `review-triager` — Opus, read-only. Decides which problems matter.

You run the procedure between them, enforce the rules they might bend, keep the
ledger, and put the result in front of the user.

## Modes

- **Review** — a plan or a diff, first round. Steps 1–8.
- **Re-review** — after fixes. Steps 1–8, next round, fixes only (§6).
- **Triage only** — findings came from somewhere else (pasted, a PR bot, a
  person). Put each one into the §3 schema yourself; a field you cannot fill
  from the finding is missing, so ask for it or drop the finding. Then steps
  1, 5–8.
- **Calibrate** — see the end.

## Steps

### 1. Subject and ledger

- The subject is a plan file, or a diff (default: current branch against
  `origin/main`).
- The ledger is `docs/reviews/<slug>.md` (§7). Create it from the §7 template
  if it does not exist.
- The round is one more than the last round in the ledger. Past round 3, stop
  and ask the user (§6).

### 2. Envelope

Use the plan's `## Review envelope` section plus the §2 defaults. For a diff,
use the envelope of the plan it implements, if there is one.

If the subject goes beyond what the envelope covers — a new trust boundary, a
new external service, new concurrency, a new kind of client — and nothing
states the assumption, **ask the user** before reviewing. Do not invent an
envelope: a wrong one makes every later decision confidently wrong.

### 3. Inputs for the reviewer

The reviewer cannot run commands. For a diff, write it to a file in your
scratchpad and pass the path. From round 2, the diff is only the commits made
since the round recorded in the ledger.

### 4. Review

Launch `adversarial-reviewer` with: the subject (plan path or diff file), the
envelope, the ledger path and the round number. Do not pass your own reasoning
for the design: the reviewer judges the artifact, not the argument for it.

The reviewer runs on Fable by default. If the user named a model for the review
("an Opus adversarial review"), launch the same agent with that model as an
override; its tools and instructions stay the same.

### 5. Validate

- Every finding has every §3 field, and its trigger names an actor, an action
  and an outcome. Send malformed findings back to the reviewer once; drop what
  is still malformed and note how many.
- Mark findings that match a ledger entry with no new evidence as Duplicate
  (D2).
- Envelope questions go straight to the user in step 8; they are not triaged.

### 6. Triage

Launch `review-triager` with the validated findings (the structured list, not
the reviewer's prose), the envelope and the ledger path. It returns a decision,
a rule id and a one-line reason for each finding.

### 7. Enforce

Do not take the triage on trust. Check each decision yourself:

- **Floors.** A finding in F1–F4 with a `concrete` or `plausible` trigger is
  Fix now, whatever the triager said (D3, D5). An F1–F3 finding the triager
  rejected becomes Guard and is flagged to the user (D4).
- **Citations.** Each decision cites a D rule whose conditions the finding's
  scores actually meet, in §5 order. If not, send it back to the triager once;
  then decide it yourself by the table.

Write every finding to the ledger, rejected ones included, with the commit the
round reviewed.

### 8. Present and stop

Show the user one table, Fix now first, then Guard, Defer, Reject, Duplicate:
id, title, decision, rule, one-line reason. List envelope questions and floor
flags separately above it.

Ask the user to approve or override. An override needs a one-line reason, which
goes in the ledger's `Override` line.

**Stop there.** Nothing is fixed in the same turn as the review.

### After approval

- Implement Fix now items and add the Guard items. Nothing else: Defer and
  Reject items are not touched, and nothing "while I'm at it".
- Reference ledger ids in the commit message ("fixes R1-3, R1-5").
- Update each entry's `Outcome` (`fixed`, `guard added`).
- A re-review happens when the user asks for one: back to step 1, next round.

## Calibrate

When asked to calibrate, read every ledger in `docs/reviews/` and report:

- Rejected, Deferred or Guarded entries whose `Outcome` is `caused #<issue>`,
  and the rule that decided them.
- Rules whose Fix now entries all ended `never happened`.
- Rules the user overrides often.

Propose the `docs/REVIEW.md` edits this suggests. Changing the policy is a
separate commit, made only once the user agrees (§8).

## Never

- Fix a finding before the user has seen and approved the table.
- Edit `docs/REVIEW.md` during a review.
- Drop a floor finding without the user's decision.
- Leave a finding out of the ledger.

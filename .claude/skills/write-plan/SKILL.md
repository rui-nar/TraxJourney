---
name: write-plan
description: Write an implementation plan for a feature or change in the shape that adversarial-review and deliver-plan consume, saved as docs/<NAME>_PLAN.md. Use when the user asks to plan a feature, write or draft a plan, plan an issue ("write a plan for #345"), or asks for something large enough to need several units of work before any code is written.
---

# Write a plan

A plan is read three times: by the user, who decides; by the adversarial
reviewer, who needs the envelope; and by `deliver-plan`, which turns its units
into hand-offs. Write it for all three.

Read `docs/DELIVERY.md` §1 (the unit) and `docs/REVIEW.md` §2 (the envelope)
first. The existing `docs/LOGGING_OBSERVABILITY_PLAN.md` is a good example of
the level of detail units need.

## Before writing

- Read the code the change touches, and the issue if there is one. Use the
  built-in `Explore` agent for broad "where is X" questions.
- Questions only the user can answer (product choices, trade-offs, what "done"
  means) get asked now, not written into the plan as assumptions. Questions
  that can wait go in Open decisions.

## The file

`docs/<NAME>_PLAN.md`, with these sections in this order. Leave out a section
only where the note says it is optional.

1. **Title** — `# <Feature> — Plan for #<issue>` when there is an issue.
2. **Problem** — what is wrong or missing, for whom, and how we know.
3. **Current state** *(optional)* — what exists today that the change builds
   on or replaces, with file references. Facts, not proposals.
4. **Decisions** — the design choices the plan makes, each with the reason and
   the alternative it rules out.
5. **Review envelope** — exactly this heading, `## Review envelope`. State only
   what differs from or adds to REVIEW.md §2's defaults: new trust boundaries,
   new external services, concurrency, scale. "REVIEW.md defaults apply" is a
   valid envelope when nothing changes.
6. **Boundaries crossed** — anything the change does to the API contract, the
   schema, stored data or an export format (REVIEW.md F4), and how old clients
   and existing data are handled. "None" is a valid answer; say it.
7. **Conventions** *(optional)* — rules binding on every unit, when the
   change has them.
8. **Open decisions** — what is still undecided, what it affects, and by when
   it must be decided.
9. **Execution units** — the work split into units in the DELIVERY.md §1
   format, grouped in waves. Fill every field except Route, which is decided
   at delivery. Each unit's Context names an example to follow wherever one
   exists: that is what lets a unit go to Sonnet.
10. **Definition of done** — checkable statements about the finished feature.

## After writing

Show the user a short summary: the decisions, the boundaries crossed, the open
decisions and the number of units per wave. Suggest the next step, an
adversarial review of the plan; do not start it unasked.

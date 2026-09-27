# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

# Project Specific Instructions
## graphify
This project has a graphify knowledge graph at graphify-out/.

Rules:
- Before answering architecture or codebase questions, read graphify-out/GRAPH_REPORT.md for god nodes and community structure
- If graphify-out/wiki/index.md exists, navigate it instead of reading raw files
- After modifying code files in this session, run `graphify update .` to keep the graph current (AST-only, no API cost)

## testing
ensure tests are available for every feature to ensure non-regressions

## versioning
version are standard x.y.z where y is for features and z is for patches. unless instructed otherwise, bumping a release means to increase the feature version. there is a github repository with github actions to automatically generate docker images so every verison bump shall also be tagged as latest

release notes are generated from the commits — when a commit changes something a user would notice, add a `Release-Note:` trailer written in the user's vocabulary, not the commit subject's. Always write it in English, whatever language the commit body or the conversation was in: the release page is published in English. see docs/RELEASING.md

## reviews
adversarial reviews (including "fable review" requests), re-reviews and any review findings to act on go through the `adversarial-review` skill, which applies docs/REVIEW.md. never fix review findings directly: triage them first and get the decision table approved.

## delivery
multi-step features go plan (`write-plan`) → adversarial review → `deliver-plan`, which applies docs/DELIVERY.md: units routed to Sonnet or Opus implementer agents, verified, integrated and reviewed once. implementing a plan file always goes through `deliver-plan`. small direct requests don't.

## current stack
current stack is compoesed of a sever in python FastAPI serving clients written in flutter for  web, android and iOS. any architecutre decision shall keep this structure in mind.

## approach
Go for the long game, don't take the easy solution, the easy fix or the short-term win. If in doubt ask what the user prefers.
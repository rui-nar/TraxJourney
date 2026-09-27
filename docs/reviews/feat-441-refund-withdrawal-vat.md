# Review ledger — #441 refunds, EU withdrawal and VAT

Subject: branch `feat/441-refund-withdrawal-vat` (stacked on `fix/429-cancel-sub-on-delete`)
Envelope: REVIEW.md defaults (E1–E6)

Rounds 1–4 (2026-09-26) came before `docs/REVIEW.md` and were not triaged. Their
findings were all fixed and are summarised in the PR description.

## Round 5 — 2026-09-27, reviewed at f386de10

Reviewer: Opus, adversarial. Triage: `review-triager` (Opus). Findings restated in the §3 schema from the reviewer's report.

### R5-1 — A withdrawal whose cancel times out or fails near the deadline is refused on retry
- Trigger: user taps Withdraw at 23:59:30 on day 14 → Stripe cancels but the answer times out, or the cancel fails → retry at 00:01 gets 409; cancelled at Stripe, nothing refunded or recorded as owed
- Scores: trigger=plausible, impact=silent-wrong, detect=user-visible, later=cheap, fix=M/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open

### R5-2 — A refund that fails after the account is deleted is dropped with no log
- Trigger: refunded user deletes the account → Stripe later sends refund.failed → webhook matches no row and logs nothing; the owner never learns a refund is owed
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open

### R5-3 — A refund failing after the owner settled re-owes the part already settled
- Trigger: row owed 200 (balance share) plus re_1 100 to the card → owner pays 200 by hand and settles → re_1 fails → the admin list shows 300 owed
- Scores: trigger=plausible, impact=silent-wrong (corrected from wrong-visible), detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open

### R5-7 — A refund that ends `canceled` is not treated as failed
- Trigger: Stripe moves one of our refunds to canceled → webhook ignores it → the row stays done with no money returned
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified (corrected from inferred)
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open

### R5-9a — A pending row with an unknown amount is listed to the admin as 0 owed
- Trigger: deletion where the customer vanished at Stripe before the amount was frozen → row with to_refund -1 → admin list shows 0 owed; the owner may settle a row that is owed money
- Scores: trigger=plausible, impact=silent-wrong (corrected from wrong-visible), detect=user-visible, later=cheap, fix=S/local, confidence=verified (corrected from inferred)
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open

### R5-5 — Admin settle fence can pass on claim_token == "" (ABA)
- Trigger: a claimant runs a whole claim → credit note → release cycle between the admin's resolve and its read → settle passes on a row Stripe just refunded
- Scores: trigger=theoretical, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=inferred (verified by the triager)
- Decision: Guard (D4) — flagged to the user
- Revisit when: —
- Guard: the settle fence also compares a `version` column bumped on every write, and a test covers it
- Override: —
- Outcome: open

### R5-4 — The 10-minute cancel bound includes finish_pending time
- Trigger: user with an earlier contract's pending refund withdraws while Stripe is slow → the cancel lands more than 600 s after requested_at → recorded as owed instead of refunded
- Scores: trigger=plausible, impact=degraded-ux, detect=logged, later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: an owed row is recorded for the cancel bound while the same request was finishing an earlier contract's refund, or finish_pending gains more Stripe calls per refund
- Guard: —
- Override: —
- Outcome: open

### R5-6 — A manual refund before settle can be followed by an automatic resend
- Trigger: owner refunds a stuck row by hand, user retries withdraw before settle → the frozen amount is resent → Stripe refuses (refundable cap) → a misleading owed row
- Scores: trigger=plausible, impact=wrong-visible, detect=logged, later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: a frozen card refund can be 50% or less of its charge (longer window, shorter period, discounts or trials), or the runbook allows partial manual refunds before settle
- Guard: —
- Override: —
- Outcome: open

### R5-9b — Subscription missing but customer present blocks withdraw and deletion with 502
- Trigger: subscription deleted at Stripe while the customer exists → cancel counts as success, refund_basis answers 502 on every attempt → deletion blocked until the admin settles
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: a log or user report shows withdraw or deletion returning 502 from refund_basis for a subscription missing at Stripe, or a deletion blocked on it for more than a day
- Guard: —
- Override: —
- Outcome: open

### R5-8 — CustomerGone is detected by a substring of the error message
- Trigger: a wrong Stripe key or account returns resource_missing on a customer= call → recorded as owed and deletion proceeds
- Scores: trigger=plausible, impact=wrong-visible, detect=logged, later=cheap, fix=S/local, confidence=inferred
- Decision: Reject (D1) — needs a wrong server key or account, which is trusted config (E3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open

### R5-10 — Test gap: the settle fence's lease clause
- Trigger: a developer removes the lease clause → the suite still passes
- Scores: trigger=theoretical, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Reject (D11)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open

### R5-11 — Test gap: record_refund_failure's locked lookup
- Trigger: a developer loosens the locked lookup → the suite still passes
- Scores: trigger=theoretical, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Reject (D11)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open

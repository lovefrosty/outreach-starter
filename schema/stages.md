# Stage Machine

12 stages. A lead moves forward through agent work and human decisions. Any lead can be suppressed or closed at any time.

## Stages

| Stage | Meaning | Moved Forward By |
|-------|---------|-----------------|
| `new` | Lead entered, not yet researched | Agent starts research |
| `researching` | Research in progress | Agent completes research + variables |
| `researched` | Research done, variables built, ready for draft | Operator says "draft it" |
| `pending_approval` | Draft in #approvals, waiting for human | Human approves/rejects/revises |
| `approved` | Human approved, ready to send | Agent sends via Gmail |
| `sent` | Email delivered | Timer or reply detection |
| `awaiting_reply` | Follow-up window open, no reply yet | Reply or follow-up trigger |
| `replied` | Prospect replied, triage complete | Human acts on recommendation |
| `qualified` | Human confirmed commercially promising | Human hands off |
| `handed_off` | Transferred to sales with brief | Terminal |
| `closed_lost` | Dead end — tried and failed, or timed out | Recoverable by human |
| `suppressed` | Do not contact — bounce, complaint, unsubscribe, manual | Recoverable only by explicit unsuppress |

## Transitions

| From | To | Trigger | Guard |
|------|----|---------|-------|
| `new` | `researching` | Research started | Lead has company_name + domain or LinkedIn |
| `researching` | `researched` | Research + variables complete | ≥1 non-null research signal; all 3 core variables populated (key_signal, pain_hypothesis, recommended_angle); hypothesis_confidence ≥ MEDIUM |
| `researched` | `pending_approval` | Draft + QA pass | QA has zero blockers |
| `pending_approval` | `approved` | Human approves in #approvals | Draft exists in thread |
| `pending_approval` | `researched` | Human says "revise" | Loops back for new draft |
| `pending_approval` | `suppressed` | Human rejects | Reason logged |
| `approved` | `sent` | Gmail confirms delivery | 2xx response within 5 min |
| `sent` | `awaiting_reply` | 48h pass with no reply | Auto-transition |
| `awaiting_reply` | `awaiting_reply` | Follow-up sent | Follow-up count < max (default 3) |
| `sent` or `awaiting_reply` | `replied` | Inbound reply detected and triaged | Reply matched to lead |
| `replied` | `qualified` | Human marks as promising | — |
| `replied` | `closed_lost` | Human marks as dead end | — |
| `qualified` | `handed_off` | Human runs handoff | Notes populated |
| 3 follow-ups, no reply | `closed_lost` | Day 21 auto-transition | — |
| Any | `suppressed` | Bounce, complaint, unsubscribe, or human request | Immediate |
| Any | `closed_lost` | Human decision or 90-day inactivity | — |

## Suppression Rules

| Trigger | Action | Reversible? |
|---------|--------|-------------|
| Hard bounce | Immediate suppress | Yes — by explicit human unsuppress |
| Spam complaint | Immediate suppress + review ICP segment | Yes — by explicit human unsuppress |
| Unsubscribe reply | Immediate suppress + confirmation reply | **No — permanent** |
| Human request | Immediate suppress | Yes — by explicit human unsuppress |
| Competitor/partner discovered | Immediate suppress | Yes — by explicit human unsuppress |

## Failure Handling

| Failure | Response |
|---------|----------|
| Research returns empty | Hold in `researching`. Ask human: retry, different approach, or skip. |
| Core variables incomplete or hypothesis_confidence < MEDIUM | Hold in `researching`. Return to research with gap list. |
| QA fails draft | Hold in `researched`. Return to drafting with QA findings. |
| Gmail send fails | Retry once at 60s. Second failure → revert to `pending_approval`, alert #agent-main. |
| Reply match ambiguous | Hold for human in #agent-main. Don't auto-classify. |
| Sheet write fails | Retry once. If persistent → log, don't advance stage. |

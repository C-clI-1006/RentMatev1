# Manager Review

This module implements the Stage 1 manager-review boundary for tenant-facing
drafts. It lists only `pending_review` drafts for properties authoritatively
bound to the authenticated manager and records one immutable decision per
draft.

## API

- `GET /api/reviews` - property-scoped pending queue.
- `GET /api/reviews/{draft_id}` - original message, grounded facts, draft and
  review reason for one authorised pending item.
- `POST /api/reviews/{draft_id}/decision` - approve or reject an exact
  `expected_version`.

An approval may carry manager-edited `final_content`; otherwise it stores the
exact draft content. A rejection requires a reason and always stores
`final_content = NULL`. Manager identity and property scope come only from the
server-side auth context and `manager_property` binding.

## Boundary

This module does not send tenant messages. It writes the existing `review`
record, updates the draft to `approved` or `rejected`, and appends an audit
event in one transaction. The notification dispatcher must later read the
exact approved `review.final_content` and matching `draft_version`. Rejected,
stale, superseded, already-decided and cross-property drafts cannot be sent
through this workflow.

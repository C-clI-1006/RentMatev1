"""Communication module (owner: Chaney).

Responsibilities (guide Ch.13):
- Message intake: persist inbound tenant message (messageId, tenantId,
  propertyId from auth context, content, receivedAt).
- Classification with validation; unclear classification -> human handoff.
- UC1 routing -> MaintenanceIntake (contracts.maintenance).
- UC2 routing -> LeaseRentQuery (contracts.lease_rent).
- Drafting grounded in returned facts (contracts.drafting).
- Delivery-result recording: show failed/pending_retry truthfully.
- Optional sentiment: tone hint only; never weakens review.

Not yet implemented — see ../../README.md for status.
"""

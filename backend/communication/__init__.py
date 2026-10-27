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

Implemented: `service.CommunicationService` (intake -> classify ->
route -> fact-grounded, conditionally reviewed draft). The classifier is a
labelled test double (llm.classifier.KeywordClassifier) until the
school Qwen proxy is confirmed working.
"""

from .service import CommunicationOutcome, CommunicationService
from .maintenance import MaintenanceCommunicationReceiver

__all__ = [
    "CommunicationOutcome",
    "CommunicationService",
    "MaintenanceCommunicationReceiver",
]

# Demonstration samples

## test-invoice.pdf

This one-page, text-based PDF contains fictional invoice data for software testing
and demonstration only. It contains no real tenant information or credentials.
The expected document category is `invoice`; classification must still be performed
by the configured classifier and verified from the actual response.

## maintenance-inspection-record.pdf

This one-page PDF is fictional inspection evidence for the kitchen maintenance
scenario. The expected document category is `inspection_record`. After upload,
record the returned opaque `document_id` and use that ID when adding an evidence
reference to the Maintenance event.

## maintenance-repair-invoice.pdf

This one-page PDF is fictional repair-invoice evidence for the same scenario. The
expected document category is `invoice`. It is safe to upload during the manager
workflow demonstration and contains no real tenant, supplier or payment data.

## Upload through Swagger

1. Start RentMate from the extracted submission package and open `/docs` at the
   address printed by the launcher.
2. Authorize using a configured manager session with access to the target property.
3. Open `POST /api/evidence/documents` and select **Try it out**.
4. Enter the authorized `property_id` (for the supplied demo seed: `demo-property`).
5. Select `demo/samples/test-invoice.pdf` from the extracted package and execute.
6. Verify upload and classification separately. HTTP 200 alone does not prove
   successful AI classification. Record the new `document_id` from this response.
7. Use that ID for PDF extraction, manager review and any Maintenance evidence
   references. Do not reuse document IDs from an earlier deployment.

For the full-chain demo, use `maintenance-inspection-record.pdf` as the first
upload, then attach its new `document_id` to the Maintenance status event. Use
`maintenance-repair-invoice.pdf` as an optional second upload to demonstrate a
different evidence category.

The property name printed in the PDF is sample text, not an authorization claim.
Live classification requires privately supplied model configuration and network
access. If inference is unavailable, verify the recorded manual-review outcome;
never report it as a successful live-model classification.

Include this directory in the final source archive. Do not package local model
configuration, private keys, operational databases or runtime upload storage.

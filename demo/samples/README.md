# Demonstration samples

## test-invoice.pdf

This one-page, text-based PDF contains fictional invoice data for software testing
and demonstration only. It contains no real tenant information or credentials.
The expected document category is `invoice`; classification must still be performed
by the configured classifier and verified from the actual response.

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

The property name printed in the PDF is sample text, not an authorization claim.
Live classification requires privately supplied model configuration and network
access. If inference is unavailable, verify the recorded manual-review outcome;
never report it as a successful live-model classification.

Include this directory in the final source archive. Do not package local model
configuration, private keys, operational databases or runtime upload storage.

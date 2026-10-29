# Stage 2 final release checklist

## Source and integration

- [ ] PR11 is merged and the new `origin/main` hash is recorded.
- [ ] PR8 is no longer Draft, the old requested changes are resolved, and its
      final reviewed commit is recorded.
- [ ] The integration candidate is rebuilt/rebased on the real final baseline.
- [ ] All six route families remain mounted: Maintenance, Evidence, Review,
      Lease/Rent, Reporting and Notification.
- [ ] The complete backend suite passes on final `main`.
- [ ] `git diff --check` and clean-working-tree checks pass.

## Functional acceptance

- [ ] UC4: paid, not-due, overdue, missing/conflicting data, approve/edit/reject,
      delivered and failed/retry paths are demonstrated.
- [ ] UC5: inside/outside window, inactive/invalid data, duplicate scan and
      review/delivery paths are demonstrated.
- [ ] Approved delivery uses only the exact reviewed version.
- [ ] Rejected drafts create no notification.
- [ ] Cross-property tenant and manager attempts are denied.
- [ ] Evidence receipt, idempotent replay and conflicting replay are verified.
- [ ] Monthly report scheduled and manager-triggered paths are verified. The
      Windows task can be registered with `Register-Monthly-Reports.ps1`; attach
      `.runtime/monthly-report-runs.jsonl` from the acceptance machine before
      marking this item complete.

## Jira and contribution evidence

- [ ] `SCRUM-10` contains the final branch/PR, commit, test command/count and
      acceptance evidence; it is not marked Done before merge.
- [ ] UC5 is represented by a dedicated issue or explicitly documented scope in
      an agreed existing issue.
- [ ] Review feedback and cross-owner integration work are visible in Jira/PRs.
- [ ] Contribution statement names every member and every rubric row totals
      exactly 100%.

## Exact archive rehearsal

- [ ] Download the actual candidate archive from Canvas/source packaging flow.
- [ ] Extract it into a new directory on a clean machine/account.
- [ ] Confirm `.env`, keys, local databases, uploads, caches and virtual
      environments are absent.
- [ ] Follow only the submitted README to install, verify and start the demo.
- [ ] Record the archive checksum, final commit, test result and rehearsal date.
- [ ] Submit before 2026-11-01 23:59 Australia/Sydney; do not plan for the last
      minute.

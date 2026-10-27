# RentMate Stage 2 final demonstration runbook

This runbook is for a clean, repeatable Week 13 demonstration. It does not
replace final-main verification or the Canvas archive rehearsal.

## Before the demonstration

1. Use the final merged `main` commit, not a contributor branch.
2. Confirm `git status --short` is empty and record `git rev-parse HEAD`.
3. Run `Start-RentMate.ps1 -Verify`; record the exact pass count and warning.
4. Start with `Start-RentMate.cmd`, then open `http://127.0.0.1:8020/demo`.
5. Keep `/docs` open as a fallback if the demo console is unavailable.
6. Do not display `.env`, API keys, auth JSON or local database contents.

## Five-minute integrated path

1. **Scheduled facts:** click **Run scheduled checks**. Explain that active
   lease/rent records are evaluated deterministically and repeated runs reuse
   stable source keys.
2. **Human control:** click **Load pending reviews**. Show one rent reminder and
   one lease-deadline alert, their source reason and exact version. Approve one
   and reject another to show that rejection creates no delivery.
3. **Truthful delivery:** click **Dispatch notifications**. Explain that
   `accepted` means queued, only the single dispatcher retries, and only a
   durable inbox write becomes `delivered`.
4. **Tenant result:** click **Load delivered messages**. Show that only the
   approved exact content appears in the property-scoped tenant inbox.
5. **Idempotency:** run the scheduled check and dispatcher again. Show zero new
   drafts/deliveries for the same logical inputs.

## Other module evidence

- Open **Maintenance demo** for registration, priority and controlled status
  transitions.
- Use `/docs` to show Evidence upload/classification/timeline, monthly report,
  manager review and receipt endpoints mounted in one application.
- Explain that Evidence/Maintenance ingestion is an in-process trusted adapter;
  there is intentionally no caller-controlled HTTP event-ingestion identity.

## Honest boundaries

- The demo console is a no-build Stage 2 prototype UI, not a production React
  client.
- The SQLite tenant inbox is the executable notification transport boundary;
  no email/SMS provider is claimed.
- Real Qwen verification depends on the course proxy, VPN and valid secret.
  Deterministic tests never make live model calls.
- A local candidate result is not final evidence until the same suite passes on
  the final merged `main` and on the exact Canvas archive.

## Fallback

If the browser path fails, use `/docs` to execute the same four API stages. If
the course proxy is unavailable, demonstrate deterministic paths and show the
recorded live-verification artifact without claiming a new live call. Never
replace failed evidence with screenshots from a different commit.

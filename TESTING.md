# Final audit and testing guide

Audit date: 25 October 2026; source and automated functionality rechecked on 26 October 2026. Architecture retained: Outlook add-in, one Python backend, SQLite and one background worker. No live UPES mailbox was accessed or used to send mail during these audits.

## Assessment

The automated checks support proceeding to a separately authorized **test-mailbox pilot**. They do not certify production activation. Microsoft tenant permissions, actual Outlook clients, recipient delivery and real pilot outcomes still require institutional testing below.

The audit found and fixed defects beyond the original passing suite:

- Automated replies could be interpreted as resolution confirmations. They now require review instead of closing a ticket.
- Internet Message-ID collisions could silently discard a different message. Immutable provider IDs now deduplicate receipts; conflicting headers stop processing visibly rather than advancing the ingestion page.
- Concurrent migrations could race, and migration scripts could partially commit. Migrations now serialize, recheck their version under lock and roll back failed schema changes.
- Core failure recovery could select an unrelated failed notification. It now selects only the relevant forwarding/follow-up jobs.
- A failed reminder draft could retain an obsolete due date. Due-date edits cancel pending and failed reminder jobs; uncertain sends still require reconciliation.
- Reminder policy/contact changes during dispatch could race with delivery. Such edits are rejected until the running delivery completes.
- HTTP redirects could relay Microsoft credentials or bearer headers. OAuth and Graph transport now refuse redirects.
- Liveness alone could appear healthy without migrations. `/readyz` checks the required schema; Compose uses it for readiness.
- Database creation permissions and Windows-compatible frontend build paths were hardened.

Regression coverage includes concurrent intake, migration rollback, migration concurrency, spoofed header collisions and the delivery cases above. The new migration is `004_audit_hardening.sql`; back up existing databases before upgrading.

## 1. Start with the fully offline checks

Run from the repository root on macOS/Linux, or in WSL2 on Windows. The backend uses a POSIX worker lock: use Linux containers/WSL2 for Windows backend development. Outlook on the web and new Outlook for Windows remain the intended staff clients.

If dependencies are not installed, create a Python virtual environment and install `requirements.txt`, then run `npm --prefix addin ci --ignore-scripts`. Node 22 and the Docker Python 3.12 environment are the deployment reference. Dependency installation needs internet access; the tests mock Microsoft operations.

```sh
.venv/bin/python -m unittest discover -s tests -v
npm --prefix addin test
npm --prefix addin run build
.venv/bin/python scripts/offline_walkthrough.py
```

Expected audit results: **85 backend tests, 14 add-in tests, a successful frontend build and 22 walkthrough checks**. Intentional failure tests print `delivery_failed`; the final unittest result must still be `OK`.

The walkthrough creates and removes its own temporary database. It ignores `.env`, forces dry-run mode, uses synthetic addresses and raises an error if the Graph adapter is constructed. It exercises intake, duplicate protection, extraction, unknown review, acknowledgements, explicit approvals, reminders, escalation, assigned-authority completion, follow-ups, reopening, fresh approval, closure, labels and metrics. It does not bypass authentication in the running application or create a usable staff login.

Expected walkthrough totals: 2 tickets, 2 explicitly approved simulated forwards, 2 acknowledgements, 2 reminders, 1 escalation, 1 closed ticket and 1 review ticket. Routing accuracy is 100% over **one explicitly labelled synthetic sample**; closure rate is 50%. These figures validate arithmetic, not real institutional performance.

For focused regression runs:

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_audit_regressions.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_identity.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_authority_api.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_delivery.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_graph.py' -v
```

These cover fault recovery, actual RSA-signed token validation with mocked key discovery, role boundaries and mocked Microsoft transport. They do not establish that UPES has consented to the real applications.

## 2. Repeat in the deployment image without networking

These commands use a Unix shell from the repository root. They mount test sources read-only and provide no `.env` or Microsoft credentials.

```sh
docker build -t upes-alumni:audited .
docker run --rm --network none \
  --mount "type=bind,source=$PWD/tests,target=/app/tests,readonly" \
  --mount "type=bind,source=$PWD/scripts,target=/app/scripts,readonly" \
  --mount "type=bind,source=$PWD/addin/manifest.template.xml,target=/app/addin/manifest.template.xml,readonly" \
  upes-alumni:audited python -m unittest discover -s tests -v
docker run --rm --network none \
  --mount "type=bind,source=$PWD/scripts,target=/app/scripts,readonly" \
  upes-alumni:audited python scripts/offline_walkthrough.py
```

The add-in test suite uses mocked Office/MSAL APIs. The built pane must still be tested inside both real Outlook clients. Opening its HTML directly in a browser does not emulate Outlook authentication or selected-message context.

## 3. Have UPES IT prepare an isolated mailbox pilot

Do not change live authorization flags merely to try the interface. Obtain explicit permission for a **dedicated test mailbox**, with a separate database, deployment and verified test recipients. Set `MAILBOX` to that supplied test address; the example configuration otherwise names the live alumni address.

IT must supply/verify:

- Exchange Online mailbox type (`standalone` versus `shared`) and actual access mode. Staff primarily signing in does not independently verify `RecipientTypeDetails`.
- The institutional tenant ID; separate API, public add-in SPA and background-worker app registrations; API audience, delegated scope and authorized SPA; appropriate redirect/NAA configuration from README.
- Admin Entra **user object IDs** and assigned-authority user object IDs. A shared account attributes actions to that identity; named institutional identities are needed for individual accountability.
- Worker client ID and securely provisioned secret, consented Graph permissions, and mailbox-scoped Exchange application authorization. Verify that the worker cannot access another mailbox.
- The intended ingestion start timestamp and test backlog; trusted HTTPS origin and certificate; generated manifest with actual IDs/origin; sideload/central deployment permission for Outlook web and new Outlook for Windows.
- Verified test authority recipients and optional escalation recipient. Actual Rita/Hemant email addresses and departments remain unset until institutionally supplied. No guessed recipient is acceptable.

Keep `DRY_RUN=true`, `LIVE_MAILBOX_AUTHORIZED=false` and `MAILBOX_TYPE_VERIFIED=false` during offline preparation. Real ingestion/sending is a separate explicitly authorized phase using the **test mailbox**, not the live UPES mailbox. Follow README's exact Microsoft configuration and activation steps; never weaken token checks to make a demo work.

## 4. Test the actual Outlook workflow

Use synthetic student data and record ticket IDs, timestamps, recipients, relevant audit events and screenshots for each case. Repeat the client-facing cases in both Outlook on the web and new Outlook for Windows; record client versions. Use an admin, Rita-test authority, Hemant-test authority and an unauthorized institutional user.

1. Send six separate requests with explicit subjects: `Transcript request`, `Provisional degree request`, `Degree verification request`, `Medium of instruction request`, `Degree collection request`, `Migration request`. Check the first five map to Rita and Migration maps to Hemant. Each must await approval. A receipt acknowledgement is allowed; **no concern forward may exist before approval**.
2. Send a grade-card request, a vague request and a mixed transcript/migration request. They must enter `NEEDS_REVIEW`, without a guessed route or forward. Check missing authority email also blocks forwarding.
3. Include explicit `Name:` and enrolment/alumni-number fields in one request. Compare extracted fields to the source. Omit them in another: absent values must remain NULL. Include HTML/script-like content: it must render safely without execution.
4. Open an original message, switch rapidly between messages and pin/unpin the pane. Check selected ticket, status, sender and history always correspond to the current message. Test no selected message and the Pending Approvals view. Verify original content/attachments through the real native forward.
5. Reject one request with a reason; change another route and record the decision. Neither action sends it. Explicitly approve a correctly configured request; verify exactly one accepted forward and its recipient. Double-click Approve and use two open panes: stale decisions must fail visibly without a second forward.
6. Let the assigned authority open the forwarded email or Assigned to me and mark it Addressed with evidence. Check Alumni Admin sees the updated status after refresh/polling. The other authority and an unrelated user must be denied server-side, including direct API requests, not just hidden buttons.
7. Confirm the follow-up reaches the original test student with the reference. Reply exactly `RESOLVED`: close the concern. On a second ticket reply `NOT RESOLVED`: reopen and require **fresh Admin approval** before another forward. Try an ambiguous reply and an automatic out-of-office reply: both require review. Test unrelated senders/threads cannot close another student's ticket.
8. Exercise manual Close/Reopen and inspect actor/reason history. Administrative closure must not masquerade as student-confirmed fulfilment in turnaround metrics.
9. Reopen Outlook and restart the backend/worker. Tickets, audit records, approvals and receipt IDs must survive. Replaying the same provider message must not create another ticket/ACK. Sending a second email with the same text is a new provider message and is **not** a valid duplicate-replay test; use the automated concurrency/replay tests for deterministic proof.

## 5. Verify reminders and failure handling

Defaults are elapsed clock hours, not business hours: due at forwarding +24h, reminders at due and due +24h, escalation at due +24h when configured. Maximum two reminder reservations and one escalation per forwarding cycle. Failed/cancelled reservations consume the cycle limit; changing a date is not a reset.

- Offline walkthrough clock injection tests the full schedule immediately. For a quick authorized mailbox test, set an unresolved forwarded ticket's due date into the past through the admin action; wait for a worker pass and inspect one reminder. Repeated passes must not duplicate it.
- Run a real 24/48-hour soak on the test mailbox before sign-off. Check timestamps, recipients, caps and escalation. Do not change the machine clock or weaken production scheduling bounds to accelerate the soak.
- With escalation contact absent, check the blocked condition is visible and no escalation is sent. Configure only a verified test recipient. Mark another ticket Addressed before due: no obsolete reminder may be sent. Edit a due date after a definitively failed reminder: its old draft must not be retried.
- Definitive forwarding failure retains the concern and exposes the error. Prepare Retry must return forwarding to fresh approval. A failed acknowledgement/reminder must remain visible without corrupting the concern lifecycle.
- A timeout/crash around send is **UNCERTAIN**, not evidence that nothing was sent. Inspect Microsoft Sent Items and the saved draft, then use evidence-based reconciliation. Never repeatedly click retry or edit database state to force sending. Deterministic crash/timeout cases are tested offline; any destructive fault injection belongs only in the isolated pilot.
- Restart after a claimed delivery and check quarantine rather than automatic resend. Start a second worker: its lock must prevent concurrent worker processing. Check heartbeat, ingestion errors, failed/uncertain deliveries and NDRs.
- Test a mailbox access denial and a transient transport failure in the isolated environment. Bounded retries must stop, retain work and expose actionable failures. A malformed message/header collision must retain the ingestion page for operator remediation rather than disappear silently.

Graph acceptance is not proof of recipient delivery. Check Sent Items, actual recipient inboxes and NDRs. Exactly-once sending across an ambiguous provider response cannot be guaranteed; manual reconciliation is deliberate.

## 6. Verify security, metrics and recovery

Security: an unauthenticated client, wrong tenant/audience/SPA, expired token and non-admin user must not read admin data or approve. Authority identities must access only assigned concerns and their allowed completion action. Revoke a role and retest. Verify mailbox scoping against a mailbox outside the approved scope. Inspect logs for useful IDs/errors without tokens, secrets or unnecessary original message bodies. Validate trusted TLS and test narrow-pane layout, keyboard access and stale-response handling in both clients.

Metrics: use the offline fixture as the numerical oracle, then independently count a small labelled test cohort. Check received-time filters and UTC boundaries, average/median student-confirmed turnaround, routing accuracy denominator, pending/overdue totals, closure, reopen/unresolved rates and accepted reminder/escalation counts. Unlabelled accuracy must remain unavailable rather than invent a percentage. Simulated data is excluded from live metrics by default. Collect reviewed labels and a manual-process baseline before claiming an improvement. The classifier remains the **rule-based baseline**; future AI must be evaluated on labelled pilot examples before any replacement.

Recovery: stop all writers, take a SQLite backup with the SQLite backup API/tooling, and restore it to a **separate path** before trusting it. Do not copy only a live main database file while WAL writes are active. Test migration of a copy of the old database through version 4; compare concern/receipt/audit/job counts and run `PRAGMA integrity_check` and `PRAGMA foreign_key_check`. Migration tests also inject failure and verify rollback. If the new unique provider-ID index reveals legacy duplicates, stop and reconcile evidence; do not delete audit records or fabricate IDs to bypass the constraint. Resume a single worker only after restore/migration verification. Preserve receipt history while Microsoft can replay messages.

`/healthz` is web/DB liveness. `/readyz` adds required-schema readiness. Neither proves worker activity, Microsoft consent or mailbox delivery: inspect authenticated Routing & health and actual pilot messages separately. Database files receive restricted creation permissions; encrypted storage/backups, retention and host access remain institutional deployment responsibilities.

## 7. Acceptance and production gate

Do not activate production until all automated tests pass, the real Outlook matrix passes, the 24/48-hour soak passes, backup restore succeeds and IT has signed off on scoped permissions and verified recipients. Required invariants are zero unapproved forwards, zero unauthorized approvals/data access, no duplicate accepted forward under replay, visible recovery for every injected failure and a complete audit trail.

Still required externally: verified mailbox type/access mode; tenant/application IDs and configuration; worker credential; scoped Graph/Exchange permissions and consent; admin/authority object IDs; Rita/Hemant verified emails and any optional departments/escalation contact; ingestion cutoff; HTTPS hosting/certificate and add-in deployment; tested backup/monitoring/retention; actual client/mailbox pilot evidence; and **explicit authorization before live UPES access/sending**.

Dependency scans on 25 October 2026 reported no known vulnerabilities: npm audit for frontend dependencies and pip-audit for resolved Python 3.12 requirements (including Gunicorn, PyJWT and cryptography). These are point-in-time advisory checks, not a penetration test or a guarantee about future dependency resolutions. Repeat scans when building/releasing and after dependency changes.

## 8. Functionality recheck — 26 October 2026

Source review covered configuration, identity, ingestion/Graph transport, classification, extraction, routing, workflow, outbox/reconciliation, scheduling, metrics, read models/API, migrations, CLI, Outlook controller and container configuration. Automated success is scoped to the cases exercised; it is not a claim of exhaustive formal verification or a completed live institutional pilot.

- **Receive and identify:** `test_workflow`, `test_delivery` and `test_audit_regressions` cover six categories, unknown/ambiguous requests, duplicate/provider-ID replay, concurrent intake, malformed-message cursor retention and auto-reply review. Actual Inbox delta behavior and the selected ingestion backlog remain tenant-pilot checks.
- **Approve and route:** `test_api`, `test_workflow` and `test_authority_api` cover explicit approval, stale/double decisions, rejection, rerouting, missing recipient, server actor attribution and denied authority approvals. A new six-category lifecycle test checks each mapping through approved forwarding, completion, follow-up and confirmed closure. No new authority email/department is inferred.
- **Track and resolve:** `test_delivery`, `test_capstone` and the walkthrough cover addressed/confirmation states, negative/unclear replies, reopening and fresh approval, explicit versus administrative closure and durable audit records. A new state-edge regression checks every disallowed transition leaves state, version and history unchanged. Student matching is sender/thread based; institutional spoof protection and real reply threading must still be verified.
- **Deliver and recover:** mocked Graph tests cover recipient/context preservation, acknowledgements, privacy-limited reminders, bounded read/throttle retry, refused redirects, ambiguous sends, restart quarantine, reconciliation and post-send metadata reads without resending. Attachments, actual recipients, Microsoft acceptance/NDRs and service-level permissions still require a test mailbox. Code inspection confirms the single-worker file lock; an operational second-worker test remains in the pilot checklist.
- **Remind and measure:** capstone tests cover due edits, bounded scheduling, missing escalation configuration, cancellation, contact changes, simulation isolation, nullable extraction, labelled accuracy, turnaround/closure/reopen arithmetic and offline candidate evaluation. The real 24/48-hour soak and independently labelled pilot measurements are still outstanding.
- **Authorize and present:** RSA token tests and API tests cover claim/signature rejection, admin/authority boundaries, origin checks and input validation. The 14 add-in tests cover safe text rendering, selection races, approval actions, assigned work, due dates, metrics and polling. Actual NAA consent, Outlook host compatibility and individual account attribution remain IT/client checks.
- **Persist and operate:** migration tests cover legacy preservation, concurrent execution, rollback, constraints and private database creation. A new HTTP test confirms `/readyz` returns 503 for a missing migration while `/healthz` remains live. Actual backup restore, encrypted host storage, retention, monitoring and deployment supervision require operational sign-off.

Recheck outcome: all 85 backend tests passed both locally and in the existing audited Docker image with networking disabled; all 14 add-in tests, all 22 offline walkthrough checks and the frontend build passed. No further runtime defect was reproduced in this pass. No application functionality was redesigned, and no mailbox access or sending was enabled.

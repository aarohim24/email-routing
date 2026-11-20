# UPES Alumni Routing — inside Outlook

Alumni Admin and institution-verified assigned authorities work in the **UPES Alumni Routing** Outlook sidebar. There is no separate staff dashboard or local-password login. A small Python backend and database worker continue operating while Outlook is closed. The add-in is an Office.js task pane, not a Windows browser extension or COM/VSTO plug-in.

**Live mailbox access is disabled. `DRY_RUN=true` prevents all draft creation and real forwarding/follow-ups. No live UPES connection or email sending has been performed.**

## Confirmed target and remaining verification

Staff primarily sign into the alumni email account on Windows, using **Outlook on the web / new Outlook for Windows**. This is the implementation target. The intended setup is a directly signed-in, standalone alumni account. That description does **not** verify Exchange's actual mailbox type.

Before connecting, UPES IT must check the mailbox's `RecipientTypeDetails` in Exchange Admin Center or `Get-EXOMailbox -Identity alumni@upes.ac.in | Select RecipientTypeDetails,PrimarySmtpAddress`. Set `MAILBOX_TYPE=standalone` for a verified user mailbox or `shared` for a verified shared mailbox, then `MAILBOX_TYPE_VERIFIED=true`. Until verification, the worker refuses live mailbox access even if `LIVE_MAILBOX_AUTHORIZED=true`. No command against Microsoft has been run by this project.

For a shared mailbox, deploy the add-in to the staff's licensed primary identities and validate the exact shared-mailbox opening mode with IT. The manifest declares shared-folder support. Do not enable direct sign-in on a shared mailbox just to use this app. If staff all use one standalone alumni sign-in, the audit can identify that Microsoft account, **not the individual human using it**. Named staff identities with delegated mailbox access are needed for per-person accountability; the API already supports an allowlist of multiple Entra user object IDs.

Supported clients must offer Office Mailbox requirement set 1.13 and **NestedAppAuth 1.1**. The sidebar checks NAA at runtime and fails closed on unsupported builds. Mobile, classic Outlook and Mac have not been qualified for this deployment. Pending Approvals is a view inside the sidebar. On web/new Outlook, open a message to launch the add-in, then optionally pin it; do not assume the add-in can be launched with the Reading Pane hidden or no message selected. A missing current item is handled safely if selection changes while pinned.

## What staff see

Open an email, choose **Apps → UPES Alumni Routing** (or its ribbon command) and optionally pin the sidebar. Institutional Microsoft sign-in authorizes the API; no new app password is required.

- **Current email:** ticket ID, detected category, proposed authority/email, current state, original inert text, deliveries and paginated full history. The add-in uses the open item's `internetMessageId` to look up a persisted receipt. It does not classify or ingest a client-submitted body, and it never guesses the ticket from a subject. Confirmation replies resolve to the original ticket through their receipts.
- **Actions:** Approve, Reject, Change Routing, Mark Addressed, Follow-up, Close, Reopen. Only valid actions are enabled by the API; the backend independently validates every command. Reasons are required for rejection, routing changes, manual close and reopen. Approval requires explicit confirmation of the displayed recipient.
- **Pending Approvals:** paginated approval queue, plus Needs Review and Failed Deliveries filters, all within the same sidebar.
- **Routing & health:** set institution-verified addresses for Rita Godiyal and Hemant Kumar, see fixed category mappings, worker heartbeat and ingestion failures. Changing an address invalidates affected pending routing decisions. No addresses are guessed or embedded in production configuration.
- **Failure recovery:** Prepare Retry returns failed forwarding to fresh approval. Ambiguous/interrupted sends require evidence-based reconciliation against Microsoft Sent Items and the saved draft before retrying. Pending or uncertain deliveries lock conflicting actions.

Selecting another email immediately clears the prior ticket. A generation guard discards slow responses for previously selected emails. All email content and history render through `textContent`, never `innerHTML`. Tokens are kept in MSAL memory, never URLs, localStorage or application logs.

## Backend retained and adapted

A modular monolith with SQLite WAL and one durable database worker. No Kafka, Redis, RAG, LLM, vector database or separate automation services.

- `alumni/classifier.py`: modular, deterministic classification. No matches, conflicting matches, explicitly out-of-scope documentation or detected negation become `UNKNOWN → NEEDS_REVIEW`.
- `alumni/workflow.py`: one concern state machine, centralized DB routing, approval/version checks and audit. Transcript, Provisional Degree, Degree Verification, Medium of Instruction and Degree Collection map to Rita Godiyal; Migration maps to Hemant Kumar. Authority emails start NULL.
- `alumni/graph.py`: worker-only OAuth client credentials; Microsoft Graph inbox delta ingestion, forward/reply draft operations and sending. The add-in never receives its client secret or a Graph token. Native forwarding retains original context and attachments; the institutional mailbox remains the sender and the original student is named/set as Reply-To.
- `alumni/worker.py`: independent ingestion, cursor persistence, delivery claims, confirmation replies and crash recovery. Exactly one worker is enforced by a file lock. Multiple web workers can share the same local-host database.
- `alumni/identity.py`: verifies signed Entra v2 **API access tokens**, RS256, tenant-specific issuer/JWKS, audience, expiry/nbf/iat, delegated `access_as_user` scope, allowed frontend `azp`, and an immutable user `oid`. The API then authorizes either an Alumni Admin allowlist entry or a current database authority assignment on every request. An ID token, Graph token, app-only token, profile email, cookie, spoofed actor field or institutional user without admin/authority assignment cannot authorize staff actions.
- `alumni/api.py`: same-origin bearer-only staff API and add-in asset host. No cookie sessions, wildcard CORS, password login, client secret or development auth bypass. User identity is derived server-side. Requests are bounded and validated; failed commands leave retained records intact.
- `alumni/service.py`, `db.py`, `migrations/`: read models, database binding to one mailbox, unique email IDs, indexes, constraints and migrations. Migrations 002 and 003 preserve existing concerns, jobs, receipts and audit. Migration 003 adds the capstone fields, notification outbox types, policy and explicit pilot labels. Legacy password-session tables remain unused for data compatibility; old sessions cannot authenticate to the new API. Old dashboard/security modules are removed.
- `addin/`: Office manifest template, bundled MSAL NAA client, narrow-sidebar UI, and mocked Office host tests. `scripts/render_manifest.py` creates an origin-specific XML manifest from explicitly supplied values.

`RECEIVED → IDENTIFIED → AWAITING_APPROVAL → FORWARDED → ADDRESSED → AWAITING_CONFIRMATION → CLOSED` remains the happy path. Rejection records review; reopened concerns require another approval. Mark Addressed queues the student follow-up automatically, preserving the earlier workflow. Separate Follow-up handles an addressed case without an active follow-up job. Exact unquoted `RESOLVED` / `NOT RESOLVED` replies from the original sender in the waiting conversation close/reopen; unclear replies require review. Thread/sender matching is not cryptographic sender authentication: Microsoft 365 anti-spoofing policies remain necessary.

### Idempotency and delivery guarantees

Email receipts and initial workflow updates commit atomically. The delta checkpoint advances only after the whole page is safely processed. A failed message pauses advancement and exposes its Graph ID/error; replay deduplicates previously committed receipts. The worker does not delete or mark emails read.

Approval and job creation commit together; jobs snapshot recipient, approver, version and dry-run mode. Duplicate or stale commands cannot queue a second active delivery. Changing modes invalidates queued jobs from the previous mode instead of converting simulated approvals into live sends. A database cannot be silently rebound to a different mailbox.

Safe reads retry transient failures with bounded exponential backoff/jitter (four attempts). Explicit throttling responses also retry within bounds. Ambiguous writes never automatically resend. The worker saves draft ID before send; a timeout/5xx or restart after a RUNNING claim makes the delivery UNCERTAIN and ticket FAILED. The operator must verify Sent Items/draft evidence and reconcile. Graph's send acceptance is not a recipient delivery receipt; monitor NDR/bounces. Exactly-once network sending cannot be promised without provider idempotency. This application prevents blind resend after ambiguity rather than claiming that guarantee.

Initial delta sync scans the Inbox without a server-side date filter, then applies `MS_INGEST_SINCE` locally to avoid Graph's filtered-delta 5,000-message cap. Choose the timezone-aware cutoff explicitly; changing it later fails visibly. Mail older than this cutoff is deliberately out of scope. Mailbox rules must not move/delete new concerns before ingestion. Other folders are not tracked by this adapter. Expired cursors stop visibly: back up the DB and perform an operator-reviewed cursor reset/rescan, keeping receipts for deduplication.

## Local setup and offline validation

Clone the project on the development or local test machine:

```sh
git clone https://github.com/aarohim24/email-routing.git
cd email-routing
```

On Windows, run the backend in WSL2 or Linux containers. The Outlook add-in
still opens in Outlook on the web or new Outlook for Windows. Cloning the
repository does not grant Microsoft mailbox access; institutional configuration
and trusted HTTPS remain required for testing inside Outlook.

See [CONTRIBUTING.md](CONTRIBUTING.md) for coding conventions, commit style and
required checks.

Python 3.9+ for local app/tests; production image uses Python 3.12 and Gunicorn (Python 3.10+). Node 22+ for add-in builds/tests. No institutional credentials are needed to run automated tests. They mock Microsoft tokens, Office host and Graph; there is no production fake-login mode.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm --prefix addin ci --ignore-scripts
npm --prefix addin run build
.venv/bin/python -m unittest discover -s tests -v
npm --prefix addin test
```

The local Python-version marker skips Gunicorn on Python 3.9; use Docker or Python 3.10+ for deployment. The frontend build emits `alumni/static/` (gitignored); Docker builds those assets in its Node stage.

Create configuration without inventing Microsoft values:

```sh
cp .env.example .env
set -a
source .env
set +a
.venv/bin/python -m alumni migrate
.venv/bin/python -m alumni serve
# In a second terminal with the same environment:
.venv/bin/python -m alumni worker
```

The development server binds HTTP on 127.0.0.1:8000. Outlook requires **trusted HTTPS**, including localhost: put a local trusted TLS reverse proxy in front and set `BASE_URL` to its HTTPS origin. The backend and add-in assets share this origin. With Microsoft configuration absent, health/assets are available but every staff API fails closed. Opening the task-pane HTML in a normal browser does not emulate Outlook and is not a replacement dashboard.

For synthetic backend data in a **separate** local database:

```sh
export DATABASE_PATH=data/demo.db
.venv/bin/python -m alumni demo
```

Demo addresses end in `.invalid`; never activate this database. Authorized staff access to synthetic data inside real Outlook still needs a test Entra registration and add-in sideload. Do not use the live UPES mailbox for this step.

## Microsoft configuration required — all still missing

Use single-tenant registrations in the institution's actual Entra tenant. Names below describe responsibilities, not invented identifiers.

1. **Institutional tenant:** actual `MS_TENANT_ID` tenant UUID and confirmation of Exchange Online availability.
2. **Backend API registration:** actual `API_CLIENT_ID` UUID. Expose application ID URI `api://<API_CLIENT_ID>` and delegated scope `access_as_user`; configure the API to issue v2 access tokens (`api.requestedAccessTokenVersion=2`). This API registration needs no client secret and no Graph permissions.
3. **Outlook SPA registration:** actual `ADDIN_CLIENT_ID` UUID, single tenant, public client with **no secret**. Add a **Single-page application** trusted-broker redirect `brk-multihub://<actual-host[:port]>` (no path) for NAA. Also register the exact HTTPS URI `<BASE_URL>/addin/auth.html`. Grant only the backend API's delegated `access_as_user` scope, provide institutional consent, and preauthorize this SPA for the API scope if required by tenant policy. No delegated Graph scopes are requested by the add-in. There is no legacy Exchange callback-token, implicit-flow or OBO dependency. This release requires NAA and deliberately has no less secure fallback on unsupported clients.
4. **Admin authorization:** real Entra **user object IDs**, not emails, client IDs or group IDs, in comma-separated `ADMIN_OBJECT_IDS`. Empty means nobody can access concerns. Include the alumni account's user object ID if it is the intended directly signed-in administrator; include staff user IDs when using delegated/shared access. Enforce appropriate MFA/Conditional Access and restrict the API enterprise app to intended users. Adding a user to the Outlook deployment alone does not grant API admin rights.
5. **Background worker registration:** actual `MS_CLIENT_ID` and rotated `MS_CLIENT_SECRET`, injected through the server's secret manager/environment. The worker requires app-only Graph **Mail.ReadWrite** (read messages/create drafts) and **Mail.Send**, with Exchange-admin authorization restricted to **only `alumni@upes.ac.in`**. Prefer mailbox-scoped Exchange Application RBAC assignments, and ensure overlapping tenant-wide Entra application grants do not defeat that restriction. Have Exchange IT validate effective in-scope and out-of-scope access. Credentials are never shipped to Outlook.
6. **Mailbox:** verified mailbox type and permission to ingest from its Inbox, confirmed client versions/builds with NAA, an agreed `MS_INGEST_SINCE` timestamp, and Inbox-retention/rule review. For shared access, configure the intended staff delegates and validate add-in availability in the actual mailbox-opening mode.
7. **Authorities:** UPES-verified Rita Godiyal email and Hemant Kumar email entered through **Routing & health inside the add-in** in the production DB. Both remain unconfigured until supplied.
8. **Explicit authorization:** institutional approval before `LIVE_MAILBOX_AUTHORIZED=true` (reads), followed by separate explicit approval before `DRY_RUN=false` (drafts/sends). Neither has been granted in this project.

## Packaging and institutional deployment

1. Choose the actual private production HTTPS origin, trusted TLS certificate, restricted single-host persistent database storage, backup/restore schedule, secret injection and worker supervision. Set the exact `BASE_URL`. The API and assets must be reachable from staff Outlook clients; no third-party cookies are required.
2. Build/run the modular monolith:

   ```sh
   docker compose up --build -d
   ```

   A one-shot migration service precedes web/worker. Web binds to host loopback; terminate TLS at an institution-managed reverse proxy. Docker runs as non-root. Use a local volume, not NFS or multi-host SQLite replicas. Web process alone does not ingest email; worker runs separately from the same codebase/database.
3. Choose a stable manifest UUID (e.g. generate once using `uuidgen`), distinct from all Entra registration IDs. Render a deployment-specific manifest:

   ```sh
   .venv/bin/python scripts/render_manifest.py --origin https://YOUR-ACTUAL-HOST --id YOUR-STABLE-MANIFEST-UUID --output build/manifest.xml
   ```

   These placeholders are instructions, not usable production values. The manifest displays **UPES Alumni Routing**, requests only Office **ReadItem** access, supports pinning and shared folders, and loads the sidebar from your host. All forwarding still happens in the backend.
4. UPES Microsoft 365 admin should validate the generated manifest with Microsoft's current manifest validator, then upload it through **Microsoft 365 admin center → Settings → Integrated apps → Upload custom apps → Office Add-in**, assigning the intended accounts/test group. Follow tenant-specific policies if custom add-in upload is restricted. No Marketplace publication is required for an internal deployment. Add-in-only XML is used for supported web/Windows task-pane commands; no Windows executable installer is required.
5. First sideload to a **dedicated test mailbox/tenant-controlled pilot**, not the live alumni mailbox. Supply test identities, test mailbox credentials/scopes and verified test routes. Validate actual NAA consent/token issuance, server admin-denial cases, pinning/item switching, original-vs-reply IDs, shared-mailbox opening mode if applicable, attachments, approval/rejection, follow-up/thread correlation, retry/reconciliation and restart recovery. Automated mocked tests do not establish tenant permissions or Outlook-host compatibility.
6. For production, create a fresh production DB, take a backup, configure verified real routes and the admin allowlist, and review mailbox scoping. After explicit read authorization, perform any dry-run ingestion check in a **separate staging database**. Do not use that database for real delivery. Start the fresh production database only after explicit send authorization, with the agreed intake cutoff and live mode. Simulated success advances simulated lifecycle states and belongs in staging. For production set `DRY_RUN=false` only after authorization and obtain fresh approvals on the production intake. A ticket whose intake mode differs from the deployment cannot queue live workflow delivery. Never promote a demo DB or reinterpret dry-run success as real delivery.
7. Monitor worker heartbeat age, ingestion errors, FAILED/UNCERTAIN jobs and institutional NDRs. `/healthz` checks web/DB liveness, not mailbox health. Keep email/receipt/audit data encrypted at rest with restricted access, tested SQLite backups/restores and institution-approved retention. Do not delete deduplication receipts while Graph can replay old messages. No automatic retention deletion is implemented.

## Validation completed and limits

See [TESTING.md](TESTING.md) for the final audit findings, credential-free walkthrough, isolated Docker checks, Outlook pilot matrix, reminder soak tests and production acceptance gates. The walkthrough command is `.venv/bin/python scripts/offline_walkthrough.py` and uses only a temporary synthetic database.

Backend tests cover classification/routing/lifecycle, duplicate ingestion/approval, crash recovery and ambiguous sends, Graph retry behavior, actual RSA-signed token validation and rejection cases, API authorization/origin/input checks, stable email/reply lookup, mailbox binding and migration preservation through version 4. Add-in tests execute the controller with a mocked Office/MSAL host, including current-email rendering, no selected item, stale message-response rejection, explicit versioned approval and pending approvals. The production JS bundle builds locally. The development XML manifest passes Microsoft's `office-addin-manifest@3.0.1` validator (including schema/acceptance checks). The multi-stage Docker image builds, and backend tests also pass inside it with networking disabled.

Tenant sign-in, validation of the final production-origin manifest, real Outlook sideloading, Exchange permission scoping and live mail round-trips remain **unverified until UPES supplies configuration and authorizes a test environment**. No real mailbox has been contacted. The new UI has not been claimed as installed in Outlook.

Primary references: [Microsoft NAA sample](https://learn.microsoft.com/en-us/samples/officedev/office-add-in-samples/outlook-add-in-sso-naa/), [NAA registration/configuration](https://learn.microsoft.com/en-us/office/dev/add-ins/develop/create-sso-office-add-ins-aspnet), [pinned task panes and item changes](https://learn.microsoft.com/en-us/office/dev/add-ins/outlook/pinnable-taskpane), [shared mailbox deployment](https://learn.microsoft.com/en-us/office/dev/add-ins/outlook/delegate-access), [no-item-context client limitations](https://learn.microsoft.com/en-us/office/dev/add-ins/outlook/contextless), [Graph delta](https://learn.microsoft.com/en-us/graph/delta-query-messages), [Exchange Application RBAC](https://learn.microsoft.com/en-gb/exchange/permissions-exo/application-rbac).

## Capstone additions: reminders, authority completion and measured pilot

The architecture remains the same add-in, API, SQLite database and single background worker. The initial pilot is **Transcript, Provisional Degree, Degree Verification, Medium of Instruction, Degree Collection and Migration**. Everything else goes to `NEEDS_REVIEW`; a mixed transcript + grade-card or recommendation-letter request also stays in review. **Every authority forward requires Alumni Admin approval**, regardless of classifier confidence. Automatic acknowledgements/reminders are notification messages, not authority forwards, and do not issue or approve documents.

### Due dates and bounded reminders

Admin configures **Routing & health → Reminder policy** in Outlook. Defaults are: authority due **24 elapsed hours after accepted forwarding**, reminder interval **24 hours**, maximum **2 reminder reservations per forwarding cycle**, escalation **24 hours after the due date** (normally hour 48). These are clock hours, not working-day or holiday calculations. Per-ticket **Set due date** accepts an explicit local date/time and stores UTC. Restoring the default uses the actual forwarding timestamp. Configured policy/due changes are audited. Changing default due hours does not silently rewrite existing due dates; Admin can edit those explicitly. Interval/cap/escalation settings apply to subsequent scheduling checks.

The worker checks every 15 seconds. Only `FORWARDED` tickets waiting for authority completion receive authority reminders. When the authority marks Addressed, queued reminders are cancelled; student confirmation follows the existing resolution workflow. Each newly approved, accepted forward starts a fresh routing cycle. Restarting does not replay missed reminders in a burst: at most one next reminder is queued per scheduler check/cadence, within the persisted cycle cap. Unique outbox delivery keys prevent duplicate acknowledgement/reminder/escalation reservations. Pending notices whose verified contact changes are cancelled at dispatch rather than sent to a removed contact. Cancelled reminder reservations consume the cycle cap, preserving the upper bound after repeated due-date edits. Disabling the policy prevents pending reminders/escalations from being dispatched; an already dispatched network operation cannot be recalled.

Escalation needs an **institution-verified escalation email**, explicitly configured by Admin. The default is NULL: no recipient or department is guessed. Missing authority/escalation recipients are shown on the ticket and in health, with a single blocked audit per condition instead of repeated audit noise. Escalation is a separate, once-per-cycle notice to the configured contact. Reminder/escalation text contains the reference and due/status context, not the original email body or attachments.

`ACK`, `REMINDER` and `ESCALATION` reuse the same durable draft/send/reconciliation safeguards as core deliveries. Their failures are visible in delivery history, the Failed Deliveries/Notifications queue and health. **Notification failures do not corrupt the concern lifecycle**: a failed acknowledgement does not turn a valid newly received request into a failed routing case. Admin can retry the latest definitively failed notice; ambiguous notices require reconciliation first. Obsolete reminder retries are blocked after completion. Mode changes cannot turn staging notices into live sends.

Legacy forwarded cases receive default due dates only where the old audit contains an explicit forwarding timestamp. Missing historical timestamps remain missing; Admin must set an explicit due date instead of the system inventing one. Historical acknowledgements are not backfilled or resent.

### Department/authority completion within Outlook

In **Routing & health**, Admin enters each authority's verified email, optional verified department, and one or more institution-verified **Entra user object IDs**. All department fields and authority assignments start empty. Neither Rita Godiyal's nor Hemant Kumar's department is inferred from their name or routing categories. Object IDs are validated UUIDs; email addresses never grant permissions.

Deploy the same add-in to the authority's own licensed Outlook identity as well as Alumni Admin. Give that identity institutional consent for the existing API delegated scope, and include it in enterprise-app assignments if the tenant restricts sign-in. No additional Graph permission, mailbox password or new service is needed for authorities.

When an authority signs in, they see **Assigned to me** and only their assigned tickets. They can mark a currently forwarded concern **Addressed**, with a required completion note/evidence. They cannot approve, reject, reroute, close/reopen, change configuration, view pilot metrics or inspect another authority's ticket. The API checks current assignment and state again inside the completion transaction, so revoking an assignment takes effect on subsequent requests. Completion is persisted/audited immediately and queues the existing student follow-up. Admin's open sidebar/queue synchronizes every **five seconds while active**; this is near-real-time polling, not a push-notification guarantee. In-progress admin text is preserved and a stale decision must be refreshed before submission.

The worker safely reads the sent message's `internetMessageId` after acceptance and links forwarded/notification emails to their ticket. These metadata reads retry in later worker passes and **never trigger a resend**. While Microsoft has not exposed the sent item yet, authorities can use Assigned to me instead of waiting for the selected-message link.

### Receipt acknowledgement and nullable structured data

New original intake atomically stores its receipt, concern/classification and exactly one `ACK` outbox reservation. It acknowledges receipt with the ticket number; it does not claim the request is approved or fulfilled. Duplicate intake creates no second ACK. Confirmation replies to existing tickets do not generate fresh receipt acknowledgements. Automated/self-generated messages (recognized by standard auto-response headers, subject prefixes or sender) suppress ACK and log the reason, avoiding acknowledgement loops. Exchange anti-spoofing/automatic-response policies should still be tested in the pilot; external systems may omit identifying headers.

`alumnus_name`, `enrolment_number`, `alumni_number` and `requested_document` are dedicated database fields shown in Outlook. The extractor reads explicit labelled lines such as `Name:`, `Enrollment Number:` and `Alumni ID:`. Missing, conflicting or invalid identities remain SQL NULL. It does not invent names from email usernames, numeric fragments or signatures. The requested document is a supported detected category or a subsequent explicit admin routing classification; unsupported/unknown requests keep it NULL. Extraction provenance and routing changes are audited. These fields are supplied email data, not identity verification.

### Pilot metrics and definitions

Admin opens **Pilot metrics** inside Outlook. The JSON read endpoint is `/api/metrics?mode=live` with optional timezone-aware `from` (inclusive) and `to` (exclusive) **receipt-date cohort** bounds. The sidebar converts its local time controls to UTC. `mode=simulated` and `mode=all` are explicit diagnostic options. Live is the default and excludes simulated concerns and delivery counts. Keep staging and production databases separate.

- **Average/median turnaround:** hours from receipt to explicitly verified fulfilment for currently closed cases. Exact positive student confirmation sets this timestamp; Admin may also close as **Verified fulfilled** with evidence. Administrative closures (duplicates, withdrawals, out-of-scope requests) count as closed but are excluded from fulfilment TAT. Reopening clears current fulfilment/closure timestamps; TAT is recomputed after subsequent verified closure. The sample size and invalid negative-duration exclusions are exposed.
- **Routing accuracy:** first accepted category **and** authority versus an explicit, independently reviewed admin label, divided by labelled, routed, in-scope cases. Corrected routes are not silently scored as the original route being correct. The initial rule-based classification accuracy is separately exposed against all reviewed labels, including UNKNOWN. Unlabelled cases are not assumed correct.
- **Pending/overdue:** pending means not CLOSED; overdue authority cases are still FORWARDED and past their persisted due date. Student confirmation waiting is pending, but does not continue prompting an authority that already marked completion.
- **Closure rate:** current CLOSED cases / all cases in the selected receipt cohort, including administrative closures. Verified fulfilled count is separately exposed so closure is not confused with successful document delivery.
- **Reopen/unresolved:** distinct cases ever reopened / cohort size, and distinct cases with an explicit negative student confirmation / cohort size. Raw reopen event count is also exposed. A manual reopen and an explicit student “not resolved” are separate measurements.
- **Reminders/escalations:** accepted (or explicitly simulated) SUCCEEDED notification jobs, with separate pending, failed and uncertain counts. Graph acceptance is not a delivery receipt. A queued, cancelled, failed or uncertain attempt is not counted as delivered/accepted.

The cohort registry includes retained unknown/manual-review requests so they do not disappear from pending/closure reporting; routing accuracy uses the narrower reviewed in-scope denominator described above. With no suitable sample, rates/TAT return **null / Not yet measurable**, not a fabricated zero or 100%. The system records measures; it does not claim improved performance until a real pilot/baseline comparison exists.

Admin uses **Label pilot outcome** on a ticket to record the correct category and institution-verified authority, with a rationale. Labels are separate from operational routing; revisions preserve audit history and never forward or reroute the ticket. The role-restricted API is `POST /api/concerns/{id}/label`. Capture a pre-pilot baseline and evaluate a representative reviewed sample; audit timestamps alone are not a substitute for independent ground truth.

### Rule-based baseline and future AI evaluation

Production selection remains explicitly **RuleBasedClassifier**, currently `rules-v2` (the added out-of-scope guard is versioned; legacy classifications retain their original stored version). Intake stores the initial category/authority and classifier version. A future candidate implements the same `version` and `classify(subject, body)` result contract. `alumni.pilot.evaluate(candidate, labelled_cases)` reports sample size, accuracy and confusion counts without changing the classifier, mappings or approval gate.

Run the current baseline against stored reviewed labels without sending email or exporting raw personal data:

```sh
.venv/bin/python -m alumni evaluate-baseline --mode simulated
# For authorized production data, use --mode live (default).
```

This reports aggregate evaluation only. Before changing production selection, compare the baseline and candidate on the same independent held-out cases, inspect unknown/ambiguous/error behavior and obtain institutional privacy approval for any external AI processing. No LLM, external AI call, autonomous promotion or additional infrastructure has been introduced. Every forward will still require Admin approval if a classifier is replaced later.

### Additional activation and upgrade steps

Back up the existing database and run `python -m alumni migrate` with worker stopped; migration 003 preserves previous delivery IDs/history and rebuilds the outbox with constraints for the additional notification kinds and delivery keys. Rebuild/redeploy the same image/add-in assets and reload the sidebar. Existing Microsoft tenant/API/SPA/worker registrations, mailbox verification, scoped Graph permissions, HTTPS, storage and explicit read/send authorizations remain required as listed above.

Additionally supply **verified authority Entra user object IDs**, optional verified department names, and the verified escalation contact if email escalation is desired. Deploy/consent the add-in for authorities. Review default reminder times, the cap and per-case due dates with Alumni Admin. Validate acknowledgements, reminders, authority authorization/revocation, student confirmation, metrics labels and uncertain-send reconciliation in a dedicated test mailbox first. Live UPES sending remains disabled until explicitly authorized; neither a successful build nor a passing test changes that flag.

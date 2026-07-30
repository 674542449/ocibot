# Web audit notes

## Pass 10 — full sweep: bugs, proactive OCI calls, common vulns (0.4.37, 2026-07-30)

Requested as three questions: are there bugs, is anything calling Oracle without
being asked, and do the usual vulnerability classes hold up.

### Fixed

| Issue | Severity | Notes |
| --- | --- | --- |
| Every state-changing REST endpoint was authorized by the session cookie alone, with `SameSite=Lax` as the only barrier. `OCIBOT_COOKIE_SAMESITE=none` is a supported setting that removes it entirely; and even on `Lax`, cookies are scoped to a **site**, so an attacker-controlled sibling host under the same registrable domain (`blog.example.com` vs `panel.example.com`) can POST with the victim's session. The WebSocket had checked `Origin` since pass 4 — the REST API never did. | Medium | New `web/backend/origin_guard.py` holds one policy for both callers, so they cannot drift apart again. POST/PUT/PATCH/DELETE require `Origin` to match `Host`, `X-Forwarded-Host`, or an `OCIBOT_CORS_ORIGINS` entry. Absent `Origin` still passes (non-browser clients carry no victim cookie). GET/HEAD/OPTIONS untouched. `OCIBOT_ORIGIN_CHECK=0` is an escape hatch for a proxy that rewrites Host without `X-Forwarded-Host`; the 403 body names both the real fix and the hatch, because otherwise every write fails at once and the cause is unguessable. |
| The login audit added in 0.4.34 records failed attempts, including ones against usernames that do not exist — so unauthenticated traffic writes rows and the attacker chooses how many. `audit_logs` had no ceiling of any kind, so credential stuffing against an exposed panel grows it until the disk fills, taking Postgres and the stack with it. | Medium (availability) | `prune_audit_log()` enforces `OCIBOT_AUDIT_RETENTION_DAYS` (180) **and** `OCIBOT_AUDIT_MAX_ROWS` (50 000). Both, because a window alone does not bound a burst inside it and a row cap alone keeps ancient rows on a quiet install. Runs in `Worker.beat` — database only, never Oracle — so it survives `OCIBOT_WORKER_BACKGROUND_OCI=0`; throttled to hourly since beat fires every few seconds. |

### Proactive Oracle calls — inventory

The answer is **capacity retry only**, which the operator wants kept.

- API `lifespan`: `init_db`, first-admin promotion, weak-secret warning. No OCI.
- Worker: `beat` (database; now also audit pruning) + `tick_capacity`. With no
  enabled job the candidate query returns empty and nothing reaches a client, so
  an install with no capacity job issues zero background requests.
- All OCI SDK usage is confined to `app/oci_client.py`. Exactly two session
  managers exist: `oci_bridge._sessions` (request handlers) and `Worker.sessions`
  (capacity retry).
- Other background threads in the API process touch no OCI: the three WebSSH
  coroutines speak SSH, and the self-update thread runs docker compose.
- One asynchronous continuation, reported rather than changed:
  `launch_service.schedule_post_launch_adjustments` spawns a daemon thread that
  waits for boot-volume hydration and then resizes VPUs. It fires only when the
  operator launched an instance with a non-default boot VPU — a continuation of
  that click, not a poll. Accepted cost: an API restart mid-flight abandons the
  adjustment silently. Making it synchronous would block the launch response for
  up to ten minutes.

### Checked, no finding

- No raw SQL / `text()`, no `eval` / `exec` / `pickle` / `yaml.load`, no
  `shell=True`, no `os.system`.
- No `v-html` or `innerHTML` anywhere in the SPA, so the attacker-controlled
  User-Agent stored with each login attempt renders escaped.
- Every REST route takes a user dependency; all 8 admin routes use
  `get_admin_user`. The only route without `Depends` is the WebSocket, which
  authenticates manually from the cookie (never a query-string token).
- Audit query: non-admins see only their own rows; admins additionally see
  `owner_id IS NULL` (anonymous events), not other accounts' rows.
- No response model carries `private_key_pem` — `TenantOut` and
  `TenantParseResult` expose `has_private_key` only.
- Backup restore reads members with `zf.open(member)` and never extracts to
  disk: no zip slip.
- SPA fallback rejects `..` and confines the resolved path with
  `relative_to(dist_root)`.
- JWT: HS256 hardcoded (not env-overridable, so no algorithm confusion), `exp`
  present and verified, `token_version` invalidates issued tokens on password
  change and logout-all.
- Login: rate limiter runs **before** password verification, keyed per IP and per
  IP+username; bcrypt runs on a dummy hash for unknown users to flatten timing;
  `LoginRequest.username` is bounded so the limiter dict cannot be grown without
  limit. With 10 attempts / 5 min and a 6-digit code, TOTP brute force is not
  feasible.

### Accepted, unchanged

- The default `OCIBOT_CORS_ORIGINS` ships with `localhost:5173`, `127.0.0.1:5173`
  and `localhost:8080` for the Vite dev server. Those origins therefore satisfy
  the new Origin check too. Exploiting it requires already running code on the
  victim's machine that serves from one of those ports, and removing them breaks
  the documented dev workflow. Operators who care should set the variable to
  their own origin (the README row and `.env.example` say so).
- TOTP codes are accepted with `valid_window=1` and are not single-use, so an
  observed code is replayable for roughly 90 seconds. Standard for the library
  and bounded by the login limiter.
- Everything already listed under "Remaining gaps / operator responsibilities"
  below, notably DNS rebinding on outbound notifications and the in-process rate
  limiter being per-worker.

### Also cleaned

`pyflakes` over `app/ web/backend/ tests/` found 12 dead imports / variables,
including leftovers from the 0.4.36 feature removal. Three were read-only
`nonlocal` / `global` declarations — each checked individually to confirm it was
not an intended assignment silently landing in the wrong scope (the real
assignments live elsewhere in every case). Removed.

## Pass 9 — self_update.py, line by line (0.4.23, 2026-07-28)

Reviewed on its own because it is the only path that escalates a panel session to
host root: it drives the mounted docker socket and, on the preferred path, runs
`docker run --privileged --pid=host` + `nsenter -t 1` to execute `install.sh update`
in the host namespaces.

### Fixed

| Issue | Severity | Notes |
|-------|----------|-------|
| `_compose_env_flags` emitted `-e OCIBOT_MASTER_KEY=<value>` (and JWT secret, Postgres password) into the `docker run` argv, and `_run_cmd` logs the command it runs. **The key that derives the Fernet key for every stored OCI private key was written verbatim into the API log on every update**, and was visible in the host process table for the duration of the call. A log shipper, a support bundle or `docker logs ocibot-api` was enough to walk away with every tenant's private key. | **High** | Secrets are passed by NAME (`docker run -e KEY`), which makes the CLI read the value from its own environment — `_run_cmd` already forwards `os.environ`. The log line is redacted as well. Same class as the `sync_db_password` fix in pass 7, in a place that pass did not reach. |
| `web/.env` — master key, JWT secret, DB password — was copied to `/tmp/ocibot.env.backup.<pid>` before `git reset --hard`, and unlinked **only on the success path**. Every early return after a failed reset, and any failure during restore, left it in a world-readable directory indefinitely. `copy2` also preserved a possibly-permissive source mode. | Medium-High | Held in memory for the duration of the reset instead; no temp file is created at all. Mirrors the pass-4 decision to keep decrypted OCI keys out of the temp directory. If the file has to be recreated it is written 0600. |
| `log_tail` is persisted and returned by `GET /api/admin/update`, and is assembled from raw command output that can echo an interpolated value back. | Medium | `_append_log` redacts on the way in. |

### Reviewed and found correct

`subprocess` is always invoked with a list argv and never `shell=True`. The two
places that do build shell strings (`_write_restart_script`, `_detach_host_install_sh`)
put every interpolated value through `_sh_quote`, and those values come from env /
`/proc/self/mountinfo`, not from HTTP input. `_repo()` / `_branch()` are validated
against strict allowlist regexes before being placed in the GitHub URL. Concurrency
is handled by a process lock plus a `SELECT … FOR UPDATE` on the status row with
`populate_existing`, because the API runs multiple processes. All three endpoints
require `get_admin_user`, and apply is audit-logged.

### Accepted risk — no integrity verification of the fetched code

The updater trusts TLS and GitHub: `git fetch` + `git reset --hard origin/<branch>`,
with no commit-signature check, and the SHA the GitHub API reported at check time is
never compared against what git actually checked out. Anyone able to serve different
content for the configured repo — a compromised repository account, a host that
trusts a rogue CA, or an operator-set `OCIBOT_UPDATE_REPO` pointing elsewhere — gets
host root on the next apply. Closing it properly needs signed tags plus a pinned
verification key. Mitigations today: `OCIBOT_UPDATE_ENABLED` defaults to 0 (the
supported installer opts in explicitly), the action is admin-only and audit-logged.
**Operators who do not use in-panel update should leave it disabled and update over
SSH.**

## Pass 8 — review of the 0.4.16–0.4.21 code (0.4.22, 2026-07-28)

Scope note: passes 4–7 covered the pre-0.4.16 codebase, including an adversarial
20-agent sweep. This pass targets the code added since — 副区 (secondary regions),
outbound-traffic tracking, and the read cache that came and went — which had never
been through one. The older surfaces were spot-checked rather than re-reviewed
(no raw SQL outside model-derived DDL, no shell=True / eval / pickle, no v-html or
innerHTML in the SPA, cookie flags and SSH command construction re-confirmed).

### Fixed

| Issue | Severity | Notes |
|-------|----------|-------|
| A 副区 row keeps a **copy** of its primary's credentials, but updating the primary's private key / fingerprint / OCIDs did not propagate. Every secondary region kept authenticating with the old key until it surfaced as a 401 — with no indication of why. | High (silent breakage) | Credential fields propagate to linked rows on update. |
| Shape resize, boot-volume growth and block-volume create/resize in a 副区 were refused by the Always-Free guard. A child inherits `account_tier` from its parent, and an empty tier means "hard cap", so a resize the user was deliberately paying for failed with 超过免费上限 — in a region with no free allowance at all, judged against a usage snapshot that only counts that one region. | Medium | All three now use `quota_guard.secondary_region_gate`, the same gate as launch; it still refuses outright if the tenant has free-only mode on. |
| The daily worker sweep paused between tenants only when `budget_monthly_usd > 0`. The 0.4.19 egress check calls Monitoring regardless of any budget, so an operator with no budgets set got every tenant's query back to back — the exact multi-account burst that pause exists to prevent. | Medium | Both checks now report whether they actually called Oracle, and the pause keys on that. |
| `subscribe_region` built the 副区 row name as `f"{parent.name} · {label}"` against a `VARCHAR(128)` column whose parent may already be 128 chars. SQLite silently overflows; **PostgreSQL raises**, and by then the Oracle subscription — which cannot be undone — has already been made, so the operator sees a 500 after an irreversible action. | Medium | Name truncated to fit; regression test with a 128-char parent. |
| `update_tenant` / `delete_tenant` evicted the cached OCI session **before** committing. A concurrent request could rebuild it from the uncommitted row and re-cache the old credentials, which then outlive the update (or, on delete, are never evicted again). | Low | Eviction moved strictly after the commit, for the row and its 副区 children. |
| Entering the instances page with `?tenant=` fetched twice concurrently: assigning `tenantId` queues the watcher, Vue flushes it on nextTick — inside the initial load's `await`. | Low (0.4.20 only) | Moot after the 0.4.21 revert to manual refresh; noted so it is not reintroduced. |

### Reviewed and found correct

Ownership is enforced before every tenant-scoped read and write, and cross-user
access answers 404 rather than 403 (`tests/test_user_isolation.py`, with both users
deliberately pointed at the same Oracle tenancy OCID — the case where a leak would
be plausible, since rows are then distinguishable only by their own uuid). The
region-subscription endpoint requires an explicit confirmation, is audit-logged, is
idempotent against a repeat click, and cannot fire a second `CreateRegionSubscription`
while Oracle still reports the new subscription as pending. `region_pair` fails
towards "home region" so an unreadable region lookup cannot block every launch, with
the tenant row's own parent link as an independent second signal. Egress is tracked
for visibility only: `validate_launch_against_quota` never reads it and its bucket is
soft, so it cannot make an unrelated launch look blocked.

### Not covered by this pass

The self-update path (`self_update.py`, which drives the mounted docker socket) and
the WebSSH terminal were reviewed in passes 4–7 and only spot-checked here. The two
gaps recorded below — DNS rebinding on outbound notifications, and the per-process
rate limiter — are unchanged.

## Pass 4 — full-codebase bug + security review (0.4.14, 2026-07-26)

Reviewed every router, the worker, the shared OCI layer entry points, the SPA and
the deployment config. Findings that were real and are now fixed:

### Security

| Issue | Severity | Fix |
|-------|----------|-----|
| `run.py` passed `proxy_headers=True, forwarded_allow_ips="*"` to uvicorn. Its ProxyHeadersMiddleware then **overwrote `scope["client"]` from `X-Forwarded-For` for every peer**, so `request.client.host` — the login/register rate-limit bucket key — was attacker-controlled. A fresh forged IP per request defeated the throttle entirely, making password brute-force unlimited even though `OCIBOT_TRUST_PROXY` defaults to 0. | **Critical** | Proxy headers are honoured only when `OCIBOT_TRUST_PROXY=1`, and only from `OCIBOT_FORWARDED_ALLOW_IPS` (loopback by default). |
| The WebSSH WebSocket authenticated by cookie but never checked `Origin`. CORS does not apply to WebSockets, so with `OCIBOT_COOKIE_SAMESITE=none` (a supported setting) any site the victim visited could open a shell on their instances (CSWSH). | High | `websocket_origin_allowed()` rejects cross-site handshakes before `accept()`; same-origin and explicitly allowlisted CORS origins pass, absent `Origin` (non-browser clients) still allowed. |
| `OCIBOT_CORS_ORIGINS=*` combined with cookie credentials makes Starlette *reflect* the caller's Origin plus `Allow-Credentials: true` — any website could read the API as the logged-in user. | High | A literal `*` is dropped from the origin list and logged loudly at startup. |
| Backup import and object-storage upload called `await file.read()` (whole body into one bytes object) *before* any size check, and did blocking work inside `async def` — memory pressure plus event-loop stalls for every other request on the worker. | High | Bounded chunked read (`web/backend/uploads.py`), both routes converted to sync/threadpool handlers, plus a 32MB request-body ceiling middleware so oversized bodies never reach the disk spool. |
| SSRF address filter missed several non-public ranges: `0.0.0.0/8` (only `0.0.0.0` itself was caught), carrier-grade NAT `100.64.0.0/10`, `192.0.0.0/24`, `240.0.0.0/4`, benchmarking/TEST-NET, and IPv6 forms that translate to IPv4 — **NAT64 `64:ff9b::/96`** and **6to4 `2002::/16`**, e.g. `64:ff9b::a9fe:a9fe` reaching cloud metadata. | Medium | Explicit CIDR list plus embedded-IPv4 unwrapping for mapped/NAT64/6to4; service-port blocklist widened. |
| No HSTS / COOP / CORP headers. | Low | Added (HSTS gated on `OCIBOT_COOKIE_SECURE` so a plain-HTTP deploy is not locked out). |
| `docker-compose.yml` defaulted `OCIBOT_UPDATE_ENABLED=1` while mounting `docker.sock`; applying an update runs a helper container and may `nsenter` the host namespace, so an admin session is effectively host root. | Medium | Default flipped to opt-in `0`. `scripts/install.sh` still sets `1` explicitly, so the supported install keeps one-click update. Added `no-new-privileges` and an `OCIBOT_BIND` knob. |
| Short/low-entropy `OCIBOT_MASTER_KEY` accepted silently by default; it is stretched with a single unsalted SHA-256 into the Fernet key, so a weak value is brute-forceable offline against a stolen DB. | Medium | Startup now warns with specific reasons (`weak_secret_reasons()`); the hard fail under `OCIBOT_REQUIRE_SECURE_SECRETS=1` is unchanged. **The derivation itself was deliberately left alone — changing it would make every stored private key undecryptable.** |
| Login `redirect` query param was passed straight to `router.replace()`. | Low | Only single-slash-rooted paths are followed. |

### Correctness

- `POST /api/jobs/capacity` validated `fallback_configs` against the free-tier
  quota and then built the row **without them**, so the worker (which reads
  `job.fallback_configs` to rotate AD × config) only ever tried the primary
  config. Field added to the schema, validation shared with the launch wizard via
  `normalize_fallback_configs()`, and now persisted.
- Power schedules could **fire twice**. The "already ran today" claim was a bare
  `db.flush()`, invisible to other connections until commit, so a second worker
  ticking in the same minute re-fired the same STOP/START. Now a conditional
  `UPDATE` committed before any OCI call. (On SQLite the old code instead hit
  `database is locked` and silently skipped the schedule.)
- `POST /tenants/{id}/launch` wrapped the quota guard in `except HTTPException:
  raise` with no other handler, so a quota-read failure surfaced as an unhandled
  500. Now 502 with the cause, matching the `/jobs/capacity` path.
- `prepare_launch_network()` derived `for_retry` from `auth_mode == "key"` instead
  of the caller's actual retry flag, validating plain launches under retry-only
  rules. Threaded through properly.
- Backup import could 500 on a hostile `password_expiry_days` (out-of-range int
  overflowing the column). Clamped.
- `AdminView.vue` declared its log-translation table as `[RegExp, string][]` while
  the last entry holds a callback — `vue-tsc` errors and `rep` narrowed to `never`
  in the callable branch. Type widened (vite never typechecked, so this was latent).

### Reviewed and found already correct

Per-endpoint ownership checks (`owner_id == current_user.id`) are present on every
tenant/job/instance/notification/audit route; JWT alg is pinned with a
`token_version` revocation counter; TOTP requires verification before enable;
cloud-init writes user scripts via a YAML block scalar so arbitrary content cannot
break out; capacity retries keep the durable committed lease, 60s floor, 429
backoff and `NoneRetryStrategy` on `LaunchInstance`; no `v-html`/`innerHTML`
anywhere in the SPA and no token in `localStorage`.

### Pass 6 — remaining deferred items closed (0.4.14)

- **SSH host key verification.** Previously `known_hosts=None` everywhere, i.e.
  none at all. Now trust-on-first-use, keyed on the **instance OCID** so routine
  IP rotation (the original reason for skipping it) is not mistaken for an
  attack. The probe uses `asyncssh.get_server_host_key()`, which performs only
  the key exchange — the ordering is the security property: verifying after
  `connect()` would already have handed the credentials to an impostor. The
  authenticated connection then pins the verified key via `known_hosts=([key],
  [], [])`. A legitimate rebuild is handled by
  `DELETE /api/tenants/{tid}/instances/{iid}/host-key` and a button in the UI.
  Mismatches are audited (`webssh.hostkey_mismatch`).
- **Quota guard no longer fails open.** `get_free_quota_usage` now reports
  `read_incomplete` when a read that feeds a *cap* was partial — including
  `list_instances_tree`'s per-compartment failures (previously swallowed) and the
  `errors` list on the volume responses (previously unread). API paths return 503;
  the worker defers the attempt **without consuming one**, so a transient blip
  cannot kill a long-running capacity job. Paid accounts are unaffected.
  Note: gating on "any notes present" would be wrong — the object-storage
  estimator writes notes on success too (`仅统计前 50/N 个存储桶`).
- **Self-update concurrency.** `threading.Lock` cannot exclude a second API
  worker process; the status row is now locked with `SELECT ... FOR UPDATE` in the
  same transaction that sets `running`, and `fetch_remote_head()` moved out of the
  critical section.

### Pass 7 — full-codebase sweep (0.4.14)

20 agents across 10 dimensions, each finding adversarially verified: 32 confirmed,
11 refuted. The newly written security code from passes 4–6 was treated as prime
suspect, and three of the four most serious findings were in it.

**Highest impact — a control that was believed complete but was not:**
`/api/backup/import`'s "5MB uncompressed" cap trusted `ZipInfo.file_size`, which
comes from the central directory and is attacker-controlled; pyzipper never
cross-checks it. `zf.read()` inflates in 1 GiB chunks and only *then* truncates to
`file_size`, so a 398KB upload declaring 1KB peaked at **830MB** of allocation
(reproduced; 11.2MB after the fix, which bounds the decompression itself via
`zf.open(member).read(LIMIT+1)`). Any authenticated user could reach it, and the
resulting `BadZipFile` was reported as a wrong-password 400, hiding the attack.

**In the new code:**
- The worker's fail-closed quota check was advisory only: `check_launch_quota` took
  its *own* snapshot, and a failed read becomes `{"read_incomplete": True}` with no
  usage keys, which the validators read as "full quota free". The pre-read is now
  passed in, so exactly one read decides — also halving OCI enumeration per attempt.
- `_apply_job` wrote `OCIBOT_GIT_SHA` into `os.environ`, so the worker that served
  the apply reported the *target* commit as the running build and skipped the
  failed-update reconciliation added in pass 5.
- `sync_db_password` re-parsed `web/.env` with `cut`, which does not match dotenv
  semantics (quotes, CR, inline comments) and could therefore set a password
  differing from the one api/worker use. It now reads what compose actually
  interpolated, and passes the statement over stdin so the password never appears
  in a process command line.
- The storage/boot-volume guards hardcoded `free_only_mode=True`, which hard-capped
  **paid** tenants at 200GB, and ignored `read_incomplete`. Both corrected.
- An unreachable SSH port was reported as a host-key mismatch / possible MITM. A
  distinct `UNREACHABLE` verdict now reports connectivity instead — a false MITM
  warning trains users to dismiss the real one.
- Concurrent first-connect hit the host-key unique constraint and surfaced as an
  internal error (fixed in 7ad1a26).

**Elsewhere:** stop-then-launch race in the capacity claim; the one-active-job
check moved next to its INSERT; unbounded `instance_ids` and several unbounded
string fields; `init_db()` racing `create_all` across two workers; the daily-check
sweep holding a SQLite write transaction across network I/O; naive timestamps
serialized without an offset (every job time rendered shifted); uncapped
notification fan-out; an unevictable launch-meta cache; the registration
username-existence oracle; `estimate_object_storage_usage` missing `fields` (the
object-storage gauge always read 0); and the inline boot-volume hydration wait that
could pin a request thread for ~31 minutes.

**Verified sound, no change needed:** the 413 from `BodySizeLimitMiddleware` carries
the security headers and logs no ERROR records (`BodyTooLarge` never reaches
`ServerErrorMiddleware`); the `ssh_host_keys` upgrade path creates the table with
its unique constraint on a pre-existing database without touching existing rows.

### Known remaining gap

**DNS rebinding on outbound notifications.** `resolve_and_check_host()` validates
the resolved addresses, then httpx resolves again when connecting, so a hostname
whose DNS answer flips between the two can still be reached. Closing it properly
needs connect-time address pinning. Mitigations today: only authenticated users
can register targets, redirects are never followed, `trust_env=False`, and the
send path re-validates. Documented in `url_safety.resolve_and_check_host`.

## Pass 2 — web-only migration + hardening (2026-07-25)

The desktop (Tkinter) version was removed; `app/` now holds only the shared OCI
business layer that the web backend imports. Changes in this pass:

### OCI API compliance (capacity retry)
- **Retries are now fully worker-owned.** The launch API no longer performs an
  immediate `LaunchInstance` in-request; it only enqueues the job. This removes
  three real compliance violations that existed on the wizard's "capacity retry"
  path: (1) the in-request attempt raced the worker for the same tenant (possible
  double launch), (2) it left `next_run_at=now` so the worker fired attempt #2
  under the 60s floor, and (3) it treated a 429 as a permanent failure and tore
  the job/NSG down instead of backing off.
- **Durable, atomic, committed lease.** The worker claims a job with a conditional
  `UPDATE ... WHERE locked_until IS NULL OR < now` and commits *before* any OCI
  call, so per-tenant single-flight now holds across processes and survives a
  crash (a crashed worker's lease simply expires). Each attempt's result is
  committed per-attempt (was one transaction per whole tick → could roll back a
  completed `LaunchInstance`).
- **One active capacity job per tenant** is enforced at creation (launch wizard +
  `POST /jobs/capacity`).
- Boot-VPU post-adjustment now also runs on worker-side capacity success (was
  only applied by the removed in-request attempt).
- Untouched and verified correct: interval/attempt clamps (60/180/3600, cap 2000),
  429 exponential backoff+jitter, non-capacity immediate stop, `LaunchInstance`
  SDK auto-retry disabled while list/get use the default strategy.

### Auth / security
- Frontend migrated to **cookie-only** auth: the JWT is no longer stored in
  `localStorage` or sent as a Bearer header (XSS can no longer read it). The
  backend already set an HttpOnly cookie; `withCredentials` carries it.
- Cookie `Secure` / `SameSite` are configurable (`OCIBOT_COOKIE_SECURE`,
  `OCIBOT_COOKIE_SAMESITE`); `SameSite=Lax` default mitigates CSRF. `logout`
  clears the cookie with matching attributes.
- **Open registration now defaults to closed** after the first (admin) user.
- Startup logs a loud warning when running with built-in dev secrets.

### Correctness bugs fixed
- `backup` import: a top-level-list backup JSON hit `AttributeError` → 500 (the
  `.get` fallback was unreachable). Now handled.
- `instance_ops` add firewall rule: a 400 validation error was re-wrapped as 502
  by the broad `except`; added `except HTTPException: raise`.
- SPA catch-all returned `index.html` (HTML 200) for unknown `/api/*` paths; now
  returns JSON 404.
- Frontend: `BackupView` `:disabled` type errors; silent-failure handlers in
  Jobs / Settings / console got `try/catch`; `AccountView` no longer resets the
  tenant selection on refresh; root-password generator uses `crypto.getRandomValues`
  (was `Math.random`); `InstancesView` guards out-of-order loads; 401 preserves
  the redirect target; admin registration toggle reverts on save failure; the
  jobs auto-refresh polls only capacity jobs.

## Pass 1 — earlier fixes (kept)

1. **Launch parity**: create-instance prepares IPv6 (optional) + managed open NSG before LaunchInstance; cleans up NSG on hard failure; applies boot VPU after success.
2. **Backup password leak**: `/api/backup/export` is POST body only (no password query string).
3. **JWT lifetime**: default 12h.
4. **Worker ownership checks**: schedule/capacity jobs verify `tenant.owner_id == job.owner_id`.
5. **Firewall rule validation**: `FirewallRuleSpec.validate()` before OCI call.
6. **Backup import size cap**: 20MB.
7. **Production secret guard**: `OCIBOT_REQUIRE_SECURE_SECRETS=1` rejects default secrets.

## Remaining gaps / operator responsibilities

| Item | Severity | Notes |
|------|----------|-------|
| Dev default secrets | High if exposed | App warns at startup; set `OCIBOT_MASTER_KEY` / `OCIBOT_JWT_SECRET` (and optionally `OCIBOT_REQUIRE_SECURE_SECRETS=1`) before any network exposure. |
| HTTPS | Ops | Terminate TLS at a reverse proxy / Tunnel and set `OCIBOT_COOKIE_SECURE=1`. |
| Rate-limit store is in-process | Low | Login/register throttle is per-process; behind multiple workers add a reverse-proxy rate limit. |
| Root password tag visibility | Inherited | Returned once on create; hashed into cloud-init, not stored in plaintext. |
| Outbound webhook/SMTP targets | Mitigated (0.4.6) | User-supplied webhook/Bark/SMTP hosts are DNS-resolved and blocked if private/loopback/link-local/metadata; no redirect following; `trust_env=False`. |
| X-Forwarded-For trust | Mitigated (0.4.6) | Default `OCIBOT_TRUST_PROXY=0`. Enable only behind a proxy that overwrites client IP headers. |
| WebSSH query JWT | Fixed (0.4.6) | Query-string tokens removed; cookie or Authorization Bearer only. |
| First-admin race | Mitigated (0.4.6) | PostgreSQL advisory lock + re-count; unique username constraint remains. |

## Pass 3 — security hardening (0.4.6, 2026-07-26)

- SSRF guards: `web/backend/url_safety.py` used by notify webhook/Bark/SMTP validation and send path
- Login timing pad + untrusted proxy IP default
- Backup import inflate/tenant caps; ignore `owner_id` from archive
- Baseline security headers + SPA path `..` rejection
- GitHub self-update: repo/branch allowlist, no env proxy

## Security checklist before public deploy

- [ ] Strong `OCIBOT_MASTER_KEY`, `OCIBOT_JWT_SECRET` (optionally `OCIBOT_REQUIRE_SECURE_SECRETS=1`)
- [ ] `OCIBOT_ALLOW_OPEN_REGISTRATION=0` (default) — open only transiently when adding users
- [ ] HTTPS + `OCIBOT_COOKIE_SECURE=1`, bind API behind a reverse proxy
- [ ] If behind reverse proxy: `OCIBOT_TRUST_PROXY=1` and ensure proxy **overwrites** `X-Forwarded-For`
- [ ] Restrict `OCIBOT_CORS_ORIGINS`
- [ ] Leave `OCIBOT_ORIGIN_CHECK=1` (default). If writes 403 after a proxy change,
      add the public origin to `OCIBOT_CORS_ORIGINS` — do not disable the check
- [ ] Audit retention suits the disk: `OCIBOT_AUDIT_RETENTION_DAYS` / `OCIBOT_AUDIT_MAX_ROWS`
- [ ] Keep the worker on a private host only
- [ ] Admin self-update mounts docker.sock — treat admin compromise as host compromise; disable with `OCIBOT_UPDATE_ENABLED=0` if multi-admin untrusted

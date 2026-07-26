# Web audit notes

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
- [ ] Keep the worker on a private host only
- [ ] Admin self-update mounts docker.sock — treat admin compromise as host compromise; disable with `OCIBOT_UPDATE_ENABLED=0` if multi-admin untrusted

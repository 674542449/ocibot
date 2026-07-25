# Web audit notes

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
| Outbound webhook/SMTP targets | Low | Notification channels fetch user-supplied URLs (SSRF is inherent to the feature; keep the panel non-public). |

## Security checklist before public deploy

- [ ] Strong `OCIBOT_MASTER_KEY`, `OCIBOT_JWT_SECRET` (optionally `OCIBOT_REQUIRE_SECURE_SECRETS=1`)
- [ ] `OCIBOT_ALLOW_OPEN_REGISTRATION=0` (default) — open only transiently when adding users
- [ ] HTTPS + `OCIBOT_COOKIE_SECURE=1`, bind API behind a reverse proxy
- [ ] Restrict `OCIBOT_CORS_ORIGINS`
- [ ] Keep the worker on a private host only

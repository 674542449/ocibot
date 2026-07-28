# OCIBot — working notes

Self-hosted multi-tenant Oracle Cloud (OCI) management panel.
FastAPI backend (`web/backend`) + Vue 3 SPA (`web/frontend`) + background worker
(`web/backend/worker.py`) + shared OCI business layer (`app/`).

## Release rule — bump the version with every shipped change

`web/backend/config.py::Settings.app_version` and the newest `## X.Y.Z` heading in
`CHANGELOG.md` must be updated **in the same commit as the change itself**.

Why it matters: `/api/health` reporting `version` is the only way an operator
confirms an update actually landed — it is the first row of README's troubleshooting
table. Ten commits once shipped under one unchanged version, which made that check
useless and left the operator unable to tell whether `install.sh update` had worked.

`tests/test_version_bump.py` enforces the two agree, so forgetting fails the suite.

Bump the patch component for fixes and for feature batches within the current
minor line (this project has been doing `0.4.x`). Add a CHANGELOG section with what
changed under 功能 / 修复 / 维护 and the upgrade snippet.

## Verification expectations

- `python -m pytest tests -q` must stay green (uses `.venv/Scripts/python.exe` on
  this machine).
- Frontend changes: `npx vue-tsc --noEmit -p tsconfig.json` **and** `npm run build`
  from `web/frontend`. Note both only check types/syntax — they cannot catch runtime
  logic errors, so async/state logic needs to be reasoned through or exercised.
- There is no frontend test runner (no vitest, no `test` script in
  `web/frontend/package.json`), so frontend regressions are guarded by comments and
  careful review only.
- `tests/test_endpoint_smoke.py` drives every reachable endpoint against a stubbed
  OCI session and fails on any 5xx. Most OCI-facing routes wrap everything in
  `except Exception -> HTTPException(502, str(exc))`, so a plain coding error (a
  missing import, a renamed helper) shows up as a generic 502 and is invisible to
  unit tests that never call the route. Keep new endpoints covered there.

## Things that are deliberate — do not "fix" them

- Pages do not auto-fetch from Oracle on entry; the user clicks 刷新 / 加载配置.
  This is to keep OCI API call volume down (CHANGELOG 0.4.13). **Auto-loading was
  tried in 0.4.20 and reverted in 0.4.21** — even with a server-side read cache in
  front of it, spending request budget on page navigation competes with the capacity
  retry loop for the same rate limit, and OCI's usage terms are the operator's
  liability. Do not reintroduce it.
- `GET /api/instances` (all-tenant aggregate) returns 400 on purpose.
- `POST .../create-image` returns 403 on purpose.
- WebSSH host keys are keyed on the **instance OCID**, not the address, so routine
  public-IP rotation is not mistaken for a MITM.
- `known_hosts` pinning uses the key verified by a KEX-only probe *before*
  authenticating. Never reorder that: verifying after `connect()` would hand the
  credentials to an impostor.
- The quota guard's `check_launch_quota` is the single source of truth for free-tier
  limits. The UI pre-check calls it via `POST /tenants/{id}/launch-quota-check`
  rather than reimplementing the caps, so the two cannot drift.
- `OCIBOT_MASTER_KEY` is stretched with a single unsalted SHA-256 into the Fernet
  key. Changing that derivation makes every stored OCI private key undecryptable.

## Security review history

`web/AUDIT.md` records seven audit passes, what was fixed, and the gaps that are
knowingly accepted (notably DNS rebinding on outbound notifications, and the
in-process rate limiter being per-worker). Read it before reporting a finding.

# Web audit notes (2026-07-25)

## Fixed in this pass

1. **Launch parity**: create-instance now prepares IPv6 (optional) + managed open NSG before LaunchInstance; cleans up NSG on hard failure; applies boot VPU after success (Always Free ignore-at-launch behavior).
2. **Backup password leak**: `/api/backup/export` is POST body only (no password query string).
3. **JWT lifetime**: default 12h (was 7 days).
4. **Worker ownership checks**: schedule/capacity jobs verify `tenant.owner_id == job.owner_id`.
5. **Firewall rule validation**: `FirewallRuleSpec.validate()` before OCI call.
6. **Backup import size cap**: 20MB.
7. **Production secret guard**: `OCIBOT_REQUIRE_SECURE_SECRETS=1` rejects default secrets.

## Remaining gaps (non-blocking for core use)

| Item | Severity | Notes |
|------|----------|-------|
| Full billing / daily cost charts | Low product gap | Desktop README mentions billing curves; web has account tier + limits only. No dedicated usage-api wrapper in `oci_client` beyond account status. |
| Token in `localStorage` | Medium | XSS can steal JWT. Prefer HttpOnly cookie for production multi-user. |
| Open registration default `true` | Medium | Set `OCIBOT_ALLOW_OPEN_REGISTRATION=false` after first admin. |
| Dev default secrets | High if exposed | Change `OCIBOT_MASTER_KEY` / `OCIBOT_JWT_SECRET` before any network exposure. |
| No rate limit on login/register | Medium | Add reverse-proxy rate limit or app-level throttle for public deploy. |
| No audit log UI | Low | Model exists; write path not fully wired for every action. |
| Root password tag visibility | Inherited | Desktop also stores password in freeform tag; web returns password once on create. |
| HTTPS | Ops | Terminate TLS at reverse proxy / Tunnel. |

## Security checklist before public deploy

- [ ] Strong `OCIBOT_MASTER_KEY`, `OCIBOT_JWT_SECRET`
- [ ] `OCIBOT_ALLOW_OPEN_REGISTRATION=false`
- [ ] Optional `OCIBOT_REQUIRE_SECURE_SECRETS=1`
- [ ] HTTPS + bind API to localhost behind reverse proxy
- [ ] Restrict CORS origins
- [ ] Keep worker on private host only

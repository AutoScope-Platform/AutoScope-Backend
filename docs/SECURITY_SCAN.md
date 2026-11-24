# Security scan of staging (OWASP ZAP), 24 November 2026

An automated scan, not a penetration test: an outside firm still tests before launch
(docs/ROADMAP.md, item 19). Rerun it with `bash /opt/autoscope/code/deploy/vm/zap_scan.sh` on
the staging VM (see the script for what each pass does).

## What was scanned

| Pass | Against | Requests |
| --- | --- | --- |
| Baseline (passive): headers, cookies, information leaks | The live staging site | 6 pages |
| Active API scan, not signed in: injection (SQL, OS command, path traversal), XSS, SSRF... on every endpoint in the OpenAPI spec | The live staging API | ~34,000 |
| Active API scan signed in as a customer, a dealer and the Super admin | A throwaway copy of the API with demo data (deleted afterwards), rate limits off | ~102,000 |

## Results

- **No high-risk findings.** No injection, XSS, path traversal or data leak was found.
- **No server errors.** Not one request out of about 136,000 made the API fail (no 500s,
  no stack traces); bad input was refused with 400/422, other roles' data with 403/404.
- **Medium and low warnings were all missing HTTP security headers.** Fixed: the API now sends
  them itself (`app/core/headers.py`), so production gets them too, not only staging:

| Warning | Fix |
| --- | --- |
| Content-Security-Policy not set (medium) | `default-src 'none'` on API answers; a policy that lets Swagger/ReDoc load on `/docs`, `/redoc` |
| Missing anti-clickjacking header (medium) | `X-Frame-Options: DENY` and `frame-ancestors 'none'` |
| X-Content-Type-Options missing (low, signed-in scans) | `nosniff` on every answer |
| Cross-Origin-Resource-Policy / Opener-Policy missing (low) | `same-origin` (`cross-origin` for signed file links the admin dashboard shows as images) |
| Permissions-Policy not set (low) | camera, microphone, geolocation and payment switched off |
| Cache-control directives (informational) | `Cache-Control: no-store` unless a route sets its own: no cache keeps personal data |
| Proxy named in the `Via` header | Removed in Caddy (staging) |

**Rescan after the fixes** (same day): the API scan has **no warnings at all** (120 checks
passed, no server errors). What remains is on the `/docs` page only, listed below.

## Accepted, with reasons

| Warning | Why it stays |
| --- | --- |
| Subresource Integrity missing, scripts from another domain (`/docs`) | Swagger UI and ReDoc load from the jsDelivr CDN (FastAPI's default). The pages show public API documentation only. For production, consider turning `/docs` off or self-hosting those files |
| CSP allows `unsafe-inline` scripts (`/docs`, `/redoc`) | FastAPI starts Swagger UI and ReDoc with an inline script. The page takes no user input to inject into; the strict policy covers every API answer. Turning `/docs` off in production removes this too |
| Cross-Origin-Embedder-Policy missing (`/docs`) | Would stop the docs pages loading their CDN files; no benefit for a JSON API |
| `text/csv` from `/admin/audit/export` | Intended: the audit log export is a CSV file. Cells are already protected against spreadsheet formula injection |

## Found along the way (not by ZAP)

- Uploads failed on staging (the uploads folder belonged to root): fixed, see `Dockerfile`.
- Changes to the Caddyfile never reached staging (a running Caddy kept the old copy): the deploy
  now recreates Caddy.

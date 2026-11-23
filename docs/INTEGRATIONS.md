# Integrations and keys

Everything external sits behind a driver chosen by an environment variable. The default is a
**mock** that works offline, so the whole app can be demoed with no accounts. Mocks are
refused when `ENV=production`.

| Service | Variable to switch | Real driver | Keys you must provide | Status |
| --- | --- | --- | --- | --- |
| Payments | `PAYMENT_DRIVER=paystack` | Paystack | `PAYSTACK_SECRET_KEY`, `PAYSTACK_PUBLIC_KEY` (start with *test* keys); set the webhook URL in the Paystack dashboard to `https://<api>/api/v1/payments/webhooks/paystack` | Written, **not tested against live Paystack** |
| SMS (OTP, alerts) | `SMS_DRIVER=termii` | Termii | `TERMII_API_KEY`, `TERMII_SENDER_ID` (sender IDs need approval in Nigeria: apply early), `TERMII_BASE_URL` (each account has its own API address, shown on the Termii dashboard: currently `https://v4.api.termii.com`). OTPs use the `dnd` route (`TERMII_CHANNEL=dnd`, the default), which Termii support must switch on for the account; `generic` does not reach numbers on Do-Not-Disturb | Written, untested live |
| E-mail | `EMAIL_DRIVER=smtp` | any SMTP | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM` | Written, untested live |
| Push | `PUSH_DRIVER=fcm` | Firebase Cloud Messaging | `FCM_SERVICE_ACCOUNT_JSON` (the service-account JSON, as one line) | Written, untested live |
| File storage (staging) | `STORAGE_DRIVER=s3` | Neon Object Storage (beta), or any S3-compatible service (Cloudflare R2, AWS S3) | `S3_ENDPOINT_URL`, `S3_REGION`, `S3_BUCKET`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`. Neon: see "Neon Object Storage" below | Driver set up for Neon (path-style, SigV4) and tested offline, **not yet against a real bucket** |
| File storage (Azure) | `STORAGE_DRIVER=azure` | Azure Blob Storage | `AZURE_STORAGE_CONNECTION_STRING` (with AccountKey), `AZURE_STORAGE_CONTAINER` | Written and tested against the Azure SDK offline (signed links, upload/download/delete with a stand-in client), **not yet against a real storage account**. Setup: docs/AZURE.md |
| VIN decoding | `VIN_DRIVER=nhtsa` | NHTSA vPIC (free, no key) | none | Written |
| Google / Apple sign-in | `SOCIAL_DRIVER=live` | Google ID tokens (google-auth), Apple ID tokens (Apple's public keys) | `GOOGLE_CLIENT_IDS`: the iOS, Android and web OAuth client ids, comma-separated. They live in the Google Cloud project behind the Firebase project (Firebase console: Authentication, enable Google; then Google Cloud console: APIs & Services, Credentials). The OAuth consent screen needs the app name, logo, support e-mail and a privacy policy link. `APPLE_CLIENT_IDS`: the iOS bundle id (add the Services id if web sign-in is added); needs an Apple Developer account. No secrets: we only verify tokens | Written and unit-tested with signed test tokens, **not yet tried with real Google/Apple accounts** |
| KYC / ID check | `KYC_DRIVER=youverify` | **Youverify** (chosen by the client) | `YOUVERIFY_API_KEY` (dashboard: API & Webhooks, create one in Sandbox mode first), `YOUVERIFY_BASE_URL` (sandbox by default; live: `https://api.youverify.co`, production refuses the sandbox) | Written: NIN / driver's licence and CAC number lookups with name matching, tested offline, **not yet against the Youverify sandbox**. Youverify cannot check document photos from the backend, so the uploaded documents go to the admin and the numbers given in `PUT /me/kyc-details` are checked. Sandbox test numbers: NIN `12345678901`, licence `ABC1234567890` |
| Maps / geocoding | not needed yet | | Distances use PostGIS; add Google/Mapbox only if the client wants address autocomplete or road routing | n/a |

## Mock behaviour (for demos and tests)
- OTP code is always `123456` (`MOCK_OTP_CODE`).
- Mock payment "charges" instantly; webhooks are signed with a fixed demo secret.
- Mock KYC passes unless the uploaded file name contains `blurred` or `address`. An ID number
  starting with `000` and the CAC number `RC0000000` come back "not found".
- Mock social sign-in accepts any `id_token` shaped `mock:<subject>:<email>:<name>` (e.g.
  `mock:g-1:ada@gmail.com:Ada Obi`), so the apps can build the Google/Apple screens before the
  real client ids exist.
- Uploads are checked by their content, not the `Content-Type` header: test files must be
  real JPEG/PNG/WebP/PDF bytes.
- Sent SMS/e-mail/push are kept in `app/integrations/mock.OUTBOX` (tests read it).

## Neon Object Storage (staging files)
Neon keeps files next to the database, reached through the S3 API (beta). Limits that matter:
- Only some regions have it: **create the Neon project in AWS Europe (Frankfurt),
  `aws-eu-central-1`** (the others are US East and Singapore). London does not have it.
- Free plan: 5 GB of files per project. Files belong to a branch: use the staging branch.
- Path-style addresses and SigV4 only (the driver is set up for this).

Setup:
1. Neon console (or `neon buckets create autoscope-uploads`): create a **private** bucket on
   the staging branch.
2. Get the branch's storage endpoint: Neon API
   `GET /api/v2/projects/{project_id}/branches/{branch_id}/storage` returns `s3_endpoint` and
   `region`. Set them as `S3_ENDPOINT_URL` and `S3_REGION`.
3. Create a credential with `storage:read` and `storage:write`
   (`neon credentials create --scope storage:read --scope storage:write`). It is shown once:
   `token_id` is `S3_ACCESS_KEY_ID`, `s3_secret_access_key` is `S3_SECRET_ACCESS_KEY`.
4. `STORAGE_DRIVER=s3`, `S3_BUCKET=autoscope-uploads`. Upload a listing photo and open the
   returned link to check.

If Neon's storage is not available on the account, Cloudflare R2 works with the same settings
(`S3_REGION=auto`).

## Going live checklist
1. Neon: create the project **in Frankfurt** (see above), enable PostGIS (`CREATE EXTENSION postgis;` is in the migration).
   Use the **pooled** string as `DATABASE_URL` and the **direct** string as `MIGRATION_DATABASE_URL`.
2. Set `ENV=production`, a long random `JWT_SECRET`, an explicit `CORS_ORIGINS` (no `*`).
3. Switch each driver from the table above and fill its keys.
4. Run `alembic upgrade head`, then create the first Super admin (do **not** run `make seed` in production).
5. Point the Paystack webhook at the API and send a test charge.
6. Set `REDIS_URL` (rate limits across instances).

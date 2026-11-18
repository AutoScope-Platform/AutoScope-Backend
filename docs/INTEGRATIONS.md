# Integrations and keys

Everything external sits behind a driver chosen by an environment variable. The default is a
**mock** that works offline, so the whole app can be demoed with no accounts. Mocks are
refused when `ENV=production`.

| Service | Variable to switch | Real driver | Keys you must provide | Status |
| --- | --- | --- | --- | --- |
| Payments | `PAYMENT_DRIVER=paystack` | Paystack | `PAYSTACK_SECRET_KEY`, `PAYSTACK_PUBLIC_KEY` (start with *test* keys); set the webhook URL in the Paystack dashboard to `https://<api>/api/v1/payments/webhooks/paystack` | Written, **not tested against live Paystack** |
| SMS (OTP, alerts) | `SMS_DRIVER=termii` | Termii | `TERMII_API_KEY`, `TERMII_SENDER_ID` (sender IDs need approval in Nigeria: apply early) | Written, untested live |
| E-mail | `EMAIL_DRIVER=smtp` | any SMTP | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM` | Written, untested live |
| Push | `PUSH_DRIVER=fcm` | Firebase Cloud Messaging | `FCM_SERVICE_ACCOUNT_JSON` (the service-account JSON, as one line) | Written, untested live |
| File storage | `STORAGE_DRIVER=s3` | any S3-compatible | `S3_ENDPOINT_URL`, `S3_BUCKET`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_REGION` | Written, untested live. Check that Neon's object storage is S3-compatible for your plan, otherwise use Cloudflare R2 |
| File storage (Azure) | `STORAGE_DRIVER=azure` | Azure Blob Storage | `AZURE_STORAGE_CONNECTION_STRING` (with AccountKey), `AZURE_STORAGE_CONTAINER` | Written and tested against the Azure SDK offline (signed links, upload/download/delete with a stand-in client), **not yet against a real storage account**. Setup: docs/AZURE.md |
| VIN decoding | `VIN_DRIVER=nhtsa` | NHTSA vPIC (free, no key) | none | Written |
| Google / Apple sign-in | `SOCIAL_DRIVER=live` | Google ID tokens (google-auth), Apple ID tokens (Apple's public keys) | `GOOGLE_CLIENT_IDS`: the iOS, Android and web OAuth client ids from Google Cloud Console, comma-separated. `APPLE_CLIENT_IDS`: the iOS bundle id (add the Services id if web sign-in is added). No secrets: we only verify tokens | Written and unit-tested with signed test tokens, **not yet tried with real Google/Apple accounts** |
| KYC / ID check | `KYC_DRIVER` | **none chosen yet** | Pick a vendor (e.g. Smile ID, Dojah, Youverify) and add a driver in `app/integrations/` | Mock only |
| Maps / geocoding | not needed yet | | Distances use PostGIS; add Google/Mapbox only if the client wants address autocomplete or road routing | n/a |

## Mock behaviour (for demos and tests)
- OTP code is always `123456` (`MOCK_OTP_CODE`).
- Mock payment "charges" instantly; webhooks are signed with a fixed demo secret.
- Mock KYC passes unless the uploaded file name contains `blurred` or `address`.
- Mock social sign-in accepts any `id_token` shaped `mock:<subject>:<email>:<name>` (e.g.
  `mock:g-1:ada@gmail.com:Ada Obi`), so the apps can build the Google/Apple screens before the
  real client ids exist.
- Uploads are checked by their content, not the `Content-Type` header: test files must be
  real JPEG/PNG/WebP/PDF bytes.
- Sent SMS/e-mail/push are kept in `app/integrations/mock.OUTBOX` (tests read it).

## Going live checklist
1. Neon: create the project, enable PostGIS (`CREATE EXTENSION postgis;` is in the migration).
   Use the **pooled** string as `DATABASE_URL` and the **direct** string as `MIGRATION_DATABASE_URL`.
2. Set `ENV=production`, a long random `JWT_SECRET`, an explicit `CORS_ORIGINS` (no `*`).
3. Switch each driver from the table above and fill its keys.
4. Run `alembic upgrade head`, then create the first Super admin (do **not** run `make seed` in production).
5. Point the Paystack webhook at the API and send a test charge.
6. Set `REDIS_URL` (rate limits across instances).

# Sign-in flows for the mobile apps

Everything is under `/api/v1`. Send `X-AutoScope-Client: mobile-app` on every call (it is
shown in the audit log). Errors always look like `{"error": {"code", "message", "details"}}`:
switch on `code`, show `message`. Full request/response shapes are in Swagger (`/docs`).

On staging the SMS/e-mail drivers are mocks: every code is **123456**.

## Sign up with phone or e-mail (FR-1.1, FR-1.3)
1. `POST /auth/register` with `full_name`, `email` and/or `phone`, `password`, `roles`
   (primary role first), `accept_terms: true`, and `business_name` for business roles.
   Nigerian numbers may be typed as `0803...`, `803...` or `+234803...`; other countries
   need the `+` country code.
2. `POST /auth/otp/verify` with the same `identifier` and the code. Returns tokens: the
   person is signed in.
3. "Didn't get a code?": `POST /auth/otp/resend` (always answers 202).

## Sign in with password
`POST /auth/login` returns either `tokens`, or `requires_otp: true` with a `challenge_token`
(staff, and anyone who switched on 2FA). Then:
- `POST /auth/login/verify` with `challenge_token` and `code`.
- "Resend code": `POST /auth/login/resend` with `challenge_token`. `429 otp_cooldown`
  means wait 30 seconds. Tapping "Sign in" twice is safe: it reuses the code just sent.

## Sign in with Google or Apple (FR-1.2)
1. Run the provider's native sign-in and take its **ID token**.
2. `POST /auth/social` with `provider` (`google` | `apple`) and `id_token`
   (and `nonce` if you used one).
3. Returning user: same answer as `/auth/login` (tokens, or an OTP challenge).
4. New user: `422 signup_details_required`. `details.missing` lists what to ask for
   (`accept_terms`, and `business_name` for business roles); `details.email` and
   `details.name` prefill the form. Send the **same** `id_token` again with `accept_terms: true`,
   `roles`, and `business_name` / `full_name` / `city` as needed.
5. Apple gives the person's name to the app only on the very first sign-in: send it as
   `full_name` then.
   On Android, ask Google for the ID token with the **web** client id (`serverClientId` /
   `requestIdToken`); on iOS the iOS client id is fine. Both ids must be in the backend's
   `GOOGLE_CLIENT_IDS`, or the token is refused with `401 invalid_social_token`.
6. If an e-mail/password account already has the same verified e-mail, it is linked
   automatically and the person signs into that account.

Accounts created this way have `has_password: false` in `GET /me`. To add a password, use
"Forgot password". `POST /me/password` answers `400 no_password_set` for them.

Staging accepts mock tokens shaped `mock:<any-id>:<email>:<name>`, e.g.
`mock:g-1:ada@gmail.com:Ada Obi`, so these screens can be built before real Google/Apple
client ids are configured.

## Tokens
- The access token lasts 15 minutes. On `401 token_expired`, call `POST /auth/refresh` with
  the refresh token and store **both** new tokens (the old refresh token stops working).
- Refresh tokens rotate. Re-using an old one signs the person out everywhere, so never
  refresh in parallel from two places in the app.
- Sign out: `POST /auth/logout` with the refresh token, `DELETE /me/devices/{token}` for push,
  then drop both tokens locally (the access token stays valid until it expires).

## Passwords
- Forgot: `POST /auth/password/forgot` (always 202), then `POST /auth/password/reset` with the
  code and `new_password`. This signs out every device.
- Change: `POST /me/password` with `current_password` and `new_password`.
- Rule: at least 8 characters, with a letter and a number.

## Delete my account (NDPR/GDPR)
`DELETE /me` with a JSON body: `{"password": "..."}`, or `{"code": "..."}` after
`POST /me/erase/code` (Google/Apple accounts have no password). On Android/Retrofit a DELETE
with a body needs `@HTTP(method = "DELETE", path = "me", hasBody = true)`.

## Uploads
KYC documents, listing photos, claim evidence and chat attachments are checked by their
content, not by the `Content-Type` you send. Send real JPEG/PNG (and WebP or PDF where
allowed); anything else gets `400 invalid_file_type` / `unsupported_file_type`.

## Rate limits
`429 rate_limited` means wait a minute. Sign-in and sign-up are limited per IP; listing
creation, checkout and payments are limited per signed-in user.

## Business verification (KYC, FR-1.4)
Business accounts (dealer, parts, mechanic, haulage, insurer) verify before their listings go live:
1. `PUT /me/kyc-details` with `consent: true` (show a checkbox: "I agree to have these numbers
   checked against government records"), `id_type` (`nin` or `drivers_license`) and
   `id_number`, and `registration_number` (CAC, e.g. `RC1234567`). Send it **before** the last
   document. `422` explains a badly formed number.
2. `POST /me/kyc-documents` three times (`government_id`, `business_registration`, `licence`).
   When the third arrives, the numbers are checked and the submission goes to the admin.
3. `GET /me/kyc` shows progress: `missing_documents`, `status` (`incomplete`, `pending`,
   `approved`, `rejected`) and `rejection_reason`. Only the last 4 digits of the ID number are kept.

With the mock (local and demo), any ID number starting with `000` is "not found" and the rest pass. With the
Youverify sandbox, use NIN `11111111111` (Sarah Jane Doe) or licence `AAA00000AA00` (John Doe); the
account name must contain the same first and last name to pass. Licence numbers are at most 12 characters.

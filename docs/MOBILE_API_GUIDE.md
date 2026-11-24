# Mobile API guide

For the AutoScope app developers. It maps each part of the app to the API calls it needs,
in the order you make them. The **full reference** (every field, every error) is the
interactive docs at `/docs` (Swagger) on the running API; this guide tells you which calls to
make and what to watch out for. Sign-in, tokens, Google/Apple and KYC are covered in more
detail in [MOBILE_AUTH.md](MOBILE_AUTH.md).

## 1. Getting started

| | |
| --- | --- |
| API base | **Staging: `https://20-106-252-207.sslip.io/api/v1`** (switches off at 11pm each night, see below). Locally `http://localhost:8000/api/v1` |
| Swagger | `https://20-106-252-207.sslip.io/docs`. Click **Authorize** and paste an access token to try any call |
| OpenAPI file | `https://20-106-252-207.sslip.io/openapi.json`. Generate a typed client from it (e.g. openapi-generator, or `dio` + `retrofit` for Flutter) |
| Live events | `wss://20-106-252-207.sslip.io/api/v1/ws/events?token=<access token>` |

**Staging hours:** the staging server switches itself off at **11pm (Nigeria time)** to save cost
and is switched back on in the morning. If calls time out late at night, that's why: the data is
kept, and everything is back as soon as it's on.

**Demo accounts** (staging and local, after `make seed`). Every password is `AutoScope#2026`,
and every SMS/OTP code is `123456`.

| Email | Role |
| --- | --- |
| `kunle@demo.ng`, `chioma@demo.ng` | Vehicle owner (customer) |
| `lekki.autos@demo.ng` | Dealer |
| `ngozi.parts@demo.ng` | Parts provider |
| `ola.motors@demo.ng` | Mechanic |
| `fastlane@demo.ng` | Haulage provider |
| `cover@demo.ng` | Insurer |
| `adaora@autoscope.ng` | Super admin (admin dashboard only) |

Staging uses mocks or test modes for anything not yet live: no real money moves,
SMS is not sent (the code is always `123456`), Google/Apple accept a mock token
(see MOBILE_AUTH.md). Real services are switched on one by one; the UI does not change.

## 2. Rules every call follows

- **Auth:** send `Authorization: Bearer <access token>` on everything except sign-up,
  sign-in and public browsing. The access token lasts 15 minutes: on `401 token_expired`,
  call `POST /auth/refresh` once, store **both** new tokens, retry. Refresh tokens rotate.
- **Errors** always look like `{"error": {"code": "...", "message": "...", "details": {...}}}`.
  Show `message` to the user (it is written for people); branch on `code`, never on the text.
  `422` means a field is wrong: `details` says which.
- **Money** is an integer in **kobo** (`price_minor: 1500000` = ₦15,000) with a `currency`.
  Never use floats. Format as `amount / 100`.
- **IDs** are UUIDs. Human references (`ORD-80312`, `BKG-...`, `KYC-2423`) are for display and
  receipts.
- **Dates** are ISO 8601 in UTC (`2026-11-23T14:05:00Z`). Show them in the phone's time zone.
- **Lists:** search and directories use `page` / `page_size` and return `total`. Feeds that
  grow (messages, notifications, wallet history, activity) use `cursor` / `limit`: pass the
  `next_cursor` from the last page; it is `null` on the last page.
- **Money writes need an `Idempotency-Key` header**: `POST /checkout` and `POST /payments`.
  Make a new UUID per *attempt the user makes*, and reuse it when you retry the same request
  after a timeout. That way a flaky network never charges twice.
- **Uploads** are `multipart/form-data` with a `file` field. Files are checked by their
  content, not the extension: photos JPEG/PNG/WebP up to 8 MB, KYC JPEG/PNG/PDF up to 5 MB,
  video MP4/MOV/WebM up to 50 MB. Image links in responses expire after about 15 minutes, so
  fetch the object again rather than caching URLs.
- **Rate limits:** `429 rate_limited` means wait a minute and retry. Don't retry in a loop.

## 3. Accounts and profile

| Screen | Calls |
| --- | --- |
| Sign up / OTP / sign in / Google & Apple | See MOBILE_AUTH.md |
| Profile | `GET /me` (also returns roles and `kyc_status`), `PATCH /me` |
| Change password | `POST /me/password` |
| "Become a seller / mechanic / ..." | `POST /me/roles`, then KYC (below) |
| Two-step sign-in | `PUT /me/two-factor` |
| Delete my account | `POST /me/erase/code` (Google/Apple users), then `DELETE /me` with `{"password"}` or `{"code"}` |
| Sign out | `DELETE /me/devices/{token}` (stop push), then `POST /auth/logout` |

**Business verification (KYC).** Dealers, parts providers, mechanics, haulage providers and
insurers verify before their listings go live: `PUT /me/kyc-details` (NIN or driver's licence,
CAC number, consent), then `POST /me/kyc-documents` three times, then show progress from
`GET /me/kyc`. Details in MOBILE_AUTH.md, "Business verification".

## 4. Garage (my vehicles)

| Screen | Calls |
| --- | --- |
| My garage | `GET /me/vehicles` |
| Add a vehicle | `GET /vehicles/vin/{vin}` to prefill make/model/year, then `POST /me/vehicles` |
| Edit / remove | `PATCH` / `DELETE /me/vehicles/{vehicle_id}` |
| Service history | `GET /me/vehicles/{vehicle_id}/service-records`; add your own with `POST` on the same path. Finished mechanic jobs are added automatically |

## 5. Browse and search (vehicles, parts, services, insurance)

`GET /listings` is public (no token needed) and powers every marketplace tab.

- `type`: `vehicle`, `part`, `mechanic_service` or `insurance_product`.
- Filters: `q` (words in any order), `city`, `min_price` / `max_price`, `make`, `model`,
  `condition`, `year_min` / `year_max`.
- Parts that fit a car: `vehicle_id` (a garage vehicle), or `fits_make` / `fits_model` / `fits_year`.
- Near me: `lat`, `lng`, `radius_km` (default 25) with `sort=distance`.
- `sort`: `newest` (default), `price_asc`, `price_desc`, `distance`, `rating`, `relevance` (needs `q`).
- The response has `counts_by_type` for the tab badges (it ignores the `type` filter).

| Screen | Calls |
| --- | --- |
| Listing details | `GET /listings/{listing_id}` (counts a view) |
| Seller's reviews | `GET /users/{user_id}/reviews` |
| "For you" | `GET /me/recommendations` |
| Find a mechanic | `GET /mechanics` (`service`, `city`, `lat`/`lng`, `sort=rating|price|distance`) |
| Ask, offer, book an inspection or test drive | `POST /listings/{listing_id}/inquiries` with `kind` and, for an offer, `offer_minor` |
| My enquiries (buyer) | `GET /me/inquiries` |
| Report a listing, review or account | `POST /reports` |

## 6. Selling (dealers and parts providers)

| Screen | Calls |
| --- | --- |
| My listings | `GET /me/listings` |
| Create | `POST /listings` (goes to admin review, and only goes live once the seller's KYC is approved), then photos `POST /listings/{id}/images` and an optional video `POST /listings/{id}/video` |
| Edit / delete | `PATCH` / `DELETE /listings/{listing_id}`; photos `DELETE /listings/{id}/images/{image_id}` |
| Mark reserved / sold / live again | `PUT /listings/{listing_id}/status` |
| Enquiries and offers on my listing | `GET /listings/{listing_id}/inquiries`, answer with `POST /inquiries/{inquiry_id}/answer` |
| Delivery fees (parts) | `GET` / `PUT /me/delivery-settings` |
| Part requests from buyers | `GET /part-requests`, quote with `PUT /part-requests/{request_id}/quote` |
| Ship a paid order | `POST /orders/{order_id}/ship` |

## 7. Buying parts and vehicles

**Parts (cart):**
1. `PUT /cart/items` with `listing_id` and `quantity` (`0` removes it). `GET /cart` shows the
   cart grouped by seller, with each seller's delivery fee.
2. `POST /checkout` with an `Idempotency-Key`. You get **one order per seller**; each order
   has a `transaction_id`.
3. Pay each order: see section 12.
4. Track: `GET /orders/{order_id}`. When it arrives: `POST /orders/{order_id}/confirm-received`
   (this releases the seller's money). Before shipping, the buyer can `POST /orders/{order_id}/cancel`.

**A vehicle:** `POST /listings/{listing_id}/purchase` (at the list price, or an offer the
dealer accepted) gives an order with a `transaction_id`. Pay it, then confirm receipt as above.

**Can't find a part?** `POST /part-requests`, then `GET /me/part-requests` and
`GET /part-requests/{request_id}` to compare quotes (cheapest first).
`POST /part-requests/{request_id}/close` when done.

## 8. Mechanic bookings

**Customer:**
1. Pick a mechanic and service, then `GET /mechanics/{mechanic_id}/slots?date=2026-12-01` for free times.
2. `POST /bookings` with the service (`listing_id`), `mechanic_id`, `vehicle_id` and `scheduled_for`. It books
   at the service's fixed price; send `quote_requested: true` to ask the mechanic for a **quote** instead.
3. Watch `GET /bookings/{booking_id}`:
   - quote sent: `POST /bookings/{id}/quote/answer` (accept or decline)
   - mechanic suggested another time: `POST /bookings/{id}/proposed-time/answer`
   - accepted: pay the `transaction_id` (section 12)
4. Invoice: `GET /bookings/{booking_id}/invoice`. Cancel before work starts:
   `POST /bookings/{booking_id}/cancel` (refunded if paid, minus the mechanic's cancellation terms).

**Mechanic:** set hours with `PUT /me/availability`. For each request:
`POST /bookings/{id}/accept`, `/decline`, `PUT /bookings/{id}/quote` or
`PUT /bookings/{id}/proposed-time`. Once paid: `POST /bookings/{id}/start`, then `/complete`.

## 9. Towing and SOS

**Customer:**
1. `GET /haulage/estimate?pickup_lat=&pickup_lng=&drop_lat=&drop_lng=&vehicle_class=` shows the fare
   (`vehicle_class`: motorcycle, car, suv, van or truck).
2. `POST /haulage/jobs` with `pickup`, `drop` and `vehicle_class` (`sos: true` for emergencies, which go to
   providers first).
3. Live tracking: open `wss://<host>/api/v1/ws/jobs/{job_id}/track?token=<access token>`. It
   sends the current state straight away, then every status or position change. Fall back to
   polling `GET /haulage/jobs/{job_id}/tracking` every few seconds if the socket drops.
4. Pay the job's `transaction_id`. Cancel up to pickup: `POST /haulage/jobs/{job_id}/cancel`.

**Haulage provider:**
- Go online and report position every 10 to 30 seconds while working:
  `PUT /me/haulage/presence` with `available`, `latitude`, `longitude`.
- Offers: `GET /me/haulage/offers` (SOS first). `POST /haulage/offers/{offer_id}/accept`
  (the first provider to accept gets it) or `/decline`.
- Then `POST /haulage/jobs/{job_id}/start` → `/pickup` → `/complete`.

## 10. Insurance

**Customer:**
- One insurer: `POST /insurance/applications` for a product and a garage vehicle.
- Several insurers at once: `POST /insurance/quote-requests`, then compare with
  `GET /insurance/quote-requests/{quote_request_id}`.
- Track: `GET /insurance/applications` and `/{application_id}`. When an offer arrives:
  `POST /insurance/applications/{id}/answer`. Accepting gives a `transaction_id` to pay.
  Withdraw before paying: `POST /insurance/applications/{id}/cancel`.
- Policies: `GET /me/policies`, `GET /insurance/policies/{policy_id}`, the certificate
  `GET /insurance/policies/{policy_id}/certificate` (a printable page), and
  `POST /insurance/policies/{policy_id}/renew`.
- Claims: `POST /insurance/policies/{policy_id}/claims`, add evidence with
  `POST /insurance/claims/{claim_id}/documents`, track with `GET /insurance/claims`.

Insurance screens **must show the disclosure** that AutoScope is a facilitator and the policy
is underwritten by the named insurer. Show the `disclosure` field that comes on insurance
products, applications and policies, as given.

**Insurer:** the same lists with `as_insurer=true`. Send offers with
`PUT /insurance/applications/{id}/offer` or `POST .../decline`, and handle claims with
`POST /insurance/claims/{claim_id}/decision`.

## 11. Chat, notifications and live updates

- Conversations: `GET /threads`, badge `GET /threads/unread-count`, open or start one with
  `POST /threads` (it reuses an existing conversation about the same listing, booking or job).
- Messages: `GET /threads/{thread_id}/messages` (newest first, cursor),
  `POST /threads/{thread_id}/messages` with a `client_id` you generate. Resending with the same
  `client_id` never duplicates, so retry freely. Photos and PDFs go to
  `POST /threads/{thread_id}/attachments`. Mark as read with `POST /threads/{thread_id}/read`.
- Notifications inbox: `GET /me/notifications`, `POST /me/notifications/read`.
- **Push:** after sign-in, `POST /me/devices` with the FCM token and platform. Call it again
  when the token refreshes. On sign-out, `DELETE /me/devices/{token}`.
- **Live events (one socket for the whole app):** `wss://<host>/api/v1/ws/events?token=<access token>`.
  After `{"type": "ready"}` you receive:
  - `{"type": "message", ...}` for a new message in any conversation,
  - `{"type": "read", "thread_id": ...}` when the other person reads yours,
  - `{"type": "notification", "kind": "order.paid", "title", "body", "data"}` for each
    notification. Provider apps refresh their queues on these instead of polling.

  The socket only receives: send messages with the REST call. Reconnect with a fresh token
  after `401`/close, with a growing delay (1 s, 2 s, 5 s, then every 30 s).

## 12. Paying (orders, bookings, tows, insurance)

Everything paid has a `transaction_id`. Card details never touch the app or our servers:
Paystack's own page handles them.

1. `POST /payments` with `{"transaction_id": ..., "method": "card"}` and an `Idempotency-Key`.
   You get an `authorization_url` and a `reference`.
2. Open `authorization_url` in an in-app browser / WebView.
3. When it closes (or redirects back), call `POST /payments/{reference}/verify`. This is the
   source of truth: show success only if the returned `transaction.paid_at` is set (its
   `escrow_status` is then `held`). If the user closed early, verify anyway (they may have
   paid), and offer "Try again" if not.
4. The money is held in **escrow** until the buyer confirms receipt or the job is completed,
   then released to the provider automatically.

| Screen | Calls |
| --- | --- |
| My activity (orders, bookings, jobs, policies) | `GET /activity`; providers add `as_provider=true` |
| One transaction | `GET /transactions/{reference}` |
| Receipt | `GET /transactions/{reference}/receipt`, or the printable page `/receipt/print` |
| Something went wrong | `POST /transactions/{transaction_id}/dispute` (freezes the escrow), then `GET /disputes/{dispute_id}` and `POST /disputes/{dispute_id}/messages` |

**Providers' money:** `GET /wallet` (balance), `GET /wallet/ledger` (history),
`GET` / `PUT /me/bank-account` (where payouts go), `GET` / `PUT /me/cancellation-policy`.

## 13. Reviews

After a completed order, booking or job: `POST /reviews`. A provider replies with
`POST /reviews/{review_id}/reply`. Ratings and reviews show on `GET /users/{user_id}/reviews`.

## 14. Error codes worth handling

| Code | What to do |
| --- | --- |
| `token_expired` (401) | Refresh once and retry |
| `rate_limited` (429) | Wait and retry later; show a gentle message |
| `otp_cooldown` (429) | Disable "Resend code" for 30 seconds |
| `signup_details_required` (422) | Social sign-in: ask for the fields in `details.missing` |
| `kyc_under_review` (409) | Show "Your documents are being reviewed" |
| `idempotency_key_reused` (409) | You reused a key for a different request: make a new key |
| `idempotency_key_required` (400) | Add the `Idempotency-Key` header |
| `unsupported_file_type` (400) | The file isn't a real JPEG/PNG/WebP/PDF |
| Any other | Show `message` |

## 15. Testing on staging

- Payments use either the mock (checkout always succeeds) or Paystack's test mode (pay with
  Paystack's test card on its page). No real money moves on staging.
- KYC uses Youverify's sandbox: NIN `11111111111` passes (Sarah Jane Doe) and `00000000000`
  is "not found"; licence `AAA00000AA00` (John Doe). The account's name must match for a pass.
- The demo data has listings of every type, a paid car sale, a completed parts order and
  booking, and a dispute. There are no tows or insurance policies yet: create them from the app
  (or Swagger) with the demo customer and provider accounts.
- Found a bug or a missing field? Send the request, the response and the time (UTC).

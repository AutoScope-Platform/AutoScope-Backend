# Backend roadmap: from staging to production

The checklist for finishing the backend and going live. Tick items off in the same PR that
completes them. Last updated: 24 November 2026.

**Where we are:** every feature in the FSD/TSD is built and tested (CI green on `develop`).
Staging is live on the client's Azure VM at **https://20-106-252-207.sslip.io**
(Swagger at `/docs`; see [VM_STAGING.md](VM_STAGING.md)). The VM switches off at 11pm daily.
Firebase push, Paystack payments (test mode) and Youverify NIN/licence checks (sandbox) are
connected and tested; the other services run on stand-ins until their accounts are ready.

## Stage 1: things we can do now

- [x] 1. Invite the mobile developer to the repo (read access).
- [ ] 2. Automated security scan of staging (OWASP ZAP baseline); fix anything real it finds.
- [ ] 3. Load test on staging: many users browsing, ordering and booking at once.
- [x] 4. Nightly database backup on the staging VM (database and uploads at 10:30pm, newest 7 kept; restore tested).
- [ ] 5. *(Optional, needs the client's OK: small cost)* Auto-start the VM every morning.
- [ ] 6. Full test card payment through Paystack's checkout page (test card), confirmed by the backend.

## Stage 2: waiting on the client or third parties

These block production, not the mobile developer. As each arrives, switch it on in staging
(`/opt/autoscope/.env` on the VM; variables in [INTEGRATIONS.md](INTEGRATIONS.md)) and test it.

- [ ] 7. **Paystack:** Transfers enabled (paying sellers out of escrow), then live keys.
- [ ] 8. **Termii:** credit topped up (₦30 today), sender ID approved (none requested on the account yet), DND route on.
- [ ] 9. **Youverify:** company (CAC) verification enabled on the account, then a live key.
- [ ] 10. **E-mail:** a provider (Brevo, Resend...) and access to the domain's DNS.
- [ ] 11. **Google/Apple sign-in:** privacy policy link, app logo, support e-mail (Google consent screen); Apple Developer account; OAuth client ids.
- [ ] 12. **Decisions:** commission rate per category; which payments use escrow; insurance disclaimer approved (legal); launch categories; whether NIN/CAC become required at KYC.

## Stage 3: mobile integration

- [ ] 13. The mobile developer integrates the app ([MOBILE_API_GUIDE.md](MOBILE_API_GUIDE.md)).
- [ ] 14. Fix what they find; add what they need.
- [ ] 15. Confirm who connects the admin dashboard to the API.

## Stage 4: production

- [ ] 16. Choose where production runs (Azure Container Apps, recommended, via [AZURE.md](AZURE.md) and `scripts/azure_setup.sh`; or a larger VM) and get the budget approved.
- [ ] 17. Set it up: live keys in Key Vault, real domain (e.g. `api.autoscope.ng`), database backups, alerts, `CORS_ORIGINS`.
- [ ] 18. Create the first Super admin (never seed demo data in production).
- [ ] 19. Independent penetration test by an outside firm; fix the findings; retest.
- [ ] 20. Final load test on the production setup.
- [ ] 21. Release: merge `develop` into `main`, tag `v1.0`, deploy.
- [ ] 22. Launch, then watch errors and performance closely for the first weeks.

## Done so far (for reference)

- Phases 1–3 of the FSD/TSD built, 200+ automated tests, CI on every PR.
- Search load test: p95 under 350 ms at 100,000 listings (local).
- Security scans (Nov 2026): no known vulnerabilities in the 99 libraries; no high or medium issues in the code.
- Integrations: Termii driver (account address, DND route), Youverify KYC (NIN, licence, CAC), Firebase push key, Paystack test keys checked.
- Repo moved to `AutoScope-Platform/AutoScope-Backend`, `develop` is the default branch.
- Staging live on the client's VM, with demo data, tested end to end.
- Nightly staging backups. Setting them up found that uploads failed on staging (the folder
  belonged to root); fixed and tested with a real photo upload.

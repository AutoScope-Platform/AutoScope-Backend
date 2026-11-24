# Staging on the Azure virtual machine

Staging runs on one virtual machine the client already pays for: `autoscope-dev-vm` in the
resource group `autoscope-dev` (Ubuntu 24.04, size B2s, East US, fixed public IP). Everything
runs on it in Docker:

| Container | What it is |
| --- | --- |
| `caddy` | HTTPS in front of the API, with a free certificate that renews itself |
| `api` | The REST API and WebSockets (runs database migrations when it starts) |
| `worker` | Background jobs and the scheduler (SMS, e-mail, push, escrow release) |
| `db` | PostgreSQL 16 with PostGIS |
| `redis` | Queue, rate limits and live events |

The address is `https://<ip-with-dashes>.sslip.io`: sslip.io turns the fixed IP into a host
name so HTTPS works without buying a domain. Swap in `api-staging.<domain>` later by setting
`SITE_ADDRESS` (see "Using a real domain").

Production is meant for Azure Container Apps (docs/AZURE.md); this VM is staging only.

## Every day: the VM switches off at 11pm

To save cost, Azure switches the VM off at **11pm (West Africa time)**. While it is off, the API
is offline. To switch it on:

- **Portal:** portal.azure.com → **Virtual machines** → **autoscope-dev-vm** → **Start**.
- **Or from a computer with the Azure CLI:** `az vm start -g autoscope-dev -n autoscope-dev-vm`

About 1–2 minutes later the API answers again. Nothing else is needed: every container restarts
by itself, and the data (database, uploads) is kept on the VM's disk.

To change or remove the nightly shutdown: VM → **Auto-shutdown** (left menu).

## Deploying an update

From the repo root on a computer signed in to Azure (`az login`), with the change merged into
`develop` and checked out:

```bash
bash scripts/vm_deploy.sh
```

It switches the VM on if needed, uploads the **committed** code, rebuilds and restarts, and
waits until `/health/ready` answers. Uncommitted changes are not deployed.

The first run did the one-off setup: a `deploy` login on the VM for this computer's SSH key
(`~/.ssh/autoscope_vm`; the VM's own `azureuser` login is untouched), Docker, and the secrets.
Another computer runs the same command and gets its own key the same way.

## Where things are on the VM

| Path | What |
| --- | --- |
| `/opt/autoscope/code` | The deployed code (`DEPLOYED_VERSION` holds the commit) |
| `/opt/autoscope/.env` | Settings and secrets (database password, JWT secret, driver switches). Readable by the deploy user only. Never commit it |

Docker volumes hold the database (`pgdata`), uploads, Redis data and the HTTPS certificates.

## Looking at it

```bash
ssh -i ~/.ssh/autoscope_vm deploy@<ip>
cd /opt/autoscope/code/deploy/vm
docker compose ps                  # what is running
docker compose logs -f api         # API log (Ctrl+C to stop)
docker compose logs --tail 100 worker
docker compose restart api         # restart one service
```

## Switching on real services (Paystack, Termii, Youverify, Firebase)

Edit `/opt/autoscope/.env` on the VM: change the driver (e.g. `PAYMENT_DRIVER=paystack`) and add
its keys (see docs/INTEGRATIONS.md for each one's variables), then
`docker compose up -d` in `/opt/autoscope/code/deploy/vm`. Keys go **only** in that file.

## Using a real domain

Point a DNS `A` record (e.g. `api-staging.autoscope.ng`) at the VM's IP, then deploy with
`SITE_ADDRESS=api-staging.autoscope.ng bash scripts/vm_deploy.sh`. Caddy fetches the certificate
itself. Add the new address to the app and to `CORS_ORIGINS` if the admin dashboard calls it.

## Backups

Every night at **10:30pm (West Africa time)**, before the 11pm shutdown, the VM backs up the
database and the uploaded files (`deploy/vm/backup.sh`, run by the `autoscope-backup` systemd
timer that `scripts/vm_deploy.sh` installs). If the VM was off at 10:30pm, the backup runs as
soon as it is switched on. Each backup is checked as readable before it is kept, and the newest
**7** are kept.

| Path on the VM | What |
| --- | --- |
| `/opt/autoscope/backups/<date>_<time>/db.dump` | The whole database (`pg_dump` custom format) |
| `/opt/autoscope/backups/<date>_<time>/uploads.tar.gz` | The uploaded files |
| `/opt/autoscope/backup.log` | What each run did |

The backups sit on the VM's own disk: they cover mistakes and bad data, not losing the VM
itself. Production gets managed backups (docs/AZURE.md).

```bash
bash /opt/autoscope/code/deploy/vm/backup.sh      # back up now
systemctl list-timers autoscope-backup.timer       # when the next one runs
tail /opt/autoscope/backup.log                     # recent runs
```

**Restoring** (replaces everything in staging with the backup; in `/opt/autoscope/code/deploy/vm`):

```bash
B=/opt/autoscope/backups/2026-11-24_2230              # the backup to restore
docker compose stop api worker
docker compose exec -T db psql -U autoscope -d postgres \
  -c "drop database autoscope with (force)" -c "create database autoscope"
docker compose exec -T db pg_restore -U autoscope -d autoscope --no-owner < $B/db.dump
docker compose run --rm -T --no-deps -u root api sh -c \
  'rm -rf /data/uploads/* && tar -xzf - -C /data/uploads && chown -R app:app /data/uploads' < $B/uploads.tar.gz
docker compose start api worker
```

To try a backup without touching staging, restore `db.dump` into a scratch database
(`create database restore_test`, `pg_restore -d restore_test ...`, then drop it): this was
done when backups were set up, and every table came back with the same row count.

## Demo data

Loaded once with `SEED=1 bash scripts/vm_deploy.sh`. Demo logins: see docs/MOBILE_API_GUIDE.md
(password `AutoScope#2026`, code `123456`). To start over with an empty database:
`docker compose down -v` on the VM (**deletes all data**), then deploy again with `SEED=1`.

## If something is wrong

| Problem | What to do |
| --- | --- |
| The API doesn't answer | Is the VM on? (portal). Then `docker compose ps` on the VM |
| `/health/ready` says the database or Redis is down | `docker compose logs db` / `redis`, then `docker compose restart db` |
| HTTPS certificate errors | `docker compose logs caddy`: ports 80 and 443 must be open on the VM's network security group |
| The disk is full | `docker system prune -f` on the VM removes old images |

#!/usr/bin/env bash
# Deploys the committed code (HEAD) to the staging virtual machine on Azure and restarts it.
#
#   az login                         # once, with an account that can manage the VM
#   bash scripts/vm_deploy.sh        # from the repo root (Git Bash on Windows is fine)
#   SEED=1 bash scripts/vm_deploy.sh # first time only: also load the demo accounts and data
#
# The first run switches the VM on, creates a "deploy" login with this computer's SSH key
# (the VM's existing logins are untouched), installs Docker and creates the secrets on the
# VM. Later runs only upload the new code and restart. See docs/VM_STAGING.md.
set -euo pipefail

RG="${RG:-autoscope-dev}"
VM="${VM:-autoscope-dev-vm}"
DEPLOY_USER="${DEPLOY_USER:-deploy}"
KEY="${KEY:-$HOME/.ssh/autoscope_vm}"
APP_DIR=/opt/autoscope
CORS_ORIGINS="${CORS_ORIGINS:-https://autoscope-admin.netlify.app}"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok() { printf '   ok: %s\n' "$*"; }
fail() { printf '\n\033[31mSTOP: %s\033[0m\n' "$*" >&2; exit 1; }

[[ -f Dockerfile && -f deploy/vm/docker-compose.yml ]] || fail "run this from the repo root"
command -v az >/dev/null || PATH="/c/Program Files/Microsoft SDKs/Azure/CLI2/wbin:$PATH"
command -v az >/dev/null || fail "the Azure CLI (az) is not installed"
command -v ssh >/dev/null || fail "ssh is not installed (it comes with Git Bash)"
az account show >/dev/null 2>&1 || fail "not signed in to Azure: run 'az login' first"
[[ -z "$(git status --porcelain -- app migrations pyproject.toml uv.lock Dockerfile alembic.ini deploy)" ]] \
  || echo "   note: uncommitted changes are NOT deployed; this deploys the last commit ($(git rev-parse --short HEAD))"

step "Switching the VM on"
state=$(az vm get-instance-view -g "$RG" -n "$VM" --query "instanceView.statuses[?starts_with(code,'PowerState/')].code | [0]" -o tsv)
[[ "$state" == "PowerState/running" ]] || az vm start -g "$RG" -n "$VM" -o none
IP=$(az vm show -d -g "$RG" -n "$VM" --query publicIps -o tsv)
SITE="${SITE_ADDRESS:-${IP//./-}.sslip.io}"
ok "running at $IP ($SITE)"

step "Secure access (SSH key for this computer)"
if [[ ! -f "$KEY" ]]; then
  mkdir -p "$(dirname "$KEY")"
  ssh-keygen -t ed25519 -N "" -C "autoscope-deploy" -f "$KEY" -q
fi
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "$DEPLOY_USER@$IP")
if ! "${SSH[@]}" -o BatchMode=yes true 2>/dev/null; then
  az vm user update -g "$RG" -n "$VM" -u "$DEPLOY_USER" --ssh-key-value "$(cat "$KEY.pub")" -o none
  for _ in $(seq 1 12); do "${SSH[@]}" -o BatchMode=yes true 2>/dev/null && break; sleep 5; done
fi
"${SSH[@]}" -o BatchMode=yes true || fail "cannot reach the VM over SSH (port 22)"
ok "signed in to the VM as $DEPLOY_USER"

step "Docker on the VM"
"${SSH[@]}" "bash -s" <<'REMOTE'
set -euo pipefail
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose-v2 >/dev/null
fi
sudo systemctl enable --now docker >/dev/null 2>&1
sudo usermod -aG docker "$USER"
sudo mkdir -p /opt/autoscope && sudo chown "$USER" /opt/autoscope
docker --version | sed 's/^/   /'
REMOTE

step "Uploading the code ($(git rev-parse --short HEAD))"
git archive --format=tar HEAD | "${SSH[@]}" "rm -rf $APP_DIR/code.new && mkdir -p $APP_DIR/code.new && tar -x -C $APP_DIR/code.new \
  && rm -rf $APP_DIR/code && mv $APP_DIR/code.new $APP_DIR/code && echo $(git rev-parse --short HEAD) > $APP_DIR/code/DEPLOYED_VERSION"
ok "code in $APP_DIR/code"

step "Settings and secrets (created once on the VM, never printed)"
"${SSH[@]}" "SITE='$SITE' CORS='$CORS_ORIGINS' bash -s" <<'REMOTE'
set -euo pipefail
ENV_FILE=/opt/autoscope/.env
if [[ ! -f "$ENV_FILE" ]]; then
  umask 077
  cat > "$ENV_FILE" <<EOF
POSTGRES_PASSWORD=$(openssl rand -hex 24)
JWT_SECRET=$(openssl rand -base64 48 | tr -d '\n')
SITE_ADDRESS=$SITE
CORS_ORIGINS=$CORS
SMS_DRIVER=mock
EMAIL_DRIVER=mock
PUSH_DRIVER=mock
PAYMENT_DRIVER=mock
KYC_DRIVER=mock
SOCIAL_DRIVER=mock
DB_POOL_SIZE=5
DB_MAX_OVERFLOW=5
EOF
  echo "   created $ENV_FILE"
else
  sed -i "s|^SITE_ADDRESS=.*|SITE_ADDRESS=$SITE|" "$ENV_FILE"
  echo "   kept the existing $ENV_FILE"
fi
ln -sf "$ENV_FILE" /opt/autoscope/code/deploy/vm/.env
REMOTE

step "Building and starting (the first build takes a few minutes)"
"${SSH[@]}" "cd $APP_DIR/code/deploy/vm && sg docker -c 'docker compose up -d --build --remove-orphans' 2>&1 | tail -15"
# Each deploy replaces the code folder, but a running Caddy keeps reading the old copy of its
# Caddyfile. Recreate it so Caddyfile changes apply (its certificates live in a volume).
"${SSH[@]}" "cd $APP_DIR/code/deploy/vm && sg docker -c 'docker compose up -d --force-recreate --no-deps caddy' 2>&1 | tail -1"
# The uploads volume was first created owned by root, which the API (user "app") can't write.
"${SSH[@]}" "cd $APP_DIR/code/deploy/vm && sg docker -c 'docker compose exec -T -u root api chown app:app /data/uploads' </dev/null"

step "Waiting for the API"
for _ in $(seq 1 40); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "https://$SITE/health/ready" || true)
  [[ "$code" == 200 ]] && break
  sleep 6
done
if [[ "$code" != 200 ]]; then
  "${SSH[@]}" "cd $APP_DIR/code/deploy/vm && sg docker -c 'docker compose ps; docker compose logs --tail 30 api caddy'" || true
  fail "https://$SITE/health/ready answered '$code' (logs above)"
fi
ok "https://$SITE/health/ready answers 200"

step "Nightly backup (database and uploads, 10:30pm West Africa time)"
# A systemd timer, not cron: with Persistent=true a backup missed while the VM was off runs
# as soon as it is switched on again.
"${SSH[@]}" "bash -s" <<'REMOTE'
set -euo pipefail
sudo tee /etc/systemd/system/autoscope-backup.service >/dev/null <<EOF
[Unit]
Description=AutoScope staging backup (database and uploads)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
User=$USER
ExecStart=/bin/bash /opt/autoscope/code/deploy/vm/backup.sh
StandardOutput=append:/opt/autoscope/backup.log
StandardError=append:/opt/autoscope/backup.log
EOF
sudo tee /etc/systemd/system/autoscope-backup.timer >/dev/null <<'EOF'
[Unit]
Description=AutoScope staging backup every night, before the 11pm shutdown

[Timer]
OnCalendar=*-*-* 22:30:00 Africa/Lagos
Persistent=true
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now autoscope-backup.timer >/dev/null 2>&1
echo "   next run: $(systemctl show autoscope-backup.timer -p NextElapseUSecRealtime --value)"
REMOTE

if [[ "${SEED:-0}" == "1" ]]; then
  step "Loading the demo accounts and data"
  "${SSH[@]}" "cd $APP_DIR/code/deploy/vm && sg docker -c 'docker compose exec -T api python -m app.seed'" 2>&1 | tail -3
fi

cat <<EOF

Done. Staging is live:
  API:      https://$SITE/api/v1
  Swagger:  https://$SITE/docs
  Health:   https://$SITE/health/ready
The VM switches itself off at 11pm. When it is switched on again, everything restarts by itself.
EOF

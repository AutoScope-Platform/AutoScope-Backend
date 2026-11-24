#!/usr/bin/env bash
# OWASP ZAP security scan of staging, run on the staging VM:
#
#   bash /opt/autoscope/code/deploy/vm/zap_scan.sh
#
# 1. Public:    baseline (passive) scan of the site, then an active scan of every API endpoint
#               in the OpenAPI spec, without signing in, against the live staging address.
# 2. Signed in: active scans as a customer, a dealer and the Super admin, against a throwaway
#               copy of the API (own database with demo data, rate limits off), so attack data
#               never lands in staging. The copy is deleted at the end.
#
# Reports (HTML and JSON) go to /opt/autoscope/zap. Takes about 25 minutes. Back up first:
# the public scan sends real requests to staging (bash deploy/vm/backup.sh).
# Results of the first run: docs/SECURITY_SCAN.md.
set -u
cd "$(dirname "$0")"
SITE="https://$(grep -E '^SITE_ADDRESS=' /opt/autoscope/.env | cut -d= -f2)"
W=/opt/autoscope/zap
IMG=ghcr.io/zaproxy/zaproxy:stable
mkdir -p $W && chmod 777 $W
docker pull -q $IMG </dev/null

summary() { grep -E "^(WARN|FAIL)|Total of|FAIL-NEW"; }

echo "== public baseline scan of $SITE"
docker run --rm -v $W:/zap/wrk:rw $IMG zap-baseline.py -t "$SITE/docs" -m 2 -I \
  -r baseline.html -J baseline.json </dev/null 2>&1 | summary
echo "== public API scan"
docker run --rm -v $W:/zap/wrk:rw $IMG zap-api-scan.py -t "$SITE/openapi.json" -f openapi -I \
  -z "-config scanner.maxScanDurationInMins=40 -config scanner.threadPerHost=3" \
  -r api.html -J api.json </dev/null 2>&1 | summary

P=(docker compose -p zapscan -f docker-compose.yml -f $W/scan.yml)
cat > $W/scan.yml <<'EOF'
services:
  api:
    environment:
      ACCESS_TOKEN_MINUTES: "240"
      ADMIN_SESSION_IDLE_MINUTES: "240"
      RATE_LIMIT_ENABLED: "false"
EOF
echo "== starting the throwaway copy"
"${P[@]}" up -d --no-build db redis api </dev/null 2>&1 | tail -1
for _ in $(seq 1 40); do
  "${P[@]}" exec -T api python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=3)" </dev/null 2>/dev/null && break
  sleep 5
done
"${P[@]}" exec -T api python -m app.seed </dev/null 2>&1 | tail -1

token() {  # signs in through the real login flow (codes are 123456 outside production)
  "${P[@]}" exec -T api python - "$1" </dev/null <<'PY'
import json, sys, urllib.request
def post(path, body):
    req = urllib.request.Request("http://127.0.0.1:8000/api/v1" + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=10))
out = post("/auth/login", {"identifier": sys.argv[1], "password": "AutoScope#2026"})
if out.get("requires_otp"):
    out = post("/auth/login/verify", {"challenge_token": out["challenge_token"], "code": "123456"})
print((out.get("tokens") or out)["access_token"])
PY
}

for who in customer:kunle@demo.ng dealer:lekki.autos@demo.ng admin:adaora@autoscope.ng; do
  role=${who%%:*}
  tok=$(token "${who#*:}" | tail -1)
  [[ ${#tok} -gt 50 ]] || { echo "== $role: could not sign in"; continue; }
  echo "== signed-in API scan as $role"
  docker run --rm --network zapscan_default -v $W:/zap/wrk:rw \
    -e ZAP_AUTH_HEADER=Authorization -e ZAP_AUTH_HEADER_VALUE="Bearer $tok" -e ZAP_AUTH_HEADER_SITE=api \
    $IMG zap-api-scan.py -t http://api:8000/openapi.json -f openapi -I \
    -z "-config scanner.maxScanDurationInMins=30 -config scanner.threadPerHost=4" \
    -r "auth_$role.html" -J "auth_$role.json" </dev/null 2>&1 | summary
done
echo "== server errors (500) in the throwaway copy: $("${P[@]}" logs api </dev/null 2>/dev/null | grep -c '" 500')"
"${P[@]}" down -v </dev/null >/dev/null 2>&1
echo "== done; reports in $W"

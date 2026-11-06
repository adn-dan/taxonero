#!/usr/bin/env bash
# Initialises the dev Immich: admin account + API key restricted to the permissions of the contract.
# Writes dev/.env.dev (ignored by git). Never run it against production.
set -euo pipefail

URL="${IMMICH_URL:-http://127.0.0.1:2284}"
EMAIL="dev@example.invalid"
PASS="dev-password"
cd "$(dirname "$0")/.."

case "$URL" in
  http://127.0.0.1:*|http://localhost:*) ;;
  *) echo "Refused: bootstrap only for a local instance ($URL)" >&2; exit 1 ;;
esac

curl -sf -X POST "$URL/api/auth/admin-sign-up" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\",\"name\":\"Dev\"}" >/dev/null \
  || echo "admin already created"

TOKEN=$(curl -sf -X POST "$URL/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["accessToken"])')

PERMS=$(PYTHONPATH=src python3 -c 'import json; from taxonero.immich.contract import REQUIRED_PERMISSIONS as p; print(json.dumps(list(p)))')

KEY=$(curl -sf -X POST "$URL/api/api-keys" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"name\":\"taxonero\",\"permissions\":$PERMS}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["secret"])')

printf 'IMMICH_URL=%s\nIMMICH_API_KEY=%s\n# Dev admin account (for the web interface)\n# %s / %s\n' "$URL" "$KEY" "$EMAIL" "$PASS" > dev/.env.dev
echo "Key created with: $PERMS"
echo "Written to dev/.env.dev"

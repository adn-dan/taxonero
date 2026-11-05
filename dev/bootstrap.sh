#!/usr/bin/env bash
# Initialise l'Immich de dev : compte admin + clé API restreinte aux permissions du contrat.
# Écrit dev/.env.dev (ignoré par git). À ne jamais lancer contre la prod.
set -euo pipefail

URL="${IMMICH_URL:-http://127.0.0.1:2284}"
EMAIL="dev@example.invalid"
PASS="dev-password"
cd "$(dirname "$0")/.."

case "$URL" in
  http://127.0.0.1:*|http://localhost:*) ;;
  *) echo "Refus : bootstrap réservé à une instance locale ($URL)" >&2; exit 1 ;;
esac

curl -sf -X POST "$URL/api/auth/admin-sign-up" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\",\"name\":\"Dev\"}" >/dev/null \
  || echo "admin déjà créé"

TOKEN=$(curl -sf -X POST "$URL/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["accessToken"])')

PERMS=$(PYTHONPATH=src python3 -c 'import json; from taxonero.immich.contract import REQUIRED_PERMISSIONS as p; print(json.dumps(list(p)))')

KEY=$(curl -sf -X POST "$URL/api/api-keys" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"name\":\"taxonero\",\"permissions\":$PERMS}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["secret"])')

printf 'IMMICH_URL=%s\nIMMICH_API_KEY=%s\n# Compte admin du dev (pour l interface web)\n# %s / %s\n' "$URL" "$KEY" "$EMAIL" "$PASS" > dev/.env.dev
echo "Clé créée avec : $PERMS"
echo "Écrit dans dev/.env.dev"

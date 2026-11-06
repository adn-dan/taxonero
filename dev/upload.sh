#!/usr/bin/env bash
# Sends dev/photos/*.jpg to the dev Immich, with the dev admin account.
set -euo pipefail
URL="${IMMICH_URL:-http://127.0.0.1:2284}"
cd "$(dirname "$0")"

case "$URL" in
  http://127.0.0.1:*|http://localhost:*) ;;
  *) echo "Refused: upload only for a local instance ($URL)" >&2; exit 1 ;;
esac

TOKEN=$(curl -sf -X POST "$URL/api/auth/login" -H 'Content-Type: application/json' \
  -d '{"email":"dev@example.invalid","password":"dev-password"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["accessToken"])')

n=0
for f in photos/*.jpg; do
  now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  curl -sf -X POST "$URL/api/assets" -H "Authorization: Bearer $TOKEN" \
    -F "assetData=@$f" -F "fileCreatedAt=$now" -F "fileModifiedAt=$now" >/dev/null && n=$((n+1))
done
echo "$n photos sent"

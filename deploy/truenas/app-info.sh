#!/bin/sh
# Fills the "Application Info" card of the Taxonero custom app on TrueNAS SCALE (24.10 and later):
# icon, title, description, links, and "App Version" read from the running image (OCI label).
# A custom app gets a generic card: TrueNAS writes its metadata.yaml once, at install, and Edit > Save
# leaves it alone, so the changes made here last. Run it again after each update, for the version.
#   sudo sh deploy/truenas/app-info.sh [app name, default taxonero]
# Then Apps > taxonero > Edit > Save, so that TrueNAS reloads the card.
set -eu
app=${1:-taxonero}
meta=/mnt/.ix-apps/app_configs/$app/metadata.yaml
[ -f "$meta" ] || { echo "no $meta: is the app named '$app'?" >&2; exit 1; }

# Version of the image the app runs (label set by the image build), empty when unknown.
container=$(docker ps --filter "label=com.docker.compose.project=ix-$app" --format '{{.Names}}' | head -n 1)
version=""
if [ -n "$container" ]; then
  image=$(docker inspect --format '{{.Image}}' "$container")
  version=$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' "$image" 2>/dev/null || true)
  case $version in "<no value>") version="" ;; esac
fi

cp -p "$meta" "$meta.bak"
python3 - "$meta" "$version" <<'EOF'
import sys
import yaml

path, version = sys.argv[1], sys.argv[2]
with open(path) as f:
    data = yaml.safe_load(f)
m = data["metadata"]   # name, train and version stay: TrueNAS requires them
m.update({
    "title": "Taxonero",
    "description": "Self-hosted AI species identification for your photos: Immich, local folders or photo apps",
    "icon": "https://raw.githubusercontent.com/adn-dan/taxonero/main/packaging/icons/taxonero.png",
    "home": "https://github.com/adn-dan/taxonero",
    "sources": ["https://github.com/adn-dan/taxonero"],
})
if version:
    m["app_version"] = version
with open(path, "w") as f:
    yaml.safe_dump(data, f, default_flow_style=False)
print(f"{path} updated (app version: {version or 'unchanged'}), backup in {path}.bak")
EOF
echo "Now: Apps > $app > Edit > Save, so that TrueNAS reloads the card."

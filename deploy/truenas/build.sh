#!/bin/sh
# Builds the taxonero image on the NAS: the TrueNAS SCALE "Install via YAML" installation only uses
# images already present (no build). Run it once, then at every update of the sources.
#
#   sudo sh deploy/truenas/build.sh /mnt/POOL/apps/taxonero/data
#
# - detects the GPU (like detect-gpu.sh) and builds taxonero:<cuda13|cuda12|cpu>;
# - creates the data folder and gives it to the TrueNAS apps user (568:568).
# Needs Internet (dependencies, ~5 GB) and about ten GB free for the build.
set -eu
cd "$(dirname "$0")/../.."
data=${1:-}

tmp=$(mktemp)
ENV_FILE=$tmp ./detect-gpu.sh >&2
gpu=$(sed -n 's/^GPU=//p' "$tmp" | tail -n 1)
rm -f "$tmp"

docker build --build-arg GPU="$gpu" -t "taxonero:$gpu" .

if [ -n "$data" ]; then
  mkdir -p "$data"
  chown 568:568 "$data"
  echo "Data: $data (owner 568:568)"
fi
echo "Image ready: taxonero:$gpu"
echo "In deploy/truenas/taxonero.yaml: image: taxonero:$gpu$([ "$gpu" = cpu ] && echo ', and remove the deploy block (GPU)')"

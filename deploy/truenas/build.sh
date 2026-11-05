#!/bin/sh
# Construit l'image de taxonero sur le NAS : l'installation « Install via YAML » de TrueNAS SCALE n'utilise
# que des images déjà présentes (pas de build). À lancer une fois, puis à chaque mise à jour des sources.
#
#   sudo sh deploy/truenas/build.sh /mnt/POOL/apps/taxonero/data
#
# - détecte le GPU (comme detect-gpu.sh) et construit taxonero:<cuda13|cuda12|cpu> ;
# - crée le dossier de données et le donne à l'utilisateur des apps TrueNAS (568:568).
# Il faut Internet (dépendances, ~5 Go) et une dizaine de Go libres pour la construction.
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
  echo "Données : $data (propriétaire 568:568)"
fi
echo "Image prête : taxonero:$gpu"
echo "Dans deploy/truenas/taxonero.yaml : image: taxonero:$gpu$([ "$gpu" = cpu ] && echo ', et retirer le bloc deploy (GPU)')"

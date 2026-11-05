#!/bin/sh
# Détecte le matériel de calcul de cette machine et écrit GPU=... dans .env (créé depuis .env.example s'il
# n'existe pas). Le GPU est pris dès qu'il y en a un de pris en charge ; sinon, le processeur.
#
#   NVIDIA, pilote >= 580 : cuda13      NVIDIA, pilote 525 à 579 : cuda12
#   AMD (ROCm) : rocm         Intel Arc ou Core Ultra : xpu         rocm et xpu : non testés
#   Intel plus ancien (Iris Xe, UHD) : processeur, PyTorch ne le prend pas en charge
#   Le programme vérifie au démarrage le GPU par un calcul d'essai : s'il échoue, analyse sur le processeur.
#
# Usage : ./detect-gpu.sh            (ENV_FILE=chemin pour écrire ailleurs que .env)
set -eu
cd "$(dirname "$0")"
env_file=${ENV_FILE:-.env}

gpu=cpu
note="aucun GPU NVIDIA détecté : analyse sur le processeur"
if command -v nvidia-smi >/dev/null 2>&1 && info=$(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | head -n 1) && [ -n "$info" ]; then
  name=${info%,*}
  driver=$(echo "${info##*,}" | tr -d ' ')
  major=${driver%%.*}
  if [ "$major" -ge 580 ]; then
    gpu=cuda13; note="$name, pilote $driver (CUDA 13)"
  elif [ "$major" -ge 525 ]; then
    gpu=cuda12; note="$name, pilote $driver (CUDA 12)"
  else
    note="$name : pilote $driver trop ancien (525 minimum), analyse sur le processeur"
  fi
  if [ "$gpu" != cpu ] && command -v docker >/dev/null 2>&1 \
     && ! docker info 2>/dev/null | grep -qi nvidia && [ ! -d /etc/cdi ] && [ ! -d /var/run/cdi ]; then
    echo "Attention : Docker ne semble pas avoir le NVIDIA Container Toolkit (nécessaire pour donner le GPU au conteneur)." >&2
  fi
elif [ -e /dev/kfd ]; then   # pilote amdgpu avec calcul (ROCm)
  gpu=rocm; note="GPU AMD (ROCm 7.2, non testé ; image de plus de 15 Go)"
elif grep -qs 0x8086 /sys/class/drm/renderD*/device/vendor; then
  # Intel Arc (cartes et iGPU des Core Ultra) : « Arc » dans le nom (lspci), ou pilote xe des modèles récents.
  if { command -v lspci >/dev/null 2>&1 && lspci | grep -iE 'vga|display|3d' | grep -i intel | grep -q 'Arc'; } \
     || ls -l /sys/class/drm/renderD*/device/driver 2>/dev/null | grep -q '/xe$'; then
    gpu=xpu; note="GPU Intel Arc (non testé)"
  else
    note="GPU Intel trop ancien pour PyTorch (Arc ou Core Ultra nécessaire) : analyse sur le processeur"
  fi
fi

[ -f "$env_file" ] || cp .env.example "$env_file"
set_var() {   # set_var NOM valeur : remplace la ligne NOM=… de .env, ou l'ajoute
  if grep -q "^$1=" "$env_file"; then sed -i "s/^$1=.*/$1=$2/" "$env_file"; else printf '\n%s=%s\n' "$1" "$2" >> "$env_file"; fi
}
set_var GPU "$gpu"
# AMD et Intel : le conteneur ne tourne pas en root ; il doit avoir le groupe propriétaire des périphériques du GPU
# (souvent « render », dont le numéro change d'une machine à l'autre).
if [ "$gpu" = rocm ] || [ "$gpu" = xpu ]; then
  node=$(ls /dev/dri/renderD* 2>/dev/null | head -n 1)
  [ -n "$node" ] && set_var RENDER_GID "$(stat -c %g "$node")"
fi
echo "GPU=$gpu  ($note) -> $env_file"

#!/bin/sh
# Detects the compute hardware of this machine and writes GPU=... to .env (created from .env.example if it
# does not exist). The GPU is used as soon as a supported one exists; otherwise, the CPU.
#
#   NVIDIA, driver >= 580: cuda13      NVIDIA, driver 525 to 579: cuda12
#   AMD (ROCm): rocm         Intel Arc or Core Ultra: xpu         rocm and xpu: untested
#   Older Intel (Iris Xe, UHD): CPU, PyTorch does not support it
#   At startup the program checks the GPU with a test computation: if it fails, the analysis runs on the CPU.
#
# Usage: ./detect-gpu.sh            (ENV_FILE=path to write somewhere other than .env)
set -eu
cd "$(dirname "$0")"
env_file=${ENV_FILE:-.env}

gpu=cpu
note="no NVIDIA GPU detected: analysis on the CPU"
if command -v nvidia-smi >/dev/null 2>&1 && info=$(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | head -n 1) && [ -n "$info" ]; then
  name=${info%,*}
  driver=$(echo "${info##*,}" | tr -d ' ')
  major=${driver%%.*}
  if [ "$major" -ge 580 ]; then
    gpu=cuda13; note="$name, driver $driver (CUDA 13)"
  elif [ "$major" -ge 525 ]; then
    gpu=cuda12; note="$name, driver $driver (CUDA 12)"
  else
    note="$name: driver $driver too old (525 minimum), analysis on the CPU"
  fi
  if [ "$gpu" != cpu ] && command -v docker >/dev/null 2>&1 \
     && ! docker info 2>/dev/null | grep -qi nvidia && [ ! -d /etc/cdi ] && [ ! -d /var/run/cdi ]; then
    echo "Warning: Docker does not seem to have the NVIDIA Container Toolkit (needed to give the GPU to the container)." >&2
  fi
elif [ -e /dev/kfd ]; then   # amdgpu driver with compute (ROCm)
  gpu=rocm; note="AMD GPU (ROCm 7.2, untested; image of more than 15 GB)"
elif grep -qs 0x8086 /sys/class/drm/renderD*/device/vendor; then
  # Intel Arc (cards and Core Ultra iGPUs): "Arc" in the name (lspci), or the xe driver of recent models.
  if { command -v lspci >/dev/null 2>&1 && lspci | grep -iE 'vga|display|3d' | grep -i intel | grep -q 'Arc'; } \
     || ls -l /sys/class/drm/renderD*/device/driver 2>/dev/null | grep -q '/xe$'; then
    gpu=xpu; note="Intel Arc GPU (untested)"
  else
    note="Intel GPU too old for PyTorch (Arc or Core Ultra needed): analysis on the CPU"
  fi
fi

[ -f "$env_file" ] || cp .env.example "$env_file"
set_var() {   # set_var NAME value: replaces the NAME=… line of .env, or adds it
  if grep -q "^$1=" "$env_file"; then sed -i "s/^$1=.*/$1=$2/" "$env_file"; else printf '\n%s=%s\n' "$1" "$2" >> "$env_file"; fi
}
set_var GPU "$gpu"
# AMD and Intel: the container does not run as root; it needs the group owning the GPU devices
# (often "render", whose number changes from one machine to another).
if [ "$gpu" = rocm ] || [ "$gpu" = xpu ]; then
  node=$(ls /dev/dri/renderD* 2>/dev/null | head -n 1)
  [ -n "$node" ] && set_var RENDER_GID "$(stat -c %g "$node")"
fi
echo "GPU=$gpu  ($note) -> $env_file"

# syntax=docker/dockerfile:1

FROM python:3.12-slim
# Image description (OCI), and its icon for the platforms that read it from a label (Unraid).
LABEL org.opencontainers.image.title="Taxonero" \
      org.opencontainers.image.description="Self-hosted AI species identification for your photos: Immich, local folders or photo apps" \
      org.opencontainers.image.source="https://github.com/adn-dan/taxonero" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      net.unraid.docker.icon="https://raw.githubusercontent.com/adn-dan/taxonero/main/packaging/icons/taxonero.png"
# Compute hardware (--build-arg GPU=...): the three models (MegaDetector, OWLv2, BioCLIP) run in PyTorch,
# hence on the same GPU, whatever it is. Weights are downloaded at first launch into /data.
#   cuda12 (default): NVIDIA, driver >= 525; torch 2.11 cu128
#   cuda13          : NVIDIA, driver >= 580; torch 2.14 cu130
#   rocm            : AMD (ROCm 7.2), experimental, untested
#   xpu             : Intel Arc and Core Ultra (Intel GPU driver included), untested; Iris Xe and older:
#                     not supported by PyTorch (detected by a test computation, analysis on the CPU)
#   cpu             : no GPU (x86 and ARM)
# ONNX Runtime (CPU) is only used for an ONNX MegaDetector provided through DETECTOR_PATH (older images).
ARG GPU=cuda12
ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/data/hf \
    DATA_DIR=/data \
    DEVICE=auto

RUN case "$GPU" in \
      cuda12) idx=cu128;   torch="torch==2.11.0 torchvision==0.26.0" ;; \
      cuda13) idx=cu130;   torch="torch==2.14.1 torchvision==0.29.1" ;; \
      rocm)   idx=rocm7.2; torch="torch==2.14.1 torchvision==0.29.1" ;; \
      xpu)    idx=xpu;     torch="torch==2.14.1 torchvision==0.29.1" ;; \
      cpu)    idx=cpu;     torch="torch==2.14.1 torchvision==0.29.1" ;; \
      *) echo "unknown GPU=$GPU: cuda12 | cuda13 | rocm | xpu | cpu" >&2; exit 1 ;; \
    esac \
    && pip install --no-cache-dir $torch --index-url https://download.pytorch.org/whl/$idx \
    && pip install --no-cache-dir onnxruntime==1.31.0

# Time zones: TZ (e.g. Europe/Paris) sets the server time, that of the analysis range.
# tzdata: TZ time zone; exiftool: metadata and XMP files of the "local folder" source.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata libimage-exiftool-perl && rm -rf /var/lib/apt/lists/*

# Intel (xpu): PyTorch XPU needs the user-space GPU driver (Intel Level Zero and OpenCL), which Debian
# does not provide; without it, no GPU is seen. Official Intel packages (compute-runtime and its IGC compiler),
# pinned versions, checksums verified (those of compute-runtime are published by Intel in ww40.sum).
ARG INTEL_CR=https://github.com/intel/compute-runtime/releases/download/26.40.40196.8
ARG INTEL_IGC=https://github.com/intel/intel-graphics-compiler/releases/download/v2.42.6
RUN if [ "$GPU" = xpu ]; then \
      set -e; mkdir /tmp/intel && cd /tmp/intel \
      && printf '%s\n' \
        "e20539632f2d99875d8f8b225d5da58b31f98baa54bb47c6328aa98ec473ab9d $INTEL_IGC/intel-igc-core-2_2.42.6+23070_amd64.deb" \
        "7f5593cf828503ffb8450967768bde9209e22753bc576296e846b5a751011fa3 $INTEL_IGC/intel-igc-opencl-2_2.42.6+23070_amd64.deb" \
        "96046069e94680b761df6e83ddc64244721178b826e3e1f8af3ca104cfd0c7c2 $INTEL_CR/intel-opencl-icd_26.40.40196.8-0_amd64.deb" \
        "053fceadb84a1d2f9a30a0d1bc9b16424b3f1a90cc49900fcd80aadcf25c8a4d $INTEL_CR/libigdgmm12_22.10.2_amd64.deb" \
        "d9c86b2bb195baea6936c2708846cd1fceeadc39bc5e167f728858a5ca6fb86e $INTEL_CR/libze-intel-gpu1_26.40.40196.8-0_amd64.deb" \
        > list \
      && while read -r sum url; do \
           f=${url##*/}; python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" "$url" "$f" \
           && echo "$sum  $f" | sha256sum -c -; \
         done < list \
      && apt-get update && apt-get install -y --no-install-recommends libze1 ocl-icd-libopencl1 ./*.deb \
      && cd / && rm -rf /tmp/intel /var/lib/apt/lists/*; \
    fi

WORKDIR /app
COPY pyproject.toml README.md constraints.txt ./
COPY src ./src
# constraints.txt: tested versions of the Python libraries (reproducible build).
RUN pip install --no-cache-dir -c constraints.txt .

# Default user 1000; another uid (TrueNAS: apps as 568) is set with `user:` in the compose file.
# /data is then the mounted volume, which must belong to this uid. HOME points to it for the caches.
# No 0777: the database holds the Immich API key; only the owner writes to it.
RUN useradd --uid 1000 --create-home nature && mkdir -p /data && chown nature /data && chmod 0750 /data
ENV HOME=/data
USER nature
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8765/healthz'); sys.exit(0)" || exit 1
ENTRYPOINT ["taxonero"]
CMD ["serve"]

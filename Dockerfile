# marker service image — one image, three roles (worker / api / batch) selected
# by the command in compose / k8s.
#
# Uses the default (CUDA-enabled) torch wheel: runs on CPU out of the box, and
# uses the GPU automatically when the container has the NVIDIA runtime (K8s GPU
# nodes). Models are NOT baked in — they download on first run into /models,
# which should be a mounted volume/PVC shared across replicas (HF_HOME and
# MODEL_CACHE_DIR both point there).

# --- stage 1: llama-server (surya's OCR inference backend) -------------------
# marker's OCR engine (surya) does not run OCR in-process: it spawns a
# `llama-server` (llama.cpp) and talks to it over loopback, so the binary has to
# be in the image or every OCR/--force_ocr conversion fails. Built from source
# rather than unpacked from a release tarball: the tarballs are built for a
# specific glibc and ship ~18 dlopen-ed CPU micro-architecture variants, while a
# plain shared-library build produces one portable libggml-cpu.so.
#
#   GGML_NATIVE=OFF        do not tune for the builder's CPU — the image has to
#                          run on whatever node schedules it.
#   LLAMA_OPENSSL=OFF      no HTTPS in the server: surya downloads the GGUF
#                          weights itself (huggingface_hub) and passes local
#                          paths, so download support is dead weight that would
#                          drag libssl into the runtime stage. LLAMA_CURL=OFF is
#                          the same intent for tags before curl was dropped (a
#                          no-op deprecation warning at v0.4.0).
#   LLAMA_USE_PREBUILT_UI=OFF  skip the build-time download of the server's Web
#                          UI bundle from the HF bucket; surya only speaks to
#                          /health, /v1/models and /v1/chat/completions.
# Built on bookworm (glibc 2.36) while python:3.12-slim has moved on to trixie:
# a binary linked against the older glibc loads on the newer one, so the runtime
# base can keep moving without rebuilding this stage differently.
FROM debian:bookworm-slim AS llama
# Pinned tag (bump deliberately, with a rebuild). Floor: surya's GGUF is a
# qwen35 multimodal model, so the server needs qwen35 + mtmd support — anything
# older than llama.cpp b7990 refuses to load it.
ARG LLAMA_CPP_REF=v0.4.0
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --depth 1 --branch "${LLAMA_CPP_REF}" \
        https://github.com/ggml-org/llama.cpp.git . \
    && cmake -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=ON \
        -DGGML_NATIVE=OFF \
        -DLLAMA_CURL=OFF \
        -DLLAMA_OPENSSL=OFF \
        -DLLAMA_USE_PREBUILT_UI=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
    && cmake --build build --target llama-server -j "$(nproc)" \
    && mkdir -p /llama/bin /llama/lib \
    && cp build/bin/llama-server /llama/bin/ \
    && cp -a build/bin/*.so* /llama/lib/

# --- stage 2: the service image ---------------------------------------------
FROM python:3.12-slim

# HOME is set explicitly: surya keeps its server sentinel/lock/log under
# ~/.cache/datalab and Kubernetes hands containers HOME=/root when the image
# does not say otherwise, which uid 1000 cannot write.
# SURYA_INFERENCE_BACKEND is pinned to llamacpp because that is the backend this
# image ships; surya would otherwise auto-select vllm on a GPU node, and vLLM is
# not installed here. Override it if you add vLLM yourself.
# MODEL_CACHE_DIR is surya's own cache, and it is a SECOND download path, not a
# duplicate of HF_HOME: surya fetches its OCR models from models.datalab.to
# over plain HTTP, not from the Hugging Face Hub, so neither HF_HOME nor
# HF_HUB_OFFLINE has any effect on them. Its default is
# platformdirs.user_cache_dir("datalab")/models, i.e. ~/.cache/datalab/models —
# inside the container's own writable layer, which is thrown away with the
# container. Every new pod therefore re-downloaded the OCR models before it
# could convert anything, and an air-gapped cluster could not run at all.
# Pointed at the PVC it is downloaded once, shared by every replica and
# survives restarts. The name has no prefix because surya reads it through
# pydantic-settings, which maps a field straight onto its own name.
#
# HOME stays on the container filesystem on purpose. surya also keeps a
# per-process llama-server sentinel, lock and log under ~/.cache/datalab/surya
# with the path hardcoded, and those must NOT be shared: on a common volume two
# replicas would each find the other's sentinel and try to attach to a server
# in a different container.
ENV PYTHONUNBUFFERED=1 \
    HOME=/home/app \
    HF_HOME=/models \
    TORCH_HOME=/models \
    MODEL_CACHE_DIR=/models/datalab \
    REDIS_URL=redis://redis:6379/0 \
    OUTPUT_DIR=/data/output \
    SURYA_INFERENCE_BACKEND=llamacpp \
    LLAMA_CPP_BINARY=/usr/local/bin/llama-server

WORKDIR /app

# System libs: libgl1/libglib2.0 for marker's OpenCV/image stack; libgomp1 is
# GNU OpenMP, which llama-server's libggml-cpu links against. The build stage
# gets it from build-essential, the slim runtime base does not ship it, and
# without it llama-server fails to load (no OCR at all).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# surya's OCR backend, from stage 1: the server binary plus the shared libraries
# the build produced (libllama-server-impl, libllama-common, libmtmd, libllama,
# libggml*, copied with -a so the .so version symlinks survive). The binary's
# build-tree RPATH points at the builder's /src/build/bin, which does not exist
# here, so ldconfig has to register /usr/local/lib (already on Debian's default
# search path) for the loader to find them.
COPY --from=llama /llama/bin/llama-server /usr/local/bin/llama-server
COPY --from=llama /llama/lib/ /usr/local/lib/
RUN ldconfig

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# marker downloads a rendering font on the first conversion, into its own
# package dir (site-packages/static/fonts). That dir is root-owned and the
# service runs as uid 1000, so every conversion failed with EACCES. Fetch it
# now, as root, so nothing has to be written there at runtime.
RUN python -c "from marker.util import download_font; download_font()" \
    && python -c "import os; from marker.settings import settings; assert os.path.getsize(settings.FONT_PATH) > 0, settings.FONT_PATH"

COPY provenance.py tasks.py worker.py api.py enqueue_batch.py ./

# Run unprivileged. /data/{input,output} and /models are the mount points; a
# host bind mount must be writable by uid 1000 (k8s: fsGroup 1000 or an RWX
# class that maps ownership). The `models` named volume inherits app's ownership.
# /home/app is chowned explicitly: HOME points there from the ENV above, so the
# root-run build steps (pip) already created a root-owned /home/app/.cache, and
# useradd does not take over a home that exists. surya then could not create
# ~/.cache/datalab and every conversion failed with EACCES.
RUN useradd --system --uid 1000 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data/input /data/output /models/datalab /home/app \
    && chown -R app:app /data /models /home/app
USER app

# Default role: worker. Override in compose/k8s for the API or batch enqueuer.
CMD ["python", "worker.py"]

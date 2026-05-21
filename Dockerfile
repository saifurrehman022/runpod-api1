# =============================================================================
# Dockerfile  (FIXED)
#
# What changed from original:
#   - Added ComfyUI-WanVideoWrapper (CRITICAL — workflow uses 9 node types from it)
#   - Added comfyui-videohelpersuite (workflow uses VHS_VideoCombine)
#   - Kept all original base structure, uv venv, start.sh, handler.py
#
# Build:
#   docker build -t yourrepo/worker-wan22:latest .
#   docker push yourrepo/worker-wan22:latest
#
# Then in RunPod Serverless: attach Network Volume under Advanced settings.
# =============================================================================

# FORCE_CLEAN_BUILD_2026_01_12_B
ARG BASE_IMAGE=nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04

FROM ${BASE_IMAGE} AS base

ARG COMFYUI_VERSION=
ARG CUDA_VERSION_FOR_COMFY
ARG ENABLE_PYTORCH_UPGRADE=false
ARG PYTORCH_INDEX_URL

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_PREFER_BINARY=1
ENV PYTHONUNBUFFERED=1
ENV CMAKE_BUILD_PARALLEL_LEVEL=8

RUN apt-get update && apt-get install -y \
    python3.12 \
    python3.12-venv \
    git \
    wget \
    curl \
    ca-certificates \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    ffmpeg \
    && ln -sf /usr/bin/python3.12 /usr/bin/python \
    && ln -sf /usr/bin/pip3 /usr/bin/pip

RUN apt-get autoremove -y && apt-get clean -y && rm -rf /var/lib/apt/lists/*

RUN wget -qO- https://astral.sh/uv/install.sh | sh \
    && ln -s /root/.local/bin/uv /usr/local/bin/uv \
    && ln -s /root/.local/bin/uvx /usr/local/bin/uvx \
    && uv venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}"

RUN /opt/venv/bin/python -m ensurepip --upgrade \
    && /opt/venv/bin/python -m pip install --upgrade pip setuptools wheel

RUN /opt/venv/bin/python -m pip install comfy-cli

RUN set -eux; \
    if [ -n "${CUDA_VERSION_FOR_COMFY:-}" ]; then \
        if [ -n "${COMFYUI_VERSION:-}" ]; then \
            /usr/bin/yes | comfy --workspace /comfyui install --version "${COMFYUI_VERSION}" --cuda-version "${CUDA_VERSION_FOR_COMFY}" --nvidia; \
        else \
            /usr/bin/yes | comfy --workspace /comfyui install --cuda-version "${CUDA_VERSION_FOR_COMFY}" --nvidia; \
        fi; \
    else \
        if [ -n "${COMFYUI_VERSION:-}" ]; then \
            /usr/bin/yes | comfy --workspace /comfyui install --version "${COMFYUI_VERSION}" --nvidia; \
        else \
            /usr/bin/yes | comfy --workspace /comfyui install --nvidia; \
        fi; \
    fi

RUN if [ "$ENABLE_PYTORCH_UPGRADE" = "true" ]; then \
    /opt/venv/bin/python -m pip install --force-reinstall torch torchvision torchaudio --index-url ${PYTORCH_INDEX_URL}; \
    fi

ENV COMFYUI_DIR=/comfyui/ComfyUI

ADD src/extra_model_paths.yaml /comfyui/ComfyUI/extra_model_paths.yaml

WORKDIR /

RUN /opt/venv/bin/python -m pip install runpod requests websocket-client

ADD src/start.sh src/network_volume.py src/handler.py ./
RUN chmod +x /start.sh

COPY scripts/comfy-node-install.sh /usr/local/bin/comfy-node-install
RUN chmod +x /usr/local/bin/comfy-node-install

ENV PIP_NO_INPUT=1

COPY scripts/comfy-manager-set-mode.sh /usr/local/bin/comfy-manager-set-mode
RUN chmod +x /usr/local/bin/comfy-manager-set-mode

# =============================================================================
# CUSTOM NODES  (this entire section was MISSING in the original Dockerfile)
# The workflow wan_i2v_LOCKED.json requires all of these.
# =============================================================================

# ComfyUI-WanVideoWrapper — provides ALL Wan-specific nodes:
#   WanVideoModelLoader, WanVideoSampler, WanVideoEncode,
#   WanVideoVAELoader, WanVideoImageClipEncode, WanVideoTextEmbedBridge,
#   WanVideoExperimentalArgs, WanVideoCacheArgs, WanVideoSLGArgs
RUN comfy-node-install ComfyUI-WanVideoWrapper

# Video Helper Suite — provides VHS_VideoCombine (output node in workflow)
RUN comfy-node-install comfyui-videohelpersuite

# =============================================================================
# RunPod Serverless entrypoint
# =============================================================================
ENTRYPOINT ["/bin/bash", "/start.sh"]

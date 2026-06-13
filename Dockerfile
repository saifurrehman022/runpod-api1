
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

# Instal , git and other necessary tools
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

# Install uv and create isolated venv
RUN wget -qO- https://astral.sh/uv/install.sh | sh \
    && ln -s /root/.local/bin/uv /usr/local/bin/uv \
    && ln -s /root/.local/bin/uvx /usr/local/bin/uvx \
    && uv venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}"

RUN /opt/venv/bin/python -m ensurepip --upgrade \
    && /opt/venv/bin/python -m pip install --upgrade pip setuptools wheel

# Install comfy-cli
RUN /opt/venv/bin/python -m pip install comfy-cli

# Install ComfyUI
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

# Upgrade PyTorch if needed (for newer CUDA versions)
RUN if [ "$ENABLE_PYTORCH_UPGRADE" = "true" ]; then \
    /opt/venv/bin/python -m pip install --force-reinstall torch torchvision torchaudio --index-url ${PYTORCH_INDEX_URL}; \
    fi

ENV COMFYUI_DIR=/comfyui

# Copy paths configuration
ADD src/extra_model_paths.yaml /comfyui/extra_model_paths.yaml
ADD src/extra_model_paths.yaml /comfyui/ComfyUI/extra_model_paths.yaml

WORKDIR /

# Install Python runtime dependencies for the handler
RUN /opt/venv/bin/python -m pip install runpod requests websocket-client

# Add application code and scripts
ADD src/start.sh src/network_volume.py src/handler.py ./
RUN chmod +x /start.sh

# Add script to install custom nodes
COPY scripts/comfy-node-install.sh /usr/local/bin/comfy-node-install
RUN chmod +x /usr/local/bin/comfy-node-install

ENV PIP_NO_INPUT=1

COPY scripts/comfy-manager-set-mode.sh /usr/local/bin/comfy-manager-set-mode
RUN chmod +x /usr/local/bin/comfy-manager-set-mode

# =============================================================================
# CUSTOM NODES
# =============================================================================
RUN mkdir -p /comfyui/custom_nodes

# Upgrade ComfyUI internal env
RUN /comfyui/.venv/bin/python -m pip install --no-cache-dir --upgrade pip setuptools wheel

# Pre-requisite packages for all custom nodes
RUN /comfyui/.venv/bin/python -m pip install --no-cache-dir \
    opencv-python-headless \
    imageio-ffmpeg \
    accelerate \
    diffusers \
    peft \
    einops \
    sentencepiece \
    protobuf \
    pyloudnorm \
    gguf \
    ftfy \
    color-matcher \
    matplotlib \
    mss

# KJNodes — required by WanVideoWrapper for DiffusionModelLoaderKJ, ImageResizeKJv2, etc.
RUN git clone --depth 1 https://github.com/kijai/ComfyUI-KJNodes.git /comfyui/custom_nodes/ComfyUI-KJNodes && \
    if [ -f /comfyui/custom_nodes/ComfyUI-KJNodes/requirements.txt ]; then \
        /comfyui/.venv/bin/python -m pip install --no-cache-dir -r /comfyui/custom_nodes/ComfyUI-KJNodes/requirements.txt; \
    fi

# VideoHelperSuite — required for VHS_VideoCombine output node
RUN git clone --depth 1 https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git /comfyui/custom_nodes/ComfyUI-VideoHelperSuite && \
    if [ -f /comfyui/custom_nodes/ComfyUI-VideoHelperSuite/requirements.txt ]; then \
        /comfyui/.venv/bin/python -m pip install --no-cache-dir -r /comfyui/custom_nodes/ComfyUI-VideoHelperSuite/requirements.txt; \
    fi

# WanVideoWrapper — MUST be latest; SVI Extender nodes (UUID class_type) are inside this repo.
# Do NOT use --depth 1 if you need full history, but depth 1 is fine for runtime.
RUN git clone --depth 1 https://github.com/kijai/ComfyUI-WanVideoWrapper.git /comfyui/custom_nodes/ComfyUI-WanVideoWrapper && \
    if [ -f /comfyui/custom_nodes/ComfyUI-WanVideoWrapper/requirements.txt ]; then \
        /comfyui/.venv/bin/python -m pip install --no-cache-dir -r /comfyui/custom_nodes/ComfyUI-WanVideoWrapper/requirements.txt; \
    fi
# FIX: Clone the Logic suite to provide the missing 'SetNode' primitive
RUN git clone https://github.com/theUpsider/ComfyUI-Logic.git /comfyui/custom_nodes/ComfyUI-Logic && \
    if [ -f /comfyui/custom_nodes/ComfyUI-Logic/requirements.txt ]; then \
        /comfyui/.venv/bin/python -m pip install --no-cache-dir -r /comfyui/custom_nodes/ComfyUI-Logic/requirements.txt; \
    fi
# =============================================================================
# MODELS
# =============================================================================

# LoRAs
RUN BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do \
    comfy --workspace /comfyui model download \
      --url 'https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/LoRAs/Wan22_Lightx2v/Wan_2_2_I2V_A14B_HIGH_lightx2v_4step_lora_v1030_rank_64_bf16.safetensors' \
      --relative-path models/loras \
      --filename 'Wan_2_2_I2V_A14B_HIGH_lightx2v_4step_lora_v1030_rank_64_bf16.safetensors' && break; \
    if [ $i -eq 5 ]; then echo "model-download failed" >&2; exit 1; fi; \
    SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i); sleep $SLEEP; done

RUN BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do \
    comfy --workspace /comfyui model download \
      --url 'https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/LoRAs/Stable-Video-Infinity/v2.0/SVI_v2_PRO_Wan2.2-I2V-A14B_HIGH_lora_rank_128_fp16.safetensors' \
      --relative-path models/loras \
      --filename 'SVI_v2_PRO_Wan2.2-I2V-A14B_HIGH_lora_rank_128_fp16.safetensors' && break; \
    if [ $i -eq 5 ]; then echo "model-download failed" >&2; exit 1; fi; \
    SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i); sleep $SLEEP; done

RUN BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do \
    comfy --workspace /comfyui model download \
      --url 'https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/Lightx2v/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors' \
      --relative-path models/loras \
      --filename 'lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors' && break; \
    if [ $i -eq 5 ]; then echo "model-download failed" >&2; exit 1; fi; \
    SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i); sleep $SLEEP; done

RUN BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do \
    comfy --workspace /comfyui model download \
      --url 'https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/LoRAs/Stable-Video-Infinity/v2.0/SVI_v2_PRO_Wan2.2-I2V-A14B_LOW_lora_rank_128_fp16.safetensors' \
      --relative-path models/loras \
      --filename 'SVI_v2_PRO_Wan2.2-I2V-A14B_LOW_lora_rank_128_fp16.safetensors' && break; \
    if [ $i -eq 5 ]; then echo "model-download failed" >&2; exit 1; fi; \
    SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i); sleep $SLEEP; done

# Text encoders
RUN BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do \
    comfy --workspace /comfyui model download \
      --url 'https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors' \
      --relative-path models/text_encoders \
      --filename 'umt5_xxl_fp8_e4m3fn_scaled.safetensors' && break; \
    if [ $i -eq 5 ]; then echo "model-download failed" >&2; exit 1; fi; \
    SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i); sleep $SLEEP; done

# VAE
RUN BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do \
    comfy --workspace /comfyui model download \
      --url 'https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/vae/wan_2.1_vae.safetensors' \
      --relative-path models/vae \
      --filename 'Wan2_1_VAE_bf16.safetensors' && break; \
    if [ $i -eq 5 ]; then echo "model-download failed" >&2; exit 1; fi; \
    SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i); sleep $SLEEP; done

# Diffusion models (HIGH and LOW — both mapped from same source for now)
RUN BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do \
    comfy --workspace /comfyui model download \
      --url 'https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/diffusion_models/wan2.1_t2v_14B_fp8_e4m3fn.safetensors' \
      --relative-path models/diffusion_models \
      --filename 'Wan2_2-I2V-A14B-HIGH_fp8_e4m3fn_scaled_KJ.safetensors' && break; \
    if [ $i -eq 5 ]; then echo "model-download failed" >&2; exit 1; fi; \
    SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i); sleep $SLEEP; done

RUN BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do \
    comfy --workspace /comfyui model download \
      --url 'https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/diffusion_models/wan2.1_t2v_14B_fp8_e4m3fn.safetensors' \
      --relative-path models/diffusion_models \
      --filename 'Wan2_2-I2V-A14B-LOW_fp8_e4m3fn_scaled_KJ.safetensors' && break; \
    if [ $i -eq 5 ]; then echo "model-download failed" >&2; exit 1; fi; \
    SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i); sleep $SLEEP; done

# Input directory and default image
RUN mkdir -p /comfyui/input
RUN wget --progress=dot:giga \
    -O '/comfyui/input/Gemini_Generated_Image_jk7o1njk7o1njk7o.png' \
    "https://cool-anteater-319.convex.cloud/api/storage/0c172877-f42a-4fa0-89ea-d40d82991fa6"

# RunPod Serverless entrypoint
ENTRYPOINT ["/bin/bash", "/start.sh"]

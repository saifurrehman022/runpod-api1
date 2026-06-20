ARG BASE_IMAGE=nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04
FROM ${BASE_IMAGE} AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_PREFER_BINARY=1 \
    PYTHONUNBUFFERED=1 \
    CMAKE_BUILD_PARALLEL_LEVEL=8 \
    PIP_NO_INPUT=1

# =============================================================================
# 2. SYSTEM DEPENDENCIES
# =============================================================================
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.12 \
    python3.12-venv \
    python3.12-dev \
    python3-pip \
    git \
    wget \
    curl \
    ca-certificates \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.12 /usr/bin/python \
    && ln -sf /usr/bin/python3.12 /usr/bin/python3

# =============================================================================
# 3. PYTHON ENVIRONMENT
# =============================================================================
# Create venv WITH pip (--seed ensures pip is installed inside venv)
RUN python3.12 -m venv /comfyui/.venv --copies
ENV PATH="/comfyui/.venv/bin:${PATH}"

# Upgrade pip inside the venv
RUN /comfyui/.venv/bin/python -m pip install --upgrade pip setuptools wheel

# Install runner + ComfyUI CLI
RUN /comfyui/.venv/bin/python -m pip install comfy-cli runpod requests websocket-client

# =============================================================================
# 4. PYTORCH FOR RTX 5090 (Blackwell — CUDA 12.8)
# =============================================================================
RUN /comfyui/.venv/bin/python -m pip install --force-reinstall \
    torch torchvision torchaudio \
    --index-url https://download.pytorch.org/whl/cu128

# =============================================================================
# 5. COMFYUI INSTALLATION
# =============================================================================
RUN git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git /comfyui/ComfyUI
RUN /comfyui/.venv/bin/python -m pip install -r /comfyui/ComfyUI/requirements.txt

ENV COMFYUI_DIR=/comfyui
WORKDIR /

# =============================================================================
# 6. RUNPOD INFRASTRUCTURE
# =============================================================================
ADD src/extra_model_paths.yaml /comfyui/extra_model_paths.yaml
ADD src/extra_model_paths.yaml /comfyui/ComfyUI/extra_model_paths.yaml

ADD src/start.sh src/network_volume.py src/handler.py ./
COPY src/workflow.json /workflow.json
RUN chmod +x /start.sh

COPY scripts/comfy-node-install.sh /usr/local/bin/comfy-node-install
RUN chmod +x /usr/local/bin/comfy-node-install

COPY scripts/comfy-manager-set-mode.sh /usr/local/bin/comfy-manager-set-mode
RUN chmod +x /usr/local/bin/comfy-manager-set-mode

# =============================================================================
# 7. CUSTOM NODES PYTHON DEPENDENCIES
# =============================================================================
RUN mkdir -p /comfyui/ComfyUI/custom_nodes

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
    mss \
    onnxruntime-gpu \
    transformers \
    sageattention \
    sympy

# KJNodes — core math, loaders, image transforms
RUN git clone --depth 1 https://github.com/kijai/ComfyUI-KJNodes.git \
    /comfyui/ComfyUI/custom_nodes/ComfyUI-KJNodes && \
    if [ -f /comfyui/ComfyUI/custom_nodes/ComfyUI-KJNodes/requirements.txt ]; then \
        /comfyui/.venv/bin/python -m pip install --no-cache-dir \
            -r /comfyui/ComfyUI/custom_nodes/ComfyUI-KJNodes/requirements.txt; \
    fi

# VideoHelperSuite — VHS_VideoCombine node
RUN git clone --depth 1 https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git \
    /comfyui/ComfyUI/custom_nodes/ComfyUI-VideoHelperSuite && \
    if [ -f /comfyui/ComfyUI/custom_nodes/ComfyUI-VideoHelperSuite/requirements.txt ]; then \
        /comfyui/.venv/bin/python -m pip install --no-cache-dir \
            -r /comfyui/ComfyUI/custom_nodes/ComfyUI-VideoHelperSuite/requirements.txt; \
    fi

# WanVideoWrapper — DiffusionModelLoaderKJ, WanImageToVideoSVIPro, etc.
RUN git clone --depth 1 https://github.com/kijai/ComfyUI-WanVideoWrapper.git \
    /comfyui/ComfyUI/custom_nodes/ComfyUI-WanVideoWrapper && \
    if [ -f /comfyui/ComfyUI/custom_nodes/ComfyUI-WanVideoWrapper/requirements.txt ]; then \
        /comfyui/.venv/bin/python -m pip install --no-cache-dir \
            -r /comfyui/ComfyUI/custom_nodes/ComfyUI-WanVideoWrapper/requirements.txt; \
    fi

# ComfyUI-Logic — SetNode / GetNode primitives
RUN git clone --depth 1 https://github.com/theUpsider/ComfyUI-Logic.git \
    /comfyui/ComfyUI/custom_nodes/ComfyUI-Logic && \
    if [ -f /comfyui/ComfyUI/custom_nodes/ComfyUI-Logic/requirements.txt ]; then \
        /comfyui/.venv/bin/python -m pip install --no-cache-dir \
            -r /comfyui/ComfyUI/custom_nodes/ComfyUI-Logic/requirements.txt; \
    fi

# SVI Pro FLF — WanImageToVideoSVIPro node
RUN git clone --depth 1 https://github.com/Well-Made/ComfyUI-Wan-SVI2Pro-FLF.git \
    /comfyui/ComfyUI/custom_nodes/ComfyUI-Wan-SVI2Pro-FLF && \
    if [ -f /comfyui/ComfyUI/custom_nodes/ComfyUI-Wan-SVI2Pro-FLF/requirements.txt ]; then \
        /comfyui/.venv/bin/python -m pip install --no-cache-dir \
            -r /comfyui/ComfyUI/custom_nodes/ComfyUI-Wan-SVI2Pro-FLF/requirements.txt; \
    fi

# =============================================================================
# 8. MODEL DOWNLOADS (retry loop — up to 5 attempts with backoff)
# =============================================================================
RUN mkdir -p /comfyui/ComfyUI/models/diffusion_models \
             /comfyui/ComfyUI/models/loras \
             /comfyui/ComfyUI/models/text_encoders \
             /comfyui/ComfyUI/models/vae \
             /comfyui/ComfyUI/models/clip_vision

# Helper: retry wget
SHELL ["/bin/bash", "-c"]

# Diffusion model HIGH
RUN for i in 1 2 3 4 5; do \
    wget -q --show-progress \
      -O /comfyui/ComfyUI/models/diffusion_models/Wan2_2-I2V-A14B-HIGH_fp8_e4m3fn_scaled_KJ.safetensors \
      https://huggingface.co/Kijai/WanVideo_comfy_fp8_scaled/resolve/main/I2V/Wan2_2-I2V-A14B-HIGH_fp8_e4m3fn_scaled_KJ.safetensors \
      && break || { echo "Retry $i..."; sleep $((i*15)); }; \
    done

# Diffusion model LOW
RUN for i in 1 2 3 4 5; do \
    wget -q --show-progress \
      -O /comfyui/ComfyUI/models/diffusion_models/Wan2_2-I2V-A14B-LOW_fp8_e4m3fn_scaled_KJ.safetensors \
      https://huggingface.co/Kijai/WanVideo_comfy_fp8_scaled/resolve/main/I2V/Wan2_2-I2V-A14B-LOW_fp8_e4m3fn_scaled_KJ.safetensors \
      && break || { echo "Retry $i..."; sleep $((i*15)); }; \
    done

# LoRA — Lightx2v HIGH noise
RUN for i in 1 2 3 4 5; do \
    wget -q --show-progress \
      -O /comfyui/ComfyUI/models/loras/Wan_2_2_I2V_A14B_HIGH_lightx2v_4step_lora_v1030_rank_64_bf16.safetensors \
      https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/LoRAs/Wan22_Lightx2v/Wan_2_2_I2V_A14B_HIGH_lightx2v_4step_lora_v1030_rank_64_bf16.safetensors \
      && break || { echo "Retry $i..."; sleep $((i*15)); }; \
    done

# LoRA — Lightx2v LOW noise
RUN for i in 1 2 3 4 5; do \
    wget -q --show-progress \
      -O /comfyui/ComfyUI/models/loras/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors \
      https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/Lightx2v/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors \
      && break || { echo "Retry $i..."; sleep $((i*15)); }; \
    done

# LoRA — SVI Pro HIGH
RUN for i in 1 2 3 4 5; do \
    wget -q --show-progress \
      -O /comfyui/ComfyUI/models/loras/SVI_v2_PRO_Wan2.2-I2V-A14B_HIGH_lora_rank_128_fp16.safetensors \
      https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/LoRAs/Stable-Video-Infinity/v2.0/SVI_v2_PRO_Wan2.2-I2V-A14B_HIGH_lora_rank_128_fp16.safetensors \
      && break || { echo "Retry $i..."; sleep $((i*15)); }; \
    done

# LoRA — SVI Pro LOW
RUN for i in 1 2 3 4 5; do \
    wget -q --show-progress \
      -O /comfyui/ComfyUI/models/loras/SVI_v2_PRO_Wan2.2-I2V-A14B_LOW_lora_rank_128_fp16.safetensors \
      https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/LoRAs/Stable-Video-Infinity/v2.0/SVI_v2_PRO_Wan2.2-I2V-A14B_LOW_lora_rank_128_fp16.safetensors \
      && break || { echo "Retry $i..."; sleep $((i*15)); }; \
    done

# Text encoder — UMT5 XXL fp8
RUN for i in 1 2 3 4 5; do \
    wget -q --show-progress \
      -O /comfyui/ComfyUI/models/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors \
      https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors \
      && break || { echo "Retry $i..."; sleep $((i*15)); }; \
    done

# VAE
RUN for i in 1 2 3 4 5; do \
    wget -q --show-progress \
      -O /comfyui/ComfyUI/models/vae/Wan2_1_VAE_bf16.safetensors \
      https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/vae/wan_2.1_vae.safetensors \
      && break || { echo "Retry $i..."; sleep $((i*15)); }; \
    done

# =============================================================================
# 9. SEED INPUT IMAGE
# =============================================================================
RUN mkdir -p /comfyui/ComfyUI/input && \
    wget -q --show-progress \
      -O /comfyui/ComfyUI/input/first-frame.png \
      "https://cool-anteater-319.convex.cloud/api/storage/0c172877-f42a-4fa0-89ea-d40d82991fa6"

ENTRYPOINT ["/bin/bash", "/start.sh"]

# ==============================================================================
# 1. BASE IMAGE & BUILD ARGUMENTS
# ==============================================================================

# Define the default base CUDA image with Ubuntu 24.04 and cuDNN runtime
ARG BASE_IMAGE=nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04
FROM ${BASE_IMAGE} AS base

# Build-time arguments for customizing ComfyUI version and PyTorch upgrades
ARG COMFYUI_VERSION=
ARG ENABLE_PYTORCH_UPGRADE=false
ARG PYTORCH_INDEX_URL

# Configure environment variables to streamline non-interactive installations
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_PREFER_BINARY=1 \
    PYTHONUNBUFFERED=1 \
    CMAKE_BUILD_PARALLEL_LEVEL=8 \
    PIP_NO_INPUT=1

# ==============================================================================
# 2. SYSTEM DEPENDENCIES & PYTHON SETUP
# ==============================================================================

# Install required system packages, Python 3.12, and multimedia libraries (OpenCV/FFmpeg dependencies)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.12 \
    python3.12-venv \
    python3.12-dev \
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
    && ln -sf /usr/bin/python3.12 /usr/bin/python

# Install Astral 'uv' for blisteringly fast Python packaging and dependency management
RUN wget -qO- https://astral.sh/uv/install.sh | sh \
    && ln -s /root/.local/bin/uv /usr/local/bin/uv \
    && ln -s /root/.local/bin/uvx /usr/local/bin/uvx

# Create the core workspace directory and initialize the Python virtual environment
RUN mkdir -p /comfyui
RUN uv venv /comfyui/.venv --seed
ENV PATH="/comfyui/.venv/bin:${PATH}"

# ==============================================================================
# 3. COMFYUI CORE INSTALLATION
# ==============================================================================

# Install the essential CLI and client tools for RunPod automation
RUN uv pip install comfy-cli runpod requests websocket-client

# Install the baseline PyTorch dependencies targeted for CUDA 12.8 compatibility
RUN uv pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128

# Clone the main ComfyUI repository (shallow clone to save layer space)
RUN git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git /comfyui/ComfyUI

# Install core ComfyUI python dependencies
RUN uv pip install -r /comfyui/ComfyUI/requirements.txt

# Set directory variables and switch working directory contexts
ENV COMFYUI_DIR=/comfyui
WORKDIR /

# ==============================================================================
# 4. CONFIGURATIONS & AUTOMATION SCRIPTS
# ==============================================================================

# Inject custom model paths so ComfyUI knows where to find network or local storage volumes
ADD src/extra_model_paths.yaml /comfyui/extra_model_paths.yaml
ADD src/extra_model_paths.yaml /comfyui/ComfyUI/extra_model_paths.yaml

# Copy server initialization handlers and internal workflows
ADD src/start.sh src/network_volume.py src/handler.py ./
COPY src/workflow.json /workflow.json
RUN chmod +x /start.sh

# Copy utility scripts for managing custom extension nodes and execution modes
COPY scripts/comfy-node-install.sh /usr/local/bin/comfy-node-install
RUN chmod +x /usr/local/bin/comfy-node-install

COPY scripts/comfy-manager-set-mode.sh /usr/local/bin/comfy-manager-set-mode
RUN chmod +x /usr/local/bin/comfy-manager-set-mode

# ==============================================================================
# 5. CUSTOM EXTENSION NODES & DEPENDENCIES
# ==============================================================================

# Prepare the custom nodes folder
RUN mkdir -p /comfyui/custom_nodes

# Ensure core pip, setuptools, and wheel packages are up-to-date in the venv
RUN uv pip install --python /comfyui/.venv/bin/python --no-cache pip setuptools wheel

# Force-reinstall PyTorch suite to guarantee correct bindings before installing secondary packages
RUN uv pip install --python /comfyui/.venv/bin/python --force-reinstall torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128

# Install an array of multimedia, mathematical, and model pipeline packages
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
    transformers\
    sageattention

# Clone Custom Node: KJNodes (General utilities and adjustments)
RUN git clone --depth 1 https://github.com/kijai/ComfyUI-KJNodes.git /comfyui/custom_nodes/ComfyUI-KJNodes && \
    if [ -f /comfyui/custom_nodes/ComfyUI-KJNodes/requirements.txt ]; then \
        /comfyui/.venv/bin/python -m pip install --no-cache-dir -r /comfyui/custom_nodes/ComfyUI-KJNodes/requirements.txt; \
    fi

# Clone Custom Node: VideoHelperSuite (Video loading, processing, and saving tools)
RUN git clone --depth 1 https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git /comfyui/custom_nodes/ComfyUI-VideoHelperSuite && \
    if [ -f /comfyui/custom_nodes/ComfyUI-VideoHelperSuite/requirements.txt ]; then \
        /comfyui/.venv/bin/python -m pip install --no-cache-dir -r /comfyui/custom_nodes/ComfyUI-VideoHelperSuite/requirements.txt; \
    fi

# Clone Custom Node: WanVideoWrapper (Wrapper nodes supporting the WanVideo models)
RUN git clone --depth 1 https://github.com/kijai/ComfyUI-WanVideoWrapper.git /comfyui/custom_nodes/ComfyUI-WanVideoWrapper && \
    if [ -f /comfyui/custom_nodes/ComfyUI-WanVideoWrapper/requirements.txt ]; then \
        /comfyui/.venv/bin/python -m pip install --no-cache-dir -r /comfyui/custom_nodes/ComfyUI-WanVideoWrapper/requirements.txt; \
    fi

# Clone Custom Node: Logic Nodes (Conditional/Logical flow control for ComfyUI)
RUN git clone --depth 1 https://github.com/theUpsider/ComfyUI-Logic.git /comfyui/custom_nodes/ComfyUI-Logic && \
    if [ -f /comfyui/custom_nodes/ComfyUI-Logic/requirements.txt ]; then \
        /comfyui/.venv/bin/python -m pip install --no-cache-dir -r /comfyui/custom_nodes/ComfyUI-Logic/requirements.txt; \
    fi

# Clone Custom Node: Wan-SVI2Pro-FLF (Inference optimizations for Wan architecture)
RUN git clone --depth 1 https://github.com/Well-Made/ComfyUI-Wan-SVI2Pro-FLF.git /comfyui/custom_nodes/ComfyUI-Wan-SVI2Pro-FLF && \
    if [ -f /comfyui/custom_nodes/ComfyUI-Wan-SVI2Pro-FLF/requirements.txt ]; then \
        /comfyui/.venv/bin/python -m pip install --no-cache-dir -r /comfyui/custom_nodes/ComfyUI-Wan-SVI2Pro-FLF/requirements.txt; \
    fi

# ==============================================================================
# 6. MODEL DOWNLOADS (WITH RETRY/BACKOFF ASSURANCE)
# ==============================================================================
# The loops below implement an exponential backoff retry mechanism to shield 
# the image build from transient Hugging Face connection drops.

# --- LORAS ---

# Download: Wan2.2 Image-to-Video Lightx2v 4-step LoRA (rank 64, bf16)
RUN BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do \
    comfy --workspace /comfyui model download \
      --url 'https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/LoRAs/Wan22_Lightx2v/Wan_2_2_I2V_A14B_HIGH_lightx2v_4step_lora_v1030_rank_64_bf16.safetensors' \
      --relative-path models/loras \
      --filename 'Wan_2_2_I2V_A14B_HIGH_lightx2v_4step_lora_v1030_rank_64_bf16.safetensors' && break; \
    if [ $i -eq 5 ]; then echo "model-download failed" >&2; exit 1; fi; \
    SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i); sleep $SLEEP; done

# Download: Stable Video Infinity (SVI) v2.0 PRO High Quality LoRA (rank 128, fp16)
RUN BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do \
    comfy --workspace /comfyui model download \
      --url 'https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/LoRAs/Stable-Video-Infinity/v2.0/SVI_v2_PRO_Wan2.2-I2V-A14B_HIGH_lora_rank_128_fp16.safetensors' \
      --relative-path models/loras \
      --filename 'SVI_v2_PRO_Wan2.2-I2V-A14B_HIGH_lora_rank_128_fp16.safetensors' && break; \
    if [ $i -eq 5 ]; then echo "model-download failed" >&2; exit 1; fi; \
    SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i); sleep $SLEEP; done

# Download: Lightx2v Config Step Distillation LoRA (rank 64, bf16)
RUN BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do \
    comfy --workspace /comfyui model download \
      --url 'https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/Lightx2v/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors' \
      --relative-path models/loras \
      --filename 'lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors' && break; \
    if [ $i -eq 5 ]; then echo "model-download failed" >&2; exit 1; fi; \
    SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i); sleep $SLEEP; done

# Download: Stable Video Infinity (SVI) v2.0 PRO Low Quality LoRA (rank 128, fp16)
RUN BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do \
    comfy --workspace /comfyui model download \
      --url 'https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/LoRAs/Stable-Video-Infinity/v2.0/SVI_v2_PRO_Wan2.2-I2V-A14B_LOW_lora_rank_128_fp16.safetensors' \
      --relative-path models/loras \
      --filename 'SVI_v2_PRO_Wan2.2-I2V-A14B_LOW_lora_rank_128_fp16.safetensors' && break; \
    if [ $i -eq 5 ]; then echo "model-download failed" >&2; exit 1; fi; \
    SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i); sleep $SLEEP; done

# --- TEXT ENCODERS ---

# Download: Repackaged UMT5 XXL Text Encoder (fp8, e4m3fn scaled format)
RUN BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do \
    comfy --workspace /comfyui model download \
      --url 'https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors' \
      --relative-path models/text_encoders \
      --filename 'umt5_xxl_fp8_e4m3fn_scaled.safetensors' && break; \
    if [ $i -eq 5 ]; then echo "model-download failed" >&2; exit 1; fi; \
    SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i); sleep $SLEEP; done

# --- VAE ---

# Download: Wan 2.1 VAE configuration file (saved as bf16)
RUN BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do \
    comfy --workspace /comfyui model download \
      --url 'https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/vae/wan_2.1_vae.safetensors' \
      --relative-path models/vae \
      --filename 'Wan2_1_VAE_bf16.safetensors' && break; \
    if [ $i -eq 5 ]; then echo "model-download failed" >&2; exit 1; fi; \
    SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i); sleep $SLEEP; done

# --- DIFFUSION MODELS ---

# Download: Wan2.2 Image-to-Video 14B HIGH Definition Checkpoint (fp8 scaled)
RUN BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do \
    comfy --workspace /comfyui model download \
      --url 'https://huggingface.co/Kijai/WanVideo_comfy_fp8_scaled/resolve/main/I2V/Wan2_2-I2V-A14B-HIGH_fp8_e4m3fn_scaled_KJ.safetensors' \
      --relative-path models/diffusion_models \
      --filename 'Wan2_2-I2V-A14B-HIGH_fp8_e4m3fn_scaled_KJ.safetensors' && break; \
    if [ $i -eq 5 ]; then echo "model-download failed" >&2; exit 1; fi; \
    SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i); sleep $SLEEP; done

# Download: Wan2.2 Image-to-Video 14B LOW Definition Checkpoint (fp8 scaled)
RUN BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do \
    comfy --workspace /comfyui model download \
      --url 'https://huggingface.co/Kijai/WanVideo_comfy_fp8_scaled/resolve/main/I2V/Wan2_2-I2V-A14B-LOW_fp8_e4m3fn_scaled_KJ.safetensors' \
      --relative-path models/diffusion_models \
      --filename 'Wan2_2-I2V-A14B-LOW_fp8_e4m3fn_scaled_KJ.safetensors' && break; \
    if [ $i -eq 5 ]; then echo "model-download failed" >&2; exit 1; fi; \
    SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i); sleep $SLEEP; done

# ==============================================================================
# 7. RUNTIME ENVIRONMENT SETUP & ENTRYPOINT
# ==============================================================================

# Create default input directory and pre-cache a placeholder input initialization image
RUN mkdir -p /comfyui/input
RUN wget --progress=dot:giga \
    -O '/comfyui/input/Gemini_Generated_Image_jk7o1njk7o1njk7o.png' \
    "https://cool-anteater-319.convex.cloud/api/storage/0c172877-f42a-4fa0-89ea-d40d82991fa6"

# RunPod entrypoint array execution
ENTRYPOINT ["/bin/bash", "/start.sh"]

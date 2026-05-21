variable "DOCKERHUB_REPO" {
  default = "geoffmccabe"
}
variable "DOCKERHUB_IMG" {
  default = "worker-wan22"
}
variable "RELEASE_VERSION" {
  default = "latest"
}
variable "COMFYUI_VERSION" {
  default = "latest"
}
variable "BASE_IMAGE" {
  default = "nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04"
}
variable "CUDA_VERSION_FOR_COMFY" {
  default = "12.6"
}
variable "ENABLE_PYTORCH_UPGRADE" {
  default = "false"
}
variable "PYTORCH_INDEX_URL" {
  default = ""
}
variable "HUGGINGFACE_ACCESS_TOKEN" {
  default = ""
}

group "default" {
  targets = ["base"]
}

target "base" {
  context    = "."
  dockerfile = "Dockerfile"
  target     = "base"
  platforms  = ["linux/amd64"]
  args = {
    BASE_IMAGE             = "${BASE_IMAGE}"
    COMFYUI_VERSION        = "${COMFYUI_VERSION}"
    CUDA_VERSION_FOR_COMFY = "${CUDA_VERSION_FOR_COMFY}"
    ENABLE_PYTORCH_UPGRADE = "${ENABLE_PYTORCH_UPGRADE}"
    PYTORCH_INDEX_URL      = "${PYTORCH_INDEX_URL}"
    MODEL_TYPE             = "base"
  }
}

target "base-cuda12-8-1" {
  context    = "."
  dockerfile = "Dockerfile"
  target     = "base"
  platforms  = ["linux/amd64"]
  args = {
    BASE_IMAGE             = "nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04"
    COMFYUI_VERSION        = "${COMFYUI_VERSION}"
    CUDA_VERSION_FOR_COMFY = ""
    ENABLE_PYTORCH_UPGRADE = "true"
    PYTORCH_INDEX_URL      = "https://download.pytorch.org/whl/cu128"
    MODEL_TYPE             = "base"
  }
}

#!/usr/bin/env bash
set -euo pipefail

echo "[start.sh] Booting container..."

COMFY_PORT="8188"
COMFY_HTTP="http://127.0.0.1:${COMFY_PORT}"
# FIX 1: Point to the correct virtual environment path created by uv
PY="/comfyui/.venv/bin/python"

echo "[start.sh] COMFY_HTTP=${COMFY_HTTP}"
echo "[start.sh] PY=${PY}"

echo "[start.sh] Starting ComfyUI via comfy-cli (workspace: /comfyui/ComfyUI)..."
# FIX 2: Point workspace to the exact directory where main.py exists
comfy --workspace /comfyui/ComfyUI launch -- --listen 0.0.0.0 --port ${COMFY_PORT} &
COMFY_PID=$!

echo "[start.sh] Waiting for ComfyUI to become ready..."
for i in $(seq 1 240); do
  if curl -fsS "${COMFY_HTTP}/system_stats" >/dev/null 2>&1; then
    echo "[start.sh] ComfyUI is ready."
    break
  fi
  sleep 1
done

if ! curl -fsS "${COMFY_HTTP}/system_stats" >/dev/null 2>&1; then
  echo "[start.sh] ERROR: ComfyUI did not become ready in time."
  echo "[start.sh] ComfyUI PID=${COMFY_PID}"
  exit 1
fi

echo "[start.sh] Starting RunPod handler..."
# This executes your handler using the correct python interpreter from your root directory
exec ${PY} -u /handler.py

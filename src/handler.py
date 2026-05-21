import os
import time
import json
import base64
import requests
import runpod

COMFY_HOST = os.environ.get("COMFY_HOST", "127.0.0.1")
COMFY_PORT = int(os.environ.get("COMFY_PORT", "8188"))
COMFY_BASE = f"http://{COMFY_HOST}:{COMFY_PORT}"
COMFY_READY_TIMEOUT = int(os.environ.get("COMFY_READY_TIMEOUT", "180"))
COMFY_READY_POLL = 1.0

# ─────────────────────────────────────────────────────────────────────────────
# Registry / LoRA config
# All three things must agree on these two values:
#   1. This file (REGISTRY_REL / LORA_DIR_REL)
#   2. tools/check_lora_sidecars.py  (LORA_DIR)
#   3. src/extra_model_paths.yaml    (loras path)
# ─────────────────────────────────────────────────────────────────────────────
LORA_DIR_REL   = "models/loras"                          # was: models/lora-video
REGISTRY_REL   = f"{LORA_DIR_REL}/registry.json"        # was: registry.generated.json

REGISTRY_CANDIDATE_BASES = [
    "/workspace/criminal_jade_guineafowl",
    "/workspace",
    "/criminal_jade_guineafowl",
    "/runpod-volume",
    "/workspace/runpod-volume",
]

COMFY_OUTPUT_DIRS = [
    "/comfyui/ComfyUI/output",
    "/comfyui/output",
    "/root/comfyui/output",
]

COMFY_LOG_CANDIDATES = [
    "/comfyui/user/comfyui.log",
    "/comfyui/ComfyUI/user/comfyui.log",
]

# ─────────────────────────────────────────────────────────────────────────────
# ComfyUI readiness
# ─────────────────────────────────────────────────────────────────────────────
_comfy_ready = False

def wait_for_comfy():
    global _comfy_ready
    if _comfy_ready:
        return
    start = time.time()
    last_err = None
    while time.time() - start < COMFY_READY_TIMEOUT:
        try:
            r = requests.get(f"{COMFY_BASE}/system_stats", timeout=2)
            if r.status_code == 200:
                _comfy_ready = True
                return
        except Exception as e:
            last_err = e
        time.sleep(COMFY_READY_POLL)
    raise RuntimeError(f"ComfyUI did not become ready in {COMFY_READY_TIMEOUT}s: {last_err}")

def comfy_get(path):
    r = requests.get(f"{COMFY_BASE}{path}", timeout=30)
    r.raise_for_status()
    return r.json()

# ─────────────────────────────────────────────────────────────────────────────
# Image upload  (BUG 5 FIX)
# ─────────────────────────────────────────────────────────────────────────────
def upload_images_to_comfy(images):
    """
    Upload base64 images to ComfyUI's /upload/image endpoint.
    Returns list of filenames that ComfyUI stored them under.
    """
    uploaded = []
    for img in images:
        name  = img["name"]
        b64   = img["image"]
        if "," in b64:                     # strip data-URI prefix if present
            b64 = b64.split(",", 1)[1]
        image_bytes = base64.b64decode(b64)
        resp = requests.post(
            f"{COMFY_BASE}/upload/image",
            files={"image": (name, image_bytes, "image/png")},
            data={"overwrite": "true"},
            timeout=60,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Image upload failed for {name}: HTTP {resp.status_code} | {resp.text[:500]}")
        result = resp.json()
        stored_name = result.get("name", name)
        uploaded.append(stored_name)
    return uploaded

# ─────────────────────────────────────────────────────────────────────────────
# Workflow patching  (BUG 5 FIX)
# ─────────────────────────────────────────────────────────────────────────────
def patch_workflow_image(workflow, uploaded_filename):
    """
    Walk the workflow nodes and replace the __INPUT_IMAGE__ placeholder
    in any LoadImage node with the actual uploaded filename.
    """
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        if node.get("class_type") == "LoadImage":
            inputs = node.get("inputs", {})
            for k, v in inputs.items():
                if isinstance(v, str) and "__INPUT_IMAGE__" in v:
                    inputs[k] = uploaded_filename
    return workflow

# ─────────────────────────────────────────────────────────────────────────────
# Prompt submission + history polling
# ─────────────────────────────────────────────────────────────────────────────
def submit_prompt(prompt):
    r = requests.post(f"{COMFY_BASE}/prompt", json={"prompt": prompt}, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"ComfyUI /prompt failed: HTTP {r.status_code} | {r.text[:4000]}")
    return r.json()

def wait_for_history(prompt_id, poll_interval=1.0, timeout=600):
    start = time.time()
    while time.time() - start < timeout:
        r = requests.get(f"{COMFY_BASE}/history/{prompt_id}", timeout=30)
        r.raise_for_status()
        data = r.json()
        if prompt_id in data:
            return data[prompt_id]
        time.sleep(poll_interval)
    raise RuntimeError(f"Prompt {prompt_id} did not finish within {timeout}s")

# ─────────────────────────────────────────────────────────────────────────────
# Output extraction  (BUG 1 FIX)
# ─────────────────────────────────────────────────────────────────────────────
def find_output_dir():
    for d in COMFY_OUTPUT_DIRS:
        if os.path.isdir(d):
            return d
    return COMFY_OUTPUT_DIRS[0]  # fallback — may not exist yet

def extract_outputs(history):
    """
    Pull all image/video files out of the ComfyUI history object.
    Returns a list of dicts: [{filename, type, data}]
    matching the standard worker-comfyui v5+ output format.
    """
    outputs = []
    output_dir = find_output_dir()

    for node_id, node_output in history.get("outputs", {}).items():
        # Images (still frames, output PNGs)
        for img in node_output.get("images", []):
            fname   = img.get("filename", "")
            subfolder = img.get("subfolder", "")
            ftype   = img.get("type", "output")
            if ftype == "temp":           # skip intermediate temp files
                continue
            fpath = os.path.join(output_dir, subfolder, fname) if subfolder else os.path.join(output_dir, fname)
            if not os.path.isfile(fpath):
                continue
            with open(fpath, "rb") as f:
                data = base64.b64encode(f.read()).decode("utf-8")
            outputs.append({"filename": fname, "type": "base64", "data": data})

        # Videos (VHS_VideoCombine, etc.)
        for vid in node_output.get("videos", []):
            fname   = vid.get("filename", "")
            subfolder = vid.get("subfolder", "")
            ftype   = vid.get("type", "output")
            if ftype == "temp":
                continue
            fpath = os.path.join(output_dir, subfolder, fname) if subfolder else os.path.join(output_dir, fname)
            if not os.path.isfile(fpath):
                continue
            with open(fpath, "rb") as f:
                data = base64.b64encode(f.read()).decode("utf-8")
            outputs.append({"filename": fname, "type": "base64", "data": data})

    return outputs

# ─────────────────────────────────────────────────────────────────────────────
# Registry helpers (unchanged logic, corrected paths)
# ─────────────────────────────────────────────────────────────────────────────
def safe_listdir(path, limit=200):
    try:
        items = sorted(os.listdir(path))[:limit]
        return {"path": path, "exists": True, "items": items}
    except FileNotFoundError:
        return {"path": path, "exists": False, "items": []}
    except Exception as e:
        return {"path": path, "exists": True, "error": str(e), "items": []}

def tail_file(path, max_bytes=8000):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes), os.SEEK_SET)
            data = f.read().decode("utf-8", errors="replace")
        return {"path": path, "exists": True, "tail": data}
    except FileNotFoundError:
        return {"path": path, "exists": False, "tail": ""}
    except Exception as e:
        return {"path": path, "exists": True, "error": str(e), "tail": ""}

def get_env_registry_path():
    return os.environ.get("LORA_REGISTRY_PATH", "").strip()

def registry_candidates():
    paths = []
    env_path = get_env_registry_path()
    if env_path:
        paths.append(env_path)
    for b in REGISTRY_CANDIDATE_BASES:
        paths.append(f"{b}/{REGISTRY_REL}")
    out, seen = [], set()
    for p in paths:
        if p not in seen:
            out.append(p)
            seen.add(p)
    return out

def resolve_registry_path():
    for p in registry_candidates():
        if os.path.exists(p):
            return p
    tried = "\n".join([f"- {p} (exists={os.path.exists(p)})" for p in registry_candidates()])
    raise RuntimeError(
        "LoRA registry not found.\nTried:\n" + tried + "\n\n"
        "Fix: attach the network volume and set LORA_REGISTRY_PATH env var, "
        "or ensure models/loras/registry.json exists on the volume."
    )

def load_registry():
    path = resolve_registry_path()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data["_resolved_registry_path"] = path
    return data

# ─────────────────────────────────────────────────────────────────────────────
# Main handler
# ─────────────────────────────────────────────────────────────────────────────
def handler(job):
    payload = job.get("input") or {}
    action  = payload.get("action")

    # ── Utility actions (no ComfyUI needed) ──────────────────────────────────
    if action == "ping":
        return {"status": "ok"}

    if action == "diag":
        diag = {
            "env": {
                "LORA_REGISTRY_PATH": os.environ.get("LORA_REGISTRY_PATH"),
                "COMFY_HOST":         os.environ.get("COMFY_HOST"),
                "COMFY_PORT":         os.environ.get("COMFY_PORT"),
            },
            "mount_listings": [
                safe_listdir("/"),
                safe_listdir("/workspace"),
                safe_listdir("/workspace/criminal_jade_guineafowl"),
                safe_listdir("/runpod-volume"),
                safe_listdir(f"/runpod-volume/{LORA_DIR_REL}"),
            ],
            "registry_candidates": [
                {"path": p, "exists": os.path.exists(p)} for p in registry_candidates()
            ],
            "output_dirs": [
                {"path": d, "exists": os.path.isdir(d)} for d in COMFY_OUTPUT_DIRS
            ],
            "comfy_log_tail": [tail_file(p) for p in COMFY_LOG_CANDIDATES],
        }
        try:
            wait_for_comfy()
            diag["comfy_system_stats"] = comfy_get("/system_stats")
        except Exception as e:
            diag["comfy_system_stats_error"] = str(e)
        return diag

    if action == "registry":
        return load_registry()

    if action == "comfy_system_stats":
        wait_for_comfy()
        return comfy_get("/system_stats")

    if action == "comfy_object_info":
        wait_for_comfy()
        return comfy_get("/object_info")

    # ── Main inference path ───────────────────────────────────────────────────
    wait_for_comfy()

    workflow = payload.get("workflow") or payload.get("prompt")
    if not workflow:
        raise ValueError(
            "Missing 'workflow' (or 'prompt') in job input. "
            "Pass the ComfyUI API-format workflow JSON under input.workflow."
        )

    # Upload input images and patch the workflow (BUG 5 FIX)
    images = payload.get("images", [])
    if images:
        uploaded = upload_images_to_comfy(images)
        # Patch the primary input image into LoadImage nodes
        if uploaded:
            workflow = patch_workflow_image(workflow, uploaded[0])

    result    = submit_prompt(workflow)
    prompt_id = result.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI did not return a prompt_id. Response: {result}")

    history  = wait_for_history(prompt_id)
    outputs  = extract_outputs(history)            # BUG 1 FIX

    errors = []
    if not outputs:
        errors.append("No output files found after job completed — check ComfyUI logs via action=diag")

    response = {"images": outputs}
    if errors:
        response["errors"] = errors
    return response


runpod.serverless.start({"handler": handler})

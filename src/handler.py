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
# Supabase signed upload
# ─────────────────────────────────────────────────────────────────────────────
SIGNED_UPLOAD_ENDPOINT: str = os.environ.get(
    "SIGNED_UPLOAD_ENDPOINT",
    "https://kabdqrzcewkzbjmeqmxx.supabase.co/functions/v1/runpod-signed-upload",
)
RUNPOD_UPLOAD_SECRET: str = os.environ.get(
    "RUNPOD_UPLOAD_SECRET",
    "67mN2pQ9xR4vT8wY3zA5bC6dE1fG0hJ4kL8nM2oP6qS9t",
)

# ─────────────────────────────────────────────────────────────────────────────
# Registry / LoRA config
# ─────────────────────────────────────────────────────────────────────────────
LORA_DIR_REL = "models/loras"
REGISTRY_REL = f"{LORA_DIR_REL}/registry.json"
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
NODE_PACK_HINTS = {
    "WanVideoModelLoader":          "ComfyUI-WanVideoWrapper",
    "WanVideoTextEncode":           "ComfyUI-WanVideoWrapper",
    "WanVideoTextEmbedBridge":      "ComfyUI-WanVideoWrapper",
    "WanVideoVAELoader":            "ComfyUI-WanVideoWrapper",
    "WanVideoEncode":               "ComfyUI-WanVideoWrapper",
    "WanVideoDecode":               "ComfyUI-WanVideoWrapper",
    "WanVideoSampler":              "ComfyUI-WanVideoWrapper",
    "WanVideoSLG":                  "ComfyUI-WanVideoWrapper",
    "WanVideoEasyCache":            "ComfyUI-WanVideoWrapper",
    "WanVideoExperimentalArgs":     "ComfyUI-WanVideoWrapper",
    "WanVideoTorchCompileSettings": "ComfyUI-WanVideoWrapper",
    "LoadWanVideoT5TextEncoder":    "ComfyUI-WanVideoWrapper",
    "WanVideoSetBlockSwap":         "ComfyUI-WanVideoWrapper",
    "ImageResizeKJv2":              "ComfyUI-KJNodes",
    "VHS_VideoCombine":             "ComfyUI-VideoHelperSuite",
}
OUTPUT_NODE_TYPES = {
    "SaveImage", "SaveAnimatedWEBP", "SaveAnimatedPNG",
    "SaveAnimatedGIF", "SaveVideo", "VHS_VideoCombine", "PreviewImage",
}

# ─────────────────────────────────────────────────────────────────────────────
# ComfyUI readiness
# ─────────────────────────────────────────────────────────────────────────────
_comfy_ready = False
_object_info_cache = None

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

def get_object_info():
    global _object_info_cache
    if _object_info_cache is None:
        _object_info_cache = comfy_get("/object_info")
    return _object_info_cache

# ─────────────────────────────────────────────────────────────────────────────
# Workflow helpers
# ─────────────────────────────────────────────────────────────────────────────
def deep_walk(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from deep_walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from deep_walk(v)
    elif isinstance(obj, str):
        yield obj

def recursive_replace(obj, replacements):
    if isinstance(obj, dict):
        return {k: recursive_replace(v, replacements) for k, v in obj.items()}
    if isinstance(obj, list):
        return [recursive_replace(v, replacements) for v in obj]
    if isinstance(obj, str):
        out = obj
        for old, new in replacements.items():
            if old in out:
                out = out.replace(old, new)
        return out
    return obj

def workflow_class_types(workflow):
    return [
        node["class_type"]
        for node in workflow.values()
        if isinstance(node, dict) and "class_type" in node
    ]

def workflow_has_output_node(workflow):
    return any(
        isinstance(node, dict) and node.get("class_type") in OUTPUT_NODE_TYPES
        for node in workflow.values()
    )

def validate_workflow(workflow):
    if not isinstance(workflow, dict):
        raise ValueError("Workflow must be a dict of ComfyUI nodes.")
    if not workflow_has_output_node(workflow):
        present = sorted(set(workflow_class_types(workflow)))
        raise RuntimeError(
            "Workflow has no output node. Need one of: "
            + ", ".join(sorted(OUTPUT_NODE_TYPES))
            + ". Present: " + ", ".join(present)
        )
    installed = set(get_object_info().keys())
    missing = [cls for cls in sorted(set(workflow_class_types(workflow))) if cls not in installed]
    if missing:
        hinted = [
            f"{cls} -> install {NODE_PACK_HINTS[cls]}" if cls in NODE_PACK_HINTS else cls
            for cls in missing
        ]
        raise RuntimeError("Missing custom node type(s):\n- " + "\n- ".join(hinted))

def patch_workflow_inputs(workflow, uploaded_filename=None, prompt=None, negative_prompt=None):
    replacements = {}
    if uploaded_filename:
        replacements["__INPUT_IMAGE__.png"] = uploaded_filename
        replacements["__INPUT_IMAGE__"]     = uploaded_filename
    if prompt is not None:
        replacements["__PROMPT__"] = prompt
    if negative_prompt is not None:
        replacements["__NEG_PROMPT__"] = negative_prompt
    return recursive_replace(workflow, replacements)

# ─────────────────────────────────────────────────────────────────────────────
# Image input — URL download or base64
# ─────────────────────────────────────────────────────────────────────────────
def fetch_image_from_url(url: str, filename: str = "input_image.png") -> str:
    resp = requests.get(url, timeout=60)
    if resp.status_code >= 400:
        raise RuntimeError(f"Failed to download image from {url}: HTTP {resp.status_code}")
    image_bytes = resp.content
    ct = resp.headers.get("Content-Type", "")
    if "jpeg" in ct or "jpg" in ct:
        filename = filename.replace(".png", ".jpg")
    elif "webp" in ct:
        filename = filename.replace(".png", ".webp")
    upload_resp = requests.post(
        f"{COMFY_BASE}/upload/image",
        files={"image": (filename, image_bytes, ct or "image/png")},
        data={"overwrite": "true"},
        timeout=60,
    )
    if upload_resp.status_code >= 400:
        raise RuntimeError(f"ComfyUI upload failed: {upload_resp.text[:500]}")
    return upload_resp.json().get("name", filename)

def resolve_input_image(payload: dict):
    for key in ("image_url", "source_url", "target_url"):
        url = payload.get(key)
        if url:
            filename = f"{key.replace('_url','')}_image.png"
            return fetch_image_from_url(url, filename)
    images = payload.get("images", [])
    if images:
        uploaded = upload_images_to_comfy(images)
        if uploaded:
            return uploaded[0]
    return None

def upload_images_to_comfy(images):
    uploaded = []
    for img in images:
        name = img["name"]
        b64  = img["image"]
        if "," in b64:
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
        uploaded.append(resp.json().get("name", name))
    return uploaded

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
# Output — stream directly from disk to Supabase (NO base64 in memory)
# ─────────────────────────────────────────────────────────────────────────────
def find_output_dir():
    for d in COMFY_OUTPUT_DIRS:
        if os.path.isdir(d):
            return d
    return COMFY_OUTPUT_DIRS[0]

def get_output_filepaths(history):
    """Return list of {filename, filepath} from ComfyUI history — no base64."""
    output_dir = find_output_dir()
    files = []
    for _, node_output in history.get("outputs", {}).items():
        for key in ("images", "videos", "gifs"):
            for item in node_output.get(key, []):
                if item.get("type") == "temp":
                    continue
                fname     = item.get("filename", "")
                subfolder = item.get("subfolder", "")
                fpath = os.path.join(output_dir, subfolder, fname) if subfolder else os.path.join(output_dir, fname)
                if os.path.isfile(fpath):
                    files.append({"filename": fname, "filepath": fpath})
    return files

def upload_output_to_supabase(filename: str, filepath: str) -> str:
    """
    Stream file DIRECTLY from disk to Supabase — no base64, no memory bloat.
    timeout=300s (5 minutes) instead of the old broken 120s.
    """
    file_size = os.path.getsize(filepath)
    print(f"Uploading {filename} ({file_size/1024/1024:.1f}MB) to Supabase...")
    with open(filepath, "rb") as f:
        resp = requests.post(
            SIGNED_UPLOAD_ENDPOINT,
            headers={
                "Authorization": f"Bearer {RUNPOD_UPLOAD_SECRET}",
                "x-filename": filename,
                "Content-Type": "video/mp4",
                "Content-Length": str(file_size),
            },
            data=f,       # stream directly from file handle — no memory bloat
            timeout=300,  # 5 minutes
        )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Supabase upload failed for {filename}: "
            f"HTTP {resp.status_code} | {resp.text[:500]}"
        )
    result = resp.json()
    url = result.get("url") or result.get("publicUrl") or result.get("public_url")
    if not url:
        raise RuntimeError(f"Supabase response missing URL. Got: {result}")
    print(f"Upload success: {url}")
    return url

def extract_and_upload_outputs(history) -> list:
    """
    Stream each output file from disk directly to Supabase.
    Returns [{filename, type, url}].
    Falls back to base64 only if upload fails so job never dies silently.
    """
    output_files = get_output_filepaths(history)
    results = []
    for item in output_files:
        fname = item["filename"]
        fpath = item["filepath"]
        try:
            url = upload_output_to_supabase(fname, fpath)
            results.append({"filename": fname, "type": "url", "url": url})
        except Exception as e:
            print(f"Upload failed for {fname}: {e} — falling back to base64")
            with open(fpath, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            results.append({
                "filename": fname,
                "type": "base64",
                "data": b64,
                "upload_error": str(e),
            })
    return results

# ─────────────────────────────────────────────────────────────────────────────
# Registry helpers
# ─────────────────────────────────────────────────────────────────────────────
def safe_listdir(path, limit=200):
    try:
        return {"path": path, "exists": True, "items": sorted(os.listdir(path))[:limit]}
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

def registry_candidates():
    paths = []
    env_path = os.environ.get("LORA_REGISTRY_PATH", "").strip()
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
    tried = "\n".join(f"- {p}" for p in registry_candidates())
    raise RuntimeError(f"LoRA registry not found.\nTried:\n{tried}")

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

    if action == "ping":
        return {"status": "ok"}

    if action == "diag":
        diag = {
            "env": {
                "LORA_REGISTRY_PATH":     os.environ.get("LORA_REGISTRY_PATH"),
                "COMFY_HOST":             os.environ.get("COMFY_HOST"),
                "COMFY_PORT":             os.environ.get("COMFY_PORT"),
                "SIGNED_UPLOAD_ENDPOINT": SIGNED_UPLOAD_ENDPOINT,
            },
            "mount_listings": [
                safe_listdir("/"),
                safe_listdir("/workspace"),
                safe_listdir("/workspace/criminal_jade_guineafowl"),
                safe_listdir("/runpod-volume"),
                safe_listdir(f"/runpod-volume/{LORA_DIR_REL}"),
            ],
            "registry_candidates": [{"path": p, "exists": os.path.exists(p)} for p in registry_candidates()],
            "output_dirs":         [{"path": d, "exists": os.path.isdir(d)}  for d in COMFY_OUTPUT_DIRS],
            "comfy_log_tail":      [tail_file(p) for p in COMFY_LOG_CANDIDATES],
        }
        try:
            wait_for_comfy()
            diag["comfy_system_stats"] = comfy_get("/system_stats")
            try:
                diag["comfy_object_info_keys"] = sorted(get_object_info().keys())
            except Exception as e:
                diag["comfy_object_info_error"] = str(e)
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

    # ── Inference ─────────────────────────────────────────────────────────────
    wait_for_comfy()

    workflow = payload.get("workflow") or payload.get("prompt")
    if not workflow:
        raise ValueError("Missing 'workflow' in job input.")

    validate_workflow(workflow)

    uploaded_filename = resolve_input_image(payload)

    prompt_text   = payload.get("prompt_text") or payload.get("prompt")
    negative_text = payload.get("negative_prompt")

    workflow_strings = "\n".join(deep_walk(workflow))
    if "__PROMPT__" in workflow_strings and not prompt_text:
        raise ValueError("Workflow has __PROMPT__ but no prompt_text provided.")
    if "__NEG_PROMPT__" in workflow_strings and negative_text is None:
        raise ValueError("Workflow has __NEG_PROMPT__ but no negative_prompt provided.")
    if "__INPUT_IMAGE__" in workflow_strings and not uploaded_filename:
        raise ValueError("Workflow has __INPUT_IMAGE__ but no image_url / source_url / images provided.")

    workflow = patch_workflow_inputs(
        workflow,
        uploaded_filename=uploaded_filename,
        prompt=prompt_text,
        negative_prompt=negative_text,
    )

    result    = submit_prompt(workflow)
    prompt_id = result.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"No prompt_id from ComfyUI: {result}")

    history = wait_for_history(prompt_id)
    outputs = extract_and_upload_outputs(history)

    response = {"outputs": outputs, "prompt_id": prompt_id}
    if not outputs:
        response["warning"] = "Job completed but no output files found."
    return response


runpod.serverless.start({"handler": handler})

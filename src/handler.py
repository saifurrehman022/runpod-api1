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

COMFY_OUTPUT_DIRS = [
    "/comfyui/ComfyUI/output",
    "/comfyui/output",
    "/root/comfyui/output",
]

OUTPUT_NODE_TYPES = {
    "SaveImage",
    "SaveAnimatedWEBP",
    "SaveAnimatedPNG",
    "SaveAnimatedGIF",
    "SaveVideo",
    "VHS_VideoCombine",
    "PreviewImage",
}

NODE_PACK_HINTS = {
    "ImageResizeKJv2": "ComfyUI-KJNodes",
    "VHS_VideoCombine": "ComfyUI-VideoHelperSuite",
    "DiffusionModelLoaderKJ": "ComfyUI-KJNodes",
    "CLIPLoader": "core",
    "VAELoader": "core",
    "LoraLoaderModelOnly": "core",
    "BasicScheduler": "core",
    "SplitSigmas": "core",
    "KSamplerSelect": "core",
    "RandomNoise": "core",
    "VAEEncode": "core",
    "LoadImage": "core",
}

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
            r = requests.get(f"{COMFY_BASE}/system_stats", timeout=3)
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
            f"{cls} -> install {NODE_PACK_HINTS[cls]}"
            if cls in NODE_PACK_HINTS else cls
            for cls in missing
        ]
        raise RuntimeError("Missing custom node type(s):\n- " + "\n- ".join(hinted))


def patch_workflow_inputs(workflow, uploaded_filename=None, prompt=None, negative_prompt=None):
    replacements = {}
    if uploaded_filename:
        replacements["__INPUT_IMAGE__.png"] = uploaded_filename
        replacements["__INPUT_IMAGE__"] = uploaded_filename
    if prompt is not None:
        replacements["__PROMPT__"] = prompt
    if negative_prompt is not None:
        replacements["__NEG_PROMPT__"] = negative_prompt
    return recursive_replace(workflow, replacements)


def fetch_image_from_url(url: str, filename: str = "input_image.png") -> str:
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()

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
    upload_resp.raise_for_status()
    return upload_resp.json().get("name", filename)


def upload_images_to_comfy(images):
    uploaded = []
    for img in images:
        name = img["name"]
        b64 = img["image"]

        if "," in b64:
            b64 = b64.split(",", 1)[1]

        image_bytes = base64.b64decode(b64)
        resp = requests.post(
            f"{COMFY_BASE}/upload/image",
            files={"image": (name, image_bytes, "image/png")},
            data={"overwrite": "true"},
            timeout=60,
        )
        resp.raise_for_status()
        uploaded.append(resp.json().get("name", name))

    return uploaded


def resolve_input_image(payload: dict):
    for key in ("image_url", "source_url", "target_url"):
        url = payload.get(key)
        if url:
            filename = f"{key.replace('_url', '')}_image.png"
            return fetch_image_from_url(url, filename)

    images = payload.get("images", [])
    if images:
        uploaded = upload_images_to_comfy(images)
        if uploaded:
            return uploaded[0]

    return None


def submit_prompt(prompt, client_id="runpod"):
    r = requests.post(
        f"{COMFY_BASE}/prompt",
        json={"prompt": prompt, "client_id": client_id},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def wait_for_history(prompt_id, poll_interval=1.0, timeout=3600):
    start = time.time()
    while time.time() - start < timeout:
        r = requests.get(f"{COMFY_BASE}/history/{prompt_id}", timeout=30)
        r.raise_for_status()
        data = r.json()
        if prompt_id in data:
            return data[prompt_id]
        time.sleep(poll_interval)
    raise RuntimeError(f"Prompt {prompt_id} did not finish within {timeout}s")


def find_output_dir():
    for d in COMFY_OUTPUT_DIRS:
        if os.path.isdir(d):
            return d
    return COMFY_OUTPUT_DIRS[0]


def get_output_filepaths(history):
    output_dir = find_output_dir()
    files = []

    for _, node_output in history.get("outputs", {}).items():
        for key in ("images", "videos", "gifs"):
            for item in node_output.get(key, []):
                if item.get("type") == "temp":
                    continue

                fname = item.get("filename", "")
                subfolder = item.get("subfolder", "")
                fpath = os.path.join(output_dir, subfolder, fname) if subfolder else os.path.join(output_dir, fname)

                if os.path.isfile(fpath):
                    files.append({"filename": fname, "filepath": fpath})

    return files


def file_to_base64(filepath):
    with open(filepath, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def extract_outputs_base64(history):
    output_files = get_output_filepaths(history)
    results = []

    for item in output_files:
        fname = item["filename"]
        fpath = item["filepath"]
        results.append({
            "filename": fname,
            "base64": file_to_base64(fpath),
        })

    return results


def handler(job):
    payload = job.get("input") or {}
    action = payload.get("action")

    if action == "ping":
        return {"status": "ok"}

    if action == "comfy_system_stats":
        wait_for_comfy()
        return comfy_get("/system_stats")

    if action == "comfy_object_info":
        wait_for_comfy()
        return comfy_get("/object_info")

    wait_for_comfy()

    workflow = payload.get("workflow") or payload.get("prompt")
    if not workflow:
        raise ValueError("Missing 'workflow' in job input.")

    if isinstance(workflow, str):
        workflow = json.loads(workflow)

    validate_workflow(workflow)

    uploaded_filename = resolve_input_image(payload)

    prompt_text = payload.get("prompt_text") or payload.get("prompt")
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

    result = submit_prompt(workflow, payload.get("client_id", "runpod"))
    prompt_id = result.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"No prompt_id from ComfyUI: {result}")

    history = wait_for_history(prompt_id)
    outputs = extract_outputs_base64(history)

    return {
        "prompt_id": prompt_id,
        "outputs": outputs,
        "warning": None if outputs else "Job completed but no output files found."
    }


runpod.serverless.start({"handler": handler})

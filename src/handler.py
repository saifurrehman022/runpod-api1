import os
import time
import json
import base64
import requests
import runpod

COMFY_HOST = os.environ.get("COMFY_HOST", "127.0.0.1")
COMFY_PORT = int(os.environ.get("COMFY_PORT", "8188"))
COMFY_BASE = f"http://{COMFY_HOST}:{COMFY_PORT}"
COMFY_READY_TIMEOUT = int(os.environ.get("COMFY_READY_TIMEOUT", "1800"))
COMFY_READY_POLL = 1.0

DEFAULT_WORKFLOW_PATH = "/workflow.json"

COMFY_OUTPUT_DIRS = [
    "/comfyui/output",
    "/comfyui/ComfyUI/output",
    "/root/comfyui/output",
]

OUTPUT_NODE_TYPES = {
    "SaveImage", "SaveAnimatedWEBP", "SaveAnimatedPNG",
    "SaveAnimatedGIF", "SaveVideo", "VHS_VideoCombine", "PreviewImage",
}

SCENE_POSITIVE_NODES = ["193:211", "181:152", "203:222"]
SCENE_NEGATIVE_NODES = ["193:209", "181:206", "203:220"]

_comfy_ready = False


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


def load_default_workflow():
    with open(DEFAULT_WORKFLOW_PATH) as f:
        return json.load(f)


def patch_workflow_inputs(workflow, uploaded_filename=None, prompts=None,
                          negative_prompt=None, sampling_steps=None):
    if uploaded_filename:
        for node_id, node in workflow.items():
            if isinstance(node, dict) and node.get("class_type") == "LoadImage":
                node["inputs"]["image"] = uploaded_filename

    if prompts:
        for idx, node_id in enumerate(SCENE_POSITIVE_NODES):
            if node_id in workflow:
                scene_prompt = prompts[min(idx, len(prompts) - 1)]
                workflow[node_id]["inputs"]["text"] = scene_prompt

    if negative_prompt:
        for node_id in SCENE_NEGATIVE_NODES:
            if node_id in workflow:
                workflow[node_id]["inputs"]["text"] = negative_prompt

    if sampling_steps is not None:
        for node_id, node in workflow.items():
            if isinstance(node, dict) and node.get("class_type") == "BasicScheduler":
                node["inputs"]["steps"] = int(sampling_steps)

    return workflow


def workflow_has_output_node(workflow):
    return any(
        isinstance(node, dict) and node.get("class_type") in OUTPUT_NODE_TYPES
        for node in workflow.values()
    )


def validate_workflow(workflow):
    if not isinstance(workflow, dict):
        raise ValueError("Workflow must be a dict of ComfyUI nodes.")
    if not workflow_has_output_node(workflow):
        present = sorted({
            node["class_type"]
            for node in workflow.values()
            if isinstance(node, dict) and "class_type" in node
        })
        raise RuntimeError(
            "Workflow has no output node. Need one of: "
            + ", ".join(sorted(OUTPUT_NODE_TYPES))
            + ". Present: " + ", ".join(present)
        )


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


def wait_for_history(prompt_id, poll_interval=2.0, timeout=3600):
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
        for key in ("images", "videos", "gifs", "files"):
            for item in node_output.get(key, []):
                if item.get("type") == "temp":
                    continue
                fname = item.get("filename", "")
                subfolder = item.get("subfolder", "")
                fpath = (
                    os.path.join(output_dir, subfolder, fname)
                    if subfolder
                    else os.path.join(output_dir, fname)
                )
                if os.path.isfile(fpath):
                    files.append({"filename": fname, "filepath": fpath})
    return files


def file_to_base64(filepath):
    with open(filepath, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def extract_outputs_base64(history):
    return [
        {"filename": item["filename"], "base64": file_to_base64(item["filepath"])}
        for item in get_output_filepaths(history)
    ]


def handler(job):
    payload = job.get("input") or {}
    action = payload.get("action")

    if action == "ping":
        return {"status": "ok"}
    if action == "comfy_system_stats":
        wait_for_comfy()
        return comfy_get("/system_stats")

    wait_for_comfy()

    workflow = payload.get("workflow") or payload.get("prompt")
    if workflow:
        if isinstance(workflow, str):
            workflow = json.loads(workflow)
    else:
        workflow = load_default_workflow()

    validate_workflow(workflow)

    uploaded_filename = resolve_input_image(payload)

    raw_prompts = payload.get("prompts")
    prompt_text = payload.get("prompt_text") or payload.get("prompt")
    if raw_prompts and isinstance(raw_prompts, list):
        prompts = raw_prompts[:3]
    elif prompt_text:
        prompts = [prompt_text]
    else:
        prompts = None

    negative_text = payload.get("negative_prompt")
    sampling_steps = payload.get("sampling_steps")

    workflow = patch_workflow_inputs(
        workflow,
        uploaded_filename=uploaded_filename,
        prompts=prompts,
        negative_prompt=negative_text,
        sampling_steps=sampling_steps,
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
        "warning": None if outputs else "Job completed but no output files found.",
    }


runpod.serverless.start({"handler": handler})

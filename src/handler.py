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

COMFY_OUTPUT_DIRS = [
    "/comfyui/output",
    "/comfyui/ComfyUI/output",
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


def fix_vhs_video_combine(workflow):
    """
    The API-format export of VHS_VideoCombine incorrectly encodes widget values
    as input slot names (strings) instead of proper node link arrays.
    This function replaces the broken node with a correctly structured one.

    Correct structure: only 'images' input wired to node 203 slot 1.
    Widget values go in widget_0..widget_N positional fields.
    """
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        if node.get("class_type") != "VHS_VideoCombine":
            continue

        inputs = node.get("inputs", {})

        # Detect the broken pattern: widget names used as input slot keys
        broken_keys = {"audio", "meta_batch", "vae", "widget_0", "widget_1",
                       "widget_2", "widget_3", "widget_4", "widget_5",
                       "widget_6", "widget_7"}
        if broken_keys & set(inputs.keys()):
            # Find the images input — should be a [node_id, slot] pair
            images_link = inputs.get("images")
            if not isinstance(images_link, list):
                # Default: wire to node 203, slot 1 (the Extension output images)
                images_link = ["203", 1]

            node["inputs"] = {
                "images": images_link,
                # audio, meta_batch, vae are optional and unconnected
            }
            # Correct widget values for VHS_VideoCombine:
            # frame_rate, loop_count, filename_prefix, format, pix_fmt,
            # crf, save_metadata, trim_to_audio, pingpong, save_output
            node["widget_0"] = 16          # frame_rate
            node["widget_1"] = 0           # loop_count
            node["widget_2"] = "Wan22_SVI_Pro"  # filename_prefix
            node["widget_3"] = "video/h264-mp4"  # format
            node["widget_4"] = "yuv420p"   # pix_fmt
            node["widget_5"] = 19          # crf
            node["widget_6"] = True        # save_metadata
            node["widget_7"] = False       # trim_to_audio
            node["widget_8"] = False       # pingpong
            node["widget_9"] = True        # save_output

    return workflow


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


def patch_workflow_inputs(workflow, uploaded_filename=None, prompt=None, negative_prompt=None):
    """
    Inject uploaded image into the LoadImage node (node 97) directly,
    and also do string-template replacement for __PROMPT__ / __NEG_PROMPT__ / __INPUT_IMAGE__.
    """
    # 1. Direct patch: update LoadImage node widget_0 with the actual filename
    if uploaded_filename:
        for node_id, node in workflow.items():
            if isinstance(node, dict) and node.get("class_type") == "LoadImage":
                node["inputs"] = node.get("inputs", {})
                node["inputs"]["widget_0"] = uploaded_filename
                node["widget_0"] = uploaded_filename

    # 2. String-template replacement fallback
    replacements = {}
    if uploaded_filename:
        replacements["__INPUT_IMAGE__.png"] = uploaded_filename
        replacements["__INPUT_IMAGE__"] = uploaded_filename
        replacements["Gemini_Generated_Image_jk7o1njk7o1njk7o.png"] = uploaded_filename
    if prompt is not None:
        replacements["__PROMPT__"] = prompt
    if negative_prompt is not None:
        replacements["__NEG_PROMPT__"] = negative_prompt

    return recursive_replace(workflow, replacements)


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
    output_files = get_output_filepaths(history)
    results = []
    for item in output_files:
        results.append({
            "filename": item["filename"],
            "base64": file_to_base64(item["filepath"]),
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

    wait_for_comfy()

    workflow = payload.get("workflow") or payload.get("prompt")
    if not workflow:
        raise ValueError("Missing 'workflow' in job input.")
    if isinstance(workflow, str):
        workflow = json.loads(workflow)

    validate_workflow(workflow)

    # Fix broken VHS_VideoCombine inputs from API-format export
    workflow = fix_vhs_video_combine(workflow)

    # Upload input image if provided
    uploaded_filename = resolve_input_image(payload)

    prompt_text = payload.get("prompt_text")
    negative_text = payload.get("negative_prompt")

    # Inject image and text into workflow
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
        "warning": None if outputs else "Job completed but no output files found.",
    }


runpod.serverless.start({"handler": handler})

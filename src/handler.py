import os
import time
import json
import base64
import copy
import requests
import runpod

COMFY_HOST = os.environ.get("COMFY_HOST", "127.0.0.1")
COMFY_PORT = int(os.environ.get("COMFY_PORT", "8188"))
COMFY_BASE = f"http://{COMFY_HOST}:{COMFY_PORT}"
COMFY_READY_TIMEOUT = int(os.environ.get("COMFY_READY_TIMEOUT", "7200000"))  # 2hr for long jobs
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

# Base scene node IDs (3 scenes in the baked workflow)
SCENE_POSITIVE_NODES = ["193:211", "181:152", "203:222"]
SCENE_NEGATIVE_NODES = ["193:209", "181:206", "203:220"]

# WanImageToVideoSVIPro node IDs per scene
SCENE_SVI_NODES = ["193:215", "181:160", "203:219"]

# Max safe frames per scene chunk on A100 80GB
MAX_FRAMES_PER_SCENE = 257
DEFAULT_FRAMES_PER_SCENE = 81
DEFAULT_FPS = 16

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
    if not os.path.exists(DEFAULT_WORKFLOW_PATH):
        print(f"[Warning] Default workflow file not found at {DEFAULT_WORKFLOW_PATH}. Returning blank dict.")
        return {}
    with open(DEFAULT_WORKFLOW_PATH) as f:
        return json.load(f)


def build_extra_scene(workflow, scene_idx, prev_sampler_node_id,
                      prev_decode_node_id, prev_overlap_node_id,
                      frames_per_scene, positive_text, negative_text,
                      noise_seed=43):
    """
    Dynamically add an extra SVI extension scene to the workflow.
    Clones the pattern from scene 2 (181:xxx nodes) with new IDs.
    Returns new node IDs: (sampler2_id, decode_id, overlap_id)
    """
    prefix = f"EXT{scene_idx}"

    pos_id   = f"{prefix}:pos"
    neg_id   = f"{prefix}:neg"
    svi_id   = f"{prefix}:svi"
    noise_id = f"{prefix}:noise"
    dnoise_id= f"{prefix}:dnoise"
    cfg_h_id = f"{prefix}:cfgh"
    cfg_l_id = f"{prefix}:cfgl"
    samp1_id = f"{prefix}:samp1"
    samp2_id = f"{prefix}:samp2"
    dec_id   = f"{prefix}:dec"
    ovlp_id  = f"{prefix}:ovlp"

    workflow[pos_id] = {
        "inputs": {"text": positive_text, "clip": ["84", 0]},
        "class_type": "CLIPTextEncode",
        "_meta": {"title": f"Positive Prompt Scene {scene_idx}"}
    }
    workflow[neg_id] = {
        "inputs": {"text": negative_text, "clip": ["84", 0]},
        "class_type": "CLIPTextEncode",
        "_meta": {"title": f"Negative Prompt Scene {scene_idx}"}
    }
    workflow[noise_id] = {
        "inputs": {"noise_seed": noise_seed + scene_idx},
        "class_type": "RandomNoise",
        "_meta": {"title": "RandomNoise"}
    }
    workflow[dnoise_id] = {
        "inputs": {},
        "class_type": "DisableNoise",
        "_meta": {"title": "DisableNoise"}
    }
    workflow[svi_id] = {
        "inputs": {
            "length": frames_per_scene,
            "motion_latent_count": 1,
            "positive": [pos_id, 0],
            "negative": [neg_id, 0],
            "anchor_samples": ["135", 0],
            "prev_samples": [prev_sampler_node_id, 0]
        },
        "class_type": "WanImageToVideoSVIPro",
        "_meta": {"title": "WanImageToVideoSVIPro"}
    }
    workflow[cfg_h_id] = {
        "inputs": {"cfg": 1, "start_percent": 0, "end_percent": 1,
                   "model": ["141", 0], "positive": [svi_id, 0], "negative": [svi_id, 1]},
        "class_type": "ScheduledCFGGuidance",
        "_meta": {"title": "ScheduledCFGGuidance HIGH"}
    }
    workflow[cfg_l_id] = {
        "inputs": {"cfg": 1, "start_percent": 0, "end_percent": 1,
                   "model": ["142", 0], "positive": [svi_id, 0], "negative": [svi_id, 1]},
        "class_type": "ScheduledCFGGuidance",
        "_meta": {"title": "ScheduledCFGGuidance LOW"}
    }
    workflow[samp1_id] = {
        "inputs": {"noise": [noise_id, 0], "guider": [cfg_h_id, 0],
                   "sampler": ["127", 0], "sigmas": ["128", 0],
                   "latent_image": [svi_id, 2]},
        "class_type": "SamplerCustomAdvanced",
        "_meta": {"title": "SamplerCustomAdvanced 1"}
    }
    workflow[samp2_id] = {
        "inputs": {"noise": [dnoise_id, 0], "guider": [cfg_l_id, 0],
                   "sampler": ["127", 0], "sigmas": ["128", 1],
                   "latent_image": [samp1_id, 0]},
        "class_type": "SamplerCustomAdvanced",
        "_meta": {"title": "SamplerCustomAdvanced 2"}
    }
    workflow[dec_id] = {
        "inputs": {"samples": [samp2_id, 0], "vae": ["90", 0]},
        "class_type": "VAEDecode",
        "_meta": {"title": "VAE Decode"}
    }
    workflow[ovlp_id] = {
        "inputs": {
            "overlap": 5,
            "overlap_side": "source",
            "overlap_mode": "linear_blend",
            "source_images": [prev_overlap_node_id, 0],
            "new_images": [dec_id, 0]
        },
        "class_type": "ImageBatchExtendWithOverlap",
        "_meta": {"title": "ImageBatchExtendWithOverlap"}
    }

    return samp2_id, dec_id, ovlp_id


def patch_workflow_inputs(workflow, uploaded_filename=None, prompts=None,
                          negative_prompt=None, sampling_steps=None,
                          frames_per_scene=None, fps=None, num_scenes=None):
    """
    Patch all inputs safely. Dynamically extends scenes beyond 3 if num_scenes > 3.
    """
    fps = int(fps or DEFAULT_FPS)
    frames_per_scene = min(int(frames_per_scene or DEFAULT_FRAMES_PER_SCENE), MAX_FRAMES_PER_SCENE)
    num_scenes = max(3, int(num_scenes or 3))

    default_neg = workflow.get("193:209", {}).get("inputs", {}).get("text", "")

    # 1. Patch LoadImage
    if uploaded_filename:
        for node_id, node in workflow.items():
            if isinstance(node, dict) and node.get("class_type") == "LoadImage":
                node["inputs"]["image"] = uploaded_filename

    # 2. Patch positive prompts for base 3 scenes
    if prompts:
        for idx, node_id in enumerate(SCENE_POSITIVE_NODES):
            if node_id in workflow:
                scene_prompt = prompts[min(idx, len(prompts) - 1)]
                workflow[node_id]["inputs"]["text"] = scene_prompt

    # 3. Patch negative prompt
    if negative_prompt:
        for node_id in SCENE_NEGATIVE_NODES:
            if node_id in workflow:
                workflow[node_id]["inputs"]["text"] = negative_prompt

    # 4. Patch frames_per_scene on all WanImageToVideoSVIPro nodes
    for node_id in SCENE_SVI_NODES:
        if node_id in workflow:
            workflow[node_id]["inputs"]["length"] = frames_per_scene

    # 5. Patch sampling steps
    if sampling_steps is not None:
        for node_id, node in workflow.items():
            if isinstance(node, dict) and node.get("class_type") == "BasicScheduler":
                node["inputs"]["steps"] = int(sampling_steps)

    # 6. Dynamically add extra scenes beyond the base 3
    if num_scenes > 3:
        # Last sampler output from scene 3
        prev_sampler = "203:226"
        prev_decode  = "203:218"
        prev_overlap = "203:227"

        for extra_idx in range(4, num_scenes + 1):
            if prompts:
                extra_prompt = prompts[min(extra_idx - 1, len(prompts) - 1)]
            else:
                extra_prompt = workflow.get("203:222", {}).get("inputs", {}).get("text", "")

            neg = negative_prompt or default_neg

            prev_sampler, prev_decode, prev_overlap = build_extra_scene(
                workflow,
                scene_idx=extra_idx,
                prev_sampler_node_id=prev_sampler,
                prev_decode_node_id=prev_decode,
                prev_overlap_node_id=prev_overlap,
                frames_per_scene=frames_per_scene,
                positive_text=extra_prompt,
                negative_text=neg,
            )

        # Rewire VHS_VideoCombine to new final overlap node
        for node_id, node in workflow.items():
            if isinstance(node, dict) and node.get("class_type") == "VHS_VideoCombine":
                node["inputs"]["images"] = [prev_overlap, 0]

    # 7. Patch FPS in VHS_VideoCombine
    for node_id, node in workflow.items():
        if isinstance(node, dict) and node.get("class_type") == "VHS_VideoCombine":
            node["inputs"]["frame_rate"] = fps

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
        if not isinstance(img, dict) or "image" not in img:
            continue
        name = img.get("name", "uploaded_image.png")
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


def wait_for_history(prompt_id, poll_interval=2.0, timeout=1455555400):  # 4hr timeout
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{COMFY_BASE}/history/{prompt_id}", timeout=30)
            r.raise_for_status()
            data = r.json()
            if prompt_id in data:
                return data[prompt_id]
        except Exception as e:
            print(f"[Warning] Error tracking historical endpoint progression: {e}")
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
    # Safe payload parsing: handles job objects, dicts, or missing wrapper roots cleanly
    if isinstance(job, dict):
        payload = job.get("input") if job.get("input") is not None else job
    else:
        payload = {}

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
            try:
                workflow = json.loads(workflow)
            except Exception:
                pass
    
    if not isinstance(workflow, dict):
        workflow = load_default_workflow()

    validate_workflow(workflow)

    uploaded_filename = resolve_input_image(payload)

    raw_prompts = payload.get("prompts")
    prompt_text = payload.get("prompt_text") or payload.get("prompt")
    if raw_prompts and isinstance(raw_prompts, list):
        prompts = raw_prompts
    elif prompt_text and isinstance(prompt_text, str):
        prompts = [prompt_text]
    else:
        prompts = None

    # Safe defaults to fall back on if dynamic variable keys are missing out of the incoming stream
    frames_per_scene = payload.get("frames_per_scene", DEFAULT_FRAMES_PER_SCENE)
    fps              = payload.get("fps", DEFAULT_FPS)
    num_scenes       = payload.get("num_scenes", 3)
    sampling_steps   = payload.get("sampling_steps")
    negative_text    = payload.get("negative_prompt")

    # Compute expected timeline metrics safely
    try:
        total_frames = int(frames_per_scene) * int(num_scenes)
        expected_seconds = total_frames / int(fps)
    except Exception:
        total_frames = int(DEFAULT_FRAMES_PER_SCENE) * 3
        expected_seconds = total_frames / int(DEFAULT_FPS)

    print(f"[handler] scenes={num_scenes} frames/scene={frames_per_scene} "
          f"fps={fps} => ~{total_frames} frames => ~{expected_seconds:.0f}s")

    workflow = patch_workflow_inputs(
        workflow,
        uploaded_filename=uploaded_filename,
        prompts=prompts,
        negative_prompt=negative_text,
        sampling_steps=sampling_steps,
        frames_per_scene=frames_per_scene,
        fps=fps,
        num_scenes=num_scenes,
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
        "expected_duration_seconds": expected_seconds,
        "total_frames": total_frames,
        "warning": None if outputs else "Job completed but no output files found.",
    }


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})

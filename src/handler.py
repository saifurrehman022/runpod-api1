import os
import time
import json
import base64
import subprocess
import tempfile
import requests
import runpod

COMFY_HOST = os.environ.get("COMFY_HOST", "127.0.0.1")
COMFY_PORT = int(os.environ.get("COMFY_PORT", "8188"))
COMFY_BASE = f"http://{COMFY_HOST}:{COMFY_PORT}"
COMFY_READY_TIMEOUT = int(os.environ.get("COMFY_READY_TIMEOUT", "1800"))

HF_TOKEN  = os.environ.get("HF_TOKEN", "")
HF_BUCKET = os.environ.get("HF_BUCKET", "KKKONNK/used123")

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
SCENE_SVI_NODES      = ["193:215", "181:160", "203:219"]

MAX_FRAMES_PER_SCENE  = 257
DEFAULT_FRAMES_PER_SCENE = 81
DEFAULT_FPS = 16

_comfy_ready = False


# ===========================================================================
# HF BUCKET UPLOAD
# ===========================================================================

def hf_upload_file(local_path: str, remote_filename: str) -> str:
    """
    Upload a file to HF bucket using the HTTP API.
    Returns the public URL of the uploaded file.
    """
    url = f"https://huggingface.co/api/buckets/{HF_BUCKET}/upload/{remote_filename}"
    with open(local_path, "rb") as f:
        resp = requests.put(
            url,
            headers={"Authorization": f"Bearer {HF_TOKEN}"},
            data=f,
            timeout=300,
        )
    resp.raise_for_status()
    public_url = f"https://huggingface.co/api/buckets/{HF_BUCKET}/{remote_filename}"
    print(f"[hf_upload] Uploaded {remote_filename} -> {public_url}")
    return public_url


# ===========================================================================
# COMFY HELPERS
# ===========================================================================

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
        time.sleep(1.0)
    raise RuntimeError(f"ComfyUI not ready after {COMFY_READY_TIMEOUT}s: {last_err}")


def comfy_get(path):
    r = requests.get(f"{COMFY_BASE}{path}", timeout=30)
    r.raise_for_status()
    return r.json()


def load_default_workflow():
    with open(DEFAULT_WORKFLOW_PATH) as f:
        return json.load(f)


def find_output_dir():
    for d in COMFY_OUTPUT_DIRS:
        if os.path.isdir(d):
            return d
    return COMFY_OUTPUT_DIRS[0]


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
            return fetch_image_from_url(url, f"{key.replace('_url','')}_image.png")
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


def wait_for_history(prompt_id, poll_interval=2.0, timeout=14400):
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{COMFY_BASE}/history/{prompt_id}", timeout=30)
            r.raise_for_status()
            data = r.json()
            if prompt_id in data:
                return data[prompt_id]
        except requests.exceptions.ConnectionError:
            # ComfyUI may briefly drop connection under load — retry
            print("[wait_for_history] Connection error, retrying...")
            time.sleep(5)
            continue
        time.sleep(poll_interval)
    raise RuntimeError(f"Prompt {prompt_id} did not finish within {timeout}s")


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
                    if subfolder else
                    os.path.join(output_dir, fname)
                )
                if os.path.isfile(fpath):
                    files.append({"filename": fname, "filepath": fpath})
    return files


# ===========================================================================
# WORKFLOW BUILDING
# ===========================================================================

def build_scene_workflow(base_workflow, scene_idx, positive_text,
                          negative_text, frames_per_scene, sampling_steps,
                          uploaded_filename, fps, prev_sampler_node=None,
                          prev_decode_node=None, prev_overlap_node=None):
    """
    Build a single-scene workflow for scene_idx (1-based).
    Scenes 1-3 use the baked node IDs.
    Scenes 4+ clone the extension pattern.
    Returns (workflow_dict, vhs_node_id, final_sampler_node, final_decode_node, final_overlap_node)
    """
    import copy
    wf = copy.deepcopy(base_workflow)

    # Patch LoadImage
    if uploaded_filename:
        for nid, node in wf.items():
            if isinstance(node, dict) and node.get("class_type") == "LoadImage":
                node["inputs"]["image"] = uploaded_filename

    # Patch steps
    if sampling_steps:
        for nid, node in wf.items():
            if isinstance(node, dict) and node.get("class_type") == "BasicScheduler":
                node["inputs"]["steps"] = int(sampling_steps)

    if scene_idx == 1:
        # Use only scene 1 nodes, remove scenes 2 and 3
        # Patch scene 1 prompt
        wf["193:211"]["inputs"]["text"] = positive_text
        wf["193:209"]["inputs"]["text"] = negative_text
        wf["193:215"]["inputs"]["length"] = frames_per_scene

        # Wire VHS directly to scene 1 decode output
        wf["204"]["inputs"]["images"] = ["193:217", 0]
        wf["204"]["inputs"]["frame_rate"] = fps

        # Remove scene 2 and 3 nodes
        to_remove = [k for k in wf if k.startswith("181:") or k.startswith("203:")]
        for k in to_remove:
            del wf[k]

        return wf, "204", "193:216", "193:217", None

    elif scene_idx == 2:
        # Scenes 1+2, remove scene 3
        wf["193:211"]["inputs"]["text"] = positive_text  # reuse scene1 prompt
        wf["193:209"]["inputs"]["text"] = negative_text
        wf["193:215"]["inputs"]["length"] = frames_per_scene
        wf["181:152"]["inputs"]["text"] = positive_text
        wf["181:206"]["inputs"]["text"] = negative_text
        wf["181:160"]["inputs"]["length"] = frames_per_scene

        wf["204"]["inputs"]["images"] = ["181:168", 0]
        wf["204"]["inputs"]["frame_rate"] = fps

        to_remove = [k for k in wf if k.startswith("203:")]
        for k in to_remove:
            del wf[k]

        return wf, "204", "181:208", "181:162", "181:168"

    elif scene_idx == 3:
        # All 3 base scenes
        wf["193:211"]["inputs"]["text"] = positive_text
        wf["193:209"]["inputs"]["text"] = negative_text
        wf["193:215"]["inputs"]["length"] = frames_per_scene
        wf["181:152"]["inputs"]["text"] = positive_text
        wf["181:206"]["inputs"]["text"] = negative_text
        wf["181:160"]["inputs"]["length"] = frames_per_scene
        wf["203:222"]["inputs"]["text"] = positive_text
        wf["203:220"]["inputs"]["text"] = negative_text
        wf["203:219"]["inputs"]["length"] = frames_per_scene

        wf["204"]["inputs"]["images"] = ["203:227", 0]
        wf["204"]["inputs"]["frame_rate"] = fps

        return wf, "204", "203:226", "203:218", "203:227"

    else:
        # Extra scene — build on top of 3-scene base, add one extension
        p = f"EXT{scene_idx}"
        pos_id   = f"{p}:pos"
        neg_id   = f"{p}:neg"
        svi_id   = f"{p}:svi"
        noise_id = f"{p}:noise"
        dn_id    = f"{p}:dn"
        cfgh_id  = f"{p}:cfgh"
        cfgl_id  = f"{p}:cfgl"
        s1_id    = f"{p}:s1"
        s2_id    = f"{p}:s2"
        dec_id   = f"{p}:dec"
        ovlp_id  = f"{p}:ovlp"

        wf[pos_id]  = {"inputs": {"text": positive_text, "clip": ["84", 0]}, "class_type": "CLIPTextEncode", "_meta": {"title": f"Pos {scene_idx}"}}
        wf[neg_id]  = {"inputs": {"text": negative_text, "clip": ["84", 0]}, "class_type": "CLIPTextEncode", "_meta": {"title": f"Neg {scene_idx}"}}
        wf[noise_id]= {"inputs": {"noise_seed": 43 + scene_idx}, "class_type": "RandomNoise", "_meta": {"title": "RandomNoise"}}
        wf[dn_id]   = {"inputs": {}, "class_type": "DisableNoise", "_meta": {"title": "DisableNoise"}}
        wf[svi_id]  = {"inputs": {"length": frames_per_scene, "motion_latent_count": 1, "positive": [pos_id, 0], "negative": [neg_id, 0], "anchor_samples": ["135", 0], "prev_samples": [prev_sampler_node, 0]}, "class_type": "WanImageToVideoSVIPro", "_meta": {"title": "WanImageToVideoSVIPro"}}
        wf[cfgh_id] = {"inputs": {"cfg": 1, "start_percent": 0, "end_percent": 1, "model": ["141", 0], "positive": [svi_id, 0], "negative": [svi_id, 1]}, "class_type": "ScheduledCFGGuidance", "_meta": {"title": "CFG HIGH"}}
        wf[cfgl_id] = {"inputs": {"cfg": 1, "start_percent": 0, "end_percent": 1, "model": ["142", 0], "positive": [svi_id, 0], "negative": [svi_id, 1]}, "class_type": "ScheduledCFGGuidance", "_meta": {"title": "CFG LOW"}}
        wf[s1_id]   = {"inputs": {"noise": [noise_id, 0], "guider": [cfgh_id, 0], "sampler": ["127", 0], "sigmas": ["128", 0], "latent_image": [svi_id, 2]}, "class_type": "SamplerCustomAdvanced", "_meta": {"title": "Sampler1"}}
        wf[s2_id]   = {"inputs": {"noise": [dn_id, 0], "guider": [cfgl_id, 0], "sampler": ["127", 0], "sigmas": ["128", 1], "latent_image": [s1_id, 0]}, "class_type": "SamplerCustomAdvanced", "_meta": {"title": "Sampler2"}}
        wf[dec_id]  = {"inputs": {"samples": [s2_id, 0], "vae": ["90", 0]}, "class_type": "VAEDecode", "_meta": {"title": "VAE Decode"}}
        wf[ovlp_id] = {"inputs": {"overlap": 5, "overlap_side": "source", "overlap_mode": "linear_blend", "source_images": [prev_overlap_node, 0], "new_images": [dec_id, 0]}, "class_type": "ImageBatchExtendWithOverlap", "_meta": {"title": "Overlap"}}

        wf["204"]["inputs"]["images"] = [ovlp_id, 0]
        wf["204"]["inputs"]["frame_rate"] = fps

        return wf, "204", s2_id, dec_id, ovlp_id


def workflow_has_output_node(workflow):
    return any(
        isinstance(node, dict) and node.get("class_type") in OUTPUT_NODE_TYPES
        for node in workflow.values()
    )


# ===========================================================================
# FFMPEG STITCH
# ===========================================================================

def ffmpeg_concat(video_paths: list, output_path: str, fps: int):
    """Concatenate video files using FFmpeg concat demuxer."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for vp in video_paths:
            f.write(f"file '{vp}'\n")
        list_file = f.name

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", list_file,
        "-c", "copy",
        output_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    os.unlink(list_file)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {result.stderr}")
    return output_path


# ===========================================================================
# MAIN HANDLER
# ===========================================================================

def handler(job):
    payload = job.get("input") or {}
    action  = payload.get("action")

    if action == "ping":
        return {"status": "ok"}
    if action == "comfy_system_stats":
        wait_for_comfy()
        return comfy_get("/system_stats")

    wait_for_comfy()

    base_workflow = payload.get("workflow") or payload.get("prompt")
    if base_workflow:
        if isinstance(base_workflow, str):
            base_workflow = json.loads(base_workflow)
    else:
        base_workflow = load_default_workflow()

    if not workflow_has_output_node(base_workflow):
        raise RuntimeError("Workflow has no output node.")

    uploaded_filename = resolve_input_image(payload)

    # Prompts
    raw_prompts  = payload.get("prompts")
    prompt_text  = payload.get("prompt_text") or payload.get("prompt")
    if raw_prompts and isinstance(raw_prompts, list):
        prompts = raw_prompts
    elif prompt_text:
        prompts = [prompt_text]
    else:
        prompts = ["cinematic video, smooth motion, highly detailed"]

    negative_text    = payload.get("negative_prompt", "blurry, static, low quality, deformed")
    sampling_steps   = payload.get("sampling_steps")
    frames_per_scene = min(int(payload.get("frames_per_scene", DEFAULT_FRAMES_PER_SCENE)), MAX_FRAMES_PER_SCENE)
    fps              = int(payload.get("fps", DEFAULT_FPS))
    num_scenes       = max(1, int(payload.get("num_scenes", 3)))

    job_id        = job.get("id", f"job_{int(time.time())}")
    output_dir    = find_output_dir()
    chunk_urls    = []
    chunk_paths   = []

    total_frames   = frames_per_scene * num_scenes
    expected_secs  = total_frames / fps
    print(f"[handler] {num_scenes} scenes × {frames_per_scene} frames @ {fps}fps "
          f"= {total_frames} frames = {expected_secs:.0f}s ({expected_secs/60:.1f} min)")

    prev_sampler = None
    prev_decode  = None
    prev_overlap = None

    # --- Run each scene as a separate ComfyUI job ---
    for scene_idx in range(1, num_scenes + 1):
        pos_text = prompts[min(scene_idx - 1, len(prompts) - 1)]
        print(f"[handler] Submitting scene {scene_idx}/{num_scenes}: {pos_text[:60]}")

        wf, vhs_id, prev_sampler, prev_decode, prev_overlap = build_scene_workflow(
            base_workflow,
            scene_idx=scene_idx,
            positive_text=pos_text,
            negative_text=negative_text,
            frames_per_scene=frames_per_scene,
            sampling_steps=sampling_steps,
            uploaded_filename=uploaded_filename,
            fps=fps,
            prev_sampler_node=prev_sampler,
            prev_decode_node=prev_decode,
            prev_overlap_node=prev_overlap,
        )

        result    = submit_prompt(wf, f"runpod_{scene_idx}")
        prompt_id = result.get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"Scene {scene_idx}: no prompt_id returned")

        history = wait_for_history(prompt_id, timeout=14400)
        files   = get_output_filepaths(history)

        if not files:
            raise RuntimeError(f"Scene {scene_idx}: no output files found")

        # Upload chunk to HF bucket
        chunk_path     = files[0]["filepath"]
        chunk_filename = f"{job_id}_scene{scene_idx:02d}.mp4"
        chunk_url      = hf_upload_file(chunk_path, chunk_filename)
        chunk_urls.append(chunk_url)
        chunk_paths.append(chunk_path)
        print(f"[handler] Scene {scene_idx} uploaded: {chunk_url}")

    # --- Stitch all chunks with FFmpeg ---
    final_filename = f"{job_id}_final.mp4"
    final_local    = os.path.join(output_dir, final_filename)

    if len(chunk_paths) == 1:
        final_local = chunk_paths[0]
        final_filename = os.path.basename(final_local)
    else:
        print(f"[handler] Stitching {len(chunk_paths)} chunks with FFmpeg...")
        ffmpeg_concat(chunk_paths, final_local, fps)

    final_url = hf_upload_file(final_local, final_filename)
    print(f"[handler] Final video: {final_url}")

    return {
        "status": "success",
        "final_video_url": final_url,
        "chunk_urls": chunk_urls,
        "total_scenes": num_scenes,
        "total_frames": total_frames,
        "expected_duration_seconds": expected_secs,
        "expected_duration_minutes": round(expected_secs / 60, 1),
    }


runpod.serverless.start({"handler": handler})

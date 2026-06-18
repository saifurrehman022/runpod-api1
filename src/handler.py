import os
import time
import json
import base64
import subprocess
import tempfile
import copy
import requests
import runpod
 
COMFY_HOST = os.environ.get("COMFY_HOST", "127.0.0.1")
COMFY_PORT = int(os.environ.get("COMFY_PORT", "8188"))
COMFY_BASE = f"http://{COMFY_HOST}:{COMFY_PORT}"
COMFY_READY_TIMEOUT = int(os.environ.get("COMFY_READY_TIMEOUT", "1800"))

SUPABASE_URL    = os.environ.get("SUPABASE_URL", "https://yaiygjwbtzevjpxncvzu.supabase.co")
SUPABASE_KEY    = os.environ.get("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InlhaXlnandidHpldmpweG5jdnp1Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MTQ2NTA0MiwiZXhwIjoyMDk3MDQxMDQyfQ.ui2Nt6AmAJv8v5XLf2ozumHlBG4BXg7ROIuo80V9UXk")
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "videos")

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

MAX_FRAMES_PER_SCENE     = 257
DEFAULT_FRAMES_PER_SCENE = 81
DEFAULT_FPS              = 16
SCENES_PER_BATCH         = 3

_comfy_ready = False


# ===========================================================================
# SUPABASE
# ===========================================================================

def supabase_upload(local_path: str, remote_filename: str) -> str:
    if not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_KEY env var not set.")
    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{remote_filename}"
    with open(local_path, "rb") as f:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "video/mp4",
                "x-upsert": "true",
            },
            data=f,
            timeout=300,
        )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Supabase upload failed {resp.status_code}: {resp.text}")
    public_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{remote_filename}"
    print(f"[supabase] -> {public_url}")
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
    raise RuntimeError(f"ComfyUI not ready: {last_err}")


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


def upload_image_bytes_to_comfy(image_bytes: bytes, filename: str) -> str:
    resp = requests.post(
        f"{COMFY_BASE}/upload/image",
        files={"image": (filename, image_bytes, "image/png")},
        data={"overwrite": "true"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json().get("name", filename)


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
            print("[wait_for_history] Connection dropped, retrying...")
            time.sleep(5)
            continue
        time.sleep(poll_interval)
    raise RuntimeError(f"Prompt {prompt_id} did not finish within {timeout}s")


def get_output_filepaths(history):
    """
    Get output files from history. 
    Returns files sorted by size descending — largest file = final stitched video.
    """
    output_dir = find_output_dir()
    files = []
    for _, node_output in history.get("outputs", {}).items():
        for key in ("images", "videos", "gifs", "files"):
            for item in node_output.get(key, []):
                if item.get("type") == "temp":
                    continue
                fname     = item.get("filename", "")
                subfolder = item.get("subfolder", "")
                fpath = (
                    os.path.join(output_dir, subfolder, fname)
                    if subfolder else
                    os.path.join(output_dir, fname)
                )
                if os.path.isfile(fpath):
                    size = os.path.getsize(fpath)
                    files.append({
                        "filename": fname,
                        "filepath": fpath,
                        "size": size
                    })
                    print(f"[output] Found: {fname} ({size/1024/1024:.1f} MB)")

    # Sort by size descending — largest = final stitched video with all scenes
    files.sort(key=lambda x: x["size"], reverse=True)
    return files


def workflow_has_output_node(workflow):
    return any(
        isinstance(node, dict) and node.get("class_type") in OUTPUT_NODE_TYPES
        for node in workflow.values()
    )


def extract_last_frame(video_path: str, output_path: str):
    cmd = ["ffmpeg", "-y", "-sseof", "-3", "-i", video_path,
           "-vframes", "1", "-q:v", "1", output_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg frame extract failed: {result.stderr}")
    return output_path


# ===========================================================================
# WORKFLOW BUILDING
# Uses exact node IDs from wan22_SVI_Pro_8_JANapi.json:
#   193:211 = Scene 1 positive prompt
#   181:152 = Scene 2 positive prompt
#   203:222 = Scene 3 positive prompt
#   193:215, 181:160, 203:219 = WanImageToVideoSVIPro nodes (length control)
#   193:217, 181:162, 203:218 = VAEDecode outputs
#   181:168, 203:227 = ImageBatchExtendWithOverlap
#   204 = VHS_VideoCombine
# ===========================================================================

def build_batch_workflow(base_workflow, scene_prompts, negative_text,
                         frames_per_scene, sampling_steps,
                         uploaded_filename, fps, batch_start_idx):
    wf = copy.deepcopy(base_workflow)
    n  = len(scene_prompts)

    # Patch LoadImage
    if uploaded_filename:
        for nid, node in wf.items():
            if isinstance(node, dict) and node.get("class_type") == "LoadImage":
                node["inputs"]["image"] = uploaded_filename

    # Patch BasicScheduler steps
    if sampling_steps:
        for nid, node in wf.items():
            if isinstance(node, dict) and node.get("class_type") == "BasicScheduler":
                node["inputs"]["steps"] = int(sampling_steps)

    # Vary seeds per batch
    if "189" in wf:
        wf["189"]["inputs"]["noise_seed"] = 43 + batch_start_idx
    if "182" in wf:
        wf["182"]["inputs"]["noise_seed"] = 44 + batch_start_idx
    if "199" in wf:
        wf["199"]["inputs"]["noise_seed"] = 45 + batch_start_idx

    # Always patch Scene 1
    wf["193:211"]["inputs"]["text"] = scene_prompts[0]
    wf["193:209"]["inputs"]["text"] = negative_text
    wf["193:215"]["inputs"]["length"] = frames_per_scene
    wf["193:215"]["inputs"]["motion_latent_count"] = 0

    if n == 1:
        # Only Scene 1 — VHS takes from 193:217 (VAEDecode output)
        wf["204"]["inputs"]["images"] = ["193:217", 0]
        wf["204"]["inputs"]["frame_rate"] = fps
        to_remove = [k for k in wf if k.startswith("181:") or k.startswith("203:")]
        for k in to_remove:
            del wf[k]

    elif n == 2:
        # Scene 1 + Scene 2
        wf["181:152"]["inputs"]["text"] = scene_prompts[1]
        wf["181:206"]["inputs"]["text"] = negative_text
        wf["181:160"]["inputs"]["length"] = frames_per_scene
        wf["181:160"]["inputs"]["motion_latent_count"] = 1
        wf["181:160"]["inputs"]["prev_samples"]  = ["193:216", 0]
        wf["181:168"]["inputs"]["source_images"] = ["193:217", 0]
        wf["181:168"]["inputs"]["new_images"]    = ["181:162", 0]
        # VHS takes from 181:168 (overlap output) — contains scene1+scene2
        wf["204"]["inputs"]["images"] = ["181:168", 0]
        wf["204"]["inputs"]["frame_rate"] = fps
        to_remove = [k for k in wf if k.startswith("203:")]
        for k in to_remove:
            del wf[k]

    else:
        # All 3 Scenes — exact workflow structure
        wf["181:152"]["inputs"]["text"] = scene_prompts[1]
        wf["181:206"]["inputs"]["text"] = negative_text
        wf["181:160"]["inputs"]["length"] = frames_per_scene
        wf["181:160"]["inputs"]["motion_latent_count"] = 1
        wf["181:160"]["inputs"]["prev_samples"]  = ["193:216", 0]
        wf["181:168"]["inputs"]["source_images"] = ["193:217", 0]
        wf["181:168"]["inputs"]["new_images"]    = ["181:162", 0]

        wf["203:222"]["inputs"]["text"] = scene_prompts[2]
        wf["203:220"]["inputs"]["text"] = negative_text
        wf["203:219"]["inputs"]["length"] = frames_per_scene
        wf["203:219"]["inputs"]["motion_latent_count"] = 1
        wf["203:219"]["inputs"]["prev_samples"]  = ["181:208", 0]
        wf["203:227"]["inputs"]["source_images"] = ["181:168", 0]
        wf["203:227"]["inputs"]["new_images"]    = ["203:218", 0]
        # VHS takes from 203:227 (final overlap) — contains all 3 scenes
        wf["204"]["inputs"]["images"] = ["203:227", 0]
        wf["204"]["inputs"]["frame_rate"] = fps

    wf["204"]["inputs"]["filename_prefix"] = f"batch_{batch_start_idx:02d}"
    return wf


# ===========================================================================
# FFMPEG STITCH
# ===========================================================================

def ffmpeg_concat(video_paths: list, output_path: str):
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for vp in video_paths:
            f.write(f"file '{os.path.abspath(vp)}'\n")
        list_file = f.name
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
           "-i", list_file, "-c", "copy", output_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    os.unlink(list_file)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg stitch failed: {result.stderr}")
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

    raw_prompts = payload.get("prompts")
    prompt_text = payload.get("prompt_text") or payload.get("prompt")
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

    job_id      = job.get("id", f"job_{int(time.time())}")
    output_dir  = find_output_dir()
    chunk_urls  = []
    chunk_paths = []

    total_frames  = frames_per_scene * num_scenes
    expected_secs = total_frames / fps
    print(f"[handler] {num_scenes} scenes x {frames_per_scene} frames @ {fps}fps "
          f"= {total_frames} frames = {expected_secs:.0f}s ({expected_secs/60:.1f} min)")

    scene_idx = 0
    batch_num = 0

    while scene_idx < num_scenes:
        batch_num  += 1
        batch_size  = min(SCENES_PER_BATCH, num_scenes - scene_idx)
        batch_prompts = [
            prompts[min(scene_idx + i, len(prompts) - 1)]
            for i in range(batch_size)
        ]

        print(f"[handler] Batch {batch_num}: scenes {scene_idx+1}-{scene_idx+batch_size} "
              f"({batch_size} scene(s))")

        wf = build_batch_workflow(
            base_workflow,
            scene_prompts=batch_prompts,
            negative_text=negative_text,
            frames_per_scene=frames_per_scene,
            sampling_steps=sampling_steps,
            uploaded_filename=uploaded_filename,
            fps=fps,
            batch_start_idx=scene_idx,
        )

        result    = submit_prompt(wf, f"runpod_b{batch_num}")
        prompt_id = result.get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"Batch {batch_num}: no prompt_id")

        history = wait_for_history(prompt_id, timeout=14400)
        files   = get_output_filepaths(history)

        if not files:
            raise RuntimeError(f"Batch {batch_num}: no output files found")

        # Always use largest file — that's the fully stitched batch video
        chunk_path     = files[0]["filepath"]
        chunk_size_mb  = files[0]["size"] / 1024 / 1024
        chunk_filename = f"{job_id}_batch{batch_num:02d}.mp4"
        print(f"[handler] Using largest output: {chunk_path} ({chunk_size_mb:.1f} MB)")

        chunk_url = supabase_upload(chunk_path, chunk_filename)
        chunk_urls.append(chunk_url)
        chunk_paths.append(chunk_path)
        print(f"[handler] Batch {batch_num} uploaded -> {chunk_url}")

        # Extract last frame for next batch continuity
        if scene_idx + batch_size < num_scenes:
            last_frame_path = os.path.join(output_dir, f"last_frame_b{batch_num}.png")
            try:
                extract_last_frame(chunk_path, last_frame_path)
                uploaded_filename = upload_image_bytes_to_comfy(
                    open(last_frame_path, "rb").read(),
                    f"last_frame_b{batch_num}.png"
                )
                print(f"[handler] Last frame -> {uploaded_filename} (next batch input)")
            except Exception as e:
                print(f"[handler] WARNING: last frame extract failed: {e}")

        scene_idx += batch_size

    # Stitch all batch chunks
    if len(chunk_paths) == 1:
        final_local    = chunk_paths[0]
        final_filename = os.path.basename(final_local)
    else:
        print(f"[handler] Stitching {len(chunk_paths)} chunks...")
        final_filename = f"{job_id}_final.mp4"
        final_local    = os.path.join(output_dir, final_filename)
        ffmpeg_concat(chunk_paths, final_local)

    final_url = supabase_upload(final_local, final_filename)
    print(f"[handler] Final -> {final_url}")

    return {
        "status":                    "success",
        "final_video_url":           final_url,
        "chunk_urls":                chunk_urls,
        "total_scenes":              num_scenes,
        "total_frames":              total_frames,
        "expected_duration_seconds": round(expected_secs),
        "expected_duration_minutes": round(expected_secs / 60, 1),
    }


runpod.serverless.start({"handler": handler})

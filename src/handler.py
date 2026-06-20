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

MAX_FRAMES_PER_SCENE     = 300
DEFAULT_FRAMES_PER_SCENE = 81   # 16 seconds at 16fps
DEFAULT_FPS              = 16

# 3 scenes per job — uses the full workflow node chain:
#   Scene 1 (193:xxx) → latent → Scene 2 (181:xxx) → latent → Scene 3 (203:xxx)
# This gives smooth latent-chained motion WITHIN each job.
# Between jobs, last frame is extracted and used as input image for the next job.
# For 10 scenes: jobs = [1,2,3] + [4,5,6] + [7,8,9] + [10]
SCENES_PER_JOB = 3

# Per-job timeout: 257 frames × 3 scenes × ~90s/scene ≈ 3.5 hrs; use 8hrs to be safe
JOB_TIMEOUT = 8 * 60 * 60   # 28800 seconds

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


def wait_for_history(prompt_id, poll_interval=3.0, timeout=JOB_TIMEOUT):
    start    = time.time()
    last_log = start
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{COMFY_BASE}/history/{prompt_id}", timeout=30)
            r.raise_for_status()
            data = r.json()
            if prompt_id in data:
                elapsed = time.time() - start
                print(f"[history] Completed in {elapsed/60:.1f} min")
                return data[prompt_id]
        except requests.exceptions.ConnectionError:
            print("[history] Connection dropped, retrying in 10s...")
            time.sleep(10)
            continue

        now = time.time()
        if now - last_log >= 300:  # log every 5 minutes
            print(f"[history] Still running... {(now-start)/60:.1f} min elapsed")
            last_log = now

        time.sleep(poll_interval)
    raise RuntimeError(f"Prompt {prompt_id} timed out after {timeout/3600:.1f}h")


def get_output_filepaths(history):
    """
    Collect output files, sort by size descending.
    Largest file = the fully stitched video with all scenes in this job.
    """
    output_dir = find_output_dir()
    files = []
    for node_id, node_output in history.get("outputs", {}).items():
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
                    files.append({"filename": fname, "filepath": fpath, "size": size})
                    print(f"[output] {fname} ({size/1024/1024:.1f} MB) [node {node_id}]")

    files.sort(key=lambda x: x["size"], reverse=True)
    return files


def workflow_has_output_node(workflow):
    return any(
        isinstance(node, dict) and node.get("class_type") in OUTPUT_NODE_TYPES
        for node in workflow.values()
    )


def extract_last_frame(video_path: str, output_path: str):
    """Extract the last frame of a video for use as the next job's input image."""
    cmd = [
        "ffmpeg", "-y",
        "-sseof", "-1",
        "-i", video_path,
        "-vframes", "1",
        "-q:v", "1",
        output_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg frame extract failed: {result.stderr}")
    return output_path


# ===========================================================================
# WORKFLOW BUILDER
#
# The workflow has exactly 3 scene slots chained via latents:
#
#   SCENE 1 (193:xxx)  motion_latent_count=0  (no prev, starts fresh from image)
#       │
#       │  193:216[0] = low sampler latent output  ──────────► Scene 2 prev_samples
#       │  193:217[0] = VAEDecode IMAGE             ──────────► Scene 2 ImageBatchExtend source
#       ▼
#   SCENE 2 (181:xxx)  motion_latent_count=1  (continues latent from scene 1)
#       │
#       │  181:208[0] = low sampler latent output  ──────────► Scene 3 prev_samples
#       │  181:168[2] = ImageBatchExtend extended_images ─────► Scene 3 ImageBatchExtend source
#       ▼
#   SCENE 3 (203:xxx)  motion_latent_count=1  (continues latent from scene 2)
#       │
#       │  203:227[2] = ImageBatchExtend extended_images ─────► VHS (ALL 3 scenes)
#       ▼
#   VHS_VideoCombine (204)  images=["203:227", 2]
#
# CRITICAL — ImageBatchExtendWithOverlap output slots:
#   slot 0 = source_images   (just the original frames passed in — NOT the combined video)
#   slot 1 = start_images    (overlap transition frames only)
#   slot 2 = extended_images (ALL frames combined — this is what VHS needs)
#
# For n=2: VHS gets ["181:168", 2]   (scene1 + scene2 combined)
# For n=3: VHS gets ["203:227", 2]   (scene1 + scene2 + scene3 combined)
# For n=1: VHS gets ["193:217", 0]   (VAEDecode direct, only 1 output slot)
# ===========================================================================

def build_job_workflow(base_workflow, scene_prompts, negative_text,
                       frames_per_scene, sampling_steps,
                       uploaded_filename, fps, job_start_scene_idx):
    """
    Build a workflow for 1, 2, or 3 scenes chained together via latents.

    scene_prompts       : list of 1, 2, or 3 positive prompt strings
    job_start_scene_idx : global index of the first scene in this job (for seed variation)
    """
    wf = copy.deepcopy(base_workflow)
    n  = len(scene_prompts)

    # ── Patch LoadImage ────────────────────────────────────────────────────
    if uploaded_filename:
        for nid, node in wf.items():
            if isinstance(node, dict) and node.get("class_type") == "LoadImage":
                node["inputs"]["image"] = uploaded_filename

    # ── Patch BasicScheduler steps ─────────────────────────────────────────
    if sampling_steps:
        for nid, node in wf.items():
            if isinstance(node, dict) and node.get("class_type") == "BasicScheduler":
                node["inputs"]["steps"] = int(sampling_steps)

    # ── Unique seeds per job ───────────────────────────────────────────────
    # 189 = scene1 HIGH sampler noise
    # 182 = scene2 HIGH sampler noise
    # 199 = scene3 HIGH sampler noise
    if "189" in wf:
        wf["189"]["inputs"]["noise_seed"] = 43 + job_start_scene_idx
    if "182" in wf:
        wf["182"]["inputs"]["noise_seed"] = 44 + job_start_scene_idx
    if "199" in wf:
        wf["199"]["inputs"]["noise_seed"] = 45 + job_start_scene_idx

    # ── SCENE 1 — always configured ───────────────────────────────────────
    wf["193:211"]["inputs"]["text"]                = scene_prompts[0]
    wf["193:209"]["inputs"]["text"]                = negative_text
    wf["193:215"]["inputs"]["length"]              = frames_per_scene
    wf["193:215"]["inputs"]["motion_latent_count"] = 0  # first scene: no prev latent

    if n == 1:
        # ── Only 1 scene ──────────────────────────────────────────────────
        # Wire VHS directly to Scene 1 VAEDecode.
        # 193:217 = VAEDecode, output slot 0 = IMAGE
        wf["204"]["inputs"]["images"]           = ["193:217", 0]
        wf["204"]["inputs"]["frame_rate"]       = fps

        # Strip unused scene 2 and 3 nodes
        for k in list(wf.keys()):
            if k.startswith("181:") or k.startswith("203:"):
                del wf[k]
        # Strip unused noise nodes for scenes 2 and 3
        for k in ["182", "199"]:
            wf.pop(k, None)

    elif n == 2:
        # ── 2 scenes — latent chained ──────────────────────────────────────

        # Scene 2 config
        wf["181:152"]["inputs"]["text"]                = scene_prompts[1]  # positive
        wf["181:206"]["inputs"]["text"]                = negative_text      # negative
        wf["181:160"]["inputs"]["length"]              = frames_per_scene
        wf["181:160"]["inputs"]["motion_latent_count"] = 1
        # Feed scene 1 latent output into scene 2 for smooth continuation
        wf["181:160"]["inputs"]["prev_samples"]        = ["193:216", 0]

        # ImageBatchExtendWithOverlap: combine scene1 + scene2 decoded frames
        wf["181:168"]["inputs"]["source_images"] = ["193:217", 0]  # scene1 VAEDecode
        wf["181:168"]["inputs"]["new_images"]    = ["181:162", 0]  # scene2 VAEDecode

        # VHS gets slot 2 = extended_images = scene1+scene2 combined
        wf["204"]["inputs"]["images"]     = ["181:168", 2]          # ← SLOT 2
        wf["204"]["inputs"]["frame_rate"] = fps

        # Strip unused scene 3 nodes
        for k in list(wf.keys()):
            if k.startswith("203:"):
                del wf[k]
        wf.pop("199", None)

    else:
        # ── 3 scenes — full latent chain (exact workflow structure) ────────

        # Scene 2 config
        wf["181:152"]["inputs"]["text"]                = scene_prompts[1]
        wf["181:206"]["inputs"]["text"]                = negative_text
        wf["181:160"]["inputs"]["length"]              = frames_per_scene
        wf["181:160"]["inputs"]["motion_latent_count"] = 1
        wf["181:160"]["inputs"]["prev_samples"]        = ["193:216", 0]  # ← scene1 latent

        # Scene1+2 frame overlap
        wf["181:168"]["inputs"]["source_images"] = ["193:217", 0]  # scene1 decoded
        wf["181:168"]["inputs"]["new_images"]    = ["181:162", 0]  # scene2 decoded

        # Scene 3 config
        wf["203:222"]["inputs"]["text"]                = scene_prompts[2]
        wf["203:220"]["inputs"]["text"]                = negative_text
        wf["203:219"]["inputs"]["length"]              = frames_per_scene
        wf["203:219"]["inputs"]["motion_latent_count"] = 1
        wf["203:219"]["inputs"]["prev_samples"]        = ["181:208", 0]  # ← scene2 latent

        # Scene2+3 frame overlap — source must be slot 2 (extended) from scene1+2 overlap
        wf["203:227"]["inputs"]["source_images"] = ["181:168", 2]   # ← SLOT 2 (scene1+2 extended)
        wf["203:227"]["inputs"]["new_images"]    = ["203:218", 0]   # scene3 decoded

        # VHS gets slot 2 = extended_images = all 3 scenes combined
        wf["204"]["inputs"]["images"]     = ["203:227", 2]           # ← SLOT 2
        wf["204"]["inputs"]["frame_rate"] = fps

    wf["204"]["inputs"]["filename_prefix"] = f"job_{job_start_scene_idx:02d}"
    return wf


# ===========================================================================
# FFMPEG STITCH
# ===========================================================================

def ffmpeg_concat(video_paths: list, output_path: str):
    """Concatenate job videos into one final video without re-encoding."""
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

    # ── Load workflow ──────────────────────────────────────────────────────
    base_workflow = payload.get("workflow") or payload.get("prompt")
    if base_workflow:
        if isinstance(base_workflow, str):
            base_workflow = json.loads(base_workflow)
    else:
        base_workflow = load_default_workflow()

    if not workflow_has_output_node(base_workflow):
        raise RuntimeError("Workflow has no VHS_VideoCombine output node.")

    # ── Input image ────────────────────────────────────────────────────────
    uploaded_filename = resolve_input_image(payload)

    # ── Prompts ────────────────────────────────────────────────────────────
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
    frames_per_scene = min(
        int(payload.get("frames_per_scene", DEFAULT_FRAMES_PER_SCENE)),
        MAX_FRAMES_PER_SCENE
    )
    fps        = int(payload.get("fps", DEFAULT_FPS))
    num_scenes = max(1, int(payload.get("num_scenes", len(prompts))))

    job_id     = job.get("id", f"job_{int(time.time())}")
    output_dir = find_output_dir()
    chunk_urls  = []
    chunk_paths = []

    total_frames  = frames_per_scene * num_scenes
    expected_secs = total_frames / fps
    num_jobs      = -(-num_scenes // SCENES_PER_JOB)  # ceiling division

    print(f"\n[handler] ═══════════════════════════════════════════════")
    print(f"[handler]  {num_scenes} scenes × {frames_per_scene} frames @ {fps}fps")
    print(f"[handler]  = {total_frames} frames = {expected_secs:.0f}s ({expected_secs/60:.1f} min)")
    print(f"[handler]  {num_jobs} ComfyUI job(s) of up to {SCENES_PER_JOB} scenes each")
    print(f"[handler]  Scenes within each job are LATENT-CHAINED (smooth motion)")
    print(f"[handler]  Between jobs: last frame extracted for continuity")
    print(f"[handler] ═══════════════════════════════════════════════\n")

    scene_idx = 0
    job_num   = 0

    while scene_idx < num_scenes:
        job_num   += 1
        batch_size = min(SCENES_PER_JOB, num_scenes - scene_idx)

        # Collect prompts for this job (fall back to last prompt if not enough given)
        batch_prompts = [
            prompts[min(scene_idx + i, len(prompts) - 1)]
            for i in range(batch_size)
        ]

        scenes_in_job = list(range(scene_idx + 1, scene_idx + batch_size + 1))
        job_secs      = frames_per_scene * batch_size / fps

        print(f"[handler] ── Job {job_num}/{num_jobs} "
              f"(scenes {scenes_in_job[0]}–{scenes_in_job[-1]}, "
              f"{batch_size} scene(s), ~{job_secs:.0f}s) ──")
        for i, p in enumerate(batch_prompts):
            print(f"  Scene {scene_idx+1+i}: {p[:100]}")
        print(f"  Input image: {uploaded_filename}")

        wf = build_job_workflow(
            base_workflow,
            scene_prompts        = batch_prompts,
            negative_text        = negative_text,
            frames_per_scene     = frames_per_scene,
            sampling_steps       = sampling_steps,
            uploaded_filename    = uploaded_filename,
            fps                  = fps,
            job_start_scene_idx  = scene_idx,
        )

        result    = submit_prompt(wf, f"runpod_j{job_num}")
        prompt_id = result.get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"Job {job_num}: no prompt_id returned from ComfyUI")

        print(f"[handler] Submitted → prompt_id={prompt_id}")
        history = wait_for_history(prompt_id)

        # Check for ComfyUI execution error
        status_str = history.get("status", {}).get("status_str", "")
        if status_str == "error":
            msgs = history.get("status", {}).get("messages", [])
            raise RuntimeError(f"Job {job_num} ComfyUI error: {msgs}")

        files = get_output_filepaths(history)
        if not files:
            raise RuntimeError(f"Job {job_num}: no output files found")

        # Largest file = the fully stitched video for this job (all scenes combined)
        best          = files[0]
        chunk_path    = best["filepath"]
        chunk_size_mb = best["size"] / 1024 / 1024
        chunk_filename = f"{job_id}_job{job_num:02d}.mp4"

        print(f"[handler] Best output: {best['filename']} ({chunk_size_mb:.1f} MB)")

        chunk_url = supabase_upload(chunk_path, chunk_filename)
        chunk_urls.append(chunk_url)
        chunk_paths.append(chunk_path)
        print(f"[handler] Job {job_num} → {chunk_url}")

        # ── Extract last frame for next job ────────────────────────────────
        # This is the ONLY boundary between jobs. Everything WITHIN a job is
        # latent-chained (no frame boundary). The frame boundary only happens
        # at scene 3→4, 6→7, 9→10 etc.
        if scene_idx + batch_size < num_scenes:
            last_frame_path = os.path.join(output_dir, f"last_frame_j{job_num}.png")
            try:
                extract_last_frame(chunk_path, last_frame_path)
                with open(last_frame_path, "rb") as f:
                    frame_bytes = f.read()
                uploaded_filename = upload_image_bytes_to_comfy(
                    frame_bytes,
                    f"last_frame_j{job_num}.png"
                )
                print(f"[handler] Last frame extracted → {uploaded_filename} "
                      f"(input for job {job_num+1})")
            except Exception as e:
                print(f"[handler] WARNING: last frame extract failed: {e} "
                      f"— using previous image")

        scene_idx += batch_size

    # ── Stitch all job chunks into final video ─────────────────────────────
    if len(chunk_paths) == 1:
        final_local    = chunk_paths[0]
        final_filename = os.path.basename(final_local)
        print(f"\n[handler] Single job — no FFmpeg stitch needed")
    else:
        print(f"\n[handler] FFmpeg stitching {len(chunk_paths)} job chunk(s)...")
        final_filename = f"{job_id}_final.mp4"
        final_local    = os.path.join(output_dir, final_filename)
        ffmpeg_concat(chunk_paths, final_local)
        final_mb = os.path.getsize(final_local) / 1024 / 1024
        print(f"[handler] Final: {final_filename} ({final_mb:.1f} MB)")

    final_url = supabase_upload(final_local, final_filename)
    print(f"\n[handler] ✓ Complete! {num_scenes} scenes, {expected_secs:.0f}s")
    print(f"[handler] Final video → {final_url}")

    return {
        "status":                    "success",
        "final_video_url":           final_url,
        "chunk_urls":                chunk_urls,
        "total_scenes":              num_scenes,
        "frames_per_scene":          frames_per_scene,
        "total_frames":              total_frames,
        "expected_duration_seconds": round(expected_secs),
        "expected_duration_minutes": round(expected_secs / 60, 1),
    }


runpod.serverless.start({"handler": handler})

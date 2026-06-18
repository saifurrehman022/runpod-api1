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
COMFY_READY_TIMEOUT = int(os.environ.get("COMFY_READY_TIMEOUT", "180000000"))

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
DEFAULT_FRAMES_PER_SCENE = 257   # ← default is now 257 (16 seconds at 16fps)
DEFAULT_FPS              = 16
SCENES_PER_BATCH         = 3     # workflow has exactly 3 scene slots; never change

# 257 frames × 3 scenes × ~90s per scene = ~12 hours worst case
# Set a safe per-batch timeout of 6 hours
BATCH_TIMEOUT_SECONDS = 600 * 60 * 60   # 21600 seconds

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


def wait_for_history(prompt_id, poll_interval=3.0, timeout=BATCH_TIMEOUT_SECONDS):
    """
    Poll ComfyUI history until the prompt finishes.
    Default timeout = 6 hours to handle 257 frames × 3 scenes.
    """
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{COMFY_BASE}/history/{prompt_id}", timeout=30)
            r.raise_for_status()
            data = r.json()
            if prompt_id in data:
                elapsed = time.time() - start
                print(f"[wait_for_history] Done in {elapsed/60:.1f} min")
                return data[prompt_id]
        except requests.exceptions.ConnectionError:
            print("[wait_for_history] Connection dropped, retrying in 10s...")
            time.sleep(10)
            continue
        time.sleep(poll_interval)
    raise RuntimeError(f"Prompt {prompt_id} did not finish within {timeout/3600:.1f}h")


def get_output_filepaths(history):
    """
    Collect all output video/image files from the ComfyUI history.
    Sort by file size descending — the largest file is always the final
    stitched video (ImageBatchExtendWithOverlap combines all scenes).
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
                    print(f"[output] node={node_id} file={fname} size={size/1024/1024:.1f}MB")

    # Largest file = fully stitched batch output
    files.sort(key=lambda x: x["size"], reverse=True)
    return files


def workflow_has_output_node(workflow):
    return any(
        isinstance(node, dict) and node.get("class_type") in OUTPUT_NODE_TYPES
        for node in workflow.values()
    )


def extract_last_frame(video_path: str, output_path: str):
    """Extract last frame from a video using FFmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-sseof", "-3",
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
# Full 3-scene node chain (workflow.json):
#
# SCENE 1 (193:xxx)
#   193:211  CLIPTextEncode          ← positive prompt
#   193:209  CLIPTextEncode          ← negative prompt
#   193:215  WanImageToVideoSVIPro   length, motion_latent_count=0, anchor=135
#   193:213  ScheduledCFGGuidance    model=141 (HIGH+SVI lora)
#   193:210  ScheduledCFGGuidance    model=142 (LOW+SVI lora)
#   193:214  SamplerCustomAdvanced   noise=189,     guider=193:213, sigmas=128[0], latent=193:215[2]
#   193:212  DisableNoise
#   193:216  SamplerCustomAdvanced   noise=193:212, guider=193:210, sigmas=128[1], latent=193:214[0]
#   193:217  VAEDecode               samples=193:216[0]
#
# SCENE 2 (181:xxx)
#   181:152  CLIPTextEncode          ← positive prompt
#   181:206  CLIPTextEncode          ← negative prompt
#   181:160  WanImageToVideoSVIPro   length, motion_latent_count=1, prev_samples=193:216[0]
#   181:158  ScheduledCFGGuidance    model=141 (HIGH)
#   181:159  ScheduledCFGGuidance    model=142 (LOW)
#   181:207  SamplerCustomAdvanced   noise=182,     guider=181:158, sigmas=128[0], latent=181:160[2]
#   181:183  DisableNoise
#   181:208  SamplerCustomAdvanced   noise=181:183, guider=181:159, sigmas=128[1], latent=181:207[0]
#   181:162  VAEDecode               samples=181:208[0]
#   181:168  ImageBatchExtendWithOverlap  source=193:217[0], new=181:162[0]
#            slot 0=source_images  slot 1=start_images  slot 2=extended_images ←
#
# SCENE 3 (203:xxx)
#   203:222  CLIPTextEncode          ← positive prompt
#   203:220  CLIPTextEncode          ← negative prompt
#   203:219  WanImageToVideoSVIPro   length, motion_latent_count=1, prev_samples=181:208[0]
#   203:224  ScheduledCFGGuidance    model=141 (HIGH)
#   203:221  ScheduledCFGGuidance    model=142 (LOW)
#   203:225  SamplerCustomAdvanced   noise=199,     guider=203:224, sigmas=128[0], latent=203:219[2]
#   203:223  DisableNoise
#   203:226  SamplerCustomAdvanced   noise=203:223, guider=203:221, sigmas=128[1], latent=203:225[0]
#   203:218  VAEDecode               samples=203:226[0]
#   203:227  ImageBatchExtendWithOverlap  source=181:168[2], new=203:218[0]
#            slot 0=source_images  slot 1=start_images  slot 2=extended_images ←
#
# VHS 204   images=["203:227", 2]   ← slot 2 = extended_images = ALL scenes combined
#
# CRITICAL: ImageBatchExtendWithOverlap output slots:
#   slot 0 = source_images   (original frames passed through — NOT what we want)
#   slot 1 = start_images    (transition frames only)
#   slot 2 = extended_images (all frames combined — THIS is what VHS must use)
# ===========================================================================

def build_batch_workflow(base_workflow, scene_prompts, negative_text,
                         frames_per_scene, sampling_steps,
                         uploaded_filename, fps, batch_start_idx):
    wf = copy.deepcopy(base_workflow)
    n  = len(scene_prompts)   # 1, 2, or 3

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

    # ── Vary seeds per batch ───────────────────────────────────────────────
    if "189" in wf:
        wf["189"]["inputs"]["noise_seed"] = 43 + batch_start_idx
    if "182" in wf:
        wf["182"]["inputs"]["noise_seed"] = 44 + batch_start_idx
    if "199" in wf:
        wf["199"]["inputs"]["noise_seed"] = 45 + batch_start_idx

    # ── SCENE 1 — always present ───────────────────────────────────────────
    wf["193:211"]["inputs"]["text"]                = scene_prompts[0]   # positive
    wf["193:209"]["inputs"]["text"]                = negative_text       # negative
    wf["193:215"]["inputs"]["length"]              = frames_per_scene    # e.g. 257
    wf["193:215"]["inputs"]["motion_latent_count"] = 0                  # first scene

    if n == 1:
        # ── 1 scene only ──────────────────────────────────────────────────
        # VAEDecode 193:217 has one output (slot 0 = IMAGE). Use it directly.
        wf["204"]["inputs"]["images"]     = ["193:217", 0]
        wf["204"]["inputs"]["frame_rate"] = fps

        # Remove unused scene 2 & 3 nodes
        for k in list(wf.keys()):
            if k.startswith("181:") or k.startswith("203:"):
                del wf[k]

    elif n == 2:
        # ── 2 scenes ──────────────────────────────────────────────────────
        wf["181:152"]["inputs"]["text"]                = scene_prompts[1]
        wf["181:206"]["inputs"]["text"]                = negative_text
        wf["181:160"]["inputs"]["length"]              = frames_per_scene
        wf["181:160"]["inputs"]["motion_latent_count"] = 1
        wf["181:160"]["inputs"]["prev_samples"]        = ["193:216", 0]  # scene1 low sampler

        # Overlap node: stitch scene1 decoded + scene2 decoded
        wf["181:168"]["inputs"]["source_images"] = ["193:217", 0]        # scene1 VAEDecode
        wf["181:168"]["inputs"]["new_images"]    = ["181:162", 0]        # scene2 VAEDecode

        # VHS must use slot 2 = extended_images (scene1+scene2 combined)
        wf["204"]["inputs"]["images"]     = ["181:168", 2]               # ← slot 2!
        wf["204"]["inputs"]["frame_rate"] = fps

        # Remove unused scene 3 nodes
        for k in list(wf.keys()):
            if k.startswith("203:"):
                del wf[k]

    else:
        # ── 3 scenes — full baked workflow ────────────────────────────────

        # Scene 2
        wf["181:152"]["inputs"]["text"]                = scene_prompts[1]
        wf["181:206"]["inputs"]["text"]                = negative_text
        wf["181:160"]["inputs"]["length"]              = frames_per_scene
        wf["181:160"]["inputs"]["motion_latent_count"] = 1
        wf["181:160"]["inputs"]["prev_samples"]        = ["193:216", 0]  # scene1 low sampler
        wf["181:168"]["inputs"]["source_images"]       = ["193:217", 0]  # scene1 VAEDecode
        wf["181:168"]["inputs"]["new_images"]          = ["181:162", 0]  # scene2 VAEDecode

        # Scene 3
        wf["203:222"]["inputs"]["text"]                = scene_prompts[2]
        wf["203:220"]["inputs"]["text"]                = negative_text
        wf["203:219"]["inputs"]["length"]              = frames_per_scene
        wf["203:219"]["inputs"]["motion_latent_count"] = 1
        wf["203:219"]["inputs"]["prev_samples"]        = ["181:208", 0]  # scene2 low sampler
        # source = slot 2 of scene1+2 overlap (extended_images, not source_images)
        wf["203:227"]["inputs"]["source_images"]       = ["181:168", 2]  # ← slot 2!
        wf["203:227"]["inputs"]["new_images"]          = ["203:218", 0]  # scene3 VAEDecode

        # VHS must use slot 2 = extended_images (all 3 scenes combined)
        wf["204"]["inputs"]["images"]     = ["203:227", 2]               # ← slot 2!
        wf["204"]["inputs"]["frame_rate"] = fps

    wf["204"]["inputs"]["filename_prefix"] = f"batch_{batch_start_idx:02d}"
    return wf


# ===========================================================================
# FFMPEG STITCH
# ===========================================================================

def ffmpeg_concat(video_paths: list, output_path: str):
    """Concatenate batch videos into final video without re-encoding."""
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
    # Default 257 frames = 16 seconds at 16fps; max 300
    frames_per_scene = min(int(payload.get("frames_per_scene", DEFAULT_FRAMES_PER_SCENE)), MAX_FRAMES_PER_SCENE)
    fps              = int(payload.get("fps", DEFAULT_FPS))
    num_scenes       = max(1, int(payload.get("num_scenes", 3)))

    job_id     = job.get("id", f"job_{int(time.time())}")
    output_dir = find_output_dir()
    chunk_urls  = []
    chunk_paths = []

    total_frames  = frames_per_scene * num_scenes
    expected_secs = total_frames / fps
    print(f"[handler] {num_scenes} scenes × {frames_per_scene} frames @ {fps}fps "
          f"= {total_frames} total frames = {expected_secs:.0f}s ({expected_secs/60:.1f} min)")
    print(f"[handler] Batches needed: {-(-num_scenes // SCENES_PER_BATCH)}"
          f" (up to {SCENES_PER_BATCH} scenes each)")

    # ── Batch loop ─────────────────────────────────────────────────────────
    # Each batch is 1–3 scenes submitted as one ComfyUI job.
    # ImageBatchExtendWithOverlap inside the workflow stitches them together,
    # so each batch produces ONE video covering all its scenes.
    # Between batches we extract the last frame for visual continuity.
    scene_idx = 0
    batch_num = 0

    while scene_idx < num_scenes:
        batch_num  += 1
        batch_size  = min(SCENES_PER_BATCH, num_scenes - scene_idx)

        batch_prompts = [
            prompts[min(scene_idx + i, len(prompts) - 1)]
            for i in range(batch_size)
        ]

        secs_this_batch = frames_per_scene * batch_size / fps
        print(f"\n[handler] ── Batch {batch_num} "
              f"(scenes {scene_idx+1}–{scene_idx+batch_size}, "
              f"{batch_size} scene(s), ~{secs_this_batch:.0f}s) ──")
        for i, p in enumerate(batch_prompts):
            print(f"  Scene {scene_idx+1+i}: {p[:100]}")

        wf = build_batch_workflow(
            base_workflow,
            scene_prompts    = batch_prompts,
            negative_text    = negative_text,
            frames_per_scene = frames_per_scene,
            sampling_steps   = sampling_steps,
            uploaded_filename= uploaded_filename,
            fps              = fps,
            batch_start_idx  = scene_idx,
        )

        result    = submit_prompt(wf, f"runpod_b{batch_num}")
        prompt_id = result.get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"Batch {batch_num}: no prompt_id from ComfyUI")

        print(f"[handler] Batch {batch_num} submitted → prompt_id={prompt_id}")
        history = wait_for_history(prompt_id, timeout=BATCH_TIMEOUT_SECONDS)

        # Check for ComfyUI execution errors
        status_str = history.get("status", {}).get("status_str", "")
        if status_str == "error":
            msgs = history.get("status", {}).get("messages", [])
            raise RuntimeError(f"Batch {batch_num} ComfyUI error: {msgs}")

        files = get_output_filepaths(history)
        if not files:
            raise RuntimeError(f"Batch {batch_num}: no output files found")

        # Pick largest file = fully stitched batch video with all scenes
        best          = files[0]
        chunk_path    = best["filepath"]
        chunk_size_mb = best["size"] / 1024 / 1024
        print(f"[handler] Selected output: {best['filename']} ({chunk_size_mb:.1f} MB)")

        chunk_filename = f"{job_id}_batch{batch_num:02d}.mp4"
        chunk_url      = supabase_upload(chunk_path, chunk_filename)
        chunk_urls.append(chunk_url)
        chunk_paths.append(chunk_path)
        print(f"[handler] Batch {batch_num} → {chunk_url}")

        # Extract last frame → use as input image for next batch
        if scene_idx + batch_size < num_scenes:
            last_frame_path = os.path.join(output_dir, f"last_frame_b{batch_num}.png")
            try:
                extract_last_frame(chunk_path, last_frame_path)
                with open(last_frame_path, "rb") as f:
                    frame_bytes = f.read()
                uploaded_filename = upload_image_bytes_to_comfy(
                    frame_bytes,
                    f"last_frame_b{batch_num}.png"
                )
                print(f"[handler] Last frame → {uploaded_filename} (input for batch {batch_num+1})")
            except Exception as e:
                print(f"[handler] WARNING: last frame extract failed: {e} — using original image")

        scene_idx += batch_size

    # ── Stitch all batch chunks into one final video ────────────────────────
    if len(chunk_paths) == 1:
        final_local    = chunk_paths[0]
        final_filename = os.path.basename(final_local)
        print(f"[handler] Single batch — no stitch needed")
    else:
        print(f"[handler] Stitching {len(chunk_paths)} batch chunks with FFmpeg...")
        final_filename = f"{job_id}_final.mp4"
        final_local    = os.path.join(output_dir, final_filename)
        ffmpeg_concat(chunk_paths, final_local)
        stitched_size = os.path.getsize(final_local) / 1024 / 1024
        print(f"[handler] Stitched final: {final_filename} ({stitched_size:.1f} MB)")

    final_url = supabase_upload(final_local, final_filename)
    print(f"[handler] ✓ Final video → {final_url}")

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

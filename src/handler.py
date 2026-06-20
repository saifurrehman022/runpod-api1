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
DEFAULT_FRAMES_PER_SCENE = 81   # 257 frames = 16 seconds at 16fps
DEFAULT_FPS              = 16

# Each scene runs as its OWN independent ComfyUI job.
# This is the simplest and most reliable approach:
#   - Scene 1 nodes (193:xxx) generate the video
#   - Scene 2 and 3 nodes are removed from every job
#   - FFmpeg stitches all scene videos at the end
# For 10 scenes: 10 jobs × 257 frames = 2570 frames = 160 seconds (~2.7 min)
SCENES_PER_JOB = 1

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
    print(f"[supabase] Uploaded -> {public_url}")
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


def wait_for_history(prompt_id, poll_interval=3.0, timeout=43200):
    """
    Poll until the prompt completes.
    timeout = 43200 seconds = 12 hours (safe for 257 frames at high quality).
    Prints elapsed time every 5 minutes so you can see progress in logs.
    """
    start = time.time()
    last_log = start
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{COMFY_BASE}/history/{prompt_id}", timeout=30)
            r.raise_for_status()
            data = r.json()
            if prompt_id in data:
                elapsed = time.time() - start
                print(f"[wait_for_history] Completed in {elapsed/60:.1f} min")
                return data[prompt_id]
        except requests.exceptions.ConnectionError:
            print("[wait_for_history] Connection dropped, retrying in 10s...")
            time.sleep(10)
            continue

        # Log progress every 5 minutes
        now = time.time()
        if now - last_log >= 300:
            elapsed = now - start
            print(f"[wait_for_history] Still running... {elapsed/60:.1f} min elapsed")
            last_log = now

        time.sleep(poll_interval)
    raise RuntimeError(f"Prompt {prompt_id} timed out after {timeout/3600:.1f}h")


def get_output_filepaths(history):
    """
    Find all output files from ComfyUI history.
    Sort by size descending so files[0] is always the largest (= main video).
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
                    print(f"[output] node={node_id} {fname} ({size/1024/1024:.1f} MB)")

    files.sort(key=lambda x: x["size"], reverse=True)
    return files


def workflow_has_output_node(workflow):
    return any(
        isinstance(node, dict) and node.get("class_type") in OUTPUT_NODE_TYPES
        for node in workflow.values()
    )


def extract_last_frame(video_path: str, output_path: str):
    """Extract the very last frame of a video using FFmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-sseof", "-1",        # 1 second before the end
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
# WORKFLOW BUILDER — SINGLE SCENE PER JOB
#
# Because SCENES_PER_JOB = 1, every job only uses Scene 1 nodes (193:xxx).
# Scene 2 (181:xxx) and Scene 3 (203:xxx) nodes are always removed.
#
# Scene 1 node chain:
#   LoadImage (97) → ImageResizeKJv2 (136) → VAEEncode (135) → anchor_samples
#   CLIPLoader (84) → CLIPTextEncode positive (193:211)
#                  → CLIPTextEncode negative (193:209)
#   DiffusionModelLoaderKJ HIGH (116) → LoraLoader lightx2v HIGH (101)
#                                     → LoraLoader SVI HIGH (141)
#                                     → ModelSamplingSD3 (104)
#                                     → BasicScheduler (122) → SplitSigmas (128)
#                                     → ScheduledCFGGuidance HIGH (193:213)
#   DiffusionModelLoaderKJ LOW (117)  → LoraLoader lightx2v LOW (102)
#                                     → LoraLoader SVI LOW (142)
#                                     → ScheduledCFGGuidance LOW (193:210)
#   WanImageToVideoSVIPro (193:215)   length=frames_per_scene, motion_latent_count=0
#   SamplerCustomAdvanced HIGH (193:214) noise=RandomNoise(189), sigmas=128[0]
#   SamplerCustomAdvanced LOW  (193:216) noise=DisableNoise(193:212), sigmas=128[1]
#   VAEDecode (193:217) → VHS_VideoCombine (204)
# ===========================================================================

def build_single_scene_workflow(base_workflow, positive_prompt, negative_text,
                                frames_per_scene, sampling_steps,
                                uploaded_filename, fps, scene_idx):
    """
    Build a workflow for exactly 1 scene using only the Scene 1 node chain.
    Scene 2 and 3 nodes are stripped out entirely.
    """
    wf = copy.deepcopy(base_workflow)

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

    # ── Unique seed per scene so each scene looks different ────────────────
    # Only node 189 (RandomNoise for scene 1 HIGH sampler) is kept.
    # Nodes 182 and 199 belong to scenes 2 and 3 and will be removed.
    wf["189"]["inputs"]["noise_seed"] = 43 + scene_idx

    # ── Patch Scene 1 prompts and length ──────────────────────────────────
    wf["193:211"]["inputs"]["text"]                = positive_prompt
    wf["193:209"]["inputs"]["text"]                = negative_text
    wf["193:215"]["inputs"]["length"]              = frames_per_scene
    wf["193:215"]["inputs"]["motion_latent_count"] = 0   # no prev_samples for first/only scene

    # ── Wire VHS directly to Scene 1 VAEDecode output ─────────────────────
    # 193:217 = VAEDecode, slot 0 = IMAGE
    wf["204"]["inputs"]["images"]            = ["193:217", 0]
    wf["204"]["inputs"]["frame_rate"]        = fps
    wf["204"]["inputs"]["filename_prefix"]   = f"scene_{scene_idx+1:02d}"

    # ── Remove ALL Scene 2 and Scene 3 nodes ──────────────────────────────
    # This prevents ComfyUI from trying to execute disconnected nodes.
    for k in list(wf.keys()):
        if k.startswith("181:") or k.startswith("203:"):
            del wf[k]

    # ── Also remove the scene 2/3 RandomNoise nodes (182, 199) ────────────
    # They are not needed and would just be dead nodes.
    for k in ["182", "199"]:
        if k in wf:
            del wf[k]

    return wf


# ===========================================================================
# FFMPEG STITCH
# ===========================================================================

def ffmpeg_concat(video_paths: list, output_path: str):
    """Concatenate scene videos into one final video without re-encoding."""
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
    scene_urls  = []
    scene_paths = []

    total_frames  = frames_per_scene * num_scenes
    expected_secs = total_frames / fps

    print(f"[handler] ══════════════════════════════════════════")
    print(f"[handler] {num_scenes} scenes × {frames_per_scene} frames @ {fps}fps")
    print(f"[handler] = {total_frames} total frames = {expected_secs:.0f}s ({expected_secs/60:.1f} min)")
    print(f"[handler] Running 1 scene per ComfyUI job ({num_scenes} jobs total)")
    print(f"[handler] ══════════════════════════════════════════")

    # ── Scene loop — one ComfyUI job per scene ─────────────────────────────
    for scene_idx in range(num_scenes):
        positive_prompt = prompts[min(scene_idx, len(prompts) - 1)]

        print(f"\n[handler] ── Scene {scene_idx+1}/{num_scenes} ──")
        print(f"[handler] Prompt : {positive_prompt[:120]}")
        print(f"[handler] Image  : {uploaded_filename}")
        print(f"[handler] Frames : {frames_per_scene} ({frames_per_scene/fps:.1f}s)")

        wf = build_single_scene_workflow(
            base_workflow,
            positive_prompt  = positive_prompt,
            negative_text    = negative_text,
            frames_per_scene = frames_per_scene,
            sampling_steps   = sampling_steps,
            uploaded_filename= uploaded_filename,
            fps              = fps,
            scene_idx        = scene_idx,
        )

        result    = submit_prompt(wf, f"runpod_scene{scene_idx+1}")
        prompt_id = result.get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"Scene {scene_idx+1}: no prompt_id returned from ComfyUI")

        print(f"[handler] Submitted → prompt_id={prompt_id}")
        history = wait_for_history(prompt_id)

        # Check for ComfyUI execution error
        status_str = history.get("status", {}).get("status_str", "")
        if status_str == "error":
            msgs = history.get("status", {}).get("messages", [])
            raise RuntimeError(f"Scene {scene_idx+1} ComfyUI error: {msgs}")

        files = get_output_filepaths(history)
        if not files:
            raise RuntimeError(f"Scene {scene_idx+1}: no output files found in history")

        # Pick the largest file — that is always the main video
        scene_file     = files[0]
        scene_path     = scene_file["filepath"]
        scene_size_mb  = scene_file["size"] / 1024 / 1024
        scene_filename = f"{job_id}_scene{scene_idx+1:02d}.mp4"

        print(f"[handler] Output : {scene_file['filename']} ({scene_size_mb:.1f} MB)")

        scene_url = supabase_upload(scene_path, scene_filename)
        scene_urls.append(scene_url)
        scene_paths.append(scene_path)
        print(f"[handler] Scene {scene_idx+1} uploaded → {scene_url}")

        # ── Extract last frame → use as input image for next scene ─────────
        # This gives visual continuity: next scene starts where this one ended.
        if scene_idx + 1 < num_scenes:
            last_frame_path = os.path.join(output_dir, f"last_frame_s{scene_idx+1}.png")
            try:
                extract_last_frame(scene_path, last_frame_path)
                with open(last_frame_path, "rb") as f:
                    frame_bytes = f.read()
                uploaded_filename = upload_image_bytes_to_comfy(
                    frame_bytes,
                    f"last_frame_s{scene_idx+1}.png"
                )
                print(f"[handler] Last frame → {uploaded_filename} (input for scene {scene_idx+2})")
            except Exception as e:
                print(f"[handler] WARNING: last frame extract failed: {e} — keeping previous image")

    # ── Stitch all scene videos into one final video ───────────────────────
    if len(scene_paths) == 1:
        final_local    = scene_paths[0]
        final_filename = os.path.basename(final_local)
        print(f"\n[handler] Single scene — no stitch needed")
    else:
        print(f"\n[handler] Stitching {len(scene_paths)} scenes with FFmpeg...")
        final_filename = f"{job_id}_final.mp4"
        final_local    = os.path.join(output_dir, final_filename)
        ffmpeg_concat(scene_paths, final_local)
        final_size_mb = os.path.getsize(final_local) / 1024 / 1024
        print(f"[handler] Final video: {final_filename} ({final_size_mb:.1f} MB)")

    final_url = supabase_upload(final_local, final_filename)
    print(f"\n[handler] ✓ Done! Final video → {final_url}")
    print(f"[handler] Duration: {expected_secs:.0f}s ({expected_secs/60:.1f} min)")

    return {
        "status":                    "success",
        "final_video_url":           final_url,
        "scene_urls":                scene_urls,
        "total_scenes":              num_scenes,
        "frames_per_scene":          frames_per_scene,
        "total_frames":              total_frames,
        "expected_duration_seconds": round(expected_secs),
        "expected_duration_minutes": round(expected_secs / 60, 1),
    }


runpod.serverless.start({"handler": handler})

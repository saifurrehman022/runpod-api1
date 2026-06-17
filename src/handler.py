import os
import time
import json
import base64
import copy
import requests
import runpod

COMFY_HOST          = os.environ.get("COMFY_HOST", "127.0.0.1")
COMFY_PORT          = int(os.environ.get("COMFY_PORT", "8188"))
COMFY_BASE          = f"http://{COMFY_HOST}:{COMFY_PORT}"
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

# ── safe defaults for A4500 (20 GB VRAM) ─────────────────────────────────────
# 81 frames × 3 scenes = ~15 s video @ 16 fps.
# NEVER raise MAX_FRAMES_PER_SCENE above 81 on this GPU — you will OOM/timeout.
DEFAULT_FRAMES_PER_SCENE = 81
MAX_FRAMES_PER_SCENE     = 81
DEFAULT_FPS              = 16
DEFAULT_STEPS            = 6

DEFAULT_NEGATIVE = (
    "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，"
    "最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，"
    "画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，"
    "杂乱的背景，三条腿，背景人很多，倒着走"
)

_comfy_ready = False


# ══════════════════════════════════════════════════════════════════════════════
# SUPABASE
# ══════════════════════════════════════════════════════════════════════════════

def supabase_upload(local_path: str, remote_filename: str) -> str:
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
    print(f"[supabase] {public_url}")
    return public_url


# ══════════════════════════════════════════════════════════════════════════════
# COMFYUI HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def wait_for_comfy():
    global _comfy_ready
    if _comfy_ready:
        return
    start, last_err = time.time(), None
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


def load_workflow():
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
    up = requests.post(
        f"{COMFY_BASE}/upload/image",
        files={"image": (filename, image_bytes, ct or "image/png")},
        data={"overwrite": "true"},
        timeout=60,
    )
    up.raise_for_status()
    return up.json().get("name", filename)


def upload_b64_images(images: list) -> str:
    first = None
    for img in images:
        name = img["name"]
        b64  = img["image"]
        if "," in b64:
            b64 = b64.split(",", 1)[1]
        data = base64.b64decode(b64)
        resp = requests.post(
            f"{COMFY_BASE}/upload/image",
            files={"image": (name, data, "image/png")},
            data={"overwrite": "true"},
            timeout=60,
        )
        resp.raise_for_status()
        if first is None:
            first = resp.json().get("name", name)
    return first


def resolve_input_image(payload: dict):
    for key in ("image_url", "source_url", "target_url"):
        url = payload.get(key)
        if url:
            print(f"[handler] Fetching input image: {url}")
            return fetch_image_from_url(url, f"{key.replace('_url','')}_image.png")
    images = payload.get("images", [])
    if images:
        return upload_b64_images(images)
    return None


def submit_prompt(prompt_dict, client_id="runpod"):
    r = requests.post(
        f"{COMFY_BASE}/prompt",
        json={"prompt": prompt_dict, "client_id": client_id},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def wait_for_history(prompt_id, poll=2.0, timeout=7200):
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{COMFY_BASE}/history/{prompt_id}", timeout=30)
            r.raise_for_status()
            data = r.json()
            if prompt_id in data:
                return data[prompt_id]
        except requests.exceptions.ConnectionError:
            print("[wait_for_history] connection dropped, retrying in 5s...")
            time.sleep(5)
            continue
        time.sleep(poll)
    raise RuntimeError(f"Prompt {prompt_id} did not finish within {timeout}s")


def get_output_files(history):
    output_dir = find_output_dir()
    files = []
    for _, node_out in history.get("outputs", {}).items():
        for key in ("images", "videos", "gifs", "files"):
            for item in node_out.get(key, []):
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
                    files.append({"filename": fname, "filepath": fpath})
    return files


# ══════════════════════════════════════════════════════════════════════════════
# WORKFLOW PATCHING
#
# src/workflow.json is the API-format (flat dict) for exactly 3 scenes.
# Node map:
#
#  SHARED
#  "97"       LoadImage               ← inputs.image
#  "122"      BasicScheduler          ← inputs.steps
#  "189"      RandomNoise             ← inputs.noise_seed  (scene 1)
#  "182"      RandomNoise             ← inputs.noise_seed  (scene 2)
#  "199"      RandomNoise             ← inputs.noise_seed  (scene 3)
#  "204"      VHS_VideoCombine        ← inputs.frame_rate, filename_prefix
#                                        inputs.images = ["203:227", 2]  (slot 2 = extended)
#
#  SCENE 1  (prefix 193:)
#  "193:211"  CLIPTextEncode  (Positive Prompt)   ← inputs.text = prompt1
#  "193:209"  CLIPTextEncode  (Negative Prompt)   ← inputs.text = negative
#  "193:215"  WanImageToVideoSVIPro               ← inputs.length, motion_latent_count=0
#  "193:214"  SamplerCustomAdvanced  (high-noise)
#  "193:216"  SamplerCustomAdvanced  (low-noise)  → output[0] = scene1 latent
#  "193:217"  VAEDecode                           → output[0] = scene1 decoded images
#
#  SCENE 2  (prefix 181:)
#  "181:152"  CLIPTextEncode  (Positive Prompt)   ← inputs.text = prompt2
#  "181:206"  CLIPTextEncode  (Negative Prompt)   ← inputs.text = negative
#  "181:160"  WanImageToVideoSVIPro               ← inputs.length, motion_latent_count=1
#                                                    inputs.prev_samples=["193:216",0]
#  "181:207"  SamplerCustomAdvanced  (high-noise)
#  "181:208"  SamplerCustomAdvanced  (low-noise)  → output[0] = scene2 latent
#  "181:162"  VAEDecode                           → output[0] = scene2 decoded images
#  "181:168"  ImageBatchExtendWithOverlap
#               inputs.source_images = ["193:217", 0]  (scene1 frames)
#               inputs.new_images    = ["181:162", 0]  (scene2 frames)
#               output slot 2 = extended_images (scene1+2 joined)
#
#  SCENE 3  (prefix 203:)
#  "203:222"  CLIPTextEncode  (Positive Prompt)   ← inputs.text = prompt3
#  "203:220"  CLIPTextEncode  (Negative Prompt)   ← inputs.text = negative
#  "203:219"  WanImageToVideoSVIPro               ← inputs.length, motion_latent_count=1
#                                                    inputs.prev_samples=["181:208",0]
#  "203:225"  SamplerCustomAdvanced  (high-noise)
#  "203:226"  SamplerCustomAdvanced  (low-noise)  → output[0] = scene3 latent
#  "203:218"  VAEDecode                           → output[0] = scene3 decoded images
#  "203:227"  ImageBatchExtendWithOverlap
#               inputs.source_images = ["181:168", 2]  (scene1+2 combined, slot 2)
#               inputs.new_images    = ["203:218", 0]  (scene3 frames)
#               output slot 2 = extended_images → VHS "204"
#
# ══════════════════════════════════════════════════════════════════════════════

def build_workflow(
    base_workflow: dict,
    prompt1: str,
    prompt2: str,
    prompt3: str,
    negative: str,
    frames_per_scene: int,
    steps: int,
    uploaded_filename,
    fps: int,
    job_id: str,
    seed_offset: int = 0,
) -> dict:
    """
    Patch the baked 3-scene workflow.json with the caller's values.
    NEVER deletes any node — always runs the full 3-scene graph.
    """
    wf = copy.deepcopy(base_workflow)

    # ── input image ───────────────────────────────────────────────────────────
    if uploaded_filename:
        wf["97"]["inputs"]["image"] = uploaded_filename

    # ── scheduler steps ───────────────────────────────────────────────────────
    wf["122"]["inputs"]["steps"] = steps

    # ── noise seeds ───────────────────────────────────────────────────────────
    wf["189"]["inputs"]["noise_seed"] = 43 + seed_offset   # scene 1
    wf["182"]["inputs"]["noise_seed"] = 44 + seed_offset   # scene 2
    wf["199"]["inputs"]["noise_seed"] = 45 + seed_offset   # scene 3

    # ── SCENE 1 ───────────────────────────────────────────────────────────────
    wf["193:211"]["inputs"]["text"]                      = prompt1
    wf["193:209"]["inputs"]["text"]                      = negative
    wf["193:215"]["inputs"]["length"]                    = frames_per_scene
    wf["193:215"]["inputs"]["motion_latent_count"]       = 0  # no prev scene

    # ── SCENE 2 ───────────────────────────────────────────────────────────────
    wf["181:152"]["inputs"]["text"]                      = prompt2
    wf["181:206"]["inputs"]["text"]                      = negative
    wf["181:160"]["inputs"]["length"]                    = frames_per_scene
    wf["181:160"]["inputs"]["motion_latent_count"]       = 1  # has prev
    wf["181:160"]["inputs"]["prev_samples"]              = ["193:216", 0]
    # overlap: scene1 decoded → source, scene2 decoded → new
    wf["181:168"]["inputs"]["source_images"]             = ["193:217", 0]
    wf["181:168"]["inputs"]["new_images"]                = ["181:162", 0]

    # ── SCENE 3 ───────────────────────────────────────────────────────────────
    wf["203:222"]["inputs"]["text"]                      = prompt3
    wf["203:220"]["inputs"]["text"]                      = negative
    wf["203:219"]["inputs"]["length"]                    = frames_per_scene
    wf["203:219"]["inputs"]["motion_latent_count"]       = 1  # has prev
    wf["203:219"]["inputs"]["prev_samples"]              = ["181:208", 0]
    # overlap: scene1+2 combined (slot 2) → source, scene3 decoded → new
    wf["203:227"]["inputs"]["source_images"]             = ["181:168", 2]
    wf["203:227"]["inputs"]["new_images"]                = ["203:218", 0]

    # ── VHS output ────────────────────────────────────────────────────────────
    # slot 2 of ImageBatchExtendWithOverlap = extended_images (full joined video)
    wf["204"]["inputs"]["images"]          = ["203:227", 2]
    wf["204"]["inputs"]["frame_rate"]      = fps
    wf["204"]["inputs"]["filename_prefix"] = f"svi_{job_id}"

    return wf


# ══════════════════════════════════════════════════════════════════════════════
# MAIN HANDLER
# ══════════════════════════════════════════════════════════════════════════════

def handler(job):
    payload = job.get("input") or {}
    action  = payload.get("action")

    if action == "ping":
        return {"status": "ok"}
    if action == "comfy_system_stats":
        wait_for_comfy()
        return comfy_get("/system_stats")

    wait_for_comfy()

    # ── load workflow ─────────────────────────────────────────────────────────
    raw_wf = payload.get("workflow") or payload.get("prompt")
    if raw_wf:
        base_workflow = json.loads(raw_wf) if isinstance(raw_wf, str) else raw_wf
    else:
        base_workflow = load_workflow()

    has_output = any(
        isinstance(v, dict) and v.get("class_type") in OUTPUT_NODE_TYPES
        for v in base_workflow.values()
    )
    if not has_output:
        raise RuntimeError("Workflow has no recognised output node (VHS_VideoCombine etc.)")

    # ── input image ───────────────────────────────────────────────────────────
    uploaded_filename = resolve_input_image(payload)
    if uploaded_filename:
        print(f"[handler] Input image uploaded: {uploaded_filename}")
    else:
        print("[handler] No input image — using workflow default")

    # ── prompts ───────────────────────────────────────────────────────────────
    # Accepted formats:
    #   "prompts": ["scene1", "scene2", "scene3"]          (preferred)
    #   "prompt1": "...", "prompt2": "...", "prompt3": "..."
    #   "prompt_text": "..."  (same text for all 3)
    prompts = payload.get("prompts", [])
    if isinstance(prompts, list) and len(prompts) >= 3:
        prompt1, prompt2, prompt3 = prompts[0], prompts[1], prompts[2]
    else:
        fallback = (
            payload.get("prompt_text")
            or (prompts[0] if prompts else None)
            or "cinematic video, smooth motion, highly detailed"
        )
        prompt1 = payload.get("prompt1", fallback)
        prompt2 = payload.get("prompt2", fallback)
        prompt3 = payload.get("prompt3", fallback)

    negative = payload.get("negative_prompt", DEFAULT_NEGATIVE)

    # ── generation params ─────────────────────────────────────────────────────
    frames_per_scene = min(
        int(payload.get("frames_per_scene", DEFAULT_FRAMES_PER_SCENE)),
        MAX_FRAMES_PER_SCENE,
    )
    steps      = int(payload.get("sampling_steps", DEFAULT_STEPS))
    fps        = int(payload.get("fps", DEFAULT_FPS))
    seed_off   = int(payload.get("seed_offset", int(time.time()) % 10000))
    job_id     = job.get("id", f"job_{int(time.time())}")

    total_frames = frames_per_scene * 3
    duration_s   = total_frames / fps

    print(f"[handler] 3 scenes × {frames_per_scene} frames @ {fps} fps "
          f"= {total_frames} frames ≈ {duration_s:.0f}s ({duration_s/60:.1f} min)")
    print(f"[handler] Scene 1: {prompt1[:100]}")
    print(f"[handler] Scene 2: {prompt2[:100]}")
    print(f"[handler] Scene 3: {prompt3[:100]}")
    print(f"[handler] Steps={steps}  seed_offset={seed_off}")

    # ── build & submit ────────────────────────────────────────────────────────
    wf = build_workflow(
        base_workflow    = base_workflow,
        prompt1          = prompt1,
        prompt2          = prompt2,
        prompt3          = prompt3,
        negative         = negative,
        frames_per_scene = frames_per_scene,
        steps            = steps,
        uploaded_filename= uploaded_filename,
        fps              = fps,
        job_id           = job_id,
        seed_offset      = seed_off,
    )

    submit_result = submit_prompt(wf, client_id=job_id)
    prompt_id = submit_result.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI did not return a prompt_id: {submit_result}")

    print(f"[handler] prompt_id={prompt_id} — waiting for completion (timeout 2 h)…")
    history = wait_for_history(prompt_id, timeout=7200)

    # ── collect output ────────────────────────────────────────────────────────
    files = get_output_files(history)
    if not files:
        raise RuntimeError("ComfyUI finished but no output video files found.")

    files.sort(key=lambda x: x["filename"])
    video_path = files[-1]["filepath"]
    print(f"[handler] Output: {video_path}")

    video_url = supabase_upload(video_path, f"{job_id}.mp4")

    return {
        "status":           "success",
        "video_url":        video_url,
        "prompt_id":        prompt_id,
        "scene_1_prompt":   prompt1,
        "scene_2_prompt":   prompt2,
        "scene_3_prompt":   prompt3,
        "frames_per_scene": frames_per_scene,
        "total_frames":     total_frames,
        "fps":              fps,
        "duration_seconds": round(duration_s),
        "duration_minutes": round(duration_s / 60, 1),
    }


runpod.serverless.start({"handler": handler})

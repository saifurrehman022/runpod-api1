"""
tools/check_lora_sidecars.py

Run this on a Pod (or locally) to verify that every entry in registry.json
has both its .safetensors model file and its .json sidecar on disk.

Usage:
    # On a Pod with volume at /workspace/wan-storage:
    python tools/check_lora_sidecars.py

    # Override the loras directory:
    LORA_DIR=/runpod-volume/models/loras python tools/check_lora_sidecars.py

FIXED vs original:
  - LORA_DIR defaults now match handler.py  (models/loras, not models/lora-video)
  - Registry filename is registry.json      (not registry.generated.json)
  - Path is configurable via LORA_DIR env var for Pod vs Serverless
"""

import json
import os
import sys
from collections import defaultdict

# ── Path config — must match src/handler.py LORA_DIR_REL ──────────────────
DEFAULT_LORA_DIR = os.environ.get(
    "LORA_DIR",
    "/workspace/criminal_jade_guineafowl/models/loras",
    "/runpod-volume/criminal_jade_guineafowl/models/loras",# Pod default
)
# On serverless the volume mounts at /runpod-volume, so:
#   LORA_DIR=/runpod-volume/models/loras python tools/check_lora_sidecars.py

LORA_DIR = DEFAULT_LORA_DIR
REGISTRY = os.path.join(LORA_DIR, "registry.json")      # was registry.generated.json


def load_registry():
    if not os.path.exists(REGISTRY):
        print(f"ERROR: registry not found at {REGISTRY}")
        print("Create it with tools/generate_registry.py or download it.")
        sys.exit(1)
    with open(REGISTRY, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["loras"] if isinstance(data, dict) else data


def main():
    print(f"LORA_DIR : {LORA_DIR}")
    print(f"REGISTRY : {REGISTRY}")
    print()

    entries = load_registry()
    by_cat  = defaultdict(list)
    for e in entries:
        cat = e.get("category", "uncategorized")
        by_cat[cat].append(e)

    missing_json = []
    missing_safe = []

    for cat, items in by_cat.items():
        for e in items:
            alias = e.get("alias", "NO_ALIAS")
            fn    = e.get("filename", alias + ".safetensors")
            base  = os.path.splitext(fn)[0]

            safe_path = os.path.join(LORA_DIR, fn)
            json_path = os.path.join(LORA_DIR, base + ".json")

            if not os.path.isfile(safe_path):
                missing_safe.append((cat, alias, fn))
            if not os.path.isfile(json_path):
                missing_json.append((cat, alias, base + ".json"))

    print("=== LoRA SIDE-CAR REPORT ===")
    print(f"Total entries      : {len(entries)}")
    print(f"Missing .safetensors : {len(missing_safe)}")
    print(f"Missing .json sidecar: {len(missing_json)}")

    def dump(title, rows):
        print(f"\n--- {title} ---")
        if not rows:
            print("  (none)")
            return
        for c, a, f in rows:
            print(f"  [{c}] {a}  ->  {f}")

    dump("MISSING SAFETENSORS", missing_safe)
    dump("MISSING JSON SIDECARS", missing_json)

    if missing_safe or missing_json:
        print("\nRun tools/download_wan22_models.sh to fetch missing files.")
        sys.exit(1)
    else:
        print("\nAll entries present — ready to deploy.")


if __name__ == "__main__":
    main()

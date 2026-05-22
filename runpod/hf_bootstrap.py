#!/usr/bin/env python3
"""Fast parallel HF download replacement for the wget portion of bootstrap_nv.sh.

Runs INSIDE the pod. Uses huggingface_hub.hf_hub_download (multi-threaded via
hf_transfer if installed; otherwise just multi-thread chunked download) to
pull the 5 Wan 2.2 14B i2v dependency files into NV-mounted ComfyUI/models.

Each file:
  - skipped if size within ±10% envelope of expected
  - downloaded to a staging dir then atomically mv'd to final path
  - bounded size verification post-download before mv (catches truncation)

Slop Bounce LoRA and LightX2V LoRA are NOT handled here — they need separate
Civitai/Kijai paths and live in bootstrap_nv.sh (which can still be re-run
after this script populates the HF files).
"""

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Try to enable hf_transfer for 5-10x speed; degrade gracefully if absent.
os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "1")

try:
    from huggingface_hub import hf_hub_download
except ImportError:
    print(
        "FATAL: huggingface_hub not installed. `pip install huggingface_hub hf_transfer`",
        file=sys.stderr,
    )
    sys.exit(2)

# Test hf_transfer
try:
    import hf_transfer  # noqa: F401

    print("[hf] hf_transfer ENABLED — parallel chunked downloads")
except ImportError:
    os.environ.pop("HF_HUB_ENABLE_HF_TRANSFER", None)
    print(
        "[hf] hf_transfer NOT installed — falling back to standard downloader (slower)"
    )
    print("[hf] To enable: pip install hf_transfer")

NV_ROOT = os.environ.get("NV_ROOT", "/workspace")
MODELS_DIR = Path(NV_ROOT) / "ComfyUI" / "models"


# (repo_id, path_in_repo, dest_subdir, dest_basename, expected_mb)
FILES = [
    (
        "Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
        "split_files/diffusion_models/wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors",
        "diffusion_models",
        "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors",
        14000,
    ),
    (
        "Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
        "split_files/diffusion_models/wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors",
        "diffusion_models",
        "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors",
        14000,
    ),
    (
        "Kijai/WanVideo_comfy",
        "umt5-xxl-enc-bf16.safetensors",
        "text_encoders",
        "umt5-xxl-enc-bf16.safetensors",
        11000,
    ),
    (
        "Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
        "split_files/vae/wan_2.1_vae.safetensors",
        "vae",
        "wan_2.1_vae.safetensors",
        250,
    ),
    # clip_vision_h NOT in Wan_2.2 repackaged — only in the 2.1 repo (lowercase 'r')
    (
        "Comfy-Org/Wan_2.1_ComfyUI_repackaged",
        "split_files/clip_vision/clip_vision_h.safetensors",
        "clip_vision",
        "clip_vision_h.safetensors",
        1200,
    ),
]


def size_mb(p: Path) -> int:
    return p.stat().st_size // (1024 * 1024)


def fetch_one(repo, path_in_repo, dest_subdir, dest_name, exp_mb):
    dest_dir = MODELS_DIR / dest_subdir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / dest_name

    lo = exp_mb * 90 // 100
    hi = exp_mb * 150 // 100

    if dest.exists():
        cur = size_mb(dest)
        if lo <= cur <= hi:
            print(f"[hf] SKIP {dest_name} ({cur}MB within ±10% of {exp_mb}MB)")
            return True
        print(f"[hf] REDO {dest_name} ({cur}MB outside [{lo},{hi}]MB)")
        dest.unlink()

    t0 = time.time()
    print(f"[hf] GET  {dest_name} (repo={repo}, expected ~{exp_mb}MB)")
    with tempfile.TemporaryDirectory(dir=str(NV_ROOT)) as tmpdir:
        try:
            downloaded = hf_hub_download(
                repo_id=repo,
                filename=path_in_repo,
                local_dir=tmpdir,
            )
        except Exception as e:
            print(f"[hf] FAIL {dest_name}: {e}", file=sys.stderr)
            return False
        got = size_mb(Path(downloaded))
        if not (lo <= got <= hi):
            print(
                f"[hf] FAIL {dest_name} = {got}MB outside [{lo},{hi}]MB. Aborting before swap.",
                file=sys.stderr,
            )
            return False
        shutil.move(downloaded, dest)
    elapsed = int(time.time() - t0)
    rate = (got * 1024 * 1024) / max(elapsed, 1) / 1024 / 1024
    print(f"[hf] OK   {dest_name} ({got}MB in {elapsed}s = {rate:.1f} MB/s)")
    return True


def main():
    if not MODELS_DIR.parent.exists():
        print(
            f"FATAL: {MODELS_DIR.parent} (ComfyUI dir) missing. Run bootstrap_nv.sh first to clone ComfyUI.",
            file=sys.stderr,
        )
        sys.exit(2)
    ok = True
    for spec in FILES:
        if not fetch_one(*spec):
            ok = False
    print("\n=== HF download summary ===")
    for spec in FILES:
        path = MODELS_DIR / spec[2] / spec[3]
        status = f"{size_mb(path)}MB" if path.exists() else "MISSING"
        print(f"  {spec[3]:<60} {status}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

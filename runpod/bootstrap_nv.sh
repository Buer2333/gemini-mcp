#!/usr/bin/env bash
# bootstrap_nv.sh — one-time model download into a RunPod Network Volume.
#
# Run this INSIDE a freshly deployed pod with the Network Volume mounted at /workspace.
# Subsequent pod restarts (or new pods deployed on the same NV) skip this entire script.
#
# Downloads (~30GB total):
#   - Wan 2.2 i2v 14B dual-expert fp8 (high + low noise)
#   - LightX2V i2v speed LoRAs (high + low)
#   - ComfyUI + WanVideoWrapper custom node
#   - Slop Bounce LoRA (requires CIVITAI_TOKEN env var)
#
# Idempotent: re-running skips files already present (size-checked, not just exists).

set -euo pipefail

NV_ROOT="${NV_ROOT:-/workspace}"
COMFY_DIR="${NV_ROOT}/ComfyUI"
MODELS_DIR="${COMFY_DIR}/models"
HF_HOST="${HF_HOST:-https://huggingface.co}"

log() { printf '[bootstrap] %s\n' "$*"; }
die() { printf '[bootstrap] FATAL: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Sanity
# ---------------------------------------------------------------------------
[[ -d "$NV_ROOT" ]] || die "NV_ROOT '$NV_ROOT' missing — is the Network Volume mounted?"
if ! mount | grep -q " on $NV_ROOT "; then
    log "⚠️  WARN: $NV_ROOT does not appear to be a separate mount."
    log "⚠️  WARN: Downloaded files will live on container disk and will be LOST"
    log "⚠️  WARN: when the pod is terminated. Verify NV is properly attached."
    log "⚠️  WARN: Sleeping 10s — Ctrl+C to abort if this is unintended."
    sleep 10
fi

if ! command -v wget >/dev/null || ! command -v git >/dev/null; then
    log "Installing wget/git via apt-get..."
    apt-get update -qq
    apt-get install -y wget git
fi

# Verify free space (need ~35GB headroom; ~30GB download + ComfyUI install)
free_gb=$(df -BG --output=avail "$NV_ROOT" | tail -1 | tr -dc 0-9)
(( free_gb >= 35 )) || die "Need 35GB free in $NV_ROOT, have ${free_gb}GB. Resize NV or clear space."

# Note: `mkdir -p "$MODELS_DIR"` is intentionally deferred until AFTER the
# ComfyUI clone block. An earlier draft created model subdirs first, which
# made $COMFY_DIR non-empty and broke `git clone` on fresh NVs.

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# Download with bounded size check. Envelope is asymmetric: corruption typically
# shrinks files (truncated downloads, partial transfers), while re-uploads /
# repackaging on the source mirror can legitimately grow files by 20-40%. So
# lo is tighter than hi.
# Args: url dest expected_mb
fetch() {
    local url="$1" dest="$2" exp_mb="$3"
    local lo=$(( exp_mb * 90 / 100 ))
    local hi=$(( exp_mb * 150 / 100 ))
    if [[ -f "$dest" ]]; then
        local cur_mb
        cur_mb=$(du -m "$dest" | cut -f1)
        if (( cur_mb >= lo && cur_mb <= hi )); then
            log "SKIP $(basename "$dest") (${cur_mb}MB within ±10% of ${exp_mb}MB)"
            return 0
        fi
        log "REDO $(basename "$dest") (${cur_mb}MB outside [${lo},${hi}]MB envelope)"
        rm -f "$dest"
    fi
    log "GET  $(basename "$dest")"
    # --tries 3 for transient HF 429/503; -c for resume on retry
    wget -q --show-progress --tries=3 -c -O "$dest.part" "$url"
    # Verify post-download size before swap
    local got_mb
    got_mb=$(du -m "$dest.part" | cut -f1)
    if (( got_mb < lo || got_mb > hi )); then
        rm -f "$dest.part"
        die "Downloaded $(basename "$dest") = ${got_mb}MB outside [${lo},${hi}]MB. Aborting before swap."
    fi
    mv "$dest.part" "$dest"
}

# Civitai download with bounded size check. CIVITAI_TOKEN required.
# Envelope is asymmetric — see fetch() for rationale. Civitai LoRA re-uploads
# at higher rank are common, so upper bound is generous.
# Args: model_version_id dest_dir filename expected_mb
fetch_civitai() {
    local mvid="$1" dst_dir="$2" name="$3" exp_mb="$4"
    local dest="$dst_dir/$name"
    local lo=$(( exp_mb * 80 / 100 ))
    local hi=$(( exp_mb * 200 / 100 ))
    if [[ -f "$dest" ]]; then
        local cur_mb
        cur_mb=$(du -m "$dest" | cut -f1)
        if (( cur_mb >= lo && cur_mb <= hi )); then
            log "SKIP $name (${cur_mb}MB within ±20% of ${exp_mb}MB)"
            return 0
        fi
        log "REDO $name (${cur_mb}MB outside [${lo},${hi}]MB)"
        rm -f "$dest"
    fi
    [[ -n "${CIVITAI_TOKEN:-}" ]] || die "CIVITAI_TOKEN not set — needed for Slop Bounce LoRA. Get one at civitai.com/user/account"
    log "GET  $name (civitai mvid=$mvid)"
    wget -q --show-progress --tries=3 \
        --header="Authorization: Bearer $CIVITAI_TOKEN" \
        -O "$dest.part" \
        "https://civitai.com/api/download/models/${mvid}"
    local got_mb
    got_mb=$(du -m "$dest.part" | cut -f1)
    if (( got_mb < lo || got_mb > hi )); then
        rm -f "$dest.part"
        die "Downloaded $name = ${got_mb}MB outside [${lo},${hi}]MB. Wrong MVID or bad token?"
    fi
    mv "$dest.part" "$dest"
}

# ---------------------------------------------------------------------------
# ComfyUI install / update
# ---------------------------------------------------------------------------
if [[ -d "$COMFY_DIR/.git" ]]; then
    log "ComfyUI already cloned at $COMFY_DIR"
elif [[ -d "$COMFY_DIR" ]] && [[ -n "$(ls -A "$COMFY_DIR" 2>/dev/null)" ]]; then
    die "$COMFY_DIR exists but is not a git repo and is non-empty. Manual cleanup needed — \`rm -rf $COMFY_DIR\` if this is a leftover from a failed bootstrap, then re-run."
else
    log "Cloning ComfyUI into $COMFY_DIR"
    git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git "$COMFY_DIR"
    pip install -q -r "$COMFY_DIR/requirements.txt"
fi

# Models subdirs MUST come after ComfyUI is cloned, otherwise an empty
# $COMFY_DIR with skeletal models/ subdirs would block `git clone` next time.
mkdir -p "$MODELS_DIR"/{diffusion_models,loras,text_encoders,vae,clip_vision}

# WanVideoWrapper custom node (Kijai's, needed for Wan 2.2 MoE workflow)
WANWRAP="$COMFY_DIR/custom_nodes/ComfyUI-WanVideoWrapper"
if [[ ! -d "$WANWRAP/.git" ]]; then
    log "Cloning ComfyUI-WanVideoWrapper"
    git clone --depth 1 https://github.com/kijai/ComfyUI-WanVideoWrapper.git "$WANWRAP"
    [[ -f "$WANWRAP/requirements.txt" ]] && pip install -q -r "$WANWRAP/requirements.txt"
fi

# ---------------------------------------------------------------------------
# Wan 2.2 base models — Comfy-Org HF mirror has the repackaged fp8 files
# ---------------------------------------------------------------------------
WAN_BASE="${HF_HOST}/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files"

fetch "${WAN_BASE}/diffusion_models/wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors" \
      "${MODELS_DIR}/diffusion_models/wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors" 14000

fetch "${WAN_BASE}/diffusion_models/wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors" \
      "${MODELS_DIR}/diffusion_models/wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors" 14000

# T5 text encoder — workflow at wan22_generate.py:333 loads "umt5-xxl-enc-bf16.safetensors"
# (NOT the fp8 variant Comfy-Org mirrors). Source: Kijai's text_encoders repo.
KIJAI_BASE="${HF_HOST}/Kijai/WanVideo_comfy/resolve/main"
fetch "${KIJAI_BASE}/umt5-xxl-enc-bf16.safetensors" \
      "${MODELS_DIR}/text_encoders/umt5-xxl-enc-bf16.safetensors" 11000

# VAE — workflow loads "wan_2.1_vae.safetensors"
fetch "${WAN_BASE}/vae/wan_2.1_vae.safetensors" \
      "${MODELS_DIR}/vae/wan_2.1_vae.safetensors" 250

# CLIP vision — workflow loads "clip_vision_h.safetensors"
fetch "${WAN_BASE}/clip_vision/clip_vision_h.safetensors" \
      "${MODELS_DIR}/clip_vision/clip_vision_h.safetensors" 1200

# ---------------------------------------------------------------------------
# LightX2V speed LoRAs — REQUIRES VERIFICATION before first prod run.
# ---------------------------------------------------------------------------
# Kijai's repo historically publishes ONE LightX2V Wan 2.2 file used as both
# high and low. Two distinct files exist in some forks. If LIGHTX2V_HIGH_URL
# and LIGHTX2V_LOW_URL are unset, we refuse to download — caller must confirm
# by setting them, or by setting LIGHTX2V_SHARED_URL=<url> for the
# "same file for both slots" pattern.
LIGHTX2V_HIGH_URL="${LIGHTX2V_HIGH_URL:-}"
LIGHTX2V_LOW_URL="${LIGHTX2V_LOW_URL:-}"
LIGHTX2V_SHARED_URL="${LIGHTX2V_SHARED_URL:-}"

# XOR guard: setting only one of HIGH/LOW is almost certainly a config bug.
if [[ -n "$LIGHTX2V_HIGH_URL" && -z "$LIGHTX2V_LOW_URL" ]] || \
   [[ -z "$LIGHTX2V_HIGH_URL" && -n "$LIGHTX2V_LOW_URL" ]]; then
    die "Set BOTH LIGHTX2V_HIGH_URL and LIGHTX2V_LOW_URL, or neither. Mixed state is ambiguous."
fi

if [[ -n "$LIGHTX2V_HIGH_URL" && -n "$LIGHTX2V_LOW_URL" ]]; then
    fetch "$LIGHTX2V_HIGH_URL" "${MODELS_DIR}/loras/i2v_lightx2v_high_noise_model.safetensors" 600
    fetch "$LIGHTX2V_LOW_URL"  "${MODELS_DIR}/loras/i2v_lightx2v_low_noise_model.safetensors"  600
elif [[ -n "$LIGHTX2V_SHARED_URL" ]]; then
    log "LightX2V: using same file for both high and low slots (LIGHTX2V_SHARED_URL set)"
    fetch "$LIGHTX2V_SHARED_URL" "${MODELS_DIR}/loras/i2v_lightx2v_high_noise_model.safetensors" 600
    cp "${MODELS_DIR}/loras/i2v_lightx2v_high_noise_model.safetensors" \
       "${MODELS_DIR}/loras/i2v_lightx2v_low_noise_model.safetensors"
else
    log "⚠️  SKIP LightX2V — set LIGHTX2V_HIGH_URL+LIGHTX2V_LOW_URL (distinct files)"
    log "⚠️       OR LIGHTX2V_SHARED_URL (single file copied to both slots)."
    log "⚠️       Without these, modes test/mid/hd (which use_lightx2v=True) will FAIL."
    log "⚠️       Browse https://huggingface.co/Kijai/WanVideo_comfy/tree/main/Lightx2v"
fi

# ---------------------------------------------------------------------------
# Slop Bounce LoRA — REQUIRES distinct mvids for high and low.
# ---------------------------------------------------------------------------
# https://civitai.com/models/1944129/slop-bounce-wan-22-i2v has separate
# HIGH and LOW noise variants as files. The Civitai page shows ONE
# modelVersionId that bundles both as multi-file download, but the API
# download endpoint /api/download/models/{mvid} returns a ZIP or the
# primary file. We require explicit envs to avoid downloading the same
# weights twice and silently degrading MoE inference.
SLOP_HIGH_MVID="${SLOP_HIGH_MVID:-}"
SLOP_LOW_MVID="${SLOP_LOW_MVID:-}"

if [[ -z "${CIVITAI_TOKEN:-}" ]]; then
    log "⚠️  SKIP Slop Bounce — set CIVITAI_TOKEN (civitai.com/user/account)"
    log "⚠️       AND SLOP_HIGH_MVID + SLOP_LOW_MVID to distinct version IDs."
    log "⚠️       Manual fallback: download both .safetensors from the civitai page"
    log "⚠️       and drop into ${MODELS_DIR}/loras/ as slop_bounce_{high,low}.safetensors"
elif [[ -z "$SLOP_HIGH_MVID" || -z "$SLOP_LOW_MVID" ]]; then
    die "CIVITAI_TOKEN is set but SLOP_HIGH_MVID / SLOP_LOW_MVID not. Refusing to default both to the same ID (would silently break MoE)."
elif [[ "$SLOP_HIGH_MVID" == "$SLOP_LOW_MVID" ]]; then
    die "SLOP_HIGH_MVID == SLOP_LOW_MVID — these must be DISTINCT version IDs (Wan 2.2 MoE needs two different LoRAs)."
else
    fetch_civitai "$SLOP_HIGH_MVID" "${MODELS_DIR}/loras" "slop_bounce_high.safetensors" 300
    fetch_civitai "$SLOP_LOW_MVID"  "${MODELS_DIR}/loras" "slop_bounce_low.safetensors"  300
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
log ""
log "=== Bootstrap complete ==="
log "NV usage:"
du -sh "$MODELS_DIR"/* 2>/dev/null | sed 's/^/  /'
log ""
log "Total in $NV_ROOT:"
df -h "$NV_ROOT" | tail -1 | awk '{printf "  used %s / total %s (%s)\n", $3, $2, $5}'
log ""
log "Next: launch ComfyUI on the pod:"
log "  cd $COMFY_DIR && python main.py --listen 0.0.0.0 --port 8188"

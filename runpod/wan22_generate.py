#!/usr/bin/env python3
"""
Wan 2.2 14B I2V video generator via RunPod ComfyUI.

Usage:
    # Quick test (low res, fast)
    python wan22_generate.py --prompt "muscle glow effect" --image ref.png --mode test

    # Production output (full res)
    python wan22_generate.py --prompt "muscle glow effect" --image ref.png --mode prod

    # Start pod if stopped
    python wan22_generate.py --start-pod

    # Stop pod
    python wan22_generate.py --stop-pod
"""

import argparse
import hashlib
import json
import os
import sys
import time
import uuid

import requests

# --- Proxy bypass (2026-05-26) ---------------------------------------------
# RunPod endpoints (api.runpod.io + *-PORT.proxy.runpod.net) are directly reachable
# from the operator host (verified: direct curl /system_stats -> HTTP 200). A local
# HTTP(S)_PROXY (e.g. Clash Verge at 127.0.0.1:7897) is therefore an unnecessary
# middleman that transiently drops long-poll connections — observed mid-FLF2V as
# `ProxyError: RemoteDisconnected` on /history after ~16min, crashing the driver while
# the pod-side gen kept running.
#
# ROOT CAUSE (2026-06-02 correction): popping the proxy env vars is NOT enough on macOS.
# `requests` (trust_env=True default) resolves proxies via urllib `getproxies()`, which
# on macOS ALSO reads the *system* network proxy (System Settings / scutil) — Clash Verge
# sets itself there too. So env-pop alone still let requests pick up the system proxy and
# intermittently drop (observed 2026-06-02: morph OK, bounce ProxyError mid-run). The old
# "== trust_env=False" claim was wrong for the macOS sysconf path.
#
# FIX: also set NO_PROXY for the RunPod domains. requests' should_bypass_proxies() matches
# NO_PROXY first and returns {} for those hosts — bypassing BOTH env and macOS system proxy.
# NOTE: only affects THIS local process; the embedded pod-side auto_stop script (an
# f-string literal) and its localhost calls are untouched.
for _pv in (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
):
    os.environ.pop(_pv, None)
# Force-bypass the system proxy (macOS sysconf) for RunPod + localhost.
_NO_PROXY = "runpod.net,runpod.io,127.0.0.1,localhost"
os.environ["NO_PROXY"] = os.environ["no_proxy"] = _NO_PROXY
# ---------------------------------------------------------------------------

# Optional: unified AI-gen logger (sidecar JSON + index).
# Soft import so the script runs even if the logger lib is missing.
try:
    sys.path.insert(0, os.path.expanduser("~/claude-dev/libs/claude-helpers"))
    from ai_gen_logger import log_generation as _log_generation  # type: ignore
except Exception:
    _log_generation = None

# Config — credentials and pod id must come from env. No literal defaults
# to avoid secret-scanning false positives and accidental key sharing in git
# history.
RUNPOD_API_KEY = os.environ.get("RUNPOD_API_KEY", "")
POD_ID = os.environ.get("RUNPOD_POD_ID", "")
COMFYUI_PORT = 8188

if not RUNPOD_API_KEY:
    sys.stderr.write(
        "ERROR: RUNPOD_API_KEY env var not set. Get one at runpod.io/console/user/settings.\n"
    )
    sys.exit(2)

# Mode presets
PRESETS = {
    "test": {
        "width": 320,
        "height": 576,
        "num_frames": 33,
        "steps": 8,
        "use_lightx2v": True,
        "desc": "Fast iteration (~1.5min, $0.004)",
    },
    "prod": {
        "width": 480,
        "height": 832,
        "num_frames": 81,
        "steps": 30,
        "use_lightx2v": False,
        "desc": "Full quality (~16min, $0.044)",
    },
    "mid": {
        "width": 480,
        "height": 832,
        "num_frames": 49,
        "steps": 15,
        "use_lightx2v": True,
        "desc": "Medium quality (~4min, $0.01)",
    },
    "hd": {
        "width": 720,
        "height": 1280,
        "num_frames": 49,
        "steps": 20,
        "use_lightx2v": True,
        "desc": "HD quality (~8min, $0.03)",
    },
    # fast81 (plan Phase 3.2, experimental): 与 prod 同尺寸/同帧数、8 步 + lightx2v 蒸馏，
    # 专供 bounce 段净 A/B（帧数/步数与 prod 解耦仅剩 lightx2v+步数变量）。
    # 三闸(肉眼/skin_gate/chroma_qc)全过前不得进产线；morph 段永远 prod（lightx2v 杀 morph 语义）。
    "fast81": {
        "width": 480,
        "height": 832,
        "num_frames": 81,
        "steps": 8,
        "use_lightx2v": True,
        "desc": "EXPERIMENTAL bounce-only lightx2v A/B (~4min est)",
    },
}


# --- 机器可读结果协议（plan Phase 0A.2）-------------------------------------
# NDJSON 事件流，每 job 一个独立文件。未传 --result-json 时 enabled=False，所有
# emit 均为 no-op —— 默认路径行为逐字节不变。
#
# 事件与时点：
#   submitting      POST 前写入并 fsync（提交意图先落盘，覆盖「响应丢失」窗口）
#   submitted       拿到 prompt_id 后写入并 fsync，落盘后才进入轮询
#   remote_completed 拿到 history completed 的 filename 时、下载前
#   downloaded      下载并校验后（带 sha256）
#   failed          class=network|business + stage + detail（见 plan 0A.4 三分类）
class ResultEmitter:
    def __init__(self, path=None, job_id=None, pod_id=None):
        self.path = path
        self.job_id = job_id
        self.pod_id = pod_id
        self.enabled = bool(path)

    def emit(self, event, fields=None, fsync=False):
        if not self.enabled:
            return None
        rec = {
            "event": event,
            "job_id": self.job_id,
            "pod_id": self.pod_id,
            "ts": time.time(),
        }
        if fields:
            rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()  # 每条事件写后立即 flush
            if fsync:
                os.fsync(f.fileno())  # submitting/submitted 额外 fsync
        return rec

    def emit_failed(self, err_class, stage, detail):
        """err_class ∈ {network, business}；stage ∈ {upload, submit_uncertain,
        submit_rejected, poll, remote, download}（plan 0A.4）。"""
        return self.emit(
            "failed",
            {"class": err_class, "stage": stage, "detail": str(detail)[:500]},
            fsync=True,
        )


_NULL_EMITTER = ResultEmitter()
# ---------------------------------------------------------------------------


class SubmitUncertain(RuntimeError):
    """POST /prompt 响应不确定（5xx/408/429/timeout/reset/空/不可解析）。

    plan 0A.4 (c)：不得当作「确定未受理」——远端任务可能已创建。仅在启用
    --result-json（编排模式）时抛出；默认路径保持原有异常/返回语义。
    """


def get_base_url():
    return f"https://{POD_ID}-{COMFYUI_PORT}.proxy.runpod.net"


def graphql(query):
    r = requests.post(
        "https://api.runpod.io/graphql",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {RUNPOD_API_KEY}",
        },
        json={"query": query},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def pod_status():
    d = graphql(
        f'query {{ pod(input: {{ podId: "{POD_ID}" }}) {{ id desiredStatus runtime {{ uptimeInSeconds }} }} }}'
    )
    pod = d.get("data", {}).get("pod")
    if not pod:
        return "NOT_FOUND"
    if pod.get("runtime"):
        return "RUNNING"
    return pod.get("desiredStatus", "UNKNOWN")


def wait_for_comfyui_ready(timeout=120):
    """Poll ComfyUI /system_stats until it responds with valid JSON.

    Pod RUNNING status ≠ ComfyUI listening. ComfyUI app typically needs
    30-90s after VM boot before it accepts API requests.
    """
    base = get_base_url()
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = requests.get(f"{base}/system_stats", timeout=10)
            if r.status_code == 200:
                r.json()  # raises if not JSON
                elapsed = int(time.time() - t0)
                print(f"  ComfyUI ready after {elapsed}s")
                return True
        except (
            requests.exceptions.SSLError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.JSONDecodeError,
            ValueError,
        ):
            pass
        time.sleep(5)
    print(f"  ComfyUI not ready after {timeout}s — proceeding anyway")
    return False


def start_pod():
    status = pod_status()
    if status == "RUNNING":
        print("Pod already running")
        wait_for_comfyui_ready()
        return True
    print("Starting pod...")
    graphql(
        f'mutation {{ podResume(input: {{ podId: "{POD_ID}", gpuCount: 1 }}) {{ id }} }}'
    )
    for i in range(30):
        time.sleep(10)
        if pod_status() == "RUNNING":
            print(f"Pod started in {(i + 1) * 10}s")
            ensure_comfyui_running()
            wait_for_comfyui_ready(timeout=180)
            return True
        print(f"  waiting... ({(i + 1) * 10}s)")
    print("Timeout starting pod")
    return False


def stop_pod():
    graphql(f'mutation {{ podStop(input: {{ podId: "{POD_ID}" }}) {{ id }} }}')
    print("Pod stopped")


def terminate_pod(force: bool = False):
    """DELETE pod entirely. Frees container disk billing (vs stop_pod which keeps
    EXITED pod and charges $0.20/GB/mo storage). Use at end of daily batch when
    you've offloaded models/state to a Network Volume.

    Safety: refuses to terminate a RUNNING pod unless force=True. This catches
    the common typo where --terminate-pod is reached for instead of --stop-pod
    while a generation is in flight.

    Returns True on confirmed deletion, False on guard-trip or API failure.
    Always verifies via follow-up status query — podTerminate returns Void so
    the absence of errors alone is not sufficient evidence.
    """
    cur = pod_status()
    if cur == "NOT_FOUND":
        print(f"[SAFETY] Pod {POD_ID} already gone — nothing to terminate")
        return True
    if cur == "RUNNING" and not force:
        print(
            f"[GUARD] Refusing to terminate RUNNING pod {POD_ID}. "
            "Stop it first (--stop-pod) or pass --force to override."
        )
        return False
    try:
        resp = graphql(f'mutation {{ podTerminate(input: {{ podId: "{POD_ID}" }}) }}')
    except requests.RequestException as e:
        print(f"  podTerminate transport error: {e}")
        return False
    if "errors" in resp:
        print(f"  podTerminate errors: {resp['errors']}")
        return False
    # Verify: a terminated pod returns NOT_FOUND on subsequent query
    for _ in range(6):
        time.sleep(5)
        if pod_status() == "NOT_FOUND":
            print(f"[SAFETY] Pod {POD_ID} terminated and verified gone")
            return True
    print(f"[WARN] podTerminate sent but pod {POD_ID} still findable after 30s")
    return False


def ensure_comfyui_running():
    """Start ComfyUI on pod via Jupyter kernel API if port 8188 isn't serving.

    Required when the pod's startup script doesn't auto-launch ComfyUI
    (or it crashed). Uses the same Jupyter WebSocket pattern as ensure_auto_stop.
    Returns True if ComfyUI started or was already running.
    """
    base = get_base_url()
    # Quick check: is ComfyUI already serving?
    try:
        r = requests.get(f"{base}/system_stats", timeout=10)
        if r.status_code == 200:
            try:
                r.json()
                print("  ComfyUI already running")
                return True
            except Exception:
                pass
    except Exception:
        pass

    print("  ComfyUI not responding — attempting to start via Jupyter...")
    jupyter_base = base.replace("-8188.", "-8888.")
    last_err = None
    for attempt in range(3):
        try:
            s = requests.Session()
            resp = s.get(jupyter_base, timeout=15)
            if resp.status_code != 200:
                raise RuntimeError(f"Jupyter unreachable: HTTP {resp.status_code}")
            xsrf = s.cookies.get("_xsrf", "")
            headers = {"X-XSRFToken": xsrf}
            # Create a kernel
            rk = s.post(
                f"{jupyter_base}/api/kernels",
                json={"name": "python3"},
                headers=headers,
                timeout=15,
            )
            if rk.status_code not in (200, 201):
                raise RuntimeError(f"kernel create: HTTP {rk.status_code}")
            kernel_id = rk.json()["id"]

            import websocket as ws_lib

            ws_url = jupyter_base.replace("https://", "wss://")
            cookies = "; ".join([f"{k}={v}" for k, v in s.cookies.items()])
            ws = ws_lib.create_connection(
                f"{ws_url}/api/kernels/{kernel_id}/channels",
                cookie=cookies,
                timeout=15,
            )
            code = (
                "import subprocess, os, time\n"
                "res = subprocess.run(['pgrep', '-f', 'ComfyUI/main.py'], capture_output=True)\n"
                "if res.returncode != 0:\n"
                "    os.makedirs('/workspace', exist_ok=True)\n"
                "    subprocess.Popen(\n"
                "        'cd /workspace/ComfyUI && nohup python main.py --listen 0.0.0.0 --port 8188 "
                "> /workspace/comfyui.log 2>&1 &',\n"
                "        shell=True,\n"
                "    )\n"
                "    print('ComfyUI start issued')\n"
                "else:\n"
                "    print('ComfyUI PID:', res.stdout.decode().strip())\n"
            )
            ws.send(
                json.dumps(
                    {
                        "header": {
                            "msg_id": "start_comfy",
                            "msg_type": "execute_request",
                            "username": "",
                            "session": "sc",
                        },
                        "parent_header": {},
                        "metadata": {},
                        "content": {"code": code, "silent": False},
                    }
                )
            )
            ws.close()
            print("  ComfyUI start command sent")
            return True
        except Exception as e:
            last_err = e
            print(
                f"  ensure_comfyui_running attempt {attempt + 1}/3 failed: {type(e).__name__}: {str(e)[:100]}"
            )
            if attempt < 2:
                time.sleep(10)
    print(f"[WARN] Could not start ComfyUI via Jupyter: {last_err}")
    return False


def upload_image(image_path):
    """Upload with 3-retry on SSL/connection errors (pod boot-time flakiness)."""
    base = get_base_url()
    last_err = None
    for attempt in range(3):
        try:
            with open(image_path, "rb") as f:
                r = requests.post(
                    f"{base}/upload/image",
                    files={"image": (os.path.basename(image_path), f)},
                    data={"overwrite": "true"},
                    timeout=60,
                )
            return r.json().get("name")
        except (
            requests.exceptions.SSLError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as e:
            last_err = e
            print(f"  Upload attempt {attempt + 1}/3 failed: {type(e).__name__}")
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Upload failed after 3 attempts: {last_err}")


# morph 段 LoRA 选择（2026-07-16 T-20260712-001 M1 矩阵）。默认 slop_bounce 保持产线现行为。
# high/low 成对（Wan2.2 双专家）。文件须先在 NV loras/ 就位（见 M1-runpod-protocol.md §1b）。
MORPH_LORAS = {
    "slop_bounce": ("slop_bounce_high.safetensors", "slop_bounce_low.safetensors"),
    "disintegration": (
        "[Wan2.2]Disintegration_Redmond_high_noise.safetensors",
        "[Wan2.2]Disintegration_Redmond_low_noise.safetensors",
    ),
    # M1 臂A(无LoRA基线)=任选一项 + --lora-strength 0；臂C Muscle(Wan2.1单侧)待实际文件名补
}


def build_workflow(
    prompt,
    negative_prompt,
    image_name,
    preset,
    seed,
    lora_strength,
    single_model=False,
    end_image_name=None,
    num_frames_override=None,
    morph_lora="slop_bounce",
    filename_prefix=None,
):
    _lora_hi, _lora_lo = MORPH_LORAS.get(morph_lora, MORPH_LORAS["slop_bounce"])
    p = PRESETS[preset]
    half_steps = p["steps"] // 2
    # right-size frame count (2026-06-25): render only the frames a segment needs
    # instead of the preset's fixed 81 — Wan render time scales ~linearly with
    # num_frames, so 48-frame transform / 24-frame intake cut RunPod time a lot
    # without touching prod quality (same MoE, no lightx2v). Falls back to preset.
    nf = num_frames_override if num_frames_override else p["num_frames"]

    workflow = {
        # High noise model (always loaded)
        "1h": {
            "class_type": "WanVideoModelLoader",
            "inputs": {
                "model": "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors",
                "base_precision": "bf16",
                "quantization": "disabled",
                "load_device": "main_device",
                "lora": ["9h", 0],
            },
        },
        "2": {
            "class_type": "WanVideoVAELoader",
            "inputs": {"model_name": "wan_2.1_vae.safetensors", "precision": "bf16"},
        },
        "3": {
            "class_type": "LoadWanVideoT5TextEncoder",
            "inputs": {
                "model_name": "umt5-xxl-enc-bf16.safetensors",
                "precision": "bf16",
                "load_device": "offload_device",
            },
        },
        "4": {
            "class_type": "WanVideoTextEncode",
            "inputs": {
                "positive_prompt": prompt,
                "negative_prompt": negative_prompt,
                "t5": ["3", 0],
                "force_offload": True,
            },
        },
        "5": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "6": {
            "class_type": "CLIPVisionLoader",
            "inputs": {"clip_name": "clip_vision_h.safetensors"},
        },
        "7": {
            "class_type": "WanVideoClipVisionEncode",
            "inputs": {
                "clip_vision": ["6", 0],
                "image_1": ["5", 0],
                "strength_1": 1.0,
                "strength_2": 0.0,
                "crop": "center",
                "combine_embeds": "average",
                "force_offload": True,
            },
        },
        "8": {
            "class_type": "WanVideoImageToVideoEncode",
            "inputs": {
                "width": p["width"],
                "height": p["height"],
                "num_frames": nf,
                "noise_aug_strength": 0.0,
                "start_latent_strength": 1.0,
                "end_latent_strength": 1.0,
                "force_offload": True,
                "vae": ["2", 0],
                "clip_embeds": ["7", 0],
                "start_image": ["5", 0],
            },
        },
        # LoRA high noise
        "9h": {
            "class_type": "WanVideoLoraSelect",
            "inputs": {
                "lora": _lora_hi,
                "strength": lora_strength,
                "merge_loras": False,
            },
        },
    }

    # FLF2V (first-last-frame): anchor the morph to end on a clean target frame.
    # WanVideoImageToVideoEncode supports end_image; end_latent_strength=1.0 already set above.
    if end_image_name:
        workflow["5b"] = {
            "class_type": "LoadImage",
            "inputs": {"image": end_image_name},
        }
        workflow["8"]["inputs"]["end_image"] = ["5b", 0]

    if single_model:
        # Single model: use high_noise for ALL steps (avoids 14GB model swap deadlock)
        workflow["11"] = {
            "class_type": "WanVideoSampler",
            "inputs": {
                "model": ["1h", 0],
                "image_embeds": ["8", 0],
                "steps": p["steps"],
                "cfg": 5.0,
                "shift": 8.0,
                "seed": seed,
                "force_offload": True,
                "scheduler": "unipc",
                "riflex_freq_index": 0,
                "text_embeds": ["4", 0],
                "start_step": 0,
                "end_step": p["steps"],
            },
        }
        decode_source = "11"
    else:
        # MoE: two-stage sampling with high and low noise models
        workflow["1l"] = {
            "class_type": "WanVideoModelLoader",
            "inputs": {
                "model": "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors",
                "base_precision": "bf16",
                "quantization": "disabled",
                "load_device": "main_device",
                "lora": ["9l", 0],
            },
        }
        workflow["9l"] = {
            "class_type": "WanVideoLoraSelect",
            "inputs": {
                "lora": _lora_lo,
                "strength": lora_strength,
                "merge_loras": False,
            },
        }
        workflow["11h"] = {
            "class_type": "WanVideoSampler",
            "inputs": {
                "model": ["1h", 0],
                "image_embeds": ["8", 0],
                "steps": p["steps"],
                "cfg": 5.0,
                "shift": 8.0,
                "seed": seed,
                "force_offload": True,
                "scheduler": "unipc",
                "riflex_freq_index": 0,
                "text_embeds": ["4", 0],
                "start_step": 0,
                "end_step": half_steps,
            },
        }
        workflow["11l"] = {
            "class_type": "WanVideoSampler",
            "inputs": {
                "model": ["1l", 0],
                "image_embeds": ["8", 0],
                "steps": p["steps"],
                "cfg": 5.0,
                "shift": 8.0,
                "seed": seed,
                "force_offload": True,
                "scheduler": "unipc",
                "riflex_freq_index": 0,
                "text_embeds": ["4", 0],
                "samples": ["11h", 0],
                "start_step": half_steps,
                "end_step": p["steps"],
            },
        }
        decode_source = "11l"

    # Decode
    workflow["12"] = {
        "class_type": "WanVideoDecode",
        "inputs": {
            "vae": ["2", 0],
            "samples": [decode_source, 0],
            "enable_vae_tiling": True,
            "tile_x": p["width"],
            "tile_y": p["height"],
            "tile_stride_x": p["width"] // 2,
            "tile_stride_y": p["height"] // 2,
        },
    }
    # Save
    # filename_prefix：并发产物隔离用（plan 0A.3）。未传时保持现行 `wan22_<preset>`。
    workflow["13"] = {
        "class_type": "SaveAnimatedWEBP",
        "inputs": {
            "images": ["12", 0],
            "filename_prefix": filename_prefix or f"wan22_{preset}",
            "fps": 16,
            "lossless": False,
            "quality": 85,
            "method": "default",
        },
    }

    # Add LightX2V speed LoRA if enabled
    if p["use_lightx2v"]:
        workflow["lx_h"] = {
            "class_type": "WanVideoLoraSelect",
            "inputs": {
                "lora": "i2v_lightx2v_high_noise_model.safetensors",
                "strength": 1.0,
                "merge_loras": False,
                "prev_lora": ["9h", 0],
            },
        }
        workflow["1h"]["inputs"]["lora"] = ["lx_h", 0]

        if not single_model:
            workflow["lx_l"] = {
                "class_type": "WanVideoLoraSelect",
                "inputs": {
                    "lora": "i2v_lightx2v_low_noise_model.safetensors",
                    "strength": 1.0,
                    "merge_loras": False,
                    "prev_lora": ["9l", 0],
                },
            }
            workflow["1l"]["inputs"]["lora"] = ["lx_l", 0]

    return workflow


def generate(
    prompt,
    image_path,
    mode="test",
    seed=None,
    lora_strength=0.8,
    negative_prompt="blurry, low quality, watermark, text, distorted, ugly, deformed",
    single_model=False,
    end_image_path=None,
    num_frames=None,
    morph_lora="slop_bounce",
    filename_prefix=None,
    emitter=None,
):
    if seed is None:
        seed = int(time.time()) % 2**32

    em = emitter or _NULL_EMITTER
    output_prefix = filename_prefix or f"wan22_{mode}"
    base = get_base_url()
    preset = PRESETS[mode]
    model_mode = "single" if single_model else "MoE"
    print(f"Mode: {mode} ({model_mode}) - {preset['desc']}")
    print(f"Prompt: {prompt[:80]}...")
    print(f"Seed: {seed}, LoRA strength: {lora_strength}")

    # Upload image
    # plan 0A.4 (a)：上传失败发生在 submitting 之前 → failed{network,upload}，
    # batch 侧保持 pending 退避重传，不涉及重提风险。异常始终原样上抛（默认路径不变）。
    print("Uploading image...")
    try:
        image_name = upload_image(image_path)
    except Exception as e:
        em.emit_failed("network", "upload", f"{type(e).__name__}: {e}")
        raise
    print(f"  Uploaded: {image_name}")

    end_image_name = None
    if end_image_path:
        print("Uploading end image (FLF2V)...")
        try:
            end_image_name = upload_image(end_image_path)
        except Exception as e:
            em.emit_failed("network", "upload", f"{type(e).__name__}: {e}")
            raise
        print(f"  Uploaded end: {end_image_name}")

    # Build and submit
    workflow = build_workflow(
        prompt,
        negative_prompt,
        image_name,
        mode,
        seed,
        lora_strength,
        single_model,
        end_image_name=end_image_name,
        num_frames_override=num_frames,
        morph_lora=morph_lora,
        filename_prefix=filename_prefix,
    )
    client_id = str(uuid.uuid4())
    # 提交意图先落盘并 fsync，然后才 POST（plan 0A.2/0A.4：覆盖「服务端已受理但
    # 响应丢失」窗口——resume 可按 output_prefix 收养原任务而非并行重提）。
    em.emit(
        "submitting",
        {"output_prefix": output_prefix, "mode": mode, "seed": seed},
        fsync=True,
    )
    try:
        r = requests.post(
            f"{base}/prompt", json={"prompt": workflow, "client_id": client_id}
        )
    except Exception as e:
        # timeout / connection reset 等传输层异常一律「响应不确定」
        em.emit_failed("network", "submit_uncertain", f"{type(e).__name__}: {e}")
        raise
    # 5xx/408/429 归「响应不确定」：POST 可能已成功，禁止当确定未受理（plan 0A.4 c）。
    # 仅编排模式（启用 emitter）走此分支，默认路径保持原有 .json() 语义。
    if em.enabled and (r.status_code in (408, 429) or r.status_code >= 500):
        detail = f"HTTP {r.status_code}: {str(getattr(r, 'text', ''))[:200]}"
        em.emit_failed("network", "submit_uncertain", detail)
        raise SubmitUncertain(detail)
    try:
        result = r.json()
    except Exception as e:
        # 空响应 / 不可解析响应同属「响应不确定」
        em.emit_failed(
            "network", "submit_uncertain", f"unparseable response: {type(e).__name__}"
        )
        raise

    if result.get("node_errors"):
        print(f"ERROR: {json.dumps(result['node_errors'], indent=2)[:500]}")
        if not result.get("prompt_id"):
            # 可解析 + 含 node_errors + 无 prompt_id = 确定未受理（可安全重提）
            em.emit_failed(
                "business",
                "submit_rejected",
                json.dumps(result["node_errors"], ensure_ascii=False)[:400],
            )
        else:
            # 有 prompt_id 说明远端已建任务 → 不可当确定未受理，交 batch 走收养路径
            em.emit_failed(
                "network",
                "submit_uncertain",
                f"node_errors with prompt_id {result.get('prompt_id')}",
            )
        return None

    prompt_id = result["prompt_id"]
    print(f"Submitted: {prompt_id}")
    # submitted 落盘（fsync）后才进入轮询
    em.emit(
        "submitted",
        {"prompt_id": prompt_id, "output_prefix": output_prefix},
        fsync=True,
    )

    # Poll. range(120)*10s≈1330s was too short for slower 48GB cards: v1 on RTX 6000
    # Ada finished morph at 1258s (barely under), but L40 prod exceeded it -> Timeout
    # returned None while the pod-side gen kept running (2026-06-02 loss). Bumped to
    # ~42min window; env POLL_ROUNDS overrides. Gen still safe on NV /workspace if this
    # ever trips again — recover via scp before terminate.
    t0 = time.time()
    poll_rounds = int(os.environ.get("POLL_ROUNDS", "240"))
    for i in range(poll_rounds):
        time.sleep(10)
        try:
            h = (
                requests.get(f"{base}/history/{prompt_id}", timeout=15)
                .json()
                .get(prompt_id, {})
            )
            st = h.get("status", {})
            if st.get("completed"):
                elapsed = time.time() - t0
                outputs = h.get("outputs", {})
                for nid, out in outputs.items():
                    if "images" in out:
                        for img in out["images"]:
                            filename = img["filename"]
                            print(f"\nCOMPLETED in {elapsed:.0f}s: {filename}")
                            # 下载前先落盘 remote_completed：产物已在 NV，
                            # batch 侧此后只重试下载、不重提（plan 0A.4/0A.5 b）
                            em.emit(
                                "remote_completed",
                                {
                                    "prompt_id": prompt_id,
                                    "remote_filename": filename,
                                    "output_prefix": output_prefix,
                                },
                                fsync=True,
                            )
                            return {
                                "filename": filename,
                                "elapsed": elapsed,
                                "seed": seed,
                                "prompt_id": prompt_id,
                                "output_prefix": output_prefix,
                            }
            if st.get("status_str") == "error":
                _msgs = []
                for m in st.get("messages", []):
                    if isinstance(m, list) and len(m) > 1 and isinstance(m[1], dict):
                        if "exception_message" in m[1]:
                            print(f"ERROR: {m[1]['exception_message'][:300]}")
                            _msgs.append(m[1]["exception_message"])
                # ComfyUI 明确报 error = 业务失败（attempt++，plan 0A.4）
                em.emit_failed(
                    "business", "remote", "; ".join(_msgs) or "comfyui status error"
                )
                return None
            elapsed = time.time() - t0
            print(f"  [{elapsed:.0f}s] generating...", end="\r")
        except requests.exceptions.Timeout:
            pass
        except requests.exceptions.RequestException as e:
            # 轮询期网络错误：runner 退出，batch 侧保持 submitted 不降级（plan 0A.8）
            em.emit_failed("network", "poll", f"{type(e).__name__}: {e}")
            raise
    print("Timeout")
    # 轮询窗口耗尽同样按网络类处理：不得标 terminal failed（plan 0A.5 d）
    em.emit_failed("network", "poll", f"polling window exhausted ({poll_rounds} rounds)")
    return None


def download_output(filename, output_dir=".", emitter=None):
    em = emitter or _NULL_EMITTER
    base = get_base_url()
    url = f"{base}/view?filename={filename}&type=output"
    output_path = os.path.join(output_dir, filename)
    last_detail = ""
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=60)
            if r.status_code == 200 and len(r.content) > 1000:
                with open(output_path, "wb") as f:
                    f.write(r.content)
                print(f"Downloaded: {output_path} ({len(r.content) / 1024:.0f}KB)")
                # sha256 = 下载校验凭据，供 batch 绑定 job/产物（plan 0A.3）
                em.emit(
                    "downloaded",
                    {
                        "file": os.path.abspath(output_path),
                        "sha256": hashlib.sha256(r.content).hexdigest(),
                        "bytes": len(r.content),
                        "remote_filename": filename,
                    },
                    fsync=True,
                )
                return output_path
            last_detail = f"status={r.status_code}, size={len(r.content)}"
            print(f"  Download attempt {attempt + 1}: {last_detail}")
        except Exception as e:
            last_detail = f"{type(e).__name__}: {e}"
            print(f"  Download attempt {attempt + 1}: {e}")
        time.sleep(3)
    print(f"FAILED to download {filename} after 3 attempts")
    # 下载失败为网络类：batch 侧保持 remote_completed 只重下（plan 0A.4/0A.5 l）
    em.emit_failed("network", "download", last_detail or "download failed")
    return None


def ensure_auto_stop(idle_minutes=10):
    """Deploy auto-stop script to pod. Stops pod after idle_minutes of no ComfyUI activity."""
    base = get_base_url()
    try:
        # Check if auto_stop is already running by looking at queue
        r = requests.get(f"{base}/queue", timeout=5)
        # Deploy via ComfyUI prompt API - use a simple Python script via Jupyter
        jupyter_base = base.replace("-8188.", "-8888.")
        s = requests.Session()
        resp = s.get(jupyter_base, timeout=5)
        if resp.status_code != 200:
            return
        xsrf = s.cookies.get("_xsrf", "")
        headers = {"X-XSRFToken": xsrf}

        # Create auto-stop script on pod
        idle_seconds = idle_minutes * 60
        script = f'''import time, requests, os, subprocess
IDLE_LIMIT = {idle_seconds}
last_active = time.time()
while True:
    time.sleep(30)
    try:
        r = requests.get("http://localhost:8188/queue", timeout=5)
        q = r.json()
        if q.get("queue_running") or q.get("queue_pending"):
            last_active = time.time()
        elif time.time() - last_active > IDLE_LIMIT:
            print(f"Idle for {{IDLE_LIMIT}}s, stopping pod...")
            pod_id = os.environ.get("RUNPOD_POD_ID", "")
            api_key = "{RUNPOD_API_KEY}"
            requests.post("https://api.runpod.io/graphql",
                headers={{"Authorization": f"Bearer {{api_key}}"}},
                json={{"query": f'mutation {{ podStop(input: {{ podId: "{{pod_id}}" }}) {{ id }} }}'}})
            break
    except:
        pass
'''
        # Create kernel and run
        r = s.post(
            f"{jupyter_base}/api/kernels",
            json={"name": "python3"},
            headers=headers,
            timeout=10,
        )
        if r.status_code not in [200, 201]:
            return
        kernel_id = r.json()["id"]

        import websocket as ws_lib

        ws_url = jupyter_base.replace("https://", "wss://")
        cookies = "; ".join([f"{k}={v}" for k, v in s.cookies.items()])
        ws = ws_lib.create_connection(
            f"{ws_url}/api/kernels/{kernel_id}/channels", cookie=cookies, timeout=10
        )
        ws.send(
            json.dumps(
                {
                    "header": {
                        "msg_id": "auto_stop",
                        "msg_type": "execute_request",
                        "username": "",
                        "session": "as",
                    },
                    "parent_header": {},
                    "metadata": {},
                    "content": {"code": script, "silent": False},
                }
            )
        )
        ws.close()
        print(f"  Auto-stop deployed ({idle_minutes}min idle timeout)")
    except Exception as e:
        print(f"  Auto-stop deploy skipped: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Wan 2.2 I2V Generator via RunPod")
    parser.add_argument("--prompt", "-p", help="Positive prompt")
    parser.add_argument("--image", "-i", help="Reference image path (start frame)")
    parser.add_argument(
        "--end-image",
        "-e",
        default=None,
        help="Optional end frame for FLF2V (first-last-frame). Anchors the morph to end on this image.",
    )
    parser.add_argument(
        "--mode", "-m", default="test", choices=["test", "mid", "hd", "prod", "fast81"]
    )
    parser.add_argument("--seed", "-s", type=int, default=None)
    parser.add_argument("--lora-strength", "-l", type=float, default=0.8)
    parser.add_argument(
        "--morph-lora",
        default="slop_bounce",
        choices=list(MORPH_LORAS.keys()),
        help="morph 段 LoRA（默认 slop_bounce=产线现行为；M1 测 disintegration）",
    )
    parser.add_argument(
        "--negative",
        "-n",
        default="blurry, low quality, watermark, text, distorted, ugly, deformed",
    )
    parser.add_argument("--output-dir", "-o", default=".")
    parser.add_argument(
        "--single-model",
        action="store_true",
        help="Use only high_noise model for all steps (avoids deadlock on <32GB VRAM)",
    )
    parser.add_argument("--start-pod", action="store_true")
    parser.add_argument("--stop-pod", action="store_true")
    parser.add_argument(
        "--terminate-pod",
        action="store_true",
        help="⚠️ DELETE pod entirely (irreversible). Frees container disk "
        "$0.20/GB/mo billing. Refuses if pod is RUNNING unless --force. "
        "Use at end of daily batch; models must live on Network Volume to survive.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass safety guard on --terminate-pod (allows killing RUNNING pod).",
    )
    parser.add_argument("--status", action="store_true")
    parser.add_argument(
        "--keep-pod",
        action="store_true",
        help="Do NOT stop pod after generation (default: stop pod to prevent runaway cost)",
    )
    parser.add_argument(
        "--skip-skin-gate",
        action="store_true",
        help="Bypass the FLF2V skin-tone gate (for non-skin paired refs: organ "
        "interiors, deliberate luminance changes). Skin variants must not skip.",
    )
    parser.add_argument(
        "--num-frames",
        type=int,
        default=None,
        help="Override preset frame count (e.g. 24/48) to right-size a segment and cut "
        "render time; defaults to the mode preset (prod=81). Keeps prod quality.",
    )
    # --- 编排接口（plan Phase 0A.1-0A.3）；默认不带这些 flag 时行为不变 ---------
    parser.add_argument(
        "--orchestrated",
        action="store_true",
        help="由 batch 编排器托管 pod 生命周期：跳过 runner 自身的 ensure_auto_stop "
        "与 finally-stop（pod 的启停/terminate 全部交 wan22_batch.py 决策表）",
    )
    parser.add_argument(
        "--result-json",
        default=None,
        help="NDJSON 事件流输出路径（每 job 一个独立文件）。启用后 exit 0 当且仅当 "
        "产物下载并校验通过",
    )
    parser.add_argument(
        "--job-id",
        default=os.environ.get("RUNPOD_JOB_ID", ""),
        help="批次内 job 标识（默认取环境变量 RUNPOD_JOB_ID），写入每条 NDJSON 事件",
    )
    parser.add_argument(
        "--filename-prefix",
        default=None,
        help="覆盖 SaveAnimatedWEBP 的 filename_prefix（并发产物隔离，如 "
        "wan22_<jobid>）；默认保持 wan22_<mode>",
    )
    args = parser.parse_args()

    if args.status:
        print(f"Pod status: {pod_status()}")
        sys.exit(0)

    if args.start_pod:
        start_pod()
        sys.exit(0)

    if args.stop_pod:
        stop_pod()
        sys.exit(0)

    if args.terminate_pod:
        ok = terminate_pod(force=args.force)
        sys.exit(0 if ok else 1)

    if not args.prompt or not args.image:
        parser.print_help()
        sys.exit(1)

    # Skin-tone numeric gate for FLF2V paired refs (来源：painpoint-factory SSOT §4b).
    # Paired before/after with mismatched skin luminance (Δ>10) morphs into a
    # full-clip skin drift that post-hoc color transfer cannot fix — reject
    # before spending GPU money. Non-skin FLF2V (organ interiors, faces with
    # deliberate luminance change) must pass --skip-skin-gate explicitly.
    if args.end_image and not args.skip_skin_gate:
        _gate = os.path.expanduser(
            "~/claude-dev/projects/health-supplements/projects/"
            "ai-generation-workspace/workspace/scripts/skin_gate.py"
        )
        if os.path.exists(_gate):
            import subprocess

            _rc = subprocess.call([sys.executable, _gate, args.image, args.end_image])
            if _rc != 0:
                print(
                    "[skin-gate] FAIL: before/after 皮肤亮度差超闸。配对 refs 须成对"
                    "重生（SSOT §4b）；非皮肤类 FLF2V 加 --skip-skin-gate 显式豁免。"
                )
                sys.exit(1)
        else:
            print(f"[skin-gate] 闸门脚本不存在（{_gate}），跳过检查")

    # Ensure pod is running AND ComfyUI is actually serving.
    # A freshly-deployed pod (grab_pod.sh / REST create) reports RUNNING
    # immediately but the base pytorch image does NOT auto-launch ComfyUI
    # (it lives on the NV at /workspace/ComfyUI). Without this, generate()
    # hits upload_image against a dead :8188 → empty body → JSONDecodeError.
    # ensure_comfyui_running() is idempotent (no-op if already serving).
    if pod_status() != "RUNNING":
        if not start_pod():
            sys.exit(1)
    else:
        ensure_comfyui_running()
        wait_for_comfyui_ready(timeout=180)

    # Deploy auto-stop as backup safety net (won't replace post-gen stop)
    # --orchestrated：pod 生命周期交 batch，runner 不装 auto-stop（plan 0A.1）
    if not args.orchestrated:
        ensure_auto_stop(idle_minutes=10)

    _emitter = ResultEmitter(args.result_json, args.job_id or None, POD_ID or None)
    _exit_code = 0
    downloaded = None
    try:
        result = generate(
            args.prompt,
            args.image,
            args.mode,
            args.seed,
            args.lora_strength,
            args.negative,
            args.single_model,
            args.end_image,
            num_frames=args.num_frames,
            morph_lora=args.morph_lora,
            filename_prefix=args.filename_prefix,
            emitter=_emitter,
        )
        if result:
            downloaded = download_output(
                result["filename"], args.output_dir, emitter=_emitter
            )
            # Log to unified AI-gen registry (sidecar JSON + index)
            if _log_generation is not None and downloaded:
                p = PRESETS[args.mode]
                try:
                    _log_generation(
                        tool="wan22",
                        model="wan2.2-i2v-14b-fp8",
                        mode=args.mode,
                        prompt=args.prompt,
                        negative_prompt=args.negative,
                        output_path=os.path.abspath(downloaded),
                        ref_images=[os.path.abspath(args.image)] if args.image else [],
                        params={
                            "seed": result["seed"],
                            "lora_strength": args.lora_strength,
                            "lora_name": args.morph_lora,
                            "width": p["width"],
                            "height": p["height"],
                            "num_frames": p["num_frames"],
                            "steps": p["steps"],
                            "use_lightx2v": p["use_lightx2v"],
                            "single_model": args.single_model,
                        },
                        cost_source=f"runpod_l40_{p['steps']}steps",
                        extra={"elapsed_sec": round(result["elapsed"], 1)},
                    )
                except Exception as e:
                    print(f"[logger] log_generation failed (non-fatal): {e}")
    except Exception as e:
        # 编排模式：异常已由 generate/download_output 分类落盘为 failed 事件，
        # 这里只收敛为非零退出码。默认路径（未启用 --result-json）原样上抛。
        if not _emitter.enabled:
            raise
        print(f"[orchestrated] aborted: {type(e).__name__}: {e}")
        _exit_code = 1
    finally:
        # GUARANTEED pod stop after every generation (pass or fail, or even Ctrl+C)
        # unless user explicitly requests --keep-pod for back-to-back batches
        # --orchestrated 时同样跳过：pod 生命周期由 batch 决策表接管（plan 0A.1）
        if not args.keep_pod and not args.orchestrated:
            print("\n[SAFETY] Stopping pod to prevent runaway cost...")
            try:
                stop_pod()
                # Verify it actually stopped — poll for up to 30s since GraphQL
                # status lags the actual stop command by several seconds
                import time as _t

                final = "RUNNING"
                for _ in range(6):
                    _t.sleep(5)
                    final = pod_status()
                    if final == "EXITED":
                        break
                print(f"[SAFETY] Pod status after stop: {final}")
                if final == "RUNNING":
                    print(
                        "[WARNING] Pod still RUNNING after 30s! Manual stop required."
                    )
            except Exception as e:
                print(f"[ERROR] Failed to stop pod: {e}")
                print("[ACTION REQUIRED] Run: python3 wan22_generate.py --stop-pod")

    # 机器可读退出码（plan 0A.2）：0 当且仅当产物下载并校验通过。仅在启用
    # --result-json 时生效，默认路径仍然自然结束（退出码不变）。
    if _emitter.enabled:
        if downloaded is None:
            _exit_code = 1
        sys.exit(_exit_code)

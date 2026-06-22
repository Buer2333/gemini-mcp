#!/bin/bash
# grab_pod.sh — bounded deploy-retry loop to beat the US-KS-2 48GB SECURE stock wall.
#
# WHY: RunPod's REST `gpuAvailability.available=True` is optimistic — the SECURE pool
# for 48GB cards (RTX 6000 Ada / L40 / A6000) at storage-DCs is frequently dry, and a
# single `POST /v1/pods` returns "no instances currently available". Stock returns at
# second-to-minute granularity, so a bounded retry loop reliably grabs one within a few
# rounds (2026-05-26: 6000 Ada grabbed on attempt 2 ~90s; A6000 on attempt 1).
#
# Failed deploy attempts create NO pod and cost nothing. First success exits 0 and prints:
#   GRABBED|POD_ID=<id>|GPU=<name>|attempt=<n>
#
# Env (all optional, defaults target the wan22-i2v project):
#   RUNPOD_API_KEY  (required)
#   NV_ID           network volume id to mount        (default: worirnghod)
#   POD_NAME        pod name                           (default: wan22-i2v)
#   GPU_LIST        newline-sep GPU type ids, priority (default: 6000 Ada -> L40 -> A6000)
#   PUBKEY_FILE     ssh pubkey to inject               (default: ~/.ssh/id_ed25519.pub)
#   MAX_ROUNDS      retry rounds before giving up      (default: 20, ~90s each)
#   IMAGE_NAME      container image                    (default: runpod pytorch 2.4.0)
set -euo pipefail
: "${RUNPOD_API_KEY:?set RUNPOD_API_KEY}"
NV_ID="${NV_ID:-worirnghod}"
POD_NAME="${POD_NAME:-wan22-i2v}"
PUBKEY_FILE="${PUBKEY_FILE:-$HOME/.ssh/id_ed25519.pub}"
MAX_ROUNDS="${MAX_ROUNDS:-20}"
IMAGE_NAME="${IMAGE_NAME:-runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04}"
GPU_LIST="${GPU_LIST:-NVIDIA RTX 6000 Ada Generation
NVIDIA L40
NVIDIA RTX A6000}"
PUBKEY="$(cat "$PUBKEY_FILE")"

deploy() {
  curl -s -X POST https://rest.runpod.io/v1/pods \
    -H "Authorization: Bearer $RUNPOD_API_KEY" -H "Content-Type: application/json" \
    -d "{\"name\":\"$POD_NAME\",\"imageName\":\"$IMAGE_NAME\",\"gpuTypeIds\":[\"$1\"],\"gpuCount\":1,\"containerDiskInGb\":20,\"volumeInGb\":0,\"volumeMountPath\":\"/workspace\",\"networkVolumeId\":\"$NV_ID\",\"ports\":[\"22/tcp\",\"8188/http\",\"8888/http\"],\"cloudType\":\"SECURE\",\"env\":{\"PUBLIC_KEY\":\"$PUBKEY\"}}"
}

for i in $(seq 1 "$MAX_ROUNDS"); do
  while IFS= read -r gpu; do
    [ -z "$gpu" ] && continue
    resp="$(deploy "$gpu")"
    pid="$(printf '%s' "$resp" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))' 2>/dev/null || true)"
    if [ -n "$pid" ]; then
      echo "GRABBED|POD_ID=$pid|GPU=$gpu|attempt=$i"
      exit 0
    fi
  done <<< "$GPU_LIST"
  echo "round $i/$MAX_ROUNDS: all dry, sleep 90s ($(date '+%H:%M:%S'))"
  # stall 主动上报（2026-06-12 固化，dip_demo pilot acceptance 工程项）：每 10 轮干涸推一次飞书，
  # 防"机器自治退化成机器沉默"（2026-06-11 US-KS-2 库存墙用户被迫两次来问的教训）。
  # FEISHU_WEBHOOK_URL 未设则静默跳过，不影响原行为。
  if [ $((i % 10)) -eq 0 ] && [ -n "${FEISHU_WEBHOOK_URL:-}" ]; then
    curl -s -m 10 -X POST "$FEISHU_WEBHOOK_URL" -H "Content-Type: application/json" \
      -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"⏳ grab_pod stall: ${i}/${MAX_ROUNDS} 轮全 GPU 干涸 (~$((i*90/60))min)。剩余 $((MAX_ROUNDS-i)) 轮 (~$(( (MAX_ROUNDS-i)*90/60 ))min)。备选: 等美国凌晨窗口 / 换 DC / MAX_ROUNDS 调大。pod 未建零成本。\"}}" >/dev/null || true
  fi
  sleep 90
done
echo "EXHAUSTED: no stock after $MAX_ROUNDS rounds"
if [ -n "${FEISHU_WEBHOOK_URL:-}" ]; then
  curl -s -m 10 -X POST "$FEISHU_WEBHOOK_URL" -H "Content-Type: application/json" \
    -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"🔴 grab_pod EXHAUSTED: ${MAX_ROUNDS} 轮无库存，已停止（零成本）。需人工决策: 换时段 / 换 DC / 第二 NV。\"}}" >/dev/null || true
fi
exit 1

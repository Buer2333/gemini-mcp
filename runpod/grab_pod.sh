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

# classify a deploy response into a code so we never mask non-stock failures.
# Returns one of: OK|<pod_id> / STOCK|<msg> / BALANCE|<msg> / AUTH|<msg> / ERR|<msg>
# WHY (2026-06-22 incident): the old loop treated ANY non-id response as "dry" and
# kept polling — masking a "balance too low" rejection for ~70min while a mis-captured
# orphan pod drained ~$9. Stock-dry retries; everything else aborts immediately.
classify() {
  printf '%s' "$1" | python3 -c '
import sys, json
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception:
    print("ERR|unparseable response: " + raw[:200]); sys.exit()
if isinstance(d, dict) and d.get("id"):
    print("OK|" + d["id"]); sys.exit()
msg = ""
if isinstance(d, dict):
    msg = d.get("error") or d.get("errors") or json.dumps(d)
elif isinstance(d, list) and d:
    msg = json.dumps(d[0])
msg = str(msg)
low = msg.lower()
if any(s in low for s in ["no instances", "no longer any instances", "not available", "currently available", "no gpu", "out of stock"]):
    print("STOCK|" + msg)
elif any(s in low for s in ["balance", "too low", "add funds", "insufficient"]):
    print("BALANCE|" + msg)
elif any(s in low for s in ["unauthor", "authentic", "invalid api", "forbidden", "401", "api key"]):
    print("AUTH|" + msg)
else:
    print("ERR|" + msg)
'
}

# fire-once feishu alert helper (silent if webhook unset)
alert() { [ -n "${FEISHU_WEBHOOK_URL:-}" ] && curl -s -m 10 -X POST "$FEISHU_WEBHOOK_URL" \
  -H "Content-Type: application/json" -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"$1\"}}" >/dev/null 2>&1 || true; }

# pre-flight: a negative/near-zero balance means EVERY deploy will be rejected.
# Abort up front with a clear message instead of looping (the 2026-06-22 lesson).
BAL="$(curl -s -X POST https://api.runpod.io/graphql \
  -H "Authorization: Bearer $RUNPOD_API_KEY" -H "Content-Type: application/json" \
  -d '{"query":"query { myself { clientBalance } }"}' 2>/dev/null \
  | python3 -c 'import sys,json
try: print(json.load(sys.stdin)["data"]["myself"]["clientBalance"])
except Exception: print("")' 2>/dev/null || true)"
if [ -n "$BAL" ] && python3 -c "import sys; sys.exit(0 if float('$BAL')<=0 else 1)" 2>/dev/null; then
  echo "ABORT|BALANCE: clientBalance=\$$BAL (<=0). Add funds at runpod.io before deploying. No attempts made (zero cost)."
  alert "🔴 grab_pod ABORT: RunPod 余额 \$$BAL (<=0)，无法租 pod。请充值。已零成本中止。"
  exit 2
fi
[ -n "$BAL" ] && echo "preflight: clientBalance=\$$BAL"

for i in $(seq 1 "$MAX_ROUNDS"); do
  while IFS= read -r gpu; do
    [ -z "$gpu" ] && continue
    res="$(classify "$(deploy "$gpu")")"
    code="${res%%|*}"; detail="${res#*|}"
    case "$code" in
      OK)
        echo "GRABBED|POD_ID=$detail|GPU=$gpu|attempt=$i"
        exit 0 ;;
      STOCK)
        : ;;  # genuine no-stock — try next GPU / next round
      BALANCE)
        echo "ABORT|BALANCE|$detail"
        alert "🔴 grab_pod ABORT: 余额不足，无法租 pod。请充值。detail: $detail"
        exit 2 ;;
      AUTH)
        echo "ABORT|AUTH|$detail"
        alert "🔴 grab_pod ABORT: API key 鉴权失败。detail: $detail"
        exit 3 ;;
      *)
        echo "ABORT|ERR|$detail"
        alert "🔴 grab_pod ABORT: 未知部署错误（非库存），已停止避免空轮询掩盖问题。detail: $detail"
        exit 4 ;;
    esac
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

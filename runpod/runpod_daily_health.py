#!/usr/bin/env python3
"""RunPod daily health check.

Queries account state via RunPod GraphQL and prints a structured report.
With --push, sends a feishu alert to the personal inbox group when any
threshold trips.

Designed for daily cron. Exit codes:
  0 — healthy (no alert lines)
  2 — alert (one or more thresholds tripped)
  3 — RunPod API unreachable / auth failed

Triggers added after 2026-05-20 incident where a stopped pod silently
drained $8 storage credit over 42 days until balance hit zero, RunPod
auto-deleted the pod, and all model caches were lost.

Thresholds (override via env):
  HEALTH_BALANCE_WARN   — warn when clientBalance < this many USD (default 5)
  HEALTH_BALANCE_CRIT   — critical when clientBalance < this (default 1)
  HEALTH_ORPHAN_HOURS   — alert if EXITED pod older than this (default 4)

Feishu push (only when --push):
  FEISHU_WEBHOOK_URL    — Custom Bot incoming webhook URL (required for push).
                          Create one in any feishu group: Settings → Bots →
                          Add Custom Bot, copy the URL, set as env. No auth
                          needed beyond URL secrecy.

Run with --dry-run to preview the payload that would be POSTed instead of
sending. Always use this first on a new HEALTH config.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests

RUNPOD_API_KEY = os.environ.get("RUNPOD_API_KEY", "")
RUNPOD_GRAPHQL = "https://api.runpod.io/graphql"

WARN_BALANCE = float(os.environ.get("HEALTH_BALANCE_WARN", "5"))
CRIT_BALANCE = float(os.environ.get("HEALTH_BALANCE_CRIT", "1"))
ORPHAN_HOURS = float(os.environ.get("HEALTH_ORPHAN_HOURS", "4"))

FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK_URL", "")


def gql(query: str) -> dict:
    """POST to RunPod GraphQL. Raises on transport failure; caller checks errors."""
    r = requests.post(
        RUNPOD_GRAPHQL,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {RUNPOD_API_KEY}",
        },
        json={"query": query},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def fetch_state() -> dict:
    """Return {balance, pods[], volumes[]} or raise."""
    q = """query {
      myself {
        clientBalance
        currentSpendPerHr
        pods {
          id name desiredStatus
          machine { gpuDisplayName }
          runtime { uptimeInSeconds }
          containerDiskInGb volumeInGb
          lastStatusChange
        }
        networkVolumes { id name size dataCenterId }
      }
    }"""
    d = gql(q)
    if "errors" in d:
        raise RuntimeError(f"RunPod GraphQL errors: {d['errors']}")
    me = d.get("data", {}).get("myself")
    if me is None:
        raise RuntimeError(f"Unexpected response shape: {d}")
    return me


def evaluate(state: dict) -> tuple[list[str], list[str]]:
    """Return (alerts, info_lines). Alerts are flagged conditions; info is always-shown."""
    alerts: list[str] = []
    info: list[str] = []

    bal = float(state.get("clientBalance") or 0)
    spend = float(state.get("currentSpendPerHr") or 0)
    info.append(f"Balance: ${bal:.2f}   Spend rate: ${spend:.4f}/hr")

    if bal < CRIT_BALANCE:
        alerts.append(
            f"🔴 CRIT balance ${bal:.2f} < ${CRIT_BALANCE} — pod will be auto-deleted soon"
        )
    elif bal < WARN_BALANCE:
        alerts.append(f"🟡 WARN balance ${bal:.2f} < ${WARN_BALANCE} — top up soon")

    pods = state.get("pods") or []
    info.append(f"Pods: {len(pods)}")
    now = int(time.time())
    for p in pods:
        pid = p.get("id", "?")
        name = p.get("name") or "(unnamed)"
        status = p.get("desiredStatus") or "UNKNOWN"
        gpu = (p.get("machine") or {}).get("gpuDisplayName") or "?"
        cd = p.get("containerDiskInGb") or 0
        vd = p.get("volumeInGb") or 0
        info.append(f"  - {pid[:12]} [{status}] {gpu} {name} cdisk={cd}G vol={vd}G")

        last_change = p.get("lastStatusChange")
        if status == "EXITED" and last_change:
            try:
                lc_int = int(last_change)
                exited_at = lc_int // 1000 if lc_int > 1e10 else lc_int
                hours_idle = (now - exited_at) / 3600
                if hours_idle > ORPHAN_HOURS:
                    alerts.append(
                        f"🟠 Orphan EXITED pod {pid[:12]} idle {hours_idle:.1f}h "
                        f"(>{ORPHAN_HOURS}h). Storage $0.20/GB/mo continues — terminate or restart."
                    )
            except (ValueError, TypeError):
                # 2026-05-20 reviewer note: silent-skip on parse failure could mask
                # a 42-day-aged orphan if RunPod ever returns ISO-8601 instead of epoch.
                info.append(
                    f"  [warn] could not parse lastStatusChange={last_change!r} "
                    f"for pod {pid[:12]} — orphan-age check skipped"
                )

    vols = state.get("networkVolumes") or []
    info.append(f"NetworkVolumes: {len(vols)}")
    for v in vols:
        info.append(
            f"  - {v.get('id', '?')[:12]} {v.get('size')}GB @ {v.get('dataCenterId')} ({v.get('name')})"
        )

    if pods and not vols:
        alerts.append(
            "🟡 Pods exist but no Network Volume — model loss risk on pod deletion. "
            "Migrate model cache to NV to survive terminate."
        )

    # 2026-05-20 incident state detection: empty account but non-zero balance.
    # This is exactly what 5-20 looked like AFTER RunPod auto-deleted everything,
    # except we noticed it because balance was negative. If user re-funds before
    # rebuilding pod+NV, an account-empty alert is the canonical signal.
    if not pods and not vols and bal > 0.05:
        alerts.append(
            f"🟡 Account has ${bal:.2f} but 0 pods and 0 NetworkVolumes — "
            "is this intentional? (post-incident rebuild not yet done, or "
            "infra silently deleted)"
        )

    return alerts, info


FEISHU_TEXT_MAX = (
    25000  # feishu text webhook truncates around 30KB; keep slack for headers
)


def build_payload(alerts: list[str], info: list[str]) -> dict:
    """Construct the feishu webhook payload. Pure function — testable, dry-runnable.

    Defensive truncation at FEISHU_TEXT_MAX (~25KB) keeps the POST under feishu's
    text-message limit (~30KB) even when a misconfigured account returns 20+ pods.
    """
    body_lines = ["🚨 RunPod Health Alert", ""] + alerts + ["", "状态："] + info
    text = "\n".join(body_lines)
    if len(text) > FEISHU_TEXT_MAX:
        text = text[:FEISHU_TEXT_MAX] + "\n…[truncated]"
    return {"msg_type": "text", "content": {"text": text}}


def push_feishu(payload: dict) -> bool:
    """POST payload to FEISHU_WEBHOOK_URL. Returns True on confirmed delivery
    (feishu API code==0), False on any failure mode including missing config.

    feishu Custom Bot webhook is auth-by-URL — set FEISHU_WEBHOOK_URL to the
    full hook URL (https://open.feishu.cn/open-apis/bot/v2/hook/<uuid>).
    """
    if not FEISHU_WEBHOOK:
        print(
            "[push] FEISHU_WEBHOOK_URL not set — alert printed but not sent",
            file=sys.stderr,
        )
        return False
    try:
        r = requests.post(FEISHU_WEBHOOK, json=payload, timeout=15)
        r.raise_for_status()
        result = r.json()
    except requests.RequestException as e:
        print(f"[push] webhook transport error: {e}", file=sys.stderr)
        return False
    except ValueError:
        print(f"[push] webhook returned non-JSON: {r.text[:200]!r}", file=sys.stderr)
        return False
    if result.get("code") == 0:
        print("[push] alert delivered", file=sys.stderr)
        return True
    print(
        f"[push] feishu rejected: code={result.get('code')!r} msg={result.get('msg')!r}",
        file=sys.stderr,
    )
    return False


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--push",
        action="store_true",
        help="Send feishu alert on threshold trip (requires FEISHU_WEBHOOK_URL)",
    )
    p.add_argument(
        "--quiet", action="store_true", help="Suppress info lines (alert-only output)"
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and print the feishu payload but do NOT POST. Use to verify "
        "the webhook target and formatting before wiring to cron.",
    )
    args = p.parse_args()

    if not RUNPOD_API_KEY:
        print("ERROR: RUNPOD_API_KEY not set", file=sys.stderr)
        return 3

    try:
        state = fetch_state()
    except (requests.RequestException, RuntimeError) as e:
        print(f"ERROR: RunPod API unreachable: {e}", file=sys.stderr)
        return 3

    alerts, info = evaluate(state)

    if not args.quiet:
        for line in info:
            print(line)
        if alerts:
            print()
    for a in alerts:
        print(a)

    if alerts:
        payload = build_payload(alerts, info)
        if args.dry_run:
            print("\n[dry-run] would POST:", file=sys.stderr)
            print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        elif args.push:
            push_feishu(payload)

    return 2 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())

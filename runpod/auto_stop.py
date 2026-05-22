#!/usr/bin/env python3
"""Auto-stop RunPod pod after N minutes of idle ComfyUI queue."""

import requests
import time
import os

IDLE_TIMEOUT = 20 * 60  # 20 minutes
CHECK_INTERVAL = 60  # check every 60s
RUNPOD_API_KEY = os.environ.get("RUNPOD_API_KEY", "")
POD_ID = os.environ.get("RUNPOD_POD_ID", "")

last_active = time.time()

while True:
    try:
        q = requests.get("http://localhost:8188/queue", timeout=5).json()
        running = len(q.get("queue_running", []))
        pending = len(q.get("queue_pending", []))

        if running > 0 or pending > 0:
            last_active = time.time()
        else:
            idle_sec = time.time() - last_active
            if idle_sec >= IDLE_TIMEOUT:
                print(f"Stopping pod after {idle_sec / 60:.0f}min idle")
                requests.post(
                    "https://api.runpod.io/graphql",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {RUNPOD_API_KEY}",
                    },
                    json={
                        "query": f'mutation {{ podStop(input: {{ podId: "{POD_ID}" }}) {{ id }} }}'
                    },
                )
                break
    except Exception as e:
        print(f"Check error: {e}")
    time.sleep(CHECK_INTERVAL)

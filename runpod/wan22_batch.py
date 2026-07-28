#!/usr/bin/env python3
"""
wan22_batch.py — wan2.2 工厂 batch 编排内核（Phase 0A 最小版，单 pod 能力）。

真源：`~/.claude/plans/frolicking-drifting-rocket.md` Phase 0A（0A.4-0A.8）。
本文件只做编排，不做生成：每个 job 以子进程调 `wan22_generate.py`
（`--orchestrated --result-json <per-job path> --filename-prefix wan22_<jobid> --keep-pod`），
读 runner 的 NDJSON 事件流推进耐久状态机，并按决策表管 owned pod 生命周期。

核心机制（逐条对应 plan）：
  0A.4 manifest 单写者协调线程（queue.Queue + tmp/os.replace 原子写）+ 耐久状态机
       pending → submitting → submitted → remote_completed → downloaded → done
       （+ business_failed 支线；网络类错误只更 last_error_* 不降级状态）
  0A.5 resume：本地 masters+sidecar sha → pod 活着按 prompt_id 续查 /history
       → NV output_prefix 收养 → 双确认 + 有界窗口（≥60s×2）才回 pending 重提
  0A.6 owned pod 决策表 + --max-spend（默认 $3，按 owned pod uptime×费率累计）；
       borrowed pod 一律不动
  0A.7 防孤儿：batch_id 原子持久化 → POD_NAME=wan22-<batch_id>-<worker>
       → 启动/resume 名称前缀查询收养 → 创建响应不确定时先查询后创建
  0A.8 长退避 60→180→420→900s 封顶，网络类不计 business attempt

job 表 JSON（`--jobs jobs.json`）：顶层 list，或 {"jobs": [...]}。字段（除 job_id/
prompt/image 外均可选，语义与 wan22_generate.py 的同名 CLI 参数一致）：
    {"job_id": "bounce_red", "prompt": "...", "image": "refs_v2/final/after_red.png",
     "end_image": null, "mode": "prod", "seed": 88, "lora_strength": 1.0,
     "morph_lora": "slop_bounce", "num_frames": null, "negative": null,
     "single_model": false, "skip_skin_gate": false, "output_dir": "gen_v2/bounce_red"}

Phase 1.1-1.2（并行扩展）在 0A 内核上加 pod 池：`--pods p1,p2[,p3]` 每 pod 一个
worker 线程从 job 队列抢活（各自注入 RUNPOD_POD_ID），manifest/ledger 仍单写者；
决策表按 pod 独立执行——某 pod 干完自己的活立刻 terminate，不等其他 pod。

用法：
    python3 wan22_batch.py --jobs jobs.json --pods <podid> --workdir .batch/run1
    python3 wan22_batch.py --jobs jobs.json --pods p1,p2 --workdir .batch/run1
    python3 wan22_batch.py --jobs jobs.json --pods p1,p2:borrowed --dry-run
    python3 wan22_batch.py --jobs jobs.json --pods <podid> --workdir .batch/run1 --resume
"""

import argparse
import hashlib
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

import requests

# --- Proxy bypass ----------------------------------------------------------
# 与 wan22_generate.py 顶部同因（macOS 系统代理会中途掐断 RunPod 长连接）：
# 见 wan22_generate.py 的 "Proxy bypass (2026-05-26)" 注释块（真源）。
for _pv in (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
):
    os.environ.pop(_pv, None)
_NO_PROXY = "runpod.net,runpod.io,127.0.0.1,localhost"
os.environ["NO_PROXY"] = os.environ["no_proxy"] = _NO_PROXY
# ---------------------------------------------------------------------------

RUNNER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wan22_generate.py")
COMFYUI_PORT = 8188

# --- plan 0A.4 状态机 -------------------------------------------------------
JOB_STATES = (
    "pending",
    "submitting",
    "submitted",
    "remote_completed",
    "downloaded",
    "done",
    "business_failed",
)

# 允许的状态转移（含自环=幂等更新）。任何未列出的转移即实现 bug，直接抛错。
ALLOWED_TRANSITIONS = {
    "pending": {"pending", "submitting", "business_failed"},
    # submitting → pending 仅两条合法途径：①确定未受理（node_errors 无 prompt_id）
    # ②有界双确认窗口内远端 queue/history 与 NV 均为空
    "submitting": {
        "submitting",
        "submitted",
        "remote_completed",
        "pending",
        "business_failed",
    },
    "submitted": {"submitted", "remote_completed", "pending", "business_failed"},
    "remote_completed": {"remote_completed", "downloaded", "pending"},
    "downloaded": {"downloaded", "done"},
    "done": {"done"},
    "business_failed": {"business_failed", "pending"},
}

MAX_BUSINESS_ATTEMPTS = 4
# plan 0A.8：网络类退避序列（秒），末项封顶
BACKOFF_SEQUENCE = (60, 180, 420, 900)
# plan 0A.4：有界确认窗口 —— 间隔 ≥60s 连续 2 次远端+NV 均空才算确认不存在
CONFIRM_INTERVAL_SEC = 60
CONFIRM_ROUNDS = 2
# plan 0A.6：网络中断且 pod 上有已确认运行中任务时的最长等待窗口（单 job 上限 25min + 余量）
NETWORK_WAIT_WINDOW_SEC = 45 * 60
DEFAULT_MAX_SPEND = 3.0
# Phase 1.2 dry-run wall 粗估（分钟）：pilot v2 实测 morph 17-21 / bounce 22-24，
# 取上界；spinup = grab_pod + ComfyUI 就绪
JOB_WALL_MIN = {"morph": 21.0, "bounce": 24.0, "test": 7.0}
POD_SPINUP_MIN = 8.0
# RTX 6000 Ada $0.77/h（plan 预算表）；API 返回 costPerHr 时以 API 为准
DEFAULT_POD_RATE = 0.77
SIGNAL_DRAIN_SEC = 30


class IllegalTransition(RuntimeError):
    pass


class SpendLimitExceeded(RuntimeError):
    pass


class CreateUncertain(RuntimeError):
    """pod 创建响应不确定（超时/空/不可解析）——禁止盲目重建（plan 0A.7 ④）。"""


def backoff_delay(attempt_index):
    """plan 0A.8：60→180→420→900s 封顶。attempt_index 从 0 起。"""
    if attempt_index < 0:
        attempt_index = 0
    return BACKOFF_SEQUENCE[min(attempt_index, len(BACKOFF_SEQUENCE) - 1)]


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# --- plan 0A.6 owned pod 决策表 --------------------------------------------
def pod_action(
    ownership,
    situation,
    remaining_jobs=0,
    has_running_task=False,
    wait_elapsed_sec=0,
):
    """返回 'terminate' | 'wait' | 'keep' | 'none'（none = 不碰）。

    borrowed pod 在四种情形下一律不动（不装 auto-stop、不 stop、不 terminate）。
    「主动停机」一律实现为 terminate —— stop 会触发 EXITED 2x penalty（runpod-ops
    cost-mechanics 铁律）。
    """
    if ownership != "owned":
        return "none"
    if situation == "job_success":
        # 下载+校验过且该 pod 无剩余 job → 立即 terminate（不等 compose/GATE）
        return "terminate" if remaining_jobs <= 0 else "keep"
    if situation == "business_failed":
        # 抓 ComfyUI 日志尾落盘由调用方先做，再 terminate
        return "terminate"
    if situation == "network_backoff":
        if not has_running_task:
            return "terminate"
        return "terminate" if wait_elapsed_sec >= NETWORK_WAIT_WINDOW_SEC else "wait"
    if situation == "signal":
        return "terminate"
    return "keep"


# --- plan 0A.4 单写者协调线程 ----------------------------------------------
class SingleWriterStore:
    """所有状态更新经 queue.Queue 发唯一协调线程串行写盘（tmp + os.replace）。

    apply(mutator) 默认阻塞直到该 mutator 在写线程执行完并落盘 —— 并发调用者不会
    丢转移（每个 mutator 读到的是上一个 mutator 的结果）。
    """

    def __init__(self, path, initial=None):
        self.path = path
        self._doc = json.loads(json.dumps(initial)) if initial else {}
        self._q = queue.Queue()
        self._thread = None
        self._stop = object()
        self._lock = threading.Lock()
        self.writes = 0

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._loop, name="store-writer", daemon=True
        )
        self._thread.start()
        return self

    def stop(self, timeout=10):
        if self._thread is None:
            return
        self._q.put(self._stop)
        self._thread.join(timeout=timeout)
        self._thread = None

    def _loop(self):
        while True:
            item = self._q.get()
            if item is self._stop:
                return
            mutator, result_box, done = item
            try:
                with self._lock:
                    ret = mutator(self._doc)
                    self._flush_locked()
                result_box.append(("ok", ret))
            except Exception as e:  # noqa: BLE001 - 传回调用线程
                result_box.append(("err", e))
            finally:
                if done is not None:
                    done.set()

    # -- io -----------------------------------------------------------------
    def _flush_locked(self):
        self._doc["updated_at"] = time.time()
        tmp = "%s.tmp.%d" % (self.path, os.getpid())
        d = os.path.dirname(os.path.abspath(self.path))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._doc, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        self.writes += 1

    # -- api ----------------------------------------------------------------
    def apply(self, mutator, wait=True):
        if self._thread is None:
            # 未启动写线程时同步执行（dry-run / 单元测试便利路径）
            with self._lock:
                ret = mutator(self._doc)
                self._flush_locked()
            return ret
        box = []
        done = threading.Event() if wait else None
        self._q.put((mutator, box, done))
        if not wait:
            return None
        done.wait()
        kind, payload = box[0]
        if kind == "err":
            raise payload
        return payload

    def snapshot(self):
        with self._lock:
            return json.loads(json.dumps(self._doc))

    @classmethod
    def load(cls, path, initial=None):
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return cls(path, json.load(f))
        return cls(path, initial)


class ManifestStore(SingleWriterStore):
    """job 状态真源。doc = {batch_id, created_at, jobs: {job_id: {...}}}。"""

    def job(self, job_id):
        with self._lock:
            return json.loads(json.dumps(self._doc.get("jobs", {}).get(job_id, {})))

    def state(self, job_id):
        return self.job(job_id).get("state")

    def ensure_job(self, job_id, spec):
        def m(doc):
            jobs = doc.setdefault("jobs", {})
            if job_id not in jobs:
                jobs[job_id] = {
                    "job_id": job_id,
                    "state": "pending",
                    "attempts": 0,
                    "net_retries": 0,
                    "spec": spec,
                    "output_prefix": "wan22_%s" % job_id,
                    "created_at": time.time(),
                }
            else:
                jobs[job_id]["spec"] = spec
            return jobs[job_id]

        return self.apply(m)

    def update_job(self, job_id, **fields):
        def m(doc):
            j = doc.setdefault("jobs", {}).setdefault("%s" % job_id, {})
            j.update(fields)
            return j

        return self.apply(m)

    def transition(self, job_id, new_state, reason=None, **fields):
        """校验后的状态转移（非法转移直接抛，暴露实现 bug 而非静默）。"""

        def m(doc):
            j = doc.setdefault("jobs", {}).setdefault("%s" % job_id, {})
            cur = j.get("state", "pending")
            allowed = ALLOWED_TRANSITIONS.get(cur, set())
            if new_state not in allowed:
                raise IllegalTransition(
                    "%s: %s -> %s not allowed" % (job_id, cur, new_state)
                )
            j["state"] = new_state
            j["state_at"] = time.time()
            if reason:
                j["state_reason"] = reason
            hist = j.setdefault("history", [])
            hist.append({"from": cur, "to": new_state, "reason": reason, "ts": time.time()})
            j.update(fields)
            return j

        return self.apply(m)

    def record_network_error(self, job_id, stage, detail):
        """plan 0A.8：网络类错误保持所在阶段状态，只更新 last_error_*，不计 attempt。"""

        def m(doc):
            j = doc.setdefault("jobs", {}).setdefault("%s" % job_id, {})
            j["last_error_class"] = "network"
            j["last_error_stage"] = stage
            j["last_error_detail"] = str(detail)[:500]
            j["last_error_at"] = time.time()
            j["net_retries"] = int(j.get("net_retries", 0)) + 1
            return j

        return self.apply(m)

    def record_business_error(self, job_id, stage, detail):
        """业务类：attempt++ 并转 business_failed（max 4 后终止该 job，不阻塞其余）。"""

        def m(doc):
            j = doc.setdefault("jobs", {}).setdefault("%s" % job_id, {})
            cur = j.get("state", "pending")
            if "business_failed" not in ALLOWED_TRANSITIONS.get(cur, set()):
                raise IllegalTransition("%s: %s -> business_failed" % (job_id, cur))
            j["attempts"] = int(j.get("attempts", 0)) + 1
            j["last_error_class"] = "business"
            j["last_error_stage"] = stage
            j["last_error_detail"] = str(detail)[:500]
            j["last_error_at"] = time.time()
            j["state"] = "business_failed"
            j["state_at"] = time.time()
            j.setdefault("history", []).append(
                {"from": cur, "to": "business_failed", "reason": stage, "ts": time.time()}
            )
            return j

        return self.apply(m)


# --- ComfyUI HTTP 客户端（远端查询：/queue /history /view）------------------
class ComfyClient:
    def __init__(self, pod_id, port=COMFYUI_PORT, session=None, timeout=15):
        self.pod_id = pod_id
        self.base = "https://%s-%d.proxy.runpod.net" % (pod_id, port)
        self.http = session or requests
        self.timeout = timeout

    # -- low level ----------------------------------------------------------
    def _get_json(self, path):
        r = self.http.get(self.base + path, timeout=self.timeout)
        if r.status_code != 200:
            raise requests.exceptions.HTTPError("HTTP %s on %s" % (r.status_code, path))
        return r.json()

    def alive(self):
        try:
            self._get_json("/system_stats")
            return True
        except Exception:
            return False

    def queue(self):
        return self._get_json("/queue")

    def history(self, prompt_id=None):
        return self._get_json("/history/%s" % prompt_id if prompt_id else "/history")

    # -- prefix 匹配 ---------------------------------------------------------
    @staticmethod
    def _iter_prefixes(obj):
        """递归取出 workflow 里所有 filename_prefix 值。"""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "filename_prefix" and isinstance(v, str):
                    yield v
                else:
                    for x in ComfyClient._iter_prefixes(v):
                        yield x
        elif isinstance(obj, list):
            for v in obj:
                for x in ComfyClient._iter_prefixes(v):
                    yield x

    def find_prompt_id_by_prefix(self, output_prefix):
        """按唯一 output_prefix 在 /queue（running+pending）与 /history 中收养 prompt_id。"""
        try:
            q = self.queue() or {}
        except Exception:
            q = {}
        for key in ("queue_running", "queue_pending"):
            for entry in q.get(key, []) or []:
                pid, workflow = self._queue_entry(entry)
                if pid and output_prefix in set(self._iter_prefixes(workflow)):
                    return pid
        try:
            h = self.history() or {}
        except Exception:
            h = {}
        for pid, rec in h.items():
            if output_prefix in set(self._iter_prefixes(rec.get("prompt", {}))):
                return pid
        return None

    @staticmethod
    def _queue_entry(entry):
        """ComfyUI /queue 项 = [number, prompt_id, workflow, extra, outputs]。"""
        if isinstance(entry, list) and len(entry) >= 3:
            return entry[1], entry[2]
        if isinstance(entry, dict):
            return entry.get("prompt_id"), entry.get("prompt")
        return None, None

    def find_output_by_prefix(self, output_prefix):
        """NV 产物检查：/history 全量扫 output filename 前缀 + /view 探测存在性。"""
        try:
            h = self.history() or {}
        except Exception:
            return None
        for _pid, rec in h.items():
            for _nid, out in (rec.get("outputs") or {}).items():
                for img in out.get("images", []) or []:
                    fn = img.get("filename", "")
                    if fn.startswith(output_prefix) and self.output_exists(fn):
                        return fn
        return None

    def output_exists(self, filename):
        try:
            r = self.http.get(
                "%s/view?filename=%s&type=output" % (self.base, filename),
                timeout=self.timeout,
            )
            return r.status_code == 200 and len(r.content or b"") > 1000
        except Exception:
            return False

    def prompt_state(self, prompt_id):
        """返回 ('completed'|'error'|'running'|'missing', payload)。"""
        try:
            h = self.history(prompt_id) or {}
        except Exception:
            raise
        rec = h.get(prompt_id) or {}
        if rec:
            st = rec.get("status", {}) or {}
            if st.get("completed"):
                fn = None
                for _nid, out in (rec.get("outputs") or {}).items():
                    for img in out.get("images", []) or []:
                        fn = img.get("filename")
                        break
                return "completed", {"filename": fn, "raw": rec}
            if st.get("status_str") == "error":
                return "error", {"raw": rec}
            return "running", {"raw": rec}
        try:
            q = self.queue() or {}
        except Exception:
            q = {}
        for key in ("queue_running", "queue_pending"):
            for entry in q.get(key, []) or []:
                pid, _wf = self._queue_entry(entry)
                if pid == prompt_id:
                    return "running", {}
        return "missing", {}

    def download(self, filename, dest_path):
        r = self.http.get(
            "%s/view?filename=%s&type=output" % (self.base, filename), timeout=120
        )
        if r.status_code != 200 or len(r.content or b"") <= 1000:
            raise requests.exceptions.HTTPError(
                "download %s: status=%s size=%s"
                % (filename, r.status_code, len(r.content or b""))
            )
        d = os.path.dirname(os.path.abspath(dest_path))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(r.content)
        return hashlib.sha256(r.content).hexdigest()

    def logs_tail(self, limit=4000):
        """业务失败时抓 ComfyUI 日志尾（plan 0A.6）。取不到就返回空串，不阻塞清理。"""
        try:
            r = self.http.get(self.base + "/internal/logs/raw", timeout=self.timeout)
            if r.status_code == 200:
                return (r.text or "")[-limit:]
        except Exception:
            pass
        return ""


# --- RunPod GraphQL 客户端 --------------------------------------------------
class RunPodClient:
    def __init__(self, api_key=None, session=None, timeout=15):
        self.api_key = api_key or os.environ.get("RUNPOD_API_KEY", "")
        self.http = session or requests
        self.timeout = timeout

    def graphql(self, query):
        r = self.http.post(
            "https://api.runpod.io/graphql",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % self.api_key,
            },
            json={"query": query},
            timeout=self.timeout,
        )
        if getattr(r, "status_code", 200) >= 500:
            raise CreateUncertain("graphql HTTP %s" % r.status_code)
        try:
            return r.json()
        except Exception as e:
            raise CreateUncertain("graphql unparseable response: %s" % type(e).__name__)

    def list_pods(self):
        d = self.graphql(
            "query { myself { pods { id name desiredStatus costPerHr "
            "runtime { uptimeInSeconds } } } }"
        )
        pods = ((d or {}).get("data") or {}).get("myself") or {}
        return pods.get("pods") or []

    def find_pods_by_name(self, name):
        return [p for p in self.list_pods() if p.get("name") == name]

    def find_pods_by_name_prefix(self, prefix):
        return [p for p in self.list_pods() if (p.get("name") or "").startswith(prefix)]

    def pod_status(self, pod_id):
        d = self.graphql(
            'query { pod(input: { podId: "%s" }) { id desiredStatus '
            "runtime { uptimeInSeconds } } }" % pod_id
        )
        pod = ((d or {}).get("data") or {}).get("pod")
        if not pod:
            return "NOT_FOUND"
        if pod.get("runtime"):
            return "RUNNING"
        return pod.get("desiredStatus", "UNKNOWN")

    def create_pod(self, name, gpu_type_id, network_volume_id, image_name, container_disk_gb=20):
        """创建 owned pod。响应不确定（空/不可解析/5xx）一律抛 CreateUncertain ——
        调用方必须先按名称查询收养，确认不存在后才允许再次 create（plan 0A.7 ④）。"""
        q = (
            "mutation { podFindAndDeployOnDemand(input: { "
            'name: "%s", imageName: "%s", gpuTypeId: "%s", gpuCount: 1, '
            'cloudType: SECURE, containerDiskInGb: %d, volumeInGb: 0, '
            'volumeMountPath: "/workspace", networkVolumeId: "%s", '
            'ports: "22/tcp,8188/http,8888/http" '
            "}) { id name } }"
            % (name, image_name, gpu_type_id, container_disk_gb, network_volume_id)
        )
        d = self.graphql(q)
        if not isinstance(d, dict) or "data" not in d:
            raise CreateUncertain("create response missing data: %s" % str(d)[:200])
        pod = (d.get("data") or {}).get("podFindAndDeployOnDemand")
        if not pod or not pod.get("id"):
            # 明确错误（库存/余额/鉴权）走 errors；无 errors 又无 id = 不确定
            if d.get("errors"):
                return None
            raise CreateUncertain("create response has neither id nor errors")
        return pod

    def terminate_pod(self, pod_id):
        try:
            resp = self.graphql(
                'mutation { podTerminate(input: { podId: "%s" }) }' % pod_id
            )
        except Exception:
            return False
        return not (isinstance(resp, dict) and resp.get("errors"))


# --- plan 0A.6/0A.7 pod 所有权 ledger + 花费上限 ----------------------------
class PodManager:
    def __init__(
        self,
        ledger,
        runpod,
        batch_id,
        max_spend=DEFAULT_MAX_SPEND,
        rate=DEFAULT_POD_RATE,
        clock=time.time,
    ):
        self.ledger = ledger
        self.runpod = runpod
        self.batch_id = batch_id
        self.max_spend = max_spend
        self.rate = rate
        self.clock = clock

    # -- ledger helpers ------------------------------------------------------
    def pod_name(self, worker):
        return "wan22-%s-%s" % (self.batch_id, worker)

    def name_prefix(self):
        return "wan22-%s-" % self.batch_id

    def register(self, pod_id, ownership="owned", worker=None, name=None, rate=None, created_at=None):
        def m(doc):
            pods = doc.setdefault("pods", {})
            rec = pods.setdefault(pod_id, {})
            rec.update(
                {
                    "pod_id": pod_id,
                    "ownership": ownership,
                    "worker": worker if worker is not None else rec.get("worker"),
                    "name": name if name is not None else rec.get("name"),
                    "rate": rate if rate is not None else rec.get("rate", self.rate),
                    "status": rec.get("status", "running"),
                }
            )
            rec.setdefault("first_seen", created_at or self.clock())
            return rec

        return self.ledger.apply(m)

    def record_intent(self, worker):
        """创建任何 pod 前先原子持久化 intent（plan 0A.7 ①）。"""

        def m(doc):
            intents = doc.setdefault("intents", {})
            intents[worker] = {"name": self.pod_name(worker), "ts": self.clock()}
            return intents[worker]

        return self.ledger.apply(m)

    def alert(self, msg):
        def m(doc):
            doc.setdefault("alerts", []).append({"ts": self.clock(), "msg": msg})
            return msg

        self.ledger.apply(m)
        print("[ALERT] %s" % msg)

    def pods(self):
        return self.ledger.snapshot().get("pods", {})

    def owned_pod_ids(self, include_terminated=False):
        out = []
        for pid, rec in sorted(self.pods().items()):
            if rec.get("ownership") != "owned":
                continue
            if not include_terminated and rec.get("status") == "terminated":
                continue
            out.append(pid)
        return out

    # -- plan 0A.6 花费上限 --------------------------------------------------
    def spend_usd(self):
        total = 0.0
        now = self.clock()
        for _pid, rec in self.pods().items():
            if rec.get("ownership") != "owned":
                continue  # borrowed 不计入本 batch 花费
            sec = rec.get("billed_sec")
            if sec is None:
                sec = max(0.0, now - float(rec.get("first_seen", now)))
            total += (sec / 3600.0) * float(rec.get("rate", self.rate))
        return total

    def check_spend(self):
        spend = self.spend_usd()
        if spend >= self.max_spend:
            self.alert(
                "花费上限触发：$%.2f >= --max-spend $%.2f，清理全部 owned pod"
                % (spend, self.max_spend)
            )
            self.cleanup_all("max_spend")
            raise SpendLimitExceeded("spend $%.2f >= max $%.2f" % (spend, self.max_spend))
        return spend

    # -- 生命周期 ------------------------------------------------------------
    def terminate(self, pod_id, reason="", ownership=None):
        """owned → terminate（stop 会触发 EXITED 2x penalty，一律 terminate）。
        失败时 ledger 留 pending_cleanup + 告警，绝不静默。"""
        rec = self.pods().get(pod_id, {})
        own = ownership or rec.get("ownership", "owned")
        if own != "owned":
            return False  # borrowed 一律不动
        if rec.get("status") == "terminated":
            return True  # 幂等：已确认删除的 pod 不重复发 API
        ok = False
        try:
            ok = bool(self.runpod.terminate_pod(pod_id))
        except Exception as e:  # noqa: BLE001
            self.alert("terminate %s 异常：%s: %s" % (pod_id, type(e).__name__, e))
            ok = False
        now = self.clock()

        def m(doc):
            r = doc.setdefault("pods", {}).setdefault(pod_id, {"pod_id": pod_id})
            if r.get("billed_sec") is None:
                r["billed_sec"] = max(0.0, now - float(r.get("first_seen", now)))
            r["status"] = "terminated" if ok else "pending_cleanup"
            r["terminated_at"] = now if ok else None
            r["terminate_reason"] = reason
            return r

        self.ledger.apply(m)
        if not ok:
            self.alert(
                "terminate 失败，pod %s 留 pending_cleanup（reason=%s），需人工清理"
                % (pod_id, reason)
            )
        return ok

    def cleanup_all(self, reason=""):
        results = {}
        for pid in self.owned_pod_ids():
            results[pid] = self.terminate(pid, reason=reason)
        return results

    # -- plan 0A.7 ③ 名称前缀收养 --------------------------------------------
    def adopt_by_name_prefix(self):
        """启动/resume 时先按 wan22-<batch_id>- 名称前缀查询账户 pod 补录 ledger，
        再决定是否创建 —— 覆盖「API 创建成功但写 ledger 前崩溃」窗口。"""
        adopted = []
        try:
            found = self.runpod.find_pods_by_name_prefix(self.name_prefix())
        except Exception as e:  # noqa: BLE001
            self.alert("按名称收养查询失败：%s: %s" % (type(e).__name__, e))
            return adopted
        for p in found:
            pid = p.get("id")
            if not pid:
                continue
            worker = (p.get("name") or "").replace(self.name_prefix(), "") or None
            self.register(
                pid,
                ownership="owned",
                worker=worker,
                name=p.get("name"),
                rate=p.get("costPerHr"),
                created_at=p.get("createdAt"),
            )
            adopted.append(pid)
        return adopted

    def dedupe_worker_pods(self, worker):
        """同名重复项：保留 createdAt 最早者，其余立即 terminate（仍计入 max-spend）。"""
        name = self.pod_name(worker)
        found = self.runpod.find_pods_by_name(name)
        if not found:
            return None, []
        def _created(p):
            v = p.get("createdAt")
            if v is None:
                return float("inf")
            try:
                return float(v)
            except (TypeError, ValueError):
                return str(v)
        ordered = sorted(found, key=lambda p: (_created(p) == float("inf"), _created(p)))
        keep = ordered[0]
        extras = ordered[1:]
        for p in ordered:
            # 所有名称匹配 pod（含重复项）一律进 ledger → 计入 max-spend 与退出清理范围
            self.register(
                p.get("id"),
                ownership="owned",
                worker=worker,
                name=name,
                rate=p.get("costPerHr"),
                created_at=p.get("createdAt"),
            )
        killed = []
        for p in extras:
            self.terminate(p.get("id"), reason="duplicate_pod")
            killed.append(p.get("id"))
        return keep.get("id"), killed

    def ensure_pod(self, worker, gpu_type_id, network_volume_id, image_name, max_attempts=3):
        """创建状态机（plan 0A.7）：intent 落盘 → 名称查询收养 → 才创建；
        创建响应不确定 → 必须先查询收养，确认不存在后才允许再次 create。"""
        self.record_intent(worker)
        pid, _killed = self.dedupe_worker_pods(worker)
        if pid:
            return pid
        for _attempt in range(max_attempts):
            self.check_spend()
            try:
                pod = self.runpod.create_pod(
                    self.pod_name(worker), gpu_type_id, network_volume_id, image_name
                )
            except CreateUncertain as e:
                # 不盲建：先按稳定 pod 名查询，把全部匹配项纳入 ledger
                self.alert("create 响应不确定（%s）→ 先查询收养再决定" % e)
                pid, _killed = self.dedupe_worker_pods(worker)
                if pid:
                    return pid
                continue
            if not pod or not pod.get("id"):
                # 明确拒绝（库存/余额/鉴权）：确定未创建，不在本函数内重试
                # ——重试策略属调用方/grab_pod.sh；这里只负责「不盲建」
                return None
            self.register(
                pod["id"], ownership="owned", worker=worker, name=self.pod_name(worker)
            )
            # 创建成功后仍去重一次：覆盖上一轮不确定响应其实建成的情形
            pid, _killed = self.dedupe_worker_pods(worker)
            return pid or pod["id"]
        return None


# --- runner 子进程 ----------------------------------------------------------
class SubprocessRunnerLauncher:
    """spawn wan22_generate.py：注入 RUNPOD_POD_ID/RUNPOD_JOB_ID +
    --orchestrated --result-json --filename-prefix --keep-pod（plan 0A.1-0A.3）。"""

    def __init__(self, runner_path=None, python_exec=None):
        self.runner_path = runner_path or RUNNER_PATH
        self.python_exec = python_exec or sys.executable

    def build_argv(self, job_id, spec, ref_path, result_path, output_dir, output_prefix):
        argv = [
            self.python_exec,
            self.runner_path,
            "--prompt",
            spec.get("prompt", ""),
            "--image",
            ref_path,
            "--mode",
            spec.get("mode", "prod"),
            "--output-dir",
            output_dir,
            "--orchestrated",
            "--result-json",
            result_path,
            "--job-id",
            job_id,
            "--filename-prefix",
            output_prefix,
            "--keep-pod",
        ]
        if spec.get("end_image"):
            argv += ["--end-image", spec["end_image"]]
        if spec.get("seed") is not None:
            argv += ["--seed", str(spec["seed"])]
        if spec.get("lora_strength") is not None:
            argv += ["--lora-strength", str(spec["lora_strength"])]
        if spec.get("morph_lora"):
            argv += ["--morph-lora", spec["morph_lora"]]
        if spec.get("num_frames"):
            argv += ["--num-frames", str(spec["num_frames"])]
        if spec.get("negative"):
            argv += ["--negative", spec["negative"]]
        if spec.get("single_model"):
            argv.append("--single-model")
        if spec.get("skip_skin_gate"):
            argv.append("--skip-skin-gate")
        return argv

    def launch(self, job_id, spec, pod_id, ref_path, result_path, output_dir, output_prefix):
        argv = self.build_argv(job_id, spec, ref_path, result_path, output_dir, output_prefix)
        env = dict(os.environ)
        env["RUNPOD_POD_ID"] = pod_id
        env["RUNPOD_JOB_ID"] = job_id
        return subprocess.Popen(argv, env=env)


# --- 编排器 -----------------------------------------------------------------
class BatchOrchestrator:
    def __init__(
        self,
        jobs,
        workdir,
        pod_specs,
        batch_id=None,
        max_spend=DEFAULT_MAX_SPEND,
        rate=DEFAULT_POD_RATE,
        resume=False,
        runpod=None,
        comfy_factory=None,
        launcher=None,
        clock=time.time,
        sleeper=time.sleep,
        tail_interval=1.0,
        max_rounds=24,
    ):
        self.workdir = os.path.abspath(workdir)
        self.results_dir = os.path.join(self.workdir, "results")
        self.refs_dir = os.path.join(self.workdir, "refs")
        self.masters_dir = os.path.join(self.workdir, "masters")
        for d in (self.workdir, self.results_dir, self.refs_dir, self.masters_dir):
            os.makedirs(d, exist_ok=True)

        self.clock = clock
        self.sleeper = sleeper
        self.tail_interval = tail_interval
        self.max_rounds = max_rounds
        self.resume = resume

        ledger_path = os.path.join(self.workdir, "pods_owned.json")
        # plan 0A.7 ①：创建任何 pod 前先原子持久化稳定 batch_id
        self.ledger = SingleWriterStore.load(
            ledger_path, {"batch_id": None, "pods": {}, "intents": {}, "alerts": []}
        ).start()
        self.batch_id = self._ensure_batch_id(batch_id)

        manifest_path = os.path.join(self.workdir, "manifest.json")
        self.store = ManifestStore.load(
            manifest_path,
            {"batch_id": self.batch_id, "created_at": time.time(), "jobs": {}},
        ).start()

        self.jobs = list(jobs)
        self.job_ids = []
        for spec in self.jobs:
            jid = spec["job_id"]
            self.job_ids.append(jid)
            self.store.ensure_job(jid, spec)

        self.runpod = runpod if runpod is not None else RunPodClient()
        self.pods = self._parse_pod_specs(pod_specs)
        self.pod_mgr = PodManager(
            self.ledger, self.runpod, self.batch_id, max_spend=max_spend, rate=rate, clock=clock
        )
        # Phase 1.1：每 pod 一个 worker（w0/w1/...），pod 名与 ledger 记录一一对应
        for idx, (pid, ownership) in enumerate(self.pods):
            self.pod_mgr.register(pid, ownership=ownership, worker="w%d" % idx)
        self._comfy_factory = comfy_factory or (lambda pod_id: ComfyClient(pod_id))
        self._comfy_cache = {}
        self._comfy_lock = threading.Lock()
        self._spend_lock = threading.Lock()
        self.launcher = launcher or SubprocessRunnerLauncher()
        self._shutting_down = False
        # job 队列：worker 线程从这里抢活（run() 填充；直接调 process_job 时为空）
        self._queue = queue.Queue()

    # -- setup ---------------------------------------------------------------
    def _ensure_batch_id(self, batch_id):
        existing = self.ledger.snapshot().get("batch_id")
        if existing and not batch_id:
            return existing
        bid = batch_id or existing or "%s-%s" % (
            time.strftime("%Y%m%d"),
            uuid.uuid4().hex[:6],
        )

        def m(doc):
            doc["batch_id"] = bid
            return bid

        self.ledger.apply(m)
        return bid

    @staticmethod
    def _parse_pod_specs(pod_specs):
        """'<podid>' → owned；'<podid>:borrowed' → borrowed（一律不动）。

        Phase 1.1：支持逗号分隔的 pod 池（`--pods p1,p2:borrowed,p3`），空格分隔
        （0A/0B 用法）继续有效；重复 pod id 去重（一个 pod 只配一个 worker）。
        """
        out = []
        seen = set()
        for raw in pod_specs or []:
            for s in str(raw).split(","):
                s = s.strip()
                if not s:
                    continue
                if ":" in s:
                    pid, own = s.split(":", 1)
                    pid = pid.strip()
                    ownership = "borrowed" if own.strip().lower().startswith("b") else "owned"
                else:
                    pid, ownership = s, "owned"
                if pid in seen:
                    continue
                seen.add(pid)
                out.append((pid, ownership))
        return out

    def comfy(self, pod_id):
        with self._comfy_lock:  # 多 worker 并发取 client，保证每 pod 只建一次
            if pod_id not in self._comfy_cache:
                self._comfy_cache[pod_id] = self._comfy_factory(pod_id)
            return self._comfy_cache[pod_id]

    def pod_for(self, job_id):
        """job 未认领时的兜底 pod（池模式下由 worker 认领时写入 manifest.pod_id）。"""
        job = self.store.job(job_id)
        return job.get("pod_id") or (self.pods[0][0] if self.pods else None)

    def ownership_of(self, pod_id):
        return self.pod_mgr.pods().get(pod_id, {}).get("ownership", "owned")

    def close(self):
        self.store.stop()
        self.ledger.stop()

    # -- 事件流应用（plan 0A.2 → 0A.4）--------------------------------------
    def result_path(self, job_id):
        return os.path.join(self.results_dir, "%s.ndjson" % job_id)

    def _read_events(self, path, offset):
        """增量读 NDJSON；只应用完整行（末尾无换行的半行留到下轮）。"""
        if not os.path.exists(path):
            return [], offset
        events = []
        with open(path, "rb") as f:  # 二进制读：offset 是确定的字节位移
            f.seek(offset)
            data = f.read()
        consumed = data.rfind(b"\n")
        if consumed == -1:
            return [], offset
        chunk = data[: consumed + 1]
        for raw in chunk.splitlines():
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return events, offset + len(chunk)

    def apply_event(self, job_id, ev):
        """runner 事件 → 耐久状态机转移（batch 收到 submitted 立即经单写者持久化）。"""
        kind = ev.get("event")
        state = self.store.state(job_id)
        if kind == "submitting":
            self.store.update_job(job_id, output_prefix=ev.get("output_prefix"))
        elif kind == "submitted":
            self.store.transition(
                job_id,
                "submitted",
                reason="runner_submitted",
                prompt_id=ev.get("prompt_id"),
                output_prefix=ev.get("output_prefix") or self.store.job(job_id).get("output_prefix"),
            )
        elif kind == "remote_completed":
            if state != "remote_completed":
                self.store.transition(
                    job_id,
                    "remote_completed",
                    reason="runner_remote_completed",
                    prompt_id=ev.get("prompt_id") or self.store.job(job_id).get("prompt_id"),
                    remote_filename=ev.get("remote_filename"),
                )
        elif kind == "downloaded":
            if state in ("submitting", "submitted"):
                # 事件流缺 remote_completed（截断/丢行）时补一跳，保持状态机连续
                self.store.transition(
                    job_id,
                    "remote_completed",
                    reason="implied_by_downloaded",
                    remote_filename=ev.get("remote_filename"),
                )
            self.store.transition(
                job_id,
                "downloaded",
                reason="runner_downloaded",
                local_file=ev.get("file"),
                sha256=ev.get("sha256"),
                remote_filename=ev.get("remote_filename")
                or self.store.job(job_id).get("remote_filename"),
            )
        elif kind == "failed":
            self._apply_failed(job_id, ev)
        return self.store.state(job_id)

    def _apply_failed(self, job_id, ev):
        cls = ev.get("class")
        stage = ev.get("stage")
        detail = ev.get("detail", "")
        if cls == "business":
            # node error / remote error：attempt++ 转 business_failed
            self.store.record_business_error(job_id, stage, detail)
            return
        # 网络类：一律不降级状态、不计 business attempt（plan 0A.8）
        self.store.record_network_error(job_id, stage, detail)
        if stage == "upload":
            # plan 0A.4 (a)：上传失败严格发生在 POST 之前 → 远端必然无任务，
            # 回 pending 退避重传是安全的（唯一允许从 submitting 直接回退的网络分支）
            if self.store.state(job_id) == "submitting":
                self.store.transition(job_id, "pending", reason="upload_failed_pre_submit")
        # submit_uncertain → 停留 submitting（由 _reconcile_submitting 收养/确认）
        # poll → 停留 submitted；download → 停留 remote_completed

    # -- 主循环（Phase 1.1：pod 池 worker 线程）-------------------------------
    def run(self):
        # plan 0A.7 ③：启动/resume 先按名称前缀收养账户里的同批 pod
        self.pod_mgr.adopt_by_name_prefix()
        results = {}
        for jid in self.job_ids:
            if self.store.state(jid) == "done":
                results[jid] = "done"  # resume：已完成的不再入队
            else:
                self._queue.put(jid)
        errors = []
        pods = self.pods or [(None, "owned")]
        # 第一个 pod 跑在当前线程：单 pod 时与 0A/0B 完全同路径（含异常直抛语义）
        threads = [
            threading.Thread(
                target=self._worker, args=(pid, results, errors), name="worker-%s" % pid
            )
            for pid, _own in pods[1:]
        ]
        for t in threads:
            t.start()
        self._worker(pods[0][0], results, errors)
        for t in threads:
            t.join()
        if errors:
            raise errors[0]
        self.finish()
        return results

    def _worker(self, pod_id, results, errors):
        """一个 pod 一个 worker：从队列抢活，队列空后按决策表独立收尾（不等其他 pod）。"""
        try:
            while True:
                try:
                    job_id = self._queue.get_nowait()
                except queue.Empty:
                    break
                results[job_id] = self.process_job(job_id, pod_id=pod_id)
            self._pod_drained_decision(pod_id)
        except Exception as e:  # noqa: BLE001 — 交 run() 汇总后重抛
            errors.append(e)

    def _claim(self, job_id, pod_id):
        """worker 认领 job：写入 pod_id（单写者）。

        已有 pod_id 且该 pod 仍在池中时保持不变 —— resume 场景下 prompt_id/产物
        绑在原 pod 的 ComfyUI 上，换 pod 会让 /history 收养失效。
        """
        cur = self.store.job(job_id).get("pod_id")
        if cur and cur in [p for p, _o in self.pods]:
            return cur
        self.store.update_job(job_id, pod_id=pod_id, worker_pod=pod_id)
        return pod_id

    def process_job(self, job_id, pod_id=None):
        if pod_id is not None:
            pod_id = self._claim(job_id, pod_id)
        for _round in range(self.max_rounds):
            with self._spend_lock:  # 多 worker 并发时只让一个走清理路径
                self.pod_mgr.check_spend()
            state = self.store.state(job_id)
            if state == "done":
                return "done"
            if state == "business_failed":
                job = self.store.job(job_id)
                if int(job.get("attempts", 0)) >= MAX_BUSINESS_ATTEMPTS:
                    return "business_failed"  # 不阻塞其余 job
                self._backoff(job_id, business=True)
                self.store.transition(job_id, "pending", reason="business_retry")
                continue
            if state == "pending":
                self._launch(job_id)
            elif state == "submitting":
                self._reconcile_submitting(job_id)
            elif state == "submitted":
                self._reconcile_submitted(job_id)
            elif state == "remote_completed":
                self._download(job_id)
            elif state == "downloaded":
                self._finalize(job_id)
            else:
                raise RuntimeError("unknown state %r for %s" % (state, job_id))
        return self.store.state(job_id)

    def _backoff(self, job_id, business=False):
        job = self.store.job(job_id)
        idx = int(job.get("attempts", 0)) if business else int(job.get("net_retries", 1)) - 1
        delay = backoff_delay(idx)
        self.store.update_job(job_id, last_backoff_sec=delay)
        self.sleeper(delay)
        return delay

    # -- pending → submitting → runner --------------------------------------
    def prepare_ref(self, job_id, image_path):
        """并发产物隔离（plan 0A.3）：上传前把 ref 复制为唯一名 job_<jobid>_<sha8>.png。"""
        sha8 = sha256_file(image_path)[:8]
        ext = os.path.splitext(image_path)[1] or ".png"
        dest = os.path.join(self.refs_dir, "job_%s_%s%s" % (job_id, sha8, ext))
        if not os.path.exists(dest):
            shutil.copy2(image_path, dest)
        return dest

    def _launch(self, job_id):
        job = self.store.job(job_id)
        spec = job.get("spec", {})
        pod_id = self.pod_for(job_id)
        prefix = job.get("output_prefix") or "wan22_%s" % job_id
        # submitting 意图先落盘（单写者 + fsync 原子写），然后才 spawn runner
        self.store.transition(
            job_id,
            "submitting",
            reason="spawn_runner",
            pod_id=pod_id,
            output_prefix=prefix,
            submitting_at=self.clock(),
        )
        ref = self.prepare_ref(job_id, spec["image"])
        out_dir = spec.get("output_dir") or self.masters_dir
        os.makedirs(out_dir, exist_ok=True)
        result_path = self.result_path(job_id)
        proc = self.launcher.launch(
            job_id, spec, pod_id, ref, result_path, out_dir, prefix
        )
        rc = self._consume(job_id, result_path, proc)
        self.store.update_job(job_id, last_runner_exit=rc)
        return rc

    def _consume(self, job_id, result_path, proc):
        offset = int(self.store.job(job_id).get("result_offset", 0))
        while True:
            events, offset = self._read_events(result_path, offset)
            for ev in events:
                self.apply_event(job_id, ev)
            self.store.update_job(job_id, result_offset=offset)
            if proc.poll() is not None:
                break
            self.sleeper(self.tail_interval)
        events, offset = self._read_events(result_path, offset)
        for ev in events:
            self.apply_event(job_id, ev)
        self.store.update_job(job_id, result_offset=offset)
        return proc.returncode

    # -- submitting：收养 or 有界双确认（plan 0A.4 c / 0A.5 g,i,j）-----------
    def _reconcile_submitting(self, job_id):
        job = self.store.job(job_id)
        pod_id = job.get("pod_id") or self.pod_for(job_id)
        prefix = job.get("output_prefix")
        comfy = self.comfy(pod_id)
        if not comfy.alive():
            # pod 不可达 → 不做任何降级（有界确认窗口要求 pod 可达）。
            # 决策表「网络中断退避」行：只有已确认运行中任务（拿到过 prompt_id）
            # 才值得等窗口，否则立即 terminate 不空转烧钱（产物经 NV 可恢复）
            self.store.record_network_error(job_id, "submit_uncertain", "pod unreachable")
            self._backoff(job_id)
            self._pod_network_decision(
                job_id, pod_id, has_running_task=bool(job.get("prompt_id"))
            )
            return self.store.state(job_id)
        kind, payload = self._confirm_absent(job_id, comfy, prefix)
        if kind == "prompt":
            self.store.transition(
                job_id, "submitted", reason="adopted_by_output_prefix", prompt_id=payload
            )
        elif kind == "output":
            self.store.transition(
                job_id,
                "remote_completed",
                reason="adopted_nv_output",
                remote_filename=payload,
            )
        else:
            # 远端 queue/history 与 NV 均确认不存在 → 才允许回 pending 重提
            self.store.transition(job_id, "pending", reason="confirmed_absent")
        return self.store.state(job_id)

    def _confirm_absent(self, job_id, comfy, prefix):
        """有界确认窗口：间隔 ≥60s 连续 CONFIRM_ROUNDS 次远端+NV 均空才算不存在。

        返回 ('prompt', prompt_id) | ('output', filename) | (None, None)。
        """
        for i in range(CONFIRM_ROUNDS):
            if i:
                self.sleeper(CONFIRM_INTERVAL_SEC)
            pid = comfy.find_prompt_id_by_prefix(prefix)
            if pid:
                return "prompt", pid
            fn = comfy.find_output_by_prefix(prefix)
            if fn:
                return "output", fn
            self.store.update_job(
                job_id, confirm_empty_rounds=i + 1, last_confirm_at=self.clock()
            )
        return None, None

    # -- submitted：按 prompt_id 续查 /history（plan 0A.5 ②③）---------------
    def _reconcile_submitted(self, job_id):
        job = self.store.job(job_id)
        pod_id = job.get("pod_id") or self.pod_for(job_id)
        comfy = self.comfy(pod_id)
        prompt_id = job.get("prompt_id")
        prefix = job.get("output_prefix")
        if not comfy.alive():
            # pod 已死/重建：状态保持 submitted（不降级），退避后按决策表处理 pod
            self.store.record_network_error(job_id, "poll", "pod unreachable")
            self._backoff(job_id)
            self._pod_network_decision(job_id, pod_id, has_running_task=True)
            return self.store.state(job_id)
        try:
            state, payload = comfy.prompt_state(prompt_id) if prompt_id else ("missing", {})
        except Exception as e:  # noqa: BLE001 网络错：保持 submitted
            self.store.record_network_error(job_id, "poll", "%s: %s" % (type(e).__name__, e))
            self._backoff(job_id)
            return self.store.state(job_id)
        if state == "completed":
            self.store.transition(
                job_id,
                "remote_completed",
                reason="history_completed",
                remote_filename=payload.get("filename"),
            )
        elif state == "error":
            self.store.record_business_error(job_id, "remote", "comfyui history error")
            self._pod_business_decision(job_id, pod_id)
        elif state == "running":
            self._wait_remote(job_id, comfy, prompt_id)
        else:  # missing：远端无任务 → 先查 NV 产物，再走有界双确认
            fn = comfy.find_output_by_prefix(prefix)
            if fn:
                self.store.transition(
                    job_id, "remote_completed", reason="nv_output_found", remote_filename=fn
                )
            else:
                kind, payload2 = self._confirm_absent(job_id, comfy, prefix)
                if kind == "prompt":
                    self.store.update_job(job_id, prompt_id=payload2)
                elif kind == "output":
                    self.store.transition(
                        job_id, "remote_completed", reason="nv_output_found", remote_filename=payload2
                    )
                else:
                    self.store.transition(job_id, "pending", reason="confirmed_absent")
        return self.store.state(job_id)

    def _wait_remote(self, job_id, comfy, prompt_id, max_polls=240, interval=10):
        """远端任务仍在跑：等它结束再取产物，绝不并行重提。"""
        for _ in range(max_polls):
            self.sleeper(interval)
            try:
                state, payload = comfy.prompt_state(prompt_id)
            except Exception as e:  # noqa: BLE001
                self.store.record_network_error(job_id, "poll", "%s: %s" % (type(e).__name__, e))
                return self.store.state(job_id)
            if state == "completed":
                return self.store.transition(
                    job_id,
                    "remote_completed",
                    reason="history_completed",
                    remote_filename=payload.get("filename"),
                )
            if state == "error":
                self.store.record_business_error(job_id, "remote", "comfyui history error")
                return self.store.state(job_id)
            if state == "missing":
                return self.store.state(job_id)
        self.store.record_network_error(job_id, "poll", "wait window exhausted")
        return self.store.state(job_id)

    # -- remote_completed → downloaded --------------------------------------
    def _download(self, job_id):
        job = self.store.job(job_id)
        pod_id = job.get("pod_id") or self.pod_for(job_id)
        comfy = self.comfy(pod_id)
        fn = job.get("remote_filename")
        if not fn:
            fn = comfy.find_output_by_prefix(job.get("output_prefix"))
            if not fn:
                self.store.record_network_error(job_id, "download", "remote filename unknown")
                self._backoff(job_id)
                return self.store.state(job_id)
            self.store.update_job(job_id, remote_filename=fn)
        out_dir = (job.get("spec") or {}).get("output_dir") or self.masters_dir
        dest = os.path.join(out_dir, os.path.basename(fn))
        try:
            sha = comfy.download(fn, dest)
        except Exception as e:  # noqa: BLE001 下载失败：状态保持 remote_completed 只重下
            self.store.record_network_error(job_id, "download", "%s: %s" % (type(e).__name__, e))
            self._backoff(job_id)
            return self.store.state(job_id)
        self.store.transition(
            job_id, "downloaded", reason="batch_download", local_file=dest, sha256=sha
        )
        return self.store.state(job_id)

    # -- downloaded → done（本地校验 + sidecar）------------------------------
    def _finalize(self, job_id):
        job = self.store.job(job_id)
        path = job.get("local_file")
        if not path or not os.path.exists(path):
            # 产物不在本地 → 退回 remote_completed 重下（不重提 GPU job）
            self.store.update_job(job_id, local_file=None)
            self.store.transition(job_id, "remote_completed", reason="local_missing")
            return self.store.state(job_id)
        sha = sha256_file(path)
        if job.get("sha256") and sha != job["sha256"]:
            self.store.record_network_error(job_id, "download", "sha mismatch on verify")
            self.store.transition(job_id, "remote_completed", reason="sha_mismatch")
            return self.store.state(job_id)
        sidecar = path + ".batch.json"
        with open(sidecar, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "batch_id": self.batch_id,
                    "job_id": job_id,
                    "prompt_id": job.get("prompt_id"),
                    "output_prefix": job.get("output_prefix"),
                    "remote_filename": job.get("remote_filename"),
                    "pod_id": job.get("pod_id"),
                    "sha256": sha,
                    "file": os.path.abspath(path),
                    "ts": time.time(),
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        self.store.transition(job_id, "done", reason="verified", sha256=sha, sidecar=sidecar)
        self._pod_success_decision(job_id, job.get("pod_id") or self.pod_for(job_id))
        return "done"

    def local_master_ok(self, job_id):
        """plan 0A.5 ①：本地 masters + sidecar sha 校验通过 → done。"""
        job = self.store.job(job_id)
        path = job.get("local_file")
        sidecar = job.get("sidecar")
        if not path or not os.path.exists(path) or not sidecar or not os.path.exists(sidecar):
            return False
        try:
            with open(sidecar, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            return False
        return meta.get("sha256") == sha256_file(path)

    # -- plan 0A.6 决策表落地 ------------------------------------------------
    def remaining_jobs_on(self, pod_id):
        """该 pod 还可能要干的活 = 已认领未完成的 job + 队列里未认领的 job。

        队列项计入是 Phase 1.1 的关键：任一空闲 worker 都可能抢到它们，
        队列非空时不能把自己的 pod 关掉（关掉了就没人能接活）。
        """
        n = self._queue.qsize()
        for jid in self.job_ids:
            j = self.store.job(jid)
            if j.get("state") in ("done", "business_failed"):
                continue
            if j.get("pod_id") == pod_id:
                n += 1
        return n

    def _pod_drained_decision(self, pod_id):
        """worker 队列抽干后按决策表独立收尾：该 pod 无剩余 job → 立即 terminate，
        不等其他 pod（plan 0A.6「job 成功且该 pod 无剩余 job」行）。"""
        if pod_id is None:
            return "none"
        action = pod_action(
            self.ownership_of(pod_id),
            "job_success",
            remaining_jobs=self.remaining_jobs_on(pod_id),
        )
        if action == "terminate":
            self.pod_mgr.terminate(pod_id, reason="pod_jobs_drained")
        return action

    def _pod_success_decision(self, job_id, pod_id):
        action = pod_action(
            self.ownership_of(pod_id),
            "job_success",
            remaining_jobs=self.remaining_jobs_on(pod_id),
        )
        if action == "terminate":
            self.pod_mgr.terminate(pod_id, reason="job_success")
        return action

    def _pod_business_decision(self, job_id, pod_id):
        job = self.store.job(job_id)
        if int(job.get("attempts", 0)) < MAX_BUSINESS_ATTEMPTS:
            return "keep"
        action = pod_action(self.ownership_of(pod_id), "business_failed")
        if action == "terminate":
            # 先抓 ComfyUI 日志尾落盘，再 terminate
            tail = self.comfy(pod_id).logs_tail()
            if tail:
                log_path = os.path.join(self.workdir, "comfyui_%s_%s.log" % (pod_id, job_id))
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write(tail)
                self.store.update_job(job_id, comfy_log=log_path)
            self.pod_mgr.terminate(pod_id, reason="business_failed")
        return action

    def _pod_network_decision(self, job_id, pod_id, has_running_task):
        job = self.store.job(job_id)
        started = float(job.get("submitting_at") or job.get("state_at") or self.clock())
        elapsed = max(0.0, self.clock() - started)
        action = pod_action(
            self.ownership_of(pod_id),
            "network_backoff",
            has_running_task=has_running_task,
            wait_elapsed_sec=elapsed,
        )
        if action == "terminate":
            self.pod_mgr.terminate(pod_id, reason="network_backoff")
        return action

    # -- 收尾 / 信号 ---------------------------------------------------------
    def finish(self):
        for pid in self.pod_mgr.owned_pod_ids():
            if self.remaining_jobs_on(pid) <= 0:
                self.pod_mgr.terminate(pid, reason="batch_finished")

    def handle_signal(self, signum=None, frame=None):
        """Ctrl-C/SIGTERM：30s 时间盒尝试补下载已 completed 产物 → 全部 terminate。"""
        if self._shutting_down:
            return
        self._shutting_down = True
        deadline = self.clock() + SIGNAL_DRAIN_SEC
        for jid in self.job_ids:
            if self.clock() >= deadline:
                break
            if self.store.state(jid) == "remote_completed":
                try:
                    self._download(jid)
                except Exception:  # noqa: BLE001 时间盒内尽力而为
                    pass
        self.pod_mgr.cleanup_all("signal")

    def install_signal_handlers(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self.handle_signal)


# --- CLI --------------------------------------------------------------------
def load_jobs(path):
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    jobs = doc.get("jobs") if isinstance(doc, dict) else doc
    if not isinstance(jobs, list):
        raise ValueError("jobs file must be a list or {'jobs': [...]}")
    seen = set()
    for j in jobs:
        for key in ("job_id", "prompt", "image"):
            if not j.get(key):
                raise ValueError("job missing required field %r: %s" % (key, j))
        if j["job_id"] in seen:
            raise ValueError("duplicate job_id: %s" % j["job_id"])
        seen.add(j["job_id"])
    return jobs


def estimate_job_minutes(spec):
    """单 job 生成耗时粗估（分钟）。实测口径：morph(FLF2V) ≈21min、bounce ≈24min、
    test/mid/hd 档 ≈7min（plan Context + Phase 2 verify 的 wall 目标同源）。"""
    if spec.get("mode", "prod") != "prod":
        return JOB_WALL_MIN["test"]
    return JOB_WALL_MIN["morph"] if spec.get("end_image") else JOB_WALL_MIN["bounce"]


def plan_allocation(jobs, pods):
    """把 job 贪心分给当前累计耗时最小的 pod —— 近似 worker 抢活（work-stealing）
    的实际落点。返回 [(pod_id, ownership, [(job, minutes)], 累计分钟), ...]。"""
    if not pods:
        return []
    buckets = [[p, o, [], 0.0] for p, o in pods]
    for spec in jobs:
        mins = estimate_job_minutes(spec)
        b = min(buckets, key=lambda x: x[3])
        b[2].append((spec, mins))
        b[3] += mins
    return [(b[0], b[1], b[2], b[3]) for b in buckets]


def print_allocation(jobs, pods, batch_id, max_spend):
    print("batch_id=%s  max_spend=$%.2f  jobs=%d  pods=%d" % (batch_id, max_spend, len(jobs), len(pods)))
    if not pods:
        print("(no pods given — 无法分配)")
        return
    alloc = plan_allocation(jobs, pods)
    for pod_id, ownership, items, total in alloc:
        print("")
        print("pod %s (%s)  jobs=%d  gen≈%.0fmin" % (pod_id, ownership, len(items), total))
        print("  %-20s %-8s %-8s %-22s %s" % ("job_id", "mode", "seed", "output_prefix", "est"))
        for spec, mins in items:
            print(
                "  %-20s %-8s %-8s %-22s %.0fmin"
                % (
                    spec["job_id"],
                    spec.get("mode", "prod"),
                    spec.get("seed", "-"),
                    "wan22_%s" % spec["job_id"],
                    mins,
                )
            )
        if not items:
            print("  (idle)")
    wall = POD_SPINUP_MIN + max(a[3] for a in alloc)
    print("")
    print(
        "预计 wall ≈ spinup %.0fmin + max(pod gen %s) = %.0fmin"
        % (POD_SPINUP_MIN, "/".join("%.0f" % a[3] for a in alloc), wall)
    )
    print("(dry-run：未发任何 API、未创建 pod、未写 manifest)")


def main(argv=None):
    ap = argparse.ArgumentParser(description="wan2.2 batch 编排内核（Phase 0A + 1.1/1.2 pod 池）")
    ap.add_argument("--jobs", required=True, help="job 表 JSON 路径")
    ap.add_argument(
        "--pods",
        nargs="+",
        default=[],
        help="pod 池：逗号或空格分隔（p1,p2:borrowed,p3），每 pod 一个 worker 线程"
        "从 job 队列抢活。<podid> 视为 owned（batch 可 terminate）；"
        "<podid>:borrowed 一律不动",
    )
    ap.add_argument("--workdir", default=".wan22_batch", help="manifest/ledger/结果目录")
    ap.add_argument("--batch-id", default=None)
    ap.add_argument("--resume", action="store_true", help="按状态机续跑既有 workdir")
    ap.add_argument("--max-spend", type=float, default=DEFAULT_MAX_SPEND)
    ap.add_argument("--pod-rate", type=float, default=DEFAULT_POD_RATE, help="$/h")
    ap.add_argument("--dry-run", action="store_true", help="只打印分配表，不发任何 API")
    args = ap.parse_args(argv)

    jobs = load_jobs(args.jobs)
    if args.dry_run:
        pods = BatchOrchestrator._parse_pod_specs(args.pods)
        print_allocation(jobs, pods, args.batch_id or "<pending>", args.max_spend)
        return 0

    orch = BatchOrchestrator(
        jobs,
        args.workdir,
        args.pods,
        batch_id=args.batch_id,
        max_spend=args.max_spend,
        rate=args.pod_rate,
        resume=args.resume,
    )
    orch.install_signal_handlers()
    rc = 0
    try:
        results = orch.run()
        for jid, st in sorted(results.items()):
            print("%-20s %s" % (jid, st))
        rc = 0 if all(s == "done" for s in results.values()) else 1
    except SpendLimitExceeded as e:
        print("[ABORT] %s" % e)
        rc = 3
    finally:
        orch.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())

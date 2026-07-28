#!/usr/bin/env python3
"""wan2.2 batch 编排内核 + runner 编排接口的离线测试（plan Phase 0A verify 全覆盖）。

**零真实网络**：所有 HTTP/GraphQL/subprocess 边界均由假对象接管；不创建任何 pod。

跑法（本机无 pytest）：
    python3 -m unittest discover -s runpod/tests -t runpod/tests -v
装了 pytest 亦可直接 `pytest runpod/tests`（用例为标准 unittest.TestCase）。

用例 ↔ plan verify 对应见各 TestCase docstring 的 [0A.x] 标记。
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import textwrap
import threading
import time
import unittest

RUNPOD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RUNPOD_DIR not in sys.path:
    sys.path.insert(0, RUNPOD_DIR)

# runner 在 import 时读这两个 env（缺 API key 会 sys.exit(2)）——先设假值再 import
os.environ.setdefault("RUNPOD_API_KEY", "test-key-offline-only")
os.environ.setdefault("RUNPOD_POD_ID", "fakepod0")

import requests  # noqa: E402  仅借用其 exceptions 类型，不发请求

import wan22_batch as wb  # noqa: E402
import wan22_generate as wg  # noqa: E402


# =============================================================================
# 测试替身
# =============================================================================
class Patcher:
    def __init__(self):
        self._saved = []

    def set(self, obj, name, value):
        self._saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._saved):
            setattr(obj, name, old)
        self._saved = []


class FakeTime:
    """替换模块级 time：sleep 记账不真睡，time() 走真实时钟。"""

    def __init__(self):
        self.slept = []

    def sleep(self, sec):
        self.slept.append(sec)

    def time(self):
        return time.time()


class OsProxy:
    """转发到真 os，但记录 fsync 调用（验证 plan 0A.2 的 fsync 时点）。"""

    def __init__(self, real):
        self._real = real
        self.fsync_calls = 0

    def __getattr__(self, name):
        return getattr(self._real, name)

    def fsync(self, fd):
        self.fsync_calls += 1
        return self._real.fsync(fd)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b"", text=""):
        self.status_code = status_code
        self._json = json_data
        self.content = content
        self.text = text or (json.dumps(json_data) if json_data is not None else "")

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError("HTTP %s" % self.status_code)


class FakeHTTP:
    """假 requests 模块：按 URL 子串路由到 handler。handler 可返回 FakeResponse 或抛异常。"""

    exceptions = requests.exceptions

    def __init__(self):
        self.routes = []  # (method, needle, handler)
        self.calls = []

    def route(self, method, needle, handler):
        self.routes.append((method.upper(), needle, handler))
        return self

    def _dispatch(self, method, url, kwargs):
        self.calls.append((method, url))
        for m, needle, handler in self.routes:
            if m == method and needle in url:
                out = handler(url, kwargs) if callable(handler) else handler
                if isinstance(out, Exception):
                    raise out
                return out
        raise AssertionError("unrouted %s %s" % (method, url))

    def get(self, url, **kwargs):
        return self._dispatch("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._dispatch("POST", url, kwargs)

    def count(self, method, needle):
        return len([1 for m, u in self.calls if m == method and needle in u])


class FakeProc:
    """已退出的 subprocess 替身。"""

    def __init__(self, returncode=0):
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class FakeLauncher:
    """按脚本模拟 runner 子进程：写 NDJSON 事件（+可选产物文件），返回 exit code。

    每个 program:
        {"events": [...], "exit": 1, "artifact": ("wan22_j1_00001.webp", b"bytes")}
    artifact 存在时自动落盘并追加 downloaded 事件（file+sha256）。
    post_count = 脚本里出现 submitting 的次数 = runner 真正走到 POST /prompt 的次数。
    """

    def __init__(self, programs, default=None):
        self.programs = programs
        self.default = default or {"events": [], "exit": 1}
        self.launches = []
        self.post_count = 0

    def launch(self, job_id, spec, pod_id, ref_path, result_path, output_dir, output_prefix):
        idx = len([1 for l in self.launches if l["job_id"] == job_id])
        progs = self.programs.get(job_id, [])
        prog = progs[idx] if idx < len(progs) else self.default
        self.launches.append(
            {
                "job_id": job_id,
                "pod_id": pod_id,
                "ref": ref_path,
                "prefix": output_prefix,
                "result": result_path,
                "out_dir": output_dir,
            }
        )
        events = [dict(e) for e in prog.get("events", [])]
        artifact = prog.get("artifact")
        if artifact:
            fn, blob = artifact
            path = os.path.join(output_dir, fn)
            with open(path, "wb") as f:
                f.write(blob)
            events.append(
                {
                    "event": "downloaded",
                    "file": os.path.abspath(path),
                    "sha256": hashlib.sha256(blob).hexdigest(),
                    "remote_filename": fn,
                }
            )
        with open(result_path, "a", encoding="utf-8") as f:
            for ev in events:
                ev.setdefault("job_id", job_id)
                ev.setdefault("pod_id", pod_id)
                ev.setdefault("ts", time.time())
                if ev.get("event") == "submitting":
                    self.post_count += 1
                f.write(json.dumps(ev) + "\n")
        return FakeProc(prog.get("exit", 0))


class FakeComfy:
    def __init__(
        self,
        alive=True,
        prompt_by_prefix=None,
        output_by_prefix=None,
        states=None,
        download_errors=0,
        content=b"W" * 4096,
    ):
        self._alive = alive
        self.prompt_by_prefix = prompt_by_prefix or {}
        self.output_by_prefix = output_by_prefix or {}
        self.states = states or {}
        self.download_errors = download_errors
        self.content = content
        self.calls = []

    def alive(self):
        self.calls.append("alive")
        return self._alive

    def find_prompt_id_by_prefix(self, prefix):
        self.calls.append(("find_prompt", prefix))
        return self.prompt_by_prefix.get(prefix)

    def find_output_by_prefix(self, prefix):
        self.calls.append(("find_output", prefix))
        return self.output_by_prefix.get(prefix)

    def prompt_state(self, prompt_id):
        self.calls.append(("prompt_state", prompt_id))
        seq = self.states.get(prompt_id)
        if not seq:
            return "missing", {}
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def download(self, filename, dest):
        self.calls.append(("download", filename))
        if self.download_errors > 0:
            self.download_errors -= 1
            raise requests.exceptions.ConnectionError("download boom")
        d = os.path.dirname(os.path.abspath(dest))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(self.content)
        return hashlib.sha256(self.content).hexdigest()

    def logs_tail(self, limit=4000):
        return "comfyui log tail"


class FakeRunPod:
    """RunPod GraphQL 替身：账户 pod 列表 + 可编排的 create 行为。"""

    def __init__(self, pods=None, create_script=None, terminate_ok=True):
        self.pods = list(pods or [])
        self.create_script = list(create_script or [])
        self.terminate_ok = terminate_ok
        self.created = []
        self.terminated = []
        self.create_calls = 0
        self._next_id = 100

    def list_pods(self):
        return [dict(p) for p in self.pods]

    def find_pods_by_name(self, name):
        return [dict(p) for p in self.pods if p.get("name") == name]

    def find_pods_by_name_prefix(self, prefix):
        return [dict(p) for p in self.pods if (p.get("name") or "").startswith(prefix)]

    def create_pod(self, name, gpu_type_id, network_volume_id, image_name, container_disk_gb=20):
        self.create_calls += 1
        behavior = self.create_script.pop(0) if self.create_script else "ok"
        if behavior == "uncertain_but_created":
            # 服务端已建 pod，但响应丢失（plan 0A.7 ④）
            self._add(name)
            raise wb.CreateUncertain("empty response")
        if behavior == "uncertain":
            raise wb.CreateUncertain("empty response")
        if behavior == "no_stock":
            return None
        pod = self._add(name)
        return {"id": pod["id"], "name": name}

    def _add(self, name):
        self._next_id += 1
        pod = {
            "id": "pod%d" % self._next_id,
            "name": name,
            "desiredStatus": "RUNNING",
            "costPerHr": 0.77,
            "createdAt": float(self._next_id),
        }
        self.pods.append(pod)
        self.created.append(pod["id"])
        return pod

    def terminate_pod(self, pod_id):
        self.terminated.append(pod_id)
        if self.terminate_ok:
            self.pods = [p for p in self.pods if p.get("id") != pod_id]
        return self.terminate_ok


def read_events(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def event_names(path):
    return [e["event"] for e in read_events(path)]


# =============================================================================
# [0A.1] runner 生命周期契约：--orchestrated 跳过 ensure_auto_stop + finally-stop
# =============================================================================
def run_runner_main(argv, patches):
    """执行 wan22_generate.py 的 `__main__` 块，globals 用打过桩的模块字典。

    不能用 subprocess（会真的发网络请求），也不能 runpy（模块自身定义会盖掉注入）。
    这里把 main 块源码单独 compile+exec，从而对 ensure_auto_stop/stop_pod 等做
    函数级 monkeypatch —— 正是 plan 0A.1 verify 要求的验证方式。
    """
    with open(wg.__file__, "r", encoding="utf-8") as f:
        src = f.read()
    head, sep, tail = src.partition('if __name__ == "__main__":')
    assert sep, "main block marker not found"
    body = textwrap.dedent(tail)
    g = dict(wg.__dict__)
    g.update(patches)
    g["__name__"] = "__main__"
    old_argv = sys.argv
    sys.argv = ["wan22_generate.py"] + list(argv)
    code = 0
    try:
        exec(compile(body, wg.__file__, "exec"), g)
    except SystemExit as e:
        code = e.code if e.code is not None else 0
    finally:
        sys.argv = old_argv
    return code, g


class TestRunnerLifecycle(unittest.TestCase):
    """[0A.1] --orchestrated 时 ensure_auto_stop / stop_pod 均不被调用；默认路径仍调用。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wan22t-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.patcher = Patcher()
        self.addCleanup(self.patcher.undo)
        # main 块里有 `import time as _t; _t.sleep(5)`（无法经 globals 打桩）→ 直接停掉真 sleep
        self.patcher.set(time, "sleep", lambda s: None)
        self.calls = []

    def _patches(self, **extra):
        p = {
            "pod_status": lambda *a, **k: "RUNNING",
            "start_pod": lambda *a, **k: True,
            "ensure_comfyui_running": lambda *a, **k: True,
            "wait_for_comfyui_ready": lambda *a, **k: True,
            "ensure_auto_stop": lambda *a, **k: self.calls.append("ensure_auto_stop"),
            "stop_pod": lambda *a, **k: self.calls.append("stop_pod"),
            "terminate_pod": lambda *a, **k: self.calls.append("terminate_pod"),
            "generate": lambda *a, **k: {
                "filename": "wan22_j1_00001.webp",
                "elapsed": 1.0,
                "seed": 88,
            },
            "download_output": lambda *a, **k: os.path.join(self.tmp, "out.webp"),
            "_log_generation": None,
        }
        p.update(extra)
        return p

    def _base_argv(self):
        img = os.path.join(self.tmp, "ref.png")
        with open(img, "wb") as f:
            f.write(b"P" * 100)
        return ["--prompt", "x", "--image", img, "--mode", "prod", "--output-dir", self.tmp]

    def test_default_path_calls_auto_stop_and_stop_pod(self):
        code, _ = run_runner_main(self._base_argv(), self._patches())
        self.assertIn("ensure_auto_stop", self.calls)
        self.assertIn("stop_pod", self.calls)
        self.assertEqual(code, 0)

    def test_orchestrated_skips_auto_stop_and_stop_pod(self):
        code, _ = run_runner_main(
            self._base_argv() + ["--orchestrated"], self._patches()
        )
        self.assertNotIn("ensure_auto_stop", self.calls)
        self.assertNotIn("stop_pod", self.calls)
        self.assertEqual(code, 0)

    def test_keep_pod_alone_still_installs_auto_stop(self):
        # --keep-pod 只跳过 finally-stop（现行为），auto-stop 仍装 —— 证明新 flag 不改旧语义
        run_runner_main(self._base_argv() + ["--keep-pod"], self._patches())
        self.assertIn("ensure_auto_stop", self.calls)
        self.assertNotIn("stop_pod", self.calls)


class TestRunnerExitCodes(unittest.TestCase):
    """[0A.2] exit 0 当且仅当 downloaded+校验过；其余非零。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wan22t-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.patcher = Patcher()
        self.addCleanup(self.patcher.undo)
        self.patcher.set(time, "sleep", lambda s: None)
        self.img = os.path.join(self.tmp, "ref.png")
        with open(self.img, "wb") as f:
            f.write(b"P" * 100)
        self.result = os.path.join(self.tmp, "j1.ndjson")

    def _argv(self, extra=()):
        return [
            "--prompt",
            "x",
            "--image",
            self.img,
            "--mode",
            "prod",
            "--output-dir",
            self.tmp,
            "--orchestrated",
            "--result-json",
            self.result,
            "--job-id",
            "j1",
        ] + list(extra)

    def _patches(self, generate, download):
        return {
            "pod_status": lambda *a, **k: "RUNNING",
            "ensure_comfyui_running": lambda *a, **k: True,
            "wait_for_comfyui_ready": lambda *a, **k: True,
            "ensure_auto_stop": lambda *a, **k: None,
            "stop_pod": lambda *a, **k: None,
            "generate": generate,
            "download_output": download,
            "_log_generation": None,
        }

    def test_exit_zero_only_when_downloaded(self):
        code, _ = run_runner_main(
            self._argv(),
            self._patches(
                lambda *a, **k: {"filename": "f.webp", "elapsed": 1, "seed": 1},
                lambda *a, **k: os.path.join(self.tmp, "f.webp"),
            ),
        )
        self.assertEqual(code, 0)

    def test_exit_nonzero_when_download_fails(self):
        code, _ = run_runner_main(
            self._argv(),
            self._patches(
                lambda *a, **k: {"filename": "f.webp", "elapsed": 1, "seed": 1},
                lambda *a, **k: None,
            ),
        )
        self.assertNotEqual(code, 0)

    def test_exit_nonzero_when_generate_returns_none(self):
        code, _ = run_runner_main(
            self._argv(), self._patches(lambda *a, **k: None, lambda *a, **k: None)
        )
        self.assertNotEqual(code, 0)

    def test_exit_nonzero_and_no_traceback_on_exception(self):
        def boom(*a, **k):
            raise RuntimeError("upload exploded")

        code, _ = run_runner_main(
            self._argv(), self._patches(boom, lambda *a, **k: None)
        )
        self.assertEqual(code, 1)

    def test_default_path_exit_code_unchanged_without_result_json(self):
        # 不带 --result-json 时不引入新的 sys.exit（generate 返回 None 也自然结束）
        argv = [
            "--prompt",
            "x",
            "--image",
            self.img,
            "--mode",
            "prod",
            "--output-dir",
            self.tmp,
            "--keep-pod",
        ]
        code, _ = run_runner_main(
            argv, self._patches(lambda *a, **k: None, lambda *a, **k: None)
        )
        self.assertEqual(code, 0)


# =============================================================================
# [0A.2] NDJSON 事件序列 / flush-fsync 时点 / 四路径
# =============================================================================
class TestRunnerEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wan22t-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.patcher = Patcher()
        self.addCleanup(self.patcher.undo)
        self.http = FakeHTTP()
        self.faketime = FakeTime()
        self.osproxy = OsProxy(os)
        self.patcher.set(wg, "requests", self.http)
        self.patcher.set(wg, "time", self.faketime)
        self.patcher.set(wg, "os", self.osproxy)
        self.patcher.set(wg, "POD_ID", "fakepod0")
        self.img = os.path.join(self.tmp, "ref.png")
        with open(self.img, "wb") as f:
            f.write(b"P" * 100)
        self.result = os.path.join(self.tmp, "j1.ndjson")
        self.em = wg.ResultEmitter(self.result, "j1", "fakepod0")

    def _gen(self, **kw):
        params = dict(
            prompt="p",
            image_path=self.img,
            mode="prod",
            seed=88,
            filename_prefix="wan22_j1",
            emitter=self.em,
        )
        params.update(kw)
        return wg.generate(**params)

    def _route_upload_ok(self):
        self.http.route("POST", "/upload/image", FakeResponse(json_data={"name": "ref.png"}))

    def test_happy_path_event_sequence_and_fsync(self):
        self._route_upload_ok()
        seen_before_post = {}

        def post_prompt(url, kw):
            # POST 前 submitting 必须已经落盘（fsync 过）——plan 0A.2 时点要求
            seen_before_post["names"] = event_names(self.result)
            seen_before_post["fsync"] = self.osproxy.fsync_calls
            return FakeResponse(json_data={"prompt_id": "p1"})

        self.http.route("POST", "/prompt", post_prompt)
        self.http.route(
            "GET",
            "/history/p1",
            FakeResponse(
                json_data={
                    "p1": {
                        "status": {"completed": True},
                        "outputs": {"13": {"images": [{"filename": "wan22_j1_00001.webp"}]}},
                    }
                }
            ),
        )
        res = self._gen()
        self.assertEqual(res["filename"], "wan22_j1_00001.webp")
        self.assertEqual(seen_before_post["names"], ["submitting"])
        self.assertGreaterEqual(seen_before_post["fsync"], 1)
        self.assertEqual(event_names(self.result), ["submitting", "submitted", "remote_completed"])
        evs = read_events(self.result)
        self.assertEqual(evs[0]["output_prefix"], "wan22_j1")
        self.assertEqual(evs[1]["prompt_id"], "p1")
        self.assertEqual(evs[2]["remote_filename"], "wan22_j1_00001.webp")
        # submitted 落盘（fsync）后才进入轮询
        self.assertGreaterEqual(self.osproxy.fsync_calls, 3)

        # 下载路径：downloaded{file,sha256}
        blob = b"D" * 4096
        self.http.route("GET", "/view", FakeResponse(status_code=200, content=blob))
        path = wg.download_output("wan22_j1_00001.webp", self.tmp, emitter=self.em)
        self.assertTrue(os.path.exists(path))
        last = read_events(self.result)[-1]
        self.assertEqual(last["event"], "downloaded")
        self.assertEqual(last["sha256"], hashlib.sha256(blob).hexdigest())

    def test_upload_failure_path(self):
        self.http.route(
            "POST", "/upload/image", lambda u, k: requests.exceptions.ConnectionError("net")
        )
        with self.assertRaises(RuntimeError):
            self._gen()
        evs = read_events(self.result)
        self.assertEqual([e["event"] for e in evs], ["failed"])
        self.assertEqual(evs[0]["class"], "network")
        self.assertEqual(evs[0]["stage"], "upload")

    def test_node_error_path_is_business_submit_rejected(self):
        self._route_upload_ok()
        self.http.route(
            "POST", "/prompt", FakeResponse(json_data={"node_errors": {"8": "bad input"}})
        )
        self.assertIsNone(self._gen())
        evs = read_events(self.result)
        self.assertEqual([e["event"] for e in evs], ["submitting", "failed"])
        self.assertEqual(evs[1]["class"], "business")
        self.assertEqual(evs[1]["stage"], "submit_rejected")

    def test_download_failure_path(self):
        self.http.route("GET", "/view", FakeResponse(status_code=500, content=b""))
        self.assertIsNone(wg.download_output("f.webp", self.tmp, emitter=self.em))
        evs = read_events(self.result)
        self.assertEqual(evs[-1]["class"], "network")
        self.assertEqual(evs[-1]["stage"], "download")

    def test_5xx_submit_is_uncertain_not_rejected(self):
        self._route_upload_ok()
        self.http.route("POST", "/prompt", FakeResponse(status_code=502, text="bad gateway"))
        with self.assertRaises(wg.SubmitUncertain):
            self._gen()
        evs = read_events(self.result)
        self.assertEqual(evs[-1]["class"], "network")
        self.assertEqual(evs[-1]["stage"], "submit_uncertain")

    def test_unparseable_submit_response_is_uncertain(self):
        self._route_upload_ok()
        self.http.route("POST", "/prompt", FakeResponse(status_code=200, json_data=None))
        with self.assertRaises(ValueError):
            self._gen()
        self.assertEqual(read_events(self.result)[-1]["stage"], "submit_uncertain")

    def test_poll_network_error_is_network_poll(self):
        self._route_upload_ok()
        self.http.route("POST", "/prompt", FakeResponse(json_data={"prompt_id": "p1"}))
        self.http.route(
            "GET", "/history/p1", lambda u, k: requests.exceptions.ConnectionError("reset")
        )
        with self.assertRaises(requests.exceptions.ConnectionError):
            self._gen()
        evs = read_events(self.result)
        self.assertEqual([e["event"] for e in evs], ["submitting", "submitted", "failed"])
        self.assertEqual(evs[-1]["stage"], "poll")
        self.assertEqual(evs[-1]["class"], "network")

    def test_remote_status_error_is_business_remote(self):
        self._route_upload_ok()
        self.http.route("POST", "/prompt", FakeResponse(json_data={"prompt_id": "p1"}))
        self.http.route(
            "GET",
            "/history/p1",
            FakeResponse(
                json_data={
                    "p1": {
                        "status": {
                            "status_str": "error",
                            "messages": [["execution_error", {"exception_message": "OOM"}]],
                        }
                    }
                }
            ),
        )
        self.assertIsNone(self._gen())
        evs = read_events(self.result)
        self.assertEqual(evs[-1]["class"], "business")
        self.assertEqual(evs[-1]["stage"], "remote")

    def test_polling_timeout_is_network_not_business(self):
        self._route_upload_ok()
        self.http.route("POST", "/prompt", FakeResponse(json_data={"prompt_id": "p1"}))
        self.http.route("GET", "/history/p1", FakeResponse(json_data={}))
        self.patcher.set(wg.os, "environ", dict(os.environ, POLL_ROUNDS="2"))
        self.assertIsNone(self._gen())
        evs = read_events(self.result)
        self.assertEqual(evs[-1]["class"], "network")
        self.assertEqual(evs[-1]["stage"], "poll")

    def test_emitter_disabled_is_noop(self):
        """未传 --result-json 时零行为变化：不建文件、不 fsync。"""
        null_path = os.path.join(self.tmp, "should_not_exist.ndjson")
        em = wg.ResultEmitter(None, "j1", "pod")
        self.assertIsNone(em.emit("submitting", {}, fsync=True))
        self.assertIsNone(em.emit_failed("network", "upload", "x"))
        self.assertFalse(os.path.exists(null_path))
        self.assertEqual(self.osproxy.fsync_calls, 0)

    def test_filename_prefix_override_and_default(self):
        wf = wg.build_workflow("p", "n", "img.png", "prod", 1, 1.0, filename_prefix="wan22_j7")
        self.assertEqual(wf["13"]["inputs"]["filename_prefix"], "wan22_j7")
        wf2 = wg.build_workflow("p", "n", "img.png", "prod", 1, 1.0)
        self.assertEqual(wf2["13"]["inputs"]["filename_prefix"], "wan22_prod")


# =============================================================================
# 编排器测试基类
# =============================================================================
class OrchBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wan22b-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.slept = []
        self.now = [1000.0]

    def sleeper(self, sec):
        self.slept.append(sec)

    def clock(self):
        return self.now[0]

    def make_job(self, job_id="j1", **kw):
        img = os.path.join(self.tmp, "%s_ref.png" % job_id)
        with open(img, "wb") as f:
            f.write(b"R" * 64 + job_id.encode())
        spec = {"job_id": job_id, "prompt": "p", "image": img, "mode": "prod", "seed": 88}
        spec.update(kw)
        return spec

    def make_orch(self, jobs, comfy=None, launcher=None, runpod=None, pods=("pod0",), **kw):
        comfy = comfy if comfy is not None else FakeComfy()
        orch = wb.BatchOrchestrator(
            jobs,
            os.path.join(self.tmp, "work"),
            list(pods),
            batch_id=kw.pop("batch_id", "testbatch"),
            runpod=runpod if runpod is not None else FakeRunPod(),
            comfy_factory=lambda pid: comfy,
            launcher=launcher if launcher is not None else FakeLauncher({}),
            clock=self.clock,
            sleeper=self.sleeper,
            tail_interval=0,
            **kw
        )
        self.addCleanup(orch.close)
        self.comfy = comfy
        return orch

    def force_state(self, orch, job_id, state, **fields):
        """构造 resume 场景：绕过转移校验直接写状态（仅测试用）。"""
        orch.store.update_job(job_id, state=state, **fields)


# =============================================================================
# [0A.3] 并发产物隔离
# =============================================================================
class TestArtifactIsolation(OrchBase):
    def test_same_basename_two_jobs_get_distinct_refs_and_shas(self):
        d1 = os.path.join(self.tmp, "a")
        d2 = os.path.join(self.tmp, "b")
        os.makedirs(d1)
        os.makedirs(d2)
        p1 = os.path.join(d1, "after_red.png")
        p2 = os.path.join(d2, "after_red.png")
        with open(p1, "wb") as f:
            f.write(b"AAAA" * 64)
        with open(p2, "wb") as f:
            f.write(b"BBBB" * 64)
        j1 = {"job_id": "j1", "prompt": "p", "image": p1}
        j2 = {"job_id": "j2", "prompt": "p", "image": p2}
        blob1 = b"one" * 2000
        blob2 = b"two" * 2000
        launcher = FakeLauncher(
            {
                "j1": [
                    {
                        "events": [
                            {"event": "submitting", "output_prefix": "wan22_j1"},
                            {"event": "submitted", "prompt_id": "p1", "output_prefix": "wan22_j1"},
                            {
                                "event": "remote_completed",
                                "prompt_id": "p1",
                                "remote_filename": "wan22_j1_00001.webp",
                                "output_prefix": "wan22_j1",
                            },
                        ],
                        "artifact": ("wan22_j1_00001.webp", blob1),
                        "exit": 0,
                    }
                ],
                "j2": [
                    {
                        "events": [
                            {"event": "submitting", "output_prefix": "wan22_j2"},
                            {"event": "submitted", "prompt_id": "p2", "output_prefix": "wan22_j2"},
                            {
                                "event": "remote_completed",
                                "prompt_id": "p2",
                                "remote_filename": "wan22_j2_00001.webp",
                                "output_prefix": "wan22_j2",
                            },
                        ],
                        "artifact": ("wan22_j2_00001.webp", blob2),
                        "exit": 0,
                    }
                ],
            }
        )
        orch = self.make_orch([j1, j2], launcher=launcher)
        results = orch.run()
        self.assertEqual(results, {"j1": "done", "j2": "done"})
        # ref 唯一名互不覆盖
        refs = [os.path.basename(l["ref"]) for l in launcher.launches]
        self.assertEqual(len(set(refs)), 2)
        for r in refs:
            self.assertTrue(r.startswith("job_"))
        # 两产物 sha 不同且各绑对 job
        s1 = orch.store.job("j1")
        s2 = orch.store.job("j2")
        self.assertNotEqual(s1["sha256"], s2["sha256"])
        self.assertEqual(s1["sha256"], hashlib.sha256(blob1).hexdigest())
        self.assertEqual(s2["sha256"], hashlib.sha256(blob2).hexdigest())
        self.assertEqual(s1["output_prefix"], "wan22_j1")
        self.assertEqual(s2["output_prefix"], "wan22_j2")
        for jid in ("j1", "j2"):
            with open(orch.store.job(jid)["sidecar"], "r", encoding="utf-8") as f:
                side = json.load(f)
            self.assertEqual(side["job_id"], jid)
            self.assertEqual(side["sha256"], orch.store.job(jid)["sha256"])


# =============================================================================
# [0A.4] 状态机转移表 + 单写者并发无丢转移
# =============================================================================
class TestStateMachine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wan22s-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "manifest.json")

    def store(self, state="pending"):
        s = wb.ManifestStore(self.path, {"jobs": {"j1": {"job_id": "j1", "state": state, "attempts": 0}}})
        return s

    def test_transition_table_full_coverage(self):
        for src in wb.JOB_STATES:
            for dst in wb.JOB_STATES:
                s = self.store(src)
                allowed = dst in wb.ALLOWED_TRANSITIONS[src]
                if allowed:
                    s.transition("j1", dst)
                    self.assertEqual(s.state("j1"), dst, "%s->%s" % (src, dst))
                else:
                    with self.assertRaises(wb.IllegalTransition, msg="%s->%s" % (src, dst)):
                        s.transition("j1", dst)
                    self.assertEqual(s.state("j1"), src)

    def test_table_covers_plan_happy_path(self):
        s = self.store("pending")
        for nxt in ("submitting", "submitted", "remote_completed", "downloaded", "done"):
            s.transition("j1", nxt)
        self.assertEqual(s.state("j1"), "done")
        self.assertEqual(len(s.job("j1")["history"]), 5)

    def test_network_error_does_not_degrade_state(self):
        for st, stage in (
            ("pending", "upload"),
            ("submitting", "submit_uncertain"),
            ("submitted", "poll"),
            ("remote_completed", "download"),
        ):
            s = self.store(st)
            s.record_network_error("j1", stage, "boom")
            self.assertEqual(s.state("j1"), st)
            self.assertEqual(s.job("j1")["attempts"], 0)
            self.assertEqual(s.job("j1")["net_retries"], 1)
            self.assertEqual(s.job("j1")["last_error_class"], "network")

    def test_business_error_increments_attempts(self):
        s = self.store("submitted")
        s.record_business_error("j1", "remote", "node error")
        self.assertEqual(s.state("j1"), "business_failed")
        self.assertEqual(s.job("j1")["attempts"], 1)

    def test_single_writer_no_lost_updates_under_concurrency(self):
        s = wb.ManifestStore(self.path, {"jobs": {}}).start()
        self.addCleanup(s.stop)
        s.ensure_job("j1", {})
        n_threads, per_thread = 8, 25

        def bump(tid):
            for i in range(per_thread):
                def m(doc, tid=tid, i=i):
                    j = doc["jobs"]["j1"]
                    j["counter"] = j.get("counter", 0) + 1
                    j.setdefault("marks", []).append("%d-%d" % (tid, i))
                    return j
                s.apply(m)

        threads = [threading.Thread(target=bump, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        job = s.job("j1")
        self.assertEqual(job["counter"], n_threads * per_thread)
        self.assertEqual(len(set(job["marks"])), n_threads * per_thread)
        # 落盘内容与内存一致（原子写，无半截文件）
        with open(self.path, "r", encoding="utf-8") as f:
            disk = json.load(f)
        self.assertEqual(disk["jobs"]["j1"]["counter"], n_threads * per_thread)

    def test_concurrent_transitions_serialize_without_lost_moves(self):
        s = wb.ManifestStore(self.path, {"jobs": {}}).start()
        self.addCleanup(s.stop)
        for i in range(20):
            s.ensure_job("j%d" % i, {})
        errors = []

        def advance(jid):
            try:
                for nxt in ("submitting", "submitted", "remote_completed", "downloaded", "done"):
                    s.transition(jid, nxt)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=advance, args=("j%d" % i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        for i in range(20):
            j = s.job("j%d" % i)
            self.assertEqual(j["state"], "done")
            self.assertEqual([h["to"] for h in j["history"]],
                             ["submitting", "submitted", "remote_completed", "downloaded", "done"])

    def test_atomic_write_uses_replace(self):
        s = self.store("pending")
        s.transition("j1", "submitting")
        self.assertTrue(os.path.exists(self.path))
        self.assertFalse([p for p in os.listdir(self.tmp) if ".tmp." in p])


# =============================================================================
# [0A.5] resume 状态转移 (a)-(l)：全部断言无并行重复 GPU job
# =============================================================================
class TestResumeTransitions(OrchBase):
    def _events(self, *evs):
        return list(evs)

    def test_a_submitted_then_network_drop_resumes_same_prompt(self):
        job = self.make_job()
        comfy = FakeComfy(states={"p1": [("completed", {"filename": "wan22_j1_1.webp"})]})
        launcher = FakeLauncher({})
        orch = self.make_orch([job], comfy=comfy, launcher=launcher)
        self.force_state(orch, "j1", "submitted", prompt_id="p1", pod_id="pod0")
        self.assertEqual(orch.process_job("j1"), "done")
        self.assertEqual(launcher.post_count, 0)  # 不重提
        self.assertEqual(len(launcher.launches), 0)
        self.assertEqual(orch.store.job("j1")["prompt_id"], "p1")

    def test_b_download_failure_only_retries_download(self):
        job = self.make_job()
        comfy = FakeComfy(download_errors=2)
        launcher = FakeLauncher({})
        orch = self.make_orch([job], comfy=comfy, launcher=launcher)
        self.force_state(
            orch, "j1", "remote_completed", pod_id="pod0", remote_filename="wan22_j1_1.webp"
        )
        self.assertEqual(orch.process_job("j1"), "done")
        self.assertEqual(launcher.post_count, 0)
        downloads = [c for c in comfy.calls if isinstance(c, tuple) and c[0] == "download"]
        self.assertEqual(len(downloads), 3)  # 2 次失败 + 1 次成功，只重下
        self.assertEqual(orch.store.job("j1")["net_retries"], 2)
        self.assertEqual(orch.store.job("j1")["attempts"], 0)

    def test_c_confirmed_node_error_increments_business_attempt(self):
        job = self.make_job()
        launcher = FakeLauncher(
            {
                "j1": [
                    {
                        "events": [
                            {"event": "submitting", "output_prefix": "wan22_j1"},
                            {"event": "submitted", "prompt_id": "p1"},
                            {
                                "event": "failed",
                                "class": "business",
                                "stage": "remote",
                                "detail": "node blew up",
                            },
                        ],
                        "exit": 1,
                    }
                ]
            }
        )
        orch = self.make_orch([job], launcher=launcher)
        orch._launch("j1")
        j = orch.store.job("j1")
        self.assertEqual(j["state"], "business_failed")
        self.assertEqual(j["attempts"], 1)
        self.assertEqual(launcher.post_count, 1)

    def test_d_polling_timeout_keeps_submitted(self):
        job = self.make_job()
        launcher = FakeLauncher(
            {
                "j1": [
                    {
                        "events": [
                            {"event": "submitting", "output_prefix": "wan22_j1"},
                            {"event": "submitted", "prompt_id": "p1", "output_prefix": "wan22_j1"},
                            {
                                "event": "failed",
                                "class": "network",
                                "stage": "poll",
                                "detail": "polling window exhausted",
                            },
                        ],
                        "exit": 1,
                    }
                ]
            }
        )
        orch = self.make_orch([job], launcher=launcher)
        orch._launch("j1")
        j = orch.store.job("j1")
        self.assertEqual(j["state"], "submitted")  # 不得标 terminal failed
        self.assertEqual(j["attempts"], 0)
        self.assertEqual(j["net_retries"], 1)
        self.assertEqual(launcher.post_count, 1)  # 无并行重复 GPU job
        # batch 收到 submitted 立即经单写者持久化进 manifest（落盘可见）
        with open(os.path.join(orch.workdir, "manifest.json"), "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["jobs"]["j1"]["state"], "submitted")

    def test_e_remote_missing_but_nv_has_artifact(self):
        job = self.make_job()
        comfy = FakeComfy(
            states={"p1": [("missing", {})]},
            output_by_prefix={"wan22_j1": "wan22_j1_00001.webp"},
        )
        launcher = FakeLauncher({})
        orch = self.make_orch([job], comfy=comfy, launcher=launcher)
        self.force_state(orch, "j1", "submitted", prompt_id="p1", pod_id="pod0")
        self.assertEqual(orch.process_job("j1"), "done")
        self.assertEqual(launcher.post_count, 0)  # 恢复产物不重提

    def test_f_remote_and_nv_both_absent_allows_resubmit(self):
        job = self.make_job()
        comfy = FakeComfy(states={"p1": [("missing", {})]})
        blob = b"z" * 3000
        launcher = FakeLauncher(
            {
                "j1": [
                    {
                        "events": [
                            {"event": "submitting", "output_prefix": "wan22_j1"},
                            {"event": "submitted", "prompt_id": "p9"},
                            {
                                "event": "remote_completed",
                                "prompt_id": "p9",
                                "remote_filename": "wan22_j1_2.webp",
                            },
                        ],
                        "artifact": ("wan22_j1_2.webp", blob),
                        "exit": 0,
                    }
                ]
            }
        )
        orch = self.make_orch([job], comfy=comfy, launcher=launcher)
        self.force_state(orch, "j1", "submitted", prompt_id="p1", pod_id="pod0")
        self.assertEqual(orch.process_job("j1"), "done")
        self.assertEqual(launcher.post_count, 1)  # 双确认后才允许唯一一次重提
        self.assertIn(wb.CONFIRM_INTERVAL_SEC, self.slept)

    def test_g_stuck_submitting_adopts_by_output_prefix(self):
        job = self.make_job()
        comfy = FakeComfy(
            prompt_by_prefix={"wan22_j1": "p1"},
            states={"p1": [("completed", {"filename": "wan22_j1_1.webp"})]},
        )
        launcher = FakeLauncher({})
        orch = self.make_orch([job], comfy=comfy, launcher=launcher)
        self.force_state(orch, "j1", "submitting", pod_id="pod0", output_prefix="wan22_j1")
        self.assertEqual(orch.process_job("j1"), "done")
        self.assertEqual(launcher.post_count, 0)  # 收养原任务，不并行重提
        self.assertEqual(orch.store.job("j1")["prompt_id"], "p1")

    def test_h_upload_failure_keeps_pending_and_retransmits(self):
        job = self.make_job()
        blob = b"u" * 3000
        launcher = FakeLauncher(
            {
                "j1": [
                    {
                        "events": [
                            {
                                "event": "failed",
                                "class": "network",
                                "stage": "upload",
                                "detail": "ConnectionError",
                            }
                        ],
                        "exit": 1,
                    },
                    {
                        "events": [
                            {"event": "submitting", "output_prefix": "wan22_j1"},
                            {"event": "submitted", "prompt_id": "p1"},
                            {
                                "event": "remote_completed",
                                "prompt_id": "p1",
                                "remote_filename": "wan22_j1_1.webp",
                            },
                        ],
                        "artifact": ("wan22_j1_1.webp", blob),
                        "exit": 0,
                    },
                ]
            }
        )
        orch = self.make_orch([job], launcher=launcher)
        rc = orch._launch("j1")
        self.assertEqual(rc, 1)
        self.assertEqual(orch.store.job("j1")["state"], "pending")  # 保持 pending
        self.assertEqual(launcher.post_count, 0)  # 上传阶段失败 → 远端无任务
        self.assertEqual(orch.process_job("j1"), "done")
        self.assertEqual(launcher.post_count, 1)  # 无重复 GPU job

    def test_i_proxy_502_after_acceptance_adopts_and_posts_once(self):
        job = self.make_job()
        comfy = FakeComfy(
            prompt_by_prefix={"wan22_j1": "p1"},
            states={"p1": [("completed", {"filename": "wan22_j1_1.webp"})]},
        )
        launcher = FakeLauncher(
            {
                "j1": [
                    {
                        "events": [
                            {"event": "submitting", "output_prefix": "wan22_j1"},
                            {
                                "event": "failed",
                                "class": "network",
                                "stage": "submit_uncertain",
                                "detail": "HTTP 502",
                            },
                        ],
                        "exit": 1,
                    }
                ]
            }
        )
        orch = self.make_orch([job], comfy=comfy, launcher=launcher)
        self.assertEqual(orch.process_job("j1"), "done")
        self.assertEqual(launcher.post_count, 1)  # POST 调用总数仍为 1
        self.assertEqual(orch.store.job("j1")["prompt_id"], "p1")

    def test_j_500_with_empty_remote_requires_double_confirm_before_second_post(self):
        job = self.make_job()
        comfy = FakeComfy()  # 远端与 NV 都查不到
        blob = b"v" * 3000
        uncertain = {
            "events": [
                {"event": "submitting", "output_prefix": "wan22_j1"},
                {
                    "event": "failed",
                    "class": "network",
                    "stage": "submit_uncertain",
                    "detail": "HTTP 500",
                },
            ],
            "exit": 1,
        }
        good = {
            "events": [
                {"event": "submitting", "output_prefix": "wan22_j1"},
                {"event": "submitted", "prompt_id": "p2"},
                {
                    "event": "remote_completed",
                    "prompt_id": "p2",
                    "remote_filename": "wan22_j1_2.webp",
                },
            ],
            "artifact": ("wan22_j1_2.webp", blob),
            "exit": 0,
        }
        launcher = FakeLauncher({"j1": [uncertain, good]})
        orch = self.make_orch([job], comfy=comfy, launcher=launcher)
        self.assertEqual(orch.process_job("j1"), "done")
        self.assertEqual(launcher.post_count, 2)  # 第二次 POST 只在双确认之后
        self.assertIn(wb.CONFIRM_INTERVAL_SEC, self.slept)
        confirms = [c for c in comfy.calls if isinstance(c, tuple) and c[0] == "find_prompt"]
        self.assertGreaterEqual(len(confirms), wb.CONFIRM_ROUNDS)

    def test_k_node_errors_without_prompt_id_is_immediate_business_failure(self):
        job = self.make_job()
        comfy = FakeComfy()
        launcher = FakeLauncher(
            {
                "j1": [
                    {
                        "events": [
                            {"event": "submitting", "output_prefix": "wan22_j1"},
                            {
                                "event": "failed",
                                "class": "business",
                                "stage": "submit_rejected",
                                "detail": "node_errors",
                            },
                        ],
                        "exit": 1,
                    }
                ]
            }
        )
        orch = self.make_orch([job], comfy=comfy, launcher=launcher)
        orch._launch("j1")
        j = orch.store.job("j1")
        self.assertEqual(j["state"], "business_failed")
        self.assertEqual(j["attempts"], 1)
        # 不走确认窗口
        self.assertNotIn(wb.CONFIRM_INTERVAL_SEC, self.slept)
        self.assertEqual([c for c in comfy.calls if isinstance(c, tuple)], [])
        self.assertEqual(launcher.post_count, 1)  # 无并行重复 GPU job

    def test_l_download_network_failure_keeps_remote_completed(self):
        job = self.make_job()
        comfy = FakeComfy(download_errors=1)
        launcher = FakeLauncher({})
        orch = self.make_orch([job], comfy=comfy, launcher=launcher)
        self.force_state(
            orch, "j1", "remote_completed", pod_id="pod0", remote_filename="wan22_j1_1.webp"
        )
        orch._download("j1")
        self.assertEqual(orch.store.job("j1")["state"], "remote_completed")  # 状态不降级
        self.assertEqual(orch.store.job("j1")["last_error_stage"], "download")
        self.assertEqual(launcher.post_count, 0)
        orch._download("j1")
        self.assertEqual(orch.store.job("j1")["state"], "downloaded")

    def test_local_master_with_sidecar_counts_as_done(self):
        """[0A.5 ①] 本地 masters + sidecar sha 校验通过 → done。"""
        job = self.make_job()
        orch = self.make_orch([job])
        blob = b"m" * 2048
        path = os.path.join(orch.masters_dir, "wan22_j1_1.webp")
        with open(path, "wb") as f:
            f.write(blob)
        sidecar = path + ".batch.json"
        with open(sidecar, "w", encoding="utf-8") as f:
            json.dump({"sha256": hashlib.sha256(blob).hexdigest()}, f)
        self.force_state(orch, "j1", "downloaded", local_file=path, sidecar=sidecar)
        self.assertTrue(orch.local_master_ok("j1"))
        self.assertEqual(orch.process_job("j1"), "done")


# =============================================================================
# [0A.6] owned pod 决策表 + max-spend
# =============================================================================
class TestPodDecisionTable(unittest.TestCase):
    def test_row_job_success_no_remaining_terminates(self):
        self.assertEqual(wb.pod_action("owned", "job_success", remaining_jobs=0), "terminate")

    def test_row_job_success_with_remaining_keeps(self):
        self.assertEqual(wb.pod_action("owned", "job_success", remaining_jobs=2), "keep")

    def test_row_business_failed_terminates(self):
        self.assertEqual(wb.pod_action("owned", "business_failed"), "terminate")

    def test_row_network_backoff_without_running_task_terminates(self):
        self.assertEqual(
            wb.pod_action("owned", "network_backoff", has_running_task=False), "terminate"
        )

    def test_row_network_backoff_with_running_task_waits_then_terminates(self):
        self.assertEqual(
            wb.pod_action("owned", "network_backoff", has_running_task=True, wait_elapsed_sec=60),
            "wait",
        )
        self.assertEqual(
            wb.pod_action(
                "owned",
                "network_backoff",
                has_running_task=True,
                wait_elapsed_sec=wb.NETWORK_WAIT_WINDOW_SEC,
            ),
            "terminate",
        )

    def test_row_signal_terminates(self):
        self.assertEqual(wb.pod_action("owned", "signal"), "terminate")

    def test_borrowed_never_touched_in_any_situation(self):
        for situation in ("job_success", "business_failed", "network_backoff", "signal"):
            self.assertEqual(wb.pod_action("borrowed", situation), "none")


class TestPodLifecycleIntegration(OrchBase):
    def test_owned_pod_terminated_after_success_borrowed_untouched(self):
        job = self.make_job()
        blob = b"k" * 3000
        launcher = FakeLauncher(
            {
                "j1": [
                    {
                        "events": [
                            {"event": "submitting", "output_prefix": "wan22_j1"},
                            {"event": "submitted", "prompt_id": "p1"},
                            {
                                "event": "remote_completed",
                                "prompt_id": "p1",
                                "remote_filename": "wan22_j1_1.webp",
                            },
                        ],
                        "artifact": ("wan22_j1_1.webp", blob),
                        "exit": 0,
                    }
                ]
            }
        )
        rp = FakeRunPod()
        orch = self.make_orch([job], launcher=launcher, runpod=rp)
        orch.run()
        self.assertEqual(rp.terminated, ["pod0"])  # 下载校验过即刻 terminate

        # borrowed 一律不动
        rp2 = FakeRunPod()
        self.tmp2 = tempfile.mkdtemp(prefix="wan22b2-")
        self.addCleanup(shutil.rmtree, self.tmp2, True)
        orch2 = wb.BatchOrchestrator(
            [self.make_job("j2")],
            os.path.join(self.tmp2, "work"),
            ["podB:borrowed"],
            batch_id="tb2",
            runpod=rp2,
            comfy_factory=lambda pid: FakeComfy(),
            launcher=FakeLauncher(
                {
                    "j2": [
                        {
                            "events": [
                                {"event": "submitting", "output_prefix": "wan22_j2"},
                                {"event": "submitted", "prompt_id": "p2"},
                                {
                                    "event": "remote_completed",
                                    "prompt_id": "p2",
                                    "remote_filename": "wan22_j2_1.webp",
                                },
                            ],
                            "artifact": ("wan22_j2_1.webp", b"q" * 3000),
                            "exit": 0,
                        }
                    ]
                }
            ),
            clock=self.clock,
            sleeper=self.sleeper,
            tail_interval=0,
        )
        self.addCleanup(orch2.close)
        orch2.run()
        self.assertEqual(rp2.terminated, [])

    def test_business_exhausted_dumps_logs_then_terminates(self):
        job = self.make_job()
        comfy = FakeComfy()
        orch = self.make_orch([job], comfy=comfy)
        orch.store.update_job("j1", attempts=wb.MAX_BUSINESS_ATTEMPTS, state="submitted")
        action = orch._pod_business_decision("j1", "pod0")
        self.assertEqual(action, "terminate")
        log = orch.store.job("j1")["comfy_log"]
        self.assertTrue(os.path.exists(log))
        with open(log, "r", encoding="utf-8") as f:
            self.assertIn("comfyui log tail", f.read())

    def test_max_spend_triggers_cleanup_and_nonzero_exit(self):
        job = self.make_job()
        rp = FakeRunPod()
        orch = self.make_orch([job], runpod=rp, max_spend=3.0, rate=0.77)
        # owned pod0 已跑 4 小时 → $3.08 ≥ $3 硬顶
        self.now[0] += 4 * 3600
        with self.assertRaises(wb.SpendLimitExceeded):
            orch.process_job("j1")
        self.assertEqual(rp.terminated, ["pod0"])
        self.assertTrue(orch.ledger.snapshot()["alerts"])

    def test_spend_counts_owned_only(self):
        job = self.make_job()
        orch = self.make_orch([job], pods=("pod0", "podB:borrowed"))
        self.now[0] += 3600
        self.assertAlmostEqual(orch.pod_mgr.spend_usd(), 0.77, places=4)

    def test_network_backoff_without_confirmed_task_terminates_immediately(self):
        """决策表「网络中断退避」行：pod 无已确认运行中任务 → 立即 terminate。"""
        job = self.make_job()
        rp = FakeRunPod()
        orch = self.make_orch([job], comfy=FakeComfy(alive=False), runpod=rp, max_rounds=1)
        self.force_state(orch, "j1", "submitting", pod_id="pod0", output_prefix="wan22_j1")
        orch.process_job("j1")
        self.assertEqual(rp.terminated, ["pod0"])
        self.assertEqual(orch.store.job("j1")["state"], "submitting")  # 状态不降级

    def test_network_backoff_with_running_task_waits_until_window(self):
        """有运行中任务 → 等待窗口 ≤45min，超时才 terminate。"""
        job = self.make_job()
        rp = FakeRunPod()
        orch = self.make_orch([job], comfy=FakeComfy(alive=False), runpod=rp, max_rounds=1)
        self.force_state(
            orch, "j1", "submitted", pod_id="pod0", prompt_id="p1",
            submitting_at=self.clock(),
        )
        orch.process_job("j1")
        self.assertEqual(rp.terminated, [])  # 窗口内等待，不动 pod
        self.now[0] += wb.NETWORK_WAIT_WINDOW_SEC + 1
        orch.process_job("j1")
        self.assertEqual(rp.terminated, ["pod0"])  # 超窗口才 terminate

    def test_signal_drains_downloads_then_terminates_all(self):
        job = self.make_job()
        comfy = FakeComfy()
        rp = FakeRunPod()
        orch = self.make_orch([job], comfy=comfy, runpod=rp)
        self.force_state(
            orch, "j1", "remote_completed", pod_id="pod0", remote_filename="wan22_j1_1.webp"
        )
        orch.handle_signal()
        self.assertEqual(orch.store.job("j1")["state"], "downloaded")  # 30s 时间盒内补下载
        self.assertEqual(rp.terminated, ["pod0"])


# =============================================================================
# [0A.7] 防孤儿 pod
# =============================================================================
class TestOrphanPrevention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wan22o-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.now = [500.0]

    def clock(self):
        return self.now[0]

    def mgr(self, runpod, batch_id="bid1", max_spend=3.0):
        ledger = wb.SingleWriterStore(
            os.path.join(self.tmp, "pods_owned.json"),
            {"batch_id": batch_id, "pods": {}, "intents": {}, "alerts": []},
        )
        return wb.PodManager(ledger, runpod, batch_id, max_spend=max_spend, clock=self.clock)

    def test_batch_id_persisted_before_any_create(self):
        rp = FakeRunPod()
        m = self.mgr(rp)
        m.record_intent("w0")
        doc = m.ledger.snapshot()
        self.assertEqual(doc["batch_id"], "bid1")
        self.assertEqual(doc["intents"]["w0"]["name"], "wan22-bid1-w0")
        self.assertEqual(rp.create_calls, 0)  # intent 先于任何 create

    def test_partial_create_success_only_cleans_created(self):
        """① 部分创建成功：只清理成功创建的那个。"""
        rp = FakeRunPod(create_script=["ok", "no_stock"])
        m = self.mgr(rp)
        p0 = m.ensure_pod("w0", "gpu", "nv", "img")
        p1 = m.ensure_pod("w1", "gpu", "nv", "img")
        self.assertIsNotNone(p0)
        self.assertIsNone(p1)
        m.cleanup_all("test")
        self.assertEqual(rp.terminated, [p0])

    def test_crash_before_ledger_is_adopted_by_name_prefix(self):
        """② 创建成功但写 ledger 前崩溃 → resume 靠名称前缀查询收养。"""
        rp = FakeRunPod(
            pods=[
                {"id": "podX", "name": "wan22-bid1-w0", "desiredStatus": "RUNNING",
                 "costPerHr": 0.77, "createdAt": 1.0},
                {"id": "podOther", "name": "someone-else", "desiredStatus": "RUNNING"},
            ]
        )
        m = self.mgr(rp)
        adopted = m.adopt_by_name_prefix()
        self.assertEqual(adopted, ["podX"])
        self.assertEqual(m.pods()["podX"]["ownership"], "owned")
        self.assertNotIn("podOther", m.pods())  # 非 owned 一律不动
        m.cleanup_all("test")
        self.assertEqual(rp.terminated, ["podX"])

    def test_cleanup_failure_leaves_pending_and_alerts(self):
        """③ 清理失败 → ledger 留 pending_cleanup + 告警，不静默。"""
        rp = FakeRunPod(create_script=["ok"], terminate_ok=False)
        m = self.mgr(rp)
        pid = m.ensure_pod("w0", "gpu", "nv", "img")
        m.cleanup_all("test")
        self.assertEqual(m.pods()[pid]["status"], "pending_cleanup")
        alerts = m.ledger.snapshot()["alerts"]
        self.assertTrue(any("pending_cleanup" in a["msg"] for a in alerts))

    def test_lost_create_response_does_not_blind_create(self):
        """④ 创建成功但响应丢失且进程不崩溃：不盲建第二个，查询收养，重复项被清理，
        全部匹配 pod 进 max-spend 与清理范围。"""
        rp = FakeRunPod(create_script=["uncertain_but_created"])
        m = self.mgr(rp)
        pid = m.ensure_pod("w0", "gpu", "nv", "img")
        self.assertEqual(rp.create_calls, 1)  # 不盲建第二个
        self.assertEqual(len(rp.created), 1)
        self.assertEqual(pid, rp.created[0])
        self.assertEqual(m.pods()[pid]["ownership"], "owned")

    def test_duplicate_pods_keep_earliest_terminate_rest_and_all_counted(self):
        rp = FakeRunPod(
            pods=[
                {"id": "podLate", "name": "wan22-bid1-w0", "createdAt": 9.0, "costPerHr": 0.77},
                {"id": "podEarly", "name": "wan22-bid1-w0", "createdAt": 2.0, "costPerHr": 0.77},
            ]
        )
        m = self.mgr(rp)
        keep, killed = m.dedupe_worker_pods("w0")
        self.assertEqual(keep, "podEarly")  # createdAt 最早者保留
        self.assertEqual(killed, ["podLate"])
        # 所有名称匹配 pod（含重复项）都进 ledger → 计入 max-spend 与清理范围
        self.assertIn("podLate", m.pods())
        self.assertIn("podEarly", m.pods())
        self.now[0] += 3600
        self.assertGreater(m.spend_usd(), 0.77)  # 重复项也计费

    def test_uncertain_then_absent_allows_one_more_create(self):
        rp = FakeRunPod(create_script=["uncertain", "ok"])
        m = self.mgr(rp)
        pid = m.ensure_pod("w0", "gpu", "nv", "img")
        self.assertEqual(rp.create_calls, 2)
        self.assertEqual(len(rp.created), 1)
        self.assertEqual(pid, rp.created[0])


# =============================================================================
# [0A.8] 长退避重试
# =============================================================================
class TestBackoff(OrchBase):
    def test_backoff_sequence_capped(self):
        self.assertEqual(
            [wb.backoff_delay(i) for i in range(6)], [60, 180, 420, 900, 900, 900]
        )

    def test_network_backoff_keeps_state_and_increments_net_retries(self):
        """假 pod 不可达：观察退避序列 + net_retries 递增 + 状态不降级 + 进程不退出。"""
        job = self.make_job()
        comfy = FakeComfy(alive=False)
        launcher = FakeLauncher({})
        orch = self.make_orch([job], comfy=comfy, launcher=launcher, max_rounds=5)
        self.force_state(orch, "j1", "submitted", prompt_id="p1", pod_id="pod0")
        state = orch.process_job("j1")  # 不抛异常 = 进程不退出
        self.assertEqual(state, "submitted")  # 状态不降级
        self.assertEqual(orch.store.job("j1")["net_retries"], 5)
        self.assertEqual(orch.store.job("j1")["attempts"], 0)  # 网络类不计 business attempt
        self.assertEqual(self.slept[:5], [60, 180, 420, 900, 900])
        self.assertEqual(launcher.post_count, 0)

    def test_business_attempts_capped_at_four(self):
        job = self.make_job()
        prog = {
            "events": [
                {"event": "submitting", "output_prefix": "wan22_j1"},
                {"event": "submitted", "prompt_id": "p1"},
                {"event": "failed", "class": "business", "stage": "remote", "detail": "boom"},
            ],
            "exit": 1,
        }
        launcher = FakeLauncher({"j1": [prog] * 6})
        orch = self.make_orch([job], launcher=launcher, max_rounds=20)
        self.assertEqual(orch.process_job("j1"), "business_failed")
        self.assertEqual(orch.store.job("j1")["attempts"], wb.MAX_BUSINESS_ATTEMPTS)
        self.assertEqual(launcher.post_count, wb.MAX_BUSINESS_ATTEMPTS)

    def test_each_stage_state_not_degraded_on_network_error(self):
        job = self.make_job()
        orch = self.make_orch([job])
        cases = [
            ("pending", {"class": "network", "stage": "upload"}, "pending"),
            ("submitting", {"class": "network", "stage": "submit_uncertain"}, "submitting"),
            ("submitted", {"class": "network", "stage": "poll"}, "submitted"),
            ("remote_completed", {"class": "network", "stage": "download"}, "remote_completed"),
        ]
        for start, ev, expect in cases:
            self.force_state(orch, "j1", start)
            payload = {"event": "failed", "detail": "x"}
            payload.update(ev)
            orch.apply_event("j1", payload)
            self.assertEqual(orch.store.job("j1")["state"], expect, "%s" % start)

    def test_upload_failure_is_the_only_downgrade_from_submitting(self):
        job = self.make_job()
        orch = self.make_orch([job])
        self.force_state(orch, "j1", "submitting")
        orch.apply_event(
            "j1", {"event": "failed", "class": "network", "stage": "upload", "detail": "x"}
        )
        self.assertEqual(orch.store.job("j1")["state"], "pending")


# =============================================================================
# CLI / job 表 / dry-run
# =============================================================================
class TestCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wan22c-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _write_jobs(self, jobs):
        p = os.path.join(self.tmp, "jobs.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(jobs, f)
        return p

    def test_load_jobs_validates_required_fields_and_uniqueness(self):
        p = self._write_jobs([{"job_id": "a", "prompt": "x", "image": "i.png"}])
        self.assertEqual(len(wb.load_jobs(p)), 1)
        p2 = self._write_jobs([{"job_id": "a", "prompt": "x"}])
        with self.assertRaises(ValueError):
            wb.load_jobs(p2)
        p3 = self._write_jobs(
            [
                {"job_id": "a", "prompt": "x", "image": "i.png"},
                {"job_id": "a", "prompt": "y", "image": "i.png"},
            ]
        )
        with self.assertRaises(ValueError):
            wb.load_jobs(p3)

    def test_dry_run_makes_no_api_calls(self):
        p = self._write_jobs(
            [
                {"job_id": "a", "prompt": "x", "image": "i.png", "mode": "prod"},
                {"job_id": "b", "prompt": "y", "image": "i.png", "mode": "prod"},
            ]
        )
        rc = wb.main(["--jobs", p, "--pods", "pod0", "--dry-run", "--workdir", self.tmp])
        self.assertEqual(rc, 0)
        # dry-run 不建 manifest/ledger（不发任何 API，也不落盘状态）
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "manifest.json")))

    def test_pod_spec_parsing(self):
        self.assertEqual(
            wb.BatchOrchestrator._parse_pod_specs(["p1", "p2:borrowed", "p3:owned"]),
            [("p1", "owned"), ("p2", "borrowed"), ("p3", "owned")],
        )


class TestRunnerArgvContract(unittest.TestCase):
    """batch → runner 的调用契约（plan 0A.1-0A.3）。"""

    def test_argv_contains_orchestration_flags(self):
        lc = wb.SubprocessRunnerLauncher(runner_path="/x/wan22_generate.py", python_exec="/py")
        argv = lc.build_argv(
            "j1",
            {"prompt": "p", "mode": "prod", "seed": 88, "lora_strength": 1.0,
             "morph_lora": "slop_bounce", "end_image": "/e.png"},
            "/refs/job_j1_abcd1234.png",
            "/res/j1.ndjson",
            "/out",
            "wan22_j1",
        )
        for flag in ("--orchestrated", "--keep-pod", "--result-json", "--filename-prefix", "--job-id"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--filename-prefix") + 1], "wan22_j1")
        self.assertEqual(argv[argv.index("--result-json") + 1], "/res/j1.ndjson")
        self.assertEqual(argv[argv.index("--image") + 1], "/refs/job_j1_abcd1234.png")
        self.assertEqual(argv[argv.index("--end-image") + 1], "/e.png")


if __name__ == "__main__":
    unittest.main(verbosity=2)

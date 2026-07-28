"""pytest 兼容层：把 runpod/ 加进 sys.path 并注入假凭证。

测试全部离线（mock requests/subprocess/GraphQL），绝不发真实网络请求、不建 pod。
wan22_generate.py 在 import 时读 RUNPOD_API_KEY/RUNPOD_POD_ID（缺 key 会 sys.exit(2)），
因此这里必须在 import 之前把假 env 设好。本机无 pytest 时用
`python3 -m unittest discover -s runpod/tests -t runpod/tests` 跑同一组用例。
"""

import os
import sys

RUNPOD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RUNPOD_DIR not in sys.path:
    sys.path.insert(0, RUNPOD_DIR)

os.environ.setdefault("RUNPOD_API_KEY", "test-key-offline-only")
os.environ.setdefault("RUNPOD_POD_ID", "fakepod0")

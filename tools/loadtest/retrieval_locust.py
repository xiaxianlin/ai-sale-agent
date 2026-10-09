# 知识产权归甲方所有（COM-IP-01）
"""检索接口 locust 压测编排（NFR-PER-02 §6.6；CI/联调用配置形态）。

定位：标准 locustfile——由 locust 运行器装载（本机不装 locust 时，用同目录
retrieval_load.py 纯 httpx 版跑同等口径），两者共用验收判定线：
P95≤500ms 且 错误率<0.1%（NFR-PER-02 / SRS §16.1）。

环境变量（凭证占位经环境注入、不入 Git——NFR-SEC-06 数据与凭证边界）：
  KB_LOAD_HOST       站点根（默认 http://127.0.0.1:8000；locust --host 可覆盖）
  KB_LOAD_PATH       检索端点路径（默认 /api/v1/knowledge/search，04-接口设计 §2）
  KB_LOAD_API_KEY    Bearer 凭证（缺省匿名——内网联调环境）
  KB_LOAD_WAIT_MIN   每用户思考时间下限秒（默认 0.05）
  KB_LOAD_WAIT_MAX   每用户思考时间上限秒（默认 0.15）

运行示例（并发 10、孵化率 2/s、时长 30s，验收跑分口径 SR-NFR-PER-02 §6.6）：
  KB_LOAD_API_KEY=*** locust -f tools/loadtest/retrieval_locust.py \
      --headless -u 10 -r 2 -t 30s --html out/bench_locust.html
"""
from __future__ import annotations

import os
from typing import Any

from locust import HttpUser, between, task

# ── 运行参数：环境变量化（NFR-MAI-02 可配置，CI 与联调零改码）──
SEARCH_PATH = os.environ.get("KB_LOAD_PATH", "/api/v1/knowledge/search")
_API_KEY = os.environ.get("KB_LOAD_API_KEY", "").strip()
_WAIT_MIN = float(os.environ.get("KB_LOAD_WAIT_MIN", "0.05"))
_WAIT_MAX = float(os.environ.get("KB_LOAD_WAIT_MAX", "0.15"))

# 典型检索问询池（轮转）：覆盖 product/training/faq 三类知识库的常见问法
# （构成参考 FR-KB-016 评测集，P4 复测可整体替换为评测集全量 100 条）
QUERY_POOL: tuple[dict[str, Any], ...] = (
    {"query": "stainless steel pipe 304 price and MOQ", "kb_types": ["product"], "top_k": 5, "rerank": True},
    {"query": "如何回复客户索要免费样品的邮件", "kb_types": ["training"], "top_k": 5, "rerank": True},
    {"query": "海运到汉堡的时效与箱规要求", "kb_types": ["product", "training"], "top_k": 5, "rerank": False},
    {"query": "payment terms L/C acceptance policy", "kb_types": ["faq"], "top_k": 3, "rerank": True},
)


class RetrievalSearchUser(HttpUser):
    """单虚拟用户：按思考时间间隔轮转压测检索端点（NFR-PER-02 验收跑分）。"""

    # 站点根：环境变量给默认值，locust --host 命令行可覆盖
    host = os.environ.get("KB_LOAD_HOST", "http://127.0.0.1:8000")
    wait_time = between(_WAIT_MIN, _WAIT_MAX)

    def on_start(self) -> None:
        """用户启动：装配鉴权头与问询轮转游标（凭证经环境变量注入，NFR-SEC-06）。"""
        self.auth_headers: dict[str, str] = {"Authorization": f"Bearer {_API_KEY}"} if _API_KEY else {}
        self._seq = 0

    @task
    def search(self) -> None:
        """发起一次检索：失败（非 2xx/3xx）由 locust 记为错误，P95 汇总供验收判定。"""
        payload = QUERY_POOL[self._seq % len(QUERY_POOL)]
        self._seq += 1
        self.client.post(SEARCH_PATH, json=payload, headers=self.auth_headers, name=SEARCH_PATH)

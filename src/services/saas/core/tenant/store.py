# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""租户数据面存储桩（FR-SAAS-001 四层隔离的业务载体；生产为 PG RLS / Milvus
Partition Key / Redis 前缀 / OSS 前缀，见 SR-SAAS-001 §6.2-6.3）。

- KnowledgeStore：知识条目（行级 tenant_id；检索按上下文租户过滤——隔离套件
  的「canary 检索零泄漏」靶场）；
- ObjectStore：对象存储（键前缀 tenants/{tenant_id}/ 强制；生产 OSS 同构）；
- KeySpace：Redis 键前缀形态（t:{tenant_id}:…，禁止裸拼，SR §4 键规范）。

leak 开关仅供隔离套件元测试注入缺陷（模拟 RLS 失效），产品环境恒为 False。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

#: 对象键前缀规范（SR-NFR-SEC-07 §6.2：tenants/{tenant_id}/…）
def object_prefix(tenant_id: int) -> str:
    return f"tenants/{int(tenant_id)}/"

#: Redis 键前缀规范（SR-SAAS-001 §4：t:{tenant_id}:…）
def redis_key(tenant_id: int, name: str) -> str:
    return f"t:{int(tenant_id)}:{name}"


@dataclass
class KnowledgeItem:
    """知识条目（行级 tenant_id 为隔离边界，生产由 RLS FORCE 兜底）。"""

    uid: str
    tenant_id: int
    title: str
    body: str
    created_at: datetime | None = None


class KnowledgeStore:
    """知识条目存储：写入强制覆写 tenant_id（双保险，SR §6.3 upsert 口径）。"""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._items: dict[str, KnowledgeItem] = {}

    def create(self, tenant_id: int, title: str, body: str) -> KnowledgeItem:
        """创建条目：uid 全局唯一，行级 tenant_id 以上下文覆写（不信任入参）。"""
        item = KnowledgeItem(
            uid=uuid.uuid4().hex,
            tenant_id=int(tenant_id),
            title=title,
            body=body,
            created_at=self._clock(),
        )
        with self._lock:
            self._items[item.uid] = item
        return item

    def get(self, uid: str) -> KnowledgeItem | None:
        """裸读（不做租户过滤——过滤在 API 层按上下文执行，等价 RLS 语义分层）。"""
        with self._lock:
            return self._items.get(uid)

    def update(self, uid: str, *, title: str | None = None, body: str | None = None) -> KnowledgeItem | None:
        with self._lock:
            item = self._items.get(uid)
            if item is None:
                return None
            if title is not None:
                item.title = title
            if body is not None:
                item.body = body
            return item

    def delete(self, uid: str) -> bool:
        with self._lock:
            return self._items.pop(uid, None) is not None

    def search(self, tenant_id: int, q: str, *, limit: int = 10) -> list[KnowledgeItem]:
        """按上下文租户过滤的子串检索（等价 Partition Key 强制过滤，SR §6.3）。"""
        with self._lock:
            rows = [i for i in self._items.values() if i.tenant_id == int(tenant_id)]
        if q:
            rows = [i for i in rows if q in i.title or q in i.body]
        return rows[:limit]

    def items_of(self, tenant_id: int) -> list[KnowledgeItem]:
        with self._lock:
            return [i for i in self._items.values() if i.tenant_id == int(tenant_id)]

    def all_items(self) -> list[KnowledgeItem]:
        with self._lock:
            return list(self._items.values())


class ObjectStore:
    """对象存储桩：写入键强制归位租户前缀；跨前缀读取拒绝（403/404 由 API 层定）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._objects: dict[str, str] = {}

    def put(self, tenant_id: int, key: str, content: str) -> str:
        """写入：键强制加 tenants/{tid}/ 前缀（业务键禁止裸拼，SR §4 口径）。"""
        canonical = object_prefix(tenant_id) + key.lstrip("/")
        with self._lock:
            self._objects[canonical] = content
        return canonical

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._objects.get(key)

    def list_prefix(self, prefix: str) -> list[str]:
        with self._lock:
            return sorted(k for k in self._objects if k.startswith(prefix))


class KeySpace:
    """Redis 键空间桩：全部业务键经本类生成（带 t:{tid}: 前缀），SCAN 校验无逃逸。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._keys: set[str] = set()

    def issue(self, tenant_id: int, name: str) -> str:
        key = redis_key(tenant_id, name)
        with self._lock:
            self._keys.add(key)
        return key

    def scan(self, tenant_id: int | None = None) -> list[str]:
        """SCAN（match 语义）：tenant_id=None 全量；否则仅该租户前缀键。"""
        with self._lock:
            keys = sorted(self._keys)
        if tenant_id is None:
            return keys
        return [k for k in keys if k.startswith(f"t:{int(tenant_id)}:")]

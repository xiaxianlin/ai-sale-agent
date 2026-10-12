# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""知识条目管理 API（FR-KB-020 可视化管理·后台 API 层，迭代 0.3）。

范围（SR-KB-020 §5②③④、§6.4；04-接口设计 §3.1）：
- POST/GET ``/api/v1/knowledge/items``、GET/PUT/DELETE ``/api/v1/knowledge/items/{uid}``
  ——知识条目增删改查，字段随 kb_type 自适应（metadata 必填项按 Schema 注册表校验）；
- 草稿-发布状态机 ``draft → published → archived``（非法迁移 40001）；
  发布触发 sync_hook（FR-KB-017 增量索引：upsert/remove + 幂等键，失败不阻断保存）；
- 全部写操作调 audit_hook 留痕（NFR-SEC-03：action_type=kb_change，before/after 摘要）；
- 存储抽象 ``KnowledgeItemStore`` Protocol + ``InMemoryKnowledgeItemStore`` 实现
  （生产为 PG knowledge_item，03-数据库设计 §3.1）。

本文件同时承载管理端共享基座（统一信封/钩子协议/Schema 提供者协议/依赖注入点），
schemas_api.py 与 health.py 单向引用本模块，避免包内环依赖。

装配与挂载（本迭代不改 app.py/routes.py，路由以 APIRouter 交付）：
- 维护者挂载：``configure_admin(app, ...)``（见包 ``__init__.py``）写 ``app.state.admin_*``
  并挂三个路由；鉴权（JWT/RBAC，NFR-SEC-02）由挂载方经路由级 dependencies 追加；
- 统一错误信封复用 api/routes.py 迭代 0.2 冻结实现（ApiError + install_error_handlers），
  挂载到既有 kb 应用（create_app 产物）时已注册，独立运行由 create_admin_app 注册。

偏差标注（相对 SR-KB-020 文稿，待设计文档评审收敛）：
① HTTP 状态码口径：SR §5③ 草案为「HTTP 200 + code=40001」；本服务迭代 0.2 检索
   API 已冻结「HTTP 400/404/409 + 业务码信封」，管理端沿用服务内既有口径；
② schema_provider 鸭子契约为 ``[{kb_type, fields:[{name,label,required,control,options}]}]``
   （并行任务 services/kb/models/registry.py 的对接口径，**不 import 其实现**），
   键名与 SR §6.1 FieldDef（key/widget）的收敛由注册表侧适配器完成；
③ RBAC/JWT 未接入：操作人以 ``X-Actor`` 头承接（生产由 JWT 中间件注入真实用户）；
④ request_id 透传审计与索引幂等键；完整「重复请求返回首次结果」幂等缓存未实现
   （04 §1.4，随 FR-KB-021/022 迭代）；
⑤ metadata 校验本迭代仅覆盖必填缺失（provider 契约未含 pattern/visible_when）；
⑥ sync_status 为条目级字段（PG 形态按 SR §4 由 knowledge_chunk.vector_status 聚合推导）。
"""
from __future__ import annotations

import inspect
import threading
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

from fastapi import APIRouter, Depends, Query, Request
from foundation.apikey.dependency import resolve_trace_id
from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.kb.api.routes import CODE_BAD_REQUEST, ApiError

from ..schemas import KB_TYPE_ENUM

# --------------------------------------------------------------------------- 常量（口径锚点）

#: 条目状态（SR-KB-020 §2 决策 3：draft → published → archived）
STATUS_DRAFT: str = "draft"
STATUS_PUBLISHED: str = "published"
STATUS_ARCHIVED: str = "archived"
ITEM_STATUSES: tuple[str, ...] = (STATUS_DRAFT, STATUS_PUBLISHED, STATUS_ARCHIVED)

#: 写入动作（SR-KB-020 §5③：action ∈ {save_draft, publish, archive, restore}）
ACTION_SAVE_DRAFT: str = "save_draft"
ACTION_PUBLISH: str = "publish"
ACTION_ARCHIVE: str = "archive"
ACTION_RESTORE: str = "restore"
ITEM_ACTIONS: tuple[str, ...] = (ACTION_SAVE_DRAFT, ACTION_PUBLISH, ACTION_ARCHIVE, ACTION_RESTORE)
#: 新建仅允许的两档入口动作（新建即归档/恢复无语义 → 40001）
CREATE_ACTIONS: tuple[str, ...] = (ACTION_SAVE_DRAFT, ACTION_PUBLISH)

#: 草稿-发布状态机（SR-KB-020 §6.4 TRANSITIONS；非法组合一律 40001）
TRANSITIONS: dict[tuple[str, str], str] = {
    (STATUS_DRAFT, ACTION_PUBLISH): STATUS_PUBLISHED,
    (STATUS_DRAFT, ACTION_SAVE_DRAFT): STATUS_DRAFT,
    (STATUS_PUBLISHED, ACTION_SAVE_DRAFT): STATUS_PUBLISHED,  # 已发布条目修改保存：出新版本、保持已发布
    (STATUS_PUBLISHED, ACTION_ARCHIVE): STATUS_ARCHIVED,
    (STATUS_ARCHIVED, ACTION_RESTORE): STATUS_DRAFT,
}

#: 索引同步状态（SR-KB-020 §4：synced / indexing / failed；本迭代增补 none/removed 两态）
SYNC_NONE: str = "none"          # 未入索引（草稿/已恢复为草稿）
SYNC_INDEXING: str = "indexing"  # 已触发 FR-KB-017 增量入队
SYNC_SYNCED: str = "synced"      # 索引完成（由 FR-KB-017 回写，本层不主动置位）
SYNC_FAILED: str = "failed"      # 入队失败（列表页呈现「索引失败」并供重试）
SYNC_REMOVED: str = "removed"    # 已从索引移除（归档/软删）
#: 索引变更方向（SR-KB-020 §6.4 enqueue(change=...)）
SYNC_CHANGE_UPSERT: str = "upsert"
SYNC_CHANGE_REMOVE: str = "remove"

#: 分页口径（04-接口设计 §1.4：page_size 上限 100；SR-KB-020 §7 默认 20）
PAGE_SIZE_DEFAULT: int = 20
PAGE_SIZE_MAX: int = 100

#: 标题长度上限（对齐检索 API QUERY_MAX_LENGTH 的管理端同量级约束）
TITLE_MAX_LENGTH: int = 512

#: 错误码（04-接口设计 §1.3 + SR-KB-020 §5 增补的 40401/40901）
CODE_NOT_FOUND: int = 40401  # 条目不存在或已删除
CODE_CONFLICT: int = 40901   # 乐观锁版本冲突（他人已修改）

#: 操作人头（JWT 未接入期的承接口径，见模块 docstring 偏差③）
ACTOR_HEADER: str = "x-actor"

#: 管理端写操作 HTTP 方法集（缺省鉴权依赖的拒绝面；GET/HEAD/OPTIONS 读操作放行）
WRITE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: 时间兜底常量（记录缺时间字段的排序/展示兜底，正常路径不会出现）
_EPOCH: datetime = datetime(1970, 1, 1, tzinfo=timezone.utc)

# --------------------------------------------------------------------------- 共享基座：统一信封与请求上下文

_DataT = TypeVar("_DataT")


class AdminEnvelope(BaseModel, Generic[_DataT]):
    """管理端统一响应信封（04-接口设计 §1.3：code/message/data/trace_id 四键齐全）。"""

    code: int = Field(0, description="0=成功；非 0 见 §1.3 错误码表")
    message: str = Field("ok", description="成功恒 ok；失败为错误摘要")
    data: _DataT
    trace_id: str = Field(..., description="全链路追踪 ID（X-Trace-Id 透传或新生成 uuid）")


def request_trace_id(request: Request) -> str:
    """解析并固化本请求 trace_id（优先透传 X-Trace-Id，04 §1.4）。

    写入 request.state 供统一错误处理器复用同一 ID（与检索路由鉴权依赖同款落点），
    保证成功与错误路径的 trace_id 全链路一致。
    """
    trace_id: str = getattr(request.state, "trace_id", "") or resolve_trace_id(request)
    request.state.trace_id = trace_id
    return trace_id


def request_actor(request: Request) -> str:
    """取操作人：X-Actor 头承接（缺省 anonymous；生产由 JWT 中间件注入真实用户）。"""
    actor: str = request.headers.get(ACTOR_HEADER, "").strip()
    return actor or "anonymous"


def deny_write_admin_auth(request: Request) -> None:
    """缺省管理端鉴权依赖（CR P0-3 修复）：未注入 ``admin_auth`` 时写操作一律拒绝
    （HTTP 403 + 业务码 40301 信封），读操作（GET/HEAD/OPTIONS）放行。

    生产 = 挂载方经 ``configure_admin(..., admin_auth=...)`` 注入 JWT/RBAC 路由依赖
    （NFR-SEC-02）；本依赖仅为「未配置鉴权即关门」的安全缺省（fail-closed）。
    """
    if request.method in WRITE_METHODS:
        raise ApiError(
            40301,
            403,
            "管理端未配置鉴权依赖：写操作被拒绝（生产必须注入 JWT/RBAC admin_auth）",
            None,
        )


async def _maybe_await(value: Any) -> None:
    """同步/异步钩子统一等待：返回可等待对象则等待，否则即同步完成。"""
    if inspect.isawaitable(value):
        await value

# --------------------------------------------------------------------------- 共享基座：三个注入协议


@runtime_checkable
class SyncHook(Protocol):
    """索引同步钩子协议（FR-KB-017 增量索引入口的鸭子契约，注入使用）。

    发布/修改已发布条目 → change="upsert"；归档/删除 → change="remove"。
    idem_key 形如 ``{uid}:v{版本}``（幂等键防双击/重试重复建索引，SR-KB-020 §6.4）。
    """

    def enqueue(self, *, item_uid: str, change: str, idem_key: str, version: int) -> None:
        """入队一次索引变更；实现可同步或异步（本层统一等待）。"""
        ...  # pragma: no cover - Protocol 桩


@runtime_checkable
class AuditHook(Protocol):
    """审计钩子协议（NFR-SEC-03：全部写操作留痕，注入使用）。

    事件为 JSON 安全字典，键：action_type（恒 "kb_change"）、action（create/update/
    delete）、state_action（save_draft/publish/archive/restore 或 None）、uid、kb_type、
    title、actor、result（恒 "success"）、before/after（摘要字典或 None）、request_id、
    trace_id、occurred_at（ISO 8601）。生产侧适配到平台 audit_log 哈希链写入器。
    """

    def record(self, event: Mapping[str, Any]) -> None:
        """记录一条审计事件；实现可同步或异步（本层统一等待）。"""
        ...  # pragma: no cover - Protocol 桩


@runtime_checkable
class SchemaProvider(Protocol):
    """Schema 注册表提供者协议（鸭子契约，并行任务 registry.py 的对接口径）。

    返回 ``[{kb_type, fields: [{name, label, required, control, options}, ...]}, ...]``；
    本层**不 import** registry 实现，测试自建桩（见模块 docstring 偏差②）。
    """

    def list_schemas(self) -> Sequence[Mapping[str, Any]]:
        """返回六大知识库字段定义清单（契约外键允许透传）。"""
        ...  # pragma: no cover - Protocol 桩


def provider_schemas(provider: Any) -> Sequence[Mapping[str, Any]]:
    """按鸭子契约取注册表清单：带 list_schemas 方法或本身可调用均可。

    其余形态（含 None）返回空序列——提供者缺省仅使 metadata 必填校验跳过，
    不阻断 CRUD 主链路（/schemas 端点由注入兜底 EmptySchemaProvider 承接）。
    """
    if provider is None:
        return ()
    if hasattr(provider, "list_schemas"):
        schemas: Any = provider.list_schemas()
    elif callable(provider):
        schemas = provider()
    else:
        return ()
    return schemas or ()


def missing_required_fields(
    provider: Any, kb_type: str, metadata: Mapping[str, Any]
) -> list[dict[str, str]]:
    """按注册表必填字段校验 metadata：缺失/空值 → 中文错误项（SR §5③ data.errors 形态）。

    注册表未登记的 kb_type 不校验（注册表并行接入期的宽松口径）；契约外规则
    （pattern/visible_when）不在本迭代范围（偏差⑤）。
    """
    errors: list[dict[str, str]] = []
    for entry in provider_schemas(provider):
        if entry.get("kb_type") != kb_type:
            continue
        for raw in entry.get("fields") or ():
            name = raw.get("name")
            if not name or not raw.get("required"):
                continue
            value = metadata.get(name)
            if value is None or value == "" or value == []:
                label = raw.get("label") or name
                errors.append({"field": str(name), "msg": f"请填写「{label}」"})
    return errors


class NoopSyncHook:
    """空同步钩子（未接线 FR-KB-017 时的占位：不触发任何索引动作）。"""

    def enqueue(self, *, item_uid: str, change: str, idem_key: str, version: int) -> None:
        """占位实现：静默吞掉入队调用（本地开发形态）。"""
        return None


class NoopAuditHook:
    """空审计钩子（本地开发占位；生产必须注入真实审计写入器，NFR-SEC-03）。"""

    def record(self, event: Mapping[str, Any]) -> None:
        """占位实现：不落任何审计事件。"""
        return None

# --------------------------------------------------------------------------- 存储抽象与内存实现


@dataclass(frozen=True)
class KnowledgeItemRecord:
    """知识条目存储行（PG knowledge_item 的内存形态，03-数据库设计 §3.1）。

    uid/current_version/sync_status 等由存储与服务层维护；create 时 uid 与时间戳
    允许缺省（存储负责补齐），便于测试与 FR-KB-017 侧灌数。
    """

    kb_type: str
    title: str
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = STATUS_DRAFT
    current_version: int = 1
    uid: str = ""
    sync_status: str = SYNC_NONE
    created_at: datetime | None = None
    updated_at: datetime | None = None
    updated_by: str = ""
    deleted_at: datetime | None = None  # 软删标记（列表/详情均不可见）


@runtime_checkable
class KnowledgeItemStore(Protocol):
    """条目存储协议：管理 API 的唯一持久化依赖（生产为 PG，测试为内存实现）。

    save 为乐观锁原子语义：行不存在、已软删或 current_version 与 expected_version
    不一致时返回 None（调用方映射 40901）；expected_version 缺省表示跳过版本比对。
    """

    def create(self, record: KnowledgeItemRecord) -> KnowledgeItemRecord:
        """建档：uid/created_at/updated_at 缺省由存储补齐；uid 冲突显式失败。"""
        ...  # pragma: no cover - Protocol 桩

    def get(self, uid: str) -> KnowledgeItemRecord | None:
        """按 uid 取未软删条目；不存在/已软删返回 None。"""
        ...  # pragma: no cover - Protocol 桩

    def save(
        self, record: KnowledgeItemRecord, expected_version: int | None = None
    ) -> KnowledgeItemRecord | None:
        """乐观锁整行覆盖保存；版本不符/行缺失返回 None。"""
        ...  # pragma: no cover - Protocol 桩

    def soft_delete(
        self, uid: str, *, now: datetime | None = None, actor: str = ""
    ) -> KnowledgeItemRecord | None:
        """软删：置 deleted_at、版本 +1；行不存在/已软删返回 None。"""
        ...  # pragma: no cover - Protocol 桩

    def query(
        self,
        *,
        kb_type: str | None = None,
        status: str | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = PAGE_SIZE_DEFAULT,
    ) -> tuple[list[KnowledgeItemRecord], int]:
        """过滤 + 分页（updated_at 降序，uid 升序决胜）；返回 (当前页行, 总数)。"""
        ...  # pragma: no cover - Protocol 桩

    def iter_all(self, *, include_deleted: bool = False) -> Iterator[KnowledgeItemRecord]:
        """遍历条目（统计用；生产实现映射 SELECT，缺省不含软删行）。"""
        ...  # pragma: no cover - Protocol 桩

    def set_sync_status(self, uid: str, sync_status: str) -> None:
        """回写索引同步状态（FR-KB-017 完成回写/失败标记的入口）。"""
        ...  # pragma: no cover - Protocol 桩


class InMemoryKnowledgeItemStore:
    """内存存储实现（线程安全）：单元测试与本地开发；语义对齐 PG 口径。

    - 软删行保留在存储中但 get/query/iter_all（缺省）均不可见（30 天恢复窗口语义）；
    - query 的关键词 q 匹配标题/正文（大小写不敏感包含；PG 形态走 pg_trgm/tsvector）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, KnowledgeItemRecord] = {}

    @staticmethod
    def _gen_uid() -> str:
        """生成条目 UID：``k-`` + 12 位十六进制（对齐 04 §2 示例形态）。"""
        return f"k-{uuid.uuid4().hex[:12]}"

    def create(self, record: KnowledgeItemRecord) -> KnowledgeItemRecord:
        """建档：补齐 uid/时间戳（显式传入值保留，供测试控制时间口径）。"""
        now = datetime.now(timezone.utc)
        stored = KnowledgeItemRecord(
            kb_type=record.kb_type,
            title=record.title,
            content=record.content,
            metadata=dict(record.metadata),
            status=record.status,
            current_version=max(record.current_version, 1),
            uid=record.uid or self._gen_uid(),
            sync_status=record.sync_status,
            created_at=record.created_at or now,
            updated_at=record.updated_at or now,
            updated_by=record.updated_by,
            deleted_at=record.deleted_at,
        )
        with self._lock:
            if stored.uid in self._items:
                raise ValueError(f"条目 UID 冲突: {stored.uid}")
            self._items[stored.uid] = stored
        return stored

    def get(self, uid: str) -> KnowledgeItemRecord | None:
        """按 uid 取未软删条目。"""
        with self._lock:
            record = self._items.get(uid)
        if record is None or record.deleted_at is not None:
            return None
        return record

    def save(
        self, record: KnowledgeItemRecord, expected_version: int | None = None
    ) -> KnowledgeItemRecord | None:
        """乐观锁保存：版本比对 + 整行覆盖在同一锁内完成（原子语义）。"""
        with self._lock:
            current = self._items.get(record.uid)
            if current is None or current.deleted_at is not None:
                return None
            if expected_version is not None and current.current_version != expected_version:
                return None
            self._items[record.uid] = record
            return record

    def soft_delete(
        self, uid: str, *, now: datetime | None = None, actor: str = ""
    ) -> KnowledgeItemRecord | None:
        """软删：置 deleted_at、版本 +1、同步状态置 removed。"""
        moment = now or datetime.now(timezone.utc)
        with self._lock:
            current = self._items.get(uid)
            if current is None or current.deleted_at is not None:
                return None
            deleted = replace(
                current,
                deleted_at=moment,
                updated_at=moment,
                updated_by=actor or current.updated_by,
                current_version=current.current_version + 1,
                sync_status=SYNC_REMOVED,
            )
            self._items[uid] = deleted
            return deleted

    def query(
        self,
        *,
        kb_type: str | None = None,
        status: str | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = PAGE_SIZE_DEFAULT,
    ) -> tuple[list[KnowledgeItemRecord], int]:
        """过滤（kb_type/status/关键词）→ 排序（updated_at 降序，uid 升序决胜）→ 分页。"""
        with self._lock:
            snapshot = [r for r in self._items.values() if r.deleted_at is None]
        matched = [
            r
            for r in snapshot
            if (kb_type is None or r.kb_type == kb_type) and (status is None or r.status == status)
        ]
        if q:
            needle = q.casefold()
            matched = [r for r in matched if needle in r.title.casefold() or needle in r.content.casefold()]
        # 两遍稳定排序：先 uid 升序，再 updated_at 降序（同刻修改按 uid 确定性排序）
        matched.sort(key=lambda r: r.uid)
        matched.sort(key=lambda r: r.updated_at or _EPOCH, reverse=True)
        total = len(matched)
        start = (page - 1) * page_size
        return matched[start : start + page_size], total

    def iter_all(self, *, include_deleted: bool = False) -> Iterator[KnowledgeItemRecord]:
        """遍历条目快照（缺省仅未软删行）。"""
        with self._lock:
            snapshot = list(self._items.values())
        for record in snapshot:
            if include_deleted or record.deleted_at is None:
                yield record

    def set_sync_status(self, uid: str, sync_status: str) -> None:
        """回写索引同步状态（软删行静默跳过）。"""
        with self._lock:
            record = self._items.get(uid)
            if record is not None and record.deleted_at is None:
                self._items[uid] = replace(record, sync_status=sync_status)

# --------------------------------------------------------------------------- 依赖注入点（app.state.admin_*）


def _require_state(request: Request, attr: str, hint: str) -> Any:
    """从 app.state 取已装配组件；缺失即显式失败（fail-loud，对齐检索路由口径）。"""
    value = getattr(request.app.state, attr, None)
    if value is None:
        raise RuntimeError(f"{hint}未装配：configure_admin 负责注入 app.state.{attr}")
    return value


def get_item_store(request: Request) -> KnowledgeItemStore:
    """依赖：条目存储（configure_admin 注入 app.state.admin_item_store）。"""
    return _require_state(request, "admin_item_store", "条目存储")  # type: ignore[no-any-return]


def get_sync_hook(request: Request) -> SyncHook:
    """依赖：索引同步钩子（app.state.admin_sync_hook；缺省 NoopSyncHook）。"""
    return _require_state(request, "admin_sync_hook", "索引同步钩子")  # type: ignore[no-any-return]


def get_audit_hook(request: Request) -> AuditHook:
    """依赖：审计钩子（app.state.admin_audit_hook；生产必须为真实写入器）。"""
    return _require_state(request, "admin_audit_hook", "审计钩子")  # type: ignore[no-any-return]


def get_schema_provider(request: Request) -> SchemaProvider:
    """依赖：Schema 注册表提供者（app.state.admin_schema_provider）。"""
    return _require_state(request, "admin_schema_provider", "Schema 注册表提供者")  # type: ignore[no-any-return]


def optional_schema_provider(request: Request) -> Any:
    """取 Schema 提供者（可缺省）：仅影响 metadata 必填校验，不阻断 CRUD 主链路。"""
    return getattr(request.app.state, "admin_schema_provider", None)

# --------------------------------------------------------------------------- 写路径公共动作（审计 / 索引触发）


def audit_summary(record: KnowledgeItemRecord) -> dict[str, Any]:
    """审计 before/after 摘要（NFR-SEC-03 摘要口径：不含全文 content）。"""
    return {
        "uid": record.uid,
        "kb_type": record.kb_type,
        "title": record.title,
        "status": record.status,
        "current_version": record.current_version,
        "sync_status": record.sync_status,
        "deleted": record.deleted_at is not None,
    }


async def fire_audit(
    hook: AuditHook,
    *,
    action: str,
    state_action: str | None,
    before: KnowledgeItemRecord | None,
    after: KnowledgeItemRecord | None,
    actor: str,
    trace_id: str,
    request_id: str | None,
    occurred_at: datetime,
) -> None:
    """写一条 kb_change 审计事件（同步/异步钩子均兼容；异常上抛交由全局兜底）。"""
    anchor = after if after is not None else before
    event: dict[str, Any] = {
        "action_type": "kb_change",
        "action": action,
        "state_action": state_action,
        "uid": anchor.uid if anchor is not None else "",
        "kb_type": anchor.kb_type if anchor is not None else "",
        "title": after.title if after is not None else (before.title if before is not None else ""),
        "actor": actor,
        "result": "success",
        "before": audit_summary(before) if before is not None else None,
        "after": audit_summary(after) if after is not None else None,
        "request_id": request_id or "",
        "trace_id": trace_id,
        "occurred_at": occurred_at.isoformat(),
    }
    await _maybe_await(hook.record(event))


async def fire_sync(
    hook: SyncHook,
    *,
    store: KnowledgeItemStore,
    item_uid: str,
    change: str,
    version: int,
    ok_status: str,
) -> None:
    """触发 FR-KB-017 增量索引：失败不阻断管理操作（PG 为权威源，SR-KB-020 §6.4）。

    入队成功 → sync_status=ok_status（indexing/removed）；入队异常 → failed，
    由列表页「索引失败」标记与重试入口承接（恢复走 SR-KB-017 补索引通道）。
    """
    idem_key = f"{item_uid}:v{version}"
    try:
        await _maybe_await(
            hook.enqueue(item_uid=item_uid, change=change, idem_key=idem_key, version=version)
        )
        store.set_sync_status(item_uid, ok_status)
    except Exception:
        store.set_sync_status(item_uid, SYNC_FAILED)

# --------------------------------------------------------------------------- 请求/响应契约模型


class ItemWriteRequest(BaseModel):
    """条目写入体（SR-KB-020 §5③：kb_type/title/content/metadata/action/expected_version/request_id）。

    extra="forbid"：契约外字段 fail-closed 拒绝（映射 40001），与检索 API 同款防漂移口径。
    """

    model_config = ConfigDict(extra="forbid")

    kb_type: str = Field(..., description="六大知识库类型（枚举校验）")
    title: str = Field(..., min_length=1, max_length=TITLE_MAX_LENGTH, description="条目标题")
    content: str = Field("", description="条目正文（纯文本/Markdown）")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="条目元数据（字段集随 kb_type 由注册表定义）"
    )
    action: str = Field(
        ACTION_SAVE_DRAFT, description="写入动作：save_draft/publish/archive/restore（新建仅前两种）"
    )
    expected_version: int | None = Field(
        None, ge=1, description="乐观锁期望版本（PUT 传入；缺省按当前版本直写）"
    )
    request_id: str | None = Field(
        None, max_length=64, description="写幂等请求号（透传审计与索引幂等键）"
    )

    @field_validator("kb_type")
    @classmethod
    def _check_kb_type(cls, value: str) -> str:
        """kb_type 枚举校验：未知类型拒绝并列出合法值（40001 定位信息）。"""
        if value not in KB_TYPE_ENUM:
            raise ValueError(f"未知知识库类型 {value}（合法值 {list(KB_TYPE_ENUM)}）")
        return value

    @field_validator("action")
    @classmethod
    def _check_action(cls, value: str) -> str:
        """action 枚举校验：状态机合法性再由端点按当前状态判定。"""
        if value not in ITEM_ACTIONS:
            raise ValueError(f"未知动作 {value}（合法值 {list(ITEM_ACTIONS)}）")
        return value


class ItemSummary(BaseModel):
    """列表行（SR-KB-020 §5② items[] 字段逐项一致）。"""

    uid: str = Field(..., description="条目 UID")
    kb_type: str = Field(..., description="所属知识库类型")
    title: str = Field(..., description="条目标题")
    status: str = Field(..., description="状态：draft/published/archived")
    current_version: int = Field(..., description="当前版本号")
    sync_status: str = Field(..., description="索引同步状态：none/indexing/synced/failed/removed")
    updated_at: datetime = Field(..., description="最后修改时间（ISO 8601 带时区）")
    updated_by: str = Field(..., description="最后修改人")


class ItemDetail(ItemSummary):
    """条目详情（列表字段 + 正文 + 元数据 + 创建时间）。"""

    content: str = Field(..., description="条目正文")
    metadata: dict[str, Any] = Field(..., description="条目元数据")
    created_at: datetime = Field(..., description="创建时间（ISO 8601 带时区）")


class ItemListData(BaseModel):
    """条目列表 data 段（04 §1.4 分页口径）。"""

    items: list[ItemSummary] = Field(..., description="当前页列表行")
    total: int = Field(..., description="过滤后总条数")
    page: int = Field(..., description="当前页码（1 起）")
    page_size: int = Field(..., description="每页条数（≤100）")


class ItemDeletedData(BaseModel):
    """删除结果 data 段。"""

    uid: str = Field(..., description="被软删条目 UID")
    deleted: bool = Field(..., description="恒 True（失败路径走错误信封）")


#: 各端点信封别名（FastAPI response_model 消费参数化具体模型）
ItemDetailEnvelope = AdminEnvelope[ItemDetail]
ItemListEnvelope = AdminEnvelope[ItemListData]
ItemDeletedEnvelope = AdminEnvelope[ItemDeletedData]


def item_summary(record: KnowledgeItemRecord) -> ItemSummary:
    """存储行 → 列表行投影。"""
    return ItemSummary(
        uid=record.uid,
        kb_type=record.kb_type,
        title=record.title,
        status=record.status,
        current_version=record.current_version,
        sync_status=record.sync_status,
        updated_at=record.updated_at or _EPOCH,
        updated_by=record.updated_by,
    )


def item_detail(record: KnowledgeItemRecord) -> ItemDetail:
    """存储行 → 详情投影。"""
    return ItemDetail(
        **item_summary(record).model_dump(),
        content=record.content,
        metadata=dict(record.metadata),
        created_at=record.created_at or _EPOCH,
    )

# --------------------------------------------------------------------------- 端点（FR-KB-020 §5②③④）

router = APIRouter(prefix="/api/v1/knowledge", tags=["kb-admin"])


@router.post(
    "/items",
    response_model=ItemDetailEnvelope,
    summary="新建知识条目（FR-KB-020：草稿/发布两档入口动作）",
)
async def create_item(
    body: ItemWriteRequest,
    request: Request,
    store: KnowledgeItemStore = Depends(get_item_store),
    sync_hook: SyncHook = Depends(get_sync_hook),
    audit_hook: AuditHook = Depends(get_audit_hook),
) -> ItemDetailEnvelope:
    """新建条目：动作合法性 → 必填字段校验 → 建档（草稿/发布）→ 审计 → 发布触发增量索引。"""
    trace_id = request_trace_id(request)
    actor = request_actor(request)
    if body.action not in CREATE_ACTIONS:
        raise ApiError(
            CODE_BAD_REQUEST,
            400,
            "参数校验失败：新建条目仅支持 save_draft / publish 动作",
            {"errors": [{"field": "action", "msg": f"新建不支持动作「{body.action}」，请先以草稿保存再发布"}]},
        )
    if errors := missing_required_fields(optional_schema_provider(request), body.kb_type, body.metadata):
        raise ApiError(CODE_BAD_REQUEST, 400, "参数校验失败：必填字段缺失", {"errors": errors})
    now = datetime.now(timezone.utc)
    record = store.create(
        KnowledgeItemRecord(
            kb_type=body.kb_type,
            title=body.title,
            content=body.content,
            metadata=dict(body.metadata),
            status=TRANSITIONS[(STATUS_DRAFT, body.action)],
            updated_by=actor,
            created_at=now,
            updated_at=now,
        )
    )
    await fire_audit(
        audit_hook,
        action="create",
        state_action=body.action,
        before=None,
        after=record,
        actor=actor,
        trace_id=trace_id,
        request_id=body.request_id,
        occurred_at=now,
    )
    if record.status == STATUS_PUBLISHED:
        # 草稿仅落库不进向量；发布即触发 FR-KB-017 增量 upsert（SR-KB-020 §2 决策 3）
        await fire_sync(
            sync_hook,
            store=store,
            item_uid=record.uid,
            change=SYNC_CHANGE_UPSERT,
            version=record.current_version,
            ok_status=SYNC_INDEXING,
        )
    # 索引触发会回写 sync_status（indexing/failed），响应取存储最新态
    latest = store.get(record.uid) or record
    return ItemDetailEnvelope(trace_id=trace_id, data=item_detail(latest))


@router.get(
    "/items",
    response_model=ItemListEnvelope,
    summary="知识条目列表（分页 + kb_type/status/关键词过滤）",
)
async def list_items(
    request: Request,
    page: int = Query(1, ge=1, description="页码（1 起）"),
    page_size: int = Query(
        PAGE_SIZE_DEFAULT, ge=1, le=PAGE_SIZE_MAX, description="每页条数（1~100，04 §1.4 上限）"
    ),
    kb_type: str | None = Query(None, description="六大知识库类型过滤"),
    status: str | None = Query(None, description="状态过滤：draft/published/archived"),
    q: str | None = Query(None, max_length=128, description="关键词（标题/正文包含匹配）"),
    store: KnowledgeItemStore = Depends(get_item_store),
) -> ItemListEnvelope:
    """列表查询：服务端分页（NFR-PER-05）+ 过滤维度校验（非法值 40001 定位字段）。"""
    trace_id = request_trace_id(request)
    if kb_type is not None and kb_type not in KB_TYPE_ENUM:
        raise ApiError(
            CODE_BAD_REQUEST,
            400,
            "参数校验失败：kb_type 非法",
            {"errors": [{"field": "kb_type", "msg": f"未知知识库类型 {kb_type}（合法值 {list(KB_TYPE_ENUM)}）"}]},
        )
    if status is not None and status not in ITEM_STATUSES:
        raise ApiError(
            CODE_BAD_REQUEST,
            400,
            "参数校验失败：status 非法",
            {"errors": [{"field": "status", "msg": f"未知状态 {status}（合法值 {list(ITEM_STATUSES)}）"}]},
        )
    records, total = store.query(kb_type=kb_type, status=status, q=q, page=page, page_size=page_size)
    return ItemListEnvelope(
        trace_id=trace_id,
        data=ItemListData(
            items=[item_summary(record) for record in records],
            total=total,
            page=page,
            page_size=page_size,
        ),
    )


@router.get(
    "/items/{uid}",
    response_model=ItemDetailEnvelope,
    summary="知识条目详情",
)
async def get_item(
    uid: str,
    request: Request,
    store: KnowledgeItemStore = Depends(get_item_store),
) -> ItemDetailEnvelope:
    """按 uid 取详情；不存在/已软删 → 40401 统一信封。"""
    record = store.get(uid)
    if record is None:
        raise ApiError(CODE_NOT_FOUND, 404, "知识条目不存在或已删除", None)
    return ItemDetailEnvelope(trace_id=request_trace_id(request), data=item_detail(record))


@router.put(
    "/items/{uid}",
    response_model=ItemDetailEnvelope,
    summary="修改知识条目（状态机迁移 + 乐观锁）",
)
async def update_item(
    uid: str,
    body: ItemWriteRequest,
    request: Request,
    store: KnowledgeItemStore = Depends(get_item_store),
    sync_hook: SyncHook = Depends(get_sync_hook),
    audit_hook: AuditHook = Depends(get_audit_hook),
) -> ItemDetailEnvelope:
    """修改条目：状态机判定 → 必填校验 → 乐观锁写库（版本 +1）→ 审计 → 按动作触发索引。

    - 条目所属知识库类型（kb_type）不可变更（内容模型与索引维度绑定）→ 40001；
    - 非法状态迁移（如 draft + archive）→ 40001（SR-KB-020 §9 test_transition_illegal）；
    - expected_version 落后 → 40901（他人已修改，提示刷新对比；SR §5③）；
    - 新状态为 published → FR-KB-017 upsert；archive → remove；restore → 回草稿态。
    """
    trace_id = request_trace_id(request)
    actor = request_actor(request)
    record = store.get(uid)
    if record is None:
        raise ApiError(CODE_NOT_FOUND, 404, "知识条目不存在或已删除", None)
    if body.kb_type != record.kb_type:
        # 条目所属知识库类型不可变更（六库内容模型与索引维度均绑定 kb_type）
        raise ApiError(
            CODE_BAD_REQUEST,
            400,
            "参数校验失败：条目所属知识库类型不可变更",
            {"errors": [{"field": "kb_type", "msg": f"条目类型为「{record.kb_type}」，不允许改为「{body.kb_type}」"}]},
        )
    new_status = TRANSITIONS.get((record.status, body.action))
    if new_status is None:
        raise ApiError(
            CODE_BAD_REQUEST,
            400,
            f"参数校验失败：非法状态迁移（{record.status} 不支持 {body.action}）",
            {"errors": [{"field": "action", "msg": f"当前状态「{record.status}」不能执行「{body.action}」"}]},
        )
    if errors := missing_required_fields(optional_schema_provider(request), body.kb_type, body.metadata):
        raise ApiError(CODE_BAD_REQUEST, 400, "参数校验失败：必填字段缺失", {"errors": errors})
    now = datetime.now(timezone.utc)
    candidate = replace(
        record,
        title=body.title,
        content=body.content,
        metadata=dict(body.metadata),
        status=new_status,
        current_version=record.current_version + 1,
        updated_at=now,
        updated_by=actor,
    )
    saved = store.save(candidate, expected_version=body.expected_version)
    if saved is None:
        raise ApiError(CODE_CONFLICT, 409, "条目已被他人修改，请刷新后对比再保存", None)
    await fire_audit(
        audit_hook,
        action="update",
        state_action=body.action,
        before=record,
        after=saved,
        actor=actor,
        trace_id=trace_id,
        request_id=body.request_id,
        occurred_at=now,
    )
    if saved.status == STATUS_PUBLISHED:
        await fire_sync(
            sync_hook,
            store=store,
            item_uid=saved.uid,
            change=SYNC_CHANGE_UPSERT,
            version=saved.current_version,
            ok_status=SYNC_INDEXING,
        )
    elif body.action == ACTION_ARCHIVE:
        await fire_sync(
            sync_hook,
            store=store,
            item_uid=saved.uid,
            change=SYNC_CHANGE_REMOVE,
            version=saved.current_version,
            ok_status=SYNC_REMOVED,
        )
    elif body.action == ACTION_RESTORE:
        # 归档恢复为草稿：不入索引（草稿仅落库），同步状态复位
        store.set_sync_status(saved.uid, SYNC_NONE)
    # 索引触发/状态复位会回写 sync_status，响应取存储最新态
    latest = store.get(saved.uid) or saved
    return ItemDetailEnvelope(trace_id=trace_id, data=item_detail(latest))


@router.delete(
    "/items/{uid}",
    response_model=ItemDeletedEnvelope,
    summary="删除知识条目（软删；已发布条目须先归档）",
)
async def delete_item(
    uid: str,
    request: Request,
    store: KnowledgeItemStore = Depends(get_item_store),
    sync_hook: SyncHook = Depends(get_sync_hook),
    audit_hook: AuditHook = Depends(get_audit_hook),
) -> ItemDeletedEnvelope:
    """软删条目（FR-KB-020 §5④）：置 deleted_at、留 30 天恢复窗口（SR §7 配置项）。

    已发布条目删除前强制先归档并二次确认（前端确认 + 本端点硬防线，
    SR §9 test_delete_published_guard 的「直接 API 删除被拒」侧）。
    """
    trace_id = request_trace_id(request)
    actor = request_actor(request)
    record = store.get(uid)
    if record is None:
        raise ApiError(CODE_NOT_FOUND, 404, "知识条目不存在或已删除", None)
    if record.status == STATUS_PUBLISHED:
        raise ApiError(
            CODE_BAD_REQUEST,
            400,
            "已发布条目删除前须先归档（archive）并二次确认",
            {"errors": [{"field": "status", "msg": "已发布条目不能直接删除，请先归档"}]},
        )
    now = datetime.now(timezone.utc)
    deleted = store.soft_delete(uid, now=now, actor=actor)
    if deleted is None:
        raise ApiError(CODE_NOT_FOUND, 404, "知识条目不存在或已删除", None)
    await fire_audit(
        audit_hook,
        action="delete",
        state_action=None,
        before=record,
        after=deleted,
        actor=actor,
        trace_id=trace_id,
        request_id=None,
        occurred_at=now,
    )
    # 软删同步触发索引 remove（幂等；草稿本就未入索引，remove 为无害空操作）
    await fire_sync(
        sync_hook,
        store=store,
        item_uid=uid,
        change=SYNC_CHANGE_REMOVE,
        version=deleted.current_version,
        ok_status=SYNC_REMOVED,
    )
    return ItemDeletedEnvelope(trace_id=trace_id, data=ItemDeletedData(uid=uid, deleted=True))

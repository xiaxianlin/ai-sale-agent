# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-KB-031 API 密钥治理（签发/校验/吊销/轮换/IP 白名单）——平台基座实现。

依据：docs/07-实现方案/KB/SR-KB-031-API鉴权与配额.md
- §2.2 密钥治理闭环：``kbk_`` 前缀 256bit 随机密钥，SHA-256 哈希存储，明文仅签发时一次性返回；
- §6.1 密钥生命周期：issue/revoke/rotate + 治理操作审计事件（哈希链落地归 SR-NFR-SEC-03）；
- §6.2 鉴权中间件的注册表查询部分：哈希查注册表 → 状态校验 → IP 白名单 → 消费方上下文；
- §4 数据模型 api_consumer（本文件为其内存形态 ConsumerRecord；PG 实现随服务侧落地）。

平台基座范围：密钥生命周期 + 存储抽象 + 内存实现（InMemoryKeyStore）；
生产形态（PG 唯一约束 / Redis 缓存版本号孤儿化）随 kb-service 落地，接口契约在此冻结。

轮换口径（SR §2.2 偏差标注的基座形态）：生产 PG 形态因 UNIQUE(consumer) 采用无重叠期立即轮换；
本基座提供**可配置宽限窗口**（default_grace_seconds / rotate(grace_seconds=...)，默认 0 = 立即失效），
宽限期内旧哈希仍可鉴权（previous_key_hash / previous_key_valid_until），供内网平滑换键场景取用。
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Iterator, Mapping

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- 常量与错误体系


KEY_PREFIX: str = "kbk_"
"""密钥前缀：便于日志与界面脱敏正则识别（kbk_****ab12，SR §2.2/NFR-SEC-04）。"""

KEY_RANDOM_BYTES: int = 32
"""随机字节数：token_urlsafe(32) = 256bit 熵（SR §2.2）。"""

MIN_KEY_LENGTH: int = 20
"""明文最短长度：低于此长度直接判无效（畸形/截断探测，SR §6.2 ①）。"""

DEFAULT_QPS: int = 20
"""新调用方缺省 QPS（SR §7 KB_API_QUOTA_DEFAULT_QPS；按 Key 差异化配额存 ConsumerRecord.quota）。"""

DEFAULT_DAILY: int = 50000
"""新调用方缺省日配额（SR §7 KB_API_QUOTA_DEFAULT_DAILY）。"""


class ApiKeyError(Exception):
    """API Key 治理基础异常：携带业务码 / HTTP 状态 / 统一文案（对齐 04-接口设计 §1.3）。"""

    code: int = 50001
    http_status: int = 500
    message: str = "API Key 治理内部错误"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message


class InvalidApiKeyError(ApiKeyError):
    """API Key 无效或缺失（40101/401）——缺头/格式错/错误 Key 统一此文案，防密钥探测（SR §6.2 ①）。"""

    code = 40101
    http_status = 401
    message = "API Key 无效或缺失"


class RevokedKeyError(InvalidApiKeyError):
    """已吊销 Key（40101）——与「无效」同文案同码（防探测）；子类仅供治理侧/测试区分归因。"""

    message = "API Key 无效或缺失"


class RotatedKeyExpiredError(InvalidApiKeyError):
    """轮换旧 Key 宽限期已过（40101）——同文案同码；子类供宽限窗口测试与告警归因。"""

    message = "API Key 无效或缺失"


class IpWhitelistDeniedError(ApiKeyError):
    """来源 IP 不在白名单（40301/403，06-安全设计 §3 越权写审计）。"""

    code = 40301
    http_status = 403
    message = "来源 IP 不在白名单"


class UnknownConsumerError(ApiKeyError):
    """调用方不存在（40001/400，治理操作入参校验，SR §6.1 revoke/rotate 分支）。"""

    code = 40001
    http_status = 400
    message = "调用方不存在"


class DuplicateConsumerError(ApiKeyError):
    """调用方已存在（40001/400）：UNIQUE(consumer) 约束的应用层等价物（SR §4）。"""

    code = 40001
    http_status = 400
    message = "调用方已存在"


def hash_key(plain: str) -> str:
    """密钥哈希：SHA-256 hexdigest——库存哈希不可逆，明文永不在库（NFR-SEC-04 口径）。"""
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    """生成 ``kbk_`` 前缀 256bit 随机密钥（secrets 密码学随机，SR §2.2）。"""
    return KEY_PREFIX + secrets.token_urlsafe(KEY_RANDOM_BYTES)


# --------------------------------------------------------------------------- 记录与上下文模型


class KeyStatus:
    """密钥状态闭枚举（SR §4 CHECK 约束 status IN ('active','revoked')）。"""

    ACTIVE = "active"
    REVOKED = "revoked"


class QuotaConfig(BaseModel):
    """按 Key 独立配置的限流配额（SR §4 quota JSONB；缺省取 SR §7 全局默认）。"""

    model_config = ConfigDict(frozen=True)

    qps: int = Field(default=DEFAULT_QPS, ge=1, le=1000)        # 缺省 QPS（SR §7 取值范围 1~1000）
    daily: int = Field(default=DEFAULT_DAILY, ge=1, le=1_000_000)  # 缺省日配额（1000~100 万）


class ConsumerRecord(BaseModel):
    """调用方注册表行（api_consumer 表行的内存形态，SR §4）。

    不可变模型：状态迁移（吊销/轮换/更新 last_used_at）经 model_copy(update=...) 整行替换，
    保证注册表内无部分更新的中间态；生产 PG 形态由事务保证同等原子性。
    """

    model_config = ConfigDict(frozen=True)

    consumer: str                                  # 调用方标识（training-service / agent-core / site-service…）
    api_key_hash: str                              # SHA-256(密钥)，明文不入库
    key_tail: str                                  # 末 4 位，界面脱敏展示 kbk_****ab12
    quota: QuotaConfig = Field(default_factory=QuotaConfig)
    ip_whitelist: tuple[str, ...] = ()             # 可选 IP 白名单；空 = 不限制（04 §1.2）
    status: str = KeyStatus.ACTIVE                 # active / revoked
    tenant_id: int = 1                             # P0-P4 恒为 1（SR §4）
    previous_key_hash: str | None = None           # 轮换旧哈希：宽限窗口内仍可鉴权（本文件头口径说明）
    previous_key_valid_until: datetime | None = None  # 旧哈希宽限截止（UTC 感知时间；None=已无宽限）
    created_at: datetime | None = None             # 创建时间（UTC）
    created_by: str = "admin"                      # 签发操作人（审计归因）
    revoked_at: datetime | None = None             # 吊销时间线（泄露应急追责，SR §2.2）
    rotated_at: datetime | None = None             # 最近轮换时间线
    last_used_at: datetime | None = None           # 最近鉴权成功时间（管理端列表展示，SR §5）


@dataclass(frozen=True)
class IssuedKey:
    """签发结果：明文密钥仅此一次返回（SR §5 签发响应示例）。"""

    consumer: str
    api_key: str                                   # 一次性明文（调用方立即复制保存）
    key_tail: str                                  # 末 4 位（脱敏展示用）
    quota: QuotaConfig
    notice: str = "请立即复制保存，关闭后无法再次查看"


@dataclass(frozen=True)
class RotatedKey:
    """轮换结果：新明文一次性返回 + 旧 Key 宽限期信息（可配宽限窗口）。"""

    consumer: str
    api_key: str                                   # 新明文（立即生效）
    old_key_valid_until: datetime | None           # 旧 Key 宽限截止；None = 立即失效（SR §2.2 生产口径）
    grace_seconds: float                           # 本次宽限窗口（秒）


@dataclass(frozen=True)
class ConsumerContext:
    """消费方上下文：鉴权通过后注入 request.state.consumer / 依赖返回值（SR §6.2）。

    不复用 platform/core/security.Principal 的原因：Principal 是 JWT/RBAC 形态
    （roles/perms/scope 展开自角色矩阵），服务间 API Key 主体没有用户角色语义，
    独立轻量上下文避免伪造 perms 与矩阵耦合；需要用户主体时仍走 JWT 通道。
    """

    consumer: str                                  # 调用方标识
    tenant_id: int = 1                             # 租户边界（P0-P4 恒为 1）
    qps: int = DEFAULT_QPS                         # 该 Key 独立 QPS 配额
    daily: int = DEFAULT_DAILY                     # 该 Key 独立日配额
    ip_whitelist: tuple[str, ...] = ()             # 白名单快照（审计/日志归因用）
    key_tail: str = ""                             # 末 4 位（日志脱敏定位）


# --------------------------------------------------------------------------- 存储抽象（SR §6.2 回源锚点）


class KeyStore(ABC):
    """注册表存储抽象：save 整行替换 / 三种哈希定位 / 按调用方读取。

    生产 PG 形态（SR §4）：api_consumer 表 + UNIQUE(consumer) + api_key_hash 索引；
    Redis 镜像缓存（kb:apicons:{ver}，60s TTL，版本号孤儿化失效）是查询前置层，
    对本抽象透明——缓存命中语义由部署侧组合，不改变存储契约。
    """

    @abstractmethod
    def save(self, record: ConsumerRecord) -> None:
        """整行写入（新建/替换，等价 INSERT .. ON CONFLICT (consumer) DO UPDATE 的应用层形态）。"""

    @abstractmethod
    def find_by_key_hash(self, key_hash: str) -> ConsumerRecord | None:
        """按当前生效哈希定位（鉴权主路径；PG 形态走 Redis 镜像 + miss 回源）。"""

    @abstractmethod
    def find_by_previous_key_hash(self, key_hash: str) -> ConsumerRecord | None:
        """按轮换旧哈希定位（宽限窗口内旧 Key 鉴权路径；返回 None 即旧哈希无主）。"""

    @abstractmethod
    def find_by_consumer(self, consumer: str) -> ConsumerRecord | None:
        """按调用方标识读取（治理操作 revoke/rotate 入口）。"""

    @abstractmethod
    def iter_records(self) -> Iterator[ConsumerRecord]:
        """全量迭代（管理端列表/测试；生产为分页查询）。"""


class InMemoryKeyStore(KeyStore):
    """内存存储实现（单元测试与本地开发用；线程安全）。

    结构：dict[consumer, ConsumerRecord]；哈希定位为线性扫描——注册表规模为
    「服务间调用方」量级（个位数到几十），扫描成本可忽略，换取实现简单可审计。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, ConsumerRecord] = {}

    def save(self, record: ConsumerRecord) -> None:
        with self._lock:
            self._records[record.consumer] = record

    def find_by_key_hash(self, key_hash: str) -> ConsumerRecord | None:
        with self._lock:
            for record in self._records.values():
                if record.api_key_hash == key_hash:
                    return record
        return None

    def find_by_previous_key_hash(self, key_hash: str) -> ConsumerRecord | None:
        with self._lock:
            for record in self._records.values():
                if record.previous_key_hash == key_hash:
                    return record
        return None

    def find_by_consumer(self, consumer: str) -> ConsumerRecord | None:
        with self._lock:
            return self._records.get(consumer)

    def iter_records(self) -> Iterator[ConsumerRecord]:
        with self._lock:
            return iter(tuple(self._records.values()))  # 快照迭代，避免遍历期间并发替换干扰

    def size(self) -> int:
        """注册表行数（测试观测用）。"""
        with self._lock:
            return len(self._records)


# --------------------------------------------------------------------------- 密钥注册表（SR §6.1 生命周期）


class KeyRegistry:
    """密钥生命周期治理：签发 / 校验 / 吊销 / 轮换（宽限窗口可配）/ IP 白名单绑定。

    时钟注入：clock 返回 UTC 感知时间（宽限窗口与时间线判定的时间源），
    测试注入假时钟做确定性边界验证；生产使用默认 datetime.now(UTC)。
    """

    def __init__(
        self,
        store: KeyStore | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        default_grace_seconds: float = 0.0,
    ) -> None:
        self._store = store if store is not None else InMemoryKeyStore()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.default_grace_seconds = max(0.0, default_grace_seconds)

    @property
    def store(self) -> KeyStore:
        """底层存储（只读检索/测试观测入口）。"""
        return self._store

    def now(self) -> datetime:
        """统一时间源（UTC 感知）。"""
        return self._clock()

    # -- 签发（SR §6.1 issue） ----------------------------------------------

    def issue(
        self,
        consumer: str,
        *,
        quota: QuotaConfig | Mapping[str, int] | None = None,
        ip_whitelist: Iterable[str] = (),
        operator: str = "admin",
    ) -> IssuedKey:
        """签发：生成 256bit 随机密钥 → 哈希 + 末 4 位入库；明文仅本次返回（§2.2）。

        - 配额按 Key 独立配置（限流配置化，04 §1.2），缺省取 SR §7 全局默认；
        - IP 白名单可选绑定：空 = 不限制；非空 = 精确匹配来源 IP；
        - 重复 consumer 抛 DuplicateConsumerError（UNIQUE(consumer) 应用层等价）；
        - 审计事件（api_key_issue，只记哈希不落明文）由依赖方经写入钩子联动，见 dependency.py。
        """
        if not consumer or not consumer.strip():
            raise UnknownConsumerError("调用方标识不能为空")
        if self._store.find_by_consumer(consumer) is not None:
            raise DuplicateConsumerError(f"调用方已存在: {consumer}")
        plain = generate_api_key()
        resolved_quota = self._resolve_quota(quota)
        record = ConsumerRecord(
            consumer=consumer,
            api_key_hash=hash_key(plain),
            key_tail=plain[-4:],
            quota=resolved_quota,
            ip_whitelist=tuple(ip_whitelist),
            created_at=self.now(),
            created_by=operator,
        )
        self._store.save(record)
        return IssuedKey(consumer=consumer, api_key=plain, key_tail=record.key_tail, quota=resolved_quota)

    # -- 校验（SR §6.2 verify 的注册表部分） --------------------------------

    def verify(self, api_key: str, *, client_ip: str | None = None) -> ConsumerContext:
        """校验明文密钥 → 消费方上下文；失败抛 40101/40301 族异常。

        判定顺序（SR §6.2 ①~③）：
        ① 形态预检：空/过短直接 40101（与无效同文案，防探测）；
        ② 哈希定位：当前哈希 → 宽限期内旧哈希（轮换窗口）→ 均未命中 40101；
           已吊销 40101；旧哈希宽限期已过 40101（RotatedKeyExpiredError 归因）；
        ③ IP 白名单：非空且来源 IP 不在名单 → 40301（IpWhitelistDeniedError）。

        client_ip 为 None（如无对端信息）时按「不在白名单」处理——fail-closed。
        """
        if not api_key or len(api_key) < MIN_KEY_LENGTH:
            raise InvalidApiKeyError()
        key_hash = hash_key(api_key)

        record = self._store.find_by_key_hash(key_hash)
        via_previous = False
        if record is None:
            record = self._store.find_by_previous_key_hash(key_hash)
            via_previous = record is not None
            if record is not None:
                until = record.previous_key_valid_until
                if until is None or self.now() > until:
                    raise RotatedKeyExpiredError()  # 宽限期已过：旧 Key 彻底失效
        if record is None:
            raise InvalidApiKeyError()
        if record.status != KeyStatus.ACTIVE:
            raise RevokedKeyError()  # 吊销后旧/新哈希一并失效（同一行 status）

        if record.ip_whitelist:
            if client_ip is None or client_ip not in record.ip_whitelist:
                raise IpWhitelistDeniedError(f"来源 IP 不在白名单: consumer={record.consumer}, ip={client_ip!r}")

        # last_used_at 更新（管理端展示）：整行替换，鉴权失败路径不更新
        self._store.save(record.model_copy(update={"last_used_at": self.now()}))
        return ConsumerContext(
            consumer=record.consumer,
            tenant_id=record.tenant_id,
            qps=record.quota.qps,
            daily=record.quota.daily,
            ip_whitelist=record.ip_whitelist,
            # 旧哈希通道不回填新 tail（旧 Key 明文已不可考，防日志误导归因）
            key_tail=record.key_tail if not via_previous else "legacy",
        )

    # -- 吊销（SR §6.1 revoke：泄露应急一键吊销） ---------------------------

    def revoke(self, consumer: str, *, operator: str = "admin", reason: str = "") -> ConsumerRecord:
        """一键吊销：status='revoked' 单行替换 + revoked_at 时间线；新请求立即 40101。

        - 不存在或已吊销抛 UnknownConsumerError（幂等拒绝，SR §6.1 分支）；
        - reason 为泄露应急追责线索（NFR-SEC-03 事后审计），由审计钩子联动落链。
        """
        record = self._require(consumer)
        if record.status != KeyStatus.ACTIVE:
            raise UnknownConsumerError(f"调用方不存在或已吊销: {consumer}")
        revoked = record.model_copy(
            update={
                "status": KeyStatus.REVOKED,
                "revoked_at": self.now(),
                "previous_key_hash": None,       # 吊销即彻底失效：宽限期旧哈希一并清除
                "previous_key_valid_until": None,
            }
        )
        self._store.save(revoked)
        return revoked

    # -- 轮换（SR §6.1 rotate + 本文件头宽限窗口口径） ----------------------

    def rotate(
        self,
        consumer: str,
        *,
        operator: str = "admin",
        grace_seconds: float | None = None,
    ) -> RotatedKey:
        """轮换：替换密钥哈希，返回新明文；旧哈希进入可配宽限窗口。

        - grace_seconds：本次宽限（秒）；None = 取 registry.default_grace_seconds；
          0 = 立即失效（SR §2.2 生产 PG 口径：UNIQUE(consumer) 无重叠期）；
        - 宽限期内旧 Key 仍可鉴权（内网平滑换键），过期后 RotatedKeyExpiredError；
        - 连续两次轮换：上一轮旧哈希立即失效（previous_key_hash 被覆盖，仅保留最近一代）；
        - 已吊销调用方不允许轮换（先恢复治理流程再签发新键，避免绕过吊销审计）。
        """
        record = self._require(consumer)
        if record.status != KeyStatus.ACTIVE:
            raise UnknownConsumerError(f"调用方不存在或已吊销: {consumer}")
        grace = self.default_grace_seconds if grace_seconds is None else max(0.0, grace_seconds)
        plain = generate_api_key()
        rotated_at = self.now()
        valid_until = rotated_at + timedelta(seconds=grace) if grace > 0 else None
        rotated = record.model_copy(
            update={
                "api_key_hash": hash_key(plain),
                "key_tail": plain[-4:],
                "previous_key_hash": record.api_key_hash,   # 旧哈希进入宽限通道
                "previous_key_valid_until": valid_until,
                "rotated_at": rotated_at,
                "created_by": operator,                      # 最近治理操作人（updated_by 语义）
            }
        )
        self._store.save(rotated)
        return RotatedKey(
            consumer=consumer, api_key=plain, old_key_valid_until=valid_until, grace_seconds=grace
        )

    # -- 查询辅助（管理端列表/测试） -----------------------------------------

    def get(self, consumer: str) -> ConsumerRecord | None:
        """按调用方读取注册表行（管理端列表脱敏展示数据源，SR §5）。"""
        return self._store.find_by_consumer(consumer)

    def _require(self, consumer: str) -> ConsumerRecord:
        record = self._store.find_by_consumer(consumer)
        if record is None:
            raise UnknownConsumerError(f"调用方不存在: {consumer}")
        return record

    @staticmethod
    def _resolve_quota(quota: QuotaConfig | Mapping[str, int] | None) -> QuotaConfig:
        """配额入参归一：QuotaConfig 原样 / dict 展开校验 / None 取缺省。"""
        if quota is None:
            return QuotaConfig()
        if isinstance(quota, QuotaConfig):
            return quota
        return QuotaConfig(**dict(quota))

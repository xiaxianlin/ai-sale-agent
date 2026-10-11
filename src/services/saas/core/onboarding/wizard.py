# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""租户开通向导与状态机（FR-SAAS-002，SR-SAAS-002 §5/§6.1/§6.4）。

核心口径（SR §2「配置即数据」）：新租户接入只产生数据行，代码路径零分叉；
- 四步配置：company_info / kb_init / accounts / strategy，仅 configuring 态可编辑，
  同一步重复提交=整体覆盖写（无部分写入半态）；
- 状态机：configuring → initializing → active，failed 为可重试回退态
  （failed → initializing 经 retry_kb）；非法迁移一律 40001；
- activate 幂等（Idempotency-Key → 进程内幂等账本，生产 Redis TTL 24h）；
- 资源前置预估 + 配额硬阻断（42901，fail-fast 在 activate 之前）；
- 企业名称唯一（并发恰一胜，锁内登记）；账号凭证提交即加密（demo XOR+Base64，
  生产 AES-256-GCM + KMS，NFR-SEC-04），回显仅掩码；
- 预分配 tenant_id（OSS 前缀/隔离键需要稳定 id）。

存储为进程内形态（demo）：生产替换 PG tenant_onboarding 行 + Redis 键，接口不变。
"""
from __future__ import annotations

import base64
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from services.saas.core.onboarding.kb_init import KbInitService
from services.saas.core.onboarding.templates import TemplateService

# --------------------------------------------------------------------------- 常量（口径锚点）

#: 业务错误码（04-接口设计 §1.3 + SR-SAAS-002 §5）
CODE_PARAM: int = 40001       # 状态违规/差异项缺失/时区语言非法/四步未齐即激活
CODE_QUOTA: int = 42901       # 资源预估超套餐配额
CODE_NOT_FOUND: int = 40401   # 向导/任务不存在

#: 开通状态机（SR §6.1：configuring→initializing→active；failed 可重试回退）
STATUS_CONFIGURING: str = "configuring"
STATUS_INITIALIZING: str = "initializing"
STATUS_ACTIVE: str = "active"
STATUS_FAILED: str = "failed"
ONBOARDING_STATUSES: tuple[str, ...] = (
    STATUS_CONFIGURING, STATUS_INITIALIZING, STATUS_ACTIVE, STATUS_FAILED)

#: 合法迁移边（非法组合一律拒绝且不留历史）
TRANSITIONS: dict[tuple[str, str], str] = {
    (STATUS_CONFIGURING, STATUS_INITIALIZING): STATUS_INITIALIZING,  # 激活
    (STATUS_INITIALIZING, STATUS_ACTIVE): STATUS_ACTIVE,            # 初始化全部成功
    (STATUS_INITIALIZING, STATUS_FAILED): STATUS_FAILED,            # 部分失败回退
    (STATUS_FAILED, STATUS_INITIALIZING): STATUS_INITIALIZING,      # 人工修复重试
}

#: 四步配置（顺序即向导页序，SR §6.5 接入清单）
STEPS: tuple[str, ...] = ("company_info", "kb_init", "accounts", "strategy")

#: 第①步值域（NFR-USA-03 引导式校验；demo 白名单，生产读时区/语言注册表）
ALLOWED_TIMEZONES: tuple[str, ...] = ("Asia/Shanghai", "UTC", "America/New_York",
                                      "Europe/London")
ALLOWED_LANGUAGES: tuple[str, ...] = ("zh-CN", "en", "es")

#: 第②步素材约束（OSS 前缀隔离 NFR-SEC-07；格式对齐 P0 流水线解析器）
OSS_KEY_PREFIX: str = "t:{tenant_id}/raw/"
ALLOWED_FORMATS: tuple[str, ...] = ("pdf", "docx", "xlsx", "csv", "md", "txt", "png", "jpg")
MATERIALS_MAX: int = 1000

#: 第③步平台账号（P4/P5 主口径：阿里国际站；代理绑定 SR-AG-001）
ALLOWED_PLATFORMS: tuple[str, ...] = ("alibaba",)

#: 预估系数（SR §7 配置默认：est_text_ratio=0.35 / est_embed_tps=120 / 千字符一块）
EST_TEXT_RATIO: float = 0.35
EST_EMBED_TPS: float = 120.0
CHARS_PER_CHUNK: int = 1000
VEC_KB_PER_CHUNK: float = 4.5  # 单 chunk≈4.5KB（03-数据库设计 §4.3）


# --------------------------------------------------------------------------- 错误与数据模型


class OnboardingError(Exception):
    """开通域业务错误（40001/42901/40401，message 面向运营中文可读）。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class QuotaExceeded(OnboardingError):
    """资源预估超套餐配额（42901，提示经 FR-SAAS-003 升配额）。"""

    def __init__(self, message: str) -> None:
        super().__init__(CODE_QUOTA, message)


@dataclass(frozen=True)
class QuotaPolicy:
    """套餐配额策略（FR-SAAS-003 联动；进程内形态，生产读 tenant.quota）。"""

    plan: str
    storage_gb: float
    kb_max_chunks: int
    max_accounts: int


#: standard 套餐默认配额（SR-SAAS-002 §6.4 口径：存储 1GB / 5 万块 / 4 账号）
DEFAULT_QUOTA = QuotaPolicy(plan="standard", storage_gb=1.0, kb_max_chunks=50000,
                            max_accounts=4)


@dataclass
class Onboarding:
    """开通向导实例（tenant_onboarding 行的进程内形态）。"""

    uid: str
    tenant_id: int
    plan: str
    status: str = STATUS_CONFIGURING
    steps: dict[str, dict] = field(default_factory=dict)
    steps_done: list[str] = field(default_factory=list)
    state_hist: list[dict] = field(default_factory=list)  # [{from,to,at,by,detail}]
    kb_task_uid: str | None = None
    company_name: str | None = None
    audit: list[dict] = field(default_factory=list)  # NFR-SEC-03 审计留痕（进程内）


@dataclass(frozen=True)
class StepResult:
    """步骤提交结果（向导页联动渲染）。"""

    step: str
    all_done: bool
    steps_done: tuple[str, ...]


@dataclass(frozen=True)
class Estimate:
    """资源预估（SR §6.4：chunks/向量存储/OSS/嵌入耗时）。"""

    chunks: int
    vec_gb: float
    oss_gb: float
    embed_minutes: float

    def as_dict(self) -> dict:
        return {"chunks": self.chunks, "vec_gb": round(self.vec_gb, 4),
                "oss_gb": round(self.oss_gb, 4), "embed_minutes": round(self.embed_minutes, 2)}


@dataclass(frozen=True)
class ActivateResult:
    """激活结果（幂等重放返回同一份）。"""

    uid: str
    tenant_id: int
    kb_task_uid: str
    cfg_rows: int
    estimate: Estimate
    status: str = STATUS_INITIALIZING


# --------------------------------------------------------------------------- 凭证加密（demo）


class XorCredentialCipher:
    """凭证加密 demo 形态：XOR 流密码 + Base64（生产替换 AES-256-GCM + KMS，NFR-SEC-04）。

    契约与生产一致：encrypt 单向落库、mask 仅回显指纹前缀，杜绝明文出接口。
    """

    _KEY: bytes = b"asa-saas-demo-key"  # demo 固定密钥；生产由 KMS 托管（06-安全设计 §4）

    def encrypt(self, plaintext: str) -> str:
        raw = bytes(b ^ k for b, k in zip(plaintext.encode("utf-8"),
                                          (self._KEY * ((len(plaintext) // len(self._KEY)) + 1))))
        return "enc:v1:" + base64.b64encode(raw).decode("ascii")

    def mask(self, ciphertext: str) -> str:
        """掩码回显：仅保留密文摘要前 6 位，杜绝明文/完整密文出接口。"""
        digest = base64.b64encode(ciphertext.encode("utf-8")).decode("ascii")[:6]
        return f"<enc:{digest}…>"


# --------------------------------------------------------------------------- 进程内存储


class InMemoryOnboardingStore:
    """进程内向导存储（demo）：tenant_id 预分配、企业名唯一、幂等账本，锁内串行。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_uid: dict[str, Onboarding] = {}
        self._next_tenant_id: int = 7000  # 预分配租户 id（隔离键/OSS 前缀稳定）
        self._company_names: set[str] = set()
        self._idem: dict[str, ActivateResult] = {}

    def create(self, plan: str) -> Onboarding:
        """创建向导：预分配 tenant_id（SR §2 决策①）。"""
        with self._lock:
            ob = Onboarding(uid=f"ob_{uuid.uuid4().hex[:12]}",
                            tenant_id=self._next_tenant_id, plan=plan)
            self._next_tenant_id += 1
            self._by_uid[ob.uid] = ob
            return ob

    def get(self, uid: str) -> Onboarding | None:
        return self._by_uid.get(uid)

    def must_get(self, uid: str) -> Onboarding:
        ob = self._by_uid.get(uid)
        if ob is None:
            raise OnboardingError(CODE_NOT_FOUND, f"开通向导不存在：{uid}")
        return ob

    def save(self, ob: Onboarding) -> None:
        with self._lock:
            self._by_uid[ob.uid] = ob

    def register_company_name(self, name: str, uid: str) -> None:
        """企业名唯一登记：并发恰一胜（锁内检查+占位），重复即 40001。"""
        with self._lock:
            if name in self._company_names:
                raise OnboardingError(CODE_PARAM, f"企业名称已存在：{name}")
            self._company_names.add(name)

    def idem_get(self, key: str) -> ActivateResult | None:
        return self._idem.get(key)

    def idem_put(self, key: str, result: ActivateResult) -> None:
        """幂等账本写入（生产 Redis saas:onboarding:idem:{key}，TTL 86400s）。"""
        with self._lock:
            self._idem.setdefault(key, result)


# --------------------------------------------------------------------------- 向导本体


class OnboardingWizard:
    """租户开通向导：四步配置 + 状态机 + 幂等激活（SR-SAAS-002 §6.1）。

    仅 configuring 态可编辑；activate 后的变更走 FR-SAAS-003 运营后台常规配置接口
    （本向导职责到此为止）。全程只有数据写入，无代码变更。
    """

    STEPS: tuple[str, ...] = STEPS

    def __init__(self, *, templates: TemplateService | None = None,
                 quota_policy: QuotaPolicy = DEFAULT_QUOTA,
                 cipher: XorCredentialCipher | None = None,
                 kb_init: KbInitService | None = None) -> None:
        self.store = InMemoryOnboardingStore()
        self.templates = templates or TemplateService()
        self.quota = quota_policy
        self.cipher = cipher or XorCredentialCipher()
        # kb_init 反向持有 transit 回调（initializing→active/failed 由任务收敛）
        self.kb_init = kb_init or KbInitService(on_transit=self.transit)

    # -- 创建与状态迁移 ----------------------------------------------------

    def create(self, plan: str = "standard", actor: str = "ops-admin") -> Onboarding:
        """创建向导（POST /onboarding）：预分配 tenant_id，初始 configuring。"""
        ob = self.store.create(plan)
        self._audit(ob, actor, "onboarding_create", {"plan": plan})
        return ob

    def transit(self, uid: str, from_status: str, to_status: str, by: str = "system",
                detail: dict | None = None) -> Onboarding:
        """状态迁移守卫：非法组合 40001 且不留历史（SR §6.1 状态机）。"""
        ob = self.store.must_get(uid)
        with self.store._lock:  # 迁移与读取同锁，防并发迁移竞态
            if ob.status != from_status or TRANSITIONS.get((from_status, to_status)) != to_status:
                raise OnboardingError(
                    CODE_PARAM, f"非法状态迁移 {from_status}→{to_status}（当前 {ob.status}）")
            ob.state_hist.append({"from": from_status, "to": to_status,
                                  "at": _now_iso(), "by": by, "detail": detail or {}})
            ob.status = to_status
        return ob

    # -- 四步提交 ----------------------------------------------------------

    def submit_step(self, uid: str, step: str, payload: dict,
                    actor: str = "ops-admin") -> StepResult:
        """步骤快照整体覆盖写（幂等：同一步重复提交=覆盖，无半态）。"""
        ob = self.store.must_get(uid)
        if step not in STEPS:
            raise OnboardingError(CODE_PARAM, f"未知步骤：{step}")
        if ob.status != STATUS_CONFIGURING:
            raise OnboardingError(CODE_PARAM, f"状态 {ob.status} 不允许修改配置")
        validator = getattr(self, f"_validate_{step}", None)
        if validator is None:  # 防御：步骤注册表与校验器失配（不达达路径）
            raise OnboardingError(CODE_PARAM, f"步骤缺少校验器：{step}")
        snapshot = validator(ob, payload)  # 校验失败即 40001，不产生部分写入
        with self.store._lock:
            ob.steps[step] = snapshot
            if step not in ob.steps_done:
                ob.steps_done.append(step)
        self._audit(ob, actor, "onboarding_step", {"step": step})
        return StepResult(step=step, all_done=len(ob.steps_done) == len(STEPS),
                          steps_done=tuple(ob.steps_done))

    def _validate_company_info(self, ob: Onboarding, payload: dict) -> dict:
        """①企业信息：名称/品牌/时区/语言（SR §5①；值域白名单校验）。"""
        name = payload.get("name")
        if not isinstance(name, str) or not (1 <= len(name) <= 64):
            raise OnboardingError(CODE_PARAM, "企业名称须为 1-64 字符")
        self.store.register_company_name(name, ob.uid)  # 并发同企业名恰一胜
        ob.company_name = name  # 台账回填（演示/清单展示用）
        brand = payload.get("brand")
        if not isinstance(brand, dict) or not brand.get("logo", "").startswith("https://"):
            raise OnboardingError(CODE_PARAM, "品牌 logo 须为 https URL")
        if not _is_hex_color(brand.get("primary", "")):
            raise OnboardingError(CODE_PARAM, f"品牌主色非法（须 #RRGGBB）：{brand.get('primary')}")
        if payload.get("timezone") not in ALLOWED_TIMEZONES:
            raise OnboardingError(CODE_PARAM, f"时区非法：{payload.get('timezone')}")
        if payload.get("language") not in ALLOWED_LANGUAGES:
            raise OnboardingError(CODE_PARAM, f"语言非法：{payload.get('language')}")
        return {"name": name, "brand": dict(brand), "timezone": payload["timezone"],
                "language": payload["language"]}

    def _validate_kb_init(self, ob: Onboarding, payload: dict) -> dict:
        """②素材登记：OSS 直传后登记（本租户前缀强校验，NFR-SEC-07）。"""
        materials = payload.get("materials")
        if not isinstance(materials, list) or not materials:
            raise OnboardingError(CODE_PARAM, "素材清单不能为空")
        if len(materials) > MATERIALS_MAX:
            raise OnboardingError(CODE_PARAM, f"素材数超上限（{MATERIALS_MAX}）")
        prefix = OSS_KEY_PREFIX.format(tenant_id=ob.tenant_id)
        cleaned: list[dict] = []
        for m in materials:
            if not str(m.get("oss_key", "")).startswith(prefix):
                raise OnboardingError(CODE_PARAM,
                                      f"oss_key 必须在本租户前缀 {prefix} 下：{m.get('oss_key')}")
            if not isinstance(m.get("size"), (int, float)) or m["size"] <= 0:
                raise OnboardingError(CODE_PARAM, "素材 size 须为正数")
            if m.get("format") not in ALLOWED_FORMATS:
                raise OnboardingError(CODE_PARAM, f"素材格式非法：{m.get('format')}")
            cleaned.append({"oss_key": m["oss_key"], "size": int(m["size"]),
                            "format": m["format"]})
        return {"materials": cleaned}

    def _validate_accounts(self, ob: Onboarding, payload: dict) -> dict:
        """③账号参数：提交即加密（快照仅存密文+掩码），账号数受套餐配额约束。"""
        accounts = payload.get("accounts")
        if not isinstance(accounts, list) or not accounts:
            raise OnboardingError(CODE_PARAM, "账号清单不能为空")
        if len(accounts) > self.quota.max_accounts:
            raise OnboardingError(
                CODE_PARAM,
                f"账号数 {len(accounts)} 超套餐上限（{self.quota.max_accounts}），请升配额")
        cleaned: list[dict] = []
        for acc in accounts:
            if acc.get("platform") not in ALLOWED_PLATFORMS:
                raise OnboardingError(CODE_PARAM, f"平台非法：{acc.get('platform')}")
            username = acc.get("username")
            if not isinstance(username, str) or not (1 <= len(username) <= 64):
                raise OnboardingError(CODE_PARAM, "账号用户名须为 1-64 字符")
            credential = acc.get("credential")
            if not isinstance(credential, str) or len(credential) < 6:
                raise OnboardingError(CODE_PARAM, "凭证长度不足（≥6）")
            # 提交即加密：快照只存密文与掩码（明文即用即弃，NFR-SEC-04）
            enc = self.cipher.encrypt(credential)
            cleaned.append({"platform": acc["platform"], "username": username,
                            "credential_enc": enc,
                            "credential_masked": self.cipher.mask(enc),
                            "proxy_id": acc.get("proxy_id")})
        return {"accounts": cleaned}

    def _validate_strategy(self, ob: Onboarding, payload: dict) -> dict:
        """④策略参数：差异项必填校验（SR §6.3，模板预填 + 仅改差异项）。"""
        self.templates.validate_required(payload)
        return dict(payload)

    # -- 激活 / 进度 / 重试 --------------------------------------------------

    def activate(self, uid: str, idem_key: str, actor: str = "ops-admin") -> ActivateResult:
        """提交开通：幂等 → 齐备/配额校验 → 物化（模板复制）→ 触发初始化任务。"""
        if not idem_key:
            raise OnboardingError(CODE_PARAM, "缺少幂等键 Idempotency-Key")
        if (prev := self.store.idem_get(idem_key)) is not None:
            return prev  # 幂等重放：返回首次结果，无重复任务
        ob = self.store.must_get(uid)
        if ob.status != STATUS_CONFIGURING:
            raise OnboardingError(CODE_PARAM, "仅 configuring 态可激活（防重复激活）")
        missing = [s for s in STEPS if s not in ob.steps_done]
        if missing:
            raise OnboardingError(CODE_PARAM, f"步骤未完成：{missing}")
        est = self.estimate(ob.steps["kb_init"]["materials"])
        self._assert_within_quota(est)
        # 单事务物化（demo 顺序写）：品牌与配额归 tenant 行，模板复制 app_config，
        # 账号密文已随第③步快照落库——全程只有数据写入
        cfg_rows = len(self.templates.copy_to_tenant(ob.tenant_id,
                                                     ob.steps["strategy"]))
        task = self.kb_init.start(ob)  # 异步长任务（§6.2）
        ob.kb_task_uid = task.uid  # 进度查询路由到任务（生产读 onboarding 行回写）
        self.transit(uid, STATUS_CONFIGURING, STATUS_INITIALIZING, by=actor,
                     detail={"kb_task": task.uid, "cfg_rows": cfg_rows,
                             "estimate": est.as_dict()})
        result = ActivateResult(uid=uid, tenant_id=ob.tenant_id, kb_task_uid=task.uid,
                                cfg_rows=cfg_rows, estimate=est)
        self.store.idem_put(idem_key, result)
        self._audit(ob, actor, "onboarding_activate", {"kb_task": task.uid})
        return result

    def progress(self, uid: str) -> dict:
        """进度查询（GET /progress 轮询渲染，NFR-USA-03）：状态机 + 文件粒度进度。"""
        ob = self.store.must_get(uid)
        data = {"status": ob.status, "kb": {"total_files": 0, "done_files": 0,
                                            "failed_files": 0, "percent": 0.0},
                "estimate": None, "tenant_id": ob.tenant_id, "steps_done": list(ob.steps_done)}
        if ob.kb_task_uid:
            task = self.kb_init.tasks.get(ob.kb_task_uid)
            if task is not None:
                data["kb"] = self.kb_init.progress(task.uid)
        if "kb_init" in ob.steps:
            data["estimate"] = self.estimate(
                ob.steps["kb_init"]["materials"]).as_dict()
        return data

    def retry_kb(self, uid: str, actor: str = "ops-admin") -> int:
        """failed 态重试：仅补跑 failed 文件重回 initializing（修复动作仍是纯配置）。"""
        ob = self.store.must_get(uid)
        if ob.status != STATUS_FAILED:
            raise OnboardingError(CODE_PARAM, f"仅 failed 态可重试（当前 {ob.status}）")
        reset = self.kb_init.retry_failed(uid, by=actor)
        return reset

    # -- 视图辅助 ----------------------------------------------------------

    def masked_accounts(self, uid: str) -> list[dict]:
        """账号掩码视图（接口回显口径：永不回密文/明文）。"""
        ob = self.store.must_get(uid)
        accounts = ob.steps.get("accounts", {}).get("accounts", [])
        return [{"platform": a["platform"], "username": a["username"],
                 "credential": a["credential_masked"], "proxy_id": a.get("proxy_id")}
                for a in accounts]

    def drill_report(self, uid: str) -> list[dict]:
        """零代码接入清单（GET /drill-report）：七步逐步执行结果（§6.5，与录屏对齐留证）。"""
        ob = self.store.must_get(uid)
        tid = ob.tenant_id
        done = set(ob.steps_done)
        plan = [
            (0, "POST /api/v1/saas/onboarding", "写 tenant 行（预分配 id）", True),
            (1, "PUT …/company-info", "steps 快照（名称/品牌/时区/语言）",
             "company_info" in done),
            (2, "POST …/kb/materials", f"OSS 直传 t:{tid}/raw/ 登记 + 预估回显",
             "kb_init" in done),
            (3, "PUT …/accounts", "platform_account 密文行（凭证仅掩码）",
             "accounts" in done),
            (4, "PUT …/strategy", "app_config 模板复制行（差异项覆盖）", "strategy" in done),
            (5, "POST …/activate", "队列任务 + 状态迁移（幂等键）",
             ob.status in (STATUS_INITIALIZING, STATUS_ACTIVE, STATUS_FAILED)),
            (6, "GET …/progress", "轮询至 active；检索仅命中本租户知识",
             ob.status == STATUS_ACTIVE),
        ]
        return [{"seq": seq, "api": api, "assert": assertion, "ok": ok}
                for seq, api, assertion, ok in plan]

    # -- 内部 ---------------------------------------------------------------

    def estimate(self, materials: list[dict]) -> Estimate:
        """资源预估（SR §6.4）：字节折算文本量 → chunk/向量/OSS/嵌入耗时。"""
        total_bytes = sum(m["size"] for m in materials)
        pages = sum(1 for m in materials if m["format"] in ("png", "jpg"))  # 图片按页折算
        est_chars = total_bytes * EST_TEXT_RATIO + pages * 1800
        chunks = max(1, -(-int(est_chars) // CHARS_PER_CHUNK))  # 向上取整
        return Estimate(chunks=chunks,
                        vec_gb=chunks * VEC_KB_PER_CHUNK / 2**20,
                        oss_gb=total_bytes / 2**30,
                        embed_minutes=chunks / 32 / EST_EMBED_TPS)

    def _assert_within_quota(self, est: Estimate) -> None:
        """配额硬阻断（fail-fast 在 activate 之前，42901 提示升配额）。"""
        used_gb = est.vec_gb + est.oss_gb
        if used_gb > self.quota.storage_gb or est.chunks > self.quota.kb_max_chunks:
            raise QuotaExceeded(
                f"预估 {used_gb:.2f}GB/{est.chunks} 块超套餐配额"
                f"（{self.quota.storage_gb}GB/{self.quota.kb_max_chunks} 块），请先升配额")

    def _audit(self, ob: Onboarding, actor: str, action: str, detail: dict) -> None:
        """审计留痕（NFR-SEC-03；生产写 audit_log 哈希链）。"""
        ob.audit.append({"actor": actor, "action": action, "detail": detail,
                         "at": _now_iso()})


def _is_hex_color(value: object) -> bool:
    """6 位 HEX 色值（防 rgb()/url() 注入 CSS，对齐 SR-SAAS-006 §6.1）。"""
    if not isinstance(value, str) or len(value) != 7 or not value.startswith("#"):
        return False
    return all(c in "0123456789abcdefABCDEF" for c in value[1:])


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""凭证保险库（NFR-SEC-04，SR-NFR-SEC-04 §6.1/§6.6；迭代 2.1 demo 形态）。

信封加密（demo 口径）：
- 主密钥（KEK）：环境变量 KB_AGENT_MASTER_KEY 注入（生产 = 阿里云 KMS CMK，
  永不出 KMS；本地兜底 KEK 走 Docker Secrets 文件，二者均不进 Git/镜像）；
- 数据密钥（DEK）：os.urandom(32) 生成，仅以「被主密钥包裹」的密文形态存于
  vault 实例（生产 = credential_dek 表 wrapped_dek 列）；明文仅在内存瞬时存在；
- 凭证本体：当前 active DEK 加密（AAD 绑定 tenant:account:cred_type 防密文移植）；
- demo 限定（显式标注）：优先 cryptography 的 AES-256-GCM（认证加密，与生产
  口径一致）；cryptography 不可用或 force_demo_cipher=True 时退化为
  HMAC-SHA256 派生流 XOR + HMAC 认证标签——该退化实现仅为演示信封结构与
  密钥纪律，未经标准认证，生产环境必须使用 KMS + AES-256-GCM（cryptography 库）；
- 轮换：rotate_dek() 版本化生成新 active DEK，旧版本密文保留可解（过渡期共存），
  生产由 DekRotator 全量重加密后 retire（SR §6.6）；
- 明文纪律：mask 仅回显尾 2 位；审计/日志只记 cipher_fp 指纹；zeroize 用后覆写。

明文永不回显为结构保证：本模块对外 API 无任何返回明文的读取路径（unseal 仅供
agent-runner 登录链路在内存瞬时使用）。
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass

#: 主密钥环境变量（demo 注入口；生产 = KMS CMK + Docker Secrets 兜底 KEK）
MASTER_KEY_ENV = "KB_AGENT_MASTER_KEY"

# ── 密码实现选择：优先 cryptography AES-256-GCM（生产口径），缺失退化 demo 流密码 ──
try:  # pragma: no cover - 环境相关分支
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    _HAS_CRYPTOGRAPHY = True
except ImportError:  # pragma: no cover - 无 cryptography 环境
    _HAS_CRYPTOGRAPHY = False

AES_CIPHER_NAME = "aes-256-gcm"
DEMO_CIPHER_NAME = "hmac-sha256-keystream-xor(demo)"

_KEY_LEN = 32       # AES-256 / DEK 长度（字节）
_NONCE_LEN = 12     # GCM nonce 长度
_TAG_LEN = 32       # HMAC-SHA256 认证标签长度（demo 降级实现）


# ─────────────────────────── 异常 ───────────────────────────


class VaultError(Exception):
    """保险库基础异常（中文上下文）。"""


class VaultConfigError(VaultError):
    """配置缺失：主密钥未注入（fail-fast，拒绝无密钥运行）。"""


class VaultDecryptError(VaultError):
    """解密失败：主密钥不匹配 / 密文被篡改 / AAD 绑定错位（归一中文报错）。"""


# ─────────────────────────── 密文包 ───────────────────────────


@dataclass(frozen=True)
class CipherBundle:
    """密文包（落库行形态：cipher_text/nonce/dek_version/key_id/mask_hint/cipher_fp）。

    生产对应 platform_credential 行（SR-NFR-SEC-04 §4，无任何明文机密列）。
    """

    nonce: bytes             # GCM 12B 随机 nonce（单 DEK 下不计数复用）
    ciphertext: bytes        # 密文（含认证标签）
    dek_version: int         # 加密所用 DEK 版本（轮换定位）
    aad: str                 # 行绑定上下文 tenant:account:cred_type
    cipher_fp: str           # sha256(cipher_text) 前 8 位：界面比对"改没改"，不暴露明文
    cipher_name: str         # 密码实现名（demo 降级时携带 demo 标记）


# ─────────────────────────── demo 退化实现原语 ───────────────────────────


def _keystream_xor(key: bytes, nonce: bytes, data: bytes) -> bytes:
    """HMAC-SHA256 计数器派生流 XOR（demo 限定，非标准认证加密）。

    keystream_i = HMAC(key, nonce || counter_i)，拼接后与数据逐字节异或。
    仅用于演示信封结构与密钥纪律；生产必须 AES-256-GCM。
    """
    blocks: list[bytes] = []
    counter = 0
    while sum(len(b) for b in blocks) < len(data):
        blocks.append(hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest())
        counter += 1
    stream = b"".join(blocks)[: len(data)]
    return bytes(a ^ b for a, b in zip(data, stream))


def _demo_tag(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    """demo 认证标签：HMAC-SHA256(nonce || ciphertext || aad)（防篡改/防错位）。"""
    return hmac.new(key, nonce + ciphertext + aad, hashlib.sha256).digest()


def _derive_key(secret: str | bytes, salt: bytes) -> bytes:
    """主密钥/口令 → 32 字节密钥（HMAC-SHA256 派生，demo 口径）。"""
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return hmac.new(salt, secret, hashlib.sha256).digest()


# ─────────────────────────── 凭证保险库 ───────────────────────────


class CredentialVault:
    """凭证保险库（NFR-SEC-04 demo 形态）：信封加密 + 掩码 + DEK 版本化轮换。

    demo 限定（demo-only）：密码原语为 AES-256-GCM（cryptography 可用时，与生产
    一致）或 HMAC-SHA256 派生流 XOR（demo 降级）；信封结构（主密钥包裹 DEK、
    DEK 加密本体、AAD 行绑定）与生产口径同构，密钥托管形态不同（环境变量 vs KMS）。
    """

    def __init__(
        self,
        master_key: str | bytes | None = None,
        *,
        force_demo_cipher: bool = False,
    ) -> None:
        secret = master_key if master_key is not None else os.environ.get(MASTER_KEY_ENV)
        if not secret:
            raise VaultConfigError(
                f"环境变量 {MASTER_KEY_ENV} 未配置：凭证保险库无法初始化"
                "（demo 口径 fail-fast；生产经 KMS/Docker Secrets 注入主密钥）"
            )
        self._kek = _derive_key(secret, b"agent-runner/vault/kek")
        self._use_aes = _HAS_CRYPTOGRAPHY and not force_demo_cipher
        self._wrapped_deks: dict[int, bytes] = {}      # {版本: 被主密钥包裹的 DEK}
        self._active_version = 0
        self.rotate_dek()                              # 初始版本 v1

    # ---- 写路径 ----

    def seal(self, plaintext: str | bytes, *, context: str) -> CipherBundle:
        """加密落库（写入路径唯一入口）：active DEK 加密 + AAD 行绑定 + 掩码固化。"""
        data = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext
        dek = self._unwrap_dek(self._active_version)
        nonce = os.urandom(_NONCE_LEN)
        aad = context.encode("utf-8")
        ciphertext, cipher_name = self._encrypt(dek, nonce, data, aad)
        return CipherBundle(
            nonce=nonce,
            ciphertext=ciphertext,
            dek_version=self._active_version,
            aad=context,
            cipher_fp=hashlib.sha256(ciphertext).hexdigest()[:8],
            cipher_name=cipher_name,
        )

    def unseal(self, bundle: CipherBundle, *, context: str) -> bytes:
        """解密（使用路径唯一入口）：AAD 错位 / 主密钥不匹配 / 密文被篡改 → 中文报错。

        返回 bytes 仅供 agent-runner 登录链路内存瞬时使用，用毕 zeroize。
        """
        if bundle.aad != context:
            raise VaultDecryptError(
                f"解密失败：AAD 绑定错位（密文属 {bundle.aad}，请求 {context}）——"
                "防密文移植，NFR-SEC-04 信封加密行绑定"
            )
        try:
            dek = self._unwrap_dek(bundle.dek_version)
        except VaultError as exc:
            raise VaultDecryptError(f"解密失败：{exc}") from exc
        return self._decrypt(dek, bundle, context.encode("utf-8"))

    def mask(self, plaintext: str) -> str:
        """API 掩码：仅回显尾 2 位（****xx）；长度 ≤2 全掩码（防尾 2 位即明文）。

        掩码在 seal 时即固化可算（mask_hint 列），读路径零解密。
        """
        if len(plaintext) <= 2:
            return "****"
        return f"****{plaintext[-2:]}"

    # ---- DEK 生命周期 ----

    def rotate_dek(self) -> int:
        """DEK 版本化轮换：生成新 DEK 并以主密钥包裹入册，返回新版本号。

        旧版本密文保留可解（过渡期共存）；生产由 DekRotator 重加密后 retire。
        """
        self._active_version += 1
        dek = os.urandom(_KEY_LEN)
        # 包裹：DEK 以主密钥派生流加密（生产 = KMS GenerateDataKey 的 wrapped_dek）
        nonce = self._version_nonce(self._active_version)
        self._wrapped_deks[self._active_version] = _keystream_xor(
            self._kek, nonce, dek
        ) + _demo_tag(self._kek, nonce, b"", str(self._active_version).encode())
        return self._active_version

    def active_version(self) -> int:
        """当前 active DEK 版本。"""
        return self._active_version

    def dek_versions(self) -> tuple[int, ...]:
        """全部在册 DEK 版本（含 retired 共存版本）。"""
        return tuple(sorted(self._wrapped_deks))

    # ---- 内存纪律 ----

    @staticmethod
    def zeroize(buf: bytearray) -> None:
        """用后即弃：覆写内存后置空（Python 语义下尽力而为，配合短生命周期）。"""
        for i in range(len(buf)):
            buf[i] = 0
        buf.clear()

    # ---- 内部 ----

    @staticmethod
    def _version_nonce(version: int) -> bytes:
        """DEK 包裹 nonce：版本派生（DEK 密文本身不入库明文形态）。"""
        return hashlib.sha256(f"dek-wrap-v{version}".encode()).digest()[:_NONCE_LEN]

    def _unwrap_dek(self, version: int) -> bytes:
        """解包 DEK：主密钥不匹配 → 标签校验失败 → VaultError（上层归一中文）。"""
        wrapped = self._wrapped_deks.get(version)
        if wrapped is None:
            raise VaultError(f"DEK 版本 {version} 不在册（未初始化或已应急禁用）")
        payload, tag = wrapped[:-_TAG_LEN], wrapped[-_TAG_LEN:]
        nonce = self._version_nonce(version)
        expected = _demo_tag(self._kek, nonce, b"", str(version).encode())
        if not hmac.compare_digest(tag, expected):
            raise VaultError("主密钥不匹配：DEK 解包标签校验失败")
        return _keystream_xor(self._kek, nonce, payload)

    def _encrypt(
        self, dek: bytes, nonce: bytes, data: bytes, aad: bytes
    ) -> tuple[bytes, str]:
        """加密：AES-256-GCM（主路径）或 demo 派生流 XOR + HMAC 标签（降级）。"""
        if self._use_aes:
            return AESGCM(dek).encrypt(nonce, data, aad), AES_CIPHER_NAME
        ciphertext = _keystream_xor(dek, nonce, data)
        tag = _demo_tag(dek, nonce, ciphertext, aad)
        return ciphertext + tag, DEMO_CIPHER_NAME

    def _decrypt(self, dek: bytes, bundle: CipherBundle, aad: bytes) -> bytes:
        """解密（与 _encrypt 对偶）：任何校验失败归一 VaultDecryptError 中文报错。"""
        if bundle.cipher_name == AES_CIPHER_NAME:
            try:
                return AESGCM(dek).decrypt(bundle.nonce, bundle.ciphertext, aad)
            except Exception as exc:  # cryptography InvalidTag 等统一归一
                raise VaultDecryptError(
                    "解密失败：主密钥不匹配或密文被篡改（AES-GCM 认证标签校验未通过）"
                ) from exc
        tag_len = _TAG_LEN
        payload, tag = bundle.ciphertext[:-tag_len], bundle.ciphertext[-tag_len:]
        expected = _demo_tag(dek, bundle.nonce, payload, aad)
        if not hmac.compare_digest(tag, expected):
            raise VaultDecryptError(
                "解密失败：主密钥不匹配或密文被篡改（demo 认证标签校验未通过）"
            )
        return _keystream_xor(dek, bundle.nonce, payload)


def zeroize(buf: bytearray) -> None:
    """模块级便捷入口：明文缓冲用后覆写清零。"""
    CredentialVault.zeroize(buf)

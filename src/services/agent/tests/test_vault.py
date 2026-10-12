# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""凭证保险库单元测试（NFR-SEC-04，SR-NFR-SEC-04 §6.1/§6.6，demo 形态）。

覆盖：
- 信封加密往返：seal → unseal 明文一致；同明文两次 seal 密文不同（随机 nonce）；
- AAD 行绑定：context 错位解密拒绝（防密文移植）；
- 错误主密钥：解密失败中文报错（口令不匹配/密文被篡改归一）；
- 明文永不回显：密文不含明文子串；mask 仅回显尾 2 位（短输入全掩码）；
- DEK 轮换版本化：rotate 后新 seal 落新版本，旧版本密文仍可解（共存过渡）；
- 主密钥注入三通道（CR A6 修复）：_FILE 文件优先（读首行、不缓存明文）、
  文件缺失中文报错、_FILE 与直读均无 fail-fast；环境变量直读与显式传参等价；
- zeroize：明文缓冲用后覆写清零。

实现方案：docs/07-实现方案/NFR/SR-NFR-SEC-04-账号凭证安全.md §6.1、§6.6、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

from pathlib import Path

import pytest

from services.agent.core.vault import (
    MASTER_KEY_ENV,
    MASTER_KEY_FILE_ENV,
    CipherBundle,
    CredentialVault,
    VaultConfigError,
    VaultDecryptError,
    zeroize,
)

PLAINTEXT = "Ali-Secret-Pass-9z"
CTX = "1:101:login_password"


def test_seal_unseal_roundtrip() -> None:
    """信封加密往返：seal → unseal 明文一致（AES-256-GCM 主路径）；
    同明文两次 seal nonce 随机 → 密文不同（防重放比对）。"""
    vault = CredentialVault("master-key-demo-001")
    bundle = vault.seal(PLAINTEXT, context=CTX)
    assert vault.unseal(bundle, context=CTX).decode("utf-8") == PLAINTEXT
    other = vault.seal(PLAINTEXT, context=CTX)
    assert bundle.nonce != other.nonce
    assert bundle.ciphertext != other.ciphertext


def test_context_binding_rejects_misplaced_row() -> None:
    """AAD 行绑定：context（tenant:account:cred_type）错位 → 解密拒绝。"""
    vault = CredentialVault("master-key-demo-001")
    bundle = vault.seal(PLAINTEXT, context=CTX)
    with pytest.raises(VaultDecryptError, match="绑定"):
        vault.unseal(bundle, context="1:999:cookie")


def test_wrong_master_key_fails_chinese() -> None:
    """错误主密钥（口令不匹配）：解密失败中文报错，绝不静默还原。"""
    vault1 = CredentialVault("master-key-demo-001")
    vault2 = CredentialVault("another-key-998")
    bundle = vault1.seal(PLAINTEXT, context=CTX)
    with pytest.raises(VaultDecryptError, match="主密钥"):
        vault2.unseal(bundle, context=CTX)


def test_ciphertext_hides_plaintext() -> None:
    """密文不含明文子串（落库全密文口径）；cipher_fp 仅 8 位指纹可比对。"""
    vault = CredentialVault("master-key-demo-001")
    bundle = vault.seal(PLAINTEXT, context=CTX)
    assert PLAINTEXT.encode("utf-8") not in bundle.ciphertext
    assert PLAINTEXT not in bundle.cipher_fp
    assert len(bundle.cipher_fp) == 8
    assert isinstance(bundle, CipherBundle)


def test_mask_tail_two_chars() -> None:
    """API 掩码：仅回显尾 2 位（****xx 形态，读路径零解密）。"""
    vault = CredentialVault("master-key-demo-001")
    assert vault.mask("Service-Pass-9z") == "****9z"
    assert vault.mask("ab") == "****"  # 长度 ≤2 全掩码，防尾 2 位即明文本身
    assert vault.mask("") == "****"
    assert vault.mask(PLAINTEXT).endswith("9z")
    assert PLAINTEXT not in vault.mask(PLAINTEXT)


def test_rotate_dek_versions() -> None:
    """DEK 轮换版本化：rotate 后新 seal 落新版本；旧版本密文仍可解（共存过渡期）。"""
    vault = CredentialVault("master-key-demo-001")
    old_bundle = vault.seal(PLAINTEXT, context=CTX)
    v_old = old_bundle.dek_version
    v_new = vault.rotate_dek()
    assert v_new == v_old + 1
    assert vault.active_version() == v_new
    new_bundle = vault.seal("新凭证", context=CTX)
    assert new_bundle.dek_version == v_new
    assert vault.unseal(old_bundle, context=CTX).decode("utf-8") == PLAINTEXT  # 旧密文可解
    assert set(vault.dek_versions()) == {v_old, v_new}


def test_missing_master_key_env_chinese(monkeypatch: pytest.MonkeyPatch) -> None:
    """主密钥两种注入口均未配置：构造即中文报错（fail-fast，拒绝无密钥运行）。"""
    monkeypatch.delenv(MASTER_KEY_ENV, raising=False)
    monkeypatch.delenv(MASTER_KEY_FILE_ENV, raising=False)  # CR A6：排除 _FILE 通道干扰
    with pytest.raises(VaultConfigError, match="均未配置"):
        CredentialVault()


def test_master_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量注入主密钥：与显式传入等价（生产形态 KMS/Secrets 注入的 demo 对位）。"""
    monkeypatch.delenv(MASTER_KEY_FILE_ENV, raising=False)  # 直读通道生效前提：_FILE 未配置
    monkeypatch.setenv(MASTER_KEY_ENV, "env-injected-key-42")
    vault = CredentialVault()
    bundle = vault.seal(PLAINTEXT, context=CTX)
    assert vault.unseal(bundle, context=CTX).decode("utf-8") == PLAINTEXT


def test_master_key_file_priority_over_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CR A6：_FILE 文件优先于环境变量直读——读文件首行（后续行忽略）。"""
    import hmac

    from services.agent.core.vault import _derive_key

    key_file = tmp_path / "vault_master_key"
    key_file.write_text("file-master-key-777\nsecond-line-must-be-ignored\n",
                        encoding="utf-8")
    monkeypatch.setenv(MASTER_KEY_FILE_ENV, str(key_file))
    monkeypatch.setenv(MASTER_KEY_ENV, "env-key-must-lose")
    vault = CredentialVault()
    bundle = vault.seal(PLAINTEXT, context=CTX)
    assert vault.unseal(bundle, context=CTX).decode("utf-8") == PLAINTEXT
    # 优先级证明（DEK 为实例内随机、跨实例不可解密，故以 KEK 派生值为判据）：
    # KEK 由文件首行值派生，而非环境变量值、也非文件第二行值
    salt = b"agent-runner/vault/kek"
    assert hmac.compare_digest(vault._kek, _derive_key("file-master-key-777", salt))
    assert not hmac.compare_digest(
        vault._kek, _derive_key("env-key-must-lose", salt))
    assert not hmac.compare_digest(
        vault._kek, _derive_key("second-line-must-be-ignored", salt))
    # 读后不缓存明文：实例状态里不得残留文件明文（仅保留派生 KEK）
    assert "file-master-key-777" not in str(vars(vault))


def test_master_key_file_missing_chinese_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CR A6：_FILE 已配置但文件缺失 → 构造即中文报错（不静默回退直读通道）。"""
    monkeypatch.setenv(MASTER_KEY_FILE_ENV, str(tmp_path / "not-exist.key"))
    monkeypatch.setenv(MASTER_KEY_ENV, "fallback-forbidden")  # 有直读值也不得回退
    with pytest.raises(VaultConfigError, match="不可读"):
        CredentialVault()


def test_master_key_file_empty_fails_fast(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CR A6：_FILE 指向空文件 → fail-fast（空密钥等同无密钥，拒绝初始化）。"""
    empty = tmp_path / "empty.key"
    empty.write_text("   \n", encoding="utf-8")
    monkeypatch.setenv(MASTER_KEY_FILE_ENV, str(empty))
    monkeypatch.delenv(MASTER_KEY_ENV, raising=False)
    with pytest.raises(VaultConfigError, match="为空"):
        CredentialVault()


def test_demo_cipher_limitation_declared() -> None:
    """demo 限定声明：信封加密为 demo 形态（非生产 KMS+AESGCM 全链），文档显式注明。"""
    assert "demo" in (CredentialVault.__doc__ or "").lower()
    fallback = CredentialVault("k", force_demo_cipher=True)
    bundle = fallback.seal(PLAINTEXT, context=CTX)
    assert fallback.unseal(bundle, context=CTX).decode("utf-8") == PLAINTEXT
    assert "demo" in bundle.cipher_name  # 降级实现名携带 demo 标记


def test_zeroize_buffer() -> None:
    """zeroize：明文缓冲用后覆写清零（内存纪律，Python 语义下尽力而为）。"""
    buf = bytearray(b"topsecret-bytes")
    zeroize(buf)
    assert len(buf) == 0 or set(buf) == {0}

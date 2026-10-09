# 知识产权归甲方所有（COM-IP-01）
"""MilvusStore 测试：pymilvus import 守护 + expr DSL → Milvus 方言翻译（纯函数）。

MilvusStore 的建表 / 入库 / 检索主逻辑依赖真实 Milvus 实例，无法在本机运行
（联调条件见 services/kb/core/vectorstore/milvus_impl.py 模块 docstring）；
本文件覆盖不依赖 pymilvus 的可测面：守护行为与 expr 翻译语义。
"""
from __future__ import annotations

import importlib.util

import pytest

from services.kb.core.vectorstore import (
    MilvusDependencyError,
    MilvusStore,
    MilvusStoreError,
    translate_expr_to_milvus,
)

_PYMILVUS_INSTALLED = importlib.util.find_spec("pymilvus") is not None


def test_milvus_dependency_guard() -> None:
    """未安装 pymilvus 时构造 MilvusStore 抛中文提示（含安装命令与替代实现）。"""
    if _PYMILVUS_INSTALLED:
        pytest.skip("环境已安装 pymilvus，缺失守护路径无法触发（联调环境跑本用例）")
    with pytest.raises(MilvusDependencyError, match="未安装 pymilvus"):
        MilvusStore()
    with pytest.raises(MilvusDependencyError, match="pymilvus>=2.5"):
        MilvusStore(uri="http://milvus-standalone:19530")


def test_milvus_error_hierarchy() -> None:
    """异常层级：依赖缺失 / 实例不可用均为 MilvusStoreError 子类，便于统一捕获。"""
    assert issubclass(MilvusDependencyError, MilvusStoreError)
    assert issubclass(MilvusStoreError, RuntimeError)


# ── expr DSL → Milvus expr 方言翻译（SR-KB-014 §6.3 强制条件 + 转义）──
def test_translate_none_expr_yields_forced_conditions_only() -> None:
    """无 expr 时仍强制注入租户与 published 条件（调用方不可省略）。"""
    assert translate_expr_to_milvus(None) == 'tenant_id == 1 and item_status == "published"'
    assert translate_expr_to_milvus("") == 'tenant_id == 1 and item_status == "published"'


def test_translate_contract_example() -> None:
    """契约示例：`kb_type in (...) and sku = '...'` → Milvus 方言（in 列表 / ==）。"""
    translated = translate_expr_to_milvus("kb_type in ('product','skill') and sku = 'ST-3200'")
    assert translated == (
        'tenant_id == 1 and item_status == "published"'
        ' and kb_type in ["product", "skill"] and sku == "ST-3200"'
    )


def test_translate_respects_tenant_override() -> None:
    """租户编号可覆盖（P5 多租户中间件注入口径）。"""
    translated = translate_expr_to_milvus("sku = 'AB-1'", tenant_id=7)
    assert translated.startswith("tenant_id == 7 and")


def test_translate_escapes_values() -> None:
    """值中的双引号与反斜杠被转义（表达式注入防护）。"""
    translated = translate_expr_to_milvus('customer_ref = \'ACME "VIP"\\X\'')
    assert translated.endswith('customer_ref == "ACME \\"VIP\\"\\\\X"')


def test_translate_rejects_invalid_expr() -> None:
    """DSL 解析 fail-closed 语义在翻译层同样生效。"""
    for bad in ("foo = 'x'", "kb_type in ('product') or sku = 'y'", "tenant_id == 1"):
        with pytest.raises(ValueError):
            translate_expr_to_milvus(bad)

# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""评测集模型测试（FR-KB-016 / SR-KB-016 §9：数据集校验族，迭代 0.3）。

覆盖：构造校验（重复 case_id 拒绝 / 非法 kb_type / 空答案 / 空集）、
规模口径（99 条拒绝冻结、100 条冻结通过、状态机违规）、维度分组稳定性、
JSON 文件加载/保存 round-trip 与非法结构拒绝。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.kb.core.evaluation.dataset import (
    DATASET_STATUS_FLOW,
    MIN_FROZEN_CASES,
    DuplicateCaseIdError,
    DatasetError,
    DatasetStateError,
    DatasetTooSmallError,
    EvalCase,
    EvalDataset,
)


def _case(case_id: str = "C0001", kb_type: str = "product", **overrides: object) -> EvalCase:
    """标准用例工厂（缺省：ST-3200 询价问法，期望块 10241）。"""
    defaults: dict[str, object] = {
        "query": "ST-3200 支架的最大承载和认证情况",
        "expected_chunk_ids": (10241,),
        "kb_type": kb_type,
        "dimensions": {"scene": "询价"},
    }
    defaults.update(overrides)
    return EvalCase(case_id=case_id, **defaults)  # type: ignore[arg-type]


def _dataset(n: int = 3, name: str = "smoke", version: int = 1) -> EvalDataset:
    """n 条用例的小集（联调/冒烟规模；冻结规模校验另测）。"""
    return EvalDataset(
        name=name,
        version=version,
        cases=[_case(f"C{i:04d}", kb_type="product" if i % 2 else "process") for i in range(n)],
    )


# ─────────────────────────────── 构造期校验 ───────────────────────────────


def test_构造通过_基础字段齐备() -> None:
    """合法用例/数据集构造成功，规模与状态按入参落位。"""
    dataset = _dataset(3)
    assert dataset.case_count == 3
    assert dataset.status == "draft"
    assert dataset.version == 1


def test_重复case_id拒绝() -> None:
    """同一评测集内 case_id 重复 → DuplicateCaseIdError（fail-closed）。"""
    with pytest.raises(DuplicateCaseIdError, match="C0001"):
        EvalDataset(
            name="dup", version=1,
            cases=[_case("C0001"), _case("C0001", kb_type="process")],
        )


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"kb_type": "catalog"}, "六大库枚举"),
        ({"expected_chunk_ids": ()}, "expected_chunk_ids 不能为空"),
        ({"query": "  "}, "query"),
        ({"case_id": ""}, "case_id"),
    ],
)
def test_用例非法字段拒绝(overrides: dict[str, object], message: str) -> None:
    """用例字段非法（库类型/空答案/空白问法/空编号）→ DatasetError。"""
    with pytest.raises(DatasetError, match=message):
        _case(**{"case_id": "C0009", "kb_type": "product", **overrides})


def test_数据集元数据非法拒绝() -> None:
    """版本号 0、非法状态、空用例集均拒绝构造。"""
    with pytest.raises(DatasetError, match="version"):
        _dataset(1, version=0)
    with pytest.raises(DatasetError, match="status"):
        EvalDataset(name="s", version=1, cases=[_case()], status="unknown")
    with pytest.raises(DatasetError, match="至少需要 1 条"):
        EvalDataset(name="s", version=1, cases=[])


# ─────────────────────────────── 规模与冻结状态机 ───────────────────────────────


def test_99条冻结拒绝_100条通过() -> None:
    """SRS ≥100 条口径：99 条 → DatasetTooSmallError；100 条 → 冻结成功。"""
    small = _dataset(MIN_FROZEN_CASES - 1, name="frozen_try")
    with pytest.raises(DatasetTooSmallError, match="99 条用例"):
        small.freeze()
    assert small.status == "draft"  # 冻结失败不改变状态

    enough = _dataset(MIN_FROZEN_CASES, name="frozen_ok")
    assert enough.freeze() is enough
    assert enough.status == "frozen"


def test_重复冻结状态机违规() -> None:
    """已冻结集合再冻结 → DatasetStateError（冻结集只读，修改派生新版本）。"""
    frozen = _dataset(MIN_FROZEN_CASES, name="frozen_once")
    frozen.freeze()
    with pytest.raises(DatasetStateError, match="已冻结"):
        frozen.freeze()


def test_状态机取值与容量阈值常量() -> None:
    """状态流与最小规模常量锚定（防止口径漂移）。"""
    assert DATASET_STATUS_FLOW == ("draft", "reviewing", "frozen", "retired")
    assert MIN_FROZEN_CASES == 100


# ─────────────────────────────── 维度分组 ───────────────────────────────


def test_维度分组键稳定() -> None:
    """kb_type 恒为首组，自定义维度按名排序；展示串分号连接。"""
    case = _case(dimensions={"scene": "询价", "sku": "ST-3200", "reason": "价格"})
    assert case.dimension_groups() == [
        "kb_type=product", "reason=价格", "scene=询价", "sku=ST-3200",
    ]
    assert case.dimension_label() == "kb_type=product; reason=价格; scene=询价; sku=ST-3200"


# ─────────────────────────────── JSON 加载/保存 ───────────────────────────────


def test_JSON往返无损(tmp_path: Path) -> None:
    """save → load round-trip：字段（含维度/过滤/状态）逐项一致，中文原样落盘。"""
    dataset = EvalDataset(
        name="round_trip", version=3, status="reviewing",
        cases=[
            _case("C0001", dimensions={"scene": "询价", "sku": "ST-3200"}),
            _case("C0002", kb_type="process", dimensions={"process_node": "报价"},
                  filters={"sku": ["ST-3200"]}),
        ],
    )
    path = dataset.save(tmp_path / "sets" / "rt.json")
    loaded = EvalDataset.load(path)
    assert loaded.to_dict() == dataset.to_dict()
    assert "询价" in path.read_text(encoding="utf-8")  # ensure_ascii=False 保中文可读


def test_加载非法结构拒绝(tmp_path: Path) -> None:
    """缺必填键 / 未知键 / 非法 JSON / 文件缺失 → DatasetError。"""
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"name": "x", "cases": []}), encoding="utf-8")  # 缺 version
    with pytest.raises(DatasetError, match="缺少必填字段"):
        EvalDataset.load(path)

    path.write_text(json.dumps({
        "name": "x", "version": 1,
        "cases": [{"case_id": "C1", "query": "q", "expected_chunk_ids": [1],
                   "kb_type": "product", "unexpected": 1}],
    }), encoding="utf-8")
    with pytest.raises(DatasetError, match="未知字段"):
        EvalDataset.load(path)

    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(DatasetError, match="不是合法 JSON"):
        EvalDataset.load(path)

    with pytest.raises(DatasetError, match="不可读"):
        EvalDataset.load(tmp_path / "nowhere.json")

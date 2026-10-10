# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""合成语料生成器（甲方素材模拟，迭代 0.3 收口）。

以确定性随机（固定种子）批量生成六大知识库素材文件（docx/xlsx/pdf），
用于在甲方真实素材（OI-03，NDA 后提供）就位前做**工程自证**：
流水线容量、入库吞吐、检索质量与并发性能的规模级验证。

重要声明：合成语料仅用于工程自证，正式验收（SRS §16.1）必须替换为
甲方真实素材与业务专家人工出题的 ≥100 条评测集。

设计约束（保证链路行为可控）：
- 每个素材含唯一锚点串（SKU/案例号/课程号），文件间内容差异 ≥30%，
  规避清洗去重（md5/simhash）误杀；
- 正文不含明文 PII 与七位以上数字串（避免脱敏改写锚点）；
- 单位使用标准中文单位，避开纠错归一化改写锚点；
- 锚点词串将被评测 query 复用（FakeEmbeddingClient 稀疏路按 unicode \\w 分词）。

用法：python3 tools/simcorp/generator.py --out out/simcorp/corpus --files 500 --seed 42
产物：素材文件 + manifest.json（file→{kb_type, anchors, sku, family}，评测集构造输入）
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

# 仓库自举（src 布局）
_REPO_ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from services.kb.core.pipeline.parsers.registry import REGISTRY  # noqa: E402

#: 六库配比（生成时按 files 归一）
KB_RATIOS: dict[str, float] = {
    "product": 0.35,
    "customer": 0.15,
    "skill": 0.15,
    "process": 0.10,
    "negative_case": 0.10,
    "training": 0.15,
}

#: 受控词表（维度过滤与内容多样性）
PRODUCT_LINES = ["壁挂支架", "云台支架", "落地灯架", "麦克风吊架", "显示器支臂"]
REGIONS = ["欧洲", "东南亚", "中东", "北美", "南美", "大洋洲"]
SCENE_TAGS = ["询价", "报价", "议价", "样品", "订单", "物流", "售后"]
PROCESS_NODES = ["P01 询盘受理", "P03 报价", "P05 样品", "P07 订单", "P09 物流", "P11 售后"]
NEGATIVE_KINDS = ["丢单复盘", "投诉处理", "错误话术警示", "平台处罚案例"]
TRAIN_COURSES = ["产品体系入门", "询盘响应实训", "报价规范", "平台规则红线", "物流常识"]


def _anchor_sentence(kind: str, uid: str, rnd: random.Random) -> str:
    """生成含唯一锚点的主题句（评测 query 的命中来源）。"""
    if kind == "product":
        line = rnd.choice(PRODUCT_LINES)
        load = rnd.choice([35, 45, 55, 65, 75, 85])
        return f"{uid} {line} 最大承载 {load} 千克 通过 CE 与 SGS 认证。"
    if kind == "customer":
        grade = rnd.choice(["A", "B", "C", "D"])
        region = rnd.choice(REGIONS)
        return f"{uid} 客户档案 {region} {grade} 级 月采购量 {rnd.choice(['八千件', '三千件', '九百件', '六十件'])}。"
    if kind == "skill":
        scene = rnd.choice(SCENE_TAGS)
        return f"{uid} {scene} 场景话术 让步必须换取对等条件 订单量或付款方式三选一。"
    if kind == "process":
        node = rnd.choice(PROCESS_NODES)
        return f"{uid} {node} 节点规程 价格数据一律从产品库结构化字段取数。"
    if kind == "negative_case":
        kindn = rnd.choice(NEGATIVE_KINDS)
        return f"{uid} {kindn} 根因是排产口径未确认 改进措施为流程双签。"
    return f"{uid} 新人课程 {rnd.choice(TRAIN_COURSES)} 课后练习要求写出型号卖点。"


def _body_paragraphs(kind: str, uid: str, sku: str, rnd: random.Random) -> list[str]:
    """围绕锚点扩充正文（每文件 4-7 段，内容差异保证去重不误杀）。"""
    paras = [_anchor_sentence(kind, uid, rnd)]
    pool = {
        "product": [
            f"适配尺寸覆盖三十七至七十英寸 安装孔距支持 400x400 毫米。",
            f"本体采用冷轧钢板 静电喷涂工艺 包装每箱两台 毛重不超过十五千克。",
            f"卖点提炼 安装免工具 快拆结构 五年质保。",
            f"竞品对比要点 本型号承载高出同级约一成 交期稳定在二十天内。",
            f"应用场景 客厅安装 会议室吊装 展厅展示。",
        ],
        "customer": [
            "沟通历史要点 首轮询盘响应及时 报价后跟进三轮。",
            "成单记录 去年秋季订单为主 复购倾向中等。",
            "丢单记录 一次因交期竞争丢单 一次因认证要求未达标。",
            "分级标准 月采购量与付款信用双维度评定。",
        ],
        "skill": [
            "开场白模板 感谢询盘 请告知目标数量与应用场景。",
            "议价策略 首次让幅不超过百分之三 次幅换取订单量。",
            "赠品策略 价格不动 改赠安装配件或延长质保。",
            "跟进节奏模板 第一天 第三天 第七天 第十四天。",
        ],
        "process": [
            "收到询盘后两个工作日内完成正式报价单发出。",
            "报价单必须包含型号 规格 FOB 单价 最小起订量与有效期。",
            "超出授权折扣范围 先提交销售主管审批再回复客户。",
            "平台红线 禁止诱导站外交易 批量群发一律拦截。",
        ],
        "negative_case": [
            "案例概况 交期承诺与排产能力脱节 客户转向竞品。",
            "直接后果 错过当地销售旺季 损失一个季度订单。",
            "错误话术示例 交期口头承诺十五天 实际需要二十八天。",
            "改进措施 交期承诺必须走排产确认流程 加急单独评估。",
        ],
        "training": [
            "课程目标 认识三大产品线与主打型号。",
            "随堂测验 型号与卖点连线 选择题五道。",
            "模拟场景 印度砍价型客户 坚守底价演练。",
            "考核标准 演练得分与笔试双双达标才可上岗。",
        ],
    }
    extra = rnd.sample(pool[kind], k=min(len(pool[kind]), rnd.randint(3, 6)))
    return paras + extra


def _xlsx_rows(kind: str, sku: str, rnd: random.Random) -> list[list[str]]:
    """xlsx 参数表内容（产品库为主）。"""
    rows = [["参数", "数值", "说明"]]
    for name, val in rnd.sample(
        [
            ("承载重量", f"{rnd.choice([35, 45, 55, 85])}千克"),
            ("适配尺寸", "三十七至七十英寸"),
            ("安装孔距", "600x400"),
            ("本体重量", "4.6千克"),
            ("质保年限", "五年"),
        ],
        k=4,
    ):
        rows.append([name, val, f"{sku} 结构化参数"])
    return rows


def generate(out_dir: Path, files: int, seed: int = 42) -> Path:
    """生成素材目录与 manifest.json，返回 manifest 路径。"""
    rnd = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    # 六库文件数分配
    counts = {kb: max(1, round(files * r / sum(KB_RATIOS.values())) - 0) for kb, r in KB_RATIOS.items()}
    manifest: dict[str, dict] = {}
    seq = 0
    for kb, n in counts.items():
        for _ in range(n):
            seq += 1
            sku = f"{rnd.choice(['ST', 'GP', 'LM', 'MC'])}-{1000 + seq}"
            uid = f"SIM{seq:04d}"
            fmt = rnd.choices(["docx", "xlsx", "pdf"], weights=[0.85, 0.10, 0.05])[0]
            name = f"{kb}_{uid}_{sku.replace('-', '')}.{fmt}"
            path = out_dir / name
            kb_label = {
                "product": "产品手册", "customer": "客户档案", "skill": "话术库",
                "process": "流程规程", "negative_case": "案例复盘", "training": "带教课程",
            }[kb]
            title = f"{uid} {kb_label}"
            paragraphs = _body_paragraphs(kb, uid, sku, rnd)
            if fmt == "docx":
                _build_docx(path, title, paragraphs)
            elif fmt == "xlsx":
                _build_xlsx(path, {"参数表": _xlsx_rows(kb, sku, rnd)})
            else:
                _build_pdf(path, title, [p.replace(" ", "") for p in paragraphs])
            manifest[name] = {
                "kb_type": kb,
                "uid": uid,
                "sku": sku,
                "anchors": [uid, sku.replace("-", "")],
                "family": sku.split("-")[0],
            }
    mf = out_dir / "manifest.json"
    mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return mf


# ── 文件构造（与 build_samples.py 同法）──────────────────────────────────────

def _build_docx(path: Path, title: str, paragraphs: list[str]) -> None:
    """docx：Heading 1 标题 + Normal 段落（python-docx）。"""
    import docx  # noqa: PLC0415

    d = docx.Document()
    d.add_heading(title, level=1)
    for p in paragraphs:
        d.add_paragraph(p)
    d.save(str(path))


def _build_xlsx(path: Path, sheets: dict[str, list[list[str]]]) -> None:
    """xlsx：多 sheet 二维表（openpyxl）。"""
    from openpyxl import Workbook  # noqa: PLC0415

    wb = Workbook()
    wb.remove(wb.active)
    for sheet, rows in sheets.items():
        ws = wb.create_sheet(sheet)
        for row in rows:
            ws.append(row)
    wb.save(str(path))


def _build_pdf(path: Path, title: str, lines: list[str]) -> None:
    """pdf：fitz 单页文本（PyMuPDF）。"""
    import fitz  # noqa: PLC0415

    doc = fitz.open()
    page = doc.new_page()
    y = 72
    page.insert_text((72, y), title, fontsize=16)
    for line in lines:
        y += 22
        page.insert_text((72, y), line, fontsize=11)
    doc.save(str(path))
    doc.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="合成语料生成器（工程自证用，非正式验收语料）")
    ap.add_argument("--out", default="out/simcorp/corpus")
    ap.add_argument("--files", type=int, default=500)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)
    mf = generate(Path(args.out), args.files, args.seed)
    n = len(json.loads(mf.read_text(encoding="utf-8")))
    print(f"[simcorp] 已生成 {n} 个素材 → {args.out}（manifest: {mf.name}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

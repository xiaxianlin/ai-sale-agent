# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""甲方实测任务单生成器（NFR-USA-01「甲方人员实测并记录」的工程化载体，迭代 0.3）。

从 tools/usability/operations.yaml（运维操作单一事实源）展开 **20 个典型任务**：
任务/前置/步骤要点/完成标准/预计分钟，生成 xlsx（openpyxl）与 md 两份任务单，
并附「实测记录表」模板——列口径对齐 SR-NFR-USA-01 §6.2/§6.3：

- 完成状态三态：完成（独立）/ 求助完成 / 失败（对应 outcome done/done_with_help/fail，
  xlsx 内置下拉约束；第 3 次提示仍无法完成即判失败收束）；
- 耗时（分钟）与困惑点原话（脱敏）列：量化与归因证据；
- 全部任务只允许经界面完成，出现 SQL/命令行/开发者工具即该单元判失败。

一致性约束（生成前校验，防两份事实源漂移）：
- 每个任务引用的操作码必须存在于 operations.yaml；
- operations.yaml 中每个操作至少被一个任务覆盖（否则说明任务集不完整，生成即失败）。

用法：python3 tools/usability/task_sheet.py [--ops tools/usability/operations.yaml]
        [--out-dir out/usability]
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# 脚本直跑自举：把仓库根加入 sys.path 以复用 tools.usability 包内共享模块
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from openpyxl import Workbook  # noqa: E402
from openpyxl.styles import Alignment, Font, PatternFill  # noqa: E402
from openpyxl.worksheet.datavalidation import DataValidation  # noqa: E402

from tools.usability.ops_registry import Operation, load_operations  # noqa: E402

#: 实测记录表「完成状态」下拉选项（USA-01 三态口径）
OUTCOME_OPTIONS: tuple[str, ...] = ("完成", "求助完成", "失败")

#: 实测纪律（任务单头部说明，对齐 SR-NFR-USA-01 §6.1）
DISCIPLINE_NOTES: tuple[str, ...] = (
    "被试为甲方非技术人员（不熟悉 SQL/命令行/开发者工具），独立测试账号、全新浏览器配置文件；",
    "主持只念任务卡不教学：被试主动求助最多给 2 次口头提示（计入求助次数），第 3 次仍无法完成即该任务判「失败」；",
    "全程零 SQL/命令行/开发者工具，出现即该任务单元判「失败」；",
    "逐任务记录：完成状态三态（完成/求助完成/失败）+ 耗时（分钟）+ 求助次数 + 困惑点原话（脱敏）；",
    "通过口径（NFR-USA-01）：独立完成率 ≥90% 且无阻断/严重问题遗留，整改后仅复测未过任务。",
)


@dataclass(frozen=True)
class UatTask:
    """一个典型实测任务（从 operations.yaml 的操作展开）。"""

    no: str                      # 任务号（T01-T20）
    title: str                   # 任务（念给被试的口语化指令）
    ops: tuple[str, ...]         # 依赖的操作码（须在 operations.yaml 登记）
    preconditions: tuple[str, ...]  # 前置条件
    steps: tuple[str, ...]       # 步骤要点
    criteria: str                # 完成标准（可判定）
    minutes: int                 # 预计分钟（时限）


#: 20 个典型任务定义（任务号/口径与 SR-NFR-USA-01 §6.2 任务库同源思想；
#: 迭代 0.3 仅覆盖已实现操作面，认证/通知类任务随对应迭代补入）
TASKS: tuple[UatTask, ...] = (
    UatTask(
        no="T01", title="新建一条产品知识并保存为草稿", ops=("KB_ITEM_CREATE",),
        preconditions=("以知识库管理员账号登录管理后台", "已了解六大知识库页签位置"),
        steps=("进入知识库管理，切换到产品库", "点击「新建」，填写标题与必填元数据", "选择「存草稿」，确认列表出现该条目且状态为草稿"),
        criteria="草稿保存成功且列表立即可见", minutes=4,
    ),
    UatTask(
        no="T02", title="新建知识条目并直接发布", ops=("KB_ITEM_CREATE",),
        preconditions=("已能完成 T01",),
        steps=("点击「新建」填写条目内容", "选择「发布」动作提交", "确认列表状态为已发布"),
        criteria="发布成功、列表状态为「已发布」，能说出草稿与发布的区别", minutes=5,
    ),
    UatTask(
        no="T03", title="在条目列表按知识库类型与状态筛选", ops=("KB_ITEM_LIST",),
        preconditions=("各库已有若干条目",),
        steps=("打开条目列表", "按知识库类型（如负面案例库）过滤", "再按状态（草稿/已发布）过滤并核对数量"),
        criteria="筛选结果与看板统计口径一致，能口述当前过滤条件", minutes=3,
    ),
    UatTask(
        no="T04", title="用关键词检索定位指定条目", ops=("KB_ITEM_LIST",),
        preconditions=("列表中存在含指定关键词的条目",),
        steps=("在列表搜索框输入关键词", "查看命中结果", "打开目标条目详情核对"),
        criteria="2 分钟内打开目标条目详情", minutes=2,
    ),
    UatTask(
        no="T05", title="查看条目详情与元数据", ops=("KB_ITEM_DETAIL",),
        preconditions=("已存在一条含元数据的条目",),
        steps=("在列表点击条目标题进入详情", "核对正文与元数据字段展示", "返回列表"),
        criteria="能读出该条目的版本号与最后修改人", minutes=2,
    ),
    UatTask(
        no="T06", title="修改草稿内容并保存", ops=("KB_ITEM_UPDATE",),
        preconditions=("已存在一条草稿条目",),
        steps=("进入草稿条目编辑", "修改标题/正文/元数据", "保存并核对版本号 +1"),
        criteria="保存成功且能口述「改了什么」", minutes=4,
    ),
    UatTask(
        no="T07", title="发布一条草稿条目（状态流转）", ops=("KB_ITEM_UPDATE",),
        preconditions=("已存在一条草稿条目",),
        steps=("进入草稿条目", "执行「发布」动作", "核对状态变为已发布"),
        criteria="状态流转成功，索引同步状态不再是「未入索引」", minutes=3,
    ),
    UatTask(
        no="T08", title="归档已发布条目并恢复", ops=("KB_ITEM_UPDATE",),
        preconditions=("已存在一条已发布条目",),
        steps=("对已发布条目执行「归档」", "在归档列表确认该条目", "执行「恢复」回到草稿态"),
        criteria="归档后检索不可见、恢复后可继续编辑", minutes=5,
    ),
    UatTask(
        no="T09", title="删除一条草稿条目（软删确认）", ops=("KB_ITEM_DELETE",),
        preconditions=("已存在一条可删除的草稿条目", "知晓 30 天恢复窗口口径"),
        steps=("对草稿条目执行「删除」", "通过高危二次确认（输入名称）", "确认列表不再显示该条目"),
        criteria="独立通过二次确认删除成功，且知道 30 天内可请管理员恢复", minutes=3,
    ),
    UatTask(
        no="T10", title="查看表单字段定义（动态表单）", ops=("KB_SCHEMA_VIEW",),
        preconditions=("注册表已登记至少一个知识库的字段",),
        steps=("打开新建/编辑表单", "对照字段标签、必填星号与下拉选项", "尝试留空必填项提交看提示"),
        criteria="能指出哪些字段必填、提示为中文可理解", minutes=3,
    ),
    UatTask(
        no="T11", title="下载批量导入模板", ops=("KB_IMPORT_TEMPLATE",),
        preconditions=("本地可打开 xlsx 文件",),
        steps=("进入批量导入页", "选择知识库类型下载模板", "打开文件核对列头与填写说明"),
        criteria="独立找到模板下载入口并成功打开文件", minutes=2,
    ),
    UatTask(
        no="T12", title="用 Excel 批量导入（含错误行）", ops=("KB_IMPORT_UPLOAD",),
        preconditions=("已按模板准备一份含错误行的导入文件",),
        steps=("上传导入文件并提交", "等待任务受理返回", "进入进度页查看导入状态"),
        criteria="导入任务创建成功并能读懂进度展示", minutes=10,
    ),
    UatTask(
        no="T13", title="查询导入任务进度直至完成", ops=("KB_IMPORT_PROGRESS",),
        preconditions=("存在一个进行中或刚完成的导入任务",),
        steps=("打开导入任务进度页", "刷新查看百分比与计数", "核对成功/跳过/失败数量"),
        criteria="能读出成功与错误行数量", minutes=3,
    ),
    UatTask(
        no="T14", title="下载错误行报告并按建议修复重导", ops=("KB_IMPORT_ERROR_REPORT", "KB_IMPORT_UPLOAD"),
        preconditions=("导入任务存在错误行",),
        steps=("下载错误行报告 xlsx", "按报告的行号/原因/修改建议修正数据", "重新上传完成二次导入"),
        criteria="自主读懂报告并完成二次导入且无错误行", minutes=8,
    ),
    UatTask(
        no="T15", title="查看条目版本历史", ops=("KB_VERSION_LIST",),
        preconditions=("目标条目至少有 2 个版本",),
        steps=("进入条目详情/版本页", "查看倒序版本时间线", "识别当前版本与回滚来源标记"),
        criteria="能指出当前版本号并说出最近一次变更时间", minutes=3,
    ),
    UatTask(
        no="T16", title="对比两个版本差异", ops=("KB_VERSION_DIFF",),
        preconditions=("目标条目至少有 2 个版本",),
        steps=("在版本页选择两个版本", "发起对比", "阅读字段级差异（新增/修改/删除）"),
        criteria="能口述两版本间「改了什么」", minutes=4,
    ),
    UatTask(
        no="T17", title="把条目回滚到指定版本（高危确认）", ops=("KB_VERSION_ROLLBACK",),
        preconditions=("目标条目存在可回滚的历史版本", "知晓回滚生成新版本而非抹除历史"),
        steps=("在版本页选择目标版本发起回滚", "通过高危二次确认（输入名称）", "核对回滚后内容与版本时间线"),
        criteria="独立完成输入名称确认并回滚成功，历史版本仍可见", minutes=5,
    ),
    UatTask(
        no="T18", title="触发一轮检索质量评测", ops=("KB_EVAL_TRIGGER",),
        preconditions=("已准备评测集（问题 + 标准答案）",),
        steps=("进入评测页上传/选择评测集", "触发评测任务", "确认任务受理提示"),
        criteria="评测任务创建成功并返回任务号", minutes=5,
    ),
    UatTask(
        no="T19", title="查看评测结果与准确率并下载报告", ops=("KB_EVAL_QUERY",),
        preconditions=("存在一个已完成或已失败的评测任务",),
        steps=("打开评测任务详情", "阅读指标与门槛结论", "下载评测报告（markdown/xlsx）"),
        criteria="能读出准确率数字并说出是否达门槛", minutes=3,
    ),
    UatTask(
        no="T20", title="发布后统一检索验证并查看看板统计", ops=("KB_SEARCH_API", "KB_STATS_VIEW"),
        preconditions=("已发布含特定关键词的条目",),
        steps=("用统一检索验证该条目可命中", "打开看板统计页", "核对条目总数与近 7 日变更趋势"),
        criteria="检索能命中新发布条目；能读出各库条目数与最近更新时间", minutes=5,
    ),
)


def build_task_rows(operations: Sequence[Operation]) -> list[dict[str, str]]:
    """任务 + 操作声明 → 任务单行（前置/步骤由操作元数据自动补全展开）。

    校验：任务引用了未登记操作码 → ValueError（任务集与清单漂移，拒绝生成）；
    返回行中「API 支撑」「危险级」「步数」均来自 operations.yaml（单一事实源）。
    """
    ops_index = {op.code: op for op in operations}
    rows: list[dict[str, str]] = []
    for task in TASKS:
        unknown = [code for code in task.ops if code not in ops_index]
        if unknown:
            raise ValueError(f"任务 {task.no} 引用未登记操作码：{', '.join(unknown)}（请先登记 operations.yaml）")
        support_parts = [f"{ops_index[code].code}:{ops_index[code].api}" for code in task.ops]
        danger_parts = sorted({ops_index[code].danger_label for code in task.ops
                               if ops_index[code].dangerous != "false"})
        preconditions = list(task.preconditions)
        if danger_parts:
            preconditions.append("高危操作：界面须输入对象名称二次确认（SR-KB-023 §6.3）")
        rows.append(
            {
                "no": task.no,
                "title": task.title,
                "ops": " + ".join(task.ops),
                "support": "；".join(support_parts),
                "preconditions": "；".join(preconditions),
                "steps": " → ".join(task.steps),
                "criteria": task.criteria,
                "minutes": str(task.minutes),
                "danger": "；".join(danger_parts) if danger_parts else "无",
            }
        )
    covered = {code for task in TASKS for code in task.ops}
    uncovered = sorted({op.code for op in operations} - covered)
    if uncovered:
        raise ValueError(
            f"操作未被任何任务覆盖（任务集不完整）：{', '.join(uncovered)}——"
            "请为这些操作补充实测任务或从清单移除（NFR-USA-01 任务集完整性）"
        )
    return rows


def _render_markdown(rows: list[dict[str, str]]) -> str:
    """任务单 + 实测记录表的 Markdown 渲染。"""
    lines: list[str] = []
    lines.append("# 知识库管理后台 甲方实测任务单（NFR-USA-01，迭代 0.3）")
    lines.append("")
    lines.append("由 tools/usability/task_sheet.py 从 operations.yaml 展开（20 个典型任务）；任务与操作码对账以该清单为单一事实源。")
    lines.append("")
    lines.append("## 一、实测纪律（观察员与主持必读）")
    lines.extend(f"{idx}. {note}" for idx, note in enumerate(DISCIPLINE_NOTES, start=1))
    lines.append("")
    lines.append("## 二、实测任务（20 项）")
    lines.append("")
    lines.append("| # | 任务 | 操作码 | API 支撑 | 前置条件 | 步骤要点 | 完成标准 | 预计分钟 | 危险级 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for row in rows:
        lines.append(
            f"| {row['no']} | {row['title']} | {row['ops']} | {row['support']} | "
            f"{row['preconditions']} | {row['steps']} | {row['criteria']} | "
            f"{row['minutes']} | {row['danger']} |"
        )
    lines.append("")
    lines.append("## 三、实测记录表（每被试 × 每任务一行，USA-01 口径）")
    lines.append("")
    lines.append("| # | 任务 | 被试 | 完成状态（完成/求助完成/失败） | 耗时（分钟） | 求助次数 | 错误操作数 | 困惑点原话（脱敏） | 备注 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for row in rows:
        lines.append(f"| {row['no']} | {row['title']} |  |  |  |  |  |  |  |")
    lines.append("")
    lines.append("> 记录口径：完成状态三态对应 done / done_with_help / fail；求助完成不计入独立完成率；")
    lines.append("> 困惑点记被试原话并脱敏（不存实名），作为问题分级（阻断/严重/一般/轻微）的归因输入。")
    return "\n".join(lines)


#: 任务单表头（xlsx Sheet1「实测任务单」列序）
TASK_HEADERS: tuple[str, ...] = (
    "序号", "任务", "操作码", "API 支撑", "前置条件", "步骤要点", "完成标准", "预计分钟", "危险级",
)

#: 实测记录表表头（xlsx Sheet2「实测记录表」列序，USA-01 口径）
RECORD_HEADERS: tuple[str, ...] = (
    "序号", "任务", "被试", "完成状态（完成/求助完成/失败）", "耗时（分钟）", "求助次数",
    "错误操作数", "困惑点原话（脱敏）", "备注",
)

#: 表头样式（浅灰底 + 加粗 + 居中）
_HEADER_FILL = PatternFill("solid", fgColor="DDE6F0")
_HEADER_FONT = Font(bold=True)
_WRAP = Alignment(wrap_text=True, vertical="top")


def _write_xlsx(rows: list[dict[str, str]], path: Path) -> None:
    """生成 xlsx：Sheet1 实测任务单 + Sheet2 实测记录表（完成状态下拉约束）。"""
    workbook = Workbook()

    sheet = workbook.active
    sheet.title = "实测任务单"
    sheet.append(list(TASK_HEADERS))
    for cell in sheet[1]:
        cell.fill, cell.font, cell.alignment = _HEADER_FILL, _HEADER_FONT, _WRAP
    for row in rows:
        sheet.append([row["no"], row["title"], row["ops"], row["support"], row["preconditions"],
                      row["steps"], row["criteria"], int(row["minutes"]), row["danger"]])
    widths = {"A": 6, "B": 30, "C": 22, "D": 46, "E": 34, "F": 46, "G": 30, "H": 9, "I": 18}
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width

    record = workbook.create_sheet("实测记录表")
    record.append(list(RECORD_HEADERS))
    for cell in record[1]:
        cell.fill, cell.font, cell.alignment = _HEADER_FILL, _HEADER_FONT, _WRAP
    for row in rows:
        record.append([row["no"], row["title"], "", "", "", "", "", "", ""])
    record_widths = {"A": 6, "B": 30, "C": 10, "D": 26, "E": 12, "F": 10, "G": 12, "H": 36, "I": 18}
    for column, width in record_widths.items():
        record.column_dimensions[column].width = width
    validation = DataValidation(
        type="list", formula1=f'"{",".join(OUTCOME_OPTIONS)}"', allow_blank=True, showErrorMessage=True,
        errorTitle="完成状态取值受限", error="只允许：完成 / 求助完成 / 失败（NFR-USA-01 三态口径）",
    )
    record.add_data_validation(validation)
    validation.add(f"D2:D{len(rows) + 1}")  # 完成状态列（D）整列下拉

    workbook.save(path)


def generate(operations: Sequence[Operation], out_dir: str | Path) -> tuple[Path, Path]:
    """生成任务单两件套：xlsx + md（返回文件路径；目录自动创建）。"""
    rows = build_task_rows(operations)
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    xlsx_path = target / "知识库后台_甲方实测任务单.xlsx"
    md_path = target / "知识库后台_甲方实测任务单.md"
    _write_xlsx(rows, xlsx_path)
    md_path.write_text(_render_markdown(rows) + "\n", encoding="utf-8")
    return xlsx_path, md_path


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：装载操作清单 → 展开 20 任务 → 输出 xlsx + md；退出码 0=成功 / 1=失败。"""
    parser = argparse.ArgumentParser(description="甲方实测任务单生成器（NFR-USA-01）")
    parser.add_argument("--ops", default=str(_REPO_ROOT / "tools" / "usability" / "operations.yaml"),
                        help="运维操作声明清单（默认 tools/usability/operations.yaml）")
    parser.add_argument("--out-dir", default=str(_REPO_ROOT / "out" / "usability"),
                        help="输出目录（默认 out/usability）")
    args = parser.parse_args(list(argv if argv is not None else sys.argv[1:]))
    try:
        operations = load_operations(args.ops)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[错误] 操作清单装载失败：{exc}")
        return 1
    try:
        xlsx_path, md_path = generate(operations, args.out_dir)
    except ValueError as exc:
        print(f"[错误] 任务单生成失败：{exc}")
        return 1
    print(f"[通过] 已生成 {len(TASKS)} 个实测任务任务单：")
    print(f"  - {xlsx_path}")
    print(f"  - {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

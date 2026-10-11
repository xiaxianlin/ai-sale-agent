# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""产品文档三件套校验器（FR-SAAS-005，SR-SAAS-005 §6.3/§6.4）。

门禁三项（对齐 SR §6.3 质量门禁脚本）：
① 章节完整性：三件套各含必备章节（受众×章节矩阵，§6.1）；
② 内部链接零断链：站内相对目标必须存在（check_links）；
③ 版本记录与产品 tag 联动：三件套「产品 Tag」一致且匹配 phase<N>-v<X.Y>。

用法：python3 tools/saas/gen_docs.py [--docs docs-product]；退出码 0 全绿 / 1 有违例
（CI 阻断语义，违例逐条指明文件与原因）。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: 三件套文件（SRS §10.2 FR-SAAS-005：用户手册 / API 文档 / 部署指南）
DOCS_FILES: tuple[str, ...] = ("user-manual.md", "api-guide.md", "deploy-guide.md")

#: 必备章节（标题级关键词；SR §6.1① 受众×章节矩阵）
REQUIRED_SECTIONS: dict[str, tuple[str, ...]] = {
    "user-manual.md": ("快速开始", "开通配置", "知识库管理", "常见问题"),
    "api-guide.md": ("鉴权", "错误码", "接口清单", "示例"),
    "deploy-guide.md": ("前置要求", "Compose 部署", "Helm 部署", "回滚"),
}

#: 产品 tag 格式（phase5-v1.0 等；SR §6.4 发布规则 phase\d+-v.* 的收紧口径）
TAG_PATTERN = re.compile(r"phase\d+-v\d+\.\d+")

#: 文档属性表中的 tag 行（| 产品 Tag | phase5-v1.0 |）
_TAG_LINE_RE = re.compile(r"产品\s*Tag\s*\|\s*(phase\d+-v\d+\.\d+)")

#: Markdown 站内链接（[text](target)；外链 http(s) 与纯锚点不入校验）
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def extract_release_tag(text: str) -> str:
    """从文档文本提取「产品 Tag」版本记录（缺省返回空串）。"""
    match = _TAG_LINE_RE.search(text)
    return match.group(1) if match else ""


def extract_links(md_path: str) -> list[tuple[int, str]]:
    """提取 markdown 站内链接（行号, 相对目标）；外链与纯锚点跳过。"""
    links: list[tuple[int, str]] = []
    for line_no, line in enumerate(Path(md_path).read_text(encoding="utf-8").splitlines(),
                                   start=1):
        # 逐行提取并剥掉行内代码段，避免误报示例文本
        cleaned = re.sub(r"`[^`]*`", "", line)
        for target in _LINK_RE.findall(cleaned):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if target.startswith("#"):  # 纯页内锚点：目标即本文件
                continue
            links.append((line_no, target.split("#", 1)[0]))
    return links


def check_links(docs_dir: str) -> list[str]:
    """门禁①：站内链接目标必须存在（相对文档所在目录解析；含跨目录相对路径）。"""
    violations: list[str] = []
    base = Path(docs_dir)
    for name in DOCS_FILES:
        md = base / name
        if not md.is_file():
            violations.append(f"{name}: 文件不存在（断链校验跳过该项）")
            continue
        for line_no, target in extract_links(str(md)):
            resolved = (md.parent / target).resolve()
            if not resolved.exists():
                violations.append(f"{name}:{line_no} 链接目标不存在：{target}")
    return violations


def check_sections(docs_dir: str) -> list[str]:
    """门禁②：三件套必备章节齐备（标题 `## ` 含关键词即可）。"""
    violations: list[str] = []
    base = Path(docs_dir)
    for name, sections in REQUIRED_SECTIONS.items():
        md = base / name
        if not md.is_file():
            violations.append(f"{name}: 文件不存在（章节校验跳过该项）")
            continue
        headings = [line.lstrip("#").strip()
                    for line in md.read_text(encoding="utf-8").splitlines()
                    if line.startswith("## ")]
        for section in sections:
            if not any(section in heading for heading in headings):
                violations.append(f"{name}: 缺必备章节「{section}」")
    return violations


def check_version_tag(docs_dir: str) -> list[str]:
    """门禁③：三件套版本记录与同一产品 tag 联动（同 tag 才可发布，SR §6.4）。"""
    violations: list[str] = []
    tags: dict[str, str] = {}
    for name in DOCS_FILES:
        md = Path(docs_dir) / name
        if not md.is_file():
            violations.append(f"{name}: 文件不存在（版本校验跳过该项）")
            continue
        tag = extract_release_tag(md.read_text(encoding="utf-8"))
        if not tag:
            violations.append(f"{name}: 缺「产品 Tag」版本记录")
        else:
            tags[name] = tag
    if tags and len(set(tags.values())) > 1:
        detail = "、".join(f"{n}={t}" for n, t in tags.items())
        violations.append(f"三件套产品 tag 不一致：{detail}")
    return violations


def check_docs(docs_dir: str) -> list[str]:
    """整体门禁：三项违例合并（空 = 全绿可发布）。"""
    return [*check_links(docs_dir), *check_sections(docs_dir), *check_version_tag(docs_dir)]


def main(argv: list[str] | None = None) -> int:
    """CI 门禁入口：违例逐条打印并返回 1（阻断），全绿返回 0。"""
    parser = argparse.ArgumentParser(description="产品文档三件套校验器（零违例才可发布）")
    repo_root = Path(__file__).resolve().parents[2]
    parser.add_argument("--docs", default=str(repo_root / "docs-product"),
                        help="产品文档目录（默认 repo/docs-product）")
    args = parser.parse_args(argv)
    violations = check_docs(args.docs)
    if violations:
        for v in violations:
            print(f"[docs 门禁] {v}", file=sys.stderr)
        return 1
    print(f"[docs 门禁] 三件套全绿：{args.docs}")
    return 0


if __name__ == "__main__":  # 直接执行入口（仓库根运行）
    raise SystemExit(main())

# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""产品文档三件套测试（FR-SAAS-005，SR-SAAS-005 §6.1/§6.3/§6.4/§9；TDD 阶段①用例）。

五类场景齐备（等价类 / 边界 / 异常 / 并发 / 降级）：
- 等价类：三件套存在且章节完整、check_docs 全绿、跨文档相对链接可达；
- 边界：链接恰等仓库真实交付物路径、版本号恰等 tag 格式 phase<N>-v<X.Y>；
- 异常：构造断链/缺章节/版本漂移样例 → 校验器逐项检出且指明文件；
- 并发：三件套版本记录与同一产品 tag 联动（同 tag 才可发布）；
- 降级：门禁 exit 0/1 可作为 CI 阻断（违例非零退出）。

验收口径映射：SRS §16.6「产品文档三件套评审通过」；NFR-USA-02（简体中文）。
实现方案：docs/07-实现方案/SAAS/SR-SAAS-005-产品文档.md §6.1、§6.3、§6.4、§9
"""

from __future__ import annotations

from pathlib import Path

from tools.saas import gen_docs

REPO_ROOT = Path(__file__).resolve().parents[4]
DOCS_DIR = REPO_ROOT / "docs-product"


# ───────────────────────── 等价类：三件套存在 / 章节完整 / 门禁全绿 ─────────────────────────


class TestEquivalence:
    def test_three_docs_exist(self) -> None:
        """三件套齐备：用户手册 / API 文档 / 部署指南（SRS §10.2 FR-SAAS-005）。"""
        for name in gen_docs.DOCS_FILES:
            assert (DOCS_DIR / name).is_file(), f"缺产品文档 {name}"

    def test_required_sections_present(self) -> None:
        """章节完整性：三件套各含必备章节（受众×章节矩阵，SR §6.1）。"""
        assert gen_docs.check_sections(str(DOCS_DIR)) == []

    def test_internal_links_zero_broken(self) -> None:
        """内部链接零断链（门禁①：站内目标必须存在，SR §6.3 check_links）。"""
        assert gen_docs.check_links(str(DOCS_DIR)) == []

    def test_docs_gate_all_green(self) -> None:
        """整体门禁 check_docs：真实三件套零违例。"""
        assert gen_docs.check_docs(str(DOCS_DIR)) == []

    def test_deploy_guide_references_package_scripts(self) -> None:
        """部署指南单一来源约定：引用部署包脚本与退出码，只解释不复制。"""
        text = (DOCS_DIR / "deploy-guide.md").read_text(encoding="utf-8")
        for token in ("install.sh", "rollback.sh", "verify", "gen_config"):
            assert token in text, f"部署指南未引用部署包交付物 {token}"


# ───────────────────────── 边界：版本记录与 tag 联动 ─────────────────────────


class TestVersionTag:
    def test_all_docs_share_same_release_tag_with_record(self) -> None:
        """版本联动：三件套「产品 Tag」一致且匹配 phase<N>-v<X.Y>；文档属性表齐备
        （文档版本/变更记录，docs 管理规范）。"""
        tags = []
        for name in gen_docs.DOCS_FILES:
            text = (DOCS_DIR / name).read_text(encoding="utf-8")
            assert "文档版本" in text and "变更记录" in text
            found = gen_docs.extract_release_tag(text)
            assert found, f"{name} 缺「产品 Tag」版本记录"
            tags.append(found)
        assert len(set(tags)) == 1
        assert gen_docs.TAG_PATTERN.fullmatch(tags[0])


# ───────────────────────── 异常：断链 / 缺章节 / 版本漂移逐项检出 ─────────────────────────


class TestViolationsDetected:
    def make_broken_docs(self, tmp_path: Path, link: bool = False, section: bool = False,
                         tag: bool = False) -> str:
        """构造违例样例目录（受控注入单类违例）。"""
        header = "| 文档版本 | V1.0 |\n| 产品 Tag | phase5-v1.0 |\n"
        good = ("# 用户手册\n\n## 快速开始\n内容\n## 开通配置\n内容\n"
                "## 知识库管理\n内容\n## 常见问题\n内容\n")
        (tmp_path / "user-manual.md").write_text(header + good, encoding="utf-8")
        api = "# API 文档\n\n## 鉴权\n## 错误码\n## 接口清单\n## 示例\n"
        (tmp_path / "api-guide.md").write_text(header + api, encoding="utf-8")
        deploy = "# 部署指南\n\n## 前置要求\n## Compose 部署\n## Helm 部署\n## 回滚\n"
        if link:
            deploy += "\n详见 [不存在的文档](no-such-doc.md)\n"
        deploy_text = header + deploy
        if tag:
            deploy_text = deploy_text.replace("phase5-v1.0", "phase5-v9.9")
        if section:
            deploy_text = deploy_text.replace("## 回滚\n", "")
        (tmp_path / "deploy-guide.md").write_text(deploy_text, encoding="utf-8")
        return str(tmp_path)

    def test_broken_link_detected(self, tmp_path: Path) -> None:
        """断链样例：check_links 检出并指明文件与目标。"""
        docs = self.make_broken_docs(tmp_path, link=True)
        violations = gen_docs.check_links(docs)
        assert len(violations) == 1
        assert "no-such-doc.md" in violations[0] and "deploy-guide.md" in violations[0]

    def test_missing_section_detected(self, tmp_path: Path) -> None:
        """缺章节样例：check_sections 检出缺失章节名。"""
        docs = self.make_broken_docs(tmp_path, section=True)
        violations = gen_docs.check_sections(docs)
        assert any("回滚" in v for v in violations)

    def test_tag_drift_detected(self, tmp_path: Path) -> None:
        """版本漂移样例：deploy-guide 换 tag → check_version_tag 检出不一致。"""
        docs = self.make_broken_docs(tmp_path, tag=True)
        violations = gen_docs.check_version_tag(docs)
        assert len(violations) == 1 and "deploy-guide.md" in violations[0]

    def test_gate_exit_code_blocks_pipeline(self, tmp_path: Path) -> None:
        """CI 阻断语义：违例目录 main→1，真实三件套 main→0（降级可作门禁）。"""
        assert gen_docs.main(["--docs", str(DOCS_DIR)]) == 0
        broken = self.make_broken_docs(tmp_path, link=True)
        assert gen_docs.main(["--docs", broken]) == 1

# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""工具脚本公共自举（CR 遗留批3：五份拷贝收敛单点）。

用法（各 CLI 脚本头部）::

    from _bootstrap import bootstrap_repo
    bootstrap_repo(__file__)
"""
from __future__ import annotations

import sys
from pathlib import Path


def bootstrap_repo(file: str) -> Path:
    """仓库根入 sys.path（src 布局）+ 标准库 platform 遮蔽自愈。

    仓库根目录进入 sys.path 后，顶层 ``platform`` 包（现 ``foundation`` 前身改名
    遗留的 pycache 影子）可能遮蔽标准库 ``platform``——httpx→rich→attrs 链在其上
    崩溃；此处幂等回填标准库语义（与 src 布局迁移前的 conftest 手法同源）。
    """
    root = Path(file).resolve().parents[2]
    src = str(root / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    try:  # 标准库 platform 语义回填（幂等）
        import platform as _stdlib_platform

        sys.modules.setdefault("platform", _stdlib_platform)
    except ImportError:
        pass
    return root


def restore_stdlib_platform() -> None:
    """向后兼容别名（既有脚本按旧函数名调用）。"""
    bootstrap_repo(__file__)

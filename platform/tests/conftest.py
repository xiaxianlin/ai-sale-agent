"""知识产权归甲方所有（COM-IP-01）

platform/tests 测试环境自举（在导入任何被测模块/依赖前执行）。

为何需要处理平台名冲突：仓库顶层包目录名为 ``platform``（开发规范 §2 代码组织），
从仓库根运行 pytest 时它先于标准库被解析——pytest 自身 ``import platform`` 拿到的是
本地空包，随后 httpx→rich→attrs 链路上的 ``platform.python_implementation()`` 会抛
AttributeError（FastAPI TestClient 不可用）。这里把标准库 platform 强制装载并回填到
已被缓存的包对象上，恢复测试进程内的标准库语义（本地包的子模块导入不受影响）。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import sysconfig
from types import ModuleType


def _restore_stdlib_platform() -> None:
    """将标准库 platform 模块语义恢复到 sys.modules（幂等，可安全重复调用）。

    - 已缓存且具备标准库能力（如从仓库外运行）：直接返回；
    - 未缓存：以标准库路径装载并注册（防御分支，pytest 实际总会先缓存本地包）；
    - 已缓存为本地空包（常规路径）：执行标准库源码得到完整模块后，把公开属性
      回填到缓存对象上——pytest 内部持有的旧模块引用同样恢复可用。
    """
    cached: ModuleType | None = sys.modules.get("platform")
    if cached is not None and hasattr(cached, "python_implementation"):
        return
    std_path = os.path.join(sysconfig.get_paths()["stdlib"], "platform.py")
    spec = importlib.util.spec_from_file_location("platform", std_path)
    if spec is None or spec.loader is None:  # 防御：标准库定位失败属环境损坏，应当显式失败
        raise RuntimeError("无法定位标准库 platform 模块（测试环境损坏）")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if cached is None:
        sys.modules["platform"] = module
        return
    for name, value in vars(module).items():
        if not name.startswith("__"):
            setattr(cached, name, value)


_restore_stdlib_platform()

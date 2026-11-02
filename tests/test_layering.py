"""分层守卫（借鉴 genie-service test_layering）——依赖方向即契约。

铁律（违反即本测试红灯）：
  1. iris_service 不得 import iris_foundation / iris_sdk（核不依赖触手与接口）
  2. iris_sdk     不得 import iris_foundation / iris_service（接口只走线协议）
  3. iris_foundation 不得 import iris_service / iris_sdk（触手不回望）
扫描方式：AST 解析 import/import from（含函数体内的 lazy import），
比运行时 sys.modules 更严——没执行到的违规也算违规。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # iris_ver2/iris_ver2/

# 包名 → 仓库目录名（Foundation/Service/SDK）
PKG_DIR = {
    "iris_foundation": "Foundation",
    "iris_service": "Service",
    "iris_sdk": "SDK",
}

FORBIDDEN = {
    "iris_service": ("iris_foundation", "iris_sdk"),
    "iris_sdk": ("iris_foundation", "iris_service"),
    "iris_foundation": ("iris_service", "iris_sdk"),
}


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"),
                     filename=str(path))   # utf-8-sig：兼容带 BOM 的源文件
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def _violations() -> list[str]:
    out = []
    for pkg, banned in FORBIDDEN.items():
        pkg_src = ROOT / PKG_DIR[pkg] / "src" / pkg
        if not pkg_src.exists():
            out.append(f"[缺失] {pkg} 源码目录不存在: {pkg_src}")
            continue
        for py in pkg_src.rglob("*.py"):
            for mod in _imports_of(py):
                for b in banned:
                    if mod == b or mod.startswith(b + "."):
                        out.append(f"[违规] {py.relative_to(ROOT)} "
                                   f"imports {mod}（{pkg} 禁依赖 {b}）")
    return out


def test_layering():
    v = _violations()
    assert not v, "分层违规:\n" + "\n".join(v)


if __name__ == "__main__":
    v = _violations()
    if v:
        print("\n".join(v))
        raise SystemExit(1)
    print("LAYERING OK：三包依赖方向全部合规")

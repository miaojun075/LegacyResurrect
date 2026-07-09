#!/usr/bin/env python3
"""
XinResurrect Offline Install Helper — 在离线信创环境上执行 .deb 包批量安装。

用法:
  python3 offline_install_helper.py ./depot [--dry-run] [--skip-depcheck]

功能:
  1. 读 depot_manifest.json → 校验 SHA256
  2. dpkg -l 查已装包 → 跳过已满足的
  3. 依赖顺序排序（简单拓扑）
  4. 逐个 dpkg -i（自动处理依赖失败）
  5. 最终 apt-get -f install 兜底修复

要求: Python 3.6+, dpkg, apt-get (离线也可用，仅用已下载包)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd: str, check: bool = False) -> Tuple[int, str, str]:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
    if check and r.returncode != 0:
        print(f"  [ERR] {r.stderr[:500]}")
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def get_installed_packages() -> Dict[str, str]:
    """返回 {包名: 版本号} 所有已安装包"""
    code, out, _ = run("dpkg -l")
    if code != 0:
        return {}
    installed: Dict[str, str] = {}
    for line in out.split("\n"):
        if line.startswith("ii"):
            parts = line.split()
            if len(parts) >= 3:
                # dpkg -l outputs "libmd0:amd64" — strip architecture suffix
                pkg_name = parts[1].split(":")[0]
                installed[pkg_name] = parts[2]
    return installed


def load_manifest(depot: Path) -> Tuple[dict, List[str]]:
    """加载清单并校验，返回 (manifest, 错误列表)"""
    mf_path = depot / "depot_manifest.json"
    if not mf_path.exists():
        return {}, [f"清单文件不存在: {mf_path}"]

    try:
        manifest = json.loads(mf_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {}, [f"清单 JSON 解析失败: {e}"]

    errors = []
    packages = manifest.get("packages", {})
    for name, info in packages.items():
        deb_path = depot / info["file"]
        if not deb_path.exists():
            errors.append(f"包文件缺失: {info['file']}")
            continue
        actual_sha = sha256_file(deb_path)
        expected_sha = info.get("sha256", "")
        if expected_sha and actual_sha != expected_sha:
            errors.append(
                f"SHA256 不匹配: {info['file']}\n"
                f"  期望: {expected_sha}\n"
                f"  实际: {actual_sha}"
            )

    return manifest, errors


def get_deb_depends(deb_path: Path) -> Set[str]:
    """从 .deb 文件提取 Depends 字段中的包名集合。"""
    code, out, _ = run(f"dpkg-deb -f {deb_path} Depends")
    if code != 0 or not out:
        return set()
    # e.g. "libc6 (>= 2.14), libx11-6 (>= 2:1.7.0)"
    names = set()
    for part in out.split(","):
        pkg = part.strip().split()[0]  # strip version constraint
        names.add(pkg)
    return names


def topological_sort(packages: Dict[str, dict], depot: Path) -> List[str]:
    """
    按依赖关系拓扑排序，优先装被依赖的包。
    使用 dpkg-deb -f 解析每个 .deb 的真实 Depends。
    """
    # Build dependency graph
    graph: Dict[str, Set[str]] = {}
    for pkg_name in packages:
        info = packages[pkg_name]
        deb_path = depot / info["file"]
        deps = get_deb_depends(deb_path)
        # Only consider deps that are also in our package set
        graph[pkg_name] = deps & set(packages.keys())

    # Kahn's algorithm
    in_degree = {pkg: 0 for pkg in packages}
    for pkg, deps in graph.items():
        for dep in deps:
            if dep in in_degree:
                in_degree[pkg] = in_degree.get(pkg, 0)  # ensure exists

    # Count in-degrees
    reverse: Dict[str, Set[str]] = defaultdict(set)
    for pkg, deps in graph.items():
        for dep in deps:
            reverse[dep].add(pkg)

    in_degree = {pkg: len(graph.get(pkg, set())) for pkg in packages}

    queue = sorted([pkg for pkg, deg in in_degree.items() if deg == 0])
    result = []
    while queue:
        pkg = queue.pop(0)
        result.append(pkg)
        for dependent in sorted(reverse.get(pkg, set())):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    # Fallback: if cycle detected, append remaining
    remaining = sorted(set(packages.keys()) - set(result))
    return result + remaining


def check_installed(pkg_name: str) -> bool:
    """检查包是否真正处于 installed 状态。"""
    fmt = chr(36) + '{Status}'  # '${Status}' — avoid shell escaping issues
    r = subprocess.run(
        ['dpkg-query', '-W', '-f', fmt, pkg_name],
        capture_output=True, text=True
    )
    return r.returncode == 0 and "install ok installed" in r.stdout


def install_single(deb_path: Path, pkg_name: str, dry_run: bool = False) -> bool:
    """安装单个 .deb，返回是否真正安装成功"""
    if dry_run:
        print(f"  [dry-run] dpkg -i {deb_path}")
        return True

    code, out, err = run(f"dpkg -i {deb_path}")
    combined = out + "\n" + err

    # 真正验证：dpkg-query 必须返回 "install ok installed"
    if check_installed(pkg_name):
        return True

    # 依赖未满足 → 标记为待修复
    if "dependency problems" in combined.lower() or "depends on" in combined.lower():
        print(f"  [WARN] 依赖未满足（稍后修复）: {deb_path.name}")
        return False  # 不算成功，需要 apt -f install

    # 其他错误
    if code != 0:
        print(f"  [ERR] 安装失败: {deb_path.name}\n    {err[:300]}")
        return False

    # dpkg 返回 0 但状态不对（罕见）
    print(f"  [WARN] dpkg 返回 0 但包未 installed: {deb_path.name}")
    return False


def fix_broken(dry_run: bool = False) -> bool:
    """apt-get -f install 修复依赖"""
    if dry_run:
        print("[fix] apt-get -f install -y")
        return True

    code, _, err = run("apt-get -f install -y --no-download")
    return code == 0


def main():
    p = argparse.ArgumentParser(description="XinResurrect 离线批量安装器")
    p.add_argument("depot", help="离线 .deb 仓库目录")
    p.add_argument("--dry-run", action="store_true", help="仅打印计划，不安装")
    p.add_argument("--skip-depcheck", action="store_true", help="跳过 SHA256 校验")
    p.add_argument("--force-all", action="store_true", help="即使已安装也重装")
    args = p.parse_args()

    depot = Path(args.depot).resolve()
    if not depot.is_dir():
        print(f"[ERROR] 目录不存在: {depot}")
        sys.exit(1)

    # 1. 加载 + 校验
    manifest, errors = load_manifest(depot)
    if not args.skip_depcheck and errors:
        print("[ERROR] 清单校验失败:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    if errors:
        print(f"[WARN] 跳过 {len(errors)} 个校验错误（--skip-depcheck）")

    packages = manifest.get("packages", {})
    if not packages:
        print("[ERROR] 清单为空")
        sys.exit(1)

    print(f"[installer] 离线包: {len(packages)} 个")
    print(f"[installer] 仓库路径: {depot}")

    # 2. 查已装包
    installed = get_installed_packages()
    print(f"[installer] 系统已安装: {len(installed)} 个包")

    # 3. 过滤已满足的
    to_install: List[str] = []
    skipped: List[str] = []
    for pkg_name in packages:
        if pkg_name in installed and not args.force_all:
            skipped.append(f"{pkg_name} (已安装 {installed[pkg_name]})")
        else:
            to_install.append(pkg_name)

    if skipped:
        print(f"[installer] 跳过 {len(skipped)} 个已安装包:")
        for s in sorted(skipped):
            print(f"  - {s}")

    if not to_install:
        print("[installer] 所有包已安装，无需操作")
        return

    # 4. 排序 + 安装
    order = topological_sort({k: packages[k] for k in to_install}, depot)
    print(f"\n[installer] 待安装: {len(order)} 个包")

    ok, fail = 0, 0
    for pkg_name in order:
        info = packages[pkg_name]
        deb_path = depot / info["file"]
        print(f"[installer] {pkg_name} ({info.get('size', 0):,} bytes)")
        if install_single(deb_path, pkg_name, args.dry_run):
            ok += 1
        else:
            fail += 1

    # 5. dpkg 兜底 fix
    print(f"\n[installer] 单个安装: {ok} 成功, {fail} 失败")
    if fail > 0:
        print("[installer] 尝试 apt-get -f install 修复依赖...")
        if fix_broken(args.dry_run):
            print("[installer] 依赖修复成功")
        else:
            print("[installer] 依赖修复失败，请手动检查: apt-get -f install")
            sys.exit(1)

    # 6. 复查安装状态
    print("\n[installer] 复查安装状态...")
    installed_after = get_installed_packages()
    missed = [p for p in to_install if p not in installed_after]
    if missed:
        print(f"[installer] ⚠️ 仍未安装: {len(missed)} 个")
        for m in sorted(missed):
            print(f"  - {m}")
    else:
        print(f"[installer] ✓ 全部 {len(to_install)} 个包已安装")


if __name__ == "__main__":
    main()

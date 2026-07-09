#!/usr/bin/env python3
"""
XinResurrect Dependency Downloader — 递归下载 .deb 包及其全部依赖。

用法:
  python3 dependency_downloader.py libx11-6 libxext6 libxrender1 ... [--depot ./depot]

输出:
  depot/
  ├── libx11-6_1.7.2_amd64.deb
  ├── libxau6_1.0.9_amd64.deb      ← 自动递归的传递依赖
  ├── ...
  └── depot_manifest.json           ← 包清单 + SHA256

要求: 在可联网的 Debian/Ubuntu/Kylin 上运行，需要 apt, dpkg, sha256sum。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Set, Tuple


# ── 配置 ──────────────────────────────────────────

# 虚拟包（无实际 .deb 文件，跳过下载但继续追踪）
VIRTUAL_PACKAGES = {
    "libx11-dev", "libxext-dev", "libc6-dev", "libc-dev",
    "x11-common", "libgl1", "libgl1-mesa-glx",
}

# 停止递归的顶层元包（它们自己的依赖由 dpkg 保证已安装）
STOP_META = {"libc6", "libgcc-s1", "libstdc++6", "libcrypt1", "libc6-dev"}


def run(cmd: str) -> Tuple[int, str, str]:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def get_arch() -> str:
    code, out, _ = run("dpkg --print-architecture")
    return out if code == 0 else "amd64"


def get_direct_deps(pkg: str) -> Set[str]:
    """获取包的直接运行时依赖（Depends, Pre-Depends 等）"""
    code, out, _ = run(f"apt-cache depends {pkg}")
    if code != 0:
        return set()

    deps: Set[str] = set()
    for line in out.split("\n"):
        line = line.strip()
        # 只抓 Depends / PreDepends，忽略 Conflicts/Breaks/Replaces/Suggests/Recommends
        for prefix in ("Depends:", "PreDepends:"):
            if line.startswith(prefix):
                dep = line[len(prefix):].strip()
                # 去掉版本约束（>= 1.0, << 2.0 等）
                dep = dep.split()[0] if dep else ""
                # 去掉 :arch 后缀
                dep = dep.split(":")[0]
                if dep and dep not in STOP_META:
                    deps.add(dep)
    return deps


def resolve_tree(packages: list, max_depth: int = 8) -> Set[str]:
    """递归解析完整依赖树，带环检测和深度限制"""
    resolved: Set[str] = set()
    visited: Set[str] = set()

    def _resolve(pkg: str, depth: int):
        if depth > max_depth or pkg in visited or pkg in STOP_META:
            return
        visited.add(pkg)
        if pkg in VIRTUAL_PACKAGES:
            return
        resolved.add(pkg)
        for dep in get_direct_deps(pkg):
            _resolve(dep, depth + 1)

    for p in packages:
        _resolve(p, 0)

    return resolved


def download_package(pkg: str, depot: Path, arch: str) -> Tuple[bool, str]:
    """下载单个 .deb 包到 depot 目录"""
    dest = depot / f"{pkg}_*.deb"
    existing = list(depot.glob(f"{pkg}_*.deb"))
    if existing:
        return True, f"skip (already in depot: {existing[0].name})"

    code, out, err = run(f"apt-get download {pkg}:{arch}")
    if code != 0:
        code2, out2, err2 = run(f"apt-get download {pkg}")
        if code2 != 0:
            return False, f"FAIL ({err or err2})"
        out = out2

    # 移动到 depot
    deb_files = list(Path(".").glob(f"{pkg}_*.deb"))
    if deb_files:
        deb_files[0].rename(dest / deb_files[0].name)
        return True, deb_files[0].name
    return False, "no .deb file produced"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(depot: Path) -> dict:
    """生成 depot 目录下的包清单"""
    manifest = {"format": "xinresurrect-depot-v1", "packages": {}}
    for deb in sorted(depot.glob("*.deb")):
        pkg_name = deb.stem.rsplit("_", 1)[0]
        manifest["packages"][pkg_name] = {
            "file": deb.name,
            "sha256": sha256_file(deb),
            "size": deb.stat().st_size,
        }
    return manifest


# ── CLI ──────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="XinResurrect Dependency Downloader — 递归下载 .deb 包"
    )
    p.add_argument("packages", nargs="+", help="要下载的顶层包名")
    p.add_argument("--depot", "-d", default="./depot",
                   help="离线仓库目录 (default: ./depot)")
    p.add_argument("--max-depth", type=int, default=8,
                   help="最大递归深度 (default: 8)")
    p.add_argument("--manifest", action="store_true",
                   help="仅生成清单，不下载")
    return p.parse_args()


def main():
    args = parse_args()
    depot = Path(args.depot).resolve()
    depot.mkdir(parents=True, exist_ok=True)

    arch = get_arch()
    print(f"[downloader] 架构: {arch}")
    print(f"[downloader] 目标: {len(args.packages)} 个顶层包")

    # 1. 解析依赖树
    print("[downloader] 解析依赖树...")
    all_pkgs = resolve_tree(args.packages, args.max_depth)
    print(f"[downloader] 依赖树: {len(all_pkgs)} 个包")
    for p in sorted(all_pkgs):
        print(f"  - {p}")

    if args.manifest:
        manifest = build_manifest(depot)
        mf_path = depot / "depot_manifest.json"
        mf_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[downloader] 清单已生成: {mf_path}")
        return

    # 2. 逐个下载
    ok, fail = 0, 0
    for pkg in sorted(all_pkgs):
        print(f"[downloader] {pkg} ... ", end="", flush=True)
        success, msg = download_package(pkg, depot, arch)
        if success:
            print(f"✓ {msg}")
            ok += 1
        else:
            print(f"✗ {msg}")
            fail += 1

    # 3. 生成清单
    manifest = build_manifest(depot)
    mf_path = depot / "depot_manifest.json"
    mf_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n[downloader] 完成: {ok} 成功, {fail} 失败")
    print(f"[downloader] 清单: {mf_path}")
    print(f"[downloader] 总计: {len(list(depot.glob('*.deb')))} 个 .deb 文件")
    print(f"[downloader] 交付命令: tar czf xinresurrect_depot.tar.gz -C {depot.parent} {depot.name}/")

    if fail > 0:
        print("[downloader] ⚠️ 有失败的包，请检查 apt 源配置")
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
XinResurrect Pipeline Orchestrator — 飞前检查 + 离线包生成

输入: ai_fixer_v2.py 输出的修复报告 (--report) + 可选的离线 depot 目录
输出: preflight_report.json + deploy_guide.txt + offline_fix_script.sh

五分类判定:
  offline     — 本地 .deb / .dll 已就绪，一键执行
  need_download — 缺离线包，给出精确下载命令
  config      — 纯 Wine 配置，无需外网
  manual      — 需要人工判断（许可证/硬件/复杂版本冲突）
  block       — AI 标记的阻断项

要求: Python 3.8+, 纯 stdlib
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

IS_WINDOWS = platform.system() == "Windows"

# ── 分类常量 ────────────────────────────────────

CATEGORY = {
    "offline": "offline",
    "need_download": "need_download",
    "config": "config",
    "manual": "manual",
    "block": "block",
}

# 常见 .dll 下载源映射
DLL_SOURCES = {
    "msvcp90.dll": "https://aka.ms/vs/16/release/vc_redist.x86.exe (VC++ 2008 SP1)",
    "msvcp100.dll": "https://aka.ms/vs/16/release/vc_redist.x86.exe (VC++ 2010)",
    "msvcp140.dll": "https://aka.ms/vs/16/release/vc_redist.x86.exe (VC++ 2015-2022)",
    "vcruntime140.dll": "https://aka.ms/vs/16/release/vc_redist.x86.exe (VC++ 2015-2022)",
}

# 常见 .so 下载源映射（按发行版）
SO_SOURCES_DEBIAN = {
    "libx11-6": "apt-get download libx11-6 libxau6 libxdmcp6 libxcb1",
    "libxext6": "apt-get download libxext6",
    "libxrender1": "apt-get download libxrender1",
    "libxrandr2": "apt-get download libxrandr2",
    "libxcursor1": "apt-get download libxcursor1",
    "libxinerama1": "apt-get download libxinerama1",
    "libxcomposite1": "apt-get download libxcomposite1",
    "libxi6": "apt-get download libxi6 libxtst6",
    "libxfixes3": "apt-get download libxfixes3",
    "libxdamage1": "apt-get download libxdamage1",
    "libgl1-mesa-glx": "apt-get download libgl1-mesa-glx libglx0",
}

# ── 加载 ────────────────────────────────────────

def load_report(path: str) -> Dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    actions: List[Dict] = []
    for entry in data.get("history", []):
        actions.extend(entry.get("actions", []))
    return {
        "status": data.get("status"),
        "attempts": data.get("attempts"),
        "actions": actions,
    }


def scan_depot(depot_path: Optional[str]) -> Dict[str, Path]:
    """扫描 depot 目录中已有的 .deb / .dll / .so 文件"""
    depot: Dict[str, Path] = {}
    if not depot_path:
        return depot
    dp = Path(depot_path)
    if not dp.is_dir():
        return depot
    for f in dp.rglob("*"):
        if f.is_file() and f.suffix in (".deb", ".dll", ".so", ".exe"):
            depot[f.name.lower()] = f
    return depot


# ── 预检逻辑 ────────────────────────────────────

def preflight_action(
    action: Dict[str, Any],
    depot: Dict[str, Path],
    os_type: str,
) -> Dict[str, Any]:
    """
    对单个动作进行五分类判定，返回：
    {
        "category": "offline"|"need_download"|"config"|"manual"|"block",
        "reason": "...",
        "detail": {...},   # 具体操作指引
    }
    """
    a_type = action.get("action", "")
    target = action.get("target", "")
    params = action.get("parameters", {})
    reason = action.get("reason", "")
    confidence = action.get("confidence", "?")

    base = {
        "action": a_type,
        "target": target,
        "params": params,
        "ai_reason": reason,
        "ai_confidence": confidence,
    }

    # ── block: AI 标记的阻断项 ──
    if a_type == "block":
        return {
            **base,
            "category": CATEGORY["block"],
            "reason": "AI 标记为阻断项，需人工判断",
            "detail": {
                "blocker": params.get("blocker", target),
                "suggested_action": params.get("suggested_manual_action", "请联系原厂或寻找替代方案"),
            },
        }

    # ── configure_layer: 纯 Wine 配置 ──
    if a_type == "configure_layer":
        return {
            **base,
            "category": CATEGORY["config"],
            "reason": "Wine 配置操作，无需外网",
            "detail": {
                "wine_command": params.get("wine_command", ""),
                "description": "在信创环境中执行 winecfg 或 reg add 命令",
            },
        }

    # ── install_dependency: 需要判断离线状态 ──
    if a_type == "install_dependency":
        method = params.get("method", "apt")
        package = params.get("package", target)

        # 离线安装包 (exe)
        if method == "offline_installer":
            installer_path = params.get("installer_path", package)
            fname = os.path.basename(installer_path).lower()
            if fname in depot:
                return {
                    **base,
                    "category": CATEGORY["offline"],
                    "reason": f"离线安装包已就绪: {depot[fname].name}",
                    "detail": {"file": str(depot[fname]), "action": "silent_install"},
                }
            return {
                **base,
                "category": CATEGORY["need_download"],
                "reason": f"离线安装包未找到: {fname}",
                "detail": {
                    "missing": fname,
                    "download_hint": f"从可信源下载 {installer_path} 放入 depot/",
                },
            }

        # apt 包
        if os_type == "linux" and method in ("apt", "apt_key", ""):
            # 检查 depot 中是否有对应 .deb
            deb_matches = [v for k, v in depot.items()
                          if k.startswith(package.lower() + "_") and k.endswith(".deb")]
            if deb_matches:
                return {
                    **base,
                    "category": CATEGORY["offline"],
                    "reason": f".deb 包已就绪: {deb_matches[0].name}",
                    "detail": {"file": str(deb_matches[0]), "size": deb_matches[0].stat().st_size},
                }

            # 给出下载命令
            download_cmd = SO_SOURCES_DEBIAN.get(package,
                f"apt-get download {package}")
            return {
                **base,
                "category": CATEGORY["need_download"],
                "reason": f".deb 包未缓存: {package}",
                "detail": {
                    "missing": f"{package}_*.deb",
                    "download_cmd": download_cmd,
                    "hint": f"在可联网的 Debian/麒麟上运行: {download_cmd}",
                },
            }

        # Windows winget/离线
        if os_type == "windows":
            return {
                **base,
                "category": CATEGORY["need_download"],
                "reason": f"Windows 运行时包: {package}",
                "detail": {
                    "missing": package,
                    "download_hint": DLL_SOURCES.get(package, f"从 Microsoft 官网下载 {package}"),
                },
            }

    # ── copy_dependency: 需要判断文件是否存在 ──
    if a_type == "copy_dependency":
        src = params.get("source", params.get("src", ""))
        fname = os.path.basename(src).lower()

        # depot 中有
        if fname in depot:
            return {
                **base,
                "category": CATEGORY["offline"],
                "reason": f"依赖文件已就绪: {depot[fname].name}",
                "detail": {
                    "file": str(depot[fname]),
                    "destination": params.get("destination", params.get("dst", "")),
                },
            }

        # src 路径存在
        if src and os.path.isfile(src):
            return {
                **base,
                "category": CATEGORY["offline"],
                "reason": f"源文件存在: {src}",
                "detail": {
                    "file": src,
                    "destination": params.get("destination", params.get("dst", "")),
                },
            }

        return {
            **base,
            "category": CATEGORY["need_download"],
            "reason": f"依赖文件未找到: {fname}",
            "detail": {
                "missing": fname,
                "download_hint": DLL_SOURCES.get(fname, f"从可联网环境获取 {fname} 放入 depot/"),
            },
        }

    # ── 未知动作: 标记为 manual ──
    return {
        **base,
        "category": CATEGORY["manual"],
        "reason": f"未识别的动作类型 '{a_type}'，需人工判断",
        "detail": {"hint": "请检查 AI 输出或手动补充操作步骤"},
    }


def build_preflight_report(
    report: Dict[str, Any],
    depot: Dict[str, Path],
    source_platform: str,
) -> Dict[str, Any]:
    """完整预检报告"""
    actions = report["actions"]
    classified = []

    counts = {v: 0 for v in CATEGORY.values()}
    for i, act in enumerate(actions):
        result = preflight_action(act, depot, source_platform)
        result["step"] = i + 1
        classified.append(result)
        counts[result["category"]] += 1

    can_offline = (
        counts["need_download"] == 0
        and counts["manual"] == 0
        and counts["block"] == 0
    )

    # 按优先级排序：block → manual → need_download → config → offline
    priority = {"block": 0, "manual": 1, "need_download": 2, "config": 3, "offline": 4}
    classified.sort(key=lambda x: priority.get(x["category"], 9))

    return {
        "format": "xinresurrect-preflight-v1",
        "generated_utc": __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(),
        "source_platform": source_platform,
        "summary": {
            "total_actions": len(actions),
            "can_fully_offline": can_offline,
            **counts,
        },
        "actions": classified,
    }


def generate_deploy_guide(
    preflight: Dict[str, Any],
    fix_script_path: Optional[str],
) -> str:
    """生成部署指南 .txt"""
    summary = preflight["summary"]
    source = preflight["source_platform"]
    lines = [
        "=" * 60,
        "  XinResurrect 部署指南",
        f"  生成时间: {preflight['generated_utc']}",
        f"  目标平台: {source}",
        "=" * 60,
        "",
        "=== 预检摘要 ===",
        f"  总动作数: {summary['total_actions']}",
        f"  可离线执行: {summary['offline']}",
        f"  需下载:     {summary['need_download']}",
        f"  纯配置:     {summary['config']}",
        f"  需人工:     {summary['manual']}",
        f"  AI阻断:     {summary['block']}",
        "",
    ]

    if summary["can_fully_offline"]:
        lines.append(">>> 全部动作可离线执行，直接运行修复脚本即可。")
        lines.append("")
    else:
        lines.append(">>> 以下操作需要在离线环境执行前完成：")
        lines.append("")

    # 分组输出
    groups = {
        "need_download": ("=== 需要在联网环境下载 ===", []),
        "config": ("=== Wine 配置（离线执行） ===", []),
        "manual": ("=== 需要人工判断 ===", []),
        "block": ("=== AI 阻断项（必须人工介入） ===", []),
    }

    for act in preflight["actions"]:
        cat = act["category"]
        if cat in groups:
            groups[cat][1].append(act)

    for cat, (header, items) in groups.items():
        if not items:
            continue
        lines.append(header)
        lines.append("")
        for i, act in enumerate(items):
            lines.append(f"  [{act['step']}] {act['action']} → {act['target']}")
            lines.append(f"      原因: {act['reason']}")
            if cat == "need_download":
                dl = act["detail"]
                if "download_cmd" in dl:
                    lines.append(f"      命令: {dl['download_cmd']}")
                if "download_hint" in dl:
                    lines.append(f"      提示: {dl['download_hint']}")
                if "missing" in dl:
                    lines.append(f"      缺失: {dl['missing']}")
            elif cat == "manual":
                lines.append(f"      提示: {act['detail'].get('hint', '请评估')}")
            elif cat == "block":
                lines.append(f"      阻断: {act['detail'].get('blocker', '')}")
                lines.append(f"      建议: {act['detail'].get('suggested_action', '')}")
            elif cat == "config":
                lines.append(f"      命令: {act['detail'].get('wine_command', '')}")
            lines.append("")

    # 执行步骤
    lines.append("=== 离线环境执行步骤 ===")
    lines.append("")
    if fix_script_path:
        lines.append(f"  1. 将本目录复制到目标信创环境")
        lines.append(f"  2. 执行修复脚本: bash {os.path.basename(fix_script_path)}")
        lines.append(f"  3. 检查输出中的 Wine 版本和程序启动状态")
    else:
        offline_count = sum(1 for a in preflight["actions"] if a["category"] in ("offline", "config"))
        if offline_count:
            lines.append(f"  1. 将 depot/ 目录复制到目标环境")
            lines.append(f"  2. 运行: python3 offline_install_helper.py depot/")
            lines.append(f"  3. 按需执行 Wine 配置命令")
    lines.append("")
    lines.append("=" * 60)

    return "\n".join(lines)


def build_offline_bundle(
    preflight: Dict[str, Any],
    depot: Dict[str, Path],
    output_dir: str,
) -> Path:
    """生成离线包目录"""
    bundle = Path(output_dir) / "offline_bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    depot_bundle = bundle / "depot"
    depot_bundle.mkdir(exist_ok=True)

    copied = 0
    for act in preflight["actions"]:
        if act["category"] == "offline":
            file_path = act["detail"].get("file", "")
            if file_path and os.path.isfile(file_path):
                dest = depot_bundle / os.path.basename(file_path)
                if not dest.exists():
                    shutil.copy2(file_path, dest)
                    copied += 1

    print(f"[orchestrator] 离线包: {copied} 个文件复制到 {depot_bundle}")
    return depot_bundle


# ── CLI ──────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="XinResurrect Pipeline Orchestrator — 预检 + 离线包生成"
    )
    p.add_argument("--report", "-r", required=True,
                   help="ai_fixer_v2.py 输出的修复报告 JSON")
    p.add_argument("--unified", "-u",
                   help="context_bridge.py 输出的统一上下文")
    p.add_argument("--depot", "-d", default="./depot",
                   help="离线 .deb/.dll 仓库目录 (default: ./depot)")
    p.add_argument("--os", choices=["linux", "windows"], default="linux",
                   help="目标操作系统 (default: linux)")
    p.add_argument("--fix-script", help="已有修复脚本路径 (executor.py 输出)")
    p.add_argument("--output-dir", "-o", default=".",
                   help="输出目录 (default: .)")
    return p.parse_args()


def main():
    args = parse_args()

    # 1. 加载报告
    report = load_report(args.report)
    if not report["actions"]:
        print("[orchestrator] 报告中没有修复动作")
        return

    # 2. 扫描 depot
    depot = scan_depot(args.depot)
    print(f"[orchestrator] depot 已缓存: {len(depot)} 个文件")
    print(f"[orchestrator] 修复动作: {len(report['actions'])} 个")
    print()

    # 3. 预检
    preflight = build_preflight_report(report, depot, args.os)
    pf_path = Path(args.output_dir) / "preflight_report.json"
    pf_path.write_text(json.dumps(preflight, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[orchestrator] 预检报告: {pf_path}")

    # 4. 摘要
    s = preflight["summary"]
    print(f"  离线:     {s['offline']}  | 需下载: {s['need_download']}")
    print(f"  配置:     {s['config']}  | 需人工: {s['manual']}  | 阻断: {s['block']}")
    print(f"  可全离线: {'YES' if s['can_fully_offline'] else 'NO — 请在联网环境完成上述下载'}")
    print()

    # 5. 生成离线包
    depot_bundle = build_offline_bundle(preflight, depot, args.output_dir)

    # 6. 生成部署指南
    guide = generate_deploy_guide(preflight, args.fix_script)
    guide_path = Path(args.output_dir) / "deploy_guide.txt"
    guide_path.write_text(guide, encoding="utf-8")
    print(f"[orchestrator] 部署指南: {guide_path}")

    # 7. 如果有 fix_script，复制到 bundle
    if args.fix_script and os.path.isfile(args.fix_script):
        bundle = Path(args.output_dir) / "offline_bundle"
        dest = bundle / os.path.basename(args.fix_script)
        shutil.copy2(args.fix_script, dest)
        os.chmod(dest, 0o755)
        print(f"[orchestrator] 修复脚本: {dest}")

    # 8. 打包提示
    bundle = Path(args.output_dir) / "offline_bundle"
    if bundle.exists():
        print(f"\n[orchestrator] offline_bundle 目录大小: "
              f"{sum(f.stat().st_size for f in bundle.rglob('*') if f.is_file()):,} bytes")
        print(f"[orchestrator] 交付命令: tar czf xinresurrect_offline_bundle.tar.gz "
              f"-C {bundle.parent} {bundle.name}/")

    # 9. 如果有 need_download，输出下载命令汇总
    download_acts = [a for a in preflight["actions"] if a["category"] == "need_download"]
    if download_acts:
        print(f"\n[orchestrator] === 需在联网环境下载 ({len(download_acts)} 项) ===")
        print("[orchestrator] 推荐使用 dependency_downloader.py 一键下载:")
        pkgs = list(set(a["target"] for a in download_acts
                       if a["action"] == "install_dependency"))
        if pkgs:
            print(f"  python3 dependency_downloader.py {' '.join(pkgs)} --depot depot/")
        for a in download_acts:
            if a["action"] == "copy_dependency":
                fname = a["detail"].get("missing", a["target"])
                print(f"  # 手动下载 {fname} 放入 depot/")


if __name__ == "__main__":
    main()

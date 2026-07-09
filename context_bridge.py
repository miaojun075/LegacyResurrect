#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
context_bridge.py — 三端探针统一桥接层
========================================
将 Windows (packager + env_collector)、Linux (xin_probe)、
XP (probe_xp + parse_probe) 的原始输出归一化为 UnifiedContext JSON。

用法：
    python context_bridge.py --platform windows \
        --packager packager_report.json \
        --environment env_fingerprint.json \
        --out unified_context.json

    python context_bridge.py --platform linux \
        --environment xin_env_fingerprint.json \
        --out unified_context.json

    python context_bridge.py --platform xp \
        --probe probe_result.json \
        --out unified_context.json

输出直接喂给 ai_fixer.py（稍后升级为 v2.0 支持 UnifiedContext）。
"""

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── 核心桥接函数 ──────────────────────────────────────────

def unify(
    source_platform: str,
    packager: Optional[Dict[str, Any]] = None,
    environment: Optional[Dict[str, Any]] = None,
    xp_probe: Optional[Dict[str, Any]] = None,
    scan_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    输入三端任意组合，输出规范化 UnifiedContext v2.0。
    """
    unified: Dict[str, Any] = {
        "schema_version": "2.0",
        "session_id": uuid.uuid4().hex[:12],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source_platform": source_platform,
        "target_app": {},
        "crash_context": {},
        "environment": {},
    }

    # 兜底空值
    environment = environment or {}
    packager = packager or {}
    xp_probe = xp_probe or {}
    scan_result = scan_result or {}

    _unify_target(unified, packager, xp_probe, scan_result)
    _unify_crash(unified, packager, xp_probe)
    _unify_environment(unified, source_platform, environment, xp_probe)
    _unify_migration_assessment(unified, source_platform, environment, xp_probe)

    return unified


# ── 目标应用 ──────────────────────────────────────────────

def _unify_target(
    unified: Dict,
    packager: Dict,
    xp_probe: Dict,
    scan: Dict,
):
    """归一化 target_app 块"""
    t = {
        "name": packager.get("app_name") or xp_probe.get("app_name", "unknown"),
        "entry_point": None,
        "classification": "UNKNOWN",
        "action": "FULL_MIGRATE",
    }

    t["entry_point"] = (
        packager.get("executable")
        or packager.get("target_path")
        or xp_probe.get("entry_point")
    )

    cls = scan.get("classification") or packager.get("classification")
    if cls:
        t["classification"] = cls
    elif xp_probe.get("registry_refs"):
        t["classification"] = "LIGHT_DEPEND"
    else:
        t["classification"] = "PURE_GREEN"

    act = scan.get("action")
    if act:
        t["action"] = act
    elif t["classification"] == "HEAVY_DEPEND":
        t["action"] = "ABORT"
    elif t["classification"] == "LIGHT_DEPEND":
        t["action"] = "FULL_MIGRATE"
    else:
        t["action"] = "COPY_ONLY"

    unified["target_app"] = t


# ── 崩溃上下文 ────────────────────────────────────────────

def _unify_crash(
    unified: Dict,
    packager: Dict,
    xp_probe: Dict,
):
    """归一化 crash_context"""
    c = {"crashed": False, "exit_code": None, "category": None, "runtime_ms": None, "stdout_tail": [], "stderr_tail": []}

    error = packager.get("error_info") or xp_probe.get("error_info")

    if error:
        c["crashed"] = error.get("crashed", False)
        ec = error.get("exit_code")
        ec_hex = error.get("exit_code_hex")
        if ec is not None:
            c["exit_code"] = {"decimal": ec, "hex": ec_hex or hex(ec & 0xFFFFFFFF)}
        c["runtime_ms"] = error.get("runtime_ms")
        c["stdout_tail"] = error.get("stdout_tail", [])
        c["stderr_tail"] = error.get("stderr_tail", [])
        c["category"] = error.get("signature") or "UNKNOWN"

    # 分类标准化
    cat = c["category"]
    if not cat and c["crashed"]:
        hex_code = (c.get("exit_code") or {}).get("hex", "")
        if hex_code:
            cat = _classify_exit_code(hex_code)
    c["category"] = cat

    unified["crash_context"] = c


def _classify_exit_code(hex_code: str) -> str:
    """Windows 和 Linux 通用退出码分类"""
    code = hex_code.upper()
    if code in ("0XC0000135", "0XC000007B", "0XC0000142"):
        return "DLL_NOT_FOUND"
    if "SEGFAULT" in code or code in ("0XC0000005", "0XB"):
        return "SEGFAULT"
    if "WINE" in code:
        return "WINE_ERROR"
    return "UNKNOWN"


# ── 宿主环境 ──────────────────────────────────────────────

def _unify_environment(
    unified: Dict,
    source_platform: str,
    env: Dict,
    xp_probe: Dict,
):
    """归一化 environment 块"""
    e: Dict[str, Any] = {
        "host": {},
        "compatibility_layer": {"type": "native", "installed": True, "version": None, "wow64": None},
        "dependencies": {"total_checked": 0, "present": 0, "missing": []},
        "frameworks": {"vcpp": [], "dotnet": {"installed": False, "max_version": None}, "directx": {"installed": False}},
        "graphics": {"display_server": "none", "opengl": {"available": False, "version": None, "vendor": None, "renderer": None}, "vulkan": {"available": False, "version": None}},
        "containers": {"docker_available": False, "podman_available": False},
    }

    # ── host ──
    os_info = env.get("os", {})
    if os_info:
        is_linux = source_platform == "linux"
        if is_linux:
            e["host"] = {
                "os_type": "linux",
                "os_name": os_info.get("distro") or f"{os_info.get('distro_id', '')} {os_info.get('distro_version', '')}".strip(),
                "os_version": os_info.get("distro_version") or os_info.get("kernel"),
                "kernel": os_info.get("kernel"),
                "arch": os_info.get("arch"),
                "byte_order": os_info.get("byte_order"),
            }
        else:
            e["host"] = {
                "os_type": "windows",
                "os_name": os_info.get("os_name") or os_info.get("distro", "Windows"),
                "os_version": os_info.get("os_release") or os_info.get("os_version", ""),
                "kernel": os_info.get("build_number") or os_info.get("kernel"),
                "arch": os_info.get("machine") or os_info.get("arch") or os_info.get("architecture"),
                "byte_order": os_info.get("byte_order", "little"),
            }

    # ── compatibility_layer ──
    if source_platform == "linux":
        wine = env.get("wine", {})
        e["compatibility_layer"] = {
            "type": "wine" if wine.get("installed") else "wine_missing",
            "installed": wine.get("installed", False),
            "version": wine.get("version"),
            "wow64": wine.get("wow64"),
        }
    else:
        e["compatibility_layer"]["version"] = "native"

    # ── dependencies ──
    if source_platform == "linux":
        libs = env.get("system_libraries", {})
        e["dependencies"] = {
            "total_checked": libs.get("total_checked", 0),
            "present": libs.get("present", 0),
            "missing": [
                {"kind": "so", "name": m["name"], "package_hint": m.get("package_hint", "unknown"), "bits": None}
                for m in libs.get("missing", [])
            ],
        }
    elif source_platform == "windows":
        winlibs = env.get("critical_dlls", env.get("system_libraries", {}))
        # Windows: critical_dlls is {dll_name: {System32:..., SysWOW64:..., any_found: bool}}
        if isinstance(winlibs, dict):
            missing = []
            present_count = 0
            for dll_name, info in winlibs.items():
                if isinstance(info, dict) and info.get("any_found"):
                    present_count += 1
                else:
                    missing.append({"kind": "dll", "name": dll_name, "package_hint": _hint_dll(dll_name), "bits": None})
            e["dependencies"] = {
                "total_checked": len(winlibs),
                "present": present_count,
                "missing": missing,
            }

    # ── frameworks (Windows only) ──
    if source_platform == "windows":
        vcpp_raw = env.get("vc_runtimes", env.get("vcpp_runtimes", []))
        if isinstance(vcpp_raw, list):
            vcpp_clean = []
            for v in vcpp_raw:
                if isinstance(v, dict):
                    vcpp_clean.append({"version": v.get("version", ""), "arch": v.get("arch", "unknown"), "installed": True})
            e["frameworks"]["vcpp"] = vcpp_clean

        dotnet = env.get("dotnet", {})
        if isinstance(dotnet, dict):
            e["frameworks"]["dotnet"] = {"installed": bool(dotnet.get("net_framework_detected")), "max_version": dotnet.get("net_framework_detected")}

        dx = env.get("directx", {})
        e["frameworks"]["directx"]["installed"] = bool(dx.get("installed") if isinstance(dx, dict) else dx)

    if source_platform == "linux":
        e["frameworks"] = {"vcpp": None, "dotnet": None, "directx": None}

    # ── graphics ──
    gfx = env.get("graphics", {})
    if gfx:
        e["graphics"] = {
            "display_server": gfx.get("display_server", "none") or "none",
            "opengl": gfx.get("opengl", {}),
            "vulkan": gfx.get("vulkan", {}),
        }
    elif source_platform == "windows":
        e["graphics"]["display_server"] = "win32"

    # ── containers ──
    ctr = env.get("containers", {})
    if ctr:
        e["containers"] = ctr

    unified["environment"] = e


def _hint_dll(name: str) -> str:
    """根据 DLL 名推断来源"""
    n = name.lower()
    if n.startswith("msvcp") or n.startswith("msvcr") or n.startswith("vcruntime"):
        return "vc_runtime"
    if n.startswith("mfc") or n.startswith("atl"):
        return "mfc_atl"
    if "legacy" in n or "custom" in n:
        return "custom"
    return "unknown"


# ── 迁移评估 ──────────────────────────────────────────────

def _unify_migration_assessment(
    unified: Dict,
    source_platform: str,
    env: Dict,
    xp_probe: Dict,
):
    """归一化 migration_assessment。Linux 端已有，直接搬运；Windows/XP 由 crash + env 推断生成。"""
    if source_platform == "linux" and env.get("migration_assessment"):
        unified["migration_assessment"] = env["migration_assessment"]
        return

    # 推断
    env_block = unified["environment"]
    crash = unified["crash_context"]
    target = unified["target_app"]

    blockers: List[str] = []
    level = "LEVEL_1"

    # 检查缺少的依赖
    missing = env_block["dependencies"]["missing"]
    if missing:
        names = [m["name"] for m in missing[:10]]
        blockers.extend(names)

    # 崩溃签名
    if crash.get("crashed") and crash.get("category"):
        blockers.insert(0, crash["category"])

    # 分类判定
    if target["classification"] == "HEAVY_DEPEND":
        level = "LEVEL_3"
    elif missing and len(missing) > 5:
        level = "LEVEL_2"
    elif crash.get("crashed") and not missing:
        level = "LEVEL_2"  # 不是缺库但崩溃，需要深入
    else:
        level = "LEVEL_1"

    labels = {
        "LEVEL_1": "直接兼容",
        "LEVEL_2": "需要转译层或依赖修复",
        "LEVEL_3": "架构不兼容或重度依赖",
    }

    unified["migration_assessment"] = {
        "level": level,
        "level_label": labels.get(level, "未知"),
        "blockers": blockers[:10],
        "recommended_action": None,
    }


# ── CLI ───────────────────────────────────────────────────

def main():
    import argparse
    p = argparse.ArgumentParser(description="三端探针统一桥接")
    p.add_argument("--platform", required=True, choices=["windows", "linux", "xp"])
    p.add_argument("--packager", help="packager_report.json (windows)")
    p.add_argument("--environment", required=True, help="环境指纹 JSON")
    p.add_argument("--scan", help="scan_result.json (windows)")
    p.add_argument("--probe", help="XP probe result JSON (xp)")
    p.add_argument("--out", default="unified_context.json", help="输出路径")

    args = p.parse_args()

    packager = _load(args.packager) if args.packager else None
    environment = _load(args.environment) if args.environment else {}
    xp_probe = _load(args.probe) if args.probe else None
    scan = _load(args.scan) if args.scan else None

    unified = unify(
        source_platform=args.platform,
        packager=packager,
        environment=environment,
        xp_probe=xp_probe,
        scan_result=scan,
    )

    out_path = Path(args.out)
    json_str = json.dumps(unified, ensure_ascii=False, indent=2)
    out_path.write_text(json_str, encoding="utf-8")
    print(f"[bridge] 统一上下文已保存: {out_path}")
    print(f"[bridge] 文件大小: {len(json_str):,} bytes")
    print(f"[bridge] 平台: {args.platform} | 评估: {unified['migration_assessment']['level']}")
    return 0


def _load(path: str) -> Optional[Dict[str, Any]]:
    if not path or not Path(path).exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    sys.exit(main())

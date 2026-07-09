#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XinResurrect — Linux 国产系统环境探针 v0.1.0
==============================================
纯 Python stdlib，零依赖。在统信 UOS / 银河麒麟 / Ubuntu / Debian 上直接运行。

用途：为信创 Windows 老软件迁移提供完整的环境指纹，输出结构化 JSON，
     供 AI 修复引擎 (ai_fixer.py) 消费。

输出文件：xin_env_fingerprint.json

设计原则：
  - 只读，不修改系统任何配置
  - 所有命令超时 5s，不挂死
  - 缺失字段用 null，不报错中断
"""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── 工具函数 ──────────────────────────────────────────────

def run(cmd: List[str], timeout: int = 5) -> Optional[str]:
    """安全执行命令，失败返回 None"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() or None
    except Exception:
        return None

def which(name: str) -> Optional[str]:
    """查找命令完整路径，找不到返回 None"""
    return shutil.which(name)

def read_file(path: str) -> Optional[str]:
    """安全读取文件"""
    try:
        return Path(path).read_text().strip() or None
    except Exception:
        return None

def parse_version(raw: Optional[str], pattern: str = r'(\d+[\d.]*)') -> Optional[str]:
    """从字符串中提取版本号"""
    if not raw:
        return None
    m = re.search(pattern, raw)
    return m.group(1) if m else None


# ── 1. OS 身份证 ──────────────────────────────────────────

def collect_os() -> Dict[str, Any]:
    """发行版、内核、架构"""
    info = {
        "hostname": platform.node() or None,
        "distro": None,
        "distro_id": None,
        "distro_version": None,
        "kernel": platform.release() or None,
        "arch": platform.machine() or None,
        "byte_order": sys.byteorder
    }

    # 尝试从 /etc/os-release 提取发行版
    os_release = read_file("/etc/os-release")
    if os_release:
        for line in os_release.split("\n"):
            if line.startswith("PRETTY_NAME="):
                info["distro"] = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("NAME="):
                info["distro_name"] = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("VERSION="):
                info["distro_version_name"] = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("ID="):
                info["distro_id"] = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("VERSION_ID="):
                info["distro_version"] = line.split("=", 1)[1].strip().strip('"')

    # 若无 PRETTY_NAME，用 NAME+VERSION 拼接
    if not info["distro"] and info.get("distro_name"):
        ver = info.get("distro_version_name", "")
        info["distro"] = f"{info['distro_name']} {ver}".strip() if ver else info["distro_name"]

    # 兜底：lsb_release
    if not info["distro"]:
        info["distro"] = run(["lsb_release", "-d"]) or run(["cat", "/etc/issue"])
        if info["distro"]:
            info["distro"] = info["distro"].split("\n")[0].strip()

    return info


# ── 2. CPU 能力 ──────────────────────────────────────────

def collect_cpu() -> Dict[str, Any]:
    """CPU 架构、指令集、模拟器"""
    cpuinfo = read_file("/proc/cpuinfo")
    info: Dict[str, Any] = {
        "arch": platform.machine(),
        "vendor": None,
        "model": None,
        "cores": os.cpu_count(),
        "bogo_mips": None,
        "features": [],
        "x86_emulation_available": False,
        "notes": []
    }

    if cpuinfo:
        for line in cpuinfo.split("\n"):
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key, val = key.strip(), val.strip()
            if not info["vendor"] and key == "vendor_id":
                info["vendor"] = val
            if not info["model"] and key == "model name":
                info["model"] = val
            if not info["bogo_mips"] and key == "BogoMIPS":
                try:
                    info["bogo_mips"] = float(val.split()[0]) if val else None
                except ValueError:
                    pass
            if key == "Features" or key == "flags":
                info["features"] = val.split()

    # ARM64 上的 x86 模拟器检测
    if info["arch"] in ("aarch64", "arm64"):
        info["x86_emulation_available"] = bool(
            which("qemu-i386") or which("qemu-x86_64") or
            which("box86") or which("box64")
        )
        if not info["x86_emulation_available"]:
            info["notes"].append("ARM64 架构，未检测到 x86 模拟器 (qemu/box86/box64)")
    else:
        info["x86_emulation_available"] = None  # x86 本机不需要

    return info


# ── 3. Wine 状态 ──────────────────────────────────────────

def collect_wine() -> Dict[str, Any]:
    """Wine 版本、架构、Prefix、winetricks 组件"""
    info: Dict[str, Any] = {
        "installed": False,
        "version": None,
        "version_raw": None,
        "arch": None,
        "wow64": False,
        "prefixes": [],
        "winetricks_verbs": [],
        "error": None
    }

    wine_bin = which("wine") or which("wine64") or which("wine32")
    if not wine_bin:
        info["error"] = "wine 未安装"
        return info

    info["path"] = wine_bin

    # 版本
    ver_raw = run(["wine", "--version"]) or run(["wine64", "--version"])
    info["version_raw"] = ver_raw
    info["version"] = parse_version(ver_raw, r'wine-(\S+)') or parse_version(ver_raw)
    info["installed"] = True

    # 架构 — 从 wine 可执行文件本身判断
    wine_path = shutil.which("wine") or shutil.which("wine64") or shutil.which("wine32")
    if wine_path:
        file_out = run(["file", wine_path])
        if file_out:
            if "64-bit" in file_out:
                info["arch"] = "x86_64"
            elif "32-bit" in file_out:
                info["arch"] = "x86"
        # WOW64 检测
        if which("wine64") and which("wine32"):
            info["wow64"] = True

    # 默认 Prefix
    wine_prefix = os.environ.get("WINEPREFIX", os.path.expanduser("~/.wine"))
    if os.path.isdir(wine_prefix):
        prefix_info = {"path": wine_prefix, "arch": None, "windows_version": None}

        # 检测 Prefix 架构
        for subdir in ["drive_c/windows/syswow64", "drive_c/windows/system32"]:
            if os.path.isdir(os.path.join(wine_prefix, subdir)):
                if subdir == "drive_c/windows/syswow64":
                    prefix_info["arch"] = "win64"
                elif prefix_info["arch"] is None:
                    prefix_info["arch"] = "win32"
                break

        # Windows 版本
        reg_files = [
            os.path.join(wine_prefix, "system.reg"),
            os.path.join(wine_prefix, "user.reg")
        ]
        for rf in reg_files:
            content = read_file(rf)
            if content:
                m = re.search(r'Version.*?=.*?win(10|7|8|8\.1|xp|vista)', content, re.IGNORECASE)
                if m:
                    prefix_info["windows_version"] = f"win{m.group(1)}"
                    break

        info["prefixes"].append(prefix_info)

    # Winetricks Verbs
    wt = run(["winetricks", "list-installed"])
    if wt:
        info["winetricks_verbs"] = [v.strip() for v in wt.split("\n") if v.strip()]

    return info


# ── 4. 图形栈 ────────────────────────────────────────────

def collect_graphics() -> Dict[str, Any]:
    """显示服务器、OpenGL、Vulkan"""
    info: Dict[str, Any] = {
        "display_server": None,
        "opengl": {"available": False, "version": None, "vendor": None, "renderer": None},
        "vulkan": {"available": False, "version": None},
        "wayland": {"available": False},
        "x11": {"available": False}
    }

    # 显示服务器
    if os.environ.get("WAYLAND_DISPLAY"):
        info["display_server"] = "Wayland"
        info["wayland"]["available"] = True
    elif os.environ.get("DISPLAY"):
        info["display_server"] = "X11"
        info["x11"]["available"] = True
    elif which("Xorg") or which("X"):
        info["display_server"] = "X11"
        info["x11"]["available"] = True

    # OpenGL (glxinfo)
    glx = run(["glxinfo", "-B"])
    if glx:
        info["opengl"]["available"] = True
        for line in glx.split("\n"):
            if line.startswith("OpenGL version string:"):
                info["opengl"]["version"] = line.split(":", 1)[1].strip()
            elif line.startswith("OpenGL vendor string:"):
                info["opengl"]["vendor"] = line.split(":", 1)[1].strip()
            elif line.startswith("OpenGL renderer string:"):
                info["opengl"]["renderer"] = line.split(":", 1)[1].strip()
    else:
        # 兜底：检查 libGL
        gl_lib = (
            read_file("/etc/ld.so.conf.d/*.conf") or ""
        )
        if which("ldconfig"):
            ld = run(["ldconfig", "-p"])
            if ld and "libGL.so" in ld:
                info["opengl"]["available"] = True
                info["opengl"]["version"] = "detected_but_no_glxinfo"

    # Vulkan (vulkaninfo)
    vk = run(["vulkaninfo", "--summary"])
    if vk:
        info["vulkan"]["available"] = True
        m = re.search(r'apiVersion.*?(\d+\.\d+)', vk)
        if m:
            info["vulkan"]["version"] = m.group(1)

    return info


# ── 5. 关键系统 .so 库 ───────────────────────────────────

CRITICAL_SO_LIBS = [
    # C 运行库
    ("libc.so.6", "libc6"),
    ("libm.so.6", "libc6"),
    ("libpthread.so.0", "libc6"),
    ("libdl.so.2", "libc6"),
    ("librt.so.1", "libc6"),
    # C++ 运行库
    ("libstdc++.so.6", "libstdc++6"),
    ("libgcc_s.so.1", "libgcc-s1"),
    # 图形 (对标 Windows GDI/GDI+)
    ("libX11.so.6", "libx11-6"),
    ("libXext.so.6", "libxext6"),
    ("libXrender.so.1", "libxrender1"),
    ("libXrandr.so.2", "libxrandr2"),
    ("libXcursor.so.1", "libxcursor1"),
    ("libXinerama.so.1", "libxinerama1"),
    ("libXcomposite.so.1", "libxcomposite1"),
    ("libXi.so.6", "libxi6"),
    ("libXfixes.so.3", "libxfixes3"),
    ("libXdamage.so.1", "libxdamage1"),
    # Font
    ("libfontconfig.so.1", "libfontconfig1"),
    ("libfreetype.so.6", "libfreetype6"),
    # Image
    ("libpng16.so.16", "libpng16-16"),
    ("libjpeg.so.8", "libjpeg8"),
    ("libtiff.so.5", "libtiff5"),
    # Network (对标 Windows WinSock/WinHTTP)
    ("libssl.so.1.1", "libssl1.1"),
    ("libssl.so.3", "libssl3"),
    ("libcrypto.so.1.1", "libcrypto1.1"),
    ("libcrypto.so.3", "libcrypto3"),
    ("libcurl.so.4", "libcurl4"),
    # DB / Audio
    ("libsqlite3.so.0", "libsqlite3-0"),
    ("libpulse.so.0", "libpulse0"),
    ("libasound.so.2", "libasound2"),
    # 老版兼容库 (对旧软件至关重要)
    ("libpng12.so.0", "libpng12-0"),
    ("libssl.so.1.0.0", "libssl1.0.0 (若可用)"),
    ("libgconf-2.so.4", "libgconf-2-4"),
    ("libgtk-3.so.0", "libgtk-3-0"),
    ("libgdk_pixbuf-2.0.so.0", "libgdk-pixbuf2.0-0"),
]


def collect_system_libs() -> Dict[str, Any]:
    """检查关键 .so 库是否存在"""
    info: Dict[str, Any] = {
        "total_checked": len(CRITICAL_SO_LIBS),
        "present": 0,
        "missing": [],
        "method": "ldconfig"
    }

    # 优先用 ldconfig -p（最准确）
    ld_output = run(["ldconfig", "-p"])
    ld_cache: Dict[str, bool] = {}
    if ld_output:
        for line in ld_output.split("\n"):
            if "=>" in line:
                # 格式: libname.so (libc6,x86-64) => /lib/x86_64-linux-gnu/libname.so
                lib_name = line.split("(")[0].strip()
                ld_cache[lib_name] = True

    for soname, pkg_hint in CRITICAL_SO_LIBS:
        found = False

        # 方式 1: ldconfig
        if ld_cache:
            # 模糊匹配 (libssl.so.1.1 可能匹配 libssl3 等)
            for cached in ld_cache:
                if cached.startswith(soname.rsplit(".", 1)[0]):
                    found = True
                    break

        # 方式 2: 文件系统查找
        if not found:
            lib_dirs = [
                "/lib", "/lib64",
                "/usr/lib", "/usr/lib64",
                "/usr/lib/x86_64-linux-gnu",
                "/usr/lib/aarch64-linux-gnu",
                "/usr/local/lib"
            ]
            for d in lib_dirs:
                base = soname.rsplit(".", 1)[0] if "." in soname else soname
                matches = list(Path(d).glob(f"{base}*")) if os.path.isdir(d) else []
                if matches:
                    found = True
                    break

        if found:
            info["present"] += 1
        else:
            info["missing"].append({"name": soname, "package_hint": pkg_hint})

    if not ld_output and not ld_cache:
        info["method"] = "filesystem_scan"

    return info


# ── 6. 容器运行时 ─────────────────────────────────────────

def collect_containers() -> Dict[str, Any]:
    """Docker / Podman 是否可用"""
    return {
        "docker_available": which("docker"),
        "podman_available": which("podman"),
    }


# ── 7. 迁移评估 (AI 的前置速判断) ─────────────────────────

def assess_migration(os_info: Dict, wine: Dict, cpu: Dict, libs: Dict) -> Dict[str, Any]:
    """
    基于采集数据给出快速预判。
    注意：这只是初步评估，最终决策由 ai_fixer 的 LLM 做出。
    """

    blockers = []
    level = "LEVEL_1"
    label = "直接兼容"

    # CPU 架构判断
    if cpu["arch"] in ("aarch64", "arm64"):
        if not cpu["x86_emulation_available"]:
            level = "LEVEL_3"
            label = "架构不兼容"
            blockers.append(f"ARM64 架构且无 x86 模拟器")
        else:
            level = "LEVEL_2"
            label = "需要指令转译层"
            blockers.append("需要 Box86/Box64 或 QEMU 用户态模拟")

    # Wine
    if not wine["installed"]:
        if level == "LEVEL_1":
            level = "LEVEL_2"
            label = "需要 Wine 运行层"
        blockers.append("Wine 未安装")

    # .so 缺失
    if libs["missing"]:
        if len(libs["missing"]) > 5:
            if level == "LEVEL_1":
                level = "LEVEL_2"
            label = "需要 API 转译层（多库缺失）"
        blockers.extend([m["name"] for m in libs["missing"][:5]])

    return {
        "level": level,
        "level_label": label,
        "blockers": blockers[:10],
        "recommended_action": _build_action(level, wine, libs)
    }


def _build_action(level: str, wine: Dict, libs: Dict) -> str:
    """生成可执行的操作建议"""
    if level == "LEVEL_1":
        if wine["installed"]:
            return "wine <target.exe>"
        else:
            return "apt install wine && wine <target.exe>"

    if level == "LEVEL_2":
        parts = []
        if not wine["installed"]:
            parts.append("apt install wine")
        if libs["missing"]:
            pkgs = [m["package_hint"] for m in libs["missing"][:5]]
            parts.append(f"apt install {' '.join(pkgs)}")
        parts.append("wine <target.exe>")
        return " && ".join(parts)

    # LEVEL_3: 架构不兼容
    parts = ["# 当前 CPU 架构不支持直接运行 x86 Windows 程序"]
    parts.append("# 建议 1: 在有 x86_64 硬件的主机上运行")
    parts.append("# 建议 2: 安装 Box86/Box64 转译层后重试")
    return "\n".join(parts)


# ── MAIN ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="XinResurrect 信创环境探针")
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="输出 JSON 文件路径 (默认: xin_env_fingerprint.json)")
    args = parser.parse_args()

    script_dir = Path(__file__).parent.resolve()
    output_path = args.output or (script_dir / "xin_env_fingerprint.json")

    print("[XinResurrect] 信创环境探针 v0.1.0")
    print(f"[XinResurrect] 输出: {output_path}")
    print()

    # 收集
    sections = [
        ("OS 身份证", collect_os),
        ("CPU 能力", collect_cpu),
        ("Wine 状态", collect_wine),
        ("图形栈", collect_graphics),
        ("关键系统 .so 库", collect_system_libs),
        ("容器运行时", collect_containers),
    ]

    result: Dict[str, Any] = {
        "schema_version": "1.0",
        "probe_type": "xinresurrect_linux",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "host": {},  # nested: os, cpu, graphics, containers
        "wine": {},
        "system_libraries": {},
        "migration_assessment": {},
    }

    for i, (name, func) in enumerate(sections):
        print(f"  [{i + 1}/{len(sections)}] 收集 {name}...")
        key_map = {
            "OS 身份证": "os", "CPU 能力": "cpu", "Wine 状态": "wine",
            "图形栈": "graphics", "关键系统 .so 库": "system_libraries", "容器运行时": "containers",
        }
        mapped = key_map[name]
        data = func()

        if mapped in ("os", "cpu", "graphics", "containers"):
            result["host"][mapped] = data
        elif mapped == "wine":
            result["wine"] = data
        elif mapped == "system_libraries":
            result["system_libraries"] = data

    # 迁移评估
    print(f"  [7/7] 生成迁移评估...")
    result["migration_assessment"] = assess_migration(
        result["host"]["os"], result["wine"], result["host"]["cpu"], result["system_libraries"]
    )

    # 写入
    json_str = json.dumps(result, ensure_ascii=False, indent=2)
    output_path.write_text(json_str, encoding="utf-8")
    print(f"\n[OK] 环境指纹已保存: {output_path}")
    print(f"[OK] 文件大小: {len(json_str):,} bytes")
    print(f"[OK] 迁移评估: {result['migration_assessment']['level']} — {result['migration_assessment']['level_label']}")

    # 打印关键摘要
    print("\n" + "=" * 50)
    print("  摘要")
    print("=" * 50)
    print(f"  系统:  {result['host']['os']['distro'] or '未知'}")
    print(f"  内核:  {result['host']['os']['kernel']}")
    print(f"  架构:  {result['host']['os']['arch']}")
    print(f"  Wine:  {result['wine']['version'] or '未安装'}")
    print(f"  图形:  {result['host']['graphics']['display_server'] or '无显示服务'}")
    print(f"  .so库: {result['system_libraries']['present']}/{result['system_libraries']['total_checked']} 存在")
    print(f"  评估:  {result['migration_assessment']['level']} — {result['migration_assessment']['level_label']}")
    if result['migration_assessment']['blockers']:
        print(f"  阻塞项: {', '.join(result['migration_assessment']['blockers'][:5])}")
    print("=" * 50)


if __name__ == "__main__":
    main()

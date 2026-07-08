#!/usr/bin/env python3
"""
Legacy App Migration MVP - Environment Collector
Generates a "System Fingerprint" JSON for AI-powered diagnosis.

Design principle: Collect EVERYTHING the AI might need to reason about
why a legacy app fails. No internet dependency. Pure local snapshot.
"""

import os
import sys
import json
import struct
import subprocess
import platform
import ctypes
from pathlib import Path
from datetime import datetime
from collections import OrderedDict

# ============================================================
# Utility: registry reading (no external deps, pure winreg)
# ============================================================
import winreg


def reg_read_key(root, subkey, bits=64):
    """Read all values and subkeys from a registry key."""
    access = winreg.KEY_READ
    if bits == 32:
        access |= winreg.KEY_WOW64_32KEY
    elif bits == 64:
        access |= winreg.KEY_WOW64_64KEY

    hkey_map = {
        "HKLM": winreg.HKEY_LOCAL_MACHINE,
        "HKCU": winreg.HKEY_CURRENT_USER,
        "HKCR": winreg.HKEY_CLASSES_ROOT,
    }

    try:
        actual_root = hkey_map.get(root, winreg.HKEY_LOCAL_MACHINE)
        with winreg.OpenKey(actual_root, subkey, 0, access) as key:
            result = {"values": OrderedDict(), "subkeys": []}
            # Read values
            idx = 0
            while True:
                try:
                    name, data, dtype = winreg.EnumValue(key, idx)
                    result["values"][name] = {"data": str(data), "type": dtype}
                    idx += 1
                except OSError:
                    break
            # Read subkeys
            idx = 0
            while True:
                try:
                    result["subkeys"].append(winreg.EnumKey(key, idx))
                    idx += 1
                except OSError:
                    break
            return result
    except OSError:
        return None


def reg_get_value(root, subkey, value_name):
    """Get a single registry value."""
    data = reg_read_key(root, subkey)
    if data and value_name in data["values"]:
        return data["values"][value_name]["data"]
    return None


# ============================================================
# Section 1: OS Base Info
# ============================================================

def collect_os_info():
    """Collect OS version, architecture, language, permissions."""
    info = OrderedDict()

    # Basic platform
    info["os_name"] = platform.system()
    info["os_release"] = platform.release()
    info["os_version"] = platform.version()
    info["architecture"] = platform.architecture()[0]
    info["machine"] = platform.machine()

    # Detailed Windows version from registry
    current_version = reg_read_key("HKLM", r"SOFTWARE\Microsoft\Windows NT\CurrentVersion")
    if current_version:
        info["product_name"] = current_version["values"].get("ProductName", {}).get("data", "Unknown")
        info["build_number"] = current_version["values"].get("CurrentBuildNumber", {}).get("data", "Unknown")
        info["display_version"] = current_version["values"].get("DisplayVersion",
                                            current_version["values"].get("ReleaseId", {}).get("data", "Unknown"))
        info["edition"] = current_version["values"].get("EditionID", {}).get("data", "Unknown")

    # UBR (update build revision)
    ubr = reg_get_value("HKLM", r"SOFTWARE\Microsoft\Windows NT\CurrentVersion", "UBR")
    if ubr:
        info["ubr"] = ubr

    # System language/locale
    info["system_language"] = platform.uname() if hasattr(platform.uname(), "language") else "N/A"
    lang_key = reg_read_key("HKLM", r"SYSTEM\CurrentControlSet\Control\Nls\Language")
    if lang_key and "(default)" in lang_key["values"]:
        info["default_language"] = lang_key["values"]["(default)"]["data"]

    # Admin privileges
    try:
        info["is_admin"] = ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        info["is_admin"] = False

    return info


# ============================================================
# Section 2: Installed Runtimes
# ============================================================

def _scan_runtimes_from_uninstall():
    """Enumerate all entries in Uninstall registry and filter for runtimes."""
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    runtimes = []

    for bits, label in [(64, "64-bit"), (32, "32-bit")]:
        entries = reg_read_key("HKLM", base, bits)
        if not entries:
            continue
        for subkey_name in entries["subkeys"]:
            key_data = reg_read_key("HKLM", f"{base}\\{subkey_name}", bits)
            if not key_data:
                continue
            vals = key_data["values"]
            display_name = vals.get("DisplayName", {}).get("data", "")
            display_version = vals.get("DisplayVersion", {}).get("data", "")
            if not display_name:
                continue
            runtimes.append({
                "name": display_name,
                "version": display_version,
                "arch": label,
                "publisher": vals.get("Publisher", {}).get("data", ""),
                "install_date": vals.get("InstallDate", {}).get("data", ""),
                "uninstall_string": vals.get("UninstallString", {}).get("data", ""),
            })
    return runtimes


def collect_vc_runtimes(all_uninstall):
    """Filter installed Visual C++ Redistributables."""
    vc_entries = []
    keywords = [
        "Visual C++", "Visual Studio",
        "Microsoft Visual C", "MSVC",
        "vcredist",
    ]
    for entry in all_uninstall:
        name_lower = entry["name"].lower()
        if any(kw.lower() in name_lower for kw in keywords):
            vc_entries.append(entry)

    # Sort by version
    vc_entries.sort(key=lambda x: x.get("version", ""))
    return vc_entries


def collect_dotnet_versions():
    """Collect installed .NET Framework versions via registry."""
    versions = OrderedDict()

    # .NET Framework 4.5+ via Release DWORD
    ndp_path = r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full"
    release = reg_get_value("HKLM", ndp_path, "Release")
    if release:
        release_int = int(release)
        versions["net4x_release_dword"] = release_int
        # Map release DWORD to friendly version
        release_map = {
            528040: "4.8",
            461808: "4.7.2",
            461308: "4.7.1",
            460798: "4.7",
            394802: "4.6.2",
            394254: "4.6.1",
            393295: "4.6",
            379893: "4.5.2",
            378675: "4.5.1",
            378389: "4.5",
        }
        for rel, ver in sorted(release_map.items()):
            if release_int >= rel:
                versions["net_framework_detected"] = ver
        if "net_framework_detected" not in versions:
            # Above 4.8
            if release_int > 528040:
                versions["net_framework_detected"] = "4.8+"

    # .NET Framework 1.0-4.0
    for ver in ["v4.0", "v3.5", "v3.0", "v2.0.50727", "v1.1.4322"]:
        ndp_ver = reg_read_key("HKLM", rf"SOFTWARE\Microsoft\NET Framework Setup\NDP\{ver}")
        if ndp_ver and "Install" in ndp_ver["values"]:
            install_val = ndp_ver["values"]["Install"]["data"]
            versions[ver] = {
                "installed": install_val == "1",
                "version": ndp_ver["values"].get("Version", {}).get("data", ""),
                "sp": ndp_ver["values"].get("SP", {}).get("data", ""),
            }

    # .NET Core / .NET 5+
    core_path = r"SOFTWARE\dotnet\Setup\InstalledVersions"
    for bits in [64, 32]:
        core_data = reg_read_key("HKLM", core_path, bits)
        if core_data:
            shared_host = core_data["values"].get("sharedHost", {}).get("data", "")
            if shared_host:
                versions["dotnet_core_shared_host"] = shared_host
            break

    return versions


def collect_directx():
    """Check DirectX installation status."""
    dx = OrderedDict()

    # DirectX registry
    dx_key = reg_read_key("HKLM", r"SOFTWARE\Microsoft\DirectX")
    if dx_key:
        dx["version"] = dx_key["values"].get("Version", {}).get("data", "")
        dx["installed"] = True
    else:
        dx["installed"] = False

    # Check d3dx9 DLLs as proxy for DirectX 9 legacy support
    system32 = os.environ.get("SystemRoot", r"C:\Windows") + r"\System32"
    d3d_files = []
    for f in ["d3dx9_43.dll", "d3dx9_42.dll", "d3dx9_41.dll", "d3dx9_39.dll",
              "d3dx9_36.dll", "d3dx9_33.dll", "d3dx9_30.dll"]:
        full = os.path.join(system32, f)
        if os.path.exists(full):
            d3d_files.append(f)
    dx["d3dx9_dlls_found"] = d3d_files

    return dx


# ============================================================
# Section 3: Critical System DLL Check
# ============================================================

CRITICAL_DLLS = [
    # VC++ runtimes
    "msvcp60.dll", "msvcp70.dll", "msvcp71.dll", "msvcp80.dll",
    "msvcp90.dll", "msvcp100.dll", "msvcp110.dll", "msvcp120.dll",
    "msvcp140.dll", "msvcr70.dll", "msvcr71.dll", "msvcr80.dll",
    "msvcr90.dll", "msvcr100.dll", "msvcr110.dll", "msvcr120.dll",
    "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll",
    "msvcrt.dll", "msvcrt20.dll", "msvcrt40.dll",
    # MFC/ATL
    "mfc40.dll", "mfc42.dll", "mfc70.dll", "mfc71.dll", "mfc80.dll",
    "mfc90.dll", "mfc100.dll", "mfc110.dll", "mfc120.dll", "mfc140.dll",
    "atl70.dll", "atl71.dll", "atl80.dll", "atl90.dll", "atl100.dll",
    # VB runtimes
    "msvbvm50.dll", "msvbvm60.dll",
    # System core
    "oleaut32.dll", "comctl32.dll", "riched20.dll", "riched32.dll",
    "msxml3.dll", "msxml4.dll", "msxml6.dll",
    # OpenGL
    "opengl32.dll", "glu32.dll",
]


def _get_file_version(filepath):
    """Extract file version from a DLL/EXE using Windows API."""
    if not os.path.exists(filepath):
        return None
    try:
        import win32api
        info = win32api.GetFileVersionInfo(filepath, "\\")
        ms = info['FileVersionMS']
        ls = info['FileVersionLS']
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        # Fallback: try with ctypes
        try:
            size = ctypes.windll.version.GetFileVersionInfoSizeW(filepath, None)
            if size:
                buf = ctypes.create_string_buffer(size)
                ctypes.windll.version.GetFileVersionInfoW(filepath, 0, size, buf)
                p = ctypes.c_void_p()
                l = ctypes.c_uint()
                ctypes.windll.version.VerQueryValueW(buf, r"\\", ctypes.byref(p), ctypes.byref(l))
                if p.value:
                    info = ctypes.cast(p, ctypes.POINTER(ctypes.c_ubyte * l.value))
                    # Fixed file info starts at the struct
                    from ctypes import wintypes
                    class VS_FIXEDFILEINFO(ctypes.Structure):
                        _fields_ = [
                            ("dwSignature", ctypes.c_uint32),
                            ("dwStrucVersion", ctypes.c_uint32),
                            ("dwFileVersionMS", ctypes.c_uint32),
                            ("dwFileVersionLS", ctypes.c_uint32),
                            ("dwProductVersionMS", ctypes.c_uint32),
                            ("dwProductVersionLS", ctypes.c_uint32),
                        ]
                    ffi = VS_FIXEDFILEINFO.from_buffer_copy(bytes(info.contents))
                    ms = ffi.dwFileVersionMS
                    ls = ffi.dwFileVersionLS
                    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
        except Exception:
            pass

    # Last resort: check file size + modification time as fingerprint
    try:
        stat = os.stat(filepath)
        return f"unknown_size:{stat.st_size}_mtime:{int(stat.st_mtime)}"
    except Exception:
        return None


def collect_critical_dlls():
    """Check existence and version of critical system DLLs."""
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    search_paths = {
        "System32": os.path.join(windir, "System32"),
        "SysWOW64": os.path.join(windir, "SysWOW64"),
    }

    dll_report = OrderedDict()
    for dll_name in CRITICAL_DLLS:
        entry = OrderedDict()
        found = False
        for label, search_dir in search_paths.items():
            full_path = os.path.join(search_dir, dll_name)
            if os.path.exists(full_path):
                ver = _get_file_version(full_path)
                entry[label] = {
                    "found": True,
                    "version": ver,
                    "path": full_path,
                }
                found = True
            else:
                entry[label] = {"found": False}
        entry["any_found"] = found
        dll_report[dll_name] = entry

    return dll_report


# ============================================================
# Section 4: Hardware & Permissions
# ============================================================

def collect_hardware_info():
    """Collect basic hardware info relevant to legacy app compatibility."""
    hw = OrderedDict()

    # CPU
    hw["processor_count"] = os.cpu_count()

    # Memory
    try:
        import psutil
        mem = psutil.virtual_memory()
        hw["total_ram_gb"] = round(mem.total / (1024**3), 1)
        hw["available_ram_gb"] = round(mem.available / (1024**3), 1)
    except ImportError:
        hw["total_ram_gb"] = "psutil_not_installed"
        hw["available_ram_gb"] = "psutil_not_installed"

    # GPU / display adapter (legacy apps sometimes hardcode GPU API)
    display_info = reg_read_key("HKLM", r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0000")
    if display_info:
        hw["gpu"] = display_info["values"].get("DriverDesc", {}).get("data", "Unknown")
        hw["gpu_driver_version"] = display_info["values"].get("DriverVersion", {}).get("data", "Unknown")
    else:
        hw["gpu"] = "Unknown"

    # Disk space on system drive
    try:
        import shutil
        usage = shutil.disk_usage(os.environ.get("SystemDrive", "C:\\"))
        hw["system_drive_free_gb"] = round(usage.free / (1024**3), 1)
        hw["system_drive_total_gb"] = round(usage.total / (1024**3), 1)
    except Exception:
        hw["system_drive_free_gb"] = "N/A"

    return hw


def collect_path_info():
    """Collect PATH and relevant environment variables."""
    path_vars = OrderedDict()
    for var in ["PATH", "PATHEXT", "TEMP", "TMP", "SystemRoot", "SystemDrive",
                "ProgramFiles", "ProgramFiles(x86)", "CommonProgramFiles",
                "CommonProgramFiles(x86)", "PROCESSOR_ARCHITECTURE", "COMPUTERNAME"]:
        path_vars[var] = os.environ.get(var, "NOT_SET")
    return path_vars


# ============================================================
# Section 5: Assembly & Output
# ============================================================

def collect_full_fingerprint(target_program_path: str = None):
    """
    Collect complete system environment fingerprint.
    Returns an OrderedDict ready for JSON serialization.
    """
    print("[ENV_COLLECTOR] 开始收集系统环境指纹...")
    start = datetime.now()

    fingerprint = OrderedDict()
    fingerprint["collection_time"] = datetime.now().isoformat()
    fingerprint["collector_version"] = "1.0.0"

    if target_program_path:
        fingerprint["target_program"] = target_program_path

    # Step 1: OS info
    print("  [1/6] 收集 OS 信息...")
    fingerprint["os"] = collect_os_info()

    # Step 2: All uninstall entries (used as base for runtime filtering)
    print("  [2/6] 扫描已安装程序清单...")
    all_uninstall = _scan_runtimes_from_uninstall()
    fingerprint["installed_programs_count"] = len(all_uninstall)

    # Step 3: VC++ runtimes
    print("  [3/6] 识别 VC++ 运行库...")
    fingerprint["vc_runtimes"] = collect_vc_runtimes(all_uninstall)

    # Step 4: .NET Framework
    print("  [4/6] 识别 .NET Framework 版本...")
    fingerprint["dotnet"] = collect_dotnet_versions()

    # Step 5: DirectX
    print("  [5/6] 检查 DirectX...")
    fingerprint["directx"] = collect_directx()

    # Step 6: Critical DLLs
    print("  [6/6] 检查关键系统 DLL...")
    fingerprint["critical_dlls"] = collect_critical_dlls()

    # Bonus: hardware, path
    fingerprint["hardware"] = collect_hardware_info()
    fingerprint["environment_variables"] = collect_path_info()

    elapsed = (datetime.now() - start).total_seconds()
    print(f"[ENV_COLLECTOR] 完成! 耗时 {elapsed:.2f}s")
    print(f"  VC++ 运行库: {len(fingerprint['vc_runtimes'])} 个")
    print(f"  .NET 版本: {json.dumps(fingerprint['dotnet'], indent=2)}")
    print(f"  DirectX: {'已安装' if fingerprint['directx'].get('installed') else '未检测到'}")
    dll_found = sum(1 for d in fingerprint["critical_dlls"].values() if d["any_found"])
    print(f"  关键 DLL: {dll_found}/{len(CRITICAL_DLLS)} 存在")

    return fingerprint


# ============================================================
# Section 6: AI Integration Helper
# ============================================================

def build_ai_context(fingerprint: dict, error_info: dict) -> dict:
    """
    Build the structured context for AI diagnosis.
    This is the input payload sent to the LLM.
    """
    return {
        "system_fingerprint": {
            "os": fingerprint["os"],
            "vc_runtimes": fingerprint["vc_runtimes"],
            "dotnet": fingerprint["dotnet"],
            "directx": fingerprint["directx"],
            "critical_dlls": fingerprint["critical_dlls"],
        },
        "error": error_info,
        "required_output_format": {
            "action": "install_runtime | copy_dll | set_registry | modify_config | manual_required",
            "target": "specific file name, registry key, or runtime installer name",
            "parameters": {},
            "reason": "brief explanation in Chinese",
            "confidence": "high | medium | low",
        },
    }


# ============================================================
# CLI Entry Point
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Legacy App Environment Collector")
    parser.add_argument("--target", "-t", type=str, default=None,
                        help="目标程序路径 (可选,仅用于标记)")
    parser.add_argument("--output", "-o", type=str, default="env_fingerprint.json",
                        help="输出JSON文件路径")
    args = parser.parse_args()

    target = args.target
    if target and target.startswith('"') and target.endswith('"'):
        target = target[1:-1]

    fingerprint = collect_full_fingerprint(target)

    # Save
    out_path = Path(args.output)
    if not out_path.is_absolute():
        out_path = Path(os.path.dirname(os.path.abspath(__file__))) / out_path

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(fingerprint, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n[OK] 环境指纹已保存到: {out_path}")
    print(f" 文件大小: {out_path.stat().st_size:,} bytes")

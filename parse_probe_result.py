#!/usr/bin/env python3
"""
Bridge: Convert probe_xp.bat output (result.txt) to scanner-compatible JSON.
This runs on the ENGINEER'S laptop, NOT on the old machine.

Usage:
    python parse_probe_result.py result.txt          → scan_result.json
    python parse_probe_result.py result.txt --target "D:\OldApp"  → scan_result.json
"""

import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional


def log(msg: str):
    print(f"[PARSE] {msg}")


# ============================================================
# Section Parser
# ============================================================

def parse_sections(raw: str) -> Dict[str, str]:
    """Split result.txt into named [SECTION] blocks."""
    sections = {}
    current_section = "HEADER"
    current_lines = []

    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if current_lines:
                sections[current_section] = "\n".join(current_lines)
            current_section = stripped[1:-1]
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        sections[current_section] = "\n".join(current_lines)

    return sections


# ============================================================
# OS Info
# ============================================================

def parse_os(section: str) -> Dict:
    info = {}
    for line in section.splitlines():
        if "Microsoft Windows" in line:
            info["version_raw"] = line.strip()
            # "Microsoft Windows XP [Version 5.1.2600]" → XP 5.1
            ver_match = re.search(r'Version\s+(\d+\.\d+)', line)
            if ver_match:
                info["version"] = ver_match.group(1)
        if line.startswith("Architecture:"):
            info["arch"] = line.split(":", 1)[1].strip()
        if line.startswith("Admin:"):
            info["admin"] = line.split(":", 1)[1].strip() == "YES"
    return info


# ============================================================
# Registry References → Target Path Matching
# ============================================================

def find_registry_refs_for_target(uninstall_section: str, target_path: str) -> List[Dict]:
    """
    Parse the raw reg query output and find entries referencing target_path.
    reg query output format:
        HKEY_LOCAL_MACHINE\SOFTWARE\...\Uninstall\{GUID}
            DisplayName    REG_SZ    App Name
            InstallLocation    REG_SZ    D:\App
    """
    matches = []
    norm_target = os.path.normpath(target_path).lower()

    current_key = None
    current_values = {}

    for line in uninstall_section.splitlines():
        # Detect new key
        if line.strip().startswith("HKEY_"):
            # Save previous
            if current_key and current_values:
                for name, (vtype, vdata) in current_values.items():
                    data_lower = vdata.lower()
                    if norm_target in data_lower:
                        matches.append({
                            "source": current_key,
                            "key_name": os.path.basename(current_key),
                            "value_name": name,
                            "value_data": vdata,
                            "value_type": vtype,
                        })
            current_key = line.strip()
            current_values = {}
            continue

        # Parse value line: "    DisplayName    REG_SZ    Some Name"
        # Tab or 4+ spaces as separator
        parts = re.split(r'\s{2,}|\t+', line.strip(), maxsplit=2)
        if len(parts) >= 3:
            name = parts[0].strip()
            vtype_str = parts[1].strip()
            data = parts[2].strip() if len(parts) > 2 else ""
            current_values[name] = (vtype_str, data)

    # Don't forget last key
    if current_key and current_values:
        for name, (vtype, vdata) in current_values.items():
            data_lower = vdata.lower()
            if norm_target in data_lower:
                matches.append({
                    "source": current_key,
                    "key_name": os.path.basename(current_key),
                    "value_name": name,
                    "value_data": vdata,
                    "value_type": vtype,
                })

    return matches


# ============================================================
# Heavy Indicators (COM/Services/Drivers)
# ============================================================

def find_heavy_indicators(services_section: str, target_path: str) -> List[Dict]:
    """Parse 'sc query' output for services referencing target."""
    norm_target = os.path.normpath(target_path).lower()
    matching_services = []

    current_svc = None
    current_binary = None

    for line in services_section.splitlines():
        if line.startswith("SERVICE_NAME:"):
            current_svc = line.split(":", 1)[1].strip()
            current_binary = None
        if "BINARY_PATH_NAME" in line or "ImagePath" in line:
            parts = line.split(":", 1)
            if len(parts) > 1:
                current_binary = parts[1].strip()
            if current_svc and current_binary and norm_target in current_binary.lower():
                indicator = "DRIVER" if ".sys" in current_binary.lower() else "SERVICE"
                matching_services.append({
                    "source": f"HKLM\\SYSTEM\\CurrentControlSet\\Services\\{current_svc}",
                    "indicator": indicator,
                    "details": current_binary,
                })

    return matching_services


# ============================================================
# DLL Check
# ============================================================

def parse_dll_check(section: str) -> Dict[str, bool]:
    """Parse FOUND/MISSING DLL entries."""
    dlls = {}
    for line in section.splitlines():
        if line.startswith("FOUND:"):
            dlls[line.split(":", 1)[1].strip()] = True
        elif line.startswith("MISSING:"):
            dlls[line.split(":", 1)[1].strip()] = False
    return dlls


# ============================================================
# Classification (same logic as scanner.py)
# ============================================================

def classify(registry_refs, heavy_indicators):
    if heavy_indicators:
        return "HEAVY_DEPENDENCY", "ABORT"
    elif registry_refs:
        return "LIGHT_DEPENDENCY", "EXTRACT_AND_MIGRATE"
    else:
        return "PURE_GREEN", "COPY_ONLY"


# ============================================================
# Full AppData Trace Detection
# ============================================================

def detect_appdata_traces(target_path: str) -> List[str]:
    """Now running on engineer's laptop, check for traces in %APPDATA%."""
    app_name = os.path.basename(os.path.normpath(target_path))
    app_name_no_ext = os.path.splitext(app_name)[0].lower()
    traces = []
    for env_var in ["APPDATA", "LOCALAPPDATA"]:
        base = os.environ.get(env_var)
        if not base:
            continue
        try:
            for item in os.listdir(base):
                item_lower = item.lower()
                if app_name_no_ext in item_lower or app_name.lower() in item_lower:
                    traces.append(os.path.join(base, item))
        except OSError:
            pass
    return traces


# ============================================================
# Main Parser
# ============================================================

def parse_probe_result(txt_path: str, target_path: Optional[str] = None) -> Dict:
    """
    Parse probe_xp.bat output into scanner-compatible scan_result.json.
    """
    log(f"解析探针结果: {txt_path}")

    with open(txt_path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()

    sections = parse_sections(raw)
    log(f"发现 {len(sections)} 个数据段: {list(sections.keys())}")

    # OS info
    os_info = parse_os(sections.get("OS", ""))

    # If target specified, find reg refs
    target = target_path
    registry_refs = []
    heavy_indicators = []

    if target:
        uninstall_raw = "\n".join([
            sections.get("REG_UNINSTALL_HKLM", ""),
            sections.get("REG_HKCU_SOFTWARE", ""),
            sections.get("REG_APP_PATHS", ""),
        ])
        registry_refs = find_registry_refs_for_target(uninstall_raw, target)
        log(f"注册表引用 {len(registry_refs)} 条")

        services_raw = sections.get("SERVICES", "")
        heavy_indicators = find_heavy_indicators(services_raw, target)
        log(f"重依赖指标 {len(heavy_indicators)} 条")

    # DLL check
    dlls = parse_dll_check(sections.get("DLL_SYSTEM32", ""))
    dll_found = sum(1 for v in dlls.values() if v)
    dll_total = len(dlls)
    log(f"DLL 检查: {dll_found}/{dll_total} 存在")

    # Classify
    classification, action = classify(registry_refs, heavy_indicators)

    # AppData (only if target given and running on engineer laptop)
    appdata_traces = []
    if target:
        appdata_traces = detect_appdata_traces(target)

    result = {
        "target_path": target or "NOT_SPECIFIED",
        "probe_source": txt_path,
        "probe_timestamp": datetime.now().isoformat(),
        "classification": classification,
        "action": action,
        "registry_refs": registry_refs,
        "heavy_indicators": heavy_indicators,
        "appdata_traces": appdata_traces,
        "os_info": os_info,
        "dll_check": dlls,
    }

    return result


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Parse probe_xp.bat output (result.txt) → scan_result.json"
    )
    parser.add_argument("input", type=str, help="result.txt 路径")
    parser.add_argument("--target", "-t", type=str, default=None,
                        help="目标程序路径 (可选，用于注册表交叉引用)")
    parser.add_argument("--output", "-o", type=str, default="scan_result.json",
                        help="输出 JSON 路径")

    args = parser.parse_args()

    result = parse_probe_result(args.input, args.target)

    out_path = Path(args.output)
    if not out_path.is_absolute():
        out_path = Path(os.path.dirname(os.path.abspath(__file__))) / args.output

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    log(f"分类: {result['classification']}")
    log(f"动作: {result['action']}")
    log(f"输出: {out_path}")

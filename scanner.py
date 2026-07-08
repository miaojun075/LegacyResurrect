#!/usr/bin/env python3
"""
Legacy App Migration MVP - Phase 1: Registry Scanner & Classifier
Day 1: Three-tier classification probe.
"""

import winreg
import os
import sys
import json
from pathlib import Path
from datetime import datetime

# --- Config ---
TARGET_PATH = None  # Will be set via CLI arg
WORKSPACE = Path(os.environ.get("LOCALAPPDATA", "C:\\Temp")) / "mvp_workspace"

# Heavy-dependency indicators
HEAVY_CLSID_PREFIXES = [
    r"SOFTWARE\Classes\CLSID",
    r"SOFTWARE\Classes\WOW6432Node\CLSID",
]
HEAVY_SERVICE_PATH = r"SYSTEM\CurrentControlSet\Services"
HEAVY_DRIVER_KEYWORDS = [".sys", "\\drivers\\", "\\System32\\drivers"]


def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}")


# ==================== Task 1.1: Registry Read ====================

def read_uninstall_keys(bits=64):
    """Read all entries from HKLM Uninstall registry."""
    access = winreg.KEY_READ
    if bits == 32:
        access |= winreg.KEY_WOW64_32KEY
    elif bits == 64:
        access |= winreg.KEY_WOW64_64KEY

    uninstall_paths = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    ]
    entries = []

    for base_path in uninstall_paths:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base_path, 0, access) as key:
                idx = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(key, idx)
                        entries.append((base_path, subkey_name))
                        idx += 1
                    except OSError:
                        break
        except OSError:
            continue

    return entries


def read_key_values(root, subkey_path, bits=64):
    """Read all values from a specific registry key."""
    access = winreg.KEY_READ
    if bits == 32:
        access |= winreg.KEY_WOW64_32KEY
    elif bits == 64:
        access |= winreg.KEY_WOW64_64KEY

    try:
        hkey_map = {
            "HKLM": winreg.HKEY_LOCAL_MACHINE,
            "HKCU": winreg.HKEY_CURRENT_USER,
            "HKCR": winreg.HKEY_CLASSES_ROOT,
        }
        actual_root = hkey_map.get(root.split("\\")[0], winreg.HKEY_LOCAL_MACHINE)
        actual_path = subkey_path if root in subkey_path else subkey_path

        with winreg.OpenKey(actual_root, actual_path, 0, access) as key:
            values = {}
            idx = 0
            while True:
                try:
                    name, data, dtype = winreg.EnumValue(key, idx)
                    values[name] = {"data": str(data), "type": dtype}
                    idx += 1
                except OSError:
                    break
            return values
    except OSError:
        return None


def find_registry_refs(target_path: str):
    """Find all registry entries that reference the target path."""
    norm_target = os.path.normpath(target_path).lower()
    log(f"搜索注册表中引用目标路径的记录: {target_path}")

    matches = []

    # Search HKLM Uninstall (64-bit + 32-bit)
    for bits in [64, 32]:
        entries = read_uninstall_keys(bits)
        for base, subkey in entries:
            full_path = f"{base}\\{subkey}"
            values = read_key_values("HKLM", full_path, bits)
            if values is None:
                continue

            # Check if any value points to target path
            for name, val in values.items():
                data_lower = val["data"].lower()
                if norm_target in data_lower or os.path.normpath(data_lower).startswith(norm_target):
                    matches.append({
                        "source": f"HKLM\\{full_path} (bits={bits})",
                        "key_name": subkey,
                        "value_name": name,
                        "value_data": val["data"],
                        "value_type": val["type"],
                    })

    # Search HKCU Software
    hkcu_found = _search_hkcu_for_target(norm_target)
    matches.extend(hkcu_found)

    return matches


def _search_hkcu_for_target(norm_target: str):
    """Search HKCU\Software for subkeys referencing the target."""
    matches = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software", 0, winreg.KEY_READ) as sw_key:
            idx = 0
            vendors = []
            while True:
                try:
                    vendors.append(winreg.EnumKey(sw_key, idx))
                    idx += 1
                except OSError:
                    break

        for vendor in vendors:
            try:
                vendor_path = f"Software\\{vendor}"
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, vendor_path, 0, winreg.KEY_READ) as vk:
                    aidx = 0
                    while True:
                        try:
                            app = winreg.EnumKey(vk, aidx)
                            app_path = f"{vendor_path}\\{app}"
                            values = read_key_values("HKCU", app_path)
                            if values:
                                for n, v in values.items():
                                    if norm_target in v["data"].lower():
                                        matches.append({
                                            "source": f"HKCU\\{app_path}",
                                            "key_name": app,
                                            "value_name": n,
                                            "value_data": v["data"],
                                            "value_type": v["type"],
                                        })
                            aidx += 1
                        except OSError:
                            break
            except OSError:
                continue
    except OSError:
        pass
    return matches


# ==================== Task 1.2: Three-tier Classification ====================

def check_heavy_indicators(target_path: str):
    """Check for heavy-dependency indicators: COM CLSID, Services, Drivers."""
    norm_target = os.path.normpath(target_path).lower()
    heavy_matches = []

    # Check CLSID entries
    for clsid_path in HEAVY_CLSID_PREFIXES:
        heavy_matches.extend(_search_clsid_for_target(clsid_path, norm_target))

    # Check Services
    heavy_matches.extend(_search_services_for_target(norm_target))

    return heavy_matches


def _search_clsid_for_target(clsid_base: str, norm_target: str):
    """Search CLSID registry for InprocServer32/LocalServer32 pointing to target path."""
    matches = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, clsid_base, 0, winreg.KEY_READ) as clsids:
            idx = 0
            count = 0
            while count < 500:  # Limit search to avoid timeout
                try:
                    clsid_name = winreg.EnumKey(clsids, idx)
                    clsid_full = f"{clsid_base}\\{clsid_name}"
                    # Check subkeys
                    for sub in ["InprocServer32", "LocalServer32"]:
                        try:
                            vals = read_key_values("HKLM", f"{clsid_full}\\{sub}")
                            if vals and "(default)" in vals:
                                val_data = vals["(default)"]["data"].lower()
                                if norm_target in val_data:
                                    matches.append({
                                        "source": f"HKLM\\{clsid_full}\\{sub}",
                                        "indicator": "COM_CLSID",
                                        "details": vals["(default)"]["data"],
                                    })
                        except OSError:
                            pass
                    idx += 1
                    count += 1
                except OSError:
                    break
    except OSError:
        pass
    return matches


def _search_services_for_target(norm_target: str):
    """Check if target path has registered Windows services."""
    matches = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, HEAVY_SERVICE_PATH, 0, winreg.KEY_READ) as svcs:
            idx = 0
            count = 0
            while count < 500:
                try:
                    svc_name = winreg.EnumKey(svcs, idx)
                    svc_path = f"{HEAVY_SERVICE_PATH}\\{svc_name}"
                    vals = read_key_values("HKLM", svc_path)
                    if vals and "ImagePath" in vals:
                        ip = vals["ImagePath"]["data"].lower()
                        if norm_target in ip:
                            matches.append({
                                "source": f"HKLM\\{svc_path}",
                                "indicator": "SERVICE",
                                "details": vals["ImagePath"]["data"],
                            })
                            # Check for driver (.sys files)
                            if any(kw in ip for kw in HEAVY_DRIVER_KEYWORDS):
                                matches[-1]["indicator"] = "DRIVER"
                    idx += 1
                    count += 1
                except OSError:
                    break
    except OSError:
        pass
    return matches


def check_appdata_traces(target_path: str):
    """Check if the app has left traces in %APPDATA% or %LOCALAPPDATA%."""
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


# ==================== Task 1.3: Classification Logic ====================

def classify(target_path: str):
    """Main classification engine."""
    log(f"===== 开始三级分类: {target_path} =====")
    log(f"目标路径: {target_path}")

    if not os.path.exists(target_path):
        log(f"错误: 目标路径不存在", "ERROR")
        return None

    result = {
        "target_path": target_path,
        "classification": None,
        "registry_refs": [],
        "heavy_indicators": [],
        "appdata_traces": [],
        "action": None,
    }

    # Step 1: Registry scan
    result["registry_refs"] = find_registry_refs(target_path)
    log(f"注册表引用: {len(result['registry_refs'])} 条")

    # Step 2: Heavy indicators
    result["heavy_indicators"] = check_heavy_indicators(target_path)
    log(f"重依赖指标: {len(result['heavy_indicators'])} 条")

    # Step 3: AppData traces
    result["appdata_traces"] = check_appdata_traces(target_path)
    log(f"AppData残留: {len(result['appdata_traces'])} 条")

    # Step 4: Classify
    if result["heavy_indicators"]:
        result["classification"] = "HEAVY_DEPENDENCY"
        result["action"] = "ABORT"
        log("分类结果: 重依赖 - 中止自动化 (V1.0不处理)", "WARN")
        for h in result["heavy_indicators"]:
            log(f"  → {h['indicator']}: {h['details'][:100]}")
    elif result["registry_refs"]:
        result["classification"] = "LIGHT_DEPENDENCY"
        result["action"] = "EXTRACT_AND_MIGRATE"
        log("分类结果: 轻依赖 - 可提取注册表并迁移", "OK")
    elif not result["registry_refs"] and not result["appdata_traces"]:
        result["classification"] = "PURE_GREEN"
        result["action"] = "COPY_ONLY"
        log("分类结果: 纯绿色 - 直接复制即可", "OK")
    else:
        # Has appdata traces but no registry = mostly green
        result["classification"] = "PURE_GREEN"
        result["action"] = "COPY_ONLY"
        log("分类结果: 纯绿色 (仅有AppData残留,不影响运行)", "OK")

    # Summary
    log("")
    log("===== 分类完毕 =====")
    log(f"类型: {result['classification']}")
    log(f"动作: {result['action']}")
    log(f"注册表项: {len(result['registry_refs'])}")
    log(f"COM/服务/驱动: {len(result['heavy_indicators'])}")
    log(f"AppData残留: {len(result['appdata_traces'])}")

    return result


# ==================== Main ====================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python scanner.py <目标程序路径>")
        print("示例: python scanner.py D:\\LegacyApp")
        sys.exit(1)

    TARGET_PATH = sys.argv[1]
    result = classify(TARGET_PATH)

    # Save result as JSON
    out_path = Path(TARGET_PATH.replace(":", "").replace("\\", "_")) if TARGET_PATH else Path("output")
    output_file = Path(os.path.dirname(__file__)) / "scan_result.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    log(f"结果已保存至: {output_file}")

    # Exit appropriately
    if result and result["action"] == "ABORT":
        log(" 重依赖程序,引擎自动中止.", "FATAL")
        sys.exit(1)
    else:
        log(" 分类通过,可进入下一阶段.", "OK")
        sys.exit(0)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai_fixer_v2.py — XinResurrect AI Repair Engine v2.0
=====================================================
跨平台（Windows / Linux-Wine / XP）统一修复引擎。

输入：context_bridge.py 产出的 unified_context.json
输出：结构化修复指令 → 执行 → 验证 → 重试（最多 3 次）

v2.0 vs v1.0：
  - 入口从双 JSON → 单 unified_context.json
  - Action 从 5 种 → 4 种：install_dependency / copy_dependency / configure_layer / block
  - System Prompt 重写为"环境修复编译器"范式
  - 执行器升级为跨平台（Windows 注册表 + Linux apt/wine）
"""

import json
import os
import re
import sys
import shutil
import subprocess
import platform as _platform
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Any, Tuple

IS_WINDOWS = _platform.system() == "Windows"
if IS_WINDOWS:
    import winreg

# ============================================================
# Config
# ============================================================
MAX_RETRY = 3
DEPOT_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / "depot"
SCRIPT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))

LLM_API_URL = os.environ.get("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "sk-placeholder")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")
LLM_TIMEOUT = 90


def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = {"INFO": "  ", "OK": "[OK]", "WARN": "[WARN]", "ERROR": "[ERR]",
              "ACTION": "[*]", "AI": "[*]", "RETRY": "[*]", "BLOCK": "[X]"}.get(level, "  ")
    print(f"[{ts}] {prefix} {msg}", flush=True)


# ============================================================
# Module 1: Context Compactor
# ============================================================

def compact_context(unified: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract AI-relevant fields from unified_context.json.
    Return a compact payload that fits in LLM context window.
    """
    env = unified.get("environment", {})
    crash = unified.get("crash_context", {})
    target = unified.get("target_app", {})
    assessment = unified.get("migration_assessment", {})

    # Dependencies: only show missing
    deps = env.get("dependencies", {})
    # Cap to top 10 critical missing deps to fit LLM context
    missing_deps = deps.get("missing", [])
    if len(missing_deps) > 10:
        missing_deps = missing_deps[:10]

    # Frameworks: only show what's relevant to current platform
    fw = env.get("frameworks", {})
    frameworks_compact = {}
    if fw.get("vcpp") is not None:
        # Show count + oldest/newest per arch
        vcpp = fw["vcpp"]
        frameworks_compact["vcpp_count"] = len(vcpp) if isinstance(vcpp, list) else 0
    if fw.get("dotnet") is not None and isinstance(fw["dotnet"], dict):
        frameworks_compact["dotnet_max"] = fw["dotnet"].get("max_version")

    # Graphics: only relevant if app might need display
    gfx = env.get("graphics", {})
    graphics_compact = {
        "display_server": gfx.get("display_server"),
        "opengl_available": gfx.get("opengl", {}).get("available", False),
    }

    return {
        "schema_version": unified.get("schema_version"),
        "source_platform": unified.get("source_platform"),
        "target_app": target,
        "crash_context": {
            "crashed": crash.get("crashed"),
            "exit_code_hex": (crash.get("exit_code") or {}).get("hex"),
            "category": crash.get("category"),
            "stderr_tail": crash.get("stderr_tail", [])[-5:],
            "stdout_tail": crash.get("stdout_tail", [])[-3:],
        },
        "host": env.get("host", {}),
        "compatibility_layer": env.get("compatibility_layer", {}),
        "missing_dependencies": missing_deps,
        "frameworks": frameworks_compact,
        "graphics": graphics_compact,
        "containers": env.get("containers", {}),
        "assessment": assessment,
    }


# ============================================================
# Module 2: System Prompt (The "Iron Law")
# ============================================================

SYSTEM_PROMPT = """You are XinResurrect AI Engine v2.0 — an expert system migration compiler.

Your ONLY purpose: read a unified_context JSON payload and output a valid JSON array of repair actions.

## CRITICAL RULES (violation = catastrophic failure):
1. Output ONLY valid JSON. No markdown, no explanations, no conversational text.
2. Your output MUST be a JSON ARRAY of action objects, even if there is only one action.
3. Every action object must have exactly these keys: "action", "target", "parameters", "reason", "confidence".
4. NEVER invent filenames, paths, or package names not present in the input context.
5. NEVER suggest actions that require resources you cannot confirm exist.

## ALLOWED ACTIONS

### 1. install_dependency
Use when: missing dependencies have known package managers or installers.
Triggers:
  - Windows: missing_dependencies with package_hint == "vc_runtime" — BUT check vcpp_count; if 40+, those runtimes ARE installed and the DLLs are just old-version leftovers, NOT blockers
  - Linux: missing_dependencies with package_hint like "libpng12-0" → apt install
  - Linux: compatibility_layer shows wine not installed → apt install wine
CRITICAL for target: use the EXACT installer filename from context. For VC++: "vcredist_x86_2008.exe" not "Microsoft Visual C++ 2008..."
Schema:
{
  "action": "install_dependency",
  "target": "vcredist_x86_2008.exe | wine | libpng12-0",
  "parameters": {
    "method": "apt | offline_installer",
    "package": "vcredist_x86_2008.exe | apt-package-name",
    "silent_flags": "/quiet /norestart | -y"
  },
  "reason": "concise diagnosis",
  "confidence": "high | medium | low"
}

### 2. copy_dependency
Use when: missing dependency is a custom/application-specific file (not a system package).
Triggers:
  - stderr/stdout explicitly names a specific DLL not in system packages (e.g. "legacy_runtime.dll")
  - missing_dependencies with package_hint == "custom" OR "unknown" when stderr explicitly names it
  - Windows: a single DLL with no associated runtime installer
CRITICAL: target MUST be the exact filename from stderr, not a description.
Schema:
{
  "action": "copy_dependency",
  "target": "legacy_runtime.dll",
  "parameters": {
    "filename": "legacy_runtime.dll",
    "kind": "dll | so",
    "bits": 32 or 64 or null,
    "source_hint": "where to find it"
  },
  "reason": "concise diagnosis",
  "confidence": "high | medium | low"
}

### 3. configure_layer
Use when: the compatibility layer needs configuration changes.
Triggers:
  - crash_context shows exit code 0xC0000005 (ACCESS_VIOLATION) but all DLLs present
  - Wine is installed but target app targets wrong Windows version
  - Registry keys or environment variables need adjustment
  - Windows 11 running XP-era app → need compatibility shim
Schema:
{
  "action": "configure_layer",
  "target": "wine | registry | env | windows_compat",
  "parameters": {
    "type": "winecfg | set_registry | set_env | set_compat_mode",
    "wine_prefix": "/path/to/prefix (for winecfg)",
    "hive": "HKCU (for registry)",
    "key": "Software\\...",
    "value_name": "name",
    "value_data": "value",
    "env_var": "WINEPREFIX",
    "env_value": "/path",
    "compat_mode": "WINXPSP3 | WIN7"
  },
  "reason": "concise diagnosis",
  "confidence": "high | medium | low"
}

### 4. block
Use when: automatic repair is impossible.
Triggers:
  - migration_assessment.level == "LEVEL_3" (architecture incompatible)
  - Missing directx (hardware-dependent)
  - CPU arch mismatch with no emulation available
  - No LLM can confidently determine the fix
Schema:
{
  "action": "block",
  "target": "summary of the blocker",
  "parameters": {
    "blocker": "detailed, human-readable reason",
    "suggested_manual_action": "what a human should do"
  },
  "reason": "concise diagnosis",
  "confidence": "high | medium | low"
}

## DIAGNOSIS GUIDELINES

### Cross-Platform Exit Code Decoder:
- 0xC0000135 → DLL_NOT_FOUND → copy_dependency or install_dependency
- 0xC000007B → BAD_IMAGE_FORMAT → 32/64-bit mismatch → copy_dependency with correct bits
- 0xC0000005 → ACCESS_VIOLATION → often VC++ or compatibility → install_dependency or configure_layer
- 0xC0000142 → DLL_INIT_FAILED → runtime broken → install_dependency
- SEGFAULT (Linux) → missing .so or Wine config issue → install_dependency or configure_layer
- WINE_ERROR → wine configuration → configure_layer

### Decision Tree:
1. Is level == LEVEL_3? → block
2. Does stderr explicitly name a SPECIFIC custom DLL (e.g. "legacy_runtime.dll")? → copy_dependency for THAT DLL ONLY — do NOT also try to install system runtimes
3. Does crash_context category show a system-level problem AND missing_dependencies exist AND vcpp_count < 10 (meaning runtimes genuinely missing)? → install_dependency
4. Crash but no missing deps? → configure_layer
5. Stdout/stderr has explicit error message? → follow it literally and ONLY repair what stderr asks for

KEY RULE: If error message says "missing X.dll" and it's a CUSTOM DLL, output ONLY copy_dependency for X.dll. Do NOT add install_dependency for unrelated system DLLs.
KEY RULE: If vcpp_count > 20, the host already has VC++ runtimes installed. Do NOT recommend install_dependency for VC++ versions.

## Linux/Wine Special Rules (OVERWRITE above when host.os_type == "linux")
On Linux, environmental gaps ARE the crash.
1. If compatibility_layer.type == "wine_missing" or installed == false → FIRST action: install_dependency(wine). THEN install_dependency for each missing .so with its package_hint.
2. If compatibility_layer.installed == false, skip copy_dependency and configure_layer — Wine must exist before anything else.
3. If source_platform == "linux" and crash category is ENV_NOT_READY, ignore the "vcpp_count" rule — on Linux there is no vcpp.
4. If CPU arch != x86_64 and no emulation available → block (architecture barrier).

### Confidence Rules:
- "high": exit code + stderr message form clear, unambiguous diagnosis
- "medium": exit code points to a class of problems, but exact fix may need one attempt
- "low": crash without clear signature, doing best guess

## IMPORTANT: Output Format
Your ENTIRE response must be a JSON array. Example:

[
  {
    "action": "install_dependency",
    "target": "wine",
    "parameters": {"method": "apt", "package": "wine", "silent_flags": "-y"},
    "reason": "Wine not installed on Linux host",
    "confidence": "high"
  },
  {
    "action": "copy_dependency",
    "target": "legacy_runtime.dll",
    "parameters": {"filename": "legacy_runtime.dll", "kind": "dll", "bits": 32, "source_hint": "custom legacy runtime from source system"},
    "reason": "Application-specific DLL not in any standard package",
    "confidence": "high"
  }
]

Do NOT wrap in markdown. Do NOT add any text before or after the JSON array.
"""


# ============================================================
# Module 3: LLM Call
# ============================================================

def call_llm(context: Dict[str, Any]) -> Tuple[Optional[List[Dict]], str]:
    """Call LLM, return (parsed_action_list, raw_text)."""
    log("调用 AI 修复引擎 v2.0 ...", "AI")

    user_message = json.dumps(context, ensure_ascii=False, indent=2)

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Repair this legacy app:\n\n{user_message}"},
        ],
        "temperature": 0.05,
        "max_tokens": 4096,
    }

    req_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_API_KEY}",
    }
    req_data = json.dumps(payload).encode("utf-8")

    try:
        import urllib.request
        import urllib.error
        req = urllib.request.Request(LLM_API_URL, data=req_data, headers=req_headers)
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else str(e)
        log(f"API HTTP {e.code}: {error_body[:200]}", "ERROR")
        return None, error_body
    except Exception as e:
        log(f"API 调用失败: {e}", "ERROR")
        return None, str(e)

    try:
        raw_text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        log(f"API 响应格式异常", "ERROR")
        return None, json.dumps(body)

    parsed = extract_json_array(raw_text)
    if parsed:
        log(f"AI 返回 {len(parsed)} 个修复动作", "OK")
        for i, act in enumerate(parsed):
            log(f"  [{i+1}] {act.get('action')} → {act.get('target')} (置信度: {act.get('confidence', '?')})")
    else:
        log(f"JSON 数组解析失败, 尝试单对象...", "WARN")
        single = extract_json(raw_text)
        if single and isinstance(single, dict) and "action" in single:
            parsed = [single]
            log(f"  回退: 单个 action={single.get('action')}", "OK")
        else:
            log(f"  完全无法解析. 原始: {raw_text[:300]}", "ERROR")

    return parsed, raw_text


def extract_json_array(text: str) -> Optional[List[Dict]]:
    """Extract JSON array from LLM output."""
    text = re.sub(r'```(?:json)?\s*', '', text)
    text = re.sub(r'```', '', text)
    text = text.strip()

    # Find first [ ... ]
    start = text.find('[')
    if start == -1:
        return None
    depth = 0
    end = -1
    for i in range(start, len(text)):
        if text[i] == '[':
            depth += 1
        elif text[i] == ']':
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        return None

    json_str = text[start:end + 1]
    json_str = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', json_str)
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        repaired = json_str.replace("'", '"')
        repaired = re.sub(r',\s*}', '}', repaired)
        repaired = re.sub(r',\s*]', ']', repaired)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            return None


def extract_json(text: str) -> Optional[Dict]:
    """Extract single JSON object (fallback)."""
    text = re.sub(r'```(?:json)?\s*', '', text)
    text = re.sub(r'```', '', text)
    start = text.find('{')
    if start == -1:
        return None
    depth = 0
    end = -1
    for i in range(start, len(text)):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        return None
    json_str = text[start:end + 1]
    json_str = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', json_str)
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        repaired = json_str.replace("'", '"')
        repaired = re.sub(r',\s*}', '}', repaired)
        repaired = re.sub(r',\s*]', ']', repaired)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            return None


def validate_actions(actions: List[Dict]) -> List[Dict]:
    """Filter and validate actions, return only valid ones."""
    valid_set = {"install_dependency", "copy_dependency", "configure_layer", "block"}
    result = []
    for act in actions:
        if not isinstance(act, dict):
            continue
        action = act.get("action", "")
        if action not in valid_set:
            log(f"无效 action 已忽略: {action}", "WARN")
            continue
        if not act.get("target"):
            log(f"action 缺少 target 已忽略: {action}", "WARN")
            continue
        result.append(act)
    return result


# ============================================================
# Module 4: Cross-Platform Action Executor
# ============================================================

def execute_action(
    action: str,
    target: str,
    parameters: Dict[str, Any],
    source_platform: str,
    workspace: str,
) -> bool:
    """Route and execute a single repair action."""
    log(f"执行修复: {action} → {target}", "ACTION")

    if action == "install_dependency":
        return _exec_install(parameters, source_platform)
    elif action == "copy_dependency":
        return _exec_copy(parameters, workspace)
    elif action == "configure_layer":
        return _exec_configure(parameters, source_platform, workspace)
    elif action == "block":
        blocker = parameters.get("blocker", "unknown")
        suggested = parameters.get("suggested_manual_action", "no suggestion")
        log(f"阻断: {blocker}", "BLOCK")
        log(f"  建议人工操作: {suggested}", "BLOCK")
        return False
    else:
        log(f"未知动作: {action}", "ERROR")
        return False


def _exec_install(params: Dict, source_platform: str) -> bool:
    """Install a dependency via package manager or offline installer."""
    method = params.get("method", "")
    package = params.get("package", params.get("target", ""))
    silent_flags = params.get("silent_flags", "")

    if not package:
        log("install_dependency: 缺少 package", "ERROR")
        return False

    if method in ("apt", "apt_key"):
        return _apt_install(package, silent_flags)
    elif method in ("offline_installer", "winget"):
        return _offline_install(package, silent_flags)
    else:
        # Auto-detect
        if not IS_WINDOWS:
            return _apt_install(package, silent_flags or "-y")
        else:
            return _offline_install(package, silent_flags)


def _apt_install(package: str, flags: str) -> bool:
    """apt-get install on Linux."""
    if IS_WINDOWS:
        log(f"apt install 仅在 Linux 可用, 当前为 Windows", "WARN")
        return False
    # 容器/root 环境不强制 sudo
    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    cmd = (["sudo", "apt-get"] if not is_root else ["apt-get"]) + ["install"] + flags.split() + [package]
    log(f"  apt: {' '.join(cmd)}", "ACTION")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            log(f"  安装成功: {package}", "OK")
            return True
        else:
            log(f"  apt 返回 {r.returncode}: {r.stderr[:200]}", "WARN")
            return False
    except Exception as e:
        log(f"  apt 失败: {e}", "ERROR")
        return False


def _offline_install(installer_name: str, flags: str) -> bool:
    """Install from depot/runtimes/ offline installer."""
    candidates = list(DEPOT_DIR.rglob(installer_name))
    if not candidates:
        log(f"离线安装包未找到: {installer_name}", "WARN")
        log(f"  请放置到 {DEPOT_DIR / 'runtimes' / installer_name}", "WARN")
        return False

    installer = candidates[0]
    cmd = [str(installer)] + flags.split()
    log(f"  静默安装: {' '.join(cmd)}", "ACTION")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        log(f"  安装完成 (退出码={r.returncode})", "OK")
        return True
    except subprocess.TimeoutExpired:
        log("  安装超时", "WARN")
        return False
    except Exception as e:
        log(f"  安装失败: {e}", "ERROR")
        return False


def _exec_copy(params: Dict, workspace: str) -> bool:
    """Copy a dependency file from depot to workspace."""
    filename = params.get("filename", params.get("target", ""))
    kind = params.get("kind", "dll")
    bits = params.get("bits")

    if not filename:
        log("copy_dependency: 缺少 filename", "ERROR")
        return False

    # Search depot
    candidates = list(DEPOT_DIR.rglob(filename))
    if not candidates:
        log(f"文件不在本地仓库: {filename}", "WARN")
        log(f"  请放置到 {DEPOT_DIR / kind / filename}", "WARN")
        return False

    src = candidates[0]
    dest = Path(workspace) / "app" / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    log(f"  已复制: {filename} -> {dest}", "OK")

    # For Windows DLLs, try regsvr32
    if IS_WINDOWS and kind == "dll" and params.get("register"):
        try:
            subprocess.run(["regsvr32", "/s", str(dest)], timeout=15, capture_output=True)
            log(f"  已注册 COM: {filename}", "OK")
        except Exception:
            pass

    return True


def _exec_configure(params: Dict, source_platform: str, workspace: str) -> bool:
    """Configure compatibility layer."""
    cfg_type = params.get("type", "")

    if cfg_type == "set_registry":
        return _reg_write(params)
    elif cfg_type == "set_env":
        return _env_set(params)
    elif cfg_type == "set_compat_mode":
        return _compat_mode_set(params, workspace)
    elif cfg_type == "winecfg":
        return _winecfg_set(params)
    else:
        # Auto-detect based on platform
        if source_platform == "linux" and params.get("wine_prefix"):
            return _winecfg_set(params)
        elif params.get("hive") and params.get("key"):
            return _reg_write(params)
        elif params.get("env_var"):
            return _env_set(params)
        else:
            log(f"configure_layer: 无法识别的配置类型", "WARN")
            return False


def _reg_write(params: Dict) -> bool:
    """Write a Windows registry key."""
    if not IS_WINDOWS:
        log("注册表写入仅在 Windows 可用", "WARN")
        return False
    hive_str = params.get("hive", "HKCU")
    key = params.get("key", "")
    value_name = params.get("value_name", "")
    value_data = params.get("value_data", "")
    value_type = params.get("value_type", "REG_SZ")

    hive_map = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER,
                "HKCR": winreg.HKEY_CLASSES_ROOT}
    hive = hive_map.get(hive_str, winreg.HKEY_CURRENT_USER)

    try:
        with winreg.CreateKey(hive, key) as h:
            if value_type == "REG_DWORD":
                winreg.SetValueEx(h, value_name, 0, winreg.REG_DWORD, int(value_data or 0))
            else:
                winreg.SetValueEx(h, value_name, 0, winreg.REG_SZ, str(value_data))
        log(f"  注册表: {hive_str}\\{key} : {value_name} = {value_data}", "OK")
        return True
    except Exception as e:
        log(f"  注册表失败: {e}", "ERROR")
        return False


def _env_set(params: Dict) -> bool:
    """Set an environment variable (session-only)."""
    var = params.get("env_var", "")
    value = params.get("env_value", "")
    if var and value:
        os.environ[var] = value
        log(f"  环境变量: {var}={value}", "OK")
        return True
    log("  set_env: 缺少 env_var 或 env_value", "WARN")
    return False


def _compat_mode_set(params: Dict, workspace: str) -> bool:
    """Set Windows compatibility mode via registry."""
    if not IS_WINDOWS:
        return False
    mode = params.get("compat_mode", "WINXPSP3")
    exe_name = params.get("exe_name", "")
    if not exe_name:
        log("set_compat_mode: 缺少 exe_name", "WARN")
        return False

    compat_map = {"WINXPSP3": "WINXPSP3", "WIN7": "WIN7RTM", "WIN8": "WIN8RTM"}
    key = f"Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers"
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as h:
            winreg.SetValueEx(h, exe_name, 0, winreg.REG_SZ, compat_map.get(mode, mode))
        log(f"  兼容模式: {exe_name} -> {mode}", "OK")
        return True
    except Exception as e:
        log(f"  兼容模式设置失败: {e}", "ERROR")
        return False


def _winecfg_set(params: Dict) -> bool:
    """Set Wine prefix Windows version."""
    wine_prefix = params.get("wine_prefix", os.path.expanduser("~/.wine"))
    win_ver = params.get("windows_version", "win10")
    os.environ["WINEPREFIX"] = wine_prefix
    log(f"  Wine: WINEPREFIX={wine_prefix} win_version={win_ver}", "OK")
    # winecfg -v is non-interactive
    try:
        subprocess.run(["winecfg", "-v", win_ver], capture_output=True, timeout=10, env=os.environ)
    except Exception:
        pass  # non-essential
    return True


# ============================================================
# Module 5: Repair Loop
# ============================================================

def run_repair_loop(unified: Dict[str, Any], workspace: str, executable: str) -> Dict[str, Any]:
    """Full repair loop (max MAX_RETRY attempts)."""
    log("=" * 60)
    log("  XinResurrect AI Repair Engine v2.0")
    log("=" * 60)

    source_platform = unified.get("source_platform", "unknown")
    crash = unified.get("crash_context", {})
    assessment = unified.get("migration_assessment", {})
    compat = unified.get("environment", {}).get("compatibility_layer", {})

    # Linux diagnostic mode: environment gaps = crash equivalent
    needs_diagnosis = (
        source_platform == "linux" and
        (
            not compat.get("installed") or
            len(assessment.get("blockers", [])) > 0 or
            len(unified.get("environment", {}).get("dependencies", {}).get("missing", [])) > 0
        )
    )

    if not crash.get("crashed") and not needs_diagnosis:
        log("程序未崩溃, 环境无阻断, 无需修复.", "OK")
        return {"status": "already_ok", "attempts": 0, "history": []}

    if needs_diagnosis and not crash.get("crashed"):
        log("诊断模式: 环境缺失即视为错误", "WARN")
        unified.setdefault("crash_context", {})
        unified["crash_context"]["crashed"] = True
        unified["crash_context"]["category"] = unified["crash_context"].get("category") or "ENV_NOT_READY"
        unified["crash_context"]["exit_code_hex"] = unified["crash_context"].get("exit_code_hex") or "WINE_NOT_INSTALLED"

    history: List[Dict] = []

    for attempt in range(1, MAX_RETRY + 1):
        log(f"\n--- 修复尝试 {attempt}/{MAX_RETRY} ---", "RETRY")

        # Step 1: Compact + call LLM
        context = compact_context(unified)
        actions, raw_text = call_llm(context)

        history.append({
            "attempt": attempt,
            "context_snapshot": {
                "crash": unified.get("crash_context", {}).get("category"),
                "missing_deps_count": len(context.get("missing_dependencies", []))
            },
            "ai_raw": raw_text,
        })

        if not actions:
            log("AI 未返回有效 JSON, 修复中止.", "ERROR")
            break

        # Step 2: Validate
        actions = validate_actions(actions)
        if not actions:
            log("AI 返回空动作列表, 修复中止.", "ERROR")
            break

        history[-1]["actions"] = actions

        # Step 3: Execute all actions
        all_ok = True
        any_executed = False
        for act in actions:
            if act["action"] == "block":
                log(f"AI 判定阻断: {act.get('parameters', {}).get('blocker', '?')}", "BLOCK")
                all_ok = False
                break
            ok = execute_action(
                act["action"], act["target"], act.get("parameters", {}),
                source_platform, workspace,
            )
            any_executed = True
            if not ok:
                log(f"动作 {act['action']} 执行失败", "WARN")
                all_ok = False

        if not any_executed:
            break

        # Step 4: Re-launch and verify
        log("重新启动程序验证...", "ACTION")
        if source_platform == "linux" or not executable:
            # Linux diagnosis mode: can't launch Windows exe from here
            # Verification happens on the target host
            log("Linux 诊断模式: 验证将推迟到目标主机执行", "OK")
            return {"status": "diagnosis_complete", "attempts": attempt, "history": history, "final_error": None}
        if IS_WINDOWS:
            from packager import launch_and_monitor, extract_error_signature
            proc_result = launch_and_monitor(Path(executable), Path(workspace) / "app")
            new_error = extract_error_signature(proc_result)
        else:
            # Linux: try wine launch
            env = os.environ.copy()
            env["WINEPREFIX"] = env.get("WINEPREFIX", os.path.expanduser("~/.wine"))
            try:
                r = subprocess.run(["wine", executable], capture_output=True, text=True,
                                   timeout=30, cwd=Path(workspace) / "app", env=env)
                new_error = {
                    "crashed": r.returncode != 0,
                    "exit_code": r.returncode,
                    "exit_code_hex": hex(r.returncode) if r.returncode else None,
                    "signature": "OK" if r.returncode == 0 else "WINE_ERROR",
                    "stderr_tail": r.stderr.split("\n")[-5:] if r.stderr else [],
                }
            except subprocess.TimeoutExpired:
                new_error = {"crashed": True, "signature": "TIMEOUT", "exit_code": None}
            except Exception as e:
                new_error = {"crashed": True, "signature": str(e), "exit_code": None}

        if not new_error.get("crashed"):
            log(f"修复成功! 程序正常运行 (尝试 {attempt} 次)", "OK")
            return {"status": "repaired", "attempts": attempt, "history": history, "final_error": None}

        log(f"修复未解决, 新错误: {new_error.get('signature')}", "WARN")
        # Update crash context in unified for next iteration
        unified["crash_context"]["category"] = new_error.get("signature", unified["crash_context"]["category"])
        unified["crash_context"] = {
            **unified["crash_context"],
            "crashed": new_error.get("crashed", True),
            "exit_code_hex": new_error.get("exit_code_hex"),
            "stderr_tail": new_error.get("stderr_tail", []),
        }
        history[-1]["launch_result"] = new_error

    log(f"\n{'='*60}")
    log(f"已尝试 {MAX_RETRY} 次, 仍无法修复.", "ERROR")
    return {"status": "failed", "attempts": MAX_RETRY, "history": history, "final_error": unified.get("crash_context")}


# ============================================================
# Module 6: Dry Run
# ============================================================

def dry_run(unified_path: str):
    """Print what would be sent to LLM."""
    with open(unified_path, "r", encoding="utf-8") as f:
        unified = json.load(f)
    context = compact_context(unified)
    print("=" * 60)
    print("  DRY RUN: Context to AI (v2.0)")
    print("=" * 60)
    print(f"\nSystem Prompt: {len(SYSTEM_PROMPT)} chars")
    print(f"Compact Context: {len(json.dumps(context))} chars")
    print(f"\n--- Compact Context ---")
    print(json.dumps(context, ensure_ascii=False, indent=2))
    print(f"\n--- AI would receive this and output JSON array ---")
    print(f"API: {LLM_API_URL}")
    print(f"Model: {LLM_MODEL}")


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="XinResurrect AI Repair Engine v2.0")
    p.add_argument("--unified", "-u", default="unified_context.json", help="context_bridge.py 输出的 unified_context.json")
    p.add_argument("--output", "-o", default="ai_fixer_v2_report.json", help="修复报告输出路径")
    p.add_argument("--dry-run", action="store_true", help="不调用 LLM, 仅打印上下文")
    p.add_argument("--max-retry", type=int, default=MAX_RETRY, help=f"最大重试次数 (默认: {MAX_RETRY})")

    args = p.parse_args()

    unified_path = Path(args.unified)
    if not unified_path.is_absolute():
        unified_path = SCRIPT_DIR / args.unified

    (DEPOT_DIR / "dll").mkdir(parents=True, exist_ok=True)
    (DEPOT_DIR / "runtimes").mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        dry_run(str(unified_path))
        sys.exit(0)

    if not unified_path.exists():
        log(f"错误: 找不到 unified_context.json: {unified_path}", "ERROR")
        log("请先运行: python context_bridge.py --platform windows ...", "INFO")
        sys.exit(1)

    with open(unified_path, "r", encoding="utf-8") as f:
        unified = json.load(f)

    target = unified.get("target_app", {})
    executable = target.get("entry_point", "")
    # packager workspace: Legacy_MVP_Workspace/FakeLegacyApp/  (app/ is 1 level below exe)
    workspace = str(Path(executable).parent.parent) if executable else "."

    result = run_repair_loop(unified, workspace, executable)

    out_path = Path(args.output)
    if not out_path.is_absolute():
        out_path = SCRIPT_DIR / args.output

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    log(f"\n修复报告已保存: {out_path}")
    sys.exit(0 if result.get("status") in ("repaired", "already_ok") else 1)

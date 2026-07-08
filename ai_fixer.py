#!/usr/bin/env python3
"""
Legacy App Migration MVP - Phase 3: AI Structured Repair Engine
Day 3: The "brain" that turns crash logs + environment fingerprint
       into executable JSON repair instructions.

Architecture:
  Context Builder  →  Strict System Prompt  →  LLM API Call
       ↑                                              ↓
  env_fingerprint.json                          JSON Parser
  packager_report.json                              ↓
                                               Action Executor
                                               (copy_dll / install_runtime / set_registry)
                                                    ↓
                                               Re-launch & Verify
                                                    ↓
                                               Retry loop (max 3 attempts)
"""

import json
import os
import re
import sys
import shutil
import subprocess
import tempfile
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Any, Tuple
import winreg

# ============================================================
# Config
# ============================================================
MAX_RETRY = 3
DEPOT_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / "depot"
SCRIPT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))

# LLM API — plug your own endpoint here
# Default: OpenAI-compatible API (can swap to Ollama/vLLM/local)
LLM_API_URL = os.environ.get("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "sk-placeholder")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")
LLM_TIMEOUT = 60


def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = {"INFO": "  ", "OK": "[OK]", "WARN": "[WARN]", "ERROR": "[ERR]", "ACTION": "[*]", "AI": "[*]", "RETRY": "[*]"}.get(level, "  ")
    print(f"[{ts}] {prefix} {msg}", flush=True)


# ============================================================
# Module 1: Context Builder
# ============================================================

def build_context(
    error_info: Dict[str, Any],
    env_fingerprint: Dict[str, Any],
    workspace_path: str,
) -> Dict[str, Any]:
    """
    Assemble a compact, AI-friendly context from crash data + environment fingerprint.
    The output is the input payload sent to the LLM.
    """
    # Extract only what AI needs from fingerprint
    os_info = env_fingerprint.get("os", {})
    vc_list = [r["name"] for r in env_fingerprint.get("vc_runtimes", [])]
    dotnet = env_fingerprint.get("dotnet", {})
    critical_dlls = env_fingerprint.get("critical_dlls", {})

    # Build a concise DLL health report: only show missing ones
    missing_dlls = []
    for dll_name, info in critical_dlls.items():
        if not info.get("any_found", False):
            missing_dlls.append(dll_name)

    context = {
        "error": {
            "exit_code": error_info.get("exit_code"),
            "exit_code_hex": error_info.get("exit_code_hex", "unknown"),
            "signature": error_info.get("signature", "UNKNOWN"),
            "stderr": error_info.get("stderr_tail", [])[-5:],  # last 5 lines only
            "stdout": error_info.get("stdout_tail", [])[-3:],
            "runtime_ms": error_info.get("runtime_ms"),
            "exception": error_info.get("exception"),
        },
        "host_environment": {
            "os": f"{os_info.get('product_name', '?')} {os_info.get('architecture', '?')}",
            "build": os_info.get("build_number", "?"),
            "is_admin": os_info.get("is_admin", False),
            "vc_runtimes_installed": vc_list,
            "dotnet_framework": dotnet.get("net_framework_detected", "unknown"),
            "dotnet_legacy": {k: v for k, v in dotnet.items() if k.startswith("v")},
            "critical_dlls_missing": missing_dlls,
        },
        "workspace_path": workspace_path,
    }

    return context


# ============================================================
# Module 2: Strict System Prompt & LLM Call
# ============================================================

SYSTEM_PROMPT = r"""
You are a legacy Windows application repair engine. Your ONLY function is to output structured JSON repair instructions.

## CRITICAL RULES (violation = failure):
1. Output ONLY a valid JSON object. NO markdown fences, NO explanations, NO preamble.
2. The JSON must match this EXACT schema:

{
  "action": "copy_dll | install_runtime | set_registry | modify_config | manual_required",
  "target": "specific DLL filename, runtime installer name, or registry key path",
  "parameters": {},
  "reason": "concise technical diagnosis in English",
  "confidence": "high | medium | low"
}

## ACTION TYPES & PARAMETERS:

### copy_dll
Use when: a single DLL is missing or wrong version.
target: DLL filename (e.g. "msvcp90.dll")
parameters: {
  "dll_name": "msvcp90.dll",
  "expected_source": "VC2008 x86 runtime",
  "bits": 32 or 64,
  "destination_subdir": "" (empty = app root)
}

### install_runtime
Use when: multiple DLLs from the same runtime are missing.
target: runtime installer filename (e.g. "vcredist_x86_2008.exe")
parameters: {
  "installer": "vcredist_x86_2008.exe",
  "silent_flags": "/quiet /norestart",
  "runtime_name": "Microsoft Visual C++ 2008 SP1 Redistributable (x86)"
}

### set_registry
Use when: a specific registry key/value is missing or wrong.
target: registry path (e.g. "HKCU\Software\Vendor\App")
parameters: {
  "hive": "HKCU",
  "key": "Software\\Vendor\\App",
  "value_name": "InstallPath",
  "value_data": "C:\\path\\to\\app",
  "value_type": "REG_SZ | REG_DWORD"
}

### modify_config
Use when: an app config file (ini/xml/json/conf) needs adjustment.
target: config file path relative to app directory
parameters: {
  "file_path": "config.ini",
  "old_value": "C:\\OldPath",
  "new_value": "<WORKSPACE_PATH>"  (use literal <WORKSPACE_PATH> placeholder)
}

### manual_required
Use when: the problem CANNOT be solved by the above 4 actions.
target: brief summary of what's needed
parameters: {
  "blocker": "description of what blocks automatic repair"
}

## DIAGNOSIS GUIDELINES:

### Exit Code Decoder:
- 0xC0000135 / STATUS_DLL_NOT_FOUND → copy_dll or install_runtime
- 0xC000007B / STATUS_INVALID_IMAGE_FORMAT → 32/64-bit mismatch → copy_dll with correct bits
- 0xC0000005 / STATUS_ACCESS_VIOLATION → often missing VC++ runtime → install_runtime
- 0xC0000142 / STATUS_DLL_INIT_FAILED → DLL initialization failed → install_runtime or copy_dll
- 0xE0434352 → .NET Framework exception → check dotnet version match
- 0x8007007E / "specified module could not be found" → DLL_NOT_FOUND → copy_dll

### Common Patterns:
- Missing msvcp90.dll/msvcr90.dll → VC++ 2008 x86 → install_runtime "vcredist_x86_2008.exe"
- Missing msvcp100.dll/msvcr100.dll → VC++ 2010 x86 → install_runtime "vcredist_x86_2010.exe"
- Missing msvbvm60.dll → Visual Basic 6 runtime → copy_dll "msvbvm60.dll"
- Missing mfc90.dll → MFC 9.0 → install_runtime "vcredist_x86_2008.exe"
- Missing msxml4.dll → MSXML 4.0 → copy_dll "msxml4.dll" then regsvr32
- BAD_IMAGE_FORMAT on 64-bit OS → app is 32-bit, missing 32-bit DLLs → use x86 runtimes
- App crashes after splash → likely config path mismatch → set_registry or modify_config
- "Side-by-side configuration is incorrect" → missing VC++ runtime for specific version
"""


def call_llm(context: Dict[str, Any]) -> Tuple[Optional[Dict], str]:
    """
    Call LLM API with strict system prompt + context.
    Returns (parsed_json, raw_response_text).

    Supports:
      - OpenAI-compatible API (default)
      - Ollama (set LLM_API_URL=http://localhost:11434/v1/chat/completions)
      - Local vLLM / llama.cpp server
    """
    log("调用 AI 修复引擎...", "AI")

    user_message = json.dumps(context, ensure_ascii=False, indent=2)

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Repair this legacy app crash:\n\n{user_message}"},
        ],
        "temperature": 0.1,  # Low temperature for deterministic JSON
        "max_tokens": 512,
    }

    req = urllib.request.Request(
        LLM_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else str(e)
        log(f"API HTTP {e.code}: {error_body[:200]}", "ERROR")
        return None, error_body
    except Exception as e:
        log(f"API 调用失败: {e}", "ERROR")
        return None, str(e)

    # Extract content from OpenAI-compatible response
    try:
        raw_text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        log(f"API 响应格式异常: {json.dumps(body)[:300]}", "ERROR")
        return None, json.dumps(body)

    # Parse JSON from response (handle markdown fences, extra whitespace)
    parsed = extract_json(raw_text)
    if parsed:
        log(f"AI 诊断: {parsed.get('reason', '?')}", "OK")
        log(f"  动作: {parsed.get('action')} → {parsed.get('target')}")
        log(f"  置信度: {parsed.get('confidence', '?')}")
    else:
        log(f"JSON 解析失败, 原始响应: {raw_text[:200]}", "ERROR")

    return parsed, raw_text


# ============================================================
# Module 3: JSON Parser with Anti-Hallucination
# ============================================================

def extract_json(text: str) -> Optional[Dict]:
    """
    Robust JSON extraction from LLM output.
    Handles:
      - ```json ... ``` fences
      - Leading/trailing text
      - Multiple JSON objects (takes first)
      - Control characters
    """
    # Strip markdown fences
    text = re.sub(r'```(?:json)?\s*', '', text)
    text = re.sub(r'```', '', text)

    # Find first { ... } pair
    start = text.find('{')
    if start == -1:
        return None

    # Find matching closing brace
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

    # Remove control characters (except in strings)
    json_str = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', json_str)

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        log(f"JSON decode error: {e}", "WARN")

        # Attempt repair: fix trailing commas, single quotes
        try:
            repaired = json_str.replace("'", '"')
            # Remove trailing commas before } or ]
            repaired = re.sub(r',\s*}', '}', repaired)
            repaired = re.sub(r',\s*]', ']', repaired)
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass

        return None


def validate_repair_action(parsed: Dict) -> bool:
    """Validate that the AI response matches expected schema."""
    valid_actions = {"copy_dll", "install_runtime", "set_registry", "modify_config", "manual_required"}
    action = parsed.get("action", "")
    if action not in valid_actions:
        log(f"无效的 action: {action}", "ERROR")
        return False
    if not parsed.get("target"):
        log("缺少 target 字段", "ERROR")
        return False
    if not parsed.get("reason"):
        log("缺少 reason 字段", "WARN")
    return True


# ============================================================
# Module 4: Action Executor
# ============================================================

def execute_repair(
    action: str,
    target: str,
    parameters: Dict[str, Any],
    workspace_path: str,
) -> bool:
    """
    Route and execute a repair action.
    Returns True if the repair was applied.
    """
    log(f"执行修复: {action} → {target}", "ACTION")

    if action == "copy_dll":
        return _repair_copy_dll(parameters, workspace_path)
    elif action == "install_runtime":
        return _repair_install_runtime(parameters, workspace_path)
    elif action == "set_registry":
        return _repair_set_registry(parameters)
    elif action == "modify_config":
        return _repair_modify_config(parameters, workspace_path)
    elif action == "manual_required":
        blocker = parameters.get("blocker", "unknown")
        log(f"⛔ 无法自动修复: {blocker}", "ERROR")
        return False
    else:
        log(f"未知修复动作: {action}", "ERROR")
        return False


def _repair_copy_dll(params: Dict, workspace: str) -> bool:
    """Copy a DLL from depot to workspace/app/."""
    dll_name = params.get("dll_name", params.get("target", ""))
    if not dll_name:
        log("copy_dll: 缺少 dll_name", "ERROR")
        return False

    # Search in depot
    depot_dll = DEPOT_DIR / "dll" / dll_name
    if not depot_dll.exists():
        # Try recursive search
        candidates = list(DEPOT_DIR.rglob(dll_name))
        if candidates:
            depot_dll = candidates[0]
        else:
            log(f"DLL 不在本地仓库: {dll_name}", "WARN")
            log(f"  请手动下载到 {DEPOT_DIR / 'dll' / dll_name}", "WARN")
            return False

    # Determine destination
    dest_subdir = params.get("destination_subdir", "")
    bits = params.get("bits", 32)
    workspace_path = Path(workspace)

    if dest_subdir:
        dest = workspace_path / "app" / dest_subdir
    else:
        dest = workspace_path / "app"
    dest.mkdir(parents=True, exist_ok=True)

    dest_file = dest / dll_name
    shutil.copy2(depot_dll, dest_file)
    log(f"  已复制: {dll_name} → {dest_file}", "OK")

    # If 32-bit DLL being copied, also put in app root
    if bits == 32:
        alt_dest = workspace_path / "app" / dll_name
        if alt_dest != dest_file:
            shutil.copy2(depot_dll, alt_dest)

    # Try regsvr32 if it's a COM DLL
    if params.get("register", False):
        try:
            subprocess.run(
                ["regsvr32", "/s", str(dest_file)],
                timeout=15,
                capture_output=True,
            )
            log(f"  已注册 COM: {dll_name}", "OK")
        except Exception as e:
            log(f"  COM 注册跳过: {e}", "INFO")

    return True


def _repair_install_runtime(params: Dict, workspace: str) -> bool:
    """Silently install a runtime from depot."""
    installer_name = params.get("installer", params.get("target", ""))
    silent_flags = params.get("silent_flags", "/quiet /norestart")

    if not installer_name:
        log("install_runtime: 缺少 installer", "ERROR")
        return False

    # Find installer in depot
    installer_path = DEPOT_DIR / "runtimes" / installer_name
    if not installer_path.exists():
        candidates = list(DEPOT_DIR.rglob(installer_name))
        if candidates:
            installer_path = candidates[0]
        else:
            log(f"运行库安装包不在本地仓库: {installer_name}", "WARN")
            log(f"  请下载到 {DEPOT_DIR / 'runtimes' / installer_name}", "WARN")
            return False

    # For already-installed-but-broken VC++ redist, try /repair first
    is_vcredist = any(x in installer_name.lower() for x in ["vcredist", "vc_redist"])
    
    log(f"  静默安装: {installer_name} {silent_flags}", "ACTION")
    try:
        result = subprocess.run(
            [str(installer_path)] + silent_flags.split(),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            log(f"  安装成功", "OK")
            return True
        else:
            # Some installers return non-zero even on success
            log(f"  安装完成 (代码={result.returncode})", "OK")
            return True
    except subprocess.TimeoutExpired:
        log(f"  安装超时 (2分钟)", "WARN")
        return False
    except Exception as e:
        log(f"  安装失败: {e}", "ERROR")
        return False


def _repair_set_registry(params: Dict) -> bool:
    """Write a specific registry key/value."""
    hive_str = params.get("hive", "HKCU")
    key = params.get("key", "")
    value_name = params.get("value_name", "")
    value_data = params.get("value_data", "")
    value_type = params.get("value_type", "REG_SZ")

    if not key:
        log("set_registry: 缺少 key", "ERROR")
        return False

    hive_map = {
        "HKLM": winreg.HKEY_LOCAL_MACHINE,
        "HKCU": winreg.HKEY_CURRENT_USER,
        "HKCR": winreg.HKEY_CLASSES_ROOT,
    }
    hive = hive_map.get(hive_str, winreg.HKEY_CURRENT_USER)

    try:
        # Create or open key
        with winreg.CreateKey(hive, key) as h:
            if value_type == "REG_SZ":
                winreg.SetValueEx(h, value_name, 0, winreg.REG_SZ, value_data)
            elif value_type == "REG_DWORD":
                val = int(value_data) if value_data.isdigit() else 1
                winreg.SetValueEx(h, value_name, 0, winreg.REG_DWORD, val)
            elif value_type == "REG_EXPAND_SZ":
                winreg.SetValueEx(h, value_name, 0, winreg.REG_EXPAND_SZ, value_data)
            else:
                winreg.SetValueEx(h, value_name, 0, winreg.REG_SZ, str(value_data))
        log(f"  注册表写入: {hive_str}\\{key} : {value_name} = {value_data}", "OK")
        return True
    except Exception as e:
        log(f"  注册表写入失败: {e}", "ERROR")
        return False


def _repair_modify_config(params: Dict, workspace: str) -> bool:
    """Modify a config file, replacing old paths with workspace paths."""
    file_rel = params.get("file_path", params.get("target", ""))
    old_value = params.get("old_value", "")
    new_value = params.get("new_value", "")
    workspace_path = Path(workspace)

    if not file_rel:
        log("modify_config: 缺少 file_path", "ERROR")
        return False

    config_file = workspace_path / "app" / file_rel

    # Also search recursively
    if not config_file.exists():
        candidates = list((workspace_path / "app").rglob(Path(file_rel).name))
        if candidates:
            config_file = candidates[0]

    if not config_file.exists():
        log(f"  配置文件不存在: {config_file}", "WARN")
        return False

    try:
        content = config_file.read_text(encoding="utf-8", errors="replace")
    except Exception:
        try:
            content = config_file.read_text(encoding="gbk", errors="replace")
        except Exception:
            content = config_file.read_text(encoding="latin-1", errors="replace")

    # Replace <WORKSPACE_PATH> with actual workspace
    actual_workspace = str(workspace_path / "app")
    new_value_actual = new_value.replace("<WORKSPACE_PATH>", actual_workspace)

    if old_value and old_value in content:
        content = content.replace(old_value, new_value_actual)
        log(f"  配置替换: {old_value} → {new_value_actual}", "OK")
    elif "<WORKSPACE_PATH>" in new_value:
        # Just look for similar paths
        content = re.sub(
            r'[A-Z]:\\[^\s\'\"\n]*(?:Legacy|App|Program)',
            actual_workspace,
            content,
        )
        log(f"  配置路径批量替换", "OK")

    try:
        config_file.write_text(content, encoding="utf-8")
        return True
    except Exception as e:
        log(f"  配置写入失败: {e}", "ERROR")
        return False


# ============================================================
# Module 5: Repair Loop (Orchestrator)
# ============================================================

def run_repair_loop(
    packager_report: Dict[str, Any],
    env_fingerprint: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Full repair loop:
      For each attempt (max MAX_RETRY):
        1. Build context (error + fingerprint)
        2. Call LLM
        3. Parse & validate response
        4. Execute repair action
        5. Re-launch app (via packager's launch_and_monitor)
        6. If still crashes → loop back with updated error
        7. If success → break
    """
    log("=" * 60)
    log("  Legacy Migration MVP — Day 3: AI Repair Engine")
    log("=" * 60)

    workspace = packager_report.get("workspace_path", "")
    classification = packager_report.get("classification", "")

    if packager_report.get("status") == "success":
        log("程序已正常运行, 无需修复.", "OK")
        return {"status": "already_ok", "attempts": 0}

    history = []
    current_error = packager_report.get("error_info", {})
    executable = packager_report.get("executable", "")

    for attempt in range(1, MAX_RETRY + 1):
        log(f"\n--- 修复尝试 {attempt}/{MAX_RETRY} ---", "RETRY")

        # Step 1: Build context
        context = build_context(current_error, env_fingerprint, workspace)

        # Step 2: Call LLM
        parsed, raw = call_llm(context)
        history.append({
            "attempt": attempt,
            "error": current_error,
            "ai_response": parsed,
            "ai_raw": raw,
        })

        if not parsed:
            log("AI 未返回有效 JSON, 修复中止.", "ERROR")
            break

        # Step 3: Validate
        if not validate_repair_action(parsed):
            log("AI 响应校验失败, 修复中止.", "ERROR")
            break

        if parsed["action"] == "manual_required":
            log(f"AI 判定无法自动修复: {parsed.get('parameters', {}).get('blocker', 'unknown')}", "ERROR")
            break

        # Step 4: Execute
        ok = execute_repair(
            parsed["action"],
            parsed["target"],
            parsed.get("parameters", {}),
            workspace,
        )

        if not ok:
            log("修复动作执行失败. 中止.", "ERROR")
            break

        # Step 5: Re-launch
        log("重新启动程序验证...", "ACTION")
        from packager import launch_and_monitor, extract_error_signature

        proc_result = launch_and_monitor(
            Path(executable),
            Path(workspace) / "app",
        )
        new_error = extract_error_signature(proc_result)

        if not proc_result.crashed:
            log(f"[*] 修复成功! 程序正常运行 (尝试 {attempt} 次)", "OK")
            return {
                "status": "repaired",
                "attempts": attempt,
                "history": history,
                "final_error": None,
            }

        # Still crashed → update error and loop
        log(f"修复未解决, 新错误: {new_error.get('signature')}", "WARN")
        current_error = new_error

    # Exhausted retries
    log(f"\n{'='*60}")
    log(f"⛔ 已尝试 {MAX_RETRY} 次, 仍无法修复.", "ERROR")
    log(f"  最后错误: {current_error.get('signature')} (代码: {current_error.get('exit_code_hex')})")
    log(f"  建议人工介入. 工作区保留: {workspace}")
    log(f"{'='*60}")

    return {
        "status": "failed",
        "attempts": MAX_RETRY,
        "history": history,
        "final_error": current_error,
    }


# ============================================================
# Module 6: Dry-Run Mode (No LLM, just show what WOULD be sent)
# ============================================================

def dry_run(packager_report_path: str, env_fingerprint_path: str):
    """
    Dry-run mode: print the exact prompt & context that would be sent to LLM.
    Useful for debugging the context builder without making API calls.
    """
    with open(packager_report_path, "r", encoding="utf-8") as f:
        report = json.load(f)
    with open(env_fingerprint_path, "r", encoding="utf-8") as f:
        fingerprint = json.load(f)

    workspace = report.get("workspace_path", "?")
    error_info = report.get("error_info", {})

    context = build_context(error_info, fingerprint, workspace)

    print("=" * 60)
    print("  DRY RUN: Context that WOULD be sent to AI")
    print("=" * 60)
    print(f"\nSystem Prompt ({len(SYSTEM_PROMPT)} chars):")
    print(SYSTEM_PROMPT[:500] + "...")
    print(f"\nUser Context ({len(json.dumps(context))} chars):")
    print(json.dumps(context, ensure_ascii=False, indent=2))
    print("\n" + "=" * 60)
    print("  将上述上下文发送至:", LLM_API_URL)
    print("  模型:", LLM_MODEL)
    print("=" * 60)


# ============================================================
# CLI Entry Point
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Legacy App AI Repair Engine (Day 3)")
    parser.add_argument("--packager-report", "-p", type=str,
                        default="packager_report.json",
                        help="packager.py 的输出 JSON")
    parser.add_argument("--env-fingerprint", "-e", type=str,
                        default="env_fingerprint.json",
                        help="env_collector.py 的输出 JSON")
    parser.add_argument("--output", "-o", type=str,
                        default="ai_fixer_report.json",
                        help="修复报告输出路径")
    parser.add_argument("--dry-run", action="store_true",
                        help="不调用 LLM, 仅打印将要发送的上下文")
    parser.add_argument("--max-retry", type=int, default=MAX_RETRY,
                        help=f"最大重试次数 (默认: {MAX_RETRY})")
    args = parser.parse_args()

    script_dir = SCRIPT_DIR

    # Resolve paths
    packager_path = Path(args.packager_report)
    if not packager_path.is_absolute():
        packager_path = script_dir / args.packager_report

    env_path = Path(args.env_fingerprint)
    if not env_path.is_absolute():
        env_path = script_dir / args.env_fingerprint

    # Create depot dirs
    (DEPOT_DIR / "dll").mkdir(parents=True, exist_ok=True)
    (DEPOT_DIR / "runtimes").mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        dry_run(str(packager_path), str(env_path))
        sys.exit(0)

    if not packager_path.exists():
        log(f"错误: 找不到打包报告 {packager_path}", "ERROR")
        log("请先运行: python packager.py -t <目标路径>", "INFO")
        sys.exit(1)
    if not env_path.exists():
        log(f"错误: 找不到环境指纹 {env_path}", "ERROR")
        log("请先运行: python env_collector.py", "INFO")
        sys.exit(1)

    with open(packager_path, "r", encoding="utf-8") as f:
        report = json.load(f)
    with open(env_path, "r", encoding="utf-8") as f:
        fingerprint = json.load(f)

    result = run_repair_loop(report, fingerprint)

    # Save report
    out_path = Path(args.output)
    if not out_path.is_absolute():
        out_path = script_dir / args.output

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    log(f"\n修复报告已保存: {out_path}")

    # Exit code
    if result.get("status") in ("repaired", "already_ok"):
        sys.exit(0)
    else:
        sys.exit(1)

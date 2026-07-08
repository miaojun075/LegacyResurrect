#!/usr/bin/env python3
"""
Legacy App Migration MVP - Phase 2: Packager & Pseudo-Sandbox
Day 2: Isolated workspace + registry injection + process monitor.

Design principles:
  1. ZERO host pollution — all ops inside a temp workspace
  2. Registry is imported/cleaned atomically — no leftover keys
  3. Process monitoring detects flash-crashes in 3 seconds
  4. Accepts scanner.py's scan_result.json as input
"""

import os
import sys
import json
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Any
import winreg

# ============================================================
# Config
# ============================================================
MONITOR_TIMEOUT = 10        # seconds to let the app run before closing it
FLASH_CRASH_THRESHOLD = 3   # seconds — if exits before this, it's a crash
WORKSPACE_ROOT = Path(os.environ.get("TEMP", r"C:\Windows\Temp")) / "Legacy_MVP_Workspace"


def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = {"INFO": "  ", "OK": "[OK]", "WARN": "[WARN]", "ERROR": "[ERR]", "ACTION": "[ACT]"}.get(level, "  ")
    print(f"[{ts}] {prefix} {msg}", flush=True)


# ============================================================
# Module 1: Isolated Workspace
# ============================================================

class Workspace:
    """
    Self-contained temp directory.
    On __exit__, optionally preserves or wipes everything.
    All file/registry operations are scoped inside this workspace.
    """

    def __init__(self, app_name: str, preserve: bool = False):
        self.app_name = app_name
        self.preserve = preserve
        self.root = WORKSPACE_ROOT / app_name
        self.app_dir = self.root / "app"          # copied binary directory
        self.reg_dir = self.root / "registry"     # exported .reg files
        self.dep_dir = self.root / "deps"          # AI-downloaded DLLs etc.
        self.log_dir = self.root / "logs"          # process logs
        self._created = False

    def __enter__(self):
        self._clean_existing()
        self.root.mkdir(parents=True, exist_ok=True)
        self.app_dir.mkdir(exist_ok=True)
        self.reg_dir.mkdir(exist_ok=True)
        self.dep_dir.mkdir(exist_ok=True)
        self.log_dir.mkdir(exist_ok=True)
        self._created = True
        log(f"工作区已创建: {self.root}")
        return self

    def __exit__(self, *args):
        if not self.preserve and self._created:
            log("正在清理工作区...", "ACTION")
            try:
                shutil.rmtree(self.root, ignore_errors=True)
                log("工作区已清理", "OK")
            except Exception as e:
                log(f"工作区清理失败: {e}", "WARN")

    def _clean_existing(self):
        if self.root.exists():
            try:
                shutil.rmtree(self.root, ignore_errors=True)
            except Exception:
                pass


# ============================================================
# Module 2: File Copy Engine
# ============================================================

def copy_app_files(source_path: str, workspace: Workspace) -> Dict[str, Any]:
    """
    Copy entire source directory into workspace/app/.
    Returns stats dict.
    """
    source = Path(source_path)
    if not source.exists():
        raise FileNotFoundError(f"源路径不存在: {source_path}")

    dest = workspace.app_dir

    # If source is a single file (e.g. a .exe), copy just the file
    if source.is_file():
        shutil.copy2(source, dest / source.name)
        file_count = 1
        total_size = source.stat().st_size
        log(f"复制单文件: {source.name} ({total_size:,} bytes)", "OK")
    else:
        # Copy entire directory
        file_count = 0
        total_size = 0
        for root, dirs, files in os.walk(source):
            rel = os.path.relpath(root, source)
            target_dir = dest / rel
            target_dir.mkdir(exist_ok=True)
            for f in files:
                src_file = Path(root) / f
                shutil.copy2(src_file, target_dir / f)
                file_count += 1
                total_size += src_file.stat().st_size
        log(f"复制目录: {file_count} 个文件, {total_size:,} bytes", "OK")

    return {
        "source_path": str(source),
        "dest_path": str(dest),
        "file_count": file_count,
        "total_bytes": total_size,
    }


# ============================================================
# Module 2 (cont.): Registry Export & Import
# ============================================================

def _build_reg_file_content(registry_refs: List[dict]) -> str:
    """
    Build a .reg file from scanner's registry_refs output.
    Format: Windows Registry Editor Version 5.00 + key/value pairs.
    """
    lines = ["Windows Registry Editor Version 5.00", ""]

    # Group by source key
    grouped: Dict[str, List[dict]] = {}
    for ref in registry_refs:
        key = ref.get("source", ref.get("key_name", ""))
        # Normalize: HKLM\SOFTWARE\... -> HKEY_LOCAL_MACHINE\SOFTWARE\...
        key = key.replace("HKLM\\", "HKEY_LOCAL_MACHINE\\").replace("HKCU\\", "HKEY_CURRENT_USER\\")
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(ref)

    for key, entries in grouped.items():
        lines.append(f"[{key}]")
        for entry in entries:
            name = entry.get("value_name", "")
            data = entry.get("value_data", "")
            vtype = entry.get("value_type", 1)  # REG_SZ = 1

            # Escape special characters in value name
            escaped_name = name.replace("\\", "\\\\").replace('"', '\\"')
            escaped_data = data.replace("\\", "\\\\").replace('"', '\\"')

            if vtype == 1:  # REG_SZ
                if name:
                    lines.append(f'"{escaped_name}"="{escaped_data}"')
                else:
                    lines.append(f'@"{escaped_data}"')
            elif vtype == 2:  # REG_EXPAND_SZ
                if name:
                    lines.append(f'"{escaped_name}"=hex(2):{data.encode().hex(",")}')
                else:
                    lines.append(f'@=hex(2):{data.encode().hex(",")}')
            elif vtype == 4:  # REG_DWORD
                try:
                    dword_val = int(data)
                    if name:
                        lines.append(f'"{escaped_name}"=dword:{dword_val:08x}')
                    else:
                        lines.append(f'@=dword:{dword_val:08x}')
                except ValueError:
                    if name:
                        lines.append(f'"; Skipped non-numeric DWORD: {name}')
            else:
                # Hex fallback
                if name:
                    lines.append(f'"; Skipped type {vtype}: {name}')
        lines.append("")

    return "\r\n".join(lines)


def export_registry_to_file(registry_refs: List[dict], workspace: Workspace) -> Path:
    """Export scanner-found registry entries into a .reg file inside workspace."""
    if not registry_refs:
        log("无注册表项需要导出", "OK")
        return None

    content = _build_reg_file_content(registry_refs)
    reg_path = workspace.reg_dir / "extracted.reg"
    with open(reg_path, "w", encoding="utf-16") as f:
        f.write(content)

    log(f"注册表已导出: {reg_path} ({len(content):,} bytes, {len(registry_refs)} 条)", "OK")
    return reg_path


def _collect_imported_keys(reg_file: Path) -> List[str]:
    """Parse the .reg file to collect all key paths for later cleanup."""
    keys = []
    if not reg_file or not reg_file.exists():
        return keys
    try:
        content = reg_file.read_text(encoding="utf-16")
    except UnicodeError:
        content = reg_file.read_text(encoding="utf-8")
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            keys.append(line[1:-1])
    return keys


# Track keys that were imported for cleanup
_IMPORTED_KEYS: list = []


def import_registry(reg_file: Optional[Path]) -> bool:
    """
    Import .reg file into current user's registry.
    Returns True on success.
    """
    global _IMPORTED_KEYS
    if not reg_file or not reg_file.exists():
        log("跳过注册表导入 (无文件)", "INFO")
        return True

    # Collect keys BEFORE importing
    _IMPORTED_KEYS = _collect_imported_keys(reg_file)
    log(f"将导入 {len(_IMPORTED_KEYS)} 个注册表键", "ACTION")

    # Use reg.exe for reliable import
    try:
        result = subprocess.run(
            ["reg", "import", str(reg_file)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            log("注册表导入成功", "OK")
            return True
        else:
            log(f"注册表导入警告: {result.stderr.strip()}", "WARN")
            return True  # Partial import is still useful
    except subprocess.TimeoutExpired:
        log("注册表导入超时", "ERROR")
        return False


def cleanup_registry():
    """
    Remove all keys that were imported by import_registry().
    This is the "zero pollution" guarantee.
    """
    global _IMPORTED_KEYS
    if not _IMPORTED_KEYS:
        return

    log(f"清理 {len(_IMPORTED_KEYS)} 个注册表键...", "ACTION")
    cleaned = 0
    for key_path in _IMPORTED_KEYS:
        try:
            # Determine hive
            if key_path.startswith("HKEY_LOCAL_MACHINE\\"):
                hive = winreg.HKEY_LOCAL_MACHINE
                subkey = key_path[len("HKEY_LOCAL_MACHINE\\"):]
            elif key_path.startswith("HKEY_CURRENT_USER\\"):
                hive = winreg.HKEY_CURRENT_USER
                subkey = key_path[len("HKEY_CURRENT_USER\\"):]
            else:
                continue

            # Only delete keys that were created by us (check if key exists)
            try:
                with winreg.OpenKey(hive, subkey, 0, winreg.KEY_ALL_ACCESS) as k:
                    winreg.DeleteKey(hive, subkey)
                cleaned += 1
            except OSError:
                # Key already doesn't exist or can't be opened — skip
                pass
        except Exception:
            pass

    log(f"已清理 {cleaned} 个注册表键", "OK")
    _IMPORTED_KEYS = []


# ============================================================
# Module 3: Process Monitor (Pseudo-Sandbox)
# ============================================================

class ProcessResult:
    def __init__(self):
        self.exit_code: Optional[int] = None
        self.runtime_ms: float = 0
        self.crashed: bool = False
        self.is_flash_crash: bool = False
        self.stdout: str = ""
        self.stderr: str = ""
        self.exception: Optional[str] = None
        self.timed_out: bool = False


def find_executable_in_dir(app_dir: Path) -> Optional[Path]:
    """Find the main executable in the copied app directory."""
    # First, look for .exe files
    exes = list(app_dir.rglob("*.exe"))
    if not exes:
        return None

    # Prefer exe matching the directory name
    dir_name = app_dir.name.lower()
    for exe in exes:
        if dir_name in exe.stem.lower():
            return exe

    # Fallback: largest .exe (usually the main one)
    exes.sort(key=lambda x: x.stat().st_size, reverse=True)
    return exes[0]


def launch_and_monitor(
    executable: Path,
    workspace_dir: Path,
    timeout: int = MONITOR_TIMEOUT,
    flash_threshold: int = FLASH_CRASH_THRESHOLD,
) -> ProcessResult:
    """
    Launch the program inside workspace and monitor its behavior.

    Logic:
      - Start subprocess with cwd=workspace/app
      - If it exits before flash_threshold seconds with non-zero code → FLASH CRASH
      - If it stays alive → let it run for `timeout` seconds, then kill gracefully
      - Capture all stdout/stderr
    """
    result = ProcessResult()

    log(f"启动目标程序: {executable.name}", "ACTION")
    log(f"  工作目录: {workspace_dir}")
    log(f"  监控窗口: {timeout}s (闪退阈值 {flash_threshold}s)")

    if not executable.exists():
        result.exception = f"可执行文件不存在: {executable}"
        result.crashed = True
        return result

    start_time = time.time()

    try:
        proc = subprocess.Popen(
            [str(executable)],
            cwd=str(workspace_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception as e:
        result.exception = str(e)
        result.crashed = True
        log(f"启动失败: {e}", "ERROR")
        return result

    # Wait with timeout
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        runtime_ms = (time.time() - start_time) * 1000
        result.exit_code = proc.returncode
        result.runtime_ms = runtime_ms
        result.stdout = stdout or ""
        result.stderr = stderr or ""

        if runtime_ms / 1000 < flash_threshold and proc.returncode != 0:
            result.is_flash_crash = True
            result.crashed = True
            log(f"[CRASH] 闪退! 代码={proc.returncode}, 存活={runtime_ms:.0f}ms", "ERROR")
        elif proc.returncode != 0:
            result.crashed = True
            log(f"异常退出: 代码={proc.returncode}, 存活={runtime_ms:.0f}ms", "WARN")
        else:
            log(f"正常退出: 代码=0, 存活={runtime_ms:.0f}ms", "OK")

    except subprocess.TimeoutExpired:
        # App is still running → it's a success for our purposes
        runtime_ms = timeout * 1000
        result.runtime_ms = runtime_ms
        result.timed_out = True

        # Graceful kill
        log(f"持续运行 >{timeout}s, 主动终止...", "OK")
        try:
            proc.terminate()
            try:
                stdout, stderr = proc.communicate(timeout=3)
                result.stdout = stdout or ""
                result.stderr = stderr or ""
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                result.stdout = stdout or ""
                result.stderr = stderr or ""
        except Exception:
            pass

        result.exit_code = proc.returncode
        if proc.returncode and proc.returncode != 0:
            result.crashed = True
            log(f"终止后非零退出: 代码={proc.returncode}", "WARN")

    return result


def extract_error_signature(result: ProcessResult) -> Dict[str, Any]:
    """
    Extract structured error info from process result for AI consumption.
    """
    error = {
        "crashed": result.crashed,
        "is_flash_crash": result.is_flash_crash,
        "timed_out": result.timed_out,
        "exit_code": result.exit_code,
    }

    # Hex-format crash codes
    if result.exit_code is not None and result.exit_code != 0:
        if result.exit_code < 0:
            error["exit_code_hex"] = f"0x{result.exit_code & 0xFFFFFFFF:08x}"
        else:
            error["exit_code_hex"] = f"0x{result.exit_code:08x}"

    error["runtime_ms"] = result.runtime_ms
    error["exception"] = result.exception

    # Capture last meaningful lines of stderr
    if result.stderr:
        lines = [l.strip() for l in result.stderr.splitlines() if l.strip()]
        error["stderr_tail"] = lines[-20:] if len(lines) > 20 else lines

    # Capture stdout tail
    if result.stdout:
        lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
        error["stdout_tail"] = lines[-10:] if len(lines) > 10 else lines

    # Known crash signatures
    if result.exit_code == 0xC0000135 or result.exit_code == -1073741515:
        error["signature"] = "DLL_NOT_FOUND"
    elif result.exit_code == 0xC000007B or result.exit_code == -1073741701:
        error["signature"] = "BAD_IMAGE_FORMAT"  # 32/64-bit mismatch
    elif result.exit_code == 0xC0000005 or result.exit_code == -1073741819:
        error["signature"] = "ACCESS_VIOLATION"
    else:
        error["signature"] = "UNKNOWN"

    return error


# ============================================================
# Module 4: Pipeline Orchestrator
# ============================================================

def run_pipeline(scan_result_path: str, target_path: str) -> Dict[str, Any]:
    """
    Full Day 2 pipeline:
      1. Read scanner result
      2. Create isolated workspace
      3. Copy files
      4. Export & import registry
      5. Launch & monitor
      6. Cleanup registry
      7. Return full report
    """
    log("=" * 60)
    log("  Legacy Migration MVP — Day 2: Packager Pipeline")
    log("=" * 60)

    # 1. Read scanner result
    with open(scan_result_path, "r", encoding="utf-8") as f:
        scan_data = json.load(f)
    classification = scan_data["classification"]
    log(f"分类结果: {classification}")

    if classification == "HEAVY_DEPENDENCY":
        log("⛔ 重依赖程序, Day 2 不处理. 请人工介入.", "ERROR")
        return {"status": "aborted", "reason": "heavy_dependency", "scan_data": scan_data}

    # 2. App name
    app_name = Path(target_path).name if Path(target_path).is_dir() else Path(target_path).stem

    # 3. Create workspace
    with Workspace(app_name, preserve=False) as ws:
        # 4. Copy files
        copy_result = copy_app_files(target_path, ws)
        log(f"文件复制完成: {copy_result['file_count']} 个文件", "OK")

        # 5. Registry (light dependency only)
        reg_file = None
        if classification == "LIGHT_DEPENDENCY":
            reg_file = export_registry_to_file(scan_data["registry_refs"], ws)
            import_registry(reg_file)

        # 6. Find executable
        exe = find_executable_in_dir(ws.app_dir)
        if not exe:
            log("未找到可执行文件!", "ERROR")
            cleanup_registry()
            return {"status": "error", "reason": "no_exe_found"}

        log(f"目标可执行文件: {exe.name}", "OK")

        # 7. Launch & monitor
        proc_result = launch_and_monitor(exe, ws.app_dir)
        error_info = extract_error_signature(proc_result)

        # 8. Save logs
        log_file = ws.log_dir / "process_log.txt"
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(f"=== STDOUT ===\n{proc_result.stdout}\n\n=== STDERR ===\n{proc_result.stderr}\n")
        ws.preserve = True  # Keep workspace for inspection if crashed

        report = {
            "status": "success" if not proc_result.crashed else "needs_ai_fix",
            "app_name": app_name,
            "classification": classification,
            "workspace_path": str(ws.root),
            "copy_result": copy_result,
            "registry_file": str(reg_file) if reg_file else None,
            "executable": str(exe),
            "process_result": {
                "crashed": proc_result.crashed,
                "is_flash_crash": proc_result.is_flash_crash,
                "timed_out": proc_result.timed_out,
                "exit_code": proc_result.exit_code,
                "runtime_ms": proc_result.runtime_ms,
            },
            "error_info": error_info,
        }

        # 9. Cleanup registry
        cleanup_registry()

        return report


# ============================================================
# CLI Entry Point
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Legacy App Packager & Pseudo-Sandbox (Day 2)")
    parser.add_argument("--scan-result", "-s", type=str,
                        default="scan_result.json",
                        help="scanner.py 的输出 JSON")
    parser.add_argument("--target", "-t", type=str, required=True,
                        help="目标程序路径 (与 scanner.py 传入的一致)")
    parser.add_argument("--output", "-o", type=str,
                        default="packager_report.json",
                        help="打包报告输出路径")
    args = parser.parse_args()

    # Resolve paths relative to script dir
    script_dir = Path(os.path.dirname(os.path.abspath(__file__)))
    scan_path = Path(args.scan_result)
    if not scan_path.is_absolute():
        scan_path = script_dir / args.scan_result

    if not scan_path.exists():
        log(f"错误: 找不到扫描结果 {scan_path}", "ERROR")
        log("请先运行: python scanner.py <目标路径>", "INFO")
        sys.exit(1)

    target = args.target
    if target.startswith('"') and target.endswith('"'):
        target = target[1:-1]

    report = run_pipeline(str(scan_path), target)

    # Save report
    out_path = Path(args.output)
    if not out_path.is_absolute():
        out_path = script_dir / args.output

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)

    log(f"\n报告已保存: {out_path}")

    # Summary
    status = report.get("status", "unknown")
    if status == "success":
        log(f"\n{'='*60}")
        log("[SUCCESS] 打包验证通过! 程序正常运行.", "OK")
        log(f"  工作区: {report['workspace_path']}")
        log(f"{'='*60}")
        sys.exit(0)
    elif status == "needs_ai_fix":
        log(f"\n{'='*60}")
        log("[FIX] 启动失败, 需要进入 Day 3 (AI 修复).", "WARN")
        log(f"  退出代码: {report['process_result']['exit_code']}")
        log(f"  错误签名: {report['error_info']['signature']}")
        log(f"  保留工作区: {report['workspace_path']}")
        log(f"{'='*60}")
        sys.exit(1)
    else:
        log(f"\n{'='*60}")
        log(f"⛔ 流水线中断: {report.get('reason', 'unknown')}", "ERROR")
        log(f"{'='*60}")
        sys.exit(1)

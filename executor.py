#!/usr/bin/env python3
"""
XinResurrect Executor — 将 AI 修复计划渲染为可执行脚本或直接执行。

输入:  ai_fixer_v2.py 输出的 AI 修复报告 (--report)
输出模式:
  --dry-run         只打印执行计划
  --script FILE.sh  生成独立可执行的修复脚本
  --execute         直接执行修复动作（需 root / 需 --confirm）
  --confirm         跳过交互式确认（CI/CD 场景）
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

IS_WINDOWS = platform.system() == "Windows"

# ── 模板函数（避免 .format() 与 bash $/{} 冲突） ──────────

def _script_header(timestamp: str, report_path: str, source_platform: str, action_count: int) -> str:
    return textwrap.dedent(f"""\
        #!/bin/bash
        # ============================================================
        #  XinResurrect 修复脚本
        #  生成时间: {timestamp}
        #  来源报告: {report_path}
        #  目标平台: {source_platform}
        # ============================================================
        #  安全提示: 请在执行前阅读本脚本全部内容
        #  回滚说明: 安装类操作可回滚（注释中有卸载命令）
        #            配置类操作有备份（.xinresurrect.bak）
        # ============================================================
        set -euo pipefail
        SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"

        # ── 回滚日志 ───────────────────────────────
        ROLLBACK_LOG="/tmp/xinresurrect_rollback_$$.log"
        echo "[XinResurrect] 回滚日志: $ROLLBACK_LOG" > "$ROLLBACK_LOG"

        rollback_install() {{
            local pkg="$1"
            echo "  [回滚] 卸载 $pkg ..."
            apt-get remove -y "$pkg" 2>/dev/null || true
        }}

        rollback_file() {{
            local bak="$1"
            if [ -f "$bak" ]; then
                echo "  [回滚] 恢复 $bak ..."
                mv "$bak" "${{bak%.xinresurrect.bak}}" 2>/dev/null || true
            fi
        }}

        echo "[XinResurrect] 开始执行修复计划..."
        echo "[XinResurrect] 动作总数: {action_count}"
        echo ""
    """)


def _script_footer(wine_verify: str) -> str:
    lines = [
        "",
        'echo ""',
        'echo "[XinResurrect] ✅ 修复脚本执行完成"',
        'echo "[XinResurrect] 回滚日志: $ROLLBACK_LOG"',
        "",
    ]
    for l in wine_verify.strip().split("\n"):
        lines.append(l)
    lines.append("")
    lines.append("exit 0")
    return "\n".join(lines)


def _wine_verify_block(exe_path: str) -> str:
    lines = [
        'if command -v wine &>/dev/null; then',
        '    echo "[XinResurrect] Wine 版本: $(wine --version 2>/dev/null || echo \'unknown\')"',
    ]
    if exe_path:
        lines.append(f'    if [ -f "{exe_path}" ]; then')
        lines.append(f'        echo "[XinResurrect] 尝试启动目标程序..."')
        lines.append(f'        wine "{exe_path}" &')
        lines.append('        WINEPID=$!')
        lines.append('        sleep 3')
        lines.append('        if kill -0 $WINEPID 2>/dev/null; then')
        lines.append('            echo "[XinResurrect] ✅ 目标程序已启动 (PID: $WINEPID)"')
        lines.append('        else')
        lines.append('            echo "[XinResurrect] ⚠️  程序启动后立即退出，请检查日志"')
        lines.append('        fi')
        lines.append('    fi')
    lines.append('fi')
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="XinResurrect Executor — AI 修复计划执行引擎"
    )
    p.add_argument("--report", "-r", required=True,
                   help="ai_fixer_v2.py 输出的修复报告 JSON")
    p.add_argument("--unified", "-u",
                   help="context_bridge.py 输出的统一上下文")
    p.add_argument("--dry-run", action="store_true", help="只打印执行计划，不生成脚本也不执行")
    p.add_argument("--script", "-s", help="生成独立修复脚本到此路径")
    p.add_argument("--execute", action="store_true", help="直接执行修复动作")
    p.add_argument("--confirm", action="store_true", help="跳过交互式确认")
    p.add_argument("--target-exe", help="目标 exe 路径（用于脚本末尾验证）")
    return p.parse_args()


# ── 加载 ──────────────────────────────────────────────────

def load_report(path: str) -> Dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    actions: List[Dict] = []
    for entry in data.get("history", []):
        actions.extend(entry.get("actions", []))
    return {"status": data.get("status"), "attempts": data.get("attempts"), "actions": actions}


def load_unified(path: Optional[str]) -> Dict[str, Any]:
    if not path or not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ── 权限检测 ──────────────────────────────────────────────

def _detect_root() -> bool:
    if IS_WINDOWS:
        import ctypes
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


# ── 渲染规则 ──────────────────────────────────────────────

def render_action(
    action: str, target: str, params: dict, platform: str, is_root: bool,
) -> tuple:
    """(commands_block, rollback_cmd, dry_run_desc)"""
    if action == "block":
        blocker = params.get("blocker", target)
        manual = params.get("suggested_manual_action", "请人工处理")
        comment = textwrap.dedent(f"""\
            # ══════════════════════════════════════════════
            # ⚠️  阻断: {blocker}
            # 建议人工操作: {manual}
            # 此步骤需要人工介入，脚本已跳过
            # ══════════════════════════════════════════════
        """)
        return comment, None, "[BLOCK] -> " + blocker

    if action == "install_dependency":
        return _render_install(params, is_root)
    if action == "copy_dependency":
        return _render_copy(params)
    if action == "configure_layer":
        return _render_configure(params, platform)

    return None, None, "[WARN] unknown action: " + action


def _render_install(params: dict, is_root: bool) -> tuple:
    method = params.get("method", "")
    package = params.get("package", params.get("target", ""))
    flags = params.get("silent_flags", "-y")
    if not package:
        return None, None, "[WARN] install missing package"

    if method in ("apt", "apt_key", ""):
        prefix = "" if is_root else "sudo "
        cmd = f"{prefix}apt-get install {flags} {package}"
        rollback = f"rollback_install {package}"
        return cmd, rollback, "[apt] install " + package

    if method in ("offline_installer", "winget"):
        exe = params.get("installer_path", package)
        if IS_WINDOWS:
            cmd = f'start /wait "" "{exe}" {flags}'
        else:
            cmd = f'wine "{exe}" {flags}' if exe.endswith(".exe") else f'bash "{exe}" {flags}'
        return cmd, None, "[pkg] offline install " + exe

    # fallback
    prefix = "" if is_root else "sudo "
    cmd = f"{prefix}apt-get install {flags} {package}"
    return cmd, f"rollback_install {package}", "[apt] install " + package


def _render_copy(params: dict) -> tuple:
    src = params.get("source", params.get("src", ""))
    dst = params.get("destination", params.get("dst", ""))
    if not src or not dst:
        return None, None, "⚠️  复制动作缺少 source/destination"
    cmd = textwrap.dedent(f"""\
        # 复制依赖文件
        if [ -f "{src}" ]; then
            cp -v "{src}" "{dst}"
        else
            echo "[XinResurrect] ⚠️  源文件不存在: {src}"
        fi
    """)
    return cmd, None, "[copy] " + src + " -> " + dst


def _render_configure(params: dict, platform: str) -> tuple:
    wine_cmd = params.get("wine_command", "")
    if not wine_cmd:
        return None, None, "⚠️  配置动作缺少 wine_command"
    wine_bin = params.get("wine_bin", "wine")
    cmd = f"{wine_bin} {wine_cmd}"
    return cmd, None, "[cfg] " + wine_bin + " " + wine_cmd


# ── 脚本组装 ──────────────────────────────────────────────

def build_script(
    actions: list, source_platform: str,
    report_path: str, target_exe: str | None = None,
) -> str:
    is_root = _detect_root()
    install_pkgs: list = []       # [(package, rollback_cmd)]
    other_blocks: list = []
    block_warnings: list = []

    for i, act in enumerate(actions):
        a_type = act.get("action", "")
        target = act.get("target", "")
        params = act.get("parameters", {})
        reason = act.get("reason", "")
        confidence = act.get("confidence", "?")

        cmd, rollback, desc = render_action(a_type, target, params, source_platform, is_root)

        if desc and desc.startswith("❌"):
            block_warnings.append(desc)
            if cmd:
                other_blocks.append(cmd)
            continue
        if not cmd:
            continue

        # 合并 apt install
        if a_type == "install_dependency" and params.get("method", "") in ("apt", "apt_key", ""):
            pkg = params.get("package", target)
            install_pkgs.append((pkg, rollback or f"rollback_install {pkg}"))
        else:
            block = textwrap.dedent(f"""\
                # ── 步骤 {i+1}: {a_type} → {target}
                # 原因: {reason}
                # 置信度: {confidence}
            """) + cmd + "\n"
            if rollback:
                block += f'echo "rollback_{i}: {rollback}" >> "$ROLLBACK_LOG"\n'
            other_blocks.append(block)

    # 组装
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    script = _script_header(ts, report_path, source_platform, len(actions))

    if block_warnings:
        script += "# ══════════════════════════════════════════════\n"
        script += "# ⚠️  以下动作需人工处理:\n"
        for w in block_warnings:
            script += f"#   {w}\n"
        script += "# ══════════════════════════════════════════════\n\n"

    # 批量 apt
    if install_pkgs:
        pkgs_str = " ".join(p[0] for p in install_pkgs)
        prefix = "" if is_root else "sudo "
        script += "# ── 批量安装系统依赖 ────────────────────────\n"
        script += f"echo '[XinResurrect] 安装 {len(install_pkgs)} 个系统依赖...'\n"
        script += f"{prefix}apt-get update -qq\n"
        script += f"{prefix}apt-get install -y {pkgs_str}\n"
        for pkg, rb in install_pkgs:
            script += f'echo "安装 {pkg} → 回滚: {rb}" >> "$ROLLBACK_LOG"\n'
        script += "echo '[XinResurrect] ✅ 系统依赖安装完成'\n\n"

    for blk in other_blocks:
        script += blk + "\n"

    wine_verify = _wine_verify_block(target_exe or "")
    script += _script_footer(wine_verify)
    return script


# ── 执行器 ────────────────────────────────────────────────

def execute_actions(actions: list, source_platform: str, confirm: bool) -> bool:
    is_root = _detect_root()

    if not confirm:
        print(f"\n即将执行 {len(actions)} 个修复动作")
        print(f"平台: {source_platform} | root: {is_root}")
        print("=" * 50)
        for a in actions:
            print(f"  [{a['action']}] {a.get('target','?')}")
        print("=" * 50)
        ans = input("确认执行? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消")
            return False

    ok, fail = 0, 0
    for act in actions:
        a_type = act["action"]
        target = act["target"]
        params = act.get("parameters", {})
        cmd, _, desc = render_action(a_type, target, params, source_platform, is_root)
        print(f"\n{desc or f'执行: {a_type} → {target}'}")
        if not cmd or cmd.startswith("#"):
            continue
        lines = [l for l in cmd.split("\n") if l.strip() and not l.strip().startswith("#")]
        for line in lines:
            print(f"  $ {line.strip()}")
            try:
                r = subprocess.run(line.strip(), shell=True, capture_output=True, text=True, timeout=300)
                if r.returncode == 0:
                    print("  ✅ 成功"); ok += 1
                else:
                    print(f"  ❌ 失败 (exit={r.returncode}): {r.stderr[:200]}"); fail += 1
            except Exception as e:
                print(f"  ❌ 异常: {e}"); fail += 1

    print(f"\n执行完成: {ok} 成功, {fail} 失败")
    return fail == 0


# ── 主入口 ────────────────────────────────────────────────

def main():
    args = parse_args()

    report = load_report(args.report)
    actions = report["actions"]
    if not actions:
        print("[executor] 报告中没有修复动作，无需执行")
        return

    source_platform = "linux"
    if args.unified:
        unified = load_unified(args.unified)
        source_platform = unified.get("source_platform", "linux")

    print(f"[executor] 加载 {len(actions)} 个修复动作")
    print(f"[executor] 平台: {source_platform}")
    print(f"[executor] 报告状态: {report['status']}")
    print()

    if args.dry_run:
        print("=== DRY RUN ===\n")
        for i, act in enumerate(actions):
            a_type = act["action"]
            target = act["target"]
            params = act.get("parameters", {})
            reason = act.get("reason", "")
            confidence = act.get("confidence", "?")
            _, _, desc = render_action(a_type, target, params, source_platform, _detect_root())
            print(f"[{i+1}/{len(actions)}] {desc or a_type + ' -> ' + target}")
            print(f"     原因: {reason}")
            print(f"     置信度: {confidence}")
            print()
        return

    if args.script:
        script = build_script(actions, source_platform, args.report, args.target_exe)
        out = Path(args.script)
        out.write_text(script, encoding="utf-8")
        out.chmod(0o755)
        print(f"[executor] Script generated: {out} ({len(script):,} bytes)")
        print(f"[executor] 可直接执行: bash {out}")
        return

    if args.execute:
        success = execute_actions(actions, source_platform, args.confirm)
        sys.exit(0 if success else 1)

    print("[executor] 请指定 --dry-run、--script 或 --execute")
    sys.exit(1)


if __name__ == "__main__":
    main()

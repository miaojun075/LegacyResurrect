# XinResurrect 使用指南

## 面向开发者 & 部署工程师

**版本**: v1.0 (2026-07-09)
**项目路径**: `legacy-migration-mvp/`
**仓库**: [github.com/miaojun075/LegacyResurrect](https://github.com/miaojun075/LegacyResurrect)
**最低 Python**: 3.8，纯 stdlib，无第三方依赖

---

## 目录

1. 快速开始
2. 文件结构与职责
3. 全链路流水线
4. 探针模块详解
5. 诊断引擎详解
6. 执行引擎详解
7. 常见问题排查
8. 已知限制与边界

---

## 1. 快速开始

### 1.1 一分钟运行

```bash
# 1. 采集环境指纹（信创 Linux 上）
python3 xin_probe.py -o my_env.json

# 2. 桥接为统一上下文
python3 context_bridge.py my_env.json

# 3. AI 诊断（需要 DashScope API Key）
export DASHSCOPE_API_KEY="your-key"
python3 ai_fixer_v2.py unified_context.json -r report.json --target my_app.exe

# 4. 生成修复脚本
python3 executor.py -r report.json -u unified_context.json \
    --script fix.sh --target-exe /opt/my_app/target.exe

# 5. 执行修复
bash fix.sh
```

### 1.2 Windows 端完整流水线

```powershell
python scanner.py                         # 扫描目标程序
python env_collector.py > env.json        # 采集 Windows 环境指纹
python context_bridge.py env.json         # 桥接
python ai_fixer_v2.py unified_context.json -r report.json
python executor.py -r report.json --script fix.bat
```

### 1.3 只需运行探针（用于评估）

```bash
python3 xin_probe.py -o /tmp/xin_fp.json
cat /tmp/xin_fp.json | python3 -m json.tool | head -50
```

输出示例：
```json
{
  "host": {
    "os": "Kylin V10 (SP1)",
    "cpu": "x86_64",
    "kernel": "Linux 5.10.0-8-generic"
  },
  "wine": {
    "installed": true,
    "path": "/opt/apps/kylin-wine/files/bin/wine",
    "version_raw": "Kylin Wine - no real backend"
  },
  "libraries": {
    "found": 10,
    "missing": 25,
    "missing_list": ["libx11.so.6", "libxext.so.6", ...]
  }
}
```

---

## 2. 文件结构与职责

```
legacy-migration-mvp/
├── README.md                    # 项目说明
│
├── 探针 (Probe)
│   ├── xin_probe.py            # Linux/信创 探针 (16.7KB)
│   ├── env_collector.py        # Windows 环境指纹采集
│   └── scanner.py              # Windows 程序三级分类扫描
│
├── 桥接 (Bridge)
│   ├── context_bridge.py       # 统一上下文归一化
│   ├── UNIFIED_SCHEMA.md       # Schema 定义
│   └── unified_context.json    # 桥接输出示例
│
├── 诊断 (AI Fixer)
│   ├── ai_fixer_v2.py          # 诊断引擎 v2.0 (29KB)
│   └── ai_fixer.py             # 旧版 v1.0 (Windows only)
│
├── 执行 (Executor)
│   ├── executor.py             # 修复计划→Bash 脚本 (13.7KB)
│   └── fix_script.sh           # 生成的修复脚本示例
│
├── 打包 (Packaging)
│   └── packager.py             # Windows 依赖打包+沙箱验证
│
├── 测试 & 模拟
│   ├── FakeLegacyApp/          # 零依赖测试态应用 (.NET)
│   ├── simulate_kylin.py       # 麒麟 V10 环境注入脚本
│   ├── run_kylin_sim.sh        # Docker 模拟执行脚本
│   ├── pipeline_kylin_v2.sh    # 全链路验证脚本
│   └── _show_results.py        # 结果展示脚本
│
└── 文档
    ├── XinResurrect_Whitepaper.md   # 对外白皮书
    └── executor-fix_20260709.md     # Executor Bug 修复记录
```

---

## 3. 全链路流水线

```
[start] ──▶ xin_probe.py ──▶ xin_env_fingerprint.json
                              │
                              ▼
                    context_bridge.py ──▶ unified_context.json
                              │
                              ▼
       ┌─── [规则引擎] 判定 LEVEL_1/2/3
       │
       ▼
ai_fixer_v2.py ◀──── DASHSCOPE_API_KEY
       │
       ▼ (最多 3 轮重试)
  ai_fixer_report.json ──▶ executor.py
                              │
                    ┌─────────┼─────────┐
                    ▼         ▼         ▼
               dry-run    script.sh   execute
```

### 3.1 三阶段状态码

| 阶段 | 状态 | 含义 |
|---|---|---|
| 探针 | `success` | 全部 7 步完成 |
| 诊断 | `diagnosis_complete` | AI 返回有效修复计划 |
| 诊断 | `repair_cycle_*` | 进入修复→验证循环 |
| 诊断 | `max_attempts` | 3 轮后依然失败 |

---

## 4. 探针模块 (xin_probe.py)

### 4.1 CLI 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `-o, --output` | `xin_env_fingerprint.json` | 输出路径 |
| `--no-wine` | false | 跳过 Wine 检测 |

### 4.2 采集项目

| 步骤 | 内容 | 说明 |
|---|---|---|
| 1. OS | NAME, VERSION, 内核, 架构 | 解析 /etc/os-release + /etc/debian_version 兜底 |
| 2. Wine | 路径, 版本, 架构 | `which wine` + `/opt/apps/kylin-wine/*` 非标检测 |
| 3. 核心库 | 35 项 .so 白名单 | `ldconfig -p` 解析 |
| 4. 运行时 | Python3, GCC | `which` 检测 |
| 5. 容器 | Docker, Podman 检测 | 写文件 + 读 cgroup |

### 4.3 输出 JSON 结构

```json
{
  "host": {
    "os": "Kylin V10 (SP1)",
    "cpu": "x86_64",
    "kernel": "Linux 5.10.0-8-generic",
    "graphics": {"available": false},
    "containers": {"docker": false, "podman": false}
  },
  "wine": {
    "installed": true,
    "path": "/opt/apps/kylin-wine/files/bin/wine",
    "version_raw": "..."
  },
  "libraries": {
    "found": 10,
    "missing": 25,
    "found_list": ["libc.so.6", ...],
    "missing_list": ["libx11.so.6", ...]
  },
  "diagnostics": {
    "migration_level": "LEVEL_2",
    "blockers": []
  }
}
```

---

## 5. 诊断引擎 (ai_fixer_v2.py)

### 5.1 CLI 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `pos.arg` | `unified_context.json` | 输入的统一上下文 |
| `-r, --report` | `ai_fixer_report.json` | 输出报告路径 |
| `--target` | 无 | 目标 exe 路径 |

### 5.2 System Prompt 四大铁律

1. **install_dependency**: 仅当 API 名称在 `missing_deps` 中且 `vcpp_count <= 20`
2. **copy_dependency**: 按 `prefer_copy` 列表中的 DLL/.so 返回精确文件名
3. **configure_layer**: Wine 配置 `winecfg` / `reg add` / 启动参数
4. **block**: 如 Wine 缺失 + 无法自动安装，标记 BLOCK 由人工处理

### 5.3 AI 输出协议

```json
[
  {
    "action": "install_dependency",
    "target": "libx11-6",
    "parameters": {"method": "apt", "package": "libx11-6", "silent_flags": "-y"},
    "reason": "Missing X11 client library required by Wine for GUI rendering",
    "confidence": "high"
  }
]
```

### 5.4 重试逻辑

- 诊断模式下：单轮诊断，返回修复计划
- 修复模式下：执行→验证→最多 3 轮，每轮输入包含上轮结果
- `needs_diagnosis` 触发条件：Wine 未安装 / 存在 blockers / Linux 端无 exe

---

## 6. 执行引擎 (executor.py)

### 6.1 CLI 模式

| 模式 | 命令 | 说明 |
|---|---|---|
| Dry Run | `--dry-run` | 打印执行计划，不生成脚本 |
| 生成脚本 | `--script fix.sh` | 输出可执行 Bash 脚本 |
| 直接执行 | `--execute` | 存在风险，需 `--confirm` |
| 验证程序 | `--target-exe /opt/apps/my.exe` | 脚本末尾 Wine 启动测试 |

### 6.2 动作合并策略

| 动作类型 | 是否合并 | 合并方式 |
|---|---|---|
| `install_dependency` + `apt` | ✅ | 所有 apt 包合并为一条 `apt-get install -y` |
| `install_dependency` + `offline` | ❌ | 每个包独立命令行 |
| `copy_dependency` | ❌ | 每个文件独立 `cp` |
| `configure_layer` | ❌ | 每个配置独立 `wine`/`winecfg` |

### 6.3 生成脚本特征

```bash
set -euo pipefail          # 严格模式，出错即停
ROLLBACK_LOG=/tmp/xinresurrect_rollback_$$.log  # 回滚日志
rollback_install() { ... } # 可逐项 apt-get remove
rollback_file() { ... }    # .xinresurrect.bak 备份恢复
```

### 6.4 安全设计

| 场景 | 行为 |
|---|---|
| 非 root 执行 apt | 自动加 `sudo` 前缀 |
| 阻断类型动作 | 生成注释警告，不作任何修改 |
| 文件复制 | 仅对存在的源文件执行 `cp` |
| 网络问题 | `set -e` 确保 apt 失败不会继续执行 |

---

## 7. 常见问题排查

### 7.1 "No module named 'xxx'"
```
# 探针和桥接层都是纯 stdlib，不应出现。
# executor.py 需要 Python 3.10+ (from __future__ import annotations)
python3 --version  # 确认 >= 3.10
```

### 7.2 "AI 诊断返回空或乱码"
```
1. 确认 DASHSCOPE_API_KEY 是否设置且有效
2. 检查 unified_context.json 是否 > 0 字节
3. 查看 --report 文件中的 raw_llm_response 字段
4. 如果提示 JSON 解析失败，查看 raw 响应的前 200 字符
```

### 7.3 "生成的 fix_script.sh 中 SCRIPT_DIR 是 ${{ }} 字面"
这是 v1.0 之前的 Bug，已在 `0e3f1e0` 修复。确认 `executor.py` 是最新版：
```bash
grep -n "BASH_SOURCE" executor.py
# 应显示: SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
# 即 Python 源码中的双花括号，生成的脚本中会变成单花括号
```

### 7.4 "麒麟环境探针报 host.os 为空"
检查 `/etc/os-release` 中是否有 `VERSION` 字段。如果没有（某些麒麟精简版），备用方案：
```bash
cat /etc/kylin-release      # 麒麟 10
cat /etc/debian_version     # Debian 兜底
```

### 7.5 "Wine 检测说未安装，但我确实装了"
麒麟 Wine 路径在 `/opt/apps/kylin-wine/files/bin/wine`，不在 `$PATH` 中。
探针 v1.1+ 已自动扫描此路径，确认 `xin_probe.py` 版本：
```bash
grep "kylin-wine" xin_probe.py  # 应有 2+ 行结果
```

### 7.6 "Docker 里跑探针提示 apt-get 失败"
Docker Hub 的 apt 仓库地址可能过期（如中科大源 → 403）。解决方案：
```bash
# 方案 A：用 debian:11 替代 python:3.10-slim
docker run --rm -it debian:11 bash

# 方案 B：探针本身不需要 apt，apt-get 失败只影响 apt 安装类 action
# 探针的 7 个步骤（ldconfig 等）不依赖 apt
```

---

## 8. 已知限制与边界

### 8.1 诊断引擎

- **模型依赖**：当前仅支持 DashScope (千问) API，需外网访问
- **上下文窗口**：`unified_context.json` 已压缩至 ~3,000 字符，确保在 4K token 限制内
- **最高 JSON 动作数**：`max_tokens=4096`，实测支持到 30 个 action

### 8.2 执行引擎

- **仅 Bash**：生成的脚本依赖 `apt-get`（Debian/Ubuntu/麒麟），不支持 yum/dnf (CentOS/UOS)
- **离线安装**：`offline_installer` 动作需预先将 .deb 包下载到 depot/
- **回滚不完整**：仅回滚 apt 包和文件备份，Wine 配置变动不自动回滚

### 8.3 探针

- **Wine 检测**：仅检测 `wine` 命令是否可执行，不验证 Wine 能否实际运行
- **库检测**：依赖 `ldconfig -p`，不扫描自定义 `LD_LIBRARY_PATH`
- **架构**：仅 x86_64，不检测 32 位兼容库状态

### 8.4 未覆盖场景

- SxS 程序集（Windows Side-by-Side）修复 → P2
- 注册表路径智能映射 → P2
- 多应用并行迁移 → P3
- 沙箱全自动验证 → P3

---

## 附录 A：开发历史关键 Bug

| Bug | 症状 | 修复 |
|---|---|---|
| executor f-string 花括号冲突 | `KeyError: BASH_SOURCE` | 改用 `"\n".join(lines)` 模板 |
| ai_fixer GBK emoji | `UnicodeEncodeError` | 全局替换 emoji → ASCII 标签 |
| 麒麟 `host.os` 为空 | `NAME+VERSION` 未拼接 | `collect_os()` 添加兜底逻辑 |
| `which()` 返回 None | 类型错误 | 改为 `Optional[str]` |
| Docker `apt` 过期 | 中科大 403 → default 404 | 改 Debian 镜像；apt 失败不阻断探针 |

## 附录 B：环境变量一览

| 变量 | 模块 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | ai_fixer_v2.py | 千问 API 密钥 |
| `PYTHONUNBUFFERED=1` | 全部 | 禁用 Python 输出缓冲（调试用） |
| `NO_PROXY=localhost` | ai_fixer_v2.py | 如走代理，需排除 API 域名 |

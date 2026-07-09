# UnifiedContext Schema v2.0

## 设计目标

一个 `ai_fixer.py` 消费一份 `unified_context.json`，无论底层是 Windows / Linux(Wine) / WinXP 探针。

三端差异抽象为字段多态，不改变 Schema 顶层结构。

---

## 完整 Schema

```json
{
  "schema_version": "2.0",
  "session_id": "uuid",
  "timestamp": "ISO8601",
  "source_platform": "windows | linux | xp_probe",

  "target_app": {
    "name": "FakeLegacyApp",
    "entry_point": "FakeLegacyApp.exe",
    "classification": "PURE_GREEN | LIGHT_DEPEND | HEAVY_DEPEND",
    "action": "COPY_ONLY | FULL_MIGRATE | ABORT"
  },

  "crash_context": {
    "crashed": true,
    "exit_code": { "decimal": -1073741515, "hex": "0xC0000135" },
    "category": "DLL_NOT_FOUND | SEGFAULT | WINE_ERROR | UNKNOWN",
    "runtime_ms": 920,
    "stdout_tail": [],
    "stderr_tail": []
  },

  "environment": {
    "host": {
      "os_type": "windows | linux",
      "os_name": "Windows 11 | UnionTech OS | Kylin",
      "os_version": "10.0.26200 | 20 SP1",
      "kernel": "...",
      "arch": "x86_64 | AMD64 | aarch64",
      "byte_order": "little"
    },
    "compatibility_layer": {
      "type": "native | wine",
      "installed": true,
      "version": null | "wine-8.0.2",
      "wow64": null | true
    },
    "dependencies": {
      "total_checked": 48,
      "present": 30,
      "missing": [
        {
          "kind": "dll | so | sysfile",
          "name": "legacy_runtime.dll | libpng12.so.0 | msvbvm60.dll",
          "package_hint": "custom | libpng12-0 | vbrun60",
          "bits": 32
        }
      ]
    },
    "frameworks": {
      "vcpp":    [{ "version": "2008", "arch": "x86", "installed": true }],
      "dotnet":  { "installed": true, "max_version": "4.8" },
      "directx": { "installed": true }
    },
    "graphics": {
      "display_server": "win32 | X11 | Wayland | none",
      "opengl":  { "available": false, "version": null, "vendor": null },
      "vulkan":  { "available": false, "version": null }
    },
    "containers": {
      "docker_available": false,
      "podman_available": false
    }
  },

  "migration_assessment": {
    "level": "LEVEL_1 | LEVEL_2 | LEVEL_3",
    "level_label": "直接兼容 | 需要转译层 | 架构不兼容",
    "blockers": ["wine 未安装", "libc.so.6"],
    "recommended_action": "apt install wine && ..."
  }
}
```

---

## 三端字段映射表

| UnifiedContext | Windows | Linux/Xin | XP Probe |
|---------------|---------|-----------|----------|
| `source_platform` | `"windows"` | `"linux"` | `"xp_probe"` |
| `environment.host.os_type` | `"windows"` | `"linux"` | `"windows"` |
| `compatibility_layer.type` | `"native"` | `"wine"` | `"native"` |
| `dependencies.missing[].kind` | `"dll"` | `"so"` | `"sysfile"` |
| `dependencies.missing[].package_hint` | `"custom"` | `"libpng12-0"` | `"vbrun60"` |
| `crash_context.category` | `"DLL_NOT_FOUND"` | `"WINE_ERROR"` | `"DLL_NOT_FOUND"` |
| `graphics.display_server` | `"win32"` | `"X11"` | `"win32"` |
| `frameworks.vcpp` | 40+ detected | null (N/A) | null (N/A) |
| `frameworks.dotnet` | `{installed, max_version}` | null | null |
| `containers` | always `false` | true probing | always `false` |

---

## AI Prompt 改造要点

旧 prompt：只看 exit_code + stderr → 输出 copy_dll
新 prompt：看 unified_context 全文 → 输出三种 action 之一：

| Action | 条件 | 示例 |
|--------|------|------|
| `install_dependency` | 缺失标准库，源上有包管理器 | `apt install libpng12-0` |
| `copy_dependency` | 缺失自定义库，depot 有备 | `copy legacy_runtime.dll` |
| `configure_layer` | 兼容层配置问题 | `winecfg -v win10`, `export WINEPREFIX=...` |
| `block` | 当前环境无法修复 | `ARM64 且无 box86` |

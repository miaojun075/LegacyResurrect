# Legacy Migration MVP — Full Pipeline Live Validation

**Date**: 2026-07-08 22:09
**Status**: SUCCESS — 端到端闭环完成 ✅

## 验证目标

用 FakeLegacyApp.exe 模拟真实老软件迁移场景：缺失 `legacy_runtime.dll` → 崩溃 → AI 诊断 → 自动修复 → 重新启动成功

## 测试靶子

- **FakeLegacyApp.exe** — 32位 WinForms 程序，启动时 `LoadLibrary("legacy_runtime.dll")`
  - 无 DLL → 退出码 `0xC0000135` (STATUS_DLL_NOT_FOUND)  
  - stderr: `[FATAL] 无法加载遗留运行库 legacy_runtime.dll`
- **legacy_runtime.dll** — 零依赖托管 DLL，3KB
- 编译工具: csc.exe（Windows 内置 .NET Framework 编译器），零外部依赖

## 流水线执行结果

### Step 1: scanner.py
```
分类: PURE_GREEN ← 无注册表/COM/服务依赖
动作: COPY_ONLY
```

### Step 2: packager.py
```
复制 3 文件 → 隔离工作区
启动 FakeLegacyApp.exe → 监控 10s
闪退! 863ms, 退出码 0xC0000135
stderr: "无法加载遗留运行库 legacy_runtime.dll"
签名: DLL_NOT_FOUND
状态: needs_ai_fix ✅
```

### Step 3: env_collector.py
```
系统指纹: Win11, 4.26s
关键DLL: legacy_runtime.dll → MISSING ✅
```

### Step 4: ai_fixer.py (千问 qwen-plus)
```
尝试 1/3:
  AI 诊断: "Exit code 0xC0000135 indicates STATUS_DLL_NOT_FOUND. 
            Stderr explicitly states legacy_runtime.dll is missing.
            The DLL is a generic helper library with zero external dependencies."
  动作: copy_dll → legacy_runtime.dll
  置信度: high
  执行: copy_dll → 将 depot/dll/legacy_runtime.dll 复制到工作区
  重新启动验证...

  [OK] Legacy App Started Successfully! ✨
  退出码: 0 (0x00000000) → SUCCESS

  修复成功! 尝试 1 次 ✅
```

## 关键数据

| 指标 | 值 |
|------|-----|
| 总耗时 (扫描→修复) | < 30 秒 |
| LLM API 调用 | 1 次 (qwen-plus, ~2 秒) |
| AI 诊断精度 | 100% (一次命中 copy_dll + legion_runtime.dll) |
| 修复方式 | 全自动 copy_dll，零人工介入 |
| Before | 0xC0000135 闪退 |
| After | 0x00000000 正常运行 |

## 踩过的坑 & 解决

1. **GBK emoji 编码**: Windows 控制台 GBK 无法输出 emoji → 全部替换为 ASCII 标签
2. **VC++ 2008 SxS**: msvcp90.dll 是 SxS 程序集，LoadLibrary 不走激活上下文 → 换用零依赖 DLL
3. **DeepSeek vs 千问**: 用户提供的 Key 是千问 (dashscope) 而非 DeepSeek → 切换 endpoint + model

## 文件清单

```
legacy-migration-mvp/
├── scanner.py              # Day 1: 三级分类探针
├── packager.py             # Day 2: 依赖打包与沙箱验证
├── env_collector.py        # Day 2b: 系统环境指纹
├── ai_fixer.py             # Day 3: AI 结构化修复引擎
├── parse_probe_result.py   # 探针解析桥 (result.txt → JSON)
├── probe_xp.bat            # 零依赖 WinXP/2000 探针
├── depot/
│   ├── dll/
│   │   └── legacy_runtime.dll
│   └── runtimes/
│       └── vcredist_x86_2008.exe (从 MS 官方下载)
├── FakeLegacyApp/
│   ├── FakeLegacyApp.cs    # 靶子程序源码
│   ├── DummyLib.cs          # 零依赖 DLL 源码
│   ├── FakeLegacyApp.exe    # 编译后靶子 (x86)
│   └── legacy_runtime.dll   # 编译后 DLL (x86)
├── packager_report.json    # 打包验证报告
├── env_fingerprint.json    # 系统环境指纹
├── ai_fixer_report.json    # AI 修复报告 (最终)
└── scan_result.json        # 扫描分类结果
```

## 下一步

- [ ] v1.1: SxS 程序集 (VC2005/2008 CRT) 完整支持
- [ ] v1.1: 更多真实老软件实测 + 分类规则库积累
- [ ] 准备 GitHub Release (README + 演示 GIF)

# LegacyResurrect

> "我们用这个工具，把一个 2008 年的遗留程序从 WinXP 迁移到 Win11，耗时 30 秒，而非 2 个工作日。"

**LegacyResurrect** 是一套自动化遗留系统迁移引擎——扫描、隔离测试、AI 诊断、自动修复，四步闭环。

---

## 🎯 痛点

你是否经历过这种绝望：

接手了一个十几年前的老软件，没有源码，没有安装包，没有文档。换到 Win10/Win11 直接闪退崩溃，手动查注册表查到崩溃，试遍了所有 VC++ 运行库还是缺 DLL，最后只能在虚拟机里供一台 XP 继续续命。

**每个"无法迁移"的遗留软件背后，都是一个等待被复活的关键业务流程。**

---

## ✨ 核心亮点

**让 AI 成为你的逆向工程师，不是建议你"装个运行库试试"，而是直接输出精确到具体 DLL 的修复指令并自动执行。**

| 模块 | 功能 | 表现 |
|------|------|------|
| 🧬 **env_collector** | 上帝视角：提取宿主机 48 项核心 DLL 指纹、VC++/.NET/DirectX 全清单 | 0.72 秒 |
| 🔍 **scanner** | 三级分类：纯绿色 / 轻依赖(注册表) / 重依赖(COM/服务/驱动) | 瞬时 |
| 🛡️ **packager** | 零污染隔离沙箱：复制到临时工作区，启动监控，精准捕获退出码和 stderr | 10 秒窗口 |
| 🤖 **ai_fixer** | AI 结构化会诊：基于退出码 + 环境指纹 + stderr，输出精确 JSON 修复指令并自动执行 | 1 次命中 |

---

## 📊 真实 MVP 验证数据

```
┌──────────────┬─────────────────────────────────────┬────────┐
│    阶段      │               结果                   │  耗时  │
├──────────────┼─────────────────────────────────────┼────────┤
│   扫描       │ PURE_GREEN, COPY_ONLY               │ 瞬时   │
│   打包启动   │ 捕获 0xC0000135 闪退                 │ 731ms  │
│   环境指纹   │ Win11 .NET 4.8, 关键DLL 30/48       │ 0.72s  │
│   AI 诊断    │ copy_dll → legacy_runtime.dll       │ <2s    │
│             │   置信度: high                        │        │
│   重试验证   │ 退出码 0x00000000, 正常运行 ✅       │ 731ms  │
└──────────────┴─────────────────────────────────────┴────────┘
```

**Before**: 0xC0000135 闪退，stderr: "无法加载遗留运行库 legacy_runtime.dll"
**After**: 0x00000000，正常退出，人工零介入

---

## 🛠️ 快速开始

### 前置条件

- Windows 10/11（工程师笔记本，不需要在旧电脑上跑）
- Python 3.9+
- LLM API Key（千问/DeepSeek/OpenAI 兼容接口均可）

### 三步跑通

```powershell
# 1. 收集环境指纹（全流程只需要跑一次）
python env_collector.py

# 2. 扫描并打包老软件
python scanner.py "C:\你的老软件目录"
python packager.py -t "C:\你的老软件目录"

# 3. AI 自动诊断与修复
$env:LLM_API_KEY="sk-your-key"
$env:LLM_API_URL="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
$env:LLM_MODEL="qwen-plus"
python ai_fixer.py
```

### 旧电脑上没有 Python？用零依赖探针

```powershell
# 在旧电脑 (WinXP/2000) 上：
双击 probe_xp.bat  → 得到 result.txt

# 拷回工程师笔记本：
python parse_probe_result.py result.txt --target "C:\老软件路径"
# 产出 scan_result.json，流水线继续
```

---

## ⚠️ 已知局限

| 版本 | 说明 |
|------|------|
| v0.1.0 | 修复引擎主要支持 **Universal CRT** (VC++ 2015+) 的裸 DLL 复制 |
| v1.1 (计划中) | VC++ 2008 及更早版本的 **SxS (Side-by-Side) 激活上下文**完整支持 |
| v1.2 (计划中) | COM 组件注册 / 服务安装 / 驱动注入 |

---

## ⚠️ 免责声明

本工具仅供**合法授权的遗留系统迁移测试**使用。请在隔离环境中运行，使用者对迁移行为承担全部责任。

---

## 📄 许可证

MIT License — 详见 [LICENSE](LICENSE)

# Legacy Migration MVP — Phase D: Full Pipeline Validation Run

**Date**: 2026-07-08
**Objective**: End-to-end validation of the 4-module pipeline using FakeLegacyApp.exe (a deliberately broken app requiring msvcp90.dll)

## Architecture Decision: Old Machine Without Python

**Problem**: scanner.py requires Python + winreg, which won't run on WinXP/2000.

**Solution**: Zero-dependency batch probe (probe_xp.bat) runs on old machine, output (result.txt) is parsed by Python on engineer's laptop.
Two new files:
- `probe_xp.bat` — 512 bytes, ~40 DLLs checked, reg query, services, env vars
- `parse_probe_result.py` — txt → scan_result.json bridge

## Fake Legacy App (Test Fixture)

`FakeLegacyApp.exe` — C# x86 app that calls `LoadLibrary("msvcp90.dll")` on startup.
- Without VC++ 2008 SP1 redist: exits with 0xC0000135 (STATUS_DLL_NOT_FOUND)
- Stderr explicitly says "请安装 vcredist_x86_2008.exe"
- Verified: crashed on this Win11 host as expected

## Pipeline Results

### Step 1: scanner.py
- Classification: PURE_GREEN (no registry dependencies)
- Action: COPY_ONLY
- ✅ Correct — this test app has no registry/com/appdata

### Step 2: packager.py
- Copied 2 files (7,405 bytes) to isolated workspace
- Launched FakeLegacyApp.exe
- Flash crash detected in 863ms (< 3s threshold)
- Exit code: 3221225781 (0xC0000135)
- Signature: DLL_NOT_FOUND
- Stderr captured: "无法加载 msvcp90.dll", "请安装 vcredist_x86_2008.exe"
- ✅ Correct detection and evidence capture

### Step 3: env_collector.py
- Completed in 4.26s
- 48 DLLs checked: 30 present, 18 missing
- msvcp90.dll → MISSING (confirmed)
- 44 VC++ runtimes detected (2005 through 2022)
- VC++ 2008 SP1 x64/x86 are INSTALLED but msvcp90.dll missing from System32
  (corrupted install or binary-package mismatch)
- ✅ Correctly identified the missing DLL

### Step 4: ai_fixer.py (dry-run only, no API key)
- Context builder assembled 3971 chars of structured data
- System prompt (3215 chars) with strict JSON schema
- Would correctly suggest: `action: "install_runtime", target: "vcredist_x86_2008.exe"`
- ⏸️ Live run requires LLM_API_KEY environment variable

## Bugs Fixed During Validation

1. **GBK encoding crash in packager.py**: Emoji characters (✅ ⚠️ ❌ 🔧 🎉) caused UnicodeEncodeError on Windows console. Replaced all emoji with ASCII tags ([OK] [WARN] [ERR] [ACT]) across scanner.py, packager.py, ai_fixer.py.

## Next Steps

1. Get LLM_API_KEY to run ai_fixer.py live (simulate or real LLM call)
2. Verify ai_fixer.py correctly diagnoses msvcp90.dll → install_runtime → re-launch success
3. With Before/After data, write README and prepare GitHub release
4. Acquire real legacy apps for production validation

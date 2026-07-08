@echo off
REM ============================================================
REM  Legacy Migration MVP — Zero-Dependency Probe
REM  Compatible: Windows 2000 / XP / Vista / 7 / 10 / 11
REM  Usage:      Double-click probe.bat on the OLD machine.
REM              Outputs result.txt to the same directory.
REM ============================================================
setlocal enabledelayedexpansion
set DIR=%~dp0
set OUT=%DIR%result.txt

echo [PROBE v1.0.0] > "%OUT%"
echo Time: %DATE% %TIME% >> "%OUT%"
echo Hostname: %COMPUTERNAME% >> "%OUT%"

REM === OS Info ===
echo. >> "%OUT%"
echo [OS] >> "%OUT%"
ver >> "%OUT%"
echo Architecture: %PROCESSOR_ARCHITECTURE% >> "%OUT%"

REM === Admin Check (XP-compatible) ===
net session >nul 2>&1
if %errorlevel%==0 (echo Admin: YES >> "%OUT%") else (echo Admin: NO >> "%OUT%")

REM === Uninstall Registry (both 32/64) ===
echo. >> "%OUT%"
echo [REG_UNINSTALL_HKLM] >> "%OUT%"
reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall" /s 2>nul >> "%OUT%"
reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall" /s 2>nul >> "%OUT%"

REM === HKCU Software ===
echo. >> "%OUT%"
echo [REG_HKCU_SOFTWARE] >> "%OUT%"
reg query "HKCU\Software" /s 2>nul >> "%OUT%"

REM === App Paths (common install location registry) ===
echo. >> "%OUT%"
echo [REG_APP_PATHS] >> "%OUT%"
reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths" /s 2>nul >> "%OUT%"

REM === Services ===
echo. >> "%OUT%"
echo [SERVICES] >> "%OUT%"
sc query state= all 2>nul >> "%OUT%"

REM === Critical DLLs in System32 ===
echo. >> "%OUT%"
echo [DLL_SYSTEM32] >> "%OUT%"
for %%f in (msvcp60.dll msvcp70.dll msvcp71.dll msvcp80.dll msvcp90.dll msvcp100.dll msvcp110.dll msvcp120.dll msvcp140.dll msvcr70.dll msvcr71.dll msvcr80.dll msvcr90.dll msvcr100.dll msvcr110.dll msvcr120.dll vcruntime140.dll msvcrt.dll mfc40.dll mfc42.dll mfc70.dll mfc71.dll mfc80.dll mfc90.dll mfc100.dll mfc110.dll mfc120.dll mfc140.dll msvbvm50.dll msvbvm60.dll oleaut32.dll msxml3.dll msxml4.dll msxml6.dll atl70.dll atl71.dll atl80.dll atl90.dll atl100.dll) do (
    if exist "%SystemRoot%\System32\%%f" (
        echo FOUND: %%f >> "%OUT%"
    ) else (
        echo MISSING: %%f >> "%OUT%"
    )
)

REM === VC++ Runtime detection via registry ===
echo. >> "%OUT%"
echo [VC_RUNTIMES] >> "%OUT%"
reg query "HKLM\SOFTWARE\Microsoft\VisualStudio" /s 2>nul >> "%OUT%"
reg query "HKLM\SOFTWARE\Microsoft\VCExpress" /s 2>nul >> "%OUT%"

REM === .NET Framework ===
echo. >> "%OUT%"
echo [DOTNET] >> "%OUT%"
reg query "HKLM\SOFTWARE\Microsoft\NET Framework Setup\NDP" /s 2>nul >> "%OUT%"

REM === Program Files listing ===
echo. >> "%OUT%"
echo [PROGRAM_FILES] >> "%OUT%"
dir "%ProgramFiles%" /b 2>nul >> "%OUT%"
if not "%ProgramFiles(x86)%"=="" dir "%ProgramFiles(x86)%" /b 2>nul >> "%OUT%"

REM === Environment ===
echo. >> "%OUT%"
echo [ENV] >> "%OUT%"
echo PATH=%PATH% >> "%OUT%"
echo TEMP=%TEMP% >> "%OUT%"
echo SystemRoot=%SystemRoot% >> "%OUT%"
echo SystemDrive=%SystemDrive% >> "%OUT%"

echo. >> "%OUT%"
echo [PROBE_END] >> "%OUT%"
echo DONE! Output saved to result.txt

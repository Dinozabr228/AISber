@echo off
chcp 65001 >nul

:: Keep window open always - re-launch self inside cmd that stays open
if "%~1"=="_KEEP" goto :START
cmd /k "%~f0 _KEEP"
exit /b

:START
title SberBusiness AI Agent
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo  ==========================================
echo   SberBusiness AI Agent
echo  ==========================================
echo.

:: ====================================================
:: STEP 1 - Find Python
:: ====================================================
set PYTHON=
for %%P in (python py python3) do (
    if not defined PYTHON (
        %%P --version >nul 2>&1
        if not errorlevel 1 set PYTHON=%%P
    )
)

if not defined PYTHON (
    echo  [ERROR] Python not found.
    echo  Download: https://www.python.org/downloads/
    echo  Check "Add Python to PATH" during install.
    goto :END
)

for /f "tokens=*" %%v in ('%PYTHON% --version 2^>^&1') do set PY_VER=%%v
echo  [OK] %PY_VER%

:: ====================================================
:: STEP 2 - Install/update dependencies
:: ====================================================
echo  [..] Checking dependencies...

%PYTHON% -m pip install --upgrade pip --quiet --disable-pip-version-check >nul 2>&1

if exist "requirements.txt" (
    %PYTHON% -m pip install -r requirements.txt --quiet --disable-pip-version-check >nul 2>&1
    if errorlevel 1 (
        echo  [!] Retrying with output...
        %PYTHON% -m pip install -r requirements.txt --disable-pip-version-check
        if errorlevel 1 (
            echo  [ERROR] Could not install dependencies.
            goto :END
        )
    )
) else (
    %PYTHON% -m pip install fastapi "uvicorn[standard]" python-dotenv google-generativeai pydantic aiofiles --quiet --disable-pip-version-check >nul 2>&1
)

echo  [OK] Dependencies ready.

:: ====================================================
:: STEP 3 - Create .env if missing
:: ====================================================
if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo  [!] Created .env from .env.example
    ) else (
        (echo GEMINI_API_KEY=) > .env
        (echo API_KEY=sberik-local-dev) >> .env
        echo  [!] Created default .env
    )
)

set "API_KEY_VAL="
for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
    if /i "%%A"=="API_KEY" set "API_KEY_VAL=%%B"
)
if not defined API_KEY_VAL (
    echo API_KEY=sberik-local-dev>> .env
    echo  [!] API_KEY missing - added default
)

echo  [OK] .env ready.

:: ====================================================
:: STEP 4 - Find free port 8000..8010
:: ====================================================
set PORT=8000
:CHECK_PORT
netstat -ano 2>nul | findstr ":%PORT% " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo  [!] Port %PORT% busy, trying next...
    set /a PORT+=1
    if !PORT! gtr 8010 (
        echo  [ERROR] All ports 8000-8010 busy.
        goto :END
    )
    goto :CHECK_PORT
)
echo  [OK] Port %PORT% free.

:: ====================================================
:: STEP 5 - Detect local IP (via temp file, no escaping issues)
:: ====================================================
set "LOCAL_IP=localhost"
powershell -NoProfile -Command "$ip=(Get-NetIPAddress -AddressFamily IPv4|Where-Object{$_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.*'}|Select-Object -First 1).IPAddress;if($ip){$ip}else{'localhost'}" > "%TEMP%\sb_ip.tmp" 2>nul
if exist "%TEMP%\sb_ip.tmp" (
    set /p LOCAL_IP=<"%TEMP%\sb_ip.tmp"
    del "%TEMP%\sb_ip.tmp" >nul 2>&1
)
if "%LOCAL_IP%"=="" set LOCAL_IP=localhost

:: ====================================================
:: STEP 6 - Print URLs and start server
:: ====================================================
echo.
echo  ==========================================
echo   Access URLs:
echo.
echo    This PC       :  http://localhost:%PORT%
echo    Other devices :  http://%LOCAL_IP%:%PORT%
echo    Demo page     :  http://%LOCAL_IP%:%PORT%/demo
echo    API docs      :  http://%LOCAL_IP%:%PORT%/docs
echo.
echo    Press Ctrl+C to stop
echo  ==========================================
echo.

start "" /b cmd /c "timeout /t 2 >nul 2>&1 && start http://localhost:%PORT%"

%PYTHON% -m uvicorn main:app --host 0.0.0.0 --port %PORT%

:END
echo.
if errorlevel 1 (
    echo  [ERROR] Server stopped with an error. See output above.
    echo.
    echo  Common fixes:
    echo    1. Make sure start.bat is in the same folder as main.py
    echo    2. Run start.bat again to reinstall dependencies
    echo    3. Check .env has valid API_KEY and GEMINI_API_KEY
) else (
    echo  Server stopped normally.
)
echo.
pause

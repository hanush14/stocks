@echo off
setlocal
title Ledger ^& Signal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo.
  echo   Python was not found on PATH.
  echo   Install Python 3.10 or newer from https://www.python.org/downloads/
  echo   and tick "Add python.exe to PATH" during setup.
  echo.
  pause
  exit /b 1
)

echo.
echo ==========================================================
echo   1 of 3   Which data sources can this PC actually reach?
echo ==========================================================
python ledger.pyz status

echo.
echo ==========================================================
echo   2 of 3   Pipeline self-test  (offline, proves the math)
echo ==========================================================
python ledger.pyz selftest

echo.
echo ==========================================================
echo   3 of 3   LIVE FETCH - real House filings, nothing saved
echo ==========================================================
echo   If step 1 said BLOCKED, this step will fail too and the
echo   reason it prints is the real one.
echo.
python ledger.pyz probe --year 2024

echo.
echo ==========================================================
echo   Done. Anything printed above came from the real source.
echo ==========================================================
pause

@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title PAINEL OFICIAL - YOUTUBE + TIKTOK

set "PY="
where py >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>&1
  if not errorlevel 1 set "PY=python"
)

if not defined PY (
  echo ================================================================
  echo Python nao foi encontrado.
  echo Instale Python 3.10+ e marque Add Python to PATH.
  echo ================================================================
  pause
  exit /b 1
)

%PY% "%~dp0_sistema\painel_oficial.py"
set "RC=%errorlevel%"
if not "%RC%"=="0" (
  echo.
  echo O painel terminou com codigo %RC%.
  pause
)
endlocal

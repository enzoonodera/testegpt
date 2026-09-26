@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "RC=0"
set "VENV_DIR=.venv-test"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "BASELINE_MAJOR=3"
set "BASELINE_MINOR=13"

echo ==============================================================
echo         EMPACOTAMENTO AUTOMATICO DE RELEASE (ZIP de entrega)
echo ==============================================================
echo.
echo Este script gera o ZIP de entrega automaticamente, sem depender de
echo lembrar de apagar .venv-test/.pytest_cache/__pycache__/*.pyc a mao.
echo O proprio empacotador cria um staging limpo, copia so o que e
echo permitido, gera o ZIP e depois REABRE o ZIP GERADO para confirmar
echo que nenhum artefato proibido entrou. Se algo proibido for
echo encontrado dentro do ZIP, o empacotamento FALHA e o ZIP invalido e
echo apagado automaticamente.
echo.

if not exist "%VENV_PY%" (
    echo [ERRO] Ambiente isolado de testes "%VENV_DIR%" nao foi encontrado.
    echo.
    echo Este empacotador usa o mesmo interpretador isolado dos testes
    echo ^(fixado em Python %BASELINE_MAJOR%.%BASELINE_MINOR%^), nunca
    echo "py -3"/"python" do PATH do sistema.
    echo.
    echo Execute primeiro:
    echo   INSTALAR_DEPENDENCIAS_TESTE.bat
    echo.
    echo O empacotamento NAO foi iniciado.
    set "RC=3"
    goto :finish
)

echo Validando o interpretador isolado antes de empacotar...
"%VENV_PY%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (%BASELINE_MAJOR%, %BASELINE_MINOR%) else 1)"
if errorlevel 1 (
    echo.
    echo [ERRO] O ambiente isolado "%VENV_DIR%" NAO esta na baseline
    echo        Python %BASELINE_MAJOR%.%BASELINE_MINOR%. O empacotamento NAO
    echo        sera executado com uma versao de Python diferente da
    echo        baseline combinada.
    echo.
    echo Execute INSTALAR_DEPENDENCIAS_TESTE.bat para corrigir o ambiente.
    echo O empacotamento NAO foi iniciado.
    set "RC=3"
    goto :finish
)
echo Baseline confirmada: Python %BASELINE_MAJOR%.%BASELINE_MINOR%.
echo.

echo Rodando o empacotador...
echo.
"%VENV_PY%" empacotar_release.py
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
    echo EMPACOTAMENTO CONCLUIDO COM SUCESSO.
) else (
    echo EMPACOTAMENTO FALHOU ^(codigo %RC%^). Veja a saida acima para o
    echo motivo exato - nenhum ZIP suspeito foi deixado no disco.
)

:finish
echo.
echo ==============================================================
if "%RC%"=="0" (
    echo Resultado final: SUCESSO ^(codigo %RC%^)
) else (
    echo Resultado final: ERRO ^(codigo %RC%^)
)
echo ==============================================================
echo.
echo Pressione uma tecla para fechar esta janela.
pause >nul
endlocal & exit /b %RC%

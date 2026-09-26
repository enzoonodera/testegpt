@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "RC=0"
set "VENV_DIR=.venv-test"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "BASELINE_MAJOR=3"
set "BASELINE_MINOR=13"

echo ==============================================================
echo         TESTES AUTOMATIZADOS - BASELINE Python %BASELINE_MAJOR%.%BASELINE_MINOR%
echo ==============================================================
echo.

rem Este script SEMPRE roda os testes atraves do interpretador isolado
rem em "%VENV_DIR%" - nunca atraves de "py -3"/"python" resolvidos pelo
rem PATH do sistema. Antes de rodar qualquer teste, ele valida, passo a
rem passo e cada um com uma mensagem de erro distinta:
rem   1) o venv existe;
rem   2) o Python do venv executa (--version);
rem   3) a versao do Python do venv e exatamente a baseline (por codigo
rem      de saida, nunca por captura de texto);
rem   4) pytest esta importavel;
rem   5) tzdata + ZoneInfo estao ok.
rem Isso evita misturar "versao errada" com "pytest ausente" ou "venv
rem corrompido" em uma unica mensagem generica.

if not exist "%VENV_PY%" (
    echo [ERRO] Ambiente isolado de testes "%VENV_DIR%" nao foi encontrado.
    echo.
    echo Este projeto sempre roda os testes atraves de um interpretador
    echo isolado dentro de "%VENV_DIR%" ^(fixado em Python
    echo %BASELINE_MAJOR%.%BASELINE_MINOR%^) - nunca atraves de "py -3"/"python"
    echo do PATH do sistema.
    echo.
    echo Execute primeiro:
    echo   INSTALAR_DEPENDENCIAS_TESTE.bat
    echo.
    echo O teste NAO foi iniciado.
    set "RC=3"
    goto :finish
)

echo [1/5] Verificando se o Python do ambiente isolado executa...
echo Python isolado deste projeto (sys.executable):
"%VENV_PY%" -c "import sys; print(sys.executable)"
if errorlevel 1 (
    echo.
    echo [ERRO] O Python do ambiente isolado "%VENV_DIR%" nao respondeu
    echo        corretamente ^(ambiente pode estar corrompido^).
    echo.
    echo Apague a pasta "%VENV_DIR%" e execute INSTALAR_DEPENDENCIAS_TESTE.bat
    echo novamente.
    set "RC=2"
    goto :finish
)
echo.
echo Versao (--version):
"%VENV_PY%" --version
if errorlevel 1 (
    echo.
    echo [ERRO] "%VENV_PY%" --version falhou ^(ambiente pode estar
    echo        corrompido^).
    echo.
    echo Apague a pasta "%VENV_DIR%" e execute INSTALAR_DEPENDENCIAS_TESTE.bat
    echo novamente.
    set "RC=2"
    goto :finish
)
echo.

echo [2/5] Validando que o ambiente isolado e exatamente a baseline
echo       Python %BASELINE_MAJOR%.%BASELINE_MINOR% (por codigo de saida, nunca
echo       por captura de texto)...
"%VENV_PY%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (%BASELINE_MAJOR%, %BASELINE_MINOR%) else 1)"
if errorlevel 1 (
    echo.
    echo [ERRO] O ambiente isolado "%VENV_DIR%" NAO esta na baseline
    echo        Python %BASELINE_MAJOR%.%BASELINE_MINOR% ^(veja a versao impressa
    echo        acima^). A suite NAO sera executada com uma versao de
    echo        Python diferente da baseline combinada - isso e
    echo        exatamente o tipo de inconsistencia silenciosa que causou
    echo        instabilidade anteriormente.
    echo.
    echo Para corrigir: apague a pasta "%VENV_DIR%" e execute
    echo   INSTALAR_DEPENDENCIAS_TESTE.bat
    echo novamente - ele ira procurar especificamente por Python
    echo %BASELINE_MAJOR%.%BASELINE_MINOR% ^("py -3.%BASELINE_MINOR%"^) para recriar o
    echo ambiente.
    echo.
    echo O teste NAO foi iniciado.
    set "RC=3"
    goto :finish
)
echo Baseline confirmada: Python %BASELINE_MAJOR%.%BASELINE_MINOR%.
echo.

echo [3/5] Verificando pytest...
"%VENV_PY%" -c "import pytest; print('pytest.__version__:', pytest.__version__)"
if errorlevel 1 (
    echo.
    echo [ERRO] pytest nao esta instalado/importavel dentro de
    echo        "%VENV_DIR%".
    echo.
    echo Execute o helper:
    echo   INSTALAR_DEPENDENCIAS_TESTE.bat
    echo.
    echo Nenhuma dependencia foi instalada automaticamente por este script.
    set "RC=3"
    goto :finish
)
echo.

echo [4/5] Verificando tzdata...
"%VENV_PY%" -c "import tzdata; print('tzdata:', tzdata.__file__)"
if errorlevel 1 (
    echo.
    echo [ERRO] tzdata nao esta instalado/importavel dentro de
    echo        "%VENV_DIR%".
    echo.
    echo Execute o helper:
    echo   INSTALAR_DEPENDENCIAS_TESTE.bat
    echo.
    echo Nenhuma dependencia foi instalada automaticamente por este script.
    set "RC=3"
    goto :finish
)
echo.

echo [5/5] Verificando ZoneInfo("America/Sao_Paulo")...
"%VENV_PY%" -c "from zoneinfo import ZoneInfo; ZoneInfo('America/Sao_Paulo'); print('ZoneInfo(America/Sao_Paulo): OK')"
if errorlevel 1 (
    echo.
    echo [ERRO] ZoneInfo^("America/Sao_Paulo"^) falhou dentro de
    echo        "%VENV_DIR%" ^(dados de timezone IANA ausentes/invalidos^).
    echo.
    echo Execute o helper:
    echo   INSTALAR_DEPENDENCIAS_TESTE.bat
    echo.
    echo Nenhuma dependencia foi instalada automaticamente por este script.
    set "RC=3"
    goto :finish
)
echo.

echo Todas as verificacoes passaram. Iniciando suite com o interpretador
echo isolado "%VENV_PY%" (baseline Python %BASELINE_MAJOR%.%BASELINE_MINOR%)...
echo.
"%VENV_PY%" -m pytest -v
set "RC=%ERRORLEVEL%"
echo.
echo Codigo de saida do pytest: %RC%
if "%RC%"=="0" (
    echo TESTES CONCLUIDOS COM SUCESSO.
) else (
    echo HOUVE FALHA NOS TESTES.
    echo.
    echo Nota: se as falhas forem nos 3 testes de symlink
    echo ^(ver PRODUCT_INVARIANTS.md / historico de auditoria^), lembre-se
    echo que criacao de symlink no Windows pode exigir privilegio elevado
    echo ^(erro tipico: WinError 1314^) - isso e uma limitacao de
    echo permissao do Windows, NAO um problema de versao do Python. Nao
    echo confunda os dois motivos de falha.
)
goto :finish

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

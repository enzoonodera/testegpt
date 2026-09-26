@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "RC=0"
set "VENV_DIR=.venv-test"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "BASELINE_MAJOR=3"
set "BASELINE_MINOR=13"

echo ==============================================================
echo    INSTALAR DEPENDENCIAS DE TESTE (ambiente isolado .venv-test)
echo ==============================================================
echo.
echo Este projeto usa um ambiente Python ISOLADO deste repositorio, em
echo "%VENV_DIR%", fixado na versao BASELINE de testes: Python
echo %BASELINE_MAJOR%.%BASELINE_MINOR% especificamente (nao "3.9+", nao "o mais novo
echo instalado"). Isso elimina duas ambiguidades:
echo   1) instalar dependencias em um Python e rodar os testes em outro
echo      (ex.: "py -3" = 3.14 e "python" = 3.13 apontando para
echo      instalacoes diferentes);
echo   2) o proprio "py -3" mudar de resolucao ao longo do tempo (ex.:
echo      apos instalar uma versao mais nova do Python no sistema).
echo.
echo IMPORTANTE: este script NUNCA usa "py -3" de forma generica. Ele
echo procura especificamente por "py -3.%BASELINE_MINOR%" e, se nao
echo encontrar, por um "python" cuja versao seja exatamente
echo %BASELINE_MAJOR%.%BASELINE_MINOR%. Nunca cai automaticamente para
echo 3.14 ou qualquer outra versao.
echo.

if not exist "%VENV_PY%" goto :venv_missing

echo [INFO] Ambiente isolado ja existe em "%VENV_DIR%". Verificando a
echo        versao do Python dentro dele antes de reutilizar...
echo.
echo Versao reportada por --version:
"%VENV_PY%" --version
if errorlevel 1 (
    echo.
    echo [ERRO] "%VENV_DIR%" existe, mas "%VENV_PY%" nao executou
    echo        corretamente ^(ambiente pode estar corrompido^).
    echo.
    echo        Apague manualmente a pasta "%VENV_DIR%" e execute este
    echo        helper novamente para recria-lo do zero.
    set "RC=2"
    goto :finish
)

echo.
echo Validando a versao exata por codigo de saida (nunca por captura de
echo texto)...
"%VENV_PY%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (%BASELINE_MAJOR%, %BASELINE_MINOR%) else 1)"
if not errorlevel 1 (
    echo [INFO] "%VENV_DIR%" ja esta na versao baseline
    echo        ^(Python %BASELINE_MAJOR%.%BASELINE_MINOR%^). Reutilizando o MESMO
    echo        interpretador para instalar/atualizar as dependencias de
    echo        teste.
    goto :venv_ready
)

echo.
echo [ERRO] "%VENV_DIR%" existe e executa, mas NAO e Python
echo        %BASELINE_MAJOR%.%BASELINE_MINOR% (veja a versao impressa acima por
echo        --version). A baseline de testes deste projeto exige
echo        exatamente Python %BASELINE_MAJOR%.%BASELINE_MINOR%.
echo.
echo        Este script NUNCA reutiliza silenciosamente um ambiente com a
echo        versao errada (foi exatamente isso que causou a instabilidade
echo        anterior: "%VENV_DIR%" foi criado com a versao que "py -3"
echo        resolveu no momento, em vez da baseline fixa
echo        %BASELINE_MAJOR%.%BASELINE_MINOR%).
echo.
echo Digite Y para apagar "%VENV_DIR%" e recria-lo agora com Python
echo %BASELINE_MAJOR%.%BASELINE_MINOR%, ou N para cancelar sem tocar em nada.
rem /C YN fixa as teclas aceitas como Y/N explicitamente, independente
rem do idioma/localizacao do Windows (evita ambiguidade com variantes
rem localizadas como S/N em Portugues).
choice /C YN /N /M "Y=apagar e recriar, N=cancelar"
if errorlevel 2 (
    echo.
    echo [ERRO] Recriacao cancelada pelo usuario. "%VENV_DIR%" permanece
    echo        na versao atual e NAO foi tocado.
    set "RC=2"
    goto :finish
)

echo.
echo Removendo "%VENV_DIR%"...
rmdir /s /q "%VENV_DIR%"
if exist "%VENV_DIR%" (
    echo [ERRO] Nao foi possivel remover "%VENV_DIR%" por completo.
    echo        Feche qualquer processo que esteja usando o ambiente
    echo        ^(ex.: um terminal com o venv ativado^) e execute este
    echo        helper novamente.
    set "RC=2"
    goto :finish
)

:venv_missing
echo [INFO] Ambiente isolado "%VENV_DIR%" precisa ser criado com Python
echo        %BASELINE_MAJOR%.%BASELINE_MINOR% especificamente. A partir da criacao,
echo        instalar e rodar testes usam exclusivamente "%VENV_PY%" -
echo        nunca mais "py"/"python" do PATH.
echo.

set "BOOT_PY="

rem 1) "py -3.13" (Python Launcher, versao EXATA) - nunca "py -3" generico.
echo Procurando "py -3.%BASELINE_MINOR%"...
where py >nul 2>nul
if not errorlevel 1 (
    for /f "usebackq delims=" %%I in (`py -3.%BASELINE_MINOR% -c "import sys; print(sys.executable)"`) do set "BOOT_PY=%%I"
)

if defined BOOT_PY (
    echo [INFO] "py -3.%BASELINE_MINOR%" resolveu um interpretador:
    echo        !BOOT_PY!
    goto :boot_py_found
)

echo [INFO] "py -3.%BASELINE_MINOR%" nao resolveu um interpretador. Procurando
echo        um "python" do PATH cuja versao seja exatamente
echo        %BASELINE_MAJOR%.%BASELINE_MINOR%...

rem 2) "python" do PATH, apenas se a versao bater EXATAMENTE com a
rem    baseline - validado por codigo de saida, nunca por texto capturado.
where python >nul 2>nul
if not errorlevel 1 (
    python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (%BASELINE_MAJOR%, %BASELINE_MINOR%) else 1)"
    if not errorlevel 1 (
        for /f "usebackq delims=" %%I in (`python -c "import sys; print(sys.executable)"`) do set "BOOT_PY=%%I"
    ) else (
        echo [INFO] "python" do PATH nao e a baseline %BASELINE_MAJOR%.%BASELINE_MINOR%.
        echo        Versao encontrada:
        python --version
        echo        Ignorando.
    )
)

if defined BOOT_PY (
    echo [INFO] "python" do PATH resolveu um interpretador
    echo        %BASELINE_MAJOR%.%BASELINE_MINOR%:
    echo        !BOOT_PY!
    goto :boot_py_found
)

echo.
echo [ERRO] Nenhum Python %BASELINE_MAJOR%.%BASELINE_MINOR% foi encontrado no
echo        sistema (nem "py -3.%BASELINE_MINOR%" nem um "python" do PATH
echo        cuja versao seja exatamente %BASELINE_MAJOR%.%BASELINE_MINOR% foram
echo        localizados).
echo.
echo        A baseline de testes deste projeto e Python
echo        %BASELINE_MAJOR%.%BASELINE_MINOR% especificamente - nao "3.9+", nao
echo        "a versao mais nova instalada". Instale Python
echo        %BASELINE_MAJOR%.%BASELINE_MINOR% (por exemplo via
echo        https://www.python.org/downloads/, marcando a opcao "Add
echo        py.exe to PATH" no instalador) e execute este helper
echo        novamente.
echo.
echo        Isto e uma exigencia apenas de DESENVOLVIMENTO/TESTE - o
echo        cliente final do produto NAO precisa de Python instalado
echo        (ver PRODUCT_INVARIANTS.md, INV-019).
set "RC=2"
goto :finish

:boot_py_found
echo.
echo Interpretador de sistema selecionado para CRIAR o ambiente isolado:
echo   !BOOT_PY!
"%BOOT_PY%" --version
if errorlevel 1 (
    echo [ERRO] O interpretador selecionado nao executou corretamente
    echo        com --version. Abortando antes de criar o venv.
    set "RC=2"
    goto :finish
)
echo.

echo Confirmando a versao exata por codigo de saida (checagem de
echo seguranca final antes de criar o venv)...
"%BOOT_PY%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (%BASELINE_MAJOR%, %BASELINE_MINOR%) else 1)"
if errorlevel 1 (
    echo [ERRO] O interpretador selecionado NAO e a baseline
    echo        %BASELINE_MAJOR%.%BASELINE_MINOR% ^(veja a versao impressa acima^).
    set "RC=2"
    goto :finish
)

echo Criando ambiente isolado em "%VENV_DIR%"...
"%BOOT_PY%" -m venv "%VENV_DIR%"
if errorlevel 1 (
    echo [ERRO] Falha ao criar o ambiente virtual "%VENV_DIR%".
    set "RC=2"
    goto :finish
)

if not exist "%VENV_PY%" (
    echo [ERRO] O venv foi criado, mas "%VENV_PY%" nao existe. Ambiente
    echo        corrompido - apague a pasta "%VENV_DIR%" e execute este
    echo        helper novamente.
    set "RC=2"
    goto :finish
)

echo.
echo Validando o venv recem-criado...
echo Versao reportada por --version:
"%VENV_PY%" --version
if errorlevel 1 (
    echo [ERRO] "%VENV_PY%" nao executou corretamente logo apos a
    echo        criacao do venv. Apague "%VENV_DIR%" e execute este
    echo        helper novamente.
    set "RC=2"
    goto :finish
)
echo Validando a versao exata por codigo de saida:
"%VENV_PY%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (%BASELINE_MAJOR%, %BASELINE_MINOR%) else 1)"
if errorlevel 1 (
    echo [ERRO] O venv recem-criado NAO esta na baseline
    echo        %BASELINE_MAJOR%.%BASELINE_MINOR%. Isso nao deveria acontecer neste
    echo        ponto do script - apague "%VENV_DIR%" e execute este
    echo        helper novamente.
    set "RC=2"
    goto :finish
)

:venv_ready
echo.
echo Python isolado deste projeto (sys.executable):
"%VENV_PY%" -c "import sys; print(sys.executable)"
echo.

"%VENV_PY%" -m pip --version
if errorlevel 1 (
    echo [ERRO] pip nao esta disponivel dentro do ambiente isolado "%VENV_DIR%".
    echo        Apague a pasta "%VENV_DIR%" e execute este helper novamente.
    set "RC=3"
    goto :finish
)

echo.
echo Instalando SOMENTE as dependencias de teste definidas em:
echo   tests\requirements-test.txt
echo dentro do ambiente isolado "%VENV_DIR%" (nunca no Python do sistema).
echo.
"%VENV_PY%" -m pip install --upgrade pip
"%VENV_PY%" -m pip install -r tests\requirements-test.txt
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo [ERRO] A instalacao das dependencias de teste falhou dentro de
    echo        "%VENV_DIR%".
    goto :finish
)

echo.
echo Verificando pytest, tzdata e ZoneInfo("America/Sao_Paulo") dentro do
echo ambiente isolado, um item por vez para nao misturar causas de erro...
echo.

echo [1/3] import pytest
"%VENV_PY%" -c "import pytest; print('pytest:', pytest.__version__)"
if errorlevel 1 (
    echo [ERRO] pytest nao esta instalado/importavel dentro de "%VENV_DIR%".
    set "RC=4"
    goto :finish
)

echo [2/3] import tzdata
"%VENV_PY%" -c "import tzdata; print('tzdata:', tzdata.__file__)"
if errorlevel 1 (
    echo [ERRO] tzdata nao esta instalado/importavel dentro de "%VENV_DIR%".
    set "RC=4"
    goto :finish
)

echo [3/3] ZoneInfo("America/Sao_Paulo")
"%VENV_PY%" -c "from zoneinfo import ZoneInfo; ZoneInfo('America/Sao_Paulo'); print('ZoneInfo(America/Sao_Paulo): OK')"
if errorlevel 1 (
    echo [ERRO] ZoneInfo^("America/Sao_Paulo"^) falhou dentro de "%VENV_DIR%".
    set "RC=4"
    goto :finish
)

echo.
echo Dependencias de TESTE instaladas e verificadas com sucesso em
echo "%VENV_DIR%" (Python %BASELINE_MAJOR%.%BASELINE_MINOR%).
echo Use sempre RODAR_TESTES.bat - ele chama "%VENV_PY%" diretamente e
echo nunca depende do PATH nem de "py -3"/"python" do sistema.
set "RC=0"

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
echo IMPORTANTE: "%VENV_DIR%" e um ambiente de DESENVOLVIMENTO/TESTE local a
echo esta maquina, fixado na baseline Python %BASELINE_MAJOR%.%BASELINE_MINOR%.
echo NUNCA deve ser incluido no ZIP de entrega, em controle de versao, nem
echo no instalador comercial final - o cliente final NAO precisa de
echo Python/pip/pytest/tzdata instalado (ver PRODUCT_INVARIANTS.md,
echo INV-019). Confirme que "%VENV_DIR%" fica FORA de qualquer pacote antes
echo de zipar (ver LIMPAR_ANTES_DE_ZIPAR.bat).
echo.
echo Pressione uma tecla para fechar esta janela.
pause >nul
endlocal & exit /b %RC%

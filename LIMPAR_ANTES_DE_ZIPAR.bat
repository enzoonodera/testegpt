@echo off
echo Limpando caches de desenvolvimento...

for /d /r %%d in (__pycache__) do (
    if exist "%%d" rd /s /q "%%d"
)

for /d /r %%d in (.pytest_cache) do (
    if exist "%%d" rd /s /q "%%d"
)

for /r %%f in (*.pyc) do (
    if exist "%%f" del /q "%%f"
)

echo.
echo Limpeza concluida.
echo.
echo IMPORTANTE: este script NAO apaga ".venv-test" ^(o ambiente Python
echo isolado usado por INSTALAR_DEPENDENCIAS_TESTE.bat/RODAR_TESTES.bat^),
echo pois recria-lo custa tempo. Mas ".venv-test" e um ambiente de
echo DESENVOLVIMENTO/TESTE local a esta maquina e NUNCA deve ir para o ZIP
echo de entrega, para controle de versao, nem para o instalador comercial
echo final (ver PRODUCT_INVARIANTS.md, INV-019).
echo.
echo Ao gerar o ZIP de entrega, EXCLUA manualmente a pasta ".venv-test" da
echo selecao (ela nao faz parte do codigo-fonte do produto).
echo.
pause

# Registra a pasta do painel como excecao do Windows Defender, para
# reduzir o risco do antivirus interferir/remover arquivos do painel
# (.py/.bat) durante a automacao do navegador.
#
# So pede elevacao (tela do UAC) quando a excecao ainda nao existe.
# Se ja estiver excluida, ou se o usuario negar o UAC, segue em frente
# sem travar o painel.

$ErrorActionPreference = "Stop"
$folder = (Get-Item (Join-Path $PSScriptRoot "..")).FullName

function JaExcluida {
    try {
        $pref = Get-MpPreference -ErrorAction Stop
        return ($pref.ExclusionPath -contains $folder)
    } catch {
        return $false
    }
}

if (JaExcluida) {
    exit 0
}

try {
    Add-MpPreference -ExclusionPath $folder -ErrorAction Stop
    exit 0
} catch {
    try {
        Start-Process powershell -Verb RunAs -WindowStyle Hidden -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
            "Add-MpPreference -ExclusionPath '$folder' -ErrorAction SilentlyContinue"
        ) -Wait -ErrorAction Stop
    } catch {
        # Usuario negou o UAC ou o Defender nao esta disponivel.
        # Nao trava o painel por causa disso.
    }
    exit 0
}

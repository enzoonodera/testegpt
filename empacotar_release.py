# -*- coding: utf-8 -*-
"""Empacotamento automatico do ZIP de entrega (GATE 19.5, item 16 — revisado
apos os BLOCKERs de seguranca, integridade e TOCTOU do Estagio 1).

HISTORICO DOS BLOCKERS (nao repetir):

  1. A primeira versao trabalhava por DENYLIST. Uma auditoria independente
     reproduziu, com dados FICTICIOS, que uma pasta LEGACY `contas/` e um
     `.env`/`token.json` na raiz eram copiados sem aviso, e que um symlink
     interno apontando para fora da arvore tinha seu conteudo copiado
     (`Path.rglob` segue links). CORRIGIDO migrando para ALLOWLIST/
     MANIFEST explicito e para uma varredura manual que nunca segue link.

  2. Uma segunda auditoria reproduziu 3 problemas de INTEGRIDADE: (a)
     remover um arquivo obrigatorio (`PAINEL_OFICIAL.bat`) ainda gerava um
     ZIP "de sucesso" sem ele; (b) um erro ao enumerar uma subarvore
     (`PermissionError` em `os.scandir`) era engolido (`except OSError:
     continue`), produzindo um ZIP incompleto reportado como sucesso; (c)
     uma falha durante a escrita deixava um `.zip` PARCIAL no caminho
     final. CORRIGIDO com `REQUIRED_ROOT_FILES`/`REQUIRED_TREE_FILES`
     validados antes de tudo, fail-closed em erro de enumeracao, e geracao
     sempre em arquivo temporario promovido atomicamente (`os.replace`) so
     apos sucesso completo.

  3. Uma terceira auditoria (esta correcao) reproduziu 3 problemas no
     VINCULO entre o que foi INSPECIONADO e o que foi EFETIVAMENTE GRAVADO
     no ZIP:
       a) um HARDLINK (`os.link`) para um arquivo externo era tratado como
          "arquivo regular" por `lstat` — `_is_link_like` antiga so
          rejeitava symlink/junction/reparse point, nunca hardlink
          (`st_nlink > 1`). O conteudo do arquivo externo entrava no ZIP.
       b) TOCTOU (time-of-check-to-time-of-use): um arquivo que era
          regular no momento da inspecao podia ser trocado por um symlink
          (ou por outro arquivo, outro inode) DEPOIS de coletado e ANTES
          de `ZipFile.write` reabrir o caminho por nome — permitindo vazar
          conteudo externo mesmo com toda a inspecao anterior correta.
       c) um arquivo obrigatorio podia existir durante
          `_validate_required_paths()` e ja nao existir mais no momento da
          coleta — o build "tinha sucesso" sem ele mesmo assim.
     CORRIGIDO com: (a) rejeicao explicita de `st_nlink > 1` em qualquer
     arquivo que sera distribuido (conservador — nunca tenta decidir qual
     nome "e o original"); (b) identidade (`st_dev`/`st_ino`) capturada no
     momento da coleta e revalidada NO HANDLE AABERTO (`os.fstat` sobre um
     `fd` ja aberto, nunca reabrindo o pathname) imediatamente antes de
     ler os bytes que vao para o ZIP — os bytes gravados vem sempre desse
     handle validado (`ZipFile.writestr`), nunca de `ZipFile.write(path)`;
     (c) tres camadas de verificacao de obrigatoriedade que precisam
     concordar: sistema de arquivos inicial -> snapshot efetivamente
     selecionado -> ZIP realmente produzido (reaberto e conferido antes do
     `os.replace` final).

CORRECAO ARQUITETURAL (este arquivo): o empacotador funciona por
ALLOWLIST/MANIFEST explicito (``ALLOWED_ROOT_FILES``/``ALLOWED_TREE_ROOTS``
abaixo) MAIS um subconjunto REQUIRED (``REQUIRED_ROOT_FILES``/
``REQUIRED_TREE_FILES``) que precisa necessariamente existir, MAIS um
vinculo de identidade que garante que o que foi inspecionado e' exatamente
o que foi gravado no ZIP.

Camadas de defesa, nesta ordem:

  0. VALIDACAO DE OBRIGATORIEDADE, camada 1/3 (sistema de arquivos
     inicial) — roda ANTES de qualquer selecao/zip.
  1. ALLOWLIST estrutural — a defesa PRINCIPAL do que PODE (nao do que
     deve) entrar.
  2. Rejeicao de link/reparse point/hardlink/tipo-nao-regular ANTES de
     entrar em qualquer diretorio ou selecionar qualquer arquivo — nunca
     atravessa a fronteira da arvore fonte, nunca "resolve" o alvo, nunca
     segue "best effort". Se a propria inspecao (``lstat``) falhar, o
     caminho e tratado como INSEGURO (fail-closed).
  2b. VALIDACAO DE OBRIGATORIEDADE, camada 2/3 (snapshot efetivamente
      selecionado apos a coleta) — fecha a janela entre "existia na
      validacao inicial" e "sumiu antes da coleta terminar".
  3. Padroes de nome sensiveis/runtime e arquivos classificados como
     LEGACY / NAO DISTRIBUIR — defesa em profundidade, aplicada mesmo
     dentro da allowlist.
  4. Erro ao ENUMERAR uma arvore permitida nunca e tratado como "pular
     diretorio" — sempre aborta o empacotamento inteiro.
  5. LEITURA VINCULADA A IDENTIDADE (fecha o TOCTOU): na hora de gravar,
     cada arquivo e aberto, seu ``fstat`` (no handle, nao no pathname) e
     conferido contra a identidade capturada na coleta (mesmo
     ``st_dev``/``st_ino``, ainda arquivo regular, ainda ``st_nlink<=1``)
     — so entao os bytes sao lidos DESSE HANDLE e gravados via
     ``ZipFile.writestr``. ``ZipFile.write(path)`` (que reabre o pathname)
     nunca e usado.
  6. Geracao ATOMICA via arquivo temporario + ``os.replace`` — qualquer
     excecao em qualquer etapa remove o temporario e nunca deixa um ZIP
     parcial/invalido no caminho final; um ZIP anterior valido no mesmo
     caminho fica intacto se o build atual falhar.
  6b. VALIDACAO DE OBRIGATORIEDADE, camada 3/3 (ZIP realmente produzido)
      — reabre o ZIP recem-gerado e confere que todo REQUIRED_* esta
      dentro dele, ANTES do ``os.replace`` promover para o nome final.
  7. Inspecao do ZIP JA GERADO contra allowlist + defesa em profundidade
     (nunca confia so no staging).

Uso:
    .venv-test\\Scripts\\python.exe empacotar_release.py [--out NOME.zip]
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import stat
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------
# MANIFESTO — allowlist estrutural. Fonte da verdade do que PODE entrar no
# ZIP de entrega. Derivado do projeto real (nao um chute); revisar
# manualmente sempre que um arquivo/pasta novo legitimo for adicionado ao
# projeto (ver validacao em tests/test_release_packaging.py).
# ---------------------------------------------------------------------------

# Arquivos permitidos diretamente na raiz do projeto (nome exato).
ALLOWED_ROOT_FILES = {
    "ARQUITETURA_ATUAL.md",
    "CLAUDE.md",
    "CORRECAO_WINDOWS_OS_REPLACE_RELATORIO.md",
    "CORRECAO_WINDOWS_SQLITE_WAL_TEARDOWN_RELATORIO.md",
    "EMPACOTAR_RELEASE.bat",
    "empacotar_release.py",
    "GATE_19_5_ESTAGIO2_RELATORIO.md",
    "GATE_19_5_RELATORIO.md",
    "INSTALAR_DEPENDENCIAS_TESTE.bat",
    "LEIA_ME_PRIMEIRO.txt",
    "LIMPAR_ANTES_DE_ZIPAR.bat",
    "MAPA_DE_DADOS.md",
    "PAINEL_OFICIAL.bat",
    "PRODUCT_INVARIANTS.md",
    "PROMPT_20_CIRCUIT_BREAKER_RELATORIO.md",
    "PROMPT_21_RETRY_INTELIGENTE_RELATORIO.md",
    "PROMPT_22_IDEMPOTENCIA_RELATORIO.md",
    "PROMPT_23_SECRETS_MANAGER_RELATORIO.md",
    "PROMPT_23_CORRECAO_RELATORIO.md",
    "PROMPT_24_SOURCE_IMPORT_RELATORIO.md",
    "PROMPT_24B_IMPORT_OPTIONS_RELATORIO.md",
    "PROMPT_25_SOURCE_CONTEXT_RESOLVER_RELATORIO.md",
    "PROMPT_26_MEDIA_PROBE_RELATORIO.md",
    "PROMPT_27_EDIT_PROJECT_RELATORIO.md",
    "PROMPT_27B_VIDEO_PROMOTION_RELATORIO.md",
    "PROMPT_27_5_MEDIA_CATALOG_RELATORIO.md",
    "PROMPT_28_TIMELINE_EDITOR_RELATORIO.md",
    "PROMPT_29_VISUAL_EDITOR_RELATORIO.md",
    "PROMPT_30_AUDIO_ENGINE_RELATORIO.md",
    "PROMPT_31_CAPTIONS_ENGINE_RELATORIO.md",
    "PROMPT_32_CAPTIONS_STYLE_RELATORIO.md",
    "PROMPT_33_AUTO_REFRAME_RELATORIO.md",
    "PROMPT_34_SILENCE_REMOVAL_RELATORIO.md",
    "PROMPT_35_METADATA_MANAGER_RELATORIO.md",
    "PROMPT_36_TEMPLATE_ENGINE_RELATORIO.md",
    "PROMPT_38_TEMPLATE_IMPORTER_RELATORIO.md",
    "PROMPT_39_LAYOUT_MAPPER_RELATORIO.md",
    "PROMPT_40_DYNAMIC_CONTENT_RELATORIO.md",
    "PROMPT_41_TEMPLATE_SELECTOR_RELATORIO.md",
    "PROMPT_42_RENDER_ENGINE_RELATORIO.md",
    "PROMPT_43_QUALITY_CONTROL_RELATORIO.md",
    "PROMPT_44_SMART_CLIP_BASE_RELATORIO.md",
    "PROMPT_45_SMART_CLIP_LOCAL_SCORING_RELATORIO.md",
    "PROMPT_46_CONTENT_TYPE_RELATORIO.md",
    "PROMPT_47_RANKING_CONTEXTO_RELATORIO.md",
    "PROMPT_CATALOG_WIRING_REFRAMED_916_RELATORIO.md",
    "PROMPT_CATALOG_WIRING_RELATORIO.md",
    "REGRESSION_CHECKLIST.md",
    "requirements.txt",
    "RISCOS_ATUAIS.md",
    "ROADMAP_COMPLETO.md",
    "RODAR_TESTES.bat",
    "TESTE_MANUAL_WINDOWS.md",
    "TESTE_MANUAL_WINDOWS_ESTAGIO2.md",
}

# Diretorios de nivel superior cujo conteudo pode entrar (recursivamente),
# sujeito ainda as extensoes/arquivos permitidos por arvore abaixo e as
# camadas de defesa 3-4. Qualquer diretorio de nivel superior que NAO
# esteja aqui e ignorado por inteiro, com aviso.
ALLOWED_TREE_ROOTS = {"_sistema", "tests", "assets"}

# Dentro de uma arvore permitida, quais extensoes de arquivo podem entrar
# livremente (tipicamente codigo-fonte, que muda com frequencia).
ALLOWED_TREE_EXTENSIONS = {".py"}

# Dentro de uma arvore permitida, arquivos que NAO batem com
# ALLOWED_TREE_EXTENSIONS mas sao legitimos e conhecidos (caminho relativo
# exato a raiz do projeto, sempre com "/").
#
# Os 2 PNGs de "assets/templates_oficiais/" (Prompt 36 -- Template Engine)
# sao adicionados aqui de proposito, NUNCA ampliando
# ALLOWED_TREE_EXTENSIONS para ".png" de forma geral: a lista explicita
# por caminho exato e a mesma disciplina ja usada para
# "tests/README.md"/"tests/requirements-test.txt" -- cada arquivo binario
# que entra no pacote e uma decisao consciente e auditavel, nunca uma
# extensao aberta que aceitaria qualquer PNG futuro sem revisao.
ALLOWED_TREE_EXTRA_FILES = {
    "tests/README.md",
    "tests/requirements-test.txt",
    "assets/templates_oficiais/template_moldura_tech_azul.png",
    "assets/templates_oficiais/template_moldura_branca_classica.png",
}

# ---------------------------------------------------------------------------
# OBRIGATORIEDADE — subconjunto de ALLOWED_* que precisa NECESSARIAMENTE
# existir para uma release ser considerada valida. Um arquivo do manifesto
# que NAO esta em nenhuma destas duas listas e OPCIONAL: sua ausencia nao
# impede o build (tipicamente documentacao interna auxiliar).
#
# Criterio: um arquivo e REQUIRED quando sua ausencia significa que o
# PRODUTO ou a INFRAESTRUTURA DE BUILD/TESTE deixam de funcionar.
# Documentacao de auditoria/arquitetura/roadmap nao impede o software de
# rodar nem o release de ser gerado — por isso e OPTIONAL.
# ---------------------------------------------------------------------------

REQUIRED_ROOT_FILES = {
    "PAINEL_OFICIAL.bat",
    "requirements.txt",
    "CLAUDE.md",
    "empacotar_release.py",
    "EMPACOTAR_RELEASE.bat",
    "INSTALAR_DEPENDENCIAS_TESTE.bat",
    "RODAR_TESTES.bat",
    "LIMPAR_ANTES_DE_ZIPAR.bat",
    "LEIA_ME_PRIMEIRO.txt",
}

OPTIONAL_ROOT_FILES = ALLOWED_ROOT_FILES - REQUIRED_ROOT_FILES

REQUIRED_TREE_FILES = {
    "_sistema/agendar_tiktok.py",
    "_sistema/agendar_youtube.py",
    "_sistema/app_paths.py",
    "_sistema/batch_engine.py",
    "_sistema/control_manager.py",
    "_sistema/domain/__init__.py",
    "_sistema/domain/checkpoints.py",
    "_sistema/domain/job_state_machine.py",
    "_sistema/domain/models.py",
    "_sistema/gerar_textos.py",
    "_sistema/job_engine.py",
    "_sistema/limpar_metadados_oficial.py",
    "_sistema/login_conta.py",
    "_sistema/painel_oficial.py",
    "_sistema/recovery_manager.py",
    "_sistema/resource_manager.py",
    "_sistema/shutdown_coordinator.py",
    "_sistema/state_json.py",
    "_sistema/storage/__init__.py",
    "_sistema/storage/audit.py",
    "_sistema/storage/backup.py",
    "_sistema/storage/database.py",
    "_sistema/storage/legacy_migration.py",
    "_sistema/storage/migrations/__init__.py",
    "_sistema/storage/migrations/m001_initial.py",
    "_sistema/storage/migrations/m002_audit_append_only.py",
    "_sistema/storage/migrations/m003_batch_engine.py",
    "_sistema/storage_manager.py",
    "_sistema/time_utils.py",
}


def _required_paths() -> list[str]:
    """Lista combinada (ordenada) de todos os caminhos obrigatorios."""
    return sorted(REQUIRED_ROOT_FILES) + sorted(REQUIRED_TREE_FILES)


# ---------------------------------------------------------------------------
# Defesa em profundidade — nomes de diretorio nunca permitidos em NENHUM
# ponto da arvore, mesmo que a allowlist um dia os deixasse passar.
# ---------------------------------------------------------------------------
FORBIDDEN_DIR_NAMES = {
    ".venv-test",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".git",
    "_to_delete",
    "contas",
    "_removidas",
    "accounts",
    "removed_accounts",
    "perfil_youtube",
    "perfil_tiktok",
    "perfil_chrome",
    "videos",
    "logs",
    "temp",
    "cache",
    "backups",
    "support",
    "database",
}

FORBIDDEN_FILE_PATTERNS = (
    "*.pyc",
    "*.pyo",
    "*.pyd",
    ".DS_Store",
    "Thumbs.db",
    "*.zip",
)

SENSITIVE_FILENAME_PATTERNS = (
    ".env",
    ".env.*",
    "*token*",
    "*cookie*",
    "*credential*",
    "*secret*",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*-wal",
    "*-shm",
)

# CORREÇÃO (Prompt 23 -- correção pós-entrega, achado durante a
# re-verificação obrigatória "gerem o ZIP e confiram com unzip -l"): o
# padrão amplo ``*secret*`` acima existe para pegar arquivos que
# CONTÊM um segredo em texto plano (``secrets.json``,
# ``client_secret.txt`` etc.) -- mas, por ser um filtro de NOME e não de
# conteúdo, ele também bloqueava, por acidente, o próprio módulo
# ``SecretsManager`` do produto (Prompt 23): ``secrets_manager.py`` é
# código-fonte revisado que GERENCIA segredos, nunca um arquivo que
# contém um; nenhuma linha dele, do teste ou do relatório carrega um
# valor de segredo real (confirmado pelos próprios testes do módulo,
# ver ``tests/test_secrets_manager.py``). A entrega anterior deste
# Prompt gerou um ZIP sem ``_sistema/secrets_manager.py``,
# ``tests/test_secrets_manager.py`` nem o relatório -- exatamente por
# este filtro -- sem que o empacotamento reportasse falha (o
# empacotamento "tem sucesso", só silenciosamente sem esses três
# arquivos, porque nenhum deles é ``REQUIRED_*``).
#
# Esta allowlist é NOMINAL (caminho relativo exato, não um padrão) e
# cobre SOMENTE estes três caminhos específicos, já revisados por este
# Prompt -- preserva o bloqueio de ``*secret*`` para QUALQUER outro
# arquivo (ex.: um ``secrets.json``, ``my_client_secret.txt`` ou um
# módulo futuro chamado ``outro_secret_qualquer.py`` adicionado sem
# revisão continuam bloqueados normalmente, ver
# ``tests/test_release_packaging.py``,
# ``test_outro_arquivo_com_secret_no_nome_fora_da_excecao_continua_bloqueado``).
# Adicionar um caminho novo aqui exige a mesma disciplina: só depois de
# confirmar, por leitura/teste, que o arquivo nunca carrega um valor de
# segredo real.
SENSITIVE_PATTERN_EXEMPTIONS = frozenset({
    "_sistema/secrets_manager.py",
    "tests/test_secrets_manager.py",
    "PROMPT_23_SECRETS_MANAGER_RELATORIO.md",
})

LEGACY_DO_NOT_DISTRIBUTE = {
    "_sistema/defender_setup.ps1": (
        "adiciona a pasta do projeto as exclusoes do Windows Defender via "
        "'Add-MpPreference -ExclusionPath', solicitando elevacao UAC se "
        "necessario (ver ARQUITETURA_ATUAL.md secao 20 e RISCOS_ATUAIS.md). "
        "Isso contraria diretamente o principio F do CLAUDE.md ('Nao "
        "desabilitar antivirus. Nao adicionar exclusoes automaticas ao "
        "Windows Defender.'). Mantido no repositorio apenas como script "
        "legado/manual para uso pontual e consciente do desenvolvedor — "
        "nunca deve ser distribuido dentro do pacote de release."
    ),
}


class PackagingSecurityError(RuntimeError):
    """Erro fatal de seguranca: link/reparse point/hardlink encontrado, a
    inspecao de um caminho falhou (fail-closed), ou a identidade de um
    arquivo mudou entre a coleta e a escrita (TOCTOU). Sempre aborta o
    empacotamento inteiro."""


class PackagingIntegrityError(RuntimeError):
    """Erro fatal de integridade: um caminho obrigatorio esta ausente (em
    qualquer uma das 3 camadas de verificacao), ou houve falha ao
    enumerar uma arvore do manifesto. Sempre aborta o empacotamento
    inteiro e nunca produz um ZIP "de sucesso" incompleto."""


class _FileIdentity(NamedTuple):
    """Identidade capturada no momento da COLETA (``lstat``), revalidada
    no HANDLE ABERTO (``fstat``) imediatamente antes da leitura dos bytes
    que vao para o ZIP. Se ``st_dev``/``st_ino`` nao baterem mais, o
    arquivo foi trocado entre a coleta e a escrita (TOCTOU) e o
    empacotamento e abortado."""

    st_dev: int
    st_ino: int


def _matches_any(name: str, patterns: tuple[str, ...]) -> str | None:
    lowered = name.lower()
    for pattern in patterns:
        if fnmatch.fnmatch(lowered, pattern.lower()):
            return pattern
    return None


def _lstat_or_fail(path: Path) -> os.stat_result:
    """``os.lstat`` fail-closed: se a propria inspecao falhar, o caminho
    NAO e tratado como "provavelmente regular" — levanta
    ``PackagingSecurityError`` diretamente."""
    try:
        return os.lstat(path)
    except OSError as exc:
        raise PackagingSecurityError(
            f"nao foi possivel inspecionar '{path}' ({type(exc).__name__}: "
            f"{exc}). Um caminho que nao pode ser inspecionado nunca e "
            f"considerado seguro — empacotamento abortado (fail-closed)."
        ) from exc


def _classify(path: Path, st: os.stat_result) -> str:
    """Classifica um ``stat_result`` (de ``lstat``, nunca segue link) em
    ``"dir"``, ``"regular"``, ``"symlink"``, ``"junction"``,
    ``"reparse_point"`` ou ``"other"`` (FIFO/socket/device/etc. — nunca
    tratado como distribuivel). NAO avalia hardlink aqui (isso e feito
    separadamente, so para o que sera efetivamente distribuido como
    arquivo)."""
    if stat.S_ISLNK(st.st_mode):
        return "symlink"

    attrs = getattr(st, "st_file_attributes", None)
    reparse_bit = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", None)
    if attrs is not None and reparse_bit is not None and (attrs & reparse_bit):
        try:
            is_junction = path.is_junction()  # Python 3.12+
        except AttributeError:
            is_junction = False
        except OSError as exc:
            raise PackagingSecurityError(
                f"reparse point encontrado em '{path}' e a inspecao "
                f"adicional (is_junction) falhou ({type(exc).__name__}: "
                f"{exc}). Tratado como inseguro — empacotamento abortado."
            ) from exc
        return "junction" if is_junction else "reparse_point"

    if stat.S_ISDIR(st.st_mode):
        return "dir"
    if stat.S_ISREG(st.st_mode):
        return "regular"
    return "other"


def _require_regular_no_hardlink(path: Path, rel: Path, st: os.stat_result) -> _FileIdentity:
    """Confirma que ``path`` e um arquivo regular seguro para ser
    distribuido: nao e link/reparse/other, e nao possui mais de um
    hardlink (``st_nlink <= 1``). Empacotamento CONSERVADOR: nunca tenta
    decidir qual dos nomes que apontam para o mesmo inode "e o original"
    — qualquer arquivo selecionado com ``st_nlink > 1`` e rejeitado,
    mesmo que o conteudo pareca inofensivo. Devolve a identidade
    (``st_dev``/``st_ino``) a ser revalidada na escrita."""
    kind = _classify(path, st)
    if kind != "regular":
        raise PackagingSecurityError(
            f"caminho dentro da arvore fonte permitida nao e um arquivo "
            f"regular seguro: {rel.as_posix()} (tipo: {kind}). "
            f"Empacotamento abortado — links/reparse points/tipos "
            f"especiais nunca sao distribuidos."
        )
    if st.st_nlink > 1:
        raise PackagingSecurityError(
            f"hardlink detectado (st_nlink={st.st_nlink}) em arquivo que "
            f"seria distribuido: {rel.as_posix()}. Empacotamento "
            f"CONSERVADOR — qualquer arquivo com mais de um hardlink e "
            f"rejeitado, sem tentar decidir qual nome e o 'original' "
            f"(um hardlink pode apontar para conteudo fora da arvore "
            f"fonte sob outro nome)."
        )
    return _FileIdentity(st_dev=st.st_dev, st_ino=st.st_ino)


def _forbidden_defense_in_depth(rel: Path) -> str | None:
    """Defesa em profundidade, aplicada mesmo a caminhos ja aprovados
    pela allowlist."""
    rel_posix = rel.as_posix()
    if rel_posix in LEGACY_DO_NOT_DISTRIBUTE:
        return f"classificado como LEGACY / NAO DISTRIBUIR: {LEGACY_DO_NOT_DISTRIBUTE[rel_posix]}"
    parts = rel.parts
    for part in parts[:-1]:
        if part in FORBIDDEN_DIR_NAMES:
            return f"dentro de diretorio proibido: {part}"
    name = parts[-1] if parts else ""
    if name in FORBIDDEN_DIR_NAMES:
        return f"diretorio proibido: {name}"
    pattern = _matches_any(name, FORBIDDEN_FILE_PATTERNS)
    if pattern is not None:
        return f"arquivo proibido (padrao {pattern}): {name}"
    if rel_posix not in SENSITIVE_PATTERN_EXEMPTIONS:
        pattern = _matches_any(name, SENSITIVE_FILENAME_PATTERNS)
        if pattern is not None:
            return f"nome de arquivo sensivel (padrao {pattern}): {name}"
    return None


def _validate_required_paths(project_root: Path) -> list[str]:
    """Camada 1/3 de obrigatoriedade — sistema de arquivos inicial.
    Devolve a lista de caminhos obrigatorios AUSENTES (vazia se tudo
    presente). Levanta ``PackagingSecurityError`` se um caminho
    obrigatorio existir mas for link/reparse/hardlink/tipo especial —
    nao pode ser tratado como "presente e valido"."""
    missing: list[str] = []
    for rel_str in _required_paths():
        abs_path = project_root / rel_str
        try:
            st = os.lstat(abs_path)
        except OSError:
            missing.append(rel_str)
            continue
        _require_regular_no_hardlink(abs_path, Path(rel_str), st)
    return missing


def _walk_tree_no_links(tree_root: Path, project_root: Path):
    """Percorre ``tree_root`` manualmente (nunca via ``rglob``) usando
    ``os.scandir``, classificando CADA entrada antes de decidir se e
    arquivo ou diretorio a descer. Gera
    ``(caminho_absoluto, caminho_relativo, identidade)`` para cada ARQUIVO
    real (regular, sem hardlink) encontrado.

    FAIL-CLOSED sobre erro de enumeracao: qualquer falha de
    ``os.scandir`` aborta o empacotamento inteiro com
    ``PackagingIntegrityError`` — nunca "pular diretorio"."""
    stack = [tree_root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            rel_current = current.relative_to(project_root)
            raise PackagingIntegrityError(
                f"falha ao enumerar diretorio dentro de uma arvore do "
                f"manifesto: {rel_current.as_posix()} "
                f"({type(exc).__name__}: {exc}). Empacotamento abortado — "
                f"um erro de leitura nunca e tratado como 'pular "
                f"diretorio', pois isso produziria um ZIP incompleto "
                f"reportado como sucesso."
            ) from exc
        for entry in sorted(entries, key=lambda e: e.name):
            entry_path = Path(entry.path)
            rel = entry_path.relative_to(project_root)
            st = _lstat_or_fail(entry_path)
            kind = _classify(entry_path, st)
            if kind == "dir":
                stack.append(entry_path)
            elif kind == "regular":
                identity = _require_regular_no_hardlink(entry_path, rel, st)
                yield entry_path, rel, identity
            else:
                raise PackagingSecurityError(
                    f"link/reparse point encontrado dentro da arvore fonte "
                    f"permitida: {rel.as_posix()} (tipo: {kind}). "
                    f"Empacotamento abortado — links nunca sao seguidos "
                    f"nem copiados, mesmo que o alvo pareca inofensivo."
                )


def _collect_allowed_files(project_root: Path) -> tuple[list[tuple[Path, Path, _FileIdentity]], list[str]]:
    """Devolve (arquivos_permitidos_com_identidade, avisos_de_ignorados)."""
    selected: list[tuple[Path, Path, _FileIdentity]] = []
    ignored: list[str] = []

    try:
        root_entries = list(project_root.iterdir())
    except OSError as exc:
        raise PackagingIntegrityError(
            f"falha ao enumerar a raiz do projeto ({type(exc).__name__}: "
            f"{exc}). Empacotamento abortado."
        ) from exc

    for entry in sorted(root_entries, key=lambda p: p.name):
        rel = entry.relative_to(project_root)
        st = _lstat_or_fail(entry)
        kind = _classify(entry, st)
        if kind == "dir":
            if entry.name not in ALLOWED_TREE_ROOTS:
                ignored.append(f"{rel.as_posix()}/ (diretorio de nivel superior fora do manifesto)")
            continue
        if kind != "regular":
            raise PackagingSecurityError(
                f"caminho de raiz nao e um arquivo/diretorio regular "
                f"seguro: {rel.as_posix()} (tipo: {kind}). Empacotamento "
                f"abortado."
            )
        if entry.name in ALLOWED_ROOT_FILES:
            identity = _require_regular_no_hardlink(entry, rel, st)
            selected.append((entry, rel, identity))
        else:
            ignored.append(f"{rel.as_posix()} (arquivo de raiz fora do manifesto)")

    for tree_name in sorted(ALLOWED_TREE_ROOTS):
        tree_root = project_root / tree_name
        if not tree_root.exists():
            continue
        for abs_path, rel, identity in _walk_tree_no_links(tree_root, project_root):
            rel_posix = rel.as_posix()
            if rel_posix in LEGACY_DO_NOT_DISTRIBUTE:
                ignored.append(
                    f"{rel_posix} (LEGACY / NAO DISTRIBUIR: {LEGACY_DO_NOT_DISTRIBUTE[rel_posix]})"
                )
                continue
            if rel.suffix in ALLOWED_TREE_EXTENSIONS or rel_posix in ALLOWED_TREE_EXTRA_FILES:
                selected.append((abs_path, rel, identity))
            else:
                ignored.append(f"{rel_posix} (dentro de arvore permitida, mas fora do manifesto de extensoes/arquivos)")

    final: list[tuple[Path, Path, _FileIdentity]] = []
    for abs_path, rel, identity in selected:
        reason = _forbidden_defense_in_depth(rel)
        if reason is not None:
            ignored.append(f"{rel.as_posix()} (bloqueado por defesa em profundidade: {reason})")
            continue
        final.append((abs_path, rel, identity))

    return final, ignored


def _missing_required_from_selected(files: list[tuple[Path, Path, _FileIdentity]]) -> list[str]:
    """Camada 2/3 de obrigatoriedade — snapshot efetivamente selecionado.
    Fecha a janela entre 'existia durante a validacao inicial' e 'sumiu
    antes da coleta terminar'."""
    selected_rel = {rel.as_posix() for _, rel, _ in files}
    return [r for r in _required_paths() if r not in selected_rel]


def _missing_required_in_zip(zip_path: Path) -> list[str]:
    """Camada 3/3 de obrigatoriedade — ZIP realmente produzido. Reabre o
    ZIP recem-gerado (ainda no caminho temporario) e confere que todo
    REQUIRED_* esta dentro, ANTES do ``os.replace`` promover para o nome
    final."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
    return [r for r in _required_paths() if r not in names]


def _read_verified_bytes(abs_path: Path, rel: Path, identity: _FileIdentity) -> bytes:
    """Le os bytes de ``abs_path`` de forma VINCULADA A IDENTIDADE
    capturada na coleta — fecha o TOCTOU entre a inspecao e a escrita.

    1. Abre o arquivo (com ``O_NOFOLLOW`` quando disponivel — se o
       pathname virou symlink entre a coleta e agora, a abertura ja
       falha aqui, antes de qualquer leitura — e com ``O_BINARY`` quando
       disponivel, ver "BUG CORRIGIDO (Windows)" abaixo).
    2. ``os.fstat`` NO HANDLE ABERTO (nunca re-``lstat`` do pathname, que
       poderia ter sido trocado de novo entre o open e o stat).
    3. Confere que o handle aberto ainda e arquivo regular.
    4. Confere que o handle aberto ainda tem ``st_nlink <= 1``.
    5. Compara ``st_dev``/``st_ino`` do handle com a identidade
       autorizada na coleta — se nao bater, o arquivo foi TROCADO entre
       a coleta e a escrita (TOCTOU), mesmo que o pathname seja o mesmo.
    6. So entao le os bytes DESSE HANDLE (nunca reabre o pathname de
       novo) e devolve — o chamador grava via ``ZipFile.writestr``.

    Nao mantem handles de multiplos arquivos abertos simultaneamente —
    cada arquivo e aberto, validado, lido e fechado antes do proximo.

    BUG CORRIGIDO (Windows) — correcao do Prompt 36: sem ``os.O_BINARY``,
    ``os.open`` no Windows abre em MODO TEXTO por padrao (semantica do
    CRT do MSVCRT, historica do MS-DOS). Em modo texto, o byte ``0x1A``
    (Ctrl-Z / SUB) e tratado como fim de arquivo e ``\\r\\n`` e traduzido
    para ``\\n`` na leitura. A assinatura PNG padrao e
    ``\\x89 P N G \\r \\n \\x1a \\n`` (8 bytes) -- ou seja, TODO arquivo
    PNG contem ``0x1A`` exatamente no 7o byte da propria assinatura.
    Sem ``O_BINARY``, qualquer PNG empacotado neste caminho era truncado
    logo na assinatura (``\\r\\n`` virava ``\\n``, e a leitura parava no
    ``0x1A``, sobrando so ``\\x89PNG\\n`` -- 5 bytes -- do arquivo
    inteiro). Isso so foi descoberto quando o primeiro asset binario
    (os 2 PNGs oficiais do Prompt 36/37) passou por este caminho pela
    primeira vez -- todo Prompt anterior so empacotava ``.py``/``.md``,
    onde ``0x1A`` e extremamente improvavel de aparecer, mascarando o
    bug. ``getattr(os, "O_BINARY", 0)`` e 0 em POSIX (mesmo padrao ja
    usado para ``O_NOFOLLOW`` acima) -- nao muda nenhum comportamento
    em Linux/macOS, so corrige o Windows."""
    try:
        fd = os.open(
            str(abs_path),
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
        )
    except OSError as exc:
        raise PackagingSecurityError(
            f"falha ao abrir '{rel.as_posix()}' para leitura verificada "
            f"({type(exc).__name__}: {exc}). Se o caminho virou symlink "
            f"entre a coleta e a escrita, esta e exatamente a protecao "
            f"esperada (O_NOFOLLOW). Empacotamento abortado."
        ) from exc
    try:
        try:
            st = os.fstat(fd)
        except OSError as exc:
            raise PackagingSecurityError(
                f"fstat falhou no handle aberto de '{rel.as_posix()}' "
                f"({type(exc).__name__}: {exc}). Empacotamento abortado — "
                f"nenhum byte foi lido/gravado a partir deste caminho."
            ) from exc

        if not stat.S_ISREG(st.st_mode):
            raise PackagingSecurityError(
                f"'{rel.as_posix()}' deixou de ser arquivo regular entre a "
                f"coleta e a escrita (tipo mudou). Empacotamento abortado "
                f"— nenhum byte foi gravado a partir deste caminho."
            )
        if st.st_nlink > 1:
            raise PackagingSecurityError(
                f"'{rel.as_posix()}' passou a ter multiplos hardlinks "
                f"(st_nlink={st.st_nlink}) entre a coleta e a escrita. "
                f"Empacotamento abortado."
            )
        if (st.st_dev, st.st_ino) != (identity.st_dev, identity.st_ino):
            raise PackagingSecurityError(
                f"identidade de '{rel.as_posix()}' mudou entre a coleta "
                f"(inspecao) e a escrita — TOCTOU detectado e abortado. "
                f"Nenhum byte foi gravado a partir deste caminho; o "
                f"pathname pode agora apontar para conteudo diferente do "
                f"que foi autorizado."
            )

        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _zip_files(files: list[tuple[Path, Path, _FileIdentity]], zip_path: Path) -> None:
    """Escreve o ZIP lendo cada arquivo atraves de ``_read_verified_bytes``
    (handle aberto + identidade revalidada) e gravando via
    ``ZipFile.writestr`` — NUNCA ``ZipFile.write(path)``, que reabriria o
    pathname por nome e reintroduziria a janela de TOCTOU."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for abs_path, rel, identity in sorted(files, key=lambda t: t[1].as_posix()):
            data = _read_verified_bytes(abs_path, rel, identity)
            zf.writestr(rel.as_posix(), data)


def _inspect_zip(zip_path: Path) -> list[str]:
    """Reabre o ZIP JA GERADO e verifica cada entrada contra a allowlist
    E a defesa em profundidade. Rede de seguranca final."""
    violations: list[str] = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            rel = Path(info.filename)
            parts = rel.parts
            if not parts:
                continue
            top = parts[0]
            allowed = False
            if len(parts) == 1 and top in ALLOWED_ROOT_FILES:
                allowed = True
            elif top in ALLOWED_TREE_ROOTS:
                if rel.suffix in ALLOWED_TREE_EXTENSIONS or rel.as_posix() in ALLOWED_TREE_EXTRA_FILES:
                    allowed = True
            if not allowed:
                violations.append(f"{info.filename} (fora do manifesto)")
                continue
            reason = _forbidden_defense_in_depth(rel)
            if reason is not None:
                violations.append(f"{info.filename} ({reason})")
    return violations


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_release_zip(project_root: Path, zip_path: Path) -> tuple[int, list[str]]:
    """Funcao reutilizavel (tambem usada pelos testes automatizados).
    Devolve (quantidade_de_arquivos_no_zip, avisos_de_ignorados).

    Tres camadas de obrigatoriedade, que precisam TODAS concordar:
    sistema de arquivos inicial -> snapshot selecionado -> ZIP realmente
    produzido. Qualquer excecao em qualquer etapa apos a criacao do
    arquivo temporario remove o temporario e nunca toca no caminho
    final ``zip_path`` (um ZIP anterior valido la permanece intacto)."""
    # Camada 1/3 de obrigatoriedade.
    missing = _validate_required_paths(project_root)
    if missing:
        raise PackagingIntegrityError(
            "EMPACOTAMENTO FALHOU — arquivo(s) obrigatorio(s) ausente(s) "
            "(release invalida, nenhum ZIP foi gerado): " + "; ".join(missing)
        )

    files, ignored = _collect_allowed_files(project_root)

    # Camada 2/3 de obrigatoriedade.
    missing_snapshot = _missing_required_from_selected(files)
    if missing_snapshot:
        raise PackagingIntegrityError(
            "EMPACOTAMENTO FALHOU — arquivo(s) obrigatorio(s) presentes na "
            "validacao inicial mas ausentes do snapshot efetivamente "
            "selecionado (removidos entre a validacao e a coleta; nenhum "
            "ZIP foi gerado): " + "; ".join(missing_snapshot)
        )

    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=".release_tmp_", suffix=".zip", dir=str(zip_path.parent)
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        _zip_files(files, tmp_path)

        violations = _inspect_zip(tmp_path)
        if violations:
            raise RuntimeError(
                "EMPACOTAMENTO FALHOU — artefatos fora do manifesto/proibidos "
                "encontrados dentro do ZIP gerado: " + "; ".join(violations)
            )

        # Camada 3/3 de obrigatoriedade — ZIP realmente produzido, ainda
        # no caminho temporario, antes de promover para o final.
        missing_in_zip = _missing_required_in_zip(tmp_path)
        if missing_in_zip:
            raise PackagingIntegrityError(
                "EMPACOTAMENTO FALHOU — ZIP gerado nao contem todos os "
                "arquivos obrigatorios (removidos/trocados entre a coleta "
                "e a escrita; o ZIP temporario foi descartado, o caminho "
                "final nao foi tocado): " + "; ".join(missing_in_zip)
            )

        os.replace(tmp_path, zip_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    return len(files), ignored


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=None,
        help="Nome do arquivo ZIP de saida (padrao: PAINEL_OFICIAL_YOUTUBE_TIKTOK_<timestamp>.zip)",
    )
    args = parser.parse_args(argv)

    project_root = Path(__file__).resolve().parent
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    out_name = args.out or f"PAINEL_OFICIAL_YOUTUBE_TIKTOK_{timestamp}.zip"
    zip_path = project_root / out_name

    print("=" * 70)
    print("EMPACOTAMENTO AUTOMATICO DE RELEASE (allowlist + obrigatoriedade + identidade)")
    print("=" * 70)
    print(f"Raiz do projeto: {project_root}")
    print(f"ZIP de saida:    {zip_path}")
    print()

    try:
        print("[1/3] Validando obrigatoriedade e selecionando arquivos permitidos pelo manifesto...")
        count, ignored = build_release_zip(project_root, zip_path)
        print(f"      arquivos incluidos: {count}")
        if ignored:
            print(f"      arquivos IGNORADOS (fora do manifesto) - revise se algum deveria entrar:")
            for msg in ignored:
                print(f"        - {msg}")
        else:
            print("      nenhum arquivo foi ignorado.")
        print()
    except PackagingSecurityError as exc:
        print()
        print(f"[ERRO DE SEGURANCA] {exc}")
        return 2
    except RuntimeError as exc:
        print()
        print(f"[ERRO] {exc}")
        return 1

    print("[2/3] ZIP gerado atomicamente, com identidade revalidada por arquivo, e ja inspecionado.")
    print()

    print("[3/3] Calculando SHA-256 do ZIP final...")
    digest = _sha256_of(zip_path)
    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"      SHA-256: {digest}")
    print(f"      Tamanho: {size_mb:.2f} MB")
    print()

    print("=" * 70)
    print("EMPACOTAMENTO CONCLUIDO COM SUCESSO")
    print(f"Arquivo: {zip_path.name}")
    print(f"SHA-256: {digest}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

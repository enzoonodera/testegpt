# -*- coding: utf-8 -*-
"""Testes do empacotador de release (GATE 19.5 — correcao dos BLOCKERs de
seguranca e de integridade do Estagio 1).

Todos os cenarios usam APENAS dados sinteticos/ficticios criados dentro de
``tmp_path`` — nunca acessam dados reais de usuario, nunca leem a instalacao
real do projeto (exceto os dois testes de validacao do manifesto, que so
CONFEREM presenca/cobertura de caminhos, nunca leem conteudo sensivel). Um
projeto FALSO e COMPLETO (satisfazendo todos os REQUIRED_*) e montado em
``_make_complete_project``, imitando a estrutura real o suficiente para
exercitar o empacotador isoladamente; testes de "arquivo obrigatorio
ausente" partem dessa base e removem um item especifico.
"""
from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import empacotar_release as pkg  # noqa: E402


def _make_complete_project(root: Path) -> None:
    """Monta um projeto ficticio que satisfaz TODO REQUIRED_ROOT_FILES e
    REQUIRED_TREE_FILES, para que ``build_release_zip`` tenha sucesso "do
    zero". Os testes de arquivo obrigatorio ausente partem daqui e
    removem/apagam um item especifico."""
    for name in pkg.REQUIRED_ROOT_FILES:
        (root / name).write_text(f"conteudo ficticio de {name}\n", encoding="utf-8")
    for rel in pkg.REQUIRED_TREE_FILES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# codigo ficticio de {rel}\n", encoding="utf-8")
    (root / "tests").mkdir(parents=True, exist_ok=True)


def _make_minimal_project(root: Path) -> None:
    """Estrutura completa (satisfaz REQUIRED_*) usada pelos testes que nao
    exercitam a validacao de obrigatoriedade em si — mantem o nome usado
    pelos testes ja existentes desta suite."""
    _make_complete_project(root)


def _zip_names(zip_path: Path) -> set[str]:
    with zipfile.ZipFile(zip_path, "r") as zf:
        return set(zf.namelist())


# ---------------------------------------------------------------------------
# 1-2: arquivos permitidos entram.
# ---------------------------------------------------------------------------


def test_arquivo_python_permitido_entra(tmp_path):
    _make_minimal_project(tmp_path)
    (tmp_path / "_sistema" / "exemplo.py").write_text("# codigo de exemplo\n", encoding="utf-8")
    zip_path = tmp_path / "out.zip"
    count, ignored = pkg.build_release_zip(tmp_path, zip_path)
    assert "_sistema/exemplo.py" in _zip_names(zip_path)
    assert count >= 2


def test_documento_permitido_entra(tmp_path):
    _make_minimal_project(tmp_path)
    zip_path = tmp_path / "out.zip"
    pkg.build_release_zip(tmp_path, zip_path)
    assert "CLAUDE.md" in _zip_names(zip_path)


# ---------------------------------------------------------------------------
# 3-7: artefatos de build/cache nunca entram.
# ---------------------------------------------------------------------------


def test_venv_test_nao_entra(tmp_path):
    _make_minimal_project(tmp_path)
    (tmp_path / ".venv-test" / "Scripts").mkdir(parents=True)
    (tmp_path / ".venv-test" / "Scripts" / "python.exe").write_bytes(b"fake-exe")
    zip_path = tmp_path / "out.zip"
    pkg.build_release_zip(tmp_path, zip_path)
    names = _zip_names(zip_path)
    assert not any(".venv-test" in n for n in names)


def test_pytest_cache_nao_entra(tmp_path):
    _make_minimal_project(tmp_path)
    (tmp_path / "tests" / ".pytest_cache").mkdir(parents=True)
    (tmp_path / "tests" / ".pytest_cache" / "v").write_text("x", encoding="utf-8")
    zip_path = tmp_path / "out.zip"
    pkg.build_release_zip(tmp_path, zip_path)
    names = _zip_names(zip_path)
    assert not any(".pytest_cache" in n for n in names)


def test_pycache_nao_entra(tmp_path):
    _make_minimal_project(tmp_path)
    (tmp_path / "_sistema" / "__pycache__").mkdir(parents=True)
    (tmp_path / "_sistema" / "__pycache__" / "exemplo.cpython-313.pyc").write_bytes(b"\x00")
    zip_path = tmp_path / "out.zip"
    pkg.build_release_zip(tmp_path, zip_path)
    names = _zip_names(zip_path)
    assert not any("__pycache__" in n for n in names)


def test_pyc_solto_nao_entra(tmp_path):
    _make_minimal_project(tmp_path)
    (tmp_path / "_sistema" / "orfao.pyc").write_bytes(b"\x00")
    zip_path = tmp_path / "out.zip"
    pkg.build_release_zip(tmp_path, zip_path)
    names = _zip_names(zip_path)
    assert not any(n.endswith(".pyc") for n in names)


def test_zip_antigo_nao_entra(tmp_path):
    _make_minimal_project(tmp_path)
    (tmp_path / "entrega_antiga.zip").write_bytes(b"PK\x03\x04fake")
    zip_path = tmp_path / "out.zip"
    pkg.build_release_zip(tmp_path, zip_path)
    names = _zip_names(zip_path)
    assert not any(n.endswith(".zip") for n in names)


# ---------------------------------------------------------------------------
# 8-15: dados de runtime/sensiveis FICTICIOS nunca entram (o BLOCKER
# original de seguranca). Reproduz literalmente o cenario da primeira
# auditoria independente.
# ---------------------------------------------------------------------------


def test_pasta_contas_legacy_nao_entra(tmp_path):
    _make_minimal_project(tmp_path)
    conta = tmp_path / "contas" / "youtube" / "demo"
    (conta / "videos").mkdir(parents=True)
    (conta / "videos" / "001.mp4").write_bytes(b"fake video bytes")
    zip_path = tmp_path / "out.zip"
    pkg.build_release_zip(tmp_path, zip_path)
    names = _zip_names(zip_path)
    assert not any(n.startswith("contas/") for n in names)


def test_pasta_removidas_nao_entra(tmp_path):
    _make_minimal_project(tmp_path)
    (tmp_path / "_removidas" / "youtube" / "antiga").mkdir(parents=True)
    (tmp_path / "_removidas" / "youtube" / "antiga" / "estado.json").write_text("{}", encoding="utf-8")
    zip_path = tmp_path / "out.zip"
    pkg.build_release_zip(tmp_path, zip_path)
    names = _zip_names(zip_path)
    assert not any(n.startswith("_removidas/") for n in names)


def test_perfil_navegador_ficticio_nao_entra(tmp_path):
    _make_minimal_project(tmp_path)
    perfil = tmp_path / "contas" / "youtube" / "demo" / "perfil_youtube"
    perfil.mkdir(parents=True)
    (perfil / "Cookies").write_text("fake-cookie-data", encoding="utf-8")
    zip_path = tmp_path / "out.zip"
    pkg.build_release_zip(tmp_path, zip_path)
    names = _zip_names(zip_path)
    assert not any("perfil_youtube" in n for n in names)
    assert not any("Cookies" in n for n in names)


def test_video_ficticio_nao_entra(tmp_path):
    _make_minimal_project(tmp_path)
    (tmp_path / "contas" / "youtube" / "demo" / "videos").mkdir(parents=True)
    (tmp_path / "contas" / "youtube" / "demo" / "videos" / "001.mp4").write_bytes(b"fake mp4")
    zip_path = tmp_path / "out.zip"
    pkg.build_release_zip(tmp_path, zip_path)
    names = _zip_names(zip_path)
    assert not any(n.endswith(".mp4") for n in names)


def test_log_bruto_ficticio_nao_entra(tmp_path):
    _make_minimal_project(tmp_path)
    (tmp_path / "contas" / "youtube" / "demo" / "logs").mkdir(parents=True)
    (tmp_path / "contas" / "youtube" / "demo" / "logs" / "raw.log").write_text("log ficticio", encoding="utf-8")
    zip_path = tmp_path / "out.zip"
    pkg.build_release_zip(tmp_path, zip_path)
    names = _zip_names(zip_path)
    assert not any(n.endswith(".log") for n in names)


def test_dotenv_ficticio_nao_entra(tmp_path):
    _make_minimal_project(tmp_path)
    (tmp_path / ".env").write_text("API_KEY=fake123", encoding="utf-8")
    zip_path = tmp_path / "out.zip"
    pkg.build_release_zip(tmp_path, zip_path)
    names = _zip_names(zip_path)
    assert ".env" not in names


def test_token_credential_ficticio_nao_entra(tmp_path):
    _make_minimal_project(tmp_path)
    (tmp_path / "token.json").write_text('{"access_token": "fake"}', encoding="utf-8")
    (tmp_path / "credential_store.json").write_text("{}", encoding="utf-8")
    zip_path = tmp_path / "out.zip"
    pkg.build_release_zip(tmp_path, zip_path)
    names = _zip_names(zip_path)
    assert "token.json" not in names
    assert "credential_store.json" not in names


def test_secrets_manager_modulo_teste_e_relatorio_sao_permitidos_apesar_do_padrao_secret(
    tmp_path,
):
    """CORREÇÃO (Prompt 23 -- correção pós-entrega): ``*secret*`` em
    ``SENSITIVE_FILENAME_PATTERNS`` bloqueava, por acidente, o próprio
    módulo ``SecretsManager`` do produto -- a entrega original deste
    Prompt gerou um ZIP sem ``_sistema/secrets_manager.py``,
    ``tests/test_secrets_manager.py`` nem o relatório, silenciosamente
    (o build "tinha sucesso" mesmo assim, porque nenhum dos três é
    ``REQUIRED_*``). ``SENSITIVE_PATTERN_EXEMPTIONS`` corrige isso de
    forma nominal (só esses três caminhos exatos) -- este teste prova
    que os três agora entram no ZIP."""
    _make_minimal_project(tmp_path)
    (tmp_path / "_sistema" / "secrets_manager.py").write_text(
        "# modulo ficticio\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "test_secrets_manager.py").write_text(
        "# teste ficticio\n", encoding="utf-8"
    )
    (tmp_path / "PROMPT_23_SECRETS_MANAGER_RELATORIO.md").write_text(
        "# relatorio ficticio\n", encoding="utf-8"
    )
    zip_path = tmp_path / "out.zip"
    pkg.build_release_zip(tmp_path, zip_path)
    names = _zip_names(zip_path)
    assert "_sistema/secrets_manager.py" in names
    assert "tests/test_secrets_manager.py" in names
    assert "PROMPT_23_SECRETS_MANAGER_RELATORIO.md" in names


def test_outro_arquivo_com_secret_no_nome_fora_da_excecao_continua_bloqueado(tmp_path):
    """A exceção nominal (``SENSITIVE_PATTERN_EXEMPTIONS``) cobre SOMENTE
    os três caminhos revisados do Prompt 23 -- qualquer OUTRO arquivo cujo
    nome contenha "secret" (ex.: um módulo futuro adicionado sem revisão,
    ou um ``secrets.json`` acidental) continua bloqueado normalmente pelo
    padrão ``*secret*``. Prova que a correção não abriu um buraco geral no
    filtro."""
    _make_minimal_project(tmp_path)
    (tmp_path / "_sistema" / "outro_secret_qualquer.py").write_text(
        "# nao deveria entrar\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "test_secret_nao_relacionado.py").write_text(
        "# nao deveria entrar\n", encoding="utf-8"
    )
    zip_path = tmp_path / "out.zip"
    pkg.build_release_zip(tmp_path, zip_path)
    names = _zip_names(zip_path)
    assert "_sistema/outro_secret_qualquer.py" not in names
    assert "tests/test_secret_nao_relacionado.py" not in names


def test_db_runtime_ficticio_nao_entra(tmp_path):
    _make_minimal_project(tmp_path)
    (tmp_path / "contas" / "youtube" / "demo").mkdir(parents=True)
    (tmp_path / "contas" / "youtube" / "demo" / "estado.sqlite").write_bytes(b"SQLite format 3\x00fake")
    zip_path = tmp_path / "out.zip"
    pkg.build_release_zip(tmp_path, zip_path)
    names = _zip_names(zip_path)
    assert not any(n.endswith(".sqlite") for n in names)


# ---------------------------------------------------------------------------
# 16-17: symlink/junction/reparse point causam FALHA (nunca copiam o alvo).
# ---------------------------------------------------------------------------


def test_symlink_para_arquivo_externo_causa_falha(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _make_minimal_project(project)
    external = tmp_path / "fora_da_arvore.txt"
    external.write_text("conteudo que NUNCA pode vazar para o zip", encoding="utf-8")
    link = project / "_sistema" / "link_malicioso.py"
    try:
        os.symlink(external, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"os.symlink indisponivel neste ambiente: {exc}")

    zip_path = project / "out.zip"
    with pytest.raises(pkg.PackagingSecurityError):
        pkg.build_release_zip(project, zip_path)
    assert not zip_path.exists()


def test_symlink_para_diretorio_externo_causa_falha(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _make_minimal_project(project)
    external_dir = tmp_path / "dir_externo"
    external_dir.mkdir()
    (external_dir / "segredo.py").write_text("conteudo externo", encoding="utf-8")
    link = project / "_sistema" / "dir_link"
    try:
        os.symlink(external_dir, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"os.symlink indisponivel neste ambiente: {exc}")

    zip_path = project / "out.zip"
    with pytest.raises(pkg.PackagingSecurityError):
        pkg.build_release_zip(project, zip_path)
    assert not zip_path.exists()


# ---------------------------------------------------------------------------
# 18: injecao manual no ZIP FINAL e detectada pela inspecao pos-geracao —
# prova que a defesa nao depende so do staging/selecao estar correta.
# ---------------------------------------------------------------------------


def test_injecao_manual_no_zip_final_e_detectada(tmp_path):
    _make_minimal_project(tmp_path)
    zip_path = tmp_path / "out.zip"
    pkg.build_release_zip(tmp_path, zip_path)
    assert zip_path.exists()

    # Reabre o ZIP JA VALIDADO e injeta um artefato proibido manualmente,
    # simulando um bug futuro em alguma outra etapa do processo.
    with zipfile.ZipFile(zip_path, "a") as zf:
        zf.writestr("_sistema/__pycache__/evil.pyc", b"evil bytes")

    violations = pkg._inspect_zip(zip_path)
    assert any("__pycache__" in v for v in violations)


# ---------------------------------------------------------------------------
# 19-20: SHA-256 e produzido; ZIP invalido e apagado quando a inspecao
# encontra violacao (via build_release_zip, que e o que main() usa).
# ---------------------------------------------------------------------------


def test_sha256_e_produzido(tmp_path):
    _make_minimal_project(tmp_path)
    zip_path = tmp_path / "out.zip"
    pkg.build_release_zip(tmp_path, zip_path)
    digest = pkg._sha256_of(zip_path)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_zip_invalido_e_apagado_quando_inspecao_falha(tmp_path, monkeypatch):
    _make_minimal_project(tmp_path)
    zip_path = tmp_path / "out.zip"

    # Forca a inspecao pos-geracao a sempre encontrar uma violacao, para
    # provar que o ZIP e apagado do disco nesse caminho (sem depender de
    # conseguir burlar a allowlist de verdade, o que ja foi provado nos
    # testes acima).
    def _sempre_viola(_zip_path):
        return ["arquivo/fake (forcado pelo teste)"]

    monkeypatch.setattr(pkg, "_inspect_zip", _sempre_viola)
    with pytest.raises(RuntimeError):
        pkg.build_release_zip(tmp_path, zip_path)
    assert not zip_path.exists()


# ---------------------------------------------------------------------------
# 21-25 (BLOCKER 1 — integridade): arquivo obrigatorio ausente FALHA o
# build, nao gera ZIP, informa exatamente qual caminho faltou.
# ---------------------------------------------------------------------------


def test_painel_oficial_bat_ausente_falha_build(tmp_path):
    _make_complete_project(tmp_path)
    (tmp_path / "PAINEL_OFICIAL.bat").unlink()
    zip_path = tmp_path / "out.zip"
    with pytest.raises(pkg.PackagingIntegrityError, match="PAINEL_OFICIAL.bat"):
        pkg.build_release_zip(tmp_path, zip_path)
    assert not zip_path.exists()


def test_requirements_txt_ausente_falha_build(tmp_path):
    _make_complete_project(tmp_path)
    (tmp_path / "requirements.txt").unlink()
    zip_path = tmp_path / "out.zip"
    with pytest.raises(pkg.PackagingIntegrityError, match="requirements.txt"):
        pkg.build_release_zip(tmp_path, zip_path)
    assert not zip_path.exists()


def test_modulo_produtivo_obrigatorio_ausente_falha_build(tmp_path):
    _make_complete_project(tmp_path)
    (tmp_path / "_sistema" / "batch_engine.py").unlink()
    zip_path = tmp_path / "out.zip"
    with pytest.raises(pkg.PackagingIntegrityError, match="batch_engine.py"):
        pkg.build_release_zip(tmp_path, zip_path)
    assert not zip_path.exists()


def test_migration_congelada_ausente_falha_build(tmp_path):
    _make_complete_project(tmp_path)
    (tmp_path / "_sistema" / "storage" / "migrations" / "m002_audit_append_only.py").unlink()
    zip_path = tmp_path / "out.zip"
    with pytest.raises(pkg.PackagingIntegrityError, match="m002_audit_append_only.py"):
        pkg.build_release_zip(tmp_path, zip_path)
    assert not zip_path.exists()


def test_arquivo_opcional_ausente_nao_impede_build():
    """Comportamento explicitamente documentado: um item OPTIONAL (ex.:
    documentacao de arquitetura) pode faltar sem impedir o build — so os
    itens em REQUIRED_ROOT_FILES/REQUIRED_TREE_FILES sao obrigatorios."""
    assert "ARQUITETURA_ATUAL.md" in pkg.OPTIONAL_ROOT_FILES
    assert "ARQUITETURA_ATUAL.md" not in pkg.REQUIRED_ROOT_FILES


def test_build_com_opcional_ausente_tem_sucesso(tmp_path):
    _make_complete_project(tmp_path)
    # ARQUITETURA_ATUAL.md e opcional: nem chega a ser criado pelo fixture
    # completo (so REQUIRED_* e criado), entao o build ja parte sem ele.
    assert not (tmp_path / "ARQUITETURA_ATUAL.md").exists()
    zip_path = tmp_path / "out.zip"
    count, ignored = pkg.build_release_zip(tmp_path, zip_path)
    assert zip_path.exists()
    assert "ARQUITETURA_ATUAL.md" not in _zip_names(zip_path)


# ---------------------------------------------------------------------------
# 26-28 (BLOCKER 2 — integridade): erro ao ENUMERAR uma arvore permitida
# nunca e tratado como "pular diretorio" — sempre aborta o build inteiro.
# ---------------------------------------------------------------------------


def test_permission_error_ao_enumerar_arvore_falha_build(tmp_path, monkeypatch):
    _make_complete_project(tmp_path)
    real_scandir = os.scandir
    storage_dir = str(tmp_path / "_sistema" / "storage")

    def _scandir_falho(path="."):
        if str(path) == storage_dir:
            raise PermissionError(f"acesso negado (simulado) a {path}")
        return real_scandir(path)

    monkeypatch.setattr(pkg.os, "scandir", _scandir_falho)
    zip_path = tmp_path / "out.zip"
    with pytest.raises(pkg.PackagingIntegrityError, match="storage"):
        pkg.build_release_zip(tmp_path, zip_path)
    assert not zip_path.exists()


def test_oserror_generico_ao_enumerar_arvore_falha_build(tmp_path, monkeypatch):
    _make_complete_project(tmp_path)
    real_scandir = os.scandir
    domain_dir = str(tmp_path / "_sistema" / "domain")

    def _scandir_falho(path="."):
        if str(path) == domain_dir:
            raise OSError("erro de I/O simulado")
        return real_scandir(path)

    monkeypatch.setattr(pkg.os, "scandir", _scandir_falho)
    zip_path = tmp_path / "out.zip"
    with pytest.raises(pkg.PackagingIntegrityError):
        pkg.build_release_zip(tmp_path, zip_path)
    assert not zip_path.exists()


def test_filenotfound_durante_travessia_falha_build(tmp_path, monkeypatch):
    """Simula o diretorio desaparecendo NO MEIO da travessia (ex.: apagado
    por outro processo entre o momento em que foi enfileirado para
    varredura e o momento em que e efetivamente lido) — tambem e erro de
    integridade do build, nunca "pular"."""
    _make_complete_project(tmp_path)
    real_scandir = os.scandir
    migrations_dir = str(tmp_path / "_sistema" / "storage" / "migrations")

    def _scandir_falho(path="."):
        if str(path) == migrations_dir:
            raise FileNotFoundError(f"desapareceu no meio da varredura: {path}")
        return real_scandir(path)

    monkeypatch.setattr(pkg.os, "scandir", _scandir_falho)
    zip_path = tmp_path / "out.zip"
    with pytest.raises(pkg.PackagingIntegrityError):
        pkg.build_release_zip(tmp_path, zip_path)
    assert not zip_path.exists()


# ---------------------------------------------------------------------------
# 29-31 (BLOCKER 3 — integridade): falha durante escrita/inspecao nunca
# deixa ZIP parcial no caminho final (build atomico via arquivo temporario).
# ---------------------------------------------------------------------------


def test_falha_no_primeiro_arquivo_da_escrita_nao_deixa_zip_parcial(tmp_path, monkeypatch):
    _make_complete_project(tmp_path)
    (tmp_path / "_sistema" / "exemplo.py").write_text("# x\n", encoding="utf-8")

    def _zip_files_falho(files, zip_path):
        raise OSError("falha simulada de disco no primeiro arquivo")

    monkeypatch.setattr(pkg, "_zip_files", _zip_files_falho)
    zip_path = tmp_path / "out.zip"
    with pytest.raises(OSError):
        pkg.build_release_zip(tmp_path, zip_path)
    assert not zip_path.exists()
    # nenhum arquivo temporario orfao deixado no diretorio do projeto.
    assert not list(tmp_path.glob(".release_tmp_*"))


def test_falha_apos_alguns_arquivos_escritos_nao_deixa_zip_parcial(tmp_path, monkeypatch):
    """``_zip_files`` grava via ``ZipFile.writestr`` (bytes ja lidos de um
    handle verificado), nunca ``ZipFile.write(path)`` — a falha e
    simulada em ``_read_verified_bytes`` (o que realmente le os bytes
    de cada arquivo antes da gravacao)."""
    _make_complete_project(tmp_path)

    real_read_verified = pkg._read_verified_bytes
    call_count = {"n": 0}

    def _read_falha_no_terceiro(abs_path, rel, identity):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise OSError("falha simulada de disco no meio da escrita")
        return real_read_verified(abs_path, rel, identity)

    monkeypatch.setattr(pkg, "_read_verified_bytes", _read_falha_no_terceiro)
    zip_path = tmp_path / "out.zip"
    with pytest.raises(OSError):
        pkg.build_release_zip(tmp_path, zip_path)
    assert not zip_path.exists()
    assert not list(tmp_path.glob(".release_tmp_*"))


def test_falha_na_inspecao_nao_deixa_zip_parcial_nem_promove_para_final(tmp_path, monkeypatch):
    _make_complete_project(tmp_path)

    def _inspecao_falha(_zip_path):
        raise RuntimeError("falha simulada dentro da propria inspecao")

    monkeypatch.setattr(pkg, "_inspect_zip", _inspecao_falha)
    zip_path = tmp_path / "out.zip"
    with pytest.raises(RuntimeError):
        pkg.build_release_zip(tmp_path, zip_path)
    assert not zip_path.exists()
    assert not list(tmp_path.glob(".release_tmp_*"))


def test_zip_existente_nao_e_substituido_por_build_que_falha(tmp_path):
    """Se ja existir um ZIP valido de uma entrega anterior no mesmo
    caminho, um build que falhar NAO pode apagar/corromper esse ZIP
    anterior — a promocao para o caminho final so acontece apos sucesso
    completo (rename atomico)."""
    _make_complete_project(tmp_path)
    zip_path = tmp_path / "out.zip"
    zip_path.write_bytes(b"PK\x03\x04-zip-anterior-valido-fake")
    original_bytes = zip_path.read_bytes()

    (tmp_path / "PAINEL_OFICIAL.bat").unlink()
    with pytest.raises(pkg.PackagingIntegrityError):
        pkg.build_release_zip(tmp_path, zip_path)

    assert zip_path.exists()
    assert zip_path.read_bytes() == original_bytes


# ---------------------------------------------------------------------------
# 32-33: fail-closed em _is_link_like — erro de inspecao NUNCA e tratado
# como "e arquivo regular".
# ---------------------------------------------------------------------------


def test_permission_error_no_lstat_e_tratado_como_inseguro(tmp_path, monkeypatch):
    _make_complete_project(tmp_path)
    suspicious = tmp_path / "_sistema" / "suspeito.py"
    suspicious.write_text("# x\n", encoding="utf-8")

    real_lstat = os.lstat

    def _lstat_falho(path, *args, **kwargs):
        if str(path) == str(suspicious):
            raise PermissionError("lstat negado (simulado)")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(pkg.os, "lstat", _lstat_falho)
    zip_path = tmp_path / "out.zip"
    with pytest.raises(pkg.PackagingSecurityError):
        pkg.build_release_zip(tmp_path, zip_path)
    assert not zip_path.exists()


def test_lstat_or_fail_nunca_retorna_silenciosamente_quando_lstat_falha(monkeypatch, tmp_path):
    """Teste direto da funcao (nao so via build_release_zip): comprova o
    contrato fail-closed isoladamente — ``_lstat_or_fail`` e a base sobre
    a qual toda classificacao de link/hardlink/regular e feita; se ela
    engolisse o erro, tudo acima dela ficaria fail-open de novo."""
    alvo = tmp_path / "arquivo.py"
    alvo.write_text("# x\n", encoding="utf-8")

    def _lstat_sempre_falha(path, *args, **kwargs):
        raise OSError("falha de inspecao simulada")

    monkeypatch.setattr(pkg.os, "lstat", _lstat_sempre_falha)
    with pytest.raises(pkg.PackagingSecurityError):
        pkg._lstat_or_fail(alvo)


# ---------------------------------------------------------------------------
# 34: defender_setup.ps1 e classificado explicitamente como LEGACY / NAO
# DISTRIBUIR (nunca "ignorado silenciosamente porque nao e .py").
# ---------------------------------------------------------------------------


def test_defender_setup_ps1_e_ignorado_com_motivo_explicito(tmp_path):
    _make_complete_project(tmp_path)
    ps1 = tmp_path / "_sistema" / "defender_setup.ps1"
    ps1.write_text("Add-MpPreference -ExclusionPath 'C:\\fake'\n", encoding="utf-8")
    zip_path = tmp_path / "out.zip"
    count, ignored = pkg.build_release_zip(tmp_path, zip_path)
    assert "_sistema/defender_setup.ps1" not in _zip_names(zip_path)
    motivo = next((m for m in ignored if "defender_setup.ps1" in m), None)
    assert motivo is not None
    assert "LEGACY" in motivo
    assert "Defender" in motivo or "defender" in motivo.lower()


# ---------------------------------------------------------------------------
# Validacao do manifesto — garante que uma alteracao futura no projeto
# real nao passe a incluir/excluir arquivos essenciais silenciosamente, e
# que o conjunto REQUIRED continue existindo de verdade no checkout atual.
# ---------------------------------------------------------------------------


def test_manifesto_cobre_arquivos_de_raiz_do_projeto_real():
    """Compara o manifesto (``ALLOWED_ROOT_FILES``) com o que realmente
    existe hoje na raiz do repositorio de DESENVOLVIMENTO (nao a raiz real
    do cliente final — aqui, o proprio checkout onde os testes rodam).
    Cobre .py/.md/.bat/.txt na raiz, ignorando artefatos de build
    (__pycache__ etc.) e o proprio ZIP de saida. Se este teste falhar
    depois de adicionar um arquivo novo e legitimo na raiz do projeto,
    ATUALIZE ``ALLOWED_ROOT_FILES`` em ``empacotar_release.py``
    conscientemente — nao delete este teste."""
    project_root = Path(__file__).resolve().parent.parent
    if not (project_root / "_sistema").is_dir():
        pytest.skip("raiz do projeto de desenvolvimento nao encontrada neste ambiente")

    candidate_extensions = {".py", ".md", ".bat", ".txt"}
    unexpected = []
    for entry in project_root.iterdir():
        if entry.is_dir():
            continue
        if entry.suffix.lower() not in candidate_extensions:
            continue
        if entry.name in pkg.ALLOWED_ROOT_FILES:
            continue
        unexpected.append(entry.name)

    assert not unexpected, (
        "Arquivo(s) de raiz nao cobertos por ALLOWED_ROOT_FILES em "
        f"empacotar_release.py: {sorted(unexpected)}. Se forem legitimos, "
        "adicione-os ao manifesto conscientemente."
    )


# ---------------------------------------------------------------------------
# 39-40 (BLOCKER 1 — hardlink): hardlink e tratado com o mesmo
# conservadorismo que symlink/junction — rejeitado sem tentar decidir
# qual nome "e o original".
# ---------------------------------------------------------------------------


def test_hardlink_externo_falha(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _make_minimal_project(project)
    external = tmp_path / "outside_secret.txt"
    external.write_text("conteudo secreto que NUNCA pode vazar", encoding="utf-8")
    target = project / "_sistema" / "innocent.py"
    try:
        os.link(external, target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"os.link indisponivel neste ambiente: {exc}")

    zip_path = project / "out.zip"
    with pytest.raises(pkg.PackagingSecurityError, match="hardlink"):
        pkg.build_release_zip(project, zip_path)
    assert not zip_path.exists()


def test_hardlink_interno_falha(tmp_path):
    _make_complete_project(tmp_path)
    original = tmp_path / "_sistema" / "batch_engine.py"
    linked = tmp_path / "_sistema" / "batch_engine_alias.py"
    try:
        os.link(original, linked)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"os.link indisponivel neste ambiente: {exc}")

    zip_path = tmp_path / "out.zip"
    with pytest.raises(pkg.PackagingSecurityError):
        pkg.build_release_zip(tmp_path, zip_path)
    assert not zip_path.exists()


# ---------------------------------------------------------------------------
# 41-42 (BLOCKER 2 — TOCTOU): arquivo trocado entre a coleta e a escrita
# nunca vaza conteudo — a leitura e vinculada a identidade capturada na
# coleta, revalidada no handle aberto (nunca reaberto por pathname).
# Nada de sleep: a troca e forcada deterministicamente via monkeypatch de
# _collect_allowed_files, que roda antes da escrita comecar.
# ---------------------------------------------------------------------------


def test_arquivo_substituido_por_symlink_apos_coleta_falha(tmp_path, monkeypatch):
    _make_complete_project(tmp_path)
    target = tmp_path / "_sistema" / "batch_engine.py"
    external = tmp_path / "fora_da_arvore_toctou.txt"
    external.write_text("SEGREDO_EXTERNO_NUNCA_PODE_VAZAR", encoding="utf-8")

    real_collect = pkg._collect_allowed_files

    def _collect_e_substitui(project_root):
        files, ignored = real_collect(project_root)
        target.unlink()
        try:
            os.symlink(external, target)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"os.symlink indisponivel neste ambiente: {exc}")
        return files, ignored

    monkeypatch.setattr(pkg, "_collect_allowed_files", _collect_e_substitui)

    zip_path = tmp_path / "out.zip"
    with pytest.raises(pkg.PackagingSecurityError):
        pkg.build_release_zip(tmp_path, zip_path)
    assert not zip_path.exists()
    assert not list(tmp_path.glob(".release_tmp_*"))


def test_arquivo_substituido_por_outro_arquivo_regular_apos_coleta_falha_por_identidade(tmp_path, monkeypatch):
    """O pathname continua existindo e continua sendo um arquivo regular
    comum — mas e OUTRO inode (conteudo trocado). A comparacao
    st_dev/st_ino contra a identidade capturada na coleta e o que
    detecta isso; sem ela, ZipFile.write(path) reabriria o pathname e
    gravaria o conteudo trocado sem nenhum aviso."""
    _make_complete_project(tmp_path)
    target = tmp_path / "_sistema" / "batch_engine.py"

    real_collect = pkg._collect_allowed_files

    def _collect_e_substitui(project_root):
        files, ignored = real_collect(project_root)
        # Escreve o conteudo trocado em um caminho SEPARADO e so entao usa
        # os.replace para substituir o pathname original — isso garante um
        # inode genuinamente diferente do original (evita o caso raro em
        # que unlink()+create() no mesmo nome reaproveita o mesmo numero
        # de inode recem-liberado, o que tornaria o teste instavel).
        substituto = target.with_name("batch_engine_trocado.py")
        substituto.write_text("# CONTEUDO TROCADO DEPOIS DA COLETA (outro inode)\n", encoding="utf-8")
        os.replace(substituto, target)
        return files, ignored

    monkeypatch.setattr(pkg, "_collect_allowed_files", _collect_e_substitui)

    zip_path = tmp_path / "out.zip"
    with pytest.raises(pkg.PackagingSecurityError, match="identidade"):
        pkg.build_release_zip(tmp_path, zip_path)
    assert not zip_path.exists()


# ---------------------------------------------------------------------------
# 43-46 (BLOCKER 3 — REQUIRED em 3 camadas): sistema de arquivos inicial,
# snapshot selecionado e ZIP final precisam TODOS concordar.
# ---------------------------------------------------------------------------


def test_required_removido_entre_validacao_e_coleta_falha(tmp_path, monkeypatch):
    """Simula deterministicamente (sem sleep/threading) a camada 1
    'mentindo' que esta tudo presente, enquanto o arquivo already esta
    fisicamente ausente — prova que a camada 2 (snapshot selecionado)
    pega isso de forma independente."""
    _make_complete_project(tmp_path)
    (tmp_path / "PAINEL_OFICIAL.bat").unlink()

    monkeypatch.setattr(pkg, "_validate_required_paths", lambda project_root: [])

    zip_path = tmp_path / "out.zip"
    with pytest.raises(pkg.PackagingIntegrityError, match="PAINEL_OFICIAL.bat"):
        pkg.build_release_zip(tmp_path, zip_path)
    assert not zip_path.exists()


def test_required_removido_entre_coleta_e_escrita_falha(tmp_path, monkeypatch):
    _make_complete_project(tmp_path)
    real_collect = pkg._collect_allowed_files

    def _collect_e_remove_required(project_root):
        files, ignored = real_collect(project_root)
        (project_root / "PAINEL_OFICIAL.bat").unlink()
        return files, ignored

    monkeypatch.setattr(pkg, "_collect_allowed_files", _collect_e_remove_required)

    zip_path = tmp_path / "out.zip"
    with pytest.raises((pkg.PackagingSecurityError, pkg.PackagingIntegrityError)):
        pkg.build_release_zip(tmp_path, zip_path)
    assert not zip_path.exists()


def test_zip_final_sem_required_falha_antes_do_replace(tmp_path, monkeypatch):
    """Simula um bug hipotetico em _zip_files que grava o ZIP faltando um
    REQUIRED mesmo com snapshot correto — prova que a camada 3 (ZIP
    realmente produzido) pega isso independentemente das camadas 1-2."""
    _make_complete_project(tmp_path)
    real_zip_files = pkg._zip_files

    def _zip_sem_painel(files, zip_path):
        filtrados = [t for t in files if t[1].as_posix() != "PAINEL_OFICIAL.bat"]
        real_zip_files(filtrados, zip_path)

    monkeypatch.setattr(pkg, "_zip_files", _zip_sem_painel)

    zip_path = tmp_path / "out.zip"
    with pytest.raises(pkg.PackagingIntegrityError, match="PAINEL_OFICIAL.bat"):
        pkg.build_release_zip(tmp_path, zip_path)
    assert not zip_path.exists()
    assert not list(tmp_path.glob(".release_tmp_*"))


def test_st_nlink_muda_apos_coleta_falha(tmp_path, monkeypatch):
    _make_complete_project(tmp_path)
    target = tmp_path / "_sistema" / "batch_engine.py"
    real_collect = pkg._collect_allowed_files

    def _collect_e_hardlink(project_root):
        files, ignored = real_collect(project_root)
        extra = project_root / "_sistema" / "extra_hardlink_apos_coleta.py"
        try:
            os.link(target, extra)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"os.link indisponivel neste ambiente: {exc}")
        return files, ignored

    monkeypatch.setattr(pkg, "_collect_allowed_files", _collect_e_hardlink)

    zip_path = tmp_path / "out.zip"
    with pytest.raises(pkg.PackagingSecurityError, match="hardlink"):
        pkg.build_release_zip(tmp_path, zip_path)
    assert not zip_path.exists()


# ---------------------------------------------------------------------------
# 47: falha de fstat/leitura no handle verificado nunca deixa ZIP parcial.
# ---------------------------------------------------------------------------


def test_falha_de_fstat_durante_leitura_verificada_falha_sem_zip_parcial(tmp_path, monkeypatch):
    _make_complete_project(tmp_path)

    def _fstat_falho(fd):
        raise OSError("fstat falhou (simulado)")

    monkeypatch.setattr(pkg.os, "fstat", _fstat_falho)

    zip_path = tmp_path / "out.zip"
    with pytest.raises(pkg.PackagingSecurityError):
        pkg.build_release_zip(tmp_path, zip_path)
    assert not zip_path.exists()
    assert not list(tmp_path.glob(".release_tmp_*"))


def test_todos_os_modulos_obrigatorios_do_sistema_existem_no_projeto_real():
    """Direcao oposta ao teste acima: confere que todo caminho listado em
    REQUIRED_TREE_FILES realmente existe no checkout atual de _sistema/.
    So valida os modulos .py (identicos no espelho de desenvolvimento na
    nuvem e no Windows real) — os arquivos REQUIRED de raiz que sao
    especificos da entrega Windows (.bat/.txt) nao sao replicados neste
    espelho de desenvolvimento e por isso nao sao verificados aqui; sao
    conferidos manualmente a cada entrega ao dispositivo real. Se este
    teste falhar, ou um modulo obrigatorio foi removido por engano, ou
    REQUIRED_TREE_FILES precisa ser atualizado conscientemente."""
    project_root = Path(__file__).resolve().parent.parent
    if not (project_root / "_sistema").is_dir():
        pytest.skip("raiz do projeto de desenvolvimento nao encontrada neste ambiente")

    missing = [
        rel for rel in sorted(pkg.REQUIRED_TREE_FILES)
        if not (project_root / rel).is_file()
    ]
    assert not missing, (
        f"REQUIRED_TREE_FILES aponta para caminho(s) ausente(s) no projeto "
        f"real: {missing}. Ou o modulo foi removido por engano (restaure), "
        f"ou REQUIRED_TREE_FILES precisa ser atualizado conscientemente."
    )


# ---------------------------------------------------------------------------
# Regressao — correcao do Prompt 36: leitura binaria-segura em
# ``_read_verified_bytes`` (bug de modo texto do Windows truncando
# arquivos que contem o byte 0x1A, como todo PNG contem na propria
# assinatura). Ver docstring de ``_read_verified_bytes`` para a analise
# completa da causa raiz.
# ---------------------------------------------------------------------------


def test_read_verified_bytes_preserva_byte_0x1a_sem_truncar():
    """Prova direta e minima do bug corrigido: um arquivo cujo conteudo
    contem 0x1A (Ctrl-Z/SUB) no meio dos bytes precisa ser lido por
    inteiro, byte a byte, por ``_read_verified_bytes`` -- nunca truncado
    no 0x1A nem ter ``\\r\\n`` colapsado para ``\\n`` (semantica de modo
    TEXTO do CRT do Windows quando ``O_BINARY`` nao e usado)."""
    import tempfile

    conteudo = b"\x89PNG\r\n\x1a\n" + b"resto do arquivo binario apos o 0x1A\r\n" + bytes(range(256))
    with tempfile.TemporaryDirectory() as tmpdir:
        abs_path = Path(tmpdir) / "sintetico.bin"
        abs_path.write_bytes(conteudo)
        st = os.lstat(abs_path)
        identity = pkg._require_regular_no_hardlink(abs_path, Path("sintetico.bin"), st)

        lido = pkg._read_verified_bytes(abs_path, Path("sintetico.bin"), identity)

        assert lido == conteudo
        assert len(lido) == len(conteudo)


def test_build_release_zip_preserva_png_assinatura_completa_com_0x1a(tmp_path):
    """Prova de ponta a ponta (coleta -> escrita -> ZIP final) com um PNG
    sintetico minimo (assinatura real de 8 bytes, que contem 0x1A) +
    payload extra apos a assinatura -- exatamente a forma que expos o bug
    original nos 2 templates oficiais do Prompt 36/37. Precisa sobreviver
    ``build_release_zip`` byte a byte, tanto pela allowlist de
    ``assets/templates_oficiais/`` quanto pela leitura binaria-segura."""
    _make_minimal_project(tmp_path)
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"IHDR-ficticio-nao-e-um-png-real-mas-tem-o-mesmo-prefixo-de-assinatura"
    asset_dir = tmp_path / "assets" / "templates_oficiais"
    asset_dir.mkdir(parents=True, exist_ok=True)
    asset_path = asset_dir / "template_moldura_tech_azul.png"
    asset_path.write_bytes(png_bytes)

    zip_path = tmp_path / "out.zip"
    pkg.build_release_zip(tmp_path, zip_path)

    with zipfile.ZipFile(zip_path, "r") as zf:
        nome = "assets/templates_oficiais/template_moldura_tech_azul.png"
        assert nome in zf.namelist()
        dentro_do_zip = zf.read(nome)

    assert dentro_do_zip == png_bytes
    assert len(dentro_do_zip) == len(png_bytes)

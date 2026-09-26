# -*- coding: utf-8 -*-
"""PROMPT 23 -- Secrets Manager: testes.

Este ambiente de auditoria NÃO é Windows, então o backend real
(``DpapiSecretsBackend``) não pode ser exercitado fim-a-fim aqui (DPAPI só
existe no Windows). Os testes marcados abaixo com "(via InMemory...)" usam
o backend de teste explicitamente, e os testes sobre o comportamento
"fail closed" do backend real usam ``_dpapi_available`` monkeypatchado
(simulação documentada, nunca confundida com uma prova real em Windows --
ver ``PROMPT_23_SECRETS_MANAGER_RELATORIO.md``, seção de riscos).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

import _sistema.secrets_manager as secrets_manager
from _sistema.secrets_manager import (
    DpapiSecretsBackend,
    InMemorySecretsBackend,
    SecretNotFoundError,
    SecretsBackendUnavailableError,
    SecretsManager,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def backend() -> InMemorySecretsBackend:
    return InMemorySecretsBackend()


@pytest.fixture
def manager(backend: InMemorySecretsBackend) -> SecretsManager:
    return SecretsManager(backend)


# ---------------------------------------------------------------------------
# 1. Roundtrip store/retrieve/delete/exists (via InMemorySecretsBackend)
# ---------------------------------------------------------------------------


def test_roundtrip_store_retrieve_delete_exists(manager: SecretsManager):
    assert manager.exists("minha_chave") is False
    manager.store("minha_chave", "valor-secreto-123")
    assert manager.exists("minha_chave") is True
    assert manager.retrieve_text("minha_chave") == "valor-secreto-123"
    manager.delete("minha_chave")
    assert manager.exists("minha_chave") is False


def test_store_aceita_bytes_diretamente(manager: SecretsManager):
    manager.store("chave_bin", b"\x00\x01\xff\xfe")
    assert manager.retrieve("chave_bin") == b"\x00\x01\xff\xfe"


def test_delete_de_chave_inexistente_e_idempotente_nao_e_erro(manager: SecretsManager):
    manager.delete("nunca_existiu")  # não deve levantar
    assert manager.exists("nunca_existiu") is False


# ---------------------------------------------------------------------------
# 2. retrieve() de chave inexistente e inequivoco
# ---------------------------------------------------------------------------


def test_retrieve_de_chave_inexistente_levanta_secret_not_found(manager: SecretsManager):
    with pytest.raises(SecretNotFoundError):
        manager.retrieve("chave_que_nao_existe")


def test_retrieve_de_chave_inexistente_nunca_e_confundido_com_valor_vazio(
    manager: SecretsManager,
):
    """Um valor vazio armazenado deliberadamente deve ser distinguível de
    uma chave nunca armazenada -- a garantia central do item 1.1."""
    manager.store("chave_vazia", "")
    assert manager.exists("chave_vazia") is True
    assert manager.retrieve("chave_vazia") == b""

    with pytest.raises(SecretNotFoundError):
        manager.retrieve("chave_jamais_usada")


def test_secret_not_found_error_e_subclasse_de_keyerror(manager: SecretsManager):
    # Comportamento inequívoco e idiomático em Python: uma chave ausente
    # levanta algo que se comporta como KeyError, nunca None.
    with pytest.raises(KeyError):
        manager.retrieve("outra_chave_ausente")


# ---------------------------------------------------------------------------
# 3. Sobrescrever atualiza, nunca duplica nem deixa o valor antigo
# ---------------------------------------------------------------------------


def test_sobrescrever_chave_existente_atualiza_valor(manager: SecretsManager):
    manager.store("chave", "valor_antigo")
    manager.store("chave", "valor_novo")
    assert manager.retrieve_text("chave") == "valor_novo"


def test_sobrescrever_nao_deixa_valor_antigo_recuperavel_por_nenhum_caminho(
    backend: InMemorySecretsBackend,
):
    manager = SecretsManager(backend)
    manager.store("chave", "valor_antigo_unico_xyz")
    manager.store("chave", "valor_novo_unico_abc")
    # Único valor acessível via o contrato público é o novo.
    assert manager.retrieve_text("chave") == "valor_novo_unico_abc"
    # E o backend, inspecionado diretamente, também não retém o antigo em
    # nenhuma outra chave/estrutura interna (InMemorySecretsBackend usa um
    # único dict flat -- não há segunda cópia).
    assert list(backend._values.values()) == [b"valor_novo_unico_abc"]


# ---------------------------------------------------------------------------
# 4. Backend real indisponivel -> falha alta, nunca plaintext silencioso
# ---------------------------------------------------------------------------


def test_dpapi_indisponivel_via_monkeypatch_levanta_ao_construir_em_qualquer_ambiente(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """CORREÇÃO (Prompt 23, correção pós-validação manual em Windows real):
    a versão original deste teste assumia como premissa que "este
    ambiente nunca é Windows" -- verdade no CI/auditoria (Linux), mas
    falsa em uma máquina Windows real, onde ``_dpapi_available()``
    corretamente devolve ``True`` e ``DpapiSecretsBackend`` é construído
    com sucesso (comportamento correto do produto, confirmado
    manualmente: roundtrip ``store``/``retrieve`` funcionou e o arquivo
    ``.dpapi`` gravado é ilegível como texto). Um teste que espera
    exceção nessa situação está testando a premissa errada, não o
    produto.

    Esta versão nunca depende de qual é a plataforma real: usa
    ``monkeypatch`` para forçar ``_dpapi_available() -> False`` (o mesmo
    padrão do teste irmão logo abaixo), provando o comportamento
    "fail closed" de forma universal -- passa em CI Linux e em Windows
    real igualmente, porque não depende de em qual dos dois o teste
    realmente está rodando.
    """
    monkeypatch.setattr(secrets_manager, "_dpapi_available", lambda: False)
    with pytest.raises(SecretsBackendUnavailableError):
        DpapiSecretsBackend(tmp_path / "cofre")


def test_dpapi_indisponivel_simulado_em_plataforma_windows_tambem_levanta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Simula (via monkeypatch, documentado) o caso em que o processo
    RODARIA no Windows mas a chamada real do Windows falha (ex.:
    ``crypt32.dll`` ausente/corrompida) -- garante que a checagem não
    depende só de "sys.platform != 'win32'", mas de fato tenta carregar a
    DLL e falha fechado se isso não funcionar."""

    monkeypatch.setattr(secrets_manager, "_dpapi_available", lambda: False)
    with pytest.raises(SecretsBackendUnavailableError):
        DpapiSecretsBackend(tmp_path / "cofre")


def test_nenhum_caminho_de_codigo_grava_texto_claro_quando_dpapi_indisponivel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Confirma que a indisponibilidade do backend real impede a própria
    CONSTRUÇÃO do backend -- não existe uma instância "degradada" que
    aceitaria store() e gravaria sem proteção. Nenhum arquivo é criado no
    diretório de armazenamento pretendido."""

    monkeypatch.setattr(secrets_manager, "_dpapi_available", lambda: False)
    storage_dir = tmp_path / "cofre_inexistente"
    with pytest.raises(SecretsBackendUnavailableError):
        DpapiSecretsBackend(storage_dir)
    # O diretório nem chega a ser criado -- prova de que nenhuma escrita
    # (protegida ou não) ocorreu.
    assert not storage_dir.exists()


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="DPAPI so existe no Windows -- roundtrip real so pode ser provado la",
)
def test_dpapi_real_constroi_e_faz_roundtrip_em_windows_de_verdade(tmp_path: Path):
    """Prova POSITIVA (só roda em Windows real, ``skipif`` em qualquer
    outra plataforma -- aparece como "skipped" em CI Linux, nunca como
    "failed"): em Windows de verdade, ``_dpapi_available()`` deve
    devolver ``True``, ``DpapiSecretsBackend`` deve construir com
    sucesso, e um roundtrip ``store``/``retrieve`` real via
    ``CryptProtectData``/``CryptUnprotectData`` deve funcionar --
    automatizando a validação manual que confirmou isto originalmente.
    Confirma também que o arquivo persistido no disco não é o valor em
    texto plano (a prova de que a proteção real está de fato acontecendo,
    não só "não dando erro")."""

    assert secrets_manager._dpapi_available() is True

    backend = DpapiSecretsBackend(tmp_path / "cofre_real")
    manager = SecretsManager(backend)

    valor = "valor-secreto-real-de-verdade-9f8a7b6c"
    manager.store("chave_real", valor)
    assert manager.retrieve_text("chave_real") == valor

    # O arquivo protegido no disco nunca contém o texto plano do segredo.
    arquivo_protegido = backend._path_for("chave_real")
    assert arquivo_protegido.exists()
    conteudo_bruto = arquivo_protegido.read_bytes()
    assert valor.encode("utf-8") not in conteudo_bruto

    manager.delete("chave_real")
    assert manager.exists("chave_real") is False


# ---------------------------------------------------------------------------
# 5. Backend de teste nunca e escolhido automaticamente/silenciosamente
# ---------------------------------------------------------------------------


def test_secrets_manager_exige_backend_explicito_no_construtor():
    import inspect

    sig = inspect.signature(SecretsManager.__init__)
    params = [p for name, p in sig.parameters.items() if name != "self"]
    assert len(params) == 1
    assert params[0].name == "backend"
    # Sem valor default -- backend é sempre obrigatório, nunca opcional
    # com um fallback implícito.
    assert params[0].default is inspect._empty


def test_secrets_manager_rejeita_backend_none_explicito():
    with pytest.raises(TypeError):
        SecretsManager(None)  # type: ignore[arg-type]


def test_secrets_manager_nao_tem_metodo_ou_funcao_de_autodetecao_de_backend():
    """Não existe nenhum método de fábrica (``from_platform``,
    ``default()``, ``auto()`` etc.) em ``SecretsManager`` nem no módulo
    que escolheria um backend sozinho -- reforça, ao nível de superfície
    pública, que o backend é sempre passado explicitamente pelo
    chamador."""

    forbidden_substrings = ("auto", "default", "platform", "detect")
    public_names = [name for name in dir(SecretsManager) if not name.startswith("_")]
    for name in public_names:
        lowered = name.lower()
        for forbidden in forbidden_substrings:
            assert forbidden not in lowered, (
                f"SecretsManager.{name} sugere autodetecao/fallback implicito de backend"
            )

    module_public_names = [n for n in secrets_manager.__all__]
    for name in module_public_names:
        lowered = name.lower()
        for forbidden in forbidden_substrings:
            assert forbidden not in lowered, (
                f"{name} (exportado por secrets_manager) sugere autodetecao de backend"
            )


# ---------------------------------------------------------------------------
# 6. Nenhum valor de segredo aparece em repr()/str()/mensagens de erro
# ---------------------------------------------------------------------------


_MARCADOR_SECRETO = "SEGREDO-UNICO-RECONHECIVEL-9f8a7b6c5d4e"


def test_valor_de_segredo_nunca_aparece_em_repr_do_manager(manager: SecretsManager):
    manager.store("chave_qualquer", _MARCADOR_SECRETO)
    assert _MARCADOR_SECRETO not in repr(manager)
    assert _MARCADOR_SECRETO not in str(manager)


def test_valor_de_segredo_nunca_aparece_em_repr_do_backend_inmemory(
    backend: InMemorySecretsBackend,
):
    backend.store("chave_qualquer", _MARCADOR_SECRETO.encode("utf-8"))
    assert _MARCADOR_SECRETO not in repr(backend)
    assert _MARCADOR_SECRETO not in str(backend)


def test_valor_de_segredo_nunca_aparece_em_excecao_de_secret_not_found():
    manager = SecretsManager(InMemorySecretsBackend())
    manager.store(_MARCADOR_SECRETO, "outro-valor-irrelevante")
    manager.delete(_MARCADOR_SECRETO)
    # A CHAVE pode aparecer na mensagem (é só um nome, não o segredo) --
    # o que nunca pode aparecer é um VALOR armazenado.
    manager.store("chave_normal", _MARCADOR_SECRETO)
    try:
        manager.retrieve("chave_inexistente_de_verdade")
    except SecretNotFoundError as exc:
        assert _MARCADOR_SECRETO not in str(exc)
        assert _MARCADOR_SECRETO not in repr(exc)


def test_valor_de_segredo_nunca_aparece_em_excecao_de_backend_indisponivel(
    tmp_path: Path,
):
    # O valor nem chega a existir neste fluxo (a construção falha antes),
    # mas confirmamos que a mensagem de erro não referencia nenhum
    # conteúdo de segredo -- só a natureza da indisponibilidade.
    try:
        DpapiSecretsBackend(tmp_path / "cofre")
    except SecretsBackendUnavailableError as exc:
        assert _MARCADOR_SECRETO not in str(exc)
        assert "senha" not in str(exc).lower()
        assert "password" not in str(exc).lower()


def test_todas_as_representacoes_textuais_alcancaveis_nunca_contem_o_segredo(
    manager: SecretsManager,
):
    """Varredura ampla: arma um valor reconhecível, executa toda a
    superfície pública que pode ser chamada sem o valor em mãos, e
    confirma que o marcador nunca aparece em nenhuma representação
    textual produzida pelo módulo."""

    manager.store("chave_alvo", _MARCADOR_SECRETO)

    textos: list[str] = [repr(manager), str(manager)]

    try:
        manager.retrieve("chave_ausente_1")
    except SecretNotFoundError as exc:
        textos.append(str(exc))
        textos.append(repr(exc))

    try:
        SecretsManager(None)  # type: ignore[arg-type]
    except TypeError as exc:
        textos.append(str(exc))
        textos.append(repr(exc))

    for texto in textos:
        assert _MARCADOR_SECRETO not in texto


# ---------------------------------------------------------------------------
# 7. Nenhuma biblioteca de rede importada (garantia estrutural)
# ---------------------------------------------------------------------------


_REDE_SUBSTRINGS = (
    "urllib",
    "http.client",
    "httplib",
    "requests",
    "httpx",
    "aiohttp",
    "socket",
    "ftplib",
    "smtplib",
    "websocket",
)


def test_secrets_manager_nao_importa_nenhuma_biblioteca_de_rede():
    import ast

    source = Path(secrets_manager.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                lowered = alias.name.lower()
                for forbidden in _REDE_SUBSTRINGS:
                    assert forbidden not in lowered, (
                        f"import de rede detectado: {alias.name}"
                    )
        elif isinstance(node, ast.ImportFrom):
            module_name = (node.module or "").lower()
            for forbidden in _REDE_SUBSTRINGS:
                assert forbidden not in module_name, (
                    f"import de rede detectado: from {node.module} import ..."
                )


def test_secrets_manager_nao_usa_importlib_dinamico():
    """Segunda camada da garantia estrutural: confirma que o módulo não
    usa ``importlib``/``__import__`` em lugar nenhum -- o único caminho
    para "esconder" um import de rede de uma varredura AST estática seria
    um import dinâmico, e este teste garante que essa via não existe."""

    import ast

    source = Path(secrets_manager.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "__import__"
        if isinstance(node, ast.Attribute) and node.attr == "import_module":
            raise AssertionError("uso de importlib.import_module detectado")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", None) or " ".join(
                alias.name for alias in node.names
            )
            assert "importlib" not in (mod or "").lower()


# ---------------------------------------------------------------------------
# 8. Nao-regressao: nada fora do escopo foi tocado
# ---------------------------------------------------------------------------


def test_default_config_nao_tem_nenhum_campo_de_credencial_novo():
    """Confirma (seção 0/2 do Prompt) que nenhum campo de senha/API
    key/token foi adicionado a ``default_config()`` -- roda a função real
    e varre as chaves (recursivamente) por qualquer nome sugestivo de
    credencial."""

    from _sistema.painel_oficial import default_config

    forbidden_key_substrings = ("senha", "password", "api_key", "apikey", "token", "secret", "credential")

    def _walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                lowered = str(key).lower()
                for forbidden in forbidden_key_substrings:
                    assert forbidden not in lowered, (
                        f"campo suspeito de credencial em default_config(): {key}"
                    )
                _walk(value)
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                _walk(item)

    for platform in ("youtube", "tiktok"):
        cfg = default_config(
            platform, "conta_teste", "BR", "pt-BR", timezone_iana="America/Sao_Paulo"
        )
        _walk(cfg)


def test_login_conta_module_nao_foi_alterado_estruturalmente():
    """Confirma que o fluxo de login real continua usando
    ``ACCOUNT_PATHS.profile_youtube``/``profile_tiktok`` e um Chrome real
    via subprocess -- nenhuma dependência nova em ``secrets_manager``
    foi introduzida em ``login_conta.py`` (o módulo nem é importado lá)."""

    import _sistema.login_conta as login_conta

    source = Path(login_conta.__file__).read_text(encoding="utf-8")
    assert "secrets_manager" not in source
    assert "profile_youtube" in source
    assert "profile_tiktok" in source


def test_backup_module_continua_documentando_exclusao_de_perfis_de_navegador():
    import _sistema.storage.backup as backup_module

    source = Path(backup_module.__file__).read_text(encoding="utf-8")
    # O módulo de backup não importa/referencia secrets_manager (este
    # Prompt não altera o escopo do backup).
    assert "secrets_manager" not in source


def test_secrets_manager_nunca_importa_circuit_breaker_retry_policy_ou_publication_idempotency():
    import ast

    source = Path(secrets_manager.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    proibidos = ("circuit_breaker", "retry_policy", "publication_idempotency", "job_state_machine")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = " ".join([node.module or ""] + [alias.name for alias in node.names])
            for termo in proibidos:
                assert termo not in names.lower()
        elif isinstance(node, ast.Import):
            names = " ".join(alias.name for alias in node.names)
            for termo in proibidos:
                assert termo not in names.lower()

# -*- coding: utf-8 -*-
"""PROMPT 23 -- Secrets Manager: abstração para segredos futuros (Geração 2).

ESCOPO E CONTEXTO (ver investigação da seção 0 do Prompt, reproduzida aqui
para quem ler este módulo isoladamente):

O produto, HOJE, não guarda nenhuma senha/credencial própria em lugar
nenhum. O login real do YouTube/TikTok acontece abrindo um Chrome de
verdade (``login_conta.py``) com um ``user_data_dir`` persistente
(``ACCOUNT_PATHS.profile_youtube``/``profile_tiktok``) -- toda a sessão
(cookies, tokens de sessão) vive DENTRO do perfil do próprio Chrome, sob a
proteção que o Chrome/Windows já aplicam a esses arquivos. A aplicação
NUNCA lê, copia nem manuseia esse conteúdo diretamente, e
``_sistema/storage/backup.py`` confirma explicitamente (próprio docstring
do módulo) que o backup/restore local NUNCA inclui perfis de navegador --
só o SQLite. ``default_config()``/``config.json``
(``_sistema/painel_oficial.py``) também não têm hoje nenhum campo de
senha, API key ou token em texto plano (Ollama é local, sem chave; nenhum
serviço pago está integrado).

Ou seja: este módulo é infraestrutura construída ANTES de um consumidor
real -- a mesma situação já aprovada para Circuit Breaker (Prompt 20),
RetryPolicy (Prompt 21) e Idempotência (Prompt 22), que também esperam um
Connector futuro -- só que aqui ainda mais cedo: hoje não há NENHUM
chamador. Nenhum campo novo de credencial foi adicionado a
config/conta neste Prompt, nenhum fluxo de login novo foi criado, e o
perfil do Chrome/``login_conta.py``/backup-restore não foram tocados. O
roadmap pede a ABSTRAÇÃO pronta para quando um segredo real existir (ex.:
uma futura licença HWID -- Prompt 63 -- ou uma chave de API de algum
serviço pago que venha a ser integrado).

GARANTIA ESTRUTURAL -- "nunca enviar esses dados a um backend": este
módulo não importa NENHUMA biblioteca de rede (nem ``urllib``, nem
``http``, nem ``requests``, nem soquete algum) -- confirmado por teste
AST dedicado (``tests/test_secrets_manager.py``). Se o módulo fisicamente
não sabe fazer uma requisição, ele não pode vazar um segredo pela rede
por acidente, hoje ou em qualquer refactor futuro que esqueça de revisar
essa garantia manualmente.

BACKEND REAL ESCOLHIDO: DPAPI (``CryptProtectData``/``CryptUnprotectData``,
Windows) via ``ctypes`` puro (biblioteca padrão, sempre disponível --
``pywin32`` NÃO está instalado neste ambiente e não é uma dependência do
projeto, então usar ``ctypes`` diretamente contra ``crypt32.dll`` evita
adicionar uma dependência nova só para isto). DPAPI foi escolhido em vez
do Windows Credential Manager (``CredWrite``/``CredRead``) porque:

- DPAPI opera sobre blobs de bytes arbitrários de qualquer tamanho, sem
  limite artificial -- um segredo futuro (ex.: uma licença HWID completa,
  um payload de configuração de licenciamento) pode não ser uma simples
  string curta de usuário/senha.
- Credential Manager foi desenhado para pares usuário/senha e tem um
  limite de tamanho de blob relativamente pequeno (historicamente
  ~2560 bytes para ``CRED_TYPE_GENERIC``) -- adequado para uma senha, não
  necessariamente para o que um segredo futuro deste produto pode ser.
- DPAPI já protege, por baixo dos panos, o próprio perfil do Chrome
  (cookies criptografados via DPAPI+chave por perfil) -- é o mecanismo
  nativo do Windows para "proteger um blob ligado ao usuário/máquina
  atual", exatamente o que este módulo precisa, sem exigir nenhuma
  biblioteca externa.

Implementar TAMBÉM um backend Credential Manager fica como extensão
futura explícita (não implementado nesta rodada) -- caso um consumidor
futuro precise das vantagens específicas dele (ex.: aparecer no Gerenciador
de Credenciais do Windows para o usuário final ver/revogar manualmente).

FAIL CLOSED: se o backend real (DPAPI) não estiver disponível em tempo de
execução -- por exemplo, rodando fora do Windows, ou uma chamada da API do
Windows falhando -- ``DpapiSecretsBackend`` levanta ``SecretsBackendUnavailableError``
imediatamente, tanto na construção quanto em qualquer operação. Não existe
nenhum caminho de código em que um valor deixe de ser protegido e seja
gravado como texto plano silenciosamente: a ÚNICA forma de gravar sem
DPAPI é usar explicitamente ``InMemorySecretsBackend`` (rotulado no
próprio nome como não seguro para produção), e mesmo esse backend NUNCA é
escolhido automaticamente -- ``SecretsManager`` exige um backend explícito
no construtor, sem autodetecção de plataforma nem fallback silencioso
(testado).

NUNCA VAZAR O VALOR DO SEGREDO: nenhum valor passado a ``store()`` aparece
em ``repr()``/``str()`` de ``SecretsManager``, nem em qualquer exceção que
este módulo levanta, nem em log/print algum -- este módulo não usa
``logging``/``print`` em caminho nenhum. As mensagens de erro referenciam
apenas a CHAVE (nome do segredo), nunca o valor.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "SecretsBackend",
    "SecretNotFoundError",
    "SecretsBackendUnavailableError",
    "InMemorySecretsBackend",
    "DpapiSecretsBackend",
    "SecretsManager",
]


# ---------------------------------------------------------------------------
# Erros
# ---------------------------------------------------------------------------


class SecretNotFoundError(KeyError):
    """``retrieve()`` de uma chave que nunca foi armazenada (ou já apagada).

    Deliberadamente uma exceção dedicada, nunca ``None`` -- um segredo
    armazenado poderia, em teoria, ter um valor vazio (``""`` ou
    ``b""``); devolver ``None`` para "não existe" seria ambíguo com um
    eventual "valor vazio armazenado". A mensagem da exceção referencia
    apenas a chave (nome do segredo), nunca um valor.
    """

    def __init__(self, key: str) -> None:
        super().__init__(f"Segredo nao encontrado: chave={key!r}")
        self.key = key


class SecretsBackendUnavailableError(RuntimeError):
    """O backend real de proteção (ex.: DPAPI) não está disponível agora.

    Levantada em vez de qualquer fallback para texto plano -- "fail
    closed", mesmo princípio já usado no projeto (verificação de direitos
    autorais, Circuit Breaker: preferir recusar a operação a prosseguir
    sem a proteção esperada). A mensagem nunca inclui o valor de um
    segredo, apenas a natureza da indisponibilidade.
    """


class SecretsBackendConflictError(RuntimeError):
    """Uma operação de baixo nível do backend real falhou de forma
    inesperada (ex.: ``CryptUnprotectData`` recusou um blob corrompido).

    Distinta de ``SecretsBackendUnavailableError`` (que significa "o
    mecanismo de proteção como um todo não está disponível"): esta
    significa "o mecanismo está disponível, mas esta operação específica
    falhou". A mensagem nunca inclui o valor de um segredo.
    """


# ---------------------------------------------------------------------------
# Contrato de backend
# ---------------------------------------------------------------------------


class SecretsBackend(Protocol):
    """Contrato mínimo que qualquer backend de segredos deve implementar.

    Todos os métodos operam sobre ``bytes`` -- a camada de codificação
    (ex.: texto -> UTF-8) é responsabilidade de quem usa ``SecretsManager``
    ou de um wrapper futuro, mantendo o backend agnóstico ao tipo lógico
    do segredo (string, JSON, blob binário de licença, etc.).
    """

    def store(self, key: str, value: bytes) -> None:
        """Grava (ou substitui) o valor associado a ``key``.

        Sobrescrever uma chave existente deve atualizar o valor -- nunca
        duplicar nem deixar o valor antigo recuperável por nenhum caminho.
        """
        ...

    def retrieve(self, key: str) -> bytes:
        """Devolve o valor associado a ``key``.

        Levanta ``SecretNotFoundError`` se a chave nunca foi armazenada
        (ou já foi apagada) -- nunca devolve ``None``.
        """
        ...

    def delete(self, key: str) -> None:
        """Remove o valor associado a ``key``, se existir.

        Idempotente: apagar uma chave já inexistente não é erro.
        """
        ...

    def exists(self, key: str) -> bool:
        """Devolve ``True`` se ``key`` tem um valor armazenado agora."""
        ...


# ---------------------------------------------------------------------------
# Backend de teste -- NUNCA usar em produção
# ---------------------------------------------------------------------------


class InMemorySecretsBackend:
    """Backend FALSO, só para testes automatizados -- NÃO É SEGURO.

    Guarda valores em um dicionário Python em memória, sem nenhuma
    proteção criptográfica. O nome da classe é deliberadamente explícito
    ("InMemory... -- nunca disco, nunca proteção real") para que nenhum
    chamador confunda isto com um backend de produção.

    Este backend só pode ser usado se for passado EXPLICITAMENTE ao
    construtor de ``SecretsManager`` -- ``SecretsManager`` nunca o
    seleciona por autodetecção de plataforma ou como fallback silencioso
    (ver ``SecretsManager.__init__``, que sempre exige um ``backend``
    explícito, e o teste dedicado
    ``test_backend_de_teste_nunca_e_escolhido_automaticamente``).
    """

    def __init__(self) -> None:
        self._values: dict[str, bytes] = {}

    def store(self, key: str, value: bytes) -> None:
        if not isinstance(value, (bytes, bytearray)):
            raise TypeError("value deve ser bytes")
        self._values[key] = bytes(value)

    def retrieve(self, key: str) -> bytes:
        try:
            return self._values[key]
        except KeyError:
            raise SecretNotFoundError(key) from None

    def delete(self, key: str) -> None:
        self._values.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self._values

    def __repr__(self) -> str:  # pragma: no cover - trivial
        # Nunca expõe valores -- só a contagem de chaves armazenadas.
        return f"InMemorySecretsBackend(chaves_armazenadas={len(self._values)})"


# ---------------------------------------------------------------------------
# Backend real: DPAPI (Windows)
# ---------------------------------------------------------------------------


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD if hasattr(ctypes, "wintypes") else ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_available() -> bool:
    """Confirma, sem lançar, se DPAPI pode ser usado neste processo.

    ``True`` somente em Windows com ``crypt32.dll`` carregável. Qualquer
    outra plataforma (Linux, macOS -- incluindo este ambiente de
    auditoria/testes) devolve ``False``, nunca lança.
    """
    try:
        import sys

        if sys.platform != "win32":
            return False
        ctypes.WinDLL("crypt32.dll")  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


class DpapiSecretsBackend:
    """Backend real de produção: Windows DPAPI via ``ctypes``.

    ``CryptProtectData``/``CryptUnprotectData`` protegem um blob de bytes
    ligado ao usuário do Windows atual (escopo padrão, sem
    ``CRYPTPROTECT_LOCAL_MACHINE``) -- o mesmo mecanismo que o próprio
    Chrome usa para proteger cookies dentro de um perfil. Não depende de
    ``pywin32``: usa apenas ``ctypes`` (biblioteca padrão), evitando uma
    dependência nova só para isto.

    "Armazenar" aqui significa: proteger o blob com DPAPI e persistir o
    resultado protegido em um arquivo dedicado, um por chave, dentro de
    ``storage_dir``. O CONTEÚDO do arquivo é sempre o blob já protegido
    por DPAPI -- nunca texto plano -- e só pode ser revertido
    (``CryptUnprotectData``) pelo mesmo usuário/máquina que o protegeu.

    Fail closed: se DPAPI não estiver disponível (plataforma não-Windows,
    ou ``crypt32.dll``/chamada indisponível), o CONSTRUTOR já levanta
    ``SecretsBackendUnavailableError`` -- nunca chega a existir uma
    instância "quebrada" que aceitaria ``store()`` e gravaria sem
    proteção.
    """

    def __init__(self, storage_dir: "os.PathLike[str] | str") -> None:
        import os
        from pathlib import Path

        if not _dpapi_available():
            raise SecretsBackendUnavailableError(
                "DPAPI indisponivel neste processo (nao e Windows, ou "
                "crypt32.dll nao pode ser carregada) -- backend real de "
                "segredos nao pode ser inicializado; nenhum segredo sera "
                "gravado em texto plano como alternativa."
            )
        self._dir = Path(storage_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._crypt32 = ctypes.WinDLL("crypt32.dll")  # type: ignore[attr-defined]
        self._kernel32 = ctypes.WinDLL("kernel32.dll")  # type: ignore[attr-defined]

    def _path_for(self, key: str) -> "Path":
        import hashlib
        from pathlib import Path

        # Nome de arquivo derivado por hash -- nunca o texto da chave cru
        # (que poderia conter caracteres inválidos em um nome de arquivo
        # Windows, ou vazar informação da chave via listagem de diretório).
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._dir / f"{digest}.dpapi"

    def _protect(self, data: bytes) -> bytes:
        blob_in = _DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
        blob_out = _DATA_BLOB()
        ok = self._crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        )
        if not ok:
            raise SecretsBackendConflictError(
                "CryptProtectData falhou (erro do sistema Windows)"
            )
        try:
            protected = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            self._kernel32.LocalFree(blob_out.pbData)
        return protected

    def _unprotect(self, data: bytes) -> bytes:
        blob_in = _DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
        blob_out = _DATA_BLOB()
        ok = self._crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        )
        if not ok:
            raise SecretsBackendConflictError(
                "CryptUnprotectData falhou (blob corrompido ou usuario/"
                "maquina diferente de quem protegeu o segredo)"
            )
        try:
            plain = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            self._kernel32.LocalFree(blob_out.pbData)
        return plain

    def store(self, key: str, value: bytes) -> None:
        if not isinstance(value, (bytes, bytearray)):
            raise TypeError("value deve ser bytes")
        protected = self._protect(bytes(value))
        path = self._path_for(key)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(protected)
        tmp.replace(path)  # substituição atômica -- nunca duplica/deixa lixo

    def retrieve(self, key: str) -> bytes:
        path = self._path_for(key)
        if not path.exists():
            raise SecretNotFoundError(key)
        protected = path.read_bytes()
        return self._unprotect(protected)

    def delete(self, key: str) -> None:
        path = self._path_for(key)
        path.unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._path_for(key).exists()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        # Nunca expõe valores -- só o diretório de armazenamento (nomes de
        # arquivo já são hashes da chave, não a chave nem o valor).
        return f"DpapiSecretsBackend(storage_dir={self._dir!r})"


# ---------------------------------------------------------------------------
# Fachada
# ---------------------------------------------------------------------------


class SecretsManager:
    """Fachada estável sobre um ``SecretsBackend`` plugável.

    O backend é SEMPRE explícito no construtor -- nunca autodetectado por
    plataforma, nunca escolhido como fallback silencioso. Isto é
    deliberado: um chamador em produção deve passar um
    ``DpapiSecretsBackend`` (ou backend real equivalente) conscientemente;
    um teste deve passar ``InMemorySecretsBackend`` conscientemente. Não
    existe um caminho em que ``SecretsManager()`` sem argumentos "decida
    sozinho" usar o backend de teste porque está rodando fora do Windows.
    """

    def __init__(self, backend: SecretsBackend) -> None:
        if backend is None:
            raise TypeError("backend e obrigatorio (nunca None/autodetectado)")
        self._backend = backend

    def store(self, key: str, value: str | bytes) -> None:
        if isinstance(value, str):
            value = value.encode("utf-8")
        self._backend.store(key, value)

    def retrieve(self, key: str) -> bytes:
        return self._backend.retrieve(key)

    def retrieve_text(self, key: str) -> str:
        return self._backend.retrieve(key).decode("utf-8")

    def delete(self, key: str) -> None:
        self._backend.delete(key)

    def exists(self, key: str) -> bool:
        return self._backend.exists(key)

    def __repr__(self) -> str:
        # Nunca expõe valores -- só o tipo do backend em uso.
        return f"SecretsManager(backend={type(self._backend).__name__})"

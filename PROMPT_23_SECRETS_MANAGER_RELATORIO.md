# PROMPT 23 — SECRETS MANAGER — Relatório de Entrega

## 1. Arquivos criados

- `_sistema/secrets_manager.py` — `SecretsManager` (fachada),
  `SecretsBackend` (Protocol), `InMemorySecretsBackend` (backend de
  teste, rotulado no nome), `DpapiSecretsBackend` (backend real de
  produção via ctypes/DPAPI), `SecretNotFoundError`,
  `SecretsBackendUnavailableError`, `SecretsBackendConflictError`.
- `tests/test_secrets_manager.py` — 25 testes.
- `PROMPT_23_SECRETS_MANAGER_RELATORIO.md` — este relatório.

## 2. Arquivos modificados

Nenhum. Este Prompt não modificou nenhum arquivo pré-existente — é
infraestrutura inteiramente nova e isolada, sem consumidor ainda.

## 3. Investigação da seção 0 (confirmada com o código real)

- **Login real**: `login_conta.py` abre um Chrome de verdade
  (`subprocess.Popen([chrome, f'--user-data-dir={profile}', ...])`) usando
  `ACCOUNT_PATHS.profile_youtube`/`profile_tiktok` como perfil persistente
  (`_sistema/app_paths.py`, `profile_youtube=root / "perfil_youtube"`,
  `profile_tiktok=root / "perfil_tiktok"`). `agendar_youtube.py`/
  `agendar_tiktok.py` reutilizam o mesmo `PROFILE_DIR` via
  `user_data_dir=str(PROFILE_DIR)`. Toda a sessão (cookies, tokens) vive
  dentro do perfil do próprio Chrome — a aplicação nunca lê/copia esse
  conteúdo. Confirmado.
- **Backup nunca inclui perfis**: `_sistema/storage/backup.py` confirma
  isso explicitamente no próprio docstring do módulo: "Ela não copia
  vídeos originais, renders, perfis de navegador, caches, modelos ou
  outros arquivos de mídia. Backups são snapshots consistentes produzidos
  pela SQLite Backup API." O manifest de metadata é allowlistado por
  campo (`_ALLOWED_METADATA_FIELDS`) e o próprio código comenta:
  "Metadata de manifest é deliberadamente allowlistada. Nada arbitrário
  como cookies/tokens/credentials/perfis pode ser persistido por engano."
  Nada a corrigir — confirmado, não modificado.
- **`default_config()`/`config.json`**: lido `_sistema/painel_oficial.py`
  linhas 123-156 — nenhum campo de senha, API key ou token em texto
  plano. `ia_local.ollama_url` é local (`http://localhost:11434`), sem
  chave. Confirmado por teste automatizado
  (`test_default_config_nao_tem_nenhum_campo_de_credencial_novo`, varre
  recursivamente as chaves de `default_config()` para ambas as
  plataformas por qualquer nome sugestivo de credencial).
- **Nenhuma chamada de rede a um backend próprio**: confirmado por
  inspeção — Geração 1 e Geração 2 não fazem nenhuma chamada HTTP a um
  serviço próprio do produto hoje (licenciamento/Prompt 63 não existe
  ainda).

Conclusão da investigação: não há segredo real hoje para migrar. Este
Prompt é puramente infraestrutura antecipada, na mesma situação já
aprovada para Circuit Breaker/RetryPolicy/Idempotência.

## 4. Comportamento novo

`SecretsManager` é uma fachada fina sobre um `SecretsBackend` plugável
(`store`/`retrieve`/`delete`/`exists`, todos operando sobre `bytes`;
`store`/`retrieve_text` na fachada aceitam/devolvem `str` via UTF-8 como
conveniência). Nenhum consumidor real existe ainda — o módulo não é
importado por nenhum outro arquivo do projeto.

## 5. Decisões arquiteturais

### 5.1 Backend real escolhido: DPAPI via `ctypes`

Justificativa (detalhada também no docstring do módulo):

- **DPAPI vs Credential Manager**: DPAPI protege blobs de bytes
  arbitrários sem limite de tamanho artificial; Credential Manager foi
  desenhado para pares usuário/senha e tem um limite de tamanho de blob
  historicamente pequeno (~2560 bytes para `CRED_TYPE_GENERIC`). Um
  segredo futuro deste produto (ex.: uma licença HWID completa, um
  payload de configuração de licenciamento) pode não ser uma simples
  string curta — DPAPI é a escolha mais segura e geral.
- **Sem `pywin32`**: confirmado que `pywin32`/`win32crypt` NÃO está
  instalado neste ambiente (`pip show pywin32` → not found) e não é
  dependência do projeto (`requirements.txt`: `playwright`,
  `faster-whisper`, `tzdata` — nenhuma lib do Windows). Implementar via
  `ctypes` puro (biblioteca padrão, sempre disponível, inclusive neste
  ambiente Linux de teste) evita adicionar uma dependência nova só para
  isto.
- **Credential Manager como extensão futura**: documentado explicitamente
  no docstring do módulo como não implementado nesta rodada — só valeria
  a pena se um consumidor futuro precisasse da vantagem específica de
  aparecer no Gerenciador de Credenciais do Windows para o usuário final
  revogar manualmente.

### 5.2 Fail closed, nunca plaintext silencioso

`DpapiSecretsBackend.__init__` chama `_dpapi_available()` (checa
`sys.platform == "win32"` E que `crypt32.dll` carrega via
`ctypes.WinDLL`) e levanta `SecretsBackendUnavailableError` IMEDIATAMENTE
se falhar — antes de criar o diretório de armazenamento, antes de
qualquer I/O. Não existe uma instância "degradada" de
`DpapiSecretsBackend` que aceitaria `store()` e gravaria sem proteção: a
falha acontece na própria construção, então o objeto nunca chega a
existir para ser usado. A ÚNICA forma de persistir um valor sem DPAPI é
escolher explicitamente `InMemorySecretsBackend` — e esse backend nunca é
selecionado automaticamente (ver 5.3).

Falhas de operação individual (`CryptProtectData`/`CryptUnprotectData`
retornando erro, ex.: blob corrompido) levantam
`SecretsBackendConflictError`, distinta de "backend indisponível" —
significa "o mecanismo está disponível, mas esta chamada específica
falhou", nunca confundida com uma exceção capturada internamente e
mascarada.

### 5.3 `InMemorySecretsBackend` nunca escolhido automaticamente

`SecretsManager.__init__(self, backend: SecretsBackend)` não tem valor
default — `backend` é sempre obrigatório e sempre passado explicitamente
pelo chamador. Não existe nenhum método de fábrica
(`SecretsManager.default()`, `.from_platform()`, `.auto()` etc.) em
`SecretsManager` nem no módulo — testado explicitamente
(`test_secrets_manager_nao_tem_metodo_ou_funcao_de_autodetecao_de_backend`,
que varre a superfície pública de `SecretsManager` e do módulo por
substrings como "auto"/"default"/"platform"/"detect").

### 5.4 Garantia estrutural de "nunca rede"

`secrets_manager.py` não importa `urllib`, `http`, `requests`, `httpx`,
`aiohttp`, `socket`, `ftplib`, `smtplib`, `websocket` nem usa
`importlib`/`__import__` para escondê-los dinamicamente — dois testes AST
dedicados confirmam isso (um varrendo `Import`/`ImportFrom` reais, outro
confirmando a ausência de qualquer import dinâmico que pudesse escapar da
primeira varredura). Se o módulo fisicamente não sabe fazer uma
requisição, "nunca enviar segredos a um backend" não depende de
disciplina de um chamador futuro.

### 5.5 Nunca vazar o valor por caminho indireto

Nenhum `__repr__`/`__str__` de `SecretsManager`, `InMemorySecretsBackend`
ou `DpapiSecretsBackend` inclui valores — só metadados não sensíveis
(nome do tipo de backend, contagem de chaves, diretório de
armazenamento). Nenhuma mensagem de exceção (`SecretNotFoundError`,
`SecretsBackendUnavailableError`, `SecretsBackendConflictError`)
referencia um valor armazenado — só a chave (nome do segredo, nunca o
valor) ou a natureza do erro. O módulo não usa `logging`/`print` em
caminho nenhum. Testado com um marcador único reconhecível
(`_MARCADOR_SECRETO`), armado e buscado em toda representação textual
alcançável sem o valor em mãos.

### 5.6 `retrieve()` inequívoco para chave inexistente

`SecretNotFoundError` (subclasse de `KeyError`) é levantada, nunca
`None` — mesmo que um valor VAZIO (`b""`/`""`) tenha sido armazenado
deliberadamente, o que é distinguível de "nunca armazenado" (testado
explicitamente: armazenar `""` e depois pedir uma chave nunca usada
produzem comportamentos diferentes e corretos).

## 6. Não fazer — confirmações

- Nenhum campo novo de senha/API key foi adicionado a `config.json`/
  `default_config()`/qualquer fluxo de conta (confirmado por teste
  automatizado, seção 5 acima, mais leitura manual do código).
- `login_conta.py`, os diretórios de perfil do Chrome
  (`profile_youtube`/`profile_tiktok`) e `storage/backup.py` não foram
  tocados — nenhum `Write`/`Edit` foi executado contra eles nesta rodada;
  confirmado por hash SHA-256 (`login_conta.py`:
  `d8b403e730d33eb3c8927afd93f6fd7c6069bc819ee4847f1074c18ba72b928c`,
  `storage/backup.py`:
  `08b4ba0adcd50ccfc850c417f9ed51b4e3f0a187da17a3ff5c4d2c9a1a391d38`,
  `painel_oficial.py`:
  `2d76d22bc2080939897f9703be24e88ac044fa479401e295bc40329e11e167ce`) e
  por `mtime` (datas de modificação anteriores a esta rodada, sem
  alteração) e por teste automatizado que varre o código-fonte real
  desses módulos confirmando que continuam usando
  `profile_youtube`/`profile_tiktok` e que nenhum deles importa
  `secrets_manager`.
- Nenhuma chamada de rede/backend real de licenciamento foi implementada.
- Geração 1 (`agendar_youtube.py`/`agendar_tiktok.py`) não foi tocada —
  hashes re-confirmados idênticos aos já registrados
  (`1e25b77e1ecb5bbb06c5d8ea1f62efb948ab98dcfa5f10a9b10bd2c283c2ce14` /
  `4cfc392a967180a47bd9270dbc7add1ef4207733a1e9a4e836398cc93d617110`).
- `circuit_breaker.py`
  (`b0ea87e441cde0aa1661dde5bb1b4d04403a637bc582ca1fd5b2ba62e48e1d9e`),
  `retry_policy.py`
  (`2df58146f8588c2b78aac537476391c84927744589c2f4f135fa9070ea074493`),
  `publication_idempotency.py`
  (`f118e0a62bbcf31b48c83c2d350cfcb3cb376d2953042b33715a99f78edc0c38`) e
  `domain/job_state_machine.py`
  (`5f613154477c8b57e0af11de94d462d86a095504d6bdf7e9f37be0de764e6169`)
  — hashes re-confirmados idênticos aos registros dos Prompts 20/21/22;
  `secrets_manager.py` não importa nenhum desses módulos (teste AST
  dedicado).

## 7. Testes automatizados

`tests/test_secrets_manager.py` — **25 testes**, cobrindo:

1. Roundtrip `store`/`retrieve`/`delete`/`exists` (via
   `InMemorySecretsBackend`).
2. `retrieve()` de chave inexistente inequívoco, distinguível de valor
   vazio armazenado.
3. Sobrescrever chave existente atualiza (nunca duplica/deixa valor
   antigo recuperável).
4. Backend real (`DpapiSecretsBackend`) indisponível → falha alta,
   nunca grava texto plano; inclusive prova que o diretório de
   armazenamento nem chega a ser criado.
5. `InMemorySecretsBackend` nunca escolhido automaticamente — backend
   sempre explícito, sem método de fábrica/autodetecção.
6. Nenhum valor de segredo aparece em `repr()`/`str()`/mensagens de erro
   (varredura com marcador único).
7. Nenhuma biblioteca de rede importada, estática (AST) nem
   dinamicamente (`importlib`/`__import__`).
8. Não-regressão: `default_config()` sem campo de credencial novo,
   `login_conta.py`/`storage/backup.py` inalterados estruturalmente,
   isolamento de `circuit_breaker`/`retry_policy`/
   `publication_idempotency`/`job_state_machine`.

Suíte completa desta rodada: **1336 passed, 36 subtests passed**, 0
falhas (1311 anteriores + 25 novos).

`python3 -m compileall -q _sistema tests`: **OK**.

## 8. Como testar manualmente

Neste ambiente (não-Windows):

```python
from _sistema.secrets_manager import SecretsManager, InMemorySecretsBackend

mgr = SecretsManager(InMemorySecretsBackend())
mgr.store("chave_teste", "valor-secreto")
print(mgr.retrieve_text("chave_teste"))  # "valor-secreto"
mgr.delete("chave_teste")
mgr.retrieve("chave_teste")  # levanta SecretNotFoundError
```

Em uma máquina Windows real (validação que este ambiente não pode fazer):

```python
from _sistema.secrets_manager import SecretsManager, DpapiSecretsBackend

backend = DpapiSecretsBackend(r"C:\Users\<usuario>\AppData\Local\PainelOficial\cofre")
mgr = SecretsManager(backend)
mgr.store("chave_teste", "valor-secreto")
print(mgr.retrieve_text("chave_teste"))
```

## 9. Riscos conhecidos e dívida técnica

- **O backend real (`DpapiSecretsBackend`) só pode ser validado de
  verdade em uma máquina Windows real.** Este ambiente de auditoria não é
  Windows — os testes que envolvem o comportamento "fail closed" do
  backend real (`test_dpapi_indisponivel_neste_ambiente_levanta_ao_construir`,
  `test_dpapi_indisponivel_simulado_em_plataforma_windows_tambem_levanta`,
  `test_nenhum_caminho_de_codigo_grava_texto_claro_quando_dpapi_indisponivel`)
  provam que a checagem de disponibilidade FUNCIONA e recusa operar sem
  proteção — mas não provam que `CryptProtectData`/`CryptUnprotectData`
  de fato funcionam corretamente em produção (isso exige Windows real). O
  segundo desses três testes usa `monkeypatch` para simular a
  indisponibilidade "como se estivesse" no Windows com a DLL falhando —
  uma simulação documentada, não uma prova em Windows real. O
  smoke test manual da seção 8 (variante Windows) precisa ser executado
  manualmente em `C:\Users\Enzo\Desktop\teste` para confirmar o
  roundtrip real de `CryptProtectData`/`CryptUnprotectData` antes deste
  backend ser usado por qualquer consumidor futuro.
- Nenhum consumidor real usa `SecretsManager` ainda — é infraestrutura
  pura, como já era esperado e autorizado pelo Prompt.
- Credential Manager como segundo backend real fica como extensão futura
  explícita, não implementada.
- `DpapiSecretsBackend` usa um arquivo por chave (nome derivado por hash
  SHA-256 da chave lógica) dentro de um diretório de armazenamento
  informado pelo chamador — a decisão de ONDE esse diretório fica (ex.:
  dentro da pasta de dados do produto) é responsabilidade de um
  consumidor futuro; este Prompt não fixa esse caminho.

## 10. Pendências

Nenhuma pendência dentro do escopo autorizado deste Prompt. Não iniciado
Prompt 24, conforme instrução.

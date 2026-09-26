# Testes automatizados — baseline atual

Esta suíte protege comportamento observável do protótipo **sem abrir navegador real** e sem fazer chamadas reais ao Ollama ou FFmpeg nos testes que usam mocks.

## Executar

Na raiz do projeto, instale as dependências exclusivas de teste uma vez:

```bat
py -3 -m pip install -r tests\requirements-test.txt
```

Depois execute:

```bat
py -3 -m pytest -v
```

Quando `py` não existir, use `python` nos mesmos comandos. No Windows, `RODAR_TESTES.bat` é o executor recomendado por duplo clique e o `pytest` continua sendo o executor canônico da suíte. O BAT identifica primeiro um Python funcional, mostra o caminho e a versão, valida `import pytest` e `ZoneInfo("America/Sao_Paulo")` antes de iniciar a suíte e sempre faz `pause` antes de encerrar, inclusive em erro. O código de saída do pytest é preservado após o `pause`.

Se as dependências de teste estiverem ausentes, `RODAR_TESTES.bat` **não instala nada automaticamente**. Ele mostra um destes comandos, conforme o Python detectado:

```bat
py -3 -m pip install -r tests\requirements-test.txt
```

ou:

```bat
python -m pip install -r tests\requirements-test.txt
```

`tests/requirements-test.txt` contém somente as dependências da suíte baseline: `pytest` e `tzdata`. O `tzdata` garante `ZoneInfo`/IANA em um Windows limpo que não forneça base de timezones ao Python.

Como alternativa explícita, `INSTALAR_DEPENDENCIAS_TESTE.bat` instala **somente** `tests\requirements-test.txt`, verifica o `pip`, confirma `import pytest` e um timezone IANA real após a instalação e também termina sempre com `pause`. Ele não instala `requirements.txt` do produto.

## Cobertura inicial

- fingerprints usados pelos módulos;
- leitura/escrita de JSON e estado da limpeza;
- configurações padrão de YouTube/TikTok;
- criação da estrutura de conta;
- nomes, ordenação natural, caminhos e numeração;
- abstração central de storage, separação install/data, migração transacional por conta, staging seguro e isolamento de fallback standalone por engine;
- modelos de domínio com UUID persistente, round-trip de campos desconhecidos, tombstone de campos descontinuados (`Publication.schedule_id`), relação canônica `Schedule.publication_id`, estados `LOCAL_PENDING`/`REMOTE_SCHEDULED`/`UNKNOWN` e independência de UI/plataformas;
- State Machine central de `Job`, incluindo estados canônicos, transições válidas/proibidas, terminais, proteção contra atribuição direta, proibição de `PUBLISHING -> RETRY`, caminho de incerteza `PUBLISHING -> UNKNOWN -> RECOVERING -> RETRY` após reconciliação e caminho de falha conhecida `PUBLISHING -> FAILED -> RETRY`;
- geração/continuação de slots de agendamento, com limite exato da quantidade solicitada;
- contrato temporal canônico UTC + timezone IANA, incluindo DST, mudança de timezone, ausência segura de fallback, precedência de timezone explícito e round-trip de `Schedule`;
- SQLite local: schema v2 com migrations 001/002 numeradas, checksum, histórico contínuo, upgrade v1->v2 com PRE_MIGRATION, initialize concorrente/idempotente sob lock SQLite, WAL, foreign keys, cardinalidade Publication->0..1 Schedule, índices, transações/rollback, CRUD dos modelos, settings e concorrência básica;
- lifecycle SQLite compatível com Windows/Python 3.14: conexões são fechadas deterministicamente, `initialize()` repetido não acumula handles e o arquivo `.db` pode ser renomeado/removido após as operações terminarem;
- audit log operacional append-only: criação/transições atômicas de Job, eventos de pause/resume/cancel/retry/processamento/upload/confirmação/reconciliação/erro/recovery, triggers persistentes contra UPDATE/DELETE/reuso de ID via INSERT OR REPLACE, authorizer como defesa adicional, ordem de append e reconstrução do histórico após restart;
- backup/restore SQLite: snapshot WAL-consistente, manifest allowlistado, `integrity_check`, preservação total por default, periodicidade baseada somente em backups válidos, pre-migration/pre-update/pre-restore, journal crash-safe com schemas source/previous separados, recovery no startup, lock entre processos, staging/sidecars seguros e exclusão de mídia original;
- migration explícita de estados JSON legados para SQLite: allowlist sem perfis, snapshot/checksum antes do write, raw payload preservado, UUIDs determinísticos, mapeamento semântico, validação de contagens, rollback e segunda execução `NOOP` sem duplicatas;
- parsing do JSON retornado pela IA;
- hashtags e assinatura do pipeline de textos;
- cache de transcrição;
- reutilização de texto por fingerprint;
- detecção de texto já processado;
- detecção de vídeo já processado e retomada de número reservado;
- deduplicação de originais idênticos no mesmo lote;
- ffprobe/FFmpeg via mocks;
- Ollama via mocks.

## Limites intencionais

A suíte não abre Chrome/Playwright e não publica conteúdo. Confirmação/reconciliação remota, CAPTCHA/2FA e mudanças de DOM continuam exigindo testes específicos de integração em etapas futuras.

Os testes também não transformam riscos conhecidos em contratos desejáveis. Comportamentos já identificados como incompatíveis com `PRODUCT_INVARIANTS.md` devem ser corrigidos em etapas futuras, acompanhados de atualização intencional dos testes afetados.

## JSON de estado corrompido — regressão fechada

Os cinco loaders (`painel_oficial`, `agendar_youtube`, `agendar_tiktok`, `gerar_textos` e `limpar_metadados_oficial`) agora distinguem os casos:

- arquivo inexistente -> retorna o `default`;
- arquivo existente porém inválido/ilegível -> levanta `StateJsonReadError`;
- o arquivo problemático não é apagado nem reescrito.

Os cinco antigos XFAILs foram convertidos em testes normais verdes. O antigo XFAIL de `TikTok build_slots` também já havia sido convertido anteriormente; o risco separado do Connector TikTok aplicar horário errado no DOM permanece fora desta etapa.


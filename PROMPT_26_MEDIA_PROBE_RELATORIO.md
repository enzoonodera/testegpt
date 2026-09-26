# PROMPT 26 — MEDIA PROBE (MediaProbe) — Relatório de Entrega

## 1. Arquivos criados

- `_sistema/media_probe.py` (23350 bytes) — módulo `MediaProbe`, `MediaProbeResult`, 13 constantes `ERRO_*`, funções auxiliares `_validate_file_for_probe`, `_parse_fps`, `_parse_duration`, `_replace_for_error`.
- `tests/test_media_probe.py` (23595 bytes) — 31 testes automatizados.
- `PROMPT_26_MEDIA_PROBE_RELATORIO.md` — este relatório.

## 2. Arquivos modificados

- `empacotar_release.py` — adicionada a entrada `"PROMPT_26_MEDIA_PROBE_RELATORIO.md"` em `ALLOWED_ROOT_FILES`, imediatamente após a entrada do Prompt 25. Tamanho final: **35843 bytes**. Nenhuma outra linha alterada.

Nenhum outro arquivo do projeto foi modificado.

## 3. Comportamento novo

`MediaProbe` é um módulo independente, sem acesso a banco de dados, que valida um arquivo de mídia via `ffprobe`/`ffmpeg` e devolve sempre um `MediaProbeResult` estruturado — nunca uma exceção crua.

- `probe(file_path) -> MediaProbeResult` — verificação **rasa**: só `ffprobe` (leitura de metadados do container). Extrai `container`, `codec`, `duration`, `fps`, `width`/`height`, `has_audio`/`audio_stream_count`, `file_size` (do filesystem, não do ffprobe). Caminho padrão, barato.
- `probe_deep(file_path, *, timeout=None) -> MediaProbeResult` — verificação **profunda**, opcional: roda `probe()` primeiro e, se válido, decodifica o arquivo inteiro via `ffmpeg -v error -i <arquivo> -f null -`. Nunca chamada automaticamente.
- `probe_batch(file_paths) -> list[MediaProbeResult]` — processa vários caminhos com isolamento total por item; um arquivo inválido/corrompido nunca interrompe a validação dos demais.

## 4. Decisões arquiteturais

### 4.1 Seção 0.2 — SEM integração automática ao fluxo de import

`MediaProbe` **não** é chamado automaticamente por `LocalFileImporter`, `FolderImporter` ou `UrlImporter`. Confirmado por teste dedicado que inspeciona o AST desses três módulos (`test_media_probe_nao_e_chamado_automaticamente_pelos_importadores`) — nunca por grep textual ingênuo, pelo motivo já documentado nos Prompts 24b/25 (o próprio `source_import.py` cita "MediaProbe" em seu docstring ao referenciá-lo como trabalho futuro fora de escopo, o que faria um `assert "MediaProbe" not in source` falso-positivar).

Justificativa, confrontando diretamente o argumento de custo:

- `ffprobe`/`ffmpeg` são chamadas de processo externo com custo real (spawn de subprocesso, leitura/parse de metadados, potencialmente segundos por arquivo; a verificação profunda decodifica o arquivo inteiro). Rodar isso automaticamente para todo arquivo de um `FolderImporter` processando centenas/milhares de vídeos imporia um custo de tempo não solicitado a uma operação hoje rápida (referenciar/copiar um arquivo) — quebrando a expectativa de desempenho já estabelecida e testada nos Prompts 24/24b, sem necessidade demonstrada.
- O próprio roadmap diz "**antes de processar** verificar" — leitura natural: validação profunda de mídia é um portão antes de uma etapa de **processamento** real (Fase 5/Editor, Prompt 27 em diante), não um passo da importação em si. Uma importação bem-sucedida (`SourceAsset` persistido) e uma validação de mídia bem-sucedida são preocupações ortogonais neste momento do produto.
- Consequência: `MediaProbe` é um módulo independente, chamável sob demanda por qualquer camada futura (por `SourceAsset.local_path` ou qualquer `Path`) antes de começar a trabalhar num arquivo.

Este comportamento diverge do precedente do Prompt 25 (`SourceContextResolver`, ligado automaticamente porque é apenas parsing de string, sem custo de processo externo) — a divergência é intencional e documentada, não silenciosa.

### 4.2 Seção 0.3 — sem acesso a banco de dados

`MediaProbe` opera exclusivamente sobre `Path`, nunca sobre uma entidade persistida. Confirmado por teste AST (`test_media_probe_nao_importa_storage_database`) que o módulo não importa `storage.database`/`LocalDatabase`. Justificativa: mantém o módulo testável sem fixtures de banco e reutilizável tanto para `SourceAsset.local_path` quanto para qualquer arquivo fora desse fluxo.

### 4.3 Seção 1.5 — profundidade de verificação de corrupção

Investigação empírica real (não assumida) em `/tmp`, antes da implementação:

- Truncar o fim de um MP4 quebra o "moov atom" → `ffprobe` falha com `returncode != 0` (corrupção **estrutural**, detectável pela verificação rasa).
- Zerar um trecho do meio do arquivo deixa o container estruturalmente íntegro → `ffprobe` continua reportando JSON completo e válido com `returncode == 0` (corrupção de **frame**, cega para a verificação rasa — limitação confirmada, não hipotética).
- Rodar `ffmpeg -v error -i <arquivo> -f null -` (decode completo) sobre esse mesmo arquivo com corrupção de frame produz `stderr` não vazio mesmo com `returncode == 0` — achado crítico: o sinal de corrupção profunda **precisa** verificar `returncode != 0 OR stderr não vazio`, nunca `returncode` isoladamente.
- Um timeout extremamente baixo (`subprocess.run(cmd, timeout=1e-7)`) contra uma chamada real de `ffprobe` levanta `subprocess.TimeoutExpired` de forma confiável, confirmando que a abordagem de "teste de timeout real" é viável.

**Decisão**: implementadas **ambas** as opções da seção 1.5, nenhuma escondendo a outra — decisão explicitamente permitida pelo próprio Prompt ("qualquer escolha é aceitável se documentada e testada com igual rigor").

- `probe()` — rasa, caminho **padrão** (usado também por `probe_batch()`). Rápida, custo aceitável mesmo em lote. Detecta corrupção estrutural. **Não** detecta corrupção de frame com container íntegro — limitação conhecida, documentada aqui e na seção 8 (riscos).
- `probe_deep()` — profunda, **opcional**, nunca chamada automaticamente por `probe()`/`probe_batch()`. Decodifica o arquivo inteiro; custo proporcional à duração do vídeo. Usa o sinal duplo (`returncode != 0 OR stderr`) confirmado empiricamente.

## 5. Migrations

Nenhuma. `MediaProbe` não possui schema/tabela — é o primeiro módulo de Geração 2 sem qualquer dependência de banco de dados. `LATEST_SCHEMA_VERSION` permanece em 8 (inalterado desde o Prompt 25).

## 6. Lista completa de códigos de erro estruturados

Todos definidos como constantes próprias em `media_probe.py`, nunca reutilizadas/importadas de `source_import.py` (mesma decisão de duplicação consciente já usada para `VIDEO_EXTS` entre módulos).

**Família ARQUIVO** (problema do arquivo em si):
| Código | Quando ocorre |
|---|---|
| `ARQUIVO_INEXISTENTE` | `path.exists()` falso, ou `OSError` ao fazer `stat()` |
| `NAO_E_ARQUIVO_REGULAR` | caminho existe mas não é um arquivo regular (ex.: diretório) |
| `ARQUIVO_VAZIO` | tamanho em disco `<= 0` |
| `FFPROBE_FALHOU` | `ffprobe` retorna código de saída diferente de zero (inclui corrupção estrutural, ex. "moov atom not found") |
| `SAIDA_JSON_INVALIDA` | stdout do `ffprobe` não é JSON válido |
| `SEM_STREAM_VIDEO` | nenhum stream com `codec_type == "video"` no JSON |
| `RESOLUCAO_INVALIDA` | `width <= 0` ou `height <= 0` |
| `DURACAO_INVALIDA` | duração ausente, não numérica, ou `<= 0` |
| `DECODE_FALHOU` | (somente `probe_deep`) decode completo via ffmpeg encontrou `returncode != 0` ou `stderr` não vazio |

**Família AMBIENTE** (problema da máquina, nunca do arquivo — distinção central pedida pelo Prompt, para um produto distribuído a 1000+ máquinas):
| Código | Quando ocorre |
|---|---|
| `FFPROBE_AUSENTE` | `shutil.which("ffprobe")` retorna `None`, verificado **antes** de qualquer invocação |
| `FFMPEG_AUSENTE` | (somente `probe_deep`) `shutil.which("ffmpeg")` retorna `None` |

**Timeout** (mesmo código para os dois subprocessos, muda apenas qual comando estourou):
| Código | Quando ocorre |
|---|---|
| `TIMEOUT` | `subprocess.TimeoutExpired` — em `probe()` (ffprobe) ou `probe_deep()` (ffmpeg) |

**Catch-all** (GATE 13 — nunca deixar exceção crua escapar):
| Código | Quando ocorre |
|---|---|
| `INESPERADO` | qualquer exceção verdadeiramente não prevista, capturada no nível mais externo de `probe()`, `probe_deep()` e por item em `probe_batch()` |

`FFPROBE_AUSENTE`/`FFMPEG_AUSENTE` nunca são confundidos com um código de arquivo inválido: são verificados via `shutil.which` **antes** de qualquer tentativa de `subprocess.run`, e retornam imediatamente sem tocar a lógica de parsing de arquivo.

## 7. Testes automatizados executados

**31 testes novos em `tests/test_media_probe.py`, todos passando, zero skipped:**

```
tests/test_media_probe.py::test_probe_video_real_campos_batem_com_geracao PASSED
tests/test_media_probe.py::test_probe_deep_video_real_valido_ok PASSED
tests/test_media_probe.py::test_probe_video_sem_stream_de_video PASSED
tests/test_media_probe.py::test_corrupcao_estrutural_truncamento_detectada_pela_verificacao_rasa PASSED
tests/test_media_probe.py::test_corrupcao_de_frame_no_meio_nao_e_detectada_pela_verificacao_rasa_mas_e_pela_profunda PASSED
tests/test_media_probe.py::test_probe_deep_arquivo_com_falha_rasa_nao_tenta_decodificar PASSED
tests/test_media_probe.py::test_arquivo_inexistente PASSED
tests/test_media_probe.py::test_arquivo_vazio PASSED
tests/test_media_probe.py::test_caminho_e_diretorio_nao_arquivo_regular PASSED
tests/test_media_probe.py::test_probe_nao_restringe_por_extensao_arquivo_generico_ainda_tenta_ffprobe PASSED
tests/test_media_probe.py::test_ffprobe_ausente_do_path_codigo_dedicado PASSED
tests/test_media_probe.py::test_ffmpeg_ausente_apenas_afeta_probe_deep_nunca_probe_raso PASSED
tests/test_media_probe.py::test_timeout_real_baixissimo_contra_arquivo_real_levanta_e_e_capturado PASSED
tests/test_media_probe.py::test_timeout_mockado_no_probe_raso PASSED
tests/test_media_probe.py::test_timeout_mockado_no_probe_deep PASSED
tests/test_media_probe.py::test_ffprobe_saida_nao_e_json_valido PASSED
tests/test_media_probe.py::test_ffprobe_codigo_saida_diferente_de_zero PASSED
tests/test_media_probe.py::test_resolucao_invalida_largura_zero PASSED
tests/test_media_probe.py::test_duracao_invalida_zero PASSED
tests/test_media_probe.py::test_duracao_nao_numerica PASSED
tests/test_media_probe.py::test_probe_batch_isolamento_arquivo_invalido_nao_afeta_os_demais PASSED
tests/test_media_probe.py::test_probe_batch_lista_vazia PASSED
tests/test_media_probe.py::test_probe_batch_isolamento_falha_inesperada_de_um_item PASSED
tests/test_media_probe.py::test_media_probe_nao_importa_modulos_protegidos PASSED
tests/test_media_probe.py::test_media_probe_nao_importa_nada_de_geracao_1 PASSED
tests/test_media_probe.py::test_media_probe_nao_importa_storage_database PASSED
tests/test_media_probe.py::test_nenhum_job_publication_schedule_project_ou_video_criado PASSED
tests/test_media_probe.py::test_media_probe_nao_e_chamado_automaticamente_pelos_importadores PASSED
tests/test_media_probe.py::test_media_probe_rejeita_timeout_nao_positivo PASSED
tests/test_media_probe.py::test_media_probe_result_e_dataclass_imutavel PASSED
tests/test_media_probe.py::test_duas_instancias_concorrentes_fazendo_probe_do_mesmo_arquivo PASSED

31 passed in 1.82s
```

**Confirmação explícita pedida pelo Prompt — testes reais de ffmpeg/ffprobe NÃO foram pulados**: os testes marcados com `@pytest.mark.skipif(shutil.which(...) is None, ...)` (incluindo `test_probe_video_real_campos_batem_com_geracao`, `test_probe_deep_video_real_valido_ok`, `test_corrupcao_estrutural_truncamento_detectada_pela_verificacao_rasa`, `test_corrupcao_de_frame_no_meio_nao_e_detectada_pela_verificacao_rasa_mas_e_pela_profunda`, e `test_timeout_real_baixissimo_contra_arquivo_real_levanta_e_e_capturado`) aparecem todos como `PASSED` na saída acima — zero `SKIPPED` no arquivo. `ffmpeg`/`ffprobe` estão presentes neste ambiente de auditoria e os testes rodaram de verdade contra vídeos gerados localmente com `ffmpeg -f lavfi` (sem internet), incluindo corrupção real de bytes (truncamento e zeramento de trecho do meio), não simulada/mockada.

Destaques de conteúdo dos testes:
- **Vídeo real gerado e corrupção real de bytes**: um MP4 válido é gerado com `testsrc`+`sine` via `ffmpeg -f lavfi`, sondado, e comparado campo a campo (resolução, fps, duração, container, codec, presença de áudio) contra os parâmetros usados na geração. Depois: truncamento real do fim do arquivo (corrupção estrutural) e zeramento real de um trecho do meio (corrupção de frame) são aplicados sobre cópias do vídeo válido, cada uma sondada e verificada com o `error_code` esperado.
- **Timeout real**: `MediaProbe(ffprobe_timeout=1e-7)` contra um `ffprobe` real força `subprocess.TimeoutExpired` de verdade (não mockado) e confirma `ERRO_TIMEOUT` sem exceção escapando; há também casos mockados cobrindo o mesmo caminho em `probe_deep`.
- **`shutil.which` monkeypatchado para `None`**: confirma `ERRO_FFPROBE_AUSENTE`/`ERRO_FFMPEG_AUSENTE`, distintos de qualquer código de arquivo inválido.
- **Lote com isolamento**: mistura de arquivos válidos/inválidos/corrompidos em `probe_batch()`, confirmando que uma falha de item nunca interrompe os demais.
- **3 testes AST** confirmando: nenhum import de `circuit_breaker`/`retry_policy`/`publication_idempotency`/`secrets_manager`/`domain.job_state_machine`; nenhum import de nada de Geração 1; nenhum import de `storage.database`/`LocalDatabase`.
- **Teste de ausência de entidades**: nenhuma referência a `Job(`/`Publication(`/`Schedule(`/`Project(`/`Video(` no código-fonte do módulo.
- **Teste de não-integração automática** (via AST, não grep textual — mesma lição já aplicada nos Prompts 24b/25): nenhum dos três importadores referencia `MediaProbe`/`media_probe`.
- **Concorrência determinística com `threading.Barrier`** (GATE 6/14): duas instâncias chamando `probe()` simultaneamente sobre o mesmo arquivo, sem estado mutável compartilhado — confirma ausência de interferência (módulo é stateless, então o teste principalmente documenta essa propriedade em vez de proteger contra uma corrida real).

**Suíte completa (Prompts 19–26, sem remover nem enfraquecer nenhum teste anterior):**

```
1465 passed, 1 skipped, 36 subtests passed in 102.34s (0:01:42)
```

O único teste pulado é pré-existente de rounds anteriores (não pertence a `test_media_probe.py` — todos os 31 testes deste módulo passaram, zero pulados, conforme detalhado acima).

**`compileall`:**

```
python3 -m compileall -q _sistema tests empacotar_release.py
compileall OK
```

## 8. Como testar manualmente

```
python -m _sistema.media_probe  # não expõe CLI; uso programático:
python -c "
from pathlib import Path
from _sistema.media_probe import MediaProbe
mp = MediaProbe()
r = mp.probe(Path('C:/caminho/para/video.mp4'))
print(r)
"
```

Para verificação profunda (decodificação completa, mais lenta):
```
python -c "
from pathlib import Path
from _sistema.media_probe import MediaProbe
mp = MediaProbe()
r = mp.probe_deep(Path('C:/caminho/para/video.mp4'))
print(r)
"
```

Em uma máquina sem `ffprobe`/`ffmpeg` no PATH, `probe()`/`probe_deep()` devolvem `valid=False` com `error_code="FFPROBE_AUSENTE"`/`"FFMPEG_AUSENTE"` — nunca uma exceção, nunca confundido com arquivo inválido.

## 9. Riscos conhecidos e dívida técnica

- **Limitação da verificação rasa (caminho padrão)**: `probe()` — e portanto também `probe_batch()`, que só chama `probe()` — detecta corrupção **estrutural** (container quebrado), mas **não** detecta corrupção de dados de frame no meio de um arquivo com container íntegro. Essa limitação é conhecida e documentada (seção 1.5), confirmada empiricamente e coberta por teste dedicado. Qualquer camada futura que precise de garantia mais forte antes de uma operação cara (ex. render) deve chamar `probe_deep()` explicitamente — isso não acontece automaticamente hoje.
- **Sem cache de resultado**: cada chamada a `probe()`/`probe_deep()` reexecuta o subprocesso; não há memoização por arquivo/hash. Aceitável para o escopo atual (módulo sob demanda), mas pode valer a pena revisitar se uma camada futura chamar `MediaProbe` repetidamente sobre os mesmos arquivos em um pipeline de alto volume.
- **`probe_deep` não expõe progresso**: para vídeos longos, o decode completo pode levar até `ffmpeg_decode_timeout` (padrão 120s) sem qualquer sinal intermediário — aceitável para uso pontual, mas uma UI que exponha `probe_deep` em lote precisaria de algum indicador de progresso ou rodar em background.
- Nenhum dos riscos acima afeta a corretude dos códigos de erro estruturados nem a distinção arquivo-vs-ambiente, que foi o requisito central deste Prompt.

## 10. Pendências

Nenhuma pendência dentro do escopo deste Prompt. `MediaProbe` está completo, testado e não integrado automaticamente a nenhum fluxo existente, conforme decisão da seção 0.2. A integração de `MediaProbe`/`probe_deep()` a uma futura camada de processamento (Fase 5/Editor) fica para um Prompt futuro (27+), que não foi iniciado.

## 11. Confirmações explícitas exigidas pelo Prompt

- **Nenhum caminho de código confunde "ffprobe ausente do ambiente" com "arquivo corrompido"**: `ERRO_FFPROBE_AUSENTE`/`ERRO_FFMPEG_AUSENTE` são verificados via `shutil.which` antes de qualquer tentativa de invocação, e retornados imediatamente — nunca compartilham código com `ERRO_FFPROBE_FALHOU`/`ERRO_SAIDA_JSON_INVALIDA`/etc.
- **Nenhuma exceção crua (incluindo timeout) escapa do método público**: `probe()`, `probe_deep()` e `probe_batch()` envolvem toda a lógica em `try/except`, com um `except Exception` externo deliberado (GATE 13) convertendo qualquer falha verdadeiramente inesperada em `ERRO_INESPERADO`. Confirmado por teste de timeout real (não apenas mockado).
- **Nenhum `Job`/`Publication`/`Schedule`/`Project`/`Video` foi criado** — confirmado por teste AST (`test_nenhum_job_publication_schedule_project_ou_video_criado`).
- **`circuit_breaker.py`/`retry_policy.py`/`publication_idempotency.py`/`secrets_manager.py`/`domain/job_state_machine.py` e Geração 1 (`agendar_youtube.py`/`agendar_tiktok.py`/`limpar_metadados_oficial.py`/`gerar_textos.py`) não foram tocados** — reconfirmado por comparação de tamanho em bytes idêntico ao round anterior antes de qualquer trabalho neste Prompt, e por 2 testes AST dedicados que confirmam ausência de import desses módulos em `media_probe.py`.
- **`_sistema/source_import.py` não foi tocado** — permanece em 49320 bytes, idêntico ao tamanho confirmado na entrega do Prompt 25.
- **Prompt 27 não foi iniciado.**

## 12. Verificação da entrega (ZIP)

`device_bash` permaneceu indisponível durante todo este round ("Workspace unavailable. The isolated Linux environment on this device failed to start."), verificado no início da fase de entrega. Por isso, o ZIP de verificação abaixo foi construído localmente no sandbox de nuvem, reproduzindo `empacotar_release.py` com os 6 arquivos raiz exclusivos do Windows (`EMPACOTAR_RELEASE.bat`, `INSTALAR_DEPENDENCIAS_TESTE.bat`, `LEIA_ME_PRIMEIRO.txt`, `LIMPAR_ANTES_DE_ZIPAR.bat`, `PAINEL_OFICIAL.bat`, `RODAR_TESTES.bat`) **emprestados temporariamente** (via `device_stage_files`, somente leitura) da máquina Windows real, copiados para a raiz do projeto no sandbox só para permitir a execução do empacotador, e **removidos do sandbox logo em seguida** (não fazem parte de nenhum artefato deste round além do ZIP de verificação abaixo).

Isso significa: este ZIP comprova que `empacotar_release.py` produz um pacote consistente com o conteúdo atualmente confirmado byte-a-byte na máquina Windows (`_sistema/media_probe.py`, `tests/test_media_probe.py`, `empacotar_release.py`, e todos os arquivos protegidos/anteriores), mas **não é** o mesmo processo de build que rodaria nativamente no Windows via `EMPACOTAR_RELEASE.bat` — divulgação honesta, mesmo padrão já usado nos relatórios dos Prompts 24b e 25 enquanto `device_bash` estava indisponível.

**SHA-256 do ZIP gerado nesta rodada:**
```
6aae44d525bdd289b4a8bbeb37fff25afa3e4880f09d9110df03e5fd052cc95e
```
Arquivo: `PAINEL_OFICIAL_ZIP_VERIFICACAO_26.zip` (0.84 MB, 120 arquivos).

**Saída literal de `unzip -l`:**
```
Archive:  PAINEL_OFICIAL_ZIP_VERIFICACAO_26.zip
  Length      Date    Time    Name
---------  ---------- -----   ----
    40632  2026-09-22 21:48   ARQUITETURA_ATUAL.md
    13132  2026-09-22 21:48   CLAUDE.md
     2687  2026-09-22 21:48   EMPACOTAR_RELEASE.bat
   169662  2026-09-22 21:48   GATE_19_5_ESTAGIO2_RELATORIO.md
    11446  2026-09-22 21:48   INSTALAR_DEPENDENCIAS_TESTE.bat
     2969  2026-09-22 21:48   LEIA_ME_PRIMEIRO.txt
      866  2026-09-22 21:48   LIMPAR_ANTES_DE_ZIPAR.bat
    24932  2026-09-22 21:48   MAPA_DE_DADOS.md
      686  2026-09-22 21:48   PAINEL_OFICIAL.bat
    29374  2026-09-22 21:48   PRODUCT_INVARIANTS.md
    23512  2026-09-22 21:48   PROMPT_20_CIRCUIT_BREAKER_RELATORIO.md
    14164  2026-09-22 21:48   PROMPT_21_RETRY_INTELIGENTE_RELATORIO.md
    13986  2026-09-22 21:48   PROMPT_22_IDEMPOTENCIA_RELATORIO.md
    17346  2026-09-22 21:48   PROMPT_23_CORRECAO_RELATORIO.md
    13751  2026-09-22 21:48   PROMPT_23_SECRETS_MANAGER_RELATORIO.md
    31899  2026-09-22 21:48   PROMPT_24B_IMPORT_OPTIONS_RELATORIO.md
    25358  2026-09-22 21:48   PROMPT_24_SOURCE_IMPORT_RELATORIO.md
    28360  2026-09-22 21:48   PROMPT_25_SOURCE_CONTEXT_RESOLVER_RELATORIO.md
    24543  2026-09-22 21:48   REGRESSION_CHECKLIST.md
    26204  2026-09-22 21:48   RISCOS_ATUAIS.md
    34429  2026-09-22 21:48   ROADMAP_COMPLETO.md
     6104  2026-09-22 21:48   RODAR_TESTES.bat
    15403  2026-09-22 21:48   TESTE_MANUAL_WINDOWS_ESTAGIO2.md
    97511  2026-09-22 21:48   _sistema/agendar_tiktok.py
    91980  2026-09-22 21:48   _sistema/agendar_youtube.py
    18611  2026-09-22 21:48   _sistema/app_paths.py
    68310  2026-09-22 21:48   _sistema/batch_engine.py
    27989  2026-09-22 21:48   _sistema/circuit_breaker.py
    55733  2026-09-22 21:48   _sistema/control_manager.py
     2545  2026-09-22 21:48   _sistema/domain/__init__.py
     3307  2026-09-22 21:48   _sistema/domain/checkpoints.py
     5442  2026-09-22 21:48   _sistema/domain/job_state_machine.py
    15978  2026-09-22 21:48   _sistema/domain/models.py
    17683  2026-09-22 21:48   _sistema/gerar_textos.py
    85229  2026-09-22 21:48   _sistema/job_engine.py
    27891  2026-09-22 21:48   _sistema/limpar_metadados_oficial.py
     2724  2026-09-22 21:48   _sistema/login_conta.py
    23350  2026-09-22 21:48   _sistema/media_probe.py
    44162  2026-09-22 21:48   _sistema/painel_oficial.py
    21683  2026-09-22 21:48   _sistema/publication_idempotency.py
    32885  2026-09-22 21:48   _sistema/recovery_manager.py
    48725  2026-09-22 21:48   _sistema/resource_manager.py
    27252  2026-09-22 21:48   _sistema/retry_policy.py
    17712  2026-09-22 21:48   _sistema/secrets_manager.py
    94188  2026-09-22 21:48   _sistema/shutdown_coordinator.py
    20616  2026-09-22 21:48   _sistema/source_context.py
    49320  2026-09-22 21:48   _sistema/source_import.py
     1036  2026-09-22 21:48   _sistema/state_json.py
     3365  2026-09-22 21:48   _sistema/storage/__init__.py
    19254  2026-09-22 21:48   _sistema/storage/audit.py
    51041  2026-09-22 21:48   _sistema/storage/backup.py
    27352  2026-09-22 21:48   _sistema/storage/database.py
    38955  2026-09-22 21:48   _sistema/storage/legacy_migration.py
     1266  2026-09-22 21:48   _sistema/storage/migrations/__init__.py
     7411  2026-09-22 21:48   _sistema/storage/migrations/m001_initial.py
      764  2026-09-22 21:48   _sistema/storage/migrations/m002_audit_append_only.py
     2395  2026-09-22 21:48   _sistema/storage/migrations/m003_batch_engine.py
     2538  2026-09-22 21:48   _sistema/storage/migrations/m004_circuit_breaker.py
     2135  2026-09-22 21:48   _sistema/storage/migrations/m005_retry_policy.py
     2605  2026-09-22 21:48   _sistema/storage/migrations/m006_publication_idempotency.py
     4713  2026-09-22 21:48   _sistema/storage/migrations/m007_source_asset_declarations.py
     4856  2026-09-22 21:48   _sistema/storage/migrations/m008_source_asset_context.py
   119736  2026-09-22 21:48   _sistema/storage_manager.py
    14323  2026-09-22 21:48   _sistema/time_utils.py
    35843  2026-09-22 21:48   empacotar_release.py
       33  2026-09-22 21:48   requirements.txt
     6014  2026-09-22 21:48   tests/README.md
       65  2026-09-22 21:48   tests/__init__.py
    11123  2026-09-22 21:48   tests/fakes_playwright.py
       21  2026-09-22 21:48   tests/requirements-test.txt
    23822  2026-09-22 21:48   tests/test_agendar_tiktok_check_item_detection.py
    19670  2026-09-22 21:48   tests/test_agendar_tiktok_checks_card_scope.py
    36470  2026-09-22 21:48   tests/test_agendar_tiktok_copyright_policy.py
    14773  2026-09-22 21:48   tests/test_agendar_tiktok_date_before_time_order.py
    13400  2026-09-22 21:48   tests/test_agendar_tiktok_interactive_copyright_policy.py
    28453  2026-09-22 21:48   tests/test_agendar_tiktok_preflight_checks.py
     7817  2026-09-22 21:48   tests/test_agendar_tiktok_skip_checks_on_allow.py
    33286  2026-09-22 21:48   tests/test_agendar_tiktok_time_layers.py
    31582  2026-09-22 21:48   tests/test_agendar_youtube_copyright_policy.py
    23904  2026-09-22 21:48   tests/test_agendar_youtube_interactive_copyright_policy.py
    10080  2026-09-22 21:48   tests/test_agendar_youtube_time_layers.py
     7327  2026-09-22 21:48   tests/test_ai_cache_parsing.py
    20247  2026-09-22 21:48   tests/test_app_paths.py
    31890  2026-09-22 21:48   tests/test_backup_restore.py
    70112  2026-09-22 21:48   tests/test_batch_engine.py
    19226  2026-09-22 21:48   tests/test_checkpoints.py
    26822  2026-09-22 21:48   tests/test_circuit_breaker.py
     7389  2026-09-22 21:48   tests/test_config_names_paths.py
    86602  2026-09-22 21:48   tests/test_control_manager.py
    13408  2026-09-22 21:48   tests/test_domain_models.py
     6240  2026-09-22 21:48   tests/test_ffmpeg_processing.py
     4450  2026-09-22 21:48   tests/test_fingerprint_state.py
     5547  2026-09-22 21:48   tests/test_gerar_textos_exception_persistence.py
     8899  2026-09-22 21:48   tests/test_gerar_textos_ollama_adversarial.py
     4796  2026-09-22 21:48   tests/test_gerar_textos_whisper_adversarial.py
    25385  2026-09-22 21:48   tests/test_job_engine.py
     6839  2026-09-22 21:48   tests/test_job_state_machine.py
    17472  2026-09-22 21:48   tests/test_legacy_json_migration.py
     3209  2026-09-22 21:48   tests/test_limpeza_permission_error.py
    23595  2026-09-22 21:48   tests/test_media_probe.py
     5888  2026-09-22 21:48   tests/test_migrations_frozen.py
    11476  2026-09-22 21:48   tests/test_operational_audit.py
    16612  2026-09-22 21:48   tests/test_painel_oficial_horarios_por_dia.py
     4723  2026-09-22 21:48   tests/test_painel_oficial_youtube_copyright_timeout_menu.py
    20700  2026-09-22 21:48   tests/test_publication_idempotency.py
    36620  2026-09-22 21:48   tests/test_recovery_manager.py
    37274  2026-09-22 21:48   tests/test_release_packaging.py
   105416  2026-09-22 21:48   tests/test_resource_manager.py
     4676  2026-09-22 21:48   tests/test_resume_detection.py
    31831  2026-09-22 21:48   tests/test_retry_policy.py
     7501  2026-09-22 21:48   tests/test_schedule_slots.py
    19241  2026-09-22 21:48   tests/test_secrets_manager.py
    89276  2026-09-22 21:48   tests/test_shutdown_coordinator.py
    21566  2026-09-22 21:48   tests/test_source_context.py
    18065  2026-09-22 21:48   tests/test_source_import.py
    29758  2026-09-22 21:48   tests/test_source_import_options.py
    24664  2026-09-22 21:48   tests/test_sqlite_storage.py
     7928  2026-09-22 21:48   tests/test_state_json_persistence.py
   116323  2026-09-22 21:48   tests/test_storage_manager.py
     5928  2026-09-22 21:48   tests/test_time_utils.py
---------                     -------
  3077293                     120 files
```

**Nota**: como este relatório (`PROMPT_26_MEDIA_PROBE_RELATORIO.md`) é escrito *depois* da construção do ZIP acima, ele próprio não está incluído nesse ZIP específico — mesmo padrão já usado no relatório do Prompt 25 (seção 13 daquele relatório). O relatório é entregue separadamente à máquina Windows, com sua própria verificação byte-a-byte.

**Varredura de padrões sensíveis** (`grep -rIniE "senha|password|token|cookie|authorization|api[_-]?key|secret"` sobre o conteúdo extraído do ZIP): todas as ocorrências encontradas são referências benignas — nomes de módulo (`secrets_manager.py`, `SecretsManager`, `DpapiSecretsBackend`), documentação descrevendo o que o produto NUNCA envia/persiste (`RISCOS_ATUAIS.md`, `MAPA_DE_DADOS.md`, `PRODUCT_INVARIANTS.md`, `CLAUDE.md`), marcadores explicitamente fictícios usados em teste (`TOKEN_FICTICIO_123`, `COOKIE_FICTICIO_456`, `SEGREDO_FICTICIO`), e trechos de relatórios anteriores citando os mesmos termos como parte de confirmações de ausência de segredos. Nenhum segredo real, credencial ou dado sensível encontrado — mesmo padrão já confirmado nos Prompts 24b/25.

Os 6 arquivos `.bat`/`.txt` emprestados foram removidos do sandbox de nuvem imediatamente após a construção do ZIP acima.

# GATE 19.5 — ESTÁGIO 2 — RELATÓRIO (ENTREGA PARCIAL HONESTA)

> **Correção de terminologia (pedida explicitamente na Parte 2):** em toda a
> seção "PARTE 1" abaixo, onde está escrito "BUG-CANDIDATO-01/02/03", leia-se
> **CANDIDATO A / CANDIDATO B / CANDIDATO C** — eram achados de inspeção
> estática (leitura de código), não reproduções determinísticas, e não
> deveriam ter sido chamados de "bugs reproduzidos". A Parte 2 (mais abaixo)
> reproduz cada um deles com testes reais e dá o veredito final de cada um:
> **CANDIDATO A → REPRODUCED**, **CANDIDATO B → REPRODUCED (parcial, com uma
> correção de escopo em relação à Parte 1)**, **CANDIDATO C → REPRODUCED (o
> mais sério dos três, confirmado tanto no YouTube quanto no TikTok)**.

## Status desta entrega

Este relatório é uma **entrega parcial**, seguindo o mesmo padrão já usado e aceito no
Estágio 1 deste projeto (Prompt 19 original também foi entregue em partes). O escopo
pedido no Estágio 2 é gigantesco: 25 seções, cobrindo ~5.300 linhas de código legado
(`painel_oficial.py`, `limpar_metadados_oficial.py`, `gerar_textos.py`,
`agendar_youtube.py` — 1756 linhas, `agendar_tiktok.py` — 859 linhas, `state_json.py`,
`login_conta.py`, `app_paths.py`, `time_utils.py`), com dezenas de cenários de teste
enumerados manualmente.

**Não vou declarar as 25 seções como "concluídas" sem evidência real**, pois isso
violaria diretamente o princípio central do CLAUDE.md e a regra explícita do próprio
Estágio 2 ("Não usar o roadmap como prova... evidência real de código").

O que segue é o que **foi de fato lido, mapeado e verificado por código** até este
ponto — com achados concretos, incluindo dois bugs arquiteturais reproduzíveis por
inspeção de código nos agendadores. Nenhuma migration foi tocada. Nenhuma integração
G1↔G2 foi feita. `empacotar_release.py` não foi alterado.

---

## 1. Mapa real do fluxo (USUÁRIO → PAINEL → AÇÃO → SCRIPT → DEPENDÊNCIA → OUTPUT → ESTADO)

Base: leitura completa de `_sistema/painel_oficial.py` (513 linhas).

```
USUÁRIO
  └─ executa PAINEL_OFICIAL.bat → python _sistema/painel_oficial.py
       └─ main(): menu numérico (0-7), loop até sair
            │
            ├─ [Selecionar/criar canal] → ensure_account()
            │     cria pasta da conta (videos/, dados/, logs/, perfil_youtube/, perfil_tiktok/)
            │     default_config() grava config.json por plataforma (idioma, horários,
            │     nao_e_para_criancas, exigir_formato_short, timezone_iana, etc.)
            │
            ├─ [Login/perfil] → run_engine(acc, "login_conta.py", [...])
            │     SUBPROCESSO separado → abre Chrome com --user-data-dir=perfil isolado
            │     por conta/plataforma. Bloqueia (wait()) até o usuário fechar o Chrome.
            │
            ├─ [Limpar/preparar vídeo] → run_engine(acc, "limpar_metadados_oficial.py",
            │     ['-i', videos_origem, '-o', videos_tratados, '-j', jobs,
            │      '--state-file', dados/limpeza_estado.json, '--log-dir', logs,
            │      '--sem-pausa'])
            │     SUBPROCESSO separado (dependência: ffmpeg/ffprobe no PATH)
            │
            ├─ [Gerar textos] → run_engine(acc, "gerar_textos.py", [...])
            │     SUBPROCESSO separado (dependências: Ollama HTTP local, faster-whisper)
            │     lê dados/textos_postagem.json, grava de volta (atômico)
            │
            └─ [Agendar YouTube/TikTok] → run_engine(acc, "agendar_youtube.py"/
                  "agendar_tiktok.py", [...])
                  SUBPROCESSO separado (Playwright + Chrome com perfil isolado da conta)
                  lê dados/textos_postagem.json + dados/estado_<plataforma>.json,
                  grava de volta (atômico)

ESTADO/PERSISTÊNCIA (todos os arquivos abaixo usam o padrão tmp+os.replace, atômico):
  dados/config.json            — configuração por conta/plataforma
  dados/limpeza_estado.json    — resume de limpeza de vídeo (por fingerprint sha256)
  dados/textos_postagem.json   — título/descrição/hashtags gerados (por fingerprint)
  dados/estado_youtube.json    — fila/histórico de agendamento YouTube
  dados/estado_tiktok.json     — fila/histórico de agendamento TikTok
  perfil_youtube/, perfil_tiktok/ — perfis de browser isolados por conta (login persiste
    no próprio Chrome, não em JSON do produto)
```

**Achado-chave, já confirmado no Estágio 1 e reconfirmado aqui por leitura completa:**
cada ação do menu roda como **subprocesso independente** (`run_engine()` chama
`py_cmd()+[script]+args` com `cwd=ROOT` e `ACCOUNT_DIR` no ambiente). Isto significa que
o "smoke test" pedido na seção 17 não pode ser um teste de função única — precisa
testar cada script isoladamente (com fakes internos) OU testar via subprocess real com
dependências mockadas via variável de ambiente/CLI. Ver seção "Pendências" abaixo.

---

## 2-3. Limpeza de vídeo / FFmpeg — `limpar_metadados_oficial.py` (820 linhas, lido por completo)

**O que existe de fato (comportamento real, não o que o roadmap promete):**

- `checar_ffmpeg()` verifica `shutil.which("ffmpeg")` e `shutil.which("ffprobe")` antes
  de qualquer processamento; se ausente, imprime erro e `sys.exit(1)` — **nunca finge
  sucesso sem FFmpeg**.
- `obter_info_video()` usa `ffprobe -show_streams -show_format`, falha explicitamente
  (retorna `None`, que vira "pular este vídeo") se: `returncode != 0`, JSON inválido,
  nenhum stream de vídeo, ou resolução `<= 0`. Vídeo sem áudio é tratado (`has_audio =
  audio is not None`), não é erro.
- `processar_video()`: se o FFmpeg retornar código != 0, o **arquivo de destino
  parcialmente escrito é apagado** (`destino.unlink()`), o original nunca é tocado (é só
  `-i`, leitura), e o item fica marcado `status: "failed"` no estado — **nunca marcado
  como concluído por engano**.
- Resume por fingerprint (`fingerprint_origem` = sha256(tamanho + 1MB inicial + 1MB
  final)): reexecução não reprocessa itens `status == "done"` cujo arquivo de saída
  ainda existe e tem tamanho > 0.
- Retry automático: itens que falharam no lote paralelo são reprocessados
  sequencialmente uma vez (`modo="retry"`) antes de desistir; se falharem de novo,
  ficam `status: "failed"` com motivo salvo, e a próxima execução do programa tenta
  de novo automaticamente (não perde o item).
- Nomeação de saída (`001.mp4`, `002.mp4`...) nunca sobrescreve número já usado —
  `proximo_indice_com_estado()` combina o maior índice já em disco, o maior índice já
  postado historicamente (lê `estado_youtube.json`/`estado_tiktok.json`), e o maior
  índice já reservado no próprio estado de limpeza.

**Chamadas reais de FFmpeg mapeadas (única linha, seção `processar_video`):**
filtro de vídeo (`zoompan` progressivo 100→104%, `eq` contraste/saturação/brilho/gamma,
`colorbalance`, `vignette`, `noise`, `unsharp`), `setpts` para variação de velocidade
0.99x–1.01x, `atempo`+`volume=1.01` no áudio (só se `has_audio`), `-map_metadata -1`
zera metadados do contêiner e depois reinjeta metadados falsos de smartphone
(creation_time/GPS/modelo — região fixa em São Paulo, data até 12/09/2026).

**Cenários dos 13 pedidos na seção 2 — status de verificação nesta entrega:**
| Cenário | Verificado por leitura de código | Testado automaticamente (novo) |
|---|---|---|
| MP4 normal | Sim (fluxo principal) | Não ainda |
| Nome com espaços/Unicode | Caminho tratado como `Path`/`str()`, sem parsing manual — não há motivo funcional para falhar, mas **não testado** | Não |
| Arquivo somente leitura | FFmpeg escreve só no destino, nunca no original — não testado | Não |
| Arquivo inexistente | `encontrar_videos()` só lista o que existe no disco (`rglob`); path inexistente simplesmente não entra na lista | Não |
| Arquivo corrompido | `obter_info_video` retorna `None` se ffprobe falhar → item pulado, sem crash | Não |
| Vídeo sem áudio | Tratado explicitamente (`-an`, sem filtro `-af`) | Não |
| FFmpeg falhando (exit≠0) | Confirmado: destino apagado, status=failed, retry automático | Não |
| FFmpeg ausente | `checar_ffmpeg()` aborta antes de começar | Não |
| Pasta sem permissão | Não tratado explicitamente — `mkdir(parents=True, exist_ok=True)` pode levantar `PermissionError` não capturado em `processar_pasta` | **BUG candidato, ver abaixo** |
| Cancelamento/interrupção | Não há tratamento de `KeyboardInterrupt`/SIGTERM explícito no meio do FFmpeg — o `Popen` fica órfão se o processo pai morrer abruptamente | **Risco identificado, não testado** |

**BUG-CANDIDATO-01 (MINOR, não corrigido ainda — requer reprodução isolada antes de
mexer em produção, conforme regra central do Estágio 2):** `processar_pasta()` chama
`saida.mkdir(parents=True, exist_ok=True)` e `log_dir.mkdir(...)` sem `try/except`. Se a
pasta de saída estiver em um caminho sem permissão de escrita (ex.: unidade
somente-leitura, política de grupo), o processo encerra com traceback Python cru em vez
da mensagem amigável que o resto do script usa. Reprodução isolada ainda não executada
neste ciclo — fica registrado como pendência de teste, não como fix aplicado.

---

## 4. Geração de texto / Ollama — `gerar_textos.py` (334 linhas, lido por completo)

Mapa real: `ollama_alive()`/`try_start_ollama()` (spawna `ollama serve` se não
responder) → `generate_text()` chama `/api/chat` com `format:"json"` forçado →
`parse_json_text()` faz `json.loads` direto, com fallback de regex `\{.*\}` se vier
lixo ao redor do JSON → validação explícita de campos obrigatórios (título+descrição
para YouTube, caption para TikTok) que **levanta `RuntimeError` se faltar campo ou vier
vazio** — nunca marca sucesso com dado inválido. `visual_summary()` usa
`/api/chat`→fallback `/api/generate` especificamente para o modelo de visão
(`moondream`), usado quando o vídeo não tem transcript (silêncio).

Cache/idempotência: `metadata_signature()` gera um hash de
`{pipeline, platform, locale, country, text_model, vision_model}`; um item só é
considerado "pronto" (pulável) se `status=="done" AND (manual_edit OR
pipeline_versao==assinatura_atual)`. **Mudar idioma/país/modelo invalida
automaticamente o cache antigo**; um texto editado manualmente pelo usuário nunca é
sobrescrito silenciosamente.

Isolamento de falha no lote: `main()` processa vídeo por vídeo com `try/except`
individual — uma falha marca `status:'failed', ultimo_erro:str(ex)` e loga em
`logs/textos_erros.log`, sem derrubar o lote inteiro; código de saída final é `0` só se
`fail==0`.

**BUG-CANDIDATO-02 (DEBT, achado real de código, ainda não corrigido):** o campo
`ultimo_erro` grava `str(ex)` **diretamente no JSON persistente**
(`textos_postagem.json`), que depois é exportado via `export_csv()`. O CLAUDE.md (seção
de segredos) proíbe persistir `str(exc)`/conteúdo arbitrário vindo de exceção sem
sanitização. Na prática, como Ollama roda localmente e as exceções capturadas aqui são
majoritariamente HTTP/JSON/timeout locais, o risco real hoje é baixo — mas é uma
violação literal da regra e um vetor real se, no futuro, uma exceção vier a conter
alguma informação sensível (ex.: um path completo do usuário, ou cabeçalho de uma
chamada de rede futura). **Não corrigido nesta entrega**: o mesmo padrão (`str(ex)` →
estado persistido) se repete em `limpar_metadados_oficial.py` (`last_error`) e
potencialmente nos agendadores — corrigir um lugar isoladamente sem revisar todos
criaria inconsistência. Fica registrado como bug reproduzido e documentado; correção
cirúrgica coordenada fica para a próxima parte desta entrega, mediante confirmação de
que é para corrigir agora (mensagens genéricas + `str(ex)` só no log local, nunca no
JSON exportável).

Os 17 cenários A-Q pedidos na seção 4 (Ollama on/off, timeout, JSON inválido, resposta
vazia, campos faltando, Unicode, PT-BR/EN, resposta longa, caracteres estranhos,
processo interrompido) **estão mapeados no código acima mas ainda não têm testes
automatizados com Ollama mockado** — fica como pendência explícita, não como "testado".

---

## 5. Whisper/transcrição — dentro de `gerar_textos.py` (não é módulo separado)

Confirmado por leitura, **não presumido**: usa `faster_whisper.WhisperModel`,
import lazy dentro de `try/except ImportError` (se ausente, imprime
`[ERRO] faster-whisper não instalado` e retorna código 2 — não crasha o processo
inteiro). Parâmetros: `model.transcribe(str(video), task='transcribe', language=None,
vad_filter=True, beam_size=1, condition_on_previous_text=False)`, tamanho do modelo
configurável (`cfg.ia_local.whisper_model_size`, padrão `'small'`), `device='cpu'`,
`compute_type='int8'`. Cache de transcript por fingerprint do vídeo
(`transcript_cache()`). Quando a transcrição vem vazia (vídeo mudo/sem fala), o sistema
cai para `visual_summary()` (descrição por frames via modelo de visão) em vez de falhar.

Cenários pedidos (PT-BR, EN, sem fala, sem áudio, áudio curto, saída vazia, modelo
ausente, modelo corrompido, falha de processo, timeout, Unicode, cancelamento): mapeados
estruturalmente pelo código acima; **testes automatizados com Whisper mockado ainda não
escritos**.

---

## 6. Legendas — classificação **NOT_IMPLEMENTED** (achado consolidado)

Duas coisas diferentes com o nome "legenda" no código, que não podem ser confundidas no
relatório final:

1. **Legenda = campo de descrição/caption do TikTok na UI** (`agendar_tiktok.py`):
   `textarea[placeholder*="legenda" i]`, `set_caption()`. Isso é **IMPLEMENTADO** — é
   apenas o nome do campo de texto que acompanha o vídeo no TikTok, não tem relação com
   legenda embutida (subtitle).
2. **Legenda = arquivo de vídeo com legenda embutida/alternativa** (`agendar_youtube.py`,
   `schedule_one()`): `video = item.get("upload_path") or item["path"]`, com um print
   `"(enviando versão legendada: ...)"` se `upload_path != path`. Confirmado por dois
   greps no projeto inteiro (`_sistema/`):
   - `upload_path` é **lido nessa única linha** e **nunca escrito/atribuído em lugar
     nenhum** do código atual.
   - Não há nenhum filtro FFmpeg de burn-in (`drawtext`, `subtitles=`, `ass=`) em
     `limpar_metadados_oficial.py` nem em nenhum outro arquivo de `_sistema/`.

**Classificação final: NOT_IMPLEMENTED.** É um gancho morto/inatingível deixado no
agendador do YouTube — não é bug funcional (nunca é ativado), mas é código morto que
pode confundir manutenção futura. Não implementar a função agora (regra explícita do
Estágio 2: "não implementar função futura só para completar o gate").

---

## 9-10-11. Agendadores (YouTube 1756 linhas / TikTok 859 linhas) — achado crítico: modelo de 4 camadas de horário

Confirmado por grep + leitura direcionada (não é leitura linha-a-linha completa dos
~2600 linhas combinadas ainda — ver pendências):

**Seção 11 — pergunta obrigatória respondida: SIM, a Geração 1 usa de fato os helpers
já testados de `time_utils.py`.** Tanto `agendar_youtube.py` quanto
`agendar_tiktok.py` importam e usam `timezone_name_from_config`, `utc_now`,
`local_today`, `local_wall_time_to_utc`, `utc_to_local`, `schedule_utc_from_record`,
`ensure_timezone_config`, `iana_zone` — as mesmas funções cobertas por
`test_time_utils.py` (22 testes) e usadas por `build_slots()` em ambos os agendadores
para calcular os horários candidatos em UTC, convertidos para o timezone IANA explícito
da conta. **Não há gap arquitetural aqui** — isto é uma boa notícia, e diferente do que
a seção 11 especulava como possível ("se NÃO: registrar como GAP ARQUITETURAL"), o
resultado real é que já está integrado.

**As 4 camadas, mapeadas por código:**

- `CALCULATED_TIME` = `target_dt`, produzido por `build_slots()` (YouTube linha 231,
  TikTok linha 659) — datetime aware, no timezone IANA da conta, derivado de
  `cfg["horarios"]`/`horarios_por_dia` + estado já agendado (nunca reusa slot ocupado).
- `PERSISTED_TIME` = gravado em `dados/estado_<plataforma>.json` após `schedule_one()`
  retornar sucesso (não lido em detalhe nesta rodada — confirmar formato exato fica
  pendente).
- `DISPLAY_TIME` = o que é efetivamente digitado/selecionado na UI da plataforma:
  - YouTube: `set_date()` (`target_dt.strftime("%d/%m/%Y")` digitado no campo de
    calendário) + `set_time()` (varre inputs visíveis da `ytcp-visibility-scheduler`
    procurando um campo de hora por heurística de atributos).
  - TikTok: `set_schedule_datetime()` (clica em opções de um seletor de hora/minuto em
    passos de 5 minutos, depois navega o calendário por diferença de meses e clica no
    dia).
- `PLATFORM_TIME` = o que a plataforma efetivamente salva/mostra depois de confirmado.

**BUG-CANDIDATO-03 (MAJOR, arquitetural, reproduzido por inspeção de código —
corresponde exatamente ao comportamento histórico do TikTok mencionado pelo usuário):**
**nenhum dos dois agendadores lê de volta o valor DISPLAY_TIME antes de confirmar o
agendamento**, para comparar com CALCULATED_TIME:

- TikTok `set_schedule_datetime()` (linhas 378-550): clica na opção de hora/minuto no
  seletor customizado (`click_time_option`) e no dia no calendário, mas **não relê o
  valor selecionado** em nenhum desses seletores antes de `schedule_one()` chamar
  `click_final_schedule()`. Se o clique acertar a opção errada (ex.: lista rolou, texto
  duplicado, seletor mudou de posição — o próprio código já lida com "o primeiro clique
  pode fechar/recriar a lista"), **o programa segue em frente acreditando que o horário
  está correto**, sem qualquer verificação.
- YouTube `confirm_success()` (linhas 1037-1122): após `click_next`/confirmar,
  verifica apenas se o **status** do vídeo virou "Programado/Scheduled" (nunca aceita
  "Rascunho/Draft" como sucesso — isso está correto), mas **em nenhum momento lê de
  volta o horário exibido na linha do vídeo (`ytcp-video-row`) para comparar com
  `target_dt`**. Um vídeo pode ficar corretamente "Programado" e ainda assim programado
  para o horário errado, sem que `confirm_success()` detecte.

**Isto é precisamente o tipo de divergência CALCULATED≠PLATFORM que a seção 10 pediu
para não presumir corrigida** — e de fato não está corrigida: a proteção que existe hoje
cobre "não virou rascunho", não "o horário bateu". Correção cirúrgica seria adicionar
uma leitura de confirmação do horário exibido (TikTok: reler o valor do picker antes de
`click_final_schedule`; YouTube: extrair o horário da linha confirmada em
`ytcp-video-row` e comparar com `target_dt`) — **não implementada nesta entrega**, pois
mexer nos dois agendadores de produção é uma mudança de risco não-trivial (seletores
Playwright reais contra Studio/TikTok ao vivo) que deveria, por prudência, ser testada
manualmente no Windows antes de ser considerada "correção", não só lida no código. Fica
registrada como BUG reproduzido e documentado, aguardando decisão do usuário sobre
prioridade de correção nesta fase.

---

## 12. Persistência / `state_json.py` (33 linhas, lido por completo)

```python
def load_state_json(path, default):
    if not path.exists(): return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateJsonReadError(path, exc) from exc
```

Contrato já correto por design: arquivo ausente → estado default (comportamento
esperado no primeiro uso); arquivo existente mas corrompido → **exceção explícita**
(`StateJsonReadError`), nunca tratado silenciosamente como "vazio". Isso já satisfaz o
princípio do CLAUDE.md de nunca perder estado silenciosamente.

Escrita: não existe função de escrita centralizada em `state_json.py` — cada chamador
(`painel_oficial.py`, `gerar_textos.py`, `limpar_metadados_oficial.py`) implementa seu
próprio `save_json`/`save_json_atomic`, todos idênticos no padrão
`tmp = path.with_suffix('.tmp'); tmp.write_text(...); tmp.replace(path)` —
`Path.replace()` é atômico no nível do SO (rename), então uma escrita interrompida no
meio nunca deixa o arquivo final truncado: ou o `.tmp` fica incompleto (e o arquivo
"real" anterior permanece intacto), ou a troca já terminou. **Isto cobre a exigência de
atomicidade por design**, mas os cenários específicos pedidos (`PermissionError` durante
`tmp.replace`, dois processos escrevendo ao mesmo tempo, dois canais) **ainda não foram
reproduzidos com teste real** — fica como pendência.

---

## Baseline preservado (seção 22)

Nenhum teste foi apagado, alterado, marcado `xfail` ou `skip` nesta entrega. Nenhum
código de produção foi modificado. O baseline de 901 testes (0 failed) do Estágio 1
permanece intocado — não há novo número de testes a reportar ainda, pois nenhum teste
novo foi escrito nesta parte.

## Migrations (seção 23) e empacotamento (seção 24)

Nenhuma alteração. `m001`/`m002`/`m003` permanecem com os mesmos hashes congelados do
Estágio 1; nenhum `m004` foi criado. `empacotar_release.py` não foi tocado nesta
entrega — nenhuma proteção foi enfraquecida.

---

## O que fica pendente para a Parte 2 desta entrega (não fabricado, listado honestamente)

1. Leitura linha-a-linha completa de `agendar_youtube.py` (login, seleção de vídeo,
   título/descrição/hashtags/tags, made-for-kids, privacidade, upload, limite diário,
   copyright claim) e `agendar_tiktok.py` (login, upload, challenge/captcha,
   confirmação final) além dos trechos já mapeados.
2. Seções 7 (isolamento de cache título/descrição entre vídeos/canais), 8 (isolamento
   canal A/B/C alternado), 13 (fechar/reabrir smoke test), 14 (lote com item que falha
   no meio), 15 (simulação de dependências externas indisponíveis) — mapeadas
   estruturalmente pelo código já lido, mas **sem testes automatizados novos ainda**.
3. `tests/test_generation1_smoke.py` — smoke test em processo com fakes de
   FFmpeg/Whisper/Ollama.
4. Testes determinísticos mockados para os cenários das seções 3, 4, 5, 12.
5. Matriz de integração futura G1→G2 (seção 21).
6. `TESTE_MANUAL_WINDOWS_ESTAGIO2.md` (seção 19).
7. Decisão do usuário sobre corrigir agora ou não os 3 bugs-candidatos documentados
   acima (permissão de pasta em limpeza, `str(ex)` persistido, ausência de readback de
   horário nos dois agendadores) — nenhum foi corrigido nesta entrega, pois a regra
   central do Estágio 2 exige reprodução isolada antes de correção, e os dois mais
   sérios (candidatos 2 e 3) tocam múltiplos arquivos de produção e merecem confirmação
   explícita antes de qualquer edição.
8. Nova rodada de pytest + empacotamento + ZIP + entrega Windows, só depois que os itens
   acima estiverem prontos com evidência real — não faz sentido gerar um novo ZIP antes
   de haver testes novos para incluir nele.

Esta era a Parte 1 da entrega do Estágio 2: mapeamento real e achados de código
verificados, sem testes automatizados novos e sem nenhuma correção aplicada. **A
Parte 2, abaixo, reproduz com testes reais o que a Parte 1 só tinha levantado por
leitura de código.**

---
---

# PARTE 2 — REPRODUÇÃO FUNCIONAL + AGENDADORES + PERSISTÊNCIA + ISOLAMENTO

## Status desta entrega

`empacotar_release.py` não foi enfraquecido — apenas 2 entradas conscientes foram
adicionadas ao `ALLOWED_ROOT_FILES` (`GATE_19_5_ESTAGIO2_RELATORIO.md` e
`TESTE_MANUAL_WINDOWS_ESTAGIO2.md`), exigidas pelo próprio teste
`test_manifesto_cobre_arquivos_de_raiz_do_projeto_real`, que falhou até essa
classificação consciente ser feita — o mecanismo de defesa funcionou como
projetado. Nenhuma migration foi tocada. Nenhuma integração G1↔G2 foi feita.
Nenhum vídeo/conteúdo real foi publicado em teste automatizado.

**Nomenclatura usada abaixo, por achado:** `CANDIDATE` (inspeção estática, sem
teste), `REPRODUCED` (teste determinístico real prova o comportamento),
`FIXED` (corrigido e coberto por teste de regressão), `DISMISSED` (investigado
e não se sustenta / risco não existe na prática), `DEFERRED_ARCHITECTURE`
(correção exigiria decisão G1→G2, fora de escopo aqui).

## Arquivos modificados/criados nesta Parte 2

Modificados:
- `empacotar_release.py` — 2 entradas novas em `ALLOWED_ROOT_FILES` (classificação
  consciente dos 2 documentos novos desta entrega, nada de proteção enfraquecida).
- `GATE_19_5_ESTAGIO2_RELATORIO.md` — este arquivo.

Criados (todos novos, nenhum teste existente foi alterado ou removido):
- `tests/fakes_playwright.py` — double mínimo da API do Playwright usado só em teste.
- `tests/test_agendar_tiktok_time_layers.py` — 2 testes, CANDIDATO C (TikTok).
- `tests/test_agendar_youtube_time_layers.py` — 1 teste, CANDIDATO C (YouTube).
- `tests/test_state_json_persistence.py` — 9 testes, persistência/restart/atomicidade.
- `tests/test_limpeza_permission_error.py` — 2 testes, CANDIDATO A.
- `tests/test_gerar_textos_exception_persistence.py` — 3 testes, CANDIDATO B.
- `tests/test_gerar_textos_ollama_adversarial.py` — 14 testes, cenários Ollama.
- `tests/test_gerar_textos_whisper_adversarial.py` — 8 testes, cenários Whisper.
- `TESTE_MANUAL_WINDOWS_ESTAGIO2.md` — roteiro manual A-N.

Nenhum código de produção em `_sistema/` foi alterado nesta Parte 2. Todos os
achados abaixo são reproduzidos e documentados, não corrigidos — conforme a regra
central do Estágio 2 ("não corrigir MAJOR automaticamente sem primeiro mostrar a
reprodução").

## Contagem de testes

Baseline de entrada: **901 PASSED, 0 FAILED**.
Nesta Parte 2: **+39 testes novos**, todos passando.
Total atual (ambiente de desenvolvimento Linux, `pytest -q`): **940 PASSED, 0
FAILED**. `python3 -m compileall -q _sistema tests empacotar_release.py` — OK, sem
erro de sintaxe.

**Esta contagem foi rodada no ambiente de desenvolvimento (Linux), não no Windows
real.** Conforme a regra permanente deste projeto, o Gate não pode ser declarado
completo sem a validação Windows real (`.venv-test\Scripts\python.exe -m pytest
-q`) — isso continua pendente para o fechamento desta Parte 2/entrega ao usuário,
exatamente como nas rodadas anteriores.

---

## CANDIDATO A — PermissionError na criação da pasta de saída (limpeza de vídeo)

**Veredito: REPRODUCED.**

Teste: `tests/test_limpeza_permission_error.py` — monkeypatcha `Path.mkdir` para
levantar `PermissionError` exatamente no ponto identificado na Parte 1
(`saida.mkdir(parents=True, exist_ok=True)`, dentro de `processar_pasta()`).

Comportamento real observado (rodando o código de produção sem alteração):
- a exceção **propaga sem nenhum tratamento** até o chamador — o programa não
  captura, não grava log amigável, não deixa nenhum rastro de diagnóstico;
- o vídeo original **permanece intocado** (a falha ocorre antes de qualquer
  chamada ao FFmpeg, então não há risco de perda de dado);
- nenhum arquivo de estado (`limpeza_estado.json`) chega a ser criado/corrompido.

**Severidade confirmada: MINOR.** Não há perda de dado nem estado inconsistente —
o problema é só uma experiência ruim (traceback Python cru em vez de mensagem
amigável) num cenário específico (pasta de saída sem permissão de escrita, ex.:
política de grupo, unidade somente leitura). Correção sugerida (não aplicada
nesta entrega): envolver o `mkdir` num `try/except PermissionError` com uma
mensagem amigável, no mesmo padrão já usado em `checar_ffmpeg()`. Fica pendente de
autorização explícita para aplicar, já que qualquer edição em
`limpar_metadados_oficial.py` deve ser cirúrgica e isolada.

---

## CANDIDATO B — `str(exc)` persistido em estado/log

**Veredito: REPRODUCED (com escopo corrigido em relação à Parte 1) + DISMISSED
para um ponto específico.**

Teste: `tests/test_gerar_textos_exception_persistence.py` — usa uma exceção
sintética contendo 4 marcadores fictícios (`TOKEN_FICTICIO_123`,
`COOKIE_FICTICIO_456`, um caminho `C:\Users\Pessoa\arquivo.mp4`, e uma URL com
`?token=SEGREDO_FICTICIO`) e as funções REAIS de persistência
(`gerar_textos.save_json`, `gerar_textos.export_csv`) para provar exatamente onde
o texto termina.

Resultado, por destino:
| Destino | Os 4 marcadores aparecem em texto puro? | Veredito |
|---|---|---|
| `dados/textos_postagem.json` (campo `ultimo_erro`) | **Sim**, verbatim | **REPRODUCED** |
| `logs/textos_erros.log` (log local) | **Sim**, verbatim | **REPRODUCED** (risco baixo — arquivo é só local, não é enviado automaticamente a lugar nenhum) |
| `dados/textos_postagem.csv` (exportado via `export_csv()`) | **Não** — o campo `ultimo_erro` nem sequer está na lista fixa de colunas do CSV | **DISMISSED** |

**Correção em relação à Parte 1:** o relatório anterior afirmou que `ultimo_erro`
"é exportado via `export_csv()`". Isso estava **errado** — `export_csv()` usa uma
lista fixa de 10 colunas (`arquivo, titulo, descricao, caption, hashtags,
pais_alvo, idioma, status, atualizado_em, fingerprint`) que não inclui
`ultimo_erro`. O teste `test_csv_exportado_nao_inclui_ultimo_erro_achado_positivo_corrige_parte1`
prova isso rodando o `export_csv()` real. Isto reduz a superfície real do
CANDIDATO B: o vazamento fica contido em 2 arquivos locais (JSON de estado +
log), nenhum dos dois é gerado para compartilhamento/exportação por padrão.

**Mapeamento adicional feito nesta Parte 2** (grep em todo `_sistema/`, não só
`gerar_textos.py`): outro ponto real de persistência de texto de exceção existe
em `agendar_youtube.py` (linha ~1675) — `direitos_autorais_bloqueados.txt` grava
`str(CopyrightClaimError)` (a mensagem da própria exceção de "direitos autorais
detectados", que hoje é sempre uma string estática definida no próprio código, não
texto arbitrário vindo de uma resposta HTTP/rede) verbatim num arquivo de log
local. Risco: baixo (mensagem controlada pelo próprio código, não texto de
terceiros), mas fica registrado para completude. Não foi coberto por teste
automatizado nesta Parte 2 — pendência para Parte 3 se o usuário quiser.
`limpar_metadados_oficial.py` **não** usa `str(exc)` em nenhum estado persistido —
usa apenas strings estáticas (`"Falha no FFmpeg; será tentado novamente..."`),
correto por design.

**Não foi aplicada nenhuma sanitização nesta entrega** (conforme instruído: "não
implementar sanitização até termos o mapa completo"). O mapa acima é o resultado
completo para `_sistema/` na Geração 1. Observação à parte: a Geração 2
(`shutdown_coordinator.py`, `storage_manager.py`, `resource_manager.py`) já tem
uma política documentada e deliberada de NUNCA persistir `str(exc)`/`repr(exc)` —
isso é só uma observação de contexto, não faz parte do escopo G1 do Estágio 2 e
não foi tocado.

---

## CANDIDATO C — ausência de verificação de horário antes da confirmação final

**Veredito: REPRODUCED, tanto no TikTok quanto no YouTube.** Este é o achado mais
sério dos três candidatos.

### Mapa de camadas — TikTok (`_sistema/agendar_tiktok.py`)

| Camada | Existe no código atual? | Onde |
|---|---|---|
| REQUESTED_TIME | Fora de escopo (vem da configuração `horarios`/`horarios_por_dia`, não de um pedido pontual do usuário por vídeo) | — |
| CALCULATED_TIME | Sim | `build_slots()` → `target_dt` |
| PERSISTED_TIME | Sim, mas só DEPOIS de `schedule_one()` retornar sem exceção | `main()`, grava em `estado_tiktok.json` via `canonical_schedule_fields()` |
| INPUT_TIME | Sim | `set_schedule_datetime()`: `hour_txt`/`minute_txt` calculados de `target_dt` |
| OBSERVED_UI_TIME | **NOT_IMPLEMENTED** | não existe leitura de volta do campo de hora/minuto depois do clique |
| CONFIRMED_TIME | **NOT_IMPLEMENTED** | nenhuma comparação acontece antes de `click_final_schedule()` |
| RESULT_TIME | **NOT_IMPLEMENTED** | `schedule_one()` só faz melhor esforço lendo texto da página (toast "scheduled"/"agendado"), nunca extrai o horário real salvo |

Prova por teste real (`tests/test_agendar_tiktok_time_layers.py`, chamando
`agendar_tiktok.set_schedule_datetime()` de produção, sem alteração, com um
Playwright fake): depois que a função termina com sucesso, o campo de hora
(`time_el`) só é lido 3 vezes — todas ANTES de qualquer clique, só para
identificar qual input é o de hora (`find_date_time_inputs`). Depois disso, **zero
leituras adicionais** ocorrem: nem `input_value()`, nem um segundo
`get_attribute("value")`. A função retorna ao chamador sem qualquer garantia de
que o picker realmente ficou no horário calculado.

### Mapa de camadas — YouTube (`_sistema/agendar_youtube.py`)

| Camada | Existe no código atual? | Onde |
|---|---|---|
| CALCULATED_TIME | Sim | `build_slots()` → `target_dt` |
| PERSISTED_TIME | Sim, só após sucesso | grava em `estado_youtube.json` (não lido em detalhe nesta rodada) |
| INPUT_TIME | Sim | `set_time()`: `wanted = target_dt.strftime('%H:%M')` |
| OBSERVED_UI_TIME | **NOT_IMPLEMENTED** | `set_time()` clica na opção que bate com `wanted` (ou preenche o campo via fallback), mas nunca relê o campo depois |
| CONFIRMED_TIME | **PARCIAL** | `confirm_success()` existe e é rígido quanto a STATUS (nunca aceita "Rascunho/Draft" como sucesso), mas não valida horário |
| RESULT_TIME | **NOT_IMPLEMENTED** | mesma limitação: nenhuma extração/comparação de horário na linha confirmada |

Prova por teste real (`tests/test_agendar_youtube_time_layers.py`, chamando
`agendar_youtube.confirm_success()` de produção): uma linha de vídeo simulada
contendo o texto "Programado" e um horário deliberadamente **divergente** do
calculado (23:59 em vez de 18:00, embutido só no texto simulado da linha) é
aceita como sucesso (`True`) sem qualquer objeção — porque a função nunca procura
por um horário na linha, só pelas palavras-chave de status.

### Duplicação de código entre os dois agendadores

Confirmado por leitura (pedido explícito da Parte 2 — documentar, não
refatorar): ambos implementam, de forma totalmente independente uma da outra, a
mesma estratégia de "encontrar a opção cujo texto bate com o horário calculado,
clicar, seguir em frente" — e a mesma ausência de leitura de confirmação. Não foi
criado nenhum helper comum nesta etapa.

### Conclusão

Isto reproduz, em código real e executável (não apenas por leitura), exatamente a
classe de bug relatada historicamente no TikTok — e mostra que o mesmo padrão de
risco existe também no YouTube, apesar de nunca ter sido relatado lá. A
invariante que o Estágio 2 pediu para eventualmente existir
(`CONFIRMED_TIME == CALCULATED_TIME` antes do clique final) **não existe hoje em
nenhum dos dois agendadores**. Conforme instruído, **nenhuma correção foi
implementada** — isto fica registrado como REPRODUCED, aguardando decisão
explícita do usuário sobre prioridade, já que a correção (ler de volta o valor do
seletor de hora no TikTok; extrair e comparar o horário da linha confirmada no
YouTube) mexe em código de produção que interage com seletores reais de
Playwright contra sites que mudam — risco não-trivial, melhor tratado como item
próprio de uma próxima parte, com teste de regressão + validação manual Windows
(ver `TESTE_MANUAL_WINDOWS_ESTAGIO2.md`, passos I e J) antes de ser considerado
fechado.

---

## Timezone — TikTok/YouTube usam `time_utils.py`? (seção 11 da Parte 1, reconfirmado)

Reconfirmado por rastreamento de import + uso real (não só grep): ambos os
agendadores importam e usam diretamente `timezone_name_from_config`, `utc_now`,
`local_today`, `local_wall_time_to_utc`, `utc_to_local`, `schedule_utc_from_record`
dentro de `build_slots()`. Os testes já existentes em `tests/test_schedule_slots.py`
já conectam esse helper ao agendador G1 de verdade (chamam
`youtube.build_slots()`/`tiktok.build_slots()` diretamente, não uma reimplementação)
— ou seja, o pedido específico da Parte 2 ("o teste deve conectar o helper ao
AGENDADOR G1, não bastar testar time_utils isoladamente") **já estava satisfeito
antes desta entrega**, por testes pré-existentes no baseline de 901. Não é uma
lacuna nova. Os casos extremos pedidos (23:59, 00:00, virada de dia/mês/ano,
horário passado, próximo slot) já estão cobertos em `test_schedule_slots.py` e
`test_time_utils.py` — não foram duplicados aqui.

---

## Persistência / state_json / restart — provado (não mais só afirmado)

Teste: `tests/test_state_json_persistence.py` (9 testes, todos passando):

- arquivo inexistente → usa default (comportamento correto);
- arquivo de 0 bytes → `StateJsonReadError` explícito, **nunca** interpretado
  como "sem estado ainda";
- JSON inválido → `StateJsonReadError`;
- JSON truncado no meio de uma string → `StateJsonReadError`;
- `PermissionError` na leitura → convertido em `StateJsonReadError`, não crash cru;
- ciclo salvar → "fechar instância" → nova leitura → comparar: dados idênticos;
- dois canais com arquivos de estado separados: **zero vazamento** de dado entre
  eles;
- falha durante a escrita do `.tmp` (`PermissionError` simulado): o **último
  estado válido em disco não é destruído** — a escrita nova nunca chega a
  substituí-lo;
- falha durante o `replace()` final: mesma garantia — o arquivo real nunca fica
  truncado nem parcialmente substituído.

**Veredito: a afirmação da Parte 1 ("escrita atômica e contrato de erro
adequados") está CONFIRMADA por teste real, não mais só por leitura de código.**
Nenhum bug encontrado nesta área.

---

## Ollama — resultado dos testes adversariais

`tests/test_gerar_textos_ollama_adversarial.py`, 14 testes, todos passando,
chamando `gerar_textos.generate_text()`/`ollama_chat()` reais com
`urllib.request.urlopen` mockado (sem rede, sem Ollama real):

Cobertos: resposta normal (YouTube), Ollama offline (connection refused),
timeout, HTTP 500 (vira `RuntimeError` com corpo truncado, nunca falso sucesso),
JSON inválido, resposta vazia, resposta só espaços, título faltando (YouTube),
descrição faltando (YouTube), caption faltando (TikTok), Unicode PT-BR
preservado, resposta com lixo ao redor do JSON (fallback regex funciona),
normalização de hashtags para exatamente 4, e confirmação de que uma resposta
inválida nunca "herda"/reaproveita um resultado anterior válido.

**Confirmado por teste real: nenhum cenário testado transforma uma resposta
inválida em falso sucesso.** Todos os cenários de falha levantam exceção
explícita. Ainda faltam (dos 17 A-Q originais da Parte 1): resposta excessivamente
longa, caracteres verdadeiramente exóticos (emoji/RTL), processo Ollama
interrompido no meio de uma chamada (`ConnectionResetError` a meio da leitura do
corpo) — pendência explícita para Parte 3, não fabricado como testado.

## Whisper — resultado dos testes

`tests/test_gerar_textos_whisper_adversarial.py`, 8 testes, todos passando,
chamando `gerar_textos.transcribe()`/`get_transcript()` reais com um fake mínimo
de `faster_whisper.WhisperModel` (sem carregar modelo real):

Cobertos: transcrição normal PT-BR, transcrição EN, sem fala (retorna string
vazia, não é erro — é o caminho que ativa o fallback visual), segmentos só com
espaços são descartados, exceção do `model.transcribe()` propaga sem ser engolida
silenciosamente, parâmetros passados ao model batem com o documentado
(`task='transcribe', language=None, vad_filter=True, beam_size=1,
condition_on_previous_text=False`), cache por fingerprint funciona (segunda
chamada não retranscreve), fingerprints diferentes não compartilham cache.

Não cobertos nesta parte (ficam pendentes, e são exatamente os que dependem de um
modelo/executável real, coerente com a regra "separar MOCK de REAL"): modelo
ausente/corrompido de verdade, timeout real de um processo `faster_whisper`
travado. Esses exigem um roteiro de integração real no Windows, não uma suíte
mockada.

---

## Legenda — classificação preservada

`NOT_IMPLEMENTED`, como já estabelecido na Parte 1. Nenhuma mudança: nenhum código
de legendas foi implementado nesta Parte 2, conforme instruído.

---

## Perfis/canais (Prioridade 3) e smoke test da Geração 1 — NÃO feitos nesta Parte 2

Ficam explicitamente como pendência, não fabricados:
- Cenário sintético A1/B1/C1/A2/B2/C2 (YouTube EN, YouTube PT-BR, TikTok EN
  alternados) com verificação de isolamento completo de idioma/título/
  descrição/hashtags/vídeo/pasta/perfil/estado/horário/configuração.
- `tests/test_generation1_smoke.py` (fluxo mínimo fake ponta a ponta).

O motivo é honesto, não é "faltou tempo" genérico: o cenário de isolamento de
canais exige simular corretamente `account_paths()`/`ensure_account()` (que já
têm testes próprios em `test_app_paths.py`) combinados com os fluxos de
`gerar_textos.py`/`agendar_*.py` de forma coerente entre si, e o smoke test
precisa decidir explicitamente se testa no nível de subprocesso (fiel ao
`run_engine()` real, mas lento e mais difícil de mockar) ou no nível de função
Python direta (rápido, mas testa uma integração que nunca acontece exatamente
assim em produção, já que cada script roda isolado). Essa decisão de desenho
merece ser explicitada e não apressada — fica para a Parte 3.

---

## O que fica pendente para a Parte 3

1. Cenário de isolamento de canais A1/B1/C1/A2/B2/C2 (Prioridade 3).
2. `tests/test_generation1_smoke.py`.
3. Cenários Ollama restantes (resposta longa, caracteres exóticos, processo
   interrompido a meio da leitura HTTP).
4. Teste de integração real (roteiro separado, seção 18 da Parte 1) para FFmpeg
   real, Whisper real, Ollama real — não pytest, roteiro manual/documentado.
5. Cobertura de `str(exc)` em `agendar_youtube.py` (`direitos_autorais_bloqueados.txt`)
   com teste automatizado (achado mapeado, não testado ainda).
6. Decisão do usuário sobre aplicar as 3 correções cirúrgicas já desenhadas
   (CANDIDATO A: try/except amigável no mkdir; CANDIDATO B: mensagens
   genéricas no lugar de `str(ex)` nos 2 arquivos locais identificados;
   CANDIDATO C: leitura de confirmação de horário nos dois agendadores) — nenhuma
   foi aplicada nesta Parte 2, só reproduzida e documentada.
7. Matriz de integração futura G1→G2 (seção 21 da Parte 1) — ainda não escrita.
8. Validação Windows real (901+39=940 testes, compileall, empacotamento, ZIP com
   SHA-256) — só depois que as pendências acima (ou uma parte delas, com
   confirmação do usuário) estiverem fechadas, para não gerar um ZIP intermediário
   descartável.

Esta era a Parte 2: reprodução funcional real dos 3 candidatos da Parte 1 (todos
REPRODUCED, com 1 correção de escopo honesta no CANDIDATO B), prova por teste da
persistência/atomicidade de `state_json.py`, testes adversariais reais para
Ollama e Whisper, e o roteiro manual Windows A-N. Nenhuma correção de produção foi
aplicada — só reproduções, documentação e os 2 ajustes conscientes no manifesto de
empacotamento. **A correção cirúrgica do CANDIDATO C está na seção abaixo.**

---
---

# CORREÇÃO CIRÚRGICA DO CANDIDATO C — VALIDAÇÃO DE HORÁRIO ANTES DA CONFIRMAÇÃO REAL

## Escopo

Autorizado e aplicado: **somente** o CANDIDATO C (ausência de verificação de
horário/data antes do clique final, nos dois agendadores). CANDIDATO A e
CANDIDATO B **não foram tocados** nesta rodada, conforme instruído — continuam
documentados como pendência para a Parte 3. Nenhuma integração G1↔G2, nenhum
refactor amplo dos agendadores, nenhuma migration alterada.

## Comportamento ANTES vs. DEPOIS

**ANTES** (provado pelos testes da Parte 2, agora reescritos): depois de
`set_schedule_datetime()` (TikTok) ou de `set_date()`+`set_time()` (YouTube), o
fluxo prosseguia direto para o clique final (`click_final_schedule()` /
`click_done()`) sem nunca reler o que ficou de fato exibido na UI. Um clique
"certo" na opção errada, ou uma UI que divergisse silenciosamente do horário
calculado, não tinha como ser detectado.

**DEPOIS**: antes de qualquer clique final,
- **TikTok**: `set_schedule_datetime()` chama
  `read_observed_schedule_datetime(page, expected_dt)` (`expected_dt` já usa o
  horário arredondado para o passo de 5 minutos que o TikTok exige — a mesma
  regra que a própria função já aplicava para decidir o que clicar). Se o valor
  relido não bater exatamente (data E hora), levanta `RuntimeError` **dentro
  da própria função**, o que impede `schedule_one()` de sequer chegar perto de
  `click_final_schedule()`.
- **YouTube**: `schedule_one()`, logo após `set_date()`/`set_time()` e antes do
  bloco que chama `click_done()`, chama
  `read_observed_schedule_datetime(page, target_dt)`. Mesma regra: diverge →
  `RuntimeError` explícito, `click_done()` nunca é alcançado.

Em ambos os casos, `save_debug()` é chamado (screenshot + HTML) antes de
levantar o erro, para dar evidência de diagnóstico — sem persistir HTML bruto,
cookie ou token na mensagem de erro em si (a mensagem só contém os valores de
data/hora esperado vs. observado, já formatados como texto simples).

## Código produtivo alterado

- **`_sistema/agendar_tiktok.py`**:
  - Novas funções: `parse_observed_clock_text()`, `parse_observed_date_text()`,
    `_read_field_value()`, `read_observed_schedule_datetime()`.
  - `set_schedule_datetime()`: adicionado o bloco de verificação (leitura +
    comparação + abort) no final da função, depois do clique no dia do
    calendário e antes do retorno. Nenhuma outra linha da função foi alterada.
- **`_sistema/agendar_youtube.py`**:
  - Novas funções: `parse_observed_clock_text()`, `parse_observed_date_text()`,
    `_locate_time_field_again()`, `read_observed_schedule_datetime()`.
  - `schedule_one()`: adicionado o bloco de verificação logo após
    `set_time(page, target_dt)` e antes do bloco de "rede de segurança" que já
    existia (checagem do diálogo de upload) e de `click_done()`. Nenhuma outra
    linha da função foi alterada.
  - `set_date()`, `set_time()`, `click_done()`, `confirm_success()`: **não
    alterados**. `confirm_success()` continua só validando STATUS
    (Programado/Scheduled), por design — a validação de horário agora acontece
    antes dela, como um gate separado.

Nenhum outro arquivo de `_sistema/` foi tocado. `empacotar_release.py` não foi
tocado nesta rodada (não há arquivo novo na raiz do projeto para classificar).

## Formatos aceitos pelo parser (e por quê)

Por instrução explícita ("não criar um parser universal gigantesco" / "se o
formato for desconhecido, falhar fechado"), os parsers de horário/data são
deliberadamente estreitos:

- **Horário**: `HH:MM` ou `H:MM`, 24 horas. Não há suporte a AM/PM em nenhum
  dos dois agendadores — não existe, no código atual, nenhuma evidência de que
  o campo de horário do YouTube Studio ou do TikTok Studio apresente 12
  horas/AM-PM (o próprio código de produção sempre calcula e escreve em 24h).
  Se um formato AM/PM ou qualquer outro aparecer na prática, o parser
  corretamente levanta erro e o programa aborta ANTES de confirmar — não tenta
  adivinhar.
- **Data**: `DD/MM/YYYY`, com separador `/`, `-` ou `.`. É o único formato que
  o restante do código deste projeto já produz (`set_date()` do YouTube escreve
  exatamente `target_dt.strftime("%d/%m/%Y")`), então é o único que o parser
  precisa reconhecer com confiança hoje.

Se o roteiro manual (`TESTE_MANUAL_WINDOWS_ESTAGIO2.md`, passos I e J) revelar
que a UI real usa um formato diferente do esperado (por exemplo, se a conta do
YouTube Studio estiver configurada num idioma que muda o formato do campo),
isso vai aparecer como um abort com mensagem "não pôde ser interpretado" — o
comportamento correto e seguro, não uma falha silenciosa. Se isso acontecer,
é motivo para ampliar o parser na Parte 3, não para o programa publicar sem
confirmar.

## Testes de regressão

Os dois arquivos de teste que **provavam o bug** na Parte 2 foram reescritos
para **provar a correção** (nenhum teste foi removido; a mudança de
expectativa está documentada no docstring de cada arquivo):

- **`tests/test_agendar_tiktok_time_layers.py`** (2 → 10 testes):
  1. esperado == observado → não levanta;
  2. hora divergente → aborta;
  3. data divergente → aborta (mesmo com hora igual);
  4. campo vazio → aborta;
  5. campo ilegível → aborta;
  6. elemento "recriado" entre escrita e leitura → relocalizado corretamente
     (prova via contagem de chamadas a `page.locator()`, trocando o conteúdo
     do registro no meio do fluxo);
  7. leitura que lança exceção → aborta;
  8. arredondamento de 5 minutos: `target_dt` com minuto=17 (arredonda para
     15) — UI em 18:15 passa, UI em 18:17 (o minuto bruto, não arredondado)
     diverge e aborta;
  9. **mismatch → `click_final_schedule` nunca é chamado** (contagem de
     cliques no botão final = 0, instrumentado via `call_log`);
  10. fluxo completo via `schedule_one()` real (login/upload/legenda/toggle
      mockados, `set_schedule_datetime`/`click_final_schedule` reais): match
      → botão final clicado 1 vez; mismatch → botão final clicado 0 vezes e a
      exceção propaga.

- **`tests/test_agendar_youtube_time_layers.py`** (1 → 9 testes): mesma
  cobertura 1-7 aplicada a `read_observed_schedule_datetime()` do YouTube, mais
  um teste único cobrindo os pontos 9+10 (`click_done` nunca chamado em
  mismatch, chamado 1x em match, via `schedule_one()` real com os passos
  anteriores mockados/desligados por config), mais o teste já existente de
  `confirm_success()` da Parte 2, agora redocumentado como "ainda válido e
  intencional" em vez de bug isolado (a checagem de horário agora acontece
  antes dele).

**O teste mais importante** (`test_9_...`/`test_9_e_10_...`) prova exatamente o
que foi pedido: **mismatch → contagem de cliques no botão final = 0.**

## Contagem de testes

Baseline de entrada desta rodada: **940 PASSED, 0 FAILED** (confirmado
anteriormente pelo usuário no Windows real).
Testes reescritos (não contam como "novos", substituem os anteriores 1:1 em
comportamento, mas aumentam em quantidade): TikTok 2→10 (+8), YouTube 1→9
(+8).
**Total nesta rodada: 940 + 16 = 956 testes.**

Rodado no ambiente de desenvolvimento (Linux):
```
pytest --collect-only -q  ->  956 tests collected
pytest -q                 ->  956 passed, 0 failed
python -m compileall -q _sistema tests  ->  sem erro
```

**Esta contagem ainda precisa ser confirmada no Windows real** pelo usuário,
exatamente como nas rodadas anteriores — nenhum ZIP será gerado antes dessa
confirmação.

## Riscos conhecidos / dívida técnica desta correção

- O parser de data/hora é estreito por design (seção acima). Isso é uma
  escolha deliberada de segurança (falhar fechado), não uma limitação
  escondida — mas significa que, se o YouTube Studio ou TikTok Studio mudarem
  o formato do campo (ex.: atualização de UI, conta em outro idioma que
  reformate o campo), a nova proteção vai abortar agendamentos legítimos até o
  parser ser ampliado. Isso é o comportamento correto (falhar fechado > confiar
  cegamente), mas precisa ser monitorado no roteiro manual.
- A releitura do campo de data no YouTube exige reabrir o seletor de data
  (`#datepicker-trigger`) porque o campo de texto só existe enquanto o
  calendário está aberto — isso adiciona uma interação a mais (abrir, ler,
  fechar com Escape) imediatamente antes da confirmação. Risco: se o Escape
  falhar silenciosamente por algum motivo e o calendário ficar aberto, o clique
  final seguinte poderia interagir com o elemento errado. Mitigação atual:
  `click_done()` já procura o botão por seletor/role explícito, não por
  coordenada, então um calendário ainda aberto não deveria interceptar o
  clique — mas isto não foi validado contra o YouTube Studio real nesta rodada
  (só contra os fakes). Fica como ponto de atenção explícito para o roteiro
  manual, passo I.
- Nenhuma tentativa de retry/correção automática foi implementada — em caso de
  mismatch, o programa aborta e não tenta de novo sozinho. Isso é intencional
  (a instrução foi "não clicar", não "tentar corrigir"), mas significa que uma
  divergência real vai exigir intervenção manual/nova execução, não é
  auto-recuperável ainda.

## O que continua igual (não foi tocado)

CANDIDATO A e CANDIDATO B permanecem exatamente como a Parte 2 deixou —
REPRODUCED e documentados, sem correção aplicada. A matriz G1→G2, o smoke test
da Geração 1, e o cenário de isolamento de canais A/B/C continuam pendentes
para a Parte 3, como já estava registrado.
empacotamento.

---

# GATE 19.5 — ESTÁGIO 2 — TIKTOK: CORREÇÃO DO BUG REAL WINDOWS (19/09/2026)

## Contexto

A correção cirúrgica anterior (CANDIDATO C) introduziu o read-back
antes do clique final, mas ainda não tinha sido validada contra o TikTok
Studio real. A primeira execução real no Windows revelou dois problemas que
os testes daquela rodada não cobriam, com evidência concreta:

```
[+] 001.mp4 -> 20/09/2026 10:00
    Programado para: 20/09/2026 10:00

[ERRO] 001.mp4:
Data exibida no TikTok não pôde ser interpretada:
formato de data não reconhecido: '2026-09-20'
```

```
CALCULATED_TIME:  20/09/2026 10:00
REQUESTED_TIME:   20/09/2026 10:00
OBSERVED_UI_DATE: 2026-09-20
OBSERVED_UI_TIME: 21:00
```

Ou seja: a proteção da rodada anterior FUNCIONOU no sentido de nunca clicar
no botão final com um horário errado (o software parou sozinho, sem publicar
e sem pular/duplicar o vídeo — comportamento preservado e testado nesta
rodada). Mas ela não deveria ter abortado por causa do formato de data, e o
fato de UI ter ficado em 21:00 quando o pedido era 10:00 mostrou que a
seleção de hora/minuto em si tinha um problema, não só a validação final.

## BUG 1 — formato de data ISO não reconhecido

`parse_observed_date_text()` só aceitava `DD/MM/YYYY` (com separador `/`,
`-` ou `.`). O TikTok Studio real devolveu `2026-09-20` (ISO `YYYY-MM-DD`).

Corrigido de forma restrita: a função agora tenta primeiro o padrão
`^(\d{4})-(\d{1,2})-(\d{1,2})$` (ISO) e, se não bater, cai para o padrão
`DD/MM/YYYY` já existente. Continua falhando fechado (`ValueError`) para
qualquer formato fora desses dois — nenhum parser universal de datas foi
introduzido.

## BUG 2 — TARGET 10:00 virou UI 21:00

Causa raiz tratada (não apenas mascarada na validação final): a seleção de
hora/minuto usava `container.get_by_text(wanted, exact=True)` escopada a uma
lista obtida por índice (`lists.nth(0)`/`lists.nth(1)`), sem nunca reler o
campo de horário depois do clique para confirmar que a UI realmente mudou. Um
clique que "acontece" mas não altera a UI (ou que atinge a lista errada,
possível quando HORA e MINUTO compartilham o mesmo texto, ex.: hora "10" e
minuto "10") seguia em frente sem qualquer detecção até a validação final —
e mesmo essa validação final só comparava o resultado consolidado, sem dizer
qual etapa (hora ou minuto) tinha falhado.

Correção estrutural em `_sistema/agendar_tiktok.py`:

- **Seletor real do DOM**: a busca de opção agora usa a estrutura real do
  TikTok Studio (`div.tiktok-timepicker-option-item` com filho
  `span.tiktok-timepicker-option-text`), escopada estritamente ao container
  da lista sendo preenchida (`_locate_time_option`). Removido o
  `get_by_text`/busca de texto genérica.
- **Nunca busca global**: `_click_option_in_list(page, list_index, wanted_text)`
  relocaliza as listas a cada chamada e só procura dentro da lista
  `list_index` (0=HORA, 1=MINUTO) — nunca em toda a página. O fallback via
  `page.evaluate` segue o mesmo escopo (`querySelectorAll` dentro da mesma
  lista).
- **Read-back progressivo**: `_select_time_component()` clica a opção e IMEDIATAMENTE
  relê o campo de horário (`_read_time_field_now`, que relocaliza via
  `find_date_time_inputs` — nunca reaproveita referência antiga). Só
  prossegue se o texto relido bater com o esperado (`_hour_ui_matches` depois
  do clique de hora; `_hour_minute_ui_matches` depois do clique de minuto).
  Um clique sem efeito na UI é tratado como falha, não como sucesso.
- **Retry controlado**: no máximo 1 retry por componente (hora ou minuto),
  para cobrir o caso do TikTok recriar o DOM/picker no meio da seleção.
  Esgotado o retry, aborta em definitivo — nunca escolhe outro horário, nunca
  tenta um horário "próximo", nunca usa fallback aleatório.
- **Gate final preservado**: depois de hora, minuto e data selecionados,
  `read_observed_schedule_datetime()` continua sendo a última barreira antes
  de `click_final_schedule()`, agora também aceitando o formato ISO (BUG 1).

## Testes

`tests/test_agendar_tiktok_time_layers.py` foi reestruturado em 4 partes
(28 testes, antes 10):

- **A. `TikTokReadBackFunctionTests`** (11 testes) — unidade de
  `read_observed_schedule_datetime()`: match, hora/data divergente, campo de
  horário/data vazio, texto ilegível de hora/data, **data ISO aceita**
  (dia/mês com 1 ou 2 dígitos), exceção durante a leitura, relocalização via
  `page.locator`.
- **B. `TikTokTimePickerSelectionTests`** (6 testes) — unidade de
  `_select_time_component`/`_click_option_in_list`: seleção com sucesso;
  **clique sem efeito na UI aborta após 1 retry** (reprodução direta do BUG 2
  no nível da função); item não encontrado aborta; retry controlado quando a
  lista é recriada no meio da seleção; **lista de HORA não confunde com
  lista de MINUTO** e vice-versa, com "10" presente nas duas listas ao mesmo
  tempo (cenário explícito de confusão de lista).
- **C. `TikTokScheduleDatetimeIntegrationTests`** (9 testes) — ponta a ponta
  de `set_schedule_datetime()`: caminho feliz; **reprodução exata da
  evidência Windows real (TARGET 10:00 -> UI 21:00) abortando antes do
  clique final**; hora correta/minuto errado aborta; **data ISO aceita no
  fluxo completo**; data divergente aborta no gate final; opção de hora
  ausente aborta; arredondamento de 5 minutos continua correto; DOM recriado
  entre escrita e leituras é sempre relocalizado; botão final nunca chamado
  em mismatch.
- **D. `TikTokScheduleOneClickCountTests`** (2 testes) — `schedule_one()`
  real: horário batendo clica o botão final (1x), horário divergente nunca
  clica (0x) e a exceção propaga sem marcar o vídeo como agendado (prova de
  que "Parei para NÃO pular nem duplicar vídeo" continua válido).

## Baseline

```
956 passed (antes desta correção)
974 passed (depois — +18 testes, nenhum removido)
python -m compileall -q _sistema tests  ->  sem erro
```

Confirmação Windows real (`collect-only` + suíte completa) ainda pendente —
nenhum ZIP será gerado antes dessa confirmação, seguindo a mesma disciplina
das rodadas anteriores.

## O que NÃO foi tocado nesta rodada

- `_sistema/agendar_youtube.py` — YouTube não foi tocado (já confirmado
  funcionando no teste real).
- Migrations — continuam congeladas (m001/m002/m003; sem m004).
- CANDIDATO A e CANDIDATO B — continuam como estavam, sem correção.
- Prompt 20 e Parte 3 — não iniciados.

---

# GATE 19.5 — ESTÁGIO 2 — TIKTOK: VERIFICAÇÃO DE DIREITOS AUTORAIS/CONTEÚDO INCOMPLETA (NOVO BUG REAL WINDOWS, 19/09/2026)

## Contexto

Confirmação Windows real: depois da correção do bug de data/hora
(TARGET 10:00 -> UI 21:00, seção anterior deste relatório), o TikTok passou a
selecionar corretamente data e hora. O teste avançou até o clique no botão
final de agendamento, quando o TikTok Studio exibiu um modal de interrupção:

```
Continuar publicando?

A verificação de direitos autorais está incompleta.
Publicar seu vídeo agora irá interromper a verificação.

Ainda estamos verificando se há possíveis problemas com seu vídeo.
Você deseja continuar publicando antes da verificação ser concluída?

[Cancelar]  [Publicar agora]
```

A automação ficou bloqueada nesse modal — bug real reproduzido no Windows.

## Comportamento implementado

Por padrão, o produto NUNCA clica em "Publicar agora". Fluxo novo em
`_sistema/agendar_tiktok.py`, inserido entre `set_schedule_datetime()`
(que já valida data/hora) e `click_final_schedule()`:

```
data/hora aplicadas -> read-back validado (já existia)
  -> aguardar verificações (NOVO: wait_for_tiktok_checks)
  -> verificações concluídas (CHECK_PASSED)
  -> clique final
  -> se o modal "Continuar publicando?" aparecer mesmo assim (corrida):
       cancelar com segurança -> aguardar verificações de novo
       -> 1 retry controlado do clique final
       -> se o modal aparecer de nova vez: ABORTAR definitivamente
```

### `get_tiktok_preflight_status(page)`

Lê a seção "Verificações" do TikTok Studio (melhor hipótese de seletor
estrutural, com fallback por texto do cabeçalho — ver nota de dívida técnica
abaixo) e devolve um status agregado por severidade dentre `CHECK_PENDING` /
`CHECK_PASSED` / `CHECK_WARNING` / `CHECK_FAILED` / `CHECK_UNKNOWN`. Cada item
é classificado preferindo atributo/estado estrutural (classe, `data-status`,
`aria-label`) e só cai para o texto do item como fallback. A seção não
encontrada, ou um item cujo estado não pôde ser determinado, NUNCA vira
`CHECK_PASSED` — vira `CHECK_UNKNOWN`, que bloqueia o clique final tanto
quanto um erro explícito (fail closed).

### `wait_for_tiktok_checks(page, timeout_seconds=240, poll_interval=3)`

Polling determinístico (nunca um `sleep` fixo gigante) com limite de tempo.
Devolve `CHECK_PASSED` quando as verificações concluírem OK. Levanta
`RuntimeError` — sem clicar em nada — se: (a) não concluir dentro do timeout
(`PENDING`/`UNKNOWN` persistente); (b) voltar `WARNING` ou `FAILED` (essas
exigem revisão manual, nunca publicação forçada — abortado imediatamente,
sem esperar o timeout inteiro). Timeout configurável via
`cfg["timeout_verificacoes_tiktok_segundos"]` (padrão 240s).

### `handle_continue_publishing_modal_if_present(page)`

Detecta o modal pelo texto real do título ("Continuar publicando?"),
escopado — nunca assume que qualquer modal visível é esse. Se aparecer:
clica em "Cancelar" (nunca em "Publicar agora"); se não conseguir clicar em
"Cancelar" com segurança, ABORTA (nunca deixa a automação parada
indefinidamente esperando um clique manual).

## Testes

Novo arquivo `tests/test_agendar_tiktok_preflight_checks.py` (29 testes) em
4 partes: unidade de `get_tiktok_preflight_status`/classificação de item (10
testes, incluindo agregação por severidade com itens mistos e fallback texto
vs. estrutura); unidade de `wait_for_tiktok_checks` (6 testes: já concluído,
pending→passed, pending até timeout, warning, failed, unknown fail-closed);
unidade do tratamento do modal (4 testes: ausente, presente com cancelamento
bem-sucedido, Cancelar não encontrado aborta, modal genérico não confundido);
e `schedule_one()` ponta a ponta (9 testes) cobrindo exatamente os 10
cenários pedidos — checks concluídos prossegue; pending→passed prossegue;
pending até timeout aborta sem publicar; warning/failed não forçam
publicação; estado desconhecido fail-closed; modal aparece uma vez → cancela
e permite 1 retry; modal aparece de novo após o retry → aborta em definitivo
(nunca uma 3ª tentativa); nenhum cenário de bloqueio imprime mensagem de
sucesso. Em todo teste, o botão "Publicar agora" é instrumentado e o clique
nele é sempre 0 (`auto_click_count == 0`).

`tests/test_agendar_tiktok_time_layers.py` (rodada anterior) precisou de um
pequeno ajuste: os testes de `schedule_one()` daquele arquivo (focados em
data/hora, não neste gate) passaram a registrar uma seção "Verificações" já
`CHECK_PASSED`, para não ficarem presos no `wait_for_tiktok_checks()` real
por falta de seção registrada — sem isso o teste ficaria esperando o timeout
inteiro (240s) antes de abortar. Nenhuma asserção desses testes mudou.

## Baseline

```
974 passed (antes desta correção)
1003 passed (depois — +29 testes, nenhum removido)
python -m compileall -q _sistema tests  ->  sem erro
```

## Riscos conhecidos / dívida técnica desta correção

- **Seletores da seção "Verificações" ainda não confirmados contra o HTML
  real.** Diferente da correção de data/hora (onde tínhamos o HTML real do
  erro), esta rodada não veio com uma captura de HTML da seção
  "Verificações" — só a descrição textual do usuário. Os seletores usados
  (`div[data-e2e="post_verifications"]` e variantes, com fallback por texto
  do cabeçalho "Verificações") são a melhor hipótese com base no padrão
  `data-e2e` já observado no restante do arquivo, mas **precisam ser
  validados no próximo teste Windows real**. Se o seletor não bater com o
  DOM real, `get_tiktok_preflight_status()` devolve `CHECK_UNKNOWN` (nunca
  `CHECK_PASSED` por engano) e `wait_for_tiktok_checks()` vai abortar por
  timeout em vez de prosseguir — comportamento seguro (fail closed), mas que
  pode bloquear agendamentos legítimos até os seletores serem ajustados com
  evidência real. Isto é esperado e deve ser tratado como o próximo ponto de
  atenção do roteiro manual (passo J), não como um novo bug se acontecer.
- O modal "Continuar publicando?" é detectado por
  `div[class*="common-modal"]` (mesmo padrão de classe já usado neste
  arquivo para o modal de permissão em `click_schedule_toggle`) com fallback
  para `div[role="dialog"]`. Também não confirmado contra o HTML real deste
  modal especificamente.
- Timeout padrão de 240s pode não ser suficiente em todos os casos ("o
  TikTok pode demorar vários minutos", conforme a própria instrução) —
  configurável via `cfg["timeout_verificacoes_tiktok_segundos"]` caso o
  teste real mostre necessidade de um valor maior.

## O que NÃO foi tocado nesta rodada

- `_sistema/agendar_youtube.py` — YouTube não foi tocado (fluxo real já
  funcionou neste gate).
- Migrations — continuam congeladas (m001/m002/m003; sem m004).
- CANDIDATO A e CANDIDATO B — continuam como estavam, sem correção.
- Prompt 20 e Parte 3 — não iniciados.

---

## 2026-09-20 — GATE 19.5 — ESTÁGIO 2 — TikTok: verificações por tipo +
## política automática de aviso de direitos autorais + controle de lote

### Evidência real Windows que motivou esta rodada

Vídeo `095.mp4`, TARGET 20/09/2026 20:00:

- Confirmado: a correção de data/hora da rodada anterior (estado
  intermediário "Hora solicitada: 20 / UI após hora: 20:50" antes do minuto
  ser ajustado) é **esperado**, não um bug — o estado final
  (`20/09/2026 20:00 == 20/09/2026 20:00`) é o único que importa. **Essa
  lógica não foi alterada nesta rodada** (nenhuma evidência de erro).
- Novo achado: a UI mostrou **WARNING** em uma verificação (direitos
  autorais: "Foram encontrados problemas de direitos autorais. Você ainda
  pode publicar este vídeo, mas ele será silenciado.") **enquanto outra**
  verificação (conteúdo: "Verificação em andamento. Isso levará cerca de 10
  minutos...") ainda estava **PENDING**. Isso prova que `WARNING` não é
  estado final quando outra verificação ainda não terminou, e que a
  implementação anterior (que priorizava `FAILED > WARNING > PENDING`)
  tinha essa ordem de prioridade **invertida**.

### Bug corrigido: prioridade de agregação (PENDING deve dominar)

`get_tiktok_preflight_status()`/`wait_for_tiktok_checks()` da rodada
anterior resolviam o estado geral checando `FAILED` e depois `WARNING`
*antes* de `PENDING` — ou seja, se uma verificação já tivesse WARNING/FAILED
mas outra ainda estivesse PENDING, o código tratava o WARNING/FAILED como
estado final e abortava/decidia cedo demais, ignorando que outra verificação
ainda não tinha terminado.

Nova função `aggregate_check_status(statuses)` (extraída para ser testável
isoladamente) corrige a ordem: **PENDING sempre domina**, mesmo sobre
FAILED. Só quando não sobra nenhum PENDING é que a severidade
`FAILED > WARNING > (tudo PASSED) > UNKNOWN` decide.

### Modelo por verificação (COPYRIGHT / CONTENT / OTHER)

`_gather_check_items()` agora devolve, para cada item, além do `status`
(PASSED/PENDING/WARNING/FAILED/UNKNOWN), um `kind` (`COPYRIGHT`, `CONTENT`
ou `OTHER`) classificado pelo texto do próprio item (`"direitos autorais"`/
`"copyright"` → COPYRIGHT; `"conteúdo"`/`"content"` → CONTENT; senão OTHER).
Isso é usado **exclusivamente** para decidir a elegibilidade da política de
aviso de copyright (abaixo) — a agregação do estado geral continua sendo
feita só por `status`, nunca por `kind`.

Também foi adicionado um padrão textual explícito ("você ainda pode
publicar" / "será silenciado") para classificar corretamente o WARNING de
copyright real observado — sem esse padrão, o texto "Foram encontrados
*problemas* de direitos autorais" corria o risco de cair no fallback
genérico e virar `UNKNOWN` (nunca virava `FAILED` por acidente, porque o
texto não bate com nenhum padrão de FAILED existente, mas o objetivo é
classificar corretamente como WARNING, não confiar em coincidência).

### Política `tiktok_copyright_warning_policy` (BLOCK / ALLOW)

Nova função `decide_tiktok_checks_outcome(items, copyright_warning_policy)`,
chamada por `wait_for_tiktok_checks()` só depois que
`aggregate_check_status()` deixa de devolver `CHECK_PENDING`:

- `PASSED` → sempre prossegue.
- `FAILED` → sempre bloqueia (`reason="failed"`), **nunca** liberado por
  ALLOW.
- `UNKNOWN` → sempre bloqueia (`reason="unknown"`), **nunca** liberado por
  ALLOW.
- `WARNING`:
  - Se **algum** item WARNING não for de `kind=COPYRIGHT` →
    sempre bloqueia (`reason="warning_non_copyright"`), **mesmo com ALLOW
    configurado** — a política é exclusiva para o aviso de copyright
    explicitamente classificado como publicável pela própria plataforma,
    nunca um `if warning: continue` genérico.
  - Se **todos** os itens WARNING forem `kind=COPYRIGHT`:
    - `policy=BLOCK` (padrão) → bloqueia (`reason="copyright_warning_block"`).
    - `policy=ALLOW` → prossegue automaticamente, e loga:
      ```
      COPYRIGHT WARNING
      POLICY = ALLOW
      ACTION = CONTINUE
      ```

`ALLOW` só é aplicado depois que `aggregate_check_status()` resolve para
`CHECK_WARNING` (nunca enquanto ainda houver `CHECK_PENDING` — testado
explicitamente).

### Política `tiktok_blocked_item_policy` (STOP_BATCH / SKIP_AND_CONTINUE)

Nova exceção `TikTokChecksBlockedError(RuntimeError)` com atributo
`.reason` (`"failed"`, `"unknown"`, `"copyright_warning_block"`,
`"warning_non_copyright"`, `"pending_timeout"`) — levantada por
`wait_for_tiktok_checks()` em todo caminho que não seja "seguro clicar".

O loop de processamento em lote de `main()` foi extraído para uma nova
função `process_prepared_batch(page, cfg, prepared, state)` (mesma lógica,
agora testável sem Playwright real). Com a política padrão (`STOP_BATCH`,
usada quando a config não define nada), o comportamento é **idêntico** ao
código anterior: para no primeiro erro, sem persistir nada daquele vídeo.

Com `tiktok_blocked_item_policy=SKIP_AND_CONTINUE`, um
`TikTokChecksBlockedError` (qualquer `reason`) **não** para o lote: o vídeo
é registrado em `state["skipped_log"]` (auditoria — nunca fonte de verdade
de agendamento) e o processamento segue para o próximo vídeo elegível. O
vídeo pulado **nunca** é gravado em `state["scheduled"]` — a autoridade
única sobre "o que foi agendado" continua sendo exclusivamente essa lista,
como antes. Qualquer outra exceção (não relacionada às verificações — DOM
não encontrado, divergência de data/hora, etc.) **sempre** para o lote,
independentemente da política configurada, porque essas indicam um problema
de automação mais amplo, não "este vídeo específico tem um aviso/problema
de conteúdo".

Log de início de lote (uma vez, no início de `process_prepared_batch`):
```
Política TikTok:
Avisos de direitos autorais: ALLOW
Problemas impeditivos: SKIP_AND_CONTINUE
```
(ou BLOCK/STOP_BATCH, conforme configurado.)

### Decisão arquitetural: persistência via config JSON por conta, NÃO SQLite

O prompt pediu para persistir as duas políticas "usando a infraestrutura
SQLite existente" e, explicitamente, para **não criar uma segunda fonte de
verdade** e **não criar uma migration nova** (m004 proibido sem parar e
documentar antes).

Investigação: `agendar_tiktok.py` (Geração 1) **não usa SQLite** — ele usa
um arquivo de config JSON por conta (`CONFIG_FILE = ACCOUNT_PATHS.config`,
já isolado por perfil/canal via `account_dir_from_env()`/`account_paths()`,
o mesmo mecanismo que já guarda `horarios`, `dias_janela`,
`timezone_iana`, etc.). O SQLite deste projeto é infraestrutura da Geração
2 (Job Engine), e a restrição permanente deste projeto (repetida em toda
rodada anterior) é **não integrar Geração 1 com Geração 2**.

Decisão: as duas novas chaves (`tiktok_copyright_warning_policy`,
`tiktok_blocked_item_policy`) foram adicionadas como campos simples do
**mesmo** arquivo de config JSON por conta que o TikTok G1 já usa e já
persiste com escrita atômica (`tmp` + `replace`, já existente em
`save_json()`). Isso satisfaz literalmente a cláusula de escape do próprio
prompt: *"Se a arquitetura atual NÃO suportar ainda configuração por
perfil/canal de forma segura via SQLite: não criar uma segunda fonte de
verdade. Documentar a limitação e usar somente o escopo existente mais
seguro."* — o escopo existente mais seguro é exatamente esse arquivo, que
já é por-conta, já sobrevive a restart/fechamento/nova execução, e não
introduz nenhuma escrita SQLite nova nem migration nenhuma. Nenhum m004 foi
criado ou cogitado como necessário.

Leitura das políticas é sempre feita com fallback seguro para valores
ausentes/inválidos: `tiktok_copyright_warning_policy` ausente ou
desconhecido → `BLOCK`; `tiktok_blocked_item_policy` ausente ou desconhecido
→ `STOP_BATCH` — os dois valores mais conservadores, idênticos ao
comportamento anterior a esta rodada quando a config não tinha essas
chaves.

### UI

Não implementada nesta rodada (explicitamente fora de escopo). O backend
(`_copyright_warning_policy_from_cfg`, `_blocked_item_policy_from_cfg`,
mais as duas constantes `TIKTOK_COPYRIGHT_WARNING_POLICIES` /
`TIKTOK_BLOCKED_ITEM_POLICIES`) já está pronto para uma futura tela expor
"Política de direitos autorais: [Bloquear aviso] [Permitir aviso]" e
"Problema impeditivo: [Parar lote] [Pular e continuar]" sem alterar a
lógica central.

### Testes

Arquivo novo: `tests/test_agendar_tiktok_copyright_policy.py` (42 testes),
organizados em 6 classes cobrindo os 40 cenários obrigatórios do prompt
(estados de agregação, política de copyright, controle de lote,
persistência, isolamento por conta, segurança) mais 2 testes extras
(política inválida cai para o padrão seguro; WARNING não-copyright nunca é
liberado por ALLOW).

Nenhum teste existente foi removido ou teve sua asserção enfraquecida.

```
1003 passed (antes desta rodada)
1045 passed (depois — +42 testes, nenhum removido)
python -m compileall -q _sistema tests  ->  sem erro
```

### Arquivos modificados

- `_sistema/agendar_tiktok.py`:
  - `aggregate_check_status()` (nova) — corrige a ordem de prioridade
    (PENDING domina).
  - `_classify_check_kind()` (nova) — COPYRIGHT/CONTENT/OTHER.
  - `_classify_check_item()` — novo padrão textual explícito para o WARNING
    de copyright publicável.
  - `_gather_check_items()` — agora devolve `{"label","kind","status"}` em
    vez de tupla `(label, status)`.
  - `get_tiktok_preflight_status()` — usa `aggregate_check_status()`.
  - `decide_tiktok_checks_outcome()` (nova) — decisão PROCEED/BLOCKED +
    reason, considerando a política de copyright.
  - `wait_for_tiktok_checks()` — assinatura ganhou
    `copyright_warning_policy`; passa a levantar `TikTokChecksBlockedError`
    (com `reason`) em vez de `RuntimeError` genérico; progresso do polling
    agora mostra status por verificação (não só uma lista de rótulos).
  - `TikTokChecksBlockedError` (nova exceção).
  - `TIKTOK_COPYRIGHT_WARNING_POLICY_*`, `TIKTOK_BLOCKED_ITEM_POLICY_*`,
    `_copyright_warning_policy_from_cfg()`, `_blocked_item_policy_from_cfg()`
    (novos).
  - `schedule_one()` — passa `copyright_warning_policy` para as duas
    chamadas de `wait_for_tiktok_checks()`.
  - `process_prepared_batch()` (nova, extraída de `main()`) — aplica
    `tiktok_blocked_item_policy`; `main()` agora só monta o contexto do
    Chrome e delega o loop a essa função.
- `tests/test_agendar_tiktok_copyright_policy.py` (novo).

### Riscos conhecidos / dívida técnica desta rodada

- Continua valendo a dívida técnica já declarada: os seletores da seção
  "Verificações" e do modal ainda não foram confirmados contra HTML real
  (isso não mudou nesta rodada).
- `warning_non_copyright` e `unknown` (com seção encontrada mas item
  ilegível) não têm evidência real de Windows ainda — só o texto/estrutura
  hipotética usada nos testes. Se o teste manual Windows mostrar um WARNING
  de um tipo de verificação diferente de copyright, vale revisar o texto de
  classificação com a evidência real, mas a política de segurança (nunca
  liberar por ALLOW) já está correta independentemente do texto exato.
- `state["skipped_log"]` é um campo novo, só para auditoria — não afeta
  retomada (que continua baseada exclusivamente em `state["scheduled"]`).
  Ainda não existe UI para o usuário consultar esse log; ele é só
  texto/JSON por enquanto.

### Validação Windows real pendente (obrigatória antes de declarar o Gate concluído)

1. Repetir com 1 vídeo real: confirmar que a validação de data/hora continua
   OK (sem alteração).
2. Provocar (ou aguardar) um cenário real de `Copyright=WARNING` +
   `Content=PENDING→PASSED`: confirmar que o programa continua esperando
   enquanto PENDING, e só decide depois.
3. Com `tiktok_copyright_warning_policy=BLOCK` (padrão): confirmar que o
   programa aborta sem agendar quando o resultado final é
   `Copyright=WARNING`.
4. Com `tiktok_copyright_warning_policy=ALLOW`: confirmar que o programa
   agenda automaticamente nesse mesmo cenário, sem pedir confirmação, e loga
   `COPYRIGHT WARNING / POLICY = ALLOW / ACTION = CONTINUE`.
5. Testar um lote pequeno com `tiktok_blocked_item_policy=SKIP_AND_CONTINUE`
   e um vídeo propositalmente problemático no meio: confirmar que ele é
   pulado (nunca marcado como agendado/publicado) e que o próximo vídeo é
   processado normalmente.

**Este Gate continua sem ser declarado concluído / sem ZIP gerado até essa
validação Windows real ser confirmada pelo usuário — igual às rodadas
anteriores.**

---

## 2026-09-20 (continuação) — GATE 19.5 — ESTÁGIO 2 — YouTube: mesma
## política de copyright/item bloqueado, mecanismo técnico investigado à parte

### Objetivo desta rodada

Estender `youtube_copyright_warning_policy` (BLOCK/ALLOW) e
`youtube_blocked_item_policy` (STOP_BATCH/SKIP_AND_CONTINUE) ao YouTube, com
o mesmo comportamento de fundo já validado no TikTok, **sem presumir** que o
mecanismo técnico do assistente de upload do YouTube é igual ao da seção
"Verificações" do TikTok.

### Diferença arquitetural confirmada (investigada, não presumida)

O TikTok expõe múltiplos itens de verificação com polling em tempo real
(`CHECK_PENDING` "vivo"). O YouTube, nesta etapa do assistente de upload
(`check_copyright_claims()` pré-existente), expõe uma verificação **única**:
o código já documentava que a varredura completa de Content ID continua em
segundo plano depois de publicado/agendado — não existe, nesta etapa, um
segundo estado observável de espera. Por isso o novo modelo do YouTube tem
`CHECK_PENDING` só enquanto a tela mostrar textualmente "ainda carregando"
(`verificando`/`aguarde`/etc.), e ao expirar o timeout sem uma leitura
conclusiva o resultado é `CHECK_UNKNOWN` — não um `CHECK_PENDING` inventado.

### Classificação FAILED vs WARNING — termos exatos usados

O texto antigo (`claim_terms`) misturava duas situações num único booleano.
Separados nesta rodada:

- **`CHECK_FAILED`** (bloqueio real de exibição — nunca elegível a ALLOW,
  mesmo que o mesmo texto também mencione terceiros):
  `"restrito em alguns países"`, `"bloqueado em alguns países"`.
- **`CHECK_WARNING`** (reivindicação de terceiros/Content ID, pode ser
  publicável — elegível a ALLOW):
  `"reivindicou"`, `"reivindicado por"`, `"terceiros reivindicaram"`,
  `"uma reivindicação de direitos autorais foi feita"`,
  `"conteúdo de terceiros foi encontrado"`,
  `"correspondência de conteúdo foi encontrada"`,
  `"correspondências de conteúdo foram encontradas"`,
  `"foi encontrado conteúdo reivindicado"`, `"conteúdo reivindicado"`,
  `"third-party content was found"`, `"a content match was found"`,
  `"claimed content was found"`, `"copyright claim"`.
- **`CHECK_PASSED`**: `"nenhum problema encontrado"`, `"não encontrámos
  problemas"`, `"não encontramos problemas"`, `"no issues found"`.
- **`CHECK_PENDING`** (só até o timeout): `"verificando"`, `"a verificar"`,
  `"a processar"`, `"aguarde"`, `"checking"`, `"processing"`.
- Qualquer outro texto → `CHECK_UNKNOWN` (nunca `PASSED` por omissão).

Prioridade quando o mesmo texto bate com mais de um padrão: CHECKING >
FAILED > PASSED > WARNING > UNKNOWN. Testado explicitamente (`test_4`,
classificação): um texto que menciona reivindicação de terceiros **e**
restrição de país continua `CHECK_FAILED`, nunca `CHECK_WARNING`.

### Mudança de comportamento deliberada: timeout agora falha FECHADO

`check_copyright_claims()` (função antiga) **não foi alterada** — continua
byte-a-byte com o mesmo comportamento observável de antes, **inclusive
falhando ABERTO no timeout** (`"Verificação não conclusiva ...; seguindo em
frente"`, ou seja, antes desta rodada um timeout deixava o vídeo prosseguir
sem confirmação). Ela continua definida e funcional, só não é mais chamada
por `schedule_one()`.

A nova função `_read_youtube_checks_status()`, que `schedule_one()` passou a
usar, falha **FECHADO**: timeout sem leitura conclusiva vira `CHECK_UNKNOWN`
→ `YouTubeChecksBlockedError` → nunca clica em `click_done()`. Isso é uma
mudança de comportamento real no caminho executado, e é **intencional e
explicitamente pedida** pelo prompt desta rodada ("timeout/erro ao ler a
seção -> CHECK_UNKNOWN" é um cenário de teste obrigatório) — sinalizada aqui
porque é exatamente o tipo de mudança de comportamento que merece ser
destacada, não porque seja uma decisão duvidosa: o comportamento antigo
(fail-open) era um risco latente que a própria arquitetura do TikTok já
evita.

### Políticas — mesmo vocabulário, DEFAULT diferente por plataforma (decisão deliberada)

`youtube_copyright_warning_policy` (BLOCK/ALLOW, default `BLOCK`) segue
exatamente a mesma regra do TikTok — investigada e confirmada idêntica:
ALLOW só se aplica a `CHECK_WARNING` resolvido, nunca a
FAILED/UNKNOWN/PENDING.

`youtube_blocked_item_policy` (STOP_BATCH/SKIP_AND_CONTINUE) tem um
**default deliberadamente diferente do TikTok**: `SKIP_AND_CONTINUE`, não
`STOP_BATCH`. Motivo: **antes desta rodada, o YouTube já pulava e colocava
em quarentena automaticamente qualquer vídeo com problema de direitos
autorais** (mover para `bloqueados/direitos_autorais/`, registrar em
`direitos_autorais_bloqueados.txt`, continuar a fila — código pré-existente,
não escrito nesta rodada). Mudar o default para `STOP_BATCH` teria sido uma
regressão silenciosa de comportamento já em produção (violando CLAUDE.md
#4, "preserve tudo que já funciona"). O TikTok não tinha esse conceito antes
desta série de rodadas, então pôde adotar `STOP_BATCH` (o mais conservador)
como default novo sem quebrar nada existente. Isso é reportado explicitamente
porque a diferença de default entre as duas plataformas é intencional, não
uma inconsistência.

O comportamento de quarentena em si (mover o arquivo, nunca apagar/alterar
o conteúdo, registrar o motivo, seguir para o próximo) **foi preservado
exatamente como estava** para o caminho `SKIP_AND_CONTINUE` — só a decisão
de pular-vs-parar agora passa pela política configurável, e o "motivo"
registrado agora vem do `reason` estruturado (`failed`/
`copyright_warning_block`/`unknown`) em vez de só o texto livre anterior.

### Persistência — correção da premissa do prompt (mesma decisão já tomada para o TikTok)

O prompt pediu para usar "a MESMA infraestrutura SQLite já definida para a
política do TikTok". **Isso parte de uma premissa incorreta**: a rodada
anterior NÃO usou SQLite para a política do TikTok — usou o arquivo de
config JSON por conta que o TikTok (Geração 1) já usava, porque o SQLite
deste projeto é infraestrutura da Geração 2 (Job Engine) e a integração
G1↔G2 é uma restrição permanente do projeto. Essa decisão já estava
documentada na seção anterior deste relatório.

Para o YouTube, a mesma lógica se aplica — e, na prática, de forma ainda
mais alinhada ao pedido original do que a princípio parece: `BASE =
account_dir_from_env("YT_CHANNEL_DIR", standalone_namespace=...)` e
`ACCOUNT_PATHS.config` seguem **exatamente o mesmo mecanismo** já usado pelo
TikTok (`app_paths.account_dir_from_env`/`account_paths`). E mais: quando o
painel normal lança os dois scripts (`agendar_tiktok.py` e
`agendar_youtube.py`) para a mesma conta, ambos recebem a mesma variável de
ambiente `ACCOUNT_DIR` (checada primeiro, antes de qualquer fallback
específico de plataforma) — ou seja, **os dois JÁ compartilham
fisicamente o MESMO arquivo `config_canal.json`** no uso normal via painel.
As chaves novas (`youtube_copyright_warning_policy`,
`youtube_blocked_item_policy`) foram adicionadas como campos simples desse
mesmo arquivo compartilhado, prefixadas por plataforma para nunca colidir
com as chaves do TikTok — literalmente "a mesma infraestrutura", só que
JSON por conta em vez de SQLite (nenhuma segunda fonte de verdade criada,
nenhuma migration nova, nenhum m004).

(`YT_CHANNEL_DIR`/`standalone_namespace="agendar_tiktok"` são fallbacks
usados **somente** em execução standalone fora do painel — nesse caso, sim,
cada script isola seu próprio diretório `_standalone/<namespace>` e os dois
arquivos de config seriam fisicamente separados. Isso é o comportamento
correto e já existente para esse cenário legado, não alterado aqui.)

### Isolamento entre plataformas do mesmo perfil

Confirmado: no uso normal (painel), TikTok e YouTube da mesma conta
compartilham o arquivo de config, mas nunca vazam dados um para o outro
porque as chaves são prefixadas (`youtube_*` vs `tiktok_*`) e cada módulo só
lê as suas próprias chaves, nunca as do outro. Testado explicitamente
(`YoutubeChecksIsolationTests.test_2`).

### "Publicar mesmo assim" — não existe no assistente do YouTube nesta etapa

O YouTube não tem, nesta etapa, um modal equivalente ao "Continuar
publicando?" do TikTok. A ação equivalente de segurança é: nunca chamar
`click_done()` (ação final de agendamento) quando o estado for FAILED,
UNKNOWN, ou WARNING+BLOCK — testado com contagem de chamadas = 0 em todos
esses cenários (`YoutubeScheduleOneSecurityTests`), incluindo o caso
`FAILED+ALLOW` (prova de que ALLOW nunca se estende a FAILED mesmo quando
configurado). Quando bloqueado, `discard_current_upload()` (já existente)
continua sendo chamado para não deixar rascunho órfão — comportamento
preservado, não novo.

### Log

No início do processamento (`main()`), antes do loop:
```
Política YouTube:
Avisos de direitos autorais: BLOCK
Problemas impeditivos: SKIP_AND_CONTINUE
```
Por vídeo bloqueado, mesmo formato usado no TikTok (vídeo, verificação,
motivo, política, ação, próximo vídeo quando houver skip). Nenhum
cookie/token/HTML bruto logado; mensagens permanecem sanitizadas (o
`detalhe`/`reason` são sempre texto controlado por este código, nunca
`str(exc)`/traceback de terceiros).

### Testes

Arquivo novo: `tests/test_agendar_youtube_copyright_policy.py` (38 testes),
7 classes: classificação (6), polling/timeout fail-closed (6), política
(6), lote (5), persistência (4), isolamento (4), segurança ponta a ponta via
`schedule_one()` (7).

Nenhum teste existente (TikTok, YouTube, data/hora, demais) foi removido ou
teve asserção enfraquecida.

```
1045 passed (antes desta rodada, ao final da parte TikTok)
1083 passed (depois — +38 testes, nenhum removido)
python -m compileall -q .  ->  sem erro
```

### Confirmação de escopo

- A lógica de data/hora (`read_observed_schedule_datetime`, `set_date`,
  `set_time`, Candidato C) **não foi tocada** em nenhum dos dois
  agendadores.
- O fluxo geral de `schedule_one()`/`main()` do YouTube permanece o mesmo
  fora do bloco de verificação de direitos autorais e do tratamento de
  exceção correspondente no loop de lote.
- Nenhuma migration nova foi criada (m004 não foi necessária nem cogitada —
  a persistência usa JSON por conta, não SQLite, pelos motivos acima).
- Prompt 20 e Parte 3 não foram iniciados.

### Arquivos modificados

- `_sistema/agendar_youtube.py`:
  - `YOUTUBE_CHECK_OK_TERMS` / `YOUTUBE_CHECK_CHECKING_TERMS` /
    `YOUTUBE_CHECK_FAILED_TERMS` / `YOUTUBE_CHECK_WARNING_TERMS` (novos,
    compartilhados entre a função antiga e a nova).
  - `check_copyright_claims()` — refatorada internamente para reaproveitar
    os termos compartilhados; comportamento externo idêntico (inclusive
    fail-open no timeout); não é mais chamada por `schedule_one()`.
  - `CHECK_PASSED`/`CHECK_WARNING`/`CHECK_FAILED`/`CHECK_UNKNOWN`/
    `CHECK_PENDING`, `YOUTUBE_COPYRIGHT_WARNING_POLICY_*`,
    `YOUTUBE_BLOCKED_ITEM_POLICY_*`,
    `_youtube_copyright_warning_policy_from_cfg()`,
    `_youtube_blocked_item_policy_from_cfg()`, `YouTubeChecksBlockedError`,
    `_classify_youtube_copyright_body()`, `_read_youtube_checks_status()`,
    `decide_youtube_checks_outcome()`, `_youtube_batch_action_for_blocked()`
    (todos novos).
  - `schedule_one()` — bloco de verificação de direitos autorais reescrito
    para usar `_read_youtube_checks_status()` + `decide_youtube_checks_outcome()`
    e levantar `YouTubeChecksBlockedError` em vez de `CopyrightClaimError`.
  - `main()` — novo bloco de log de política antes do loop; bloco
    `except CopyrightClaimError` substituído por `except
    YouTubeChecksBlockedError`, aplicando `youtube_blocked_item_policy`
    (preserva o comportamento de quarentena existente no caminho
    SKIP_AND_CONTINUE; STOP_BATCH é o caminho novo).
- `tests/test_agendar_youtube_copyright_policy.py` (novo).

### Riscos conhecidos / dívida técnica desta rodada

- Os termos de classificação (`YOUTUBE_CHECK_*_TERMS`) ainda não foram
  confirmados contra uma tela real do assistente de upload mostrando
  especificamente "restrito/bloqueado em alguns países" — a separação
  FAILED/WARNING é baseada no texto que já existia no código antes desta
  rodada (`claim_terms`), agora só reclassificado, não em uma nova captura
  de tela real. Se o teste manual Windows mostrar um texto diferente do
  esperado, a classificação deve ser ajustada com a evidência real, seguindo
  o mesmo padrão de honestidade já usado para o TikTok.
  - `CopyrightClaimError` continua definida, mas nada no projeto a levanta
  mais — mantida só por compatibilidade caso algum código externo a
  referencie (nenhum foi encontrado nesta investigação).
- `_read_youtube_checks_status()` não tem como diferenciar, na prática,
  "ainda carregando até o timeout" de "texto realmente não reconhecido"
  além do que a leitura repetida já oferece — as duas causas colapsam no
  mesmo `reason="pending_timeout"`. Isso é uma limitação real do que a tela
  do YouTube expõe nesta etapa (documentada, não uma lacuna de
  implementação).

### Validação Windows real pendente (YouTube, além da já pendente do TikTok)

1. Confirmar que a lógica de data/hora do YouTube continua OK (sem
   alteração).
2. Provocar um vídeo com reivindicação de terceiros sem menção a país:
   confirmar classificação WARNING (não FAILED).
3. Se possível, um vídeo com "restrito/bloqueado em alguns países":
   confirmar classificação FAILED (nunca WARNING, nunca liberado por ALLOW).
4. Com `youtube_copyright_warning_policy=BLOCK` (padrão): confirmar que o
   vídeo com WARNING não é agendado.
5. Com `youtube_copyright_warning_policy=ALLOW`: confirmar agendamento
   automático nesse mesmo cenário, com o log `COPYRIGHT WARNING` / `POLICY
   = ALLOW` / `ACTION = CONTINUE`.
6. Confirmar que, com a configuração padrão (sem definir
   `youtube_blocked_item_policy`), um vídeo bloqueado continua sendo
   movido para quarentena e a fila continua — comportamento igual ao de
   antes desta rodada.
7. Com `youtube_blocked_item_policy=STOP_BATCH`: confirmar que um vídeo
   bloqueado agora PARA o lote em vez de pular.

**Este Gate continua sem ser declarado concluído / sem ZIP gerado até essa
validação Windows real ser confirmada pelo usuário (TikTok e YouTube) —
igual às rodadas anteriores.**

---

## 2026-09-20 — GATE 19.5 — ESTÁGIO 2 (continuação) — CORREÇÃO: detecção de
## itens na seção "Verificações" do TikTok (bug real Windows)

### Contexto / bug relatado

Depois da correção de data/hora (validada no Windows real nesta mesma
sessão), o teste real avançou até a leitura da seção "Verificações" e
revelou um NOVO bloqueador, desta vez sempre presente (não intermitente):
a tela real do TikTok Studio mostra exatamente 2 checks (direitos autorais
de música + conteúdo simples, ambos com sucesso/verde), mas
`_gather_check_items()` encontrava 4 "itens" — os 2 reais mais 3
fantasmas, classificados `OTHER: DESCONHECIDO`. Como UNKNOWN corretamente
bloqueia a agregação (`aggregate_check_status`, não alterado nesta rodada),
o estado geral nunca resolvia para PASSED mesmo com as duas verificações
reais 100% ok, travando TODA publicação automática.

O usuário forneceu outerHTML real capturado no Windows provando a causa
raiz: o último seletor de fallback do `_gather_check_items()` antigo,
`div[class*="check"]`, casava com QUALQUER elemento cuja classe contivesse
a substring "check" — incluindo uma div puramente decorativa, sempre
presente e vazia (`content-check__divider`), e potencialmente outras
variantes `status-*` não relacionadas a um item real.

### Causa raiz confirmada

Não era um bug de classificação de texto (o único item real encontrado já
era lido corretamente) — era exclusivamente sobre QUAIS elementos entravam
na lista de itens. O seletor de fallback fazia "lista negra frágil" (casar
qualquer coisa com "check" na classe) em vez de "lista branca por
identidade/estrutura" (só contar o que é comprovadamente um check real).

### Mecanismo novo (evidência Windows real)

Cada check tem exatamente 5 divs-variante de status
(`status-ready`→PENDING, `status-checking`→PENDING, `status-error`→FAILED,
`status-warn`→WARNING, `status-success`→PASSED) dentro de um grupo de
status, só uma ativa por vez. O check de copyright tem identidade estável
via `div[data-e2e="copyright_container"]`; o de conteúdo não tem
`data-e2e` próprio e é localizado pelo texto do headline
("verificação de conteúdo simples", comparação manual em minúsculas —
funciona com qualquer capitalização, já que o fake/real Playwright não
garante `get_by_text` case-insensitive).

### Antes vs. depois do mecanismo de seleção de itens

**Antes:** `_gather_check_items()` tentava, em cascata,
`div[data-e2e*="verification_item"]` → `li` → `div[class*="verification"]`
→ `div[class*="check"]` (o último SEMPRE batia no DOM real, capturando
elementos decorativos).

**Depois:** dois passes, ambos por IDENTIDADE/ESTRUTURA, nunca por
substring de classe genérica:
- Pass 1 (checks conhecidos): localiza o container de copyright via
  `data-e2e="copyright_container"` e o de conteúdo via texto do headline;
  cada um SEMPRE aparece no resultado — se a identidade não for encontrada,
  entra como `CHECK_UNKNOWN` explícito (nunca omitido silenciosamente).
  Estado lido primariamente pelas 5 variantes estruturais
  (`_read_status_group_state`, preferindo `is_visible()` real sobre o
  valor literal de `data-show`), caindo para o fallback textual já
  existente (`_classify_check_item`) só se nenhuma variante resolver.
- Pass 2 (checks futuros/desconhecidos): varre toda a seção por
  `div[class*="status-result"]` (nome de classe real, estável, não
  confundível com "check" genérico) e sobe até o container de cada
  variante encontrada (2 níveis reais — folha → grupo de status →
  container do check; ver `_status_group_check_container`), excluindo
  containers já reivindicados por copyright/conteúdo (por contenção, não
  por identidade de objeto — Locators não são comparáveis via `is` em
  Playwright real). O que sobra vira item `kind=OTHER`, com o MESMO
  mecanismo estrutural de leitura de estado — nunca ignorado
  silenciosamente.

Por que `content-check__divider` (e qualquer outro elemento puramente
decorativo) parou de ser contado: ele nunca tem nenhuma das 5 variantes de
status como descendente, então nunca aparece na varredura do Pass 2 —
exclusão por definição estrutural, sem exceção hardcoded para o divisor
especificamente.

### Achado adversarial corrigido durante esta própria rodada

Na primeira versão da correção, o Pass 2 subia só 1 nível real a partir da
variante de status folha para achar o "container do check" usado na
exclusão por contenção. No DOM real, a variante é filha do GRUPO de status,
que por sua vez é filho do container do check (irmão da identidade) — ou
seja, são 2 níveis reais, não 1. Com 1 nível só, a exclusão por contenção
falhava (o grupo de status não contém a identidade como descendente,
mesmo sendo o container real que contém) e o Pass 2 reintroduzia
copyright/conteúdo DUPLICADOS como itens fantasmas `kind=OTHER` — a mesma
classe de bug desta rodada, agora auto-infligida pela própria descoberta
genérica. Corrigido subindo 2 níveis reais
(`_status_group_check_container`), com fallback gracioso para 1 nível
quando a estrutura for mais rasa (nunca falha, só perde granularidade de
texto/dedup nesse caso hipotético). Um teste adversarial dedicado
(`test_1b_estrutura_real_de_2_niveis_...` em
`tests/test_agendar_tiktok_check_item_detection.py`) modela essa
profundidade real explicitamente e prova que a duplicação não ocorre — os
outros testes deste arquivo simplificam para 1 nível nas fixtures (o que
mascarava esse bug) e por isso não substituem esse teste específico.

### Arquivos modificados

- `_sistema/agendar_tiktok.py` — reescritas apenas: `_find_checks_section`
  (só docstring, lógica de seletor intacta — já funcionava no Windows
  real), `_gather_check_items` (reescrita completa, ver acima),
  `_classify_check_item` (mantida sem alteração de lógica — agora chamada
  como FALLBACK, não mais como via primária), `_classify_check_kind`
  (mantida, papel reduzido a referência/compat — kind agora vem por
  identidade do caminho de descoberta). Funções NOVAS adicionadas, todas
  de suporte estrito ao escopo acima: `_read_status_group_state`,
  `_classify_check_container`, `_find_copyright_check_container`,
  `_find_content_check_container`, `_container_is_copyright_check`,
  `_container_is_content_check`, `_status_group_check_container`,
  `_find_other_check_containers`, e as constantes
  `STATUS_RESULT_SELECTOR`, `COPYRIGHT_CONTAINER_SELECTOR`,
  `CONTENT_HEADLINE_SELECTOR`, `CONTENT_HEADLINE_TEXT_HINTS`.
- `tests/test_agendar_tiktok_preflight_checks.py` — fixtures reescritas
  (`_make_check_item`/`ITEM_SELECTOR`-based → `_make_copyright_check`/
  `_make_content_check`/`_build_section`, estrutura real de identidade +
  5 variantes), preservando a intenção de cada teste original (29
  testes, todos mantidos, nenhum removido/enfraquecido).
- `tests/test_agendar_tiktok_copyright_policy.py` — fixtures reescritas
  (`_copyright_item`/`_content_item`/`_other_item`/`_section_from_items`
  agora constroem a estrutura real via `_CheckDescriptor`, mantendo o
  fallback textual original de cada cenário como forma de exercitar
  `_classify_check_item` quando nenhuma variante estrutural resolve —
  42 testes, todos mantidos, nenhum removido/enfraquecido).
- `tests/test_agendar_tiktok_time_layers.py` — `_register_checks_passed`
  (helper usado só para NÃO travar em `wait_for_tiktok_checks` nos testes
  de data/hora, que não são o foco desta rodada) reescrito para a
  estrutura real; sem essa correção, esses testes ficavam presos em
  espera real (timeout do `wait_for_tiktok_checks`) porque o
  `_gather_check_items()` novo não reconhece mais o registro antigo — 28
  testes, todos mantidos.
- `tests/test_agendar_tiktok_check_item_detection.py` — **NOVO**, 19
  testes cobrindo os 7 blocos obrigatórios do Gate: regressão do bug real
  (2, incluindo o adversarial de profundidade real), mapeamento estrutural
  das 5 variantes (5, com subTest), combinações via `aggregate_check_status`
  não alterado (4), isolamento entre checks (2), identificação por texto
  incl. capitalização e "headline não encontrado" (2), elemento decorativo
  nunca vira item (1), check desconhecido/novo kind=OTHER (2).

### Confirmação do teste de regressão do bug real

`TikTokRealBugRegressionTests::test_1_copyright_ok_content_ok_divider_presente_resolve_passed_nunca_unknown`
reproduz o cenário exato relatado (copyright=success, conteúdo=success,
divisor decorativo presente) e confirma: exatamente 2 itens (nunca 4),
nenhum `kind=OTHER` fantasma, estado geral `CHECK_PASSED` (nunca
`CHECK_UNKNOWN`). `test_2_wait_for_tiktok_checks_prossegue_sem_bloquear_no_cenario_real`
confirma o mesmo fim a fim via `wait_for_tiktok_checks()` (não alterado).
Ambos **PASSARAM**.

### Testes automatizados executados (rodada atual)

- `tests/test_agendar_tiktok_check_item_detection.py`: 19 passed, 8
  subtests passed.
- `tests/test_agendar_tiktok_preflight_checks.py`: 29 passed (nenhum
  removido/enfraquecido em relação à rodada anterior).
- `tests/test_agendar_tiktok_copyright_policy.py`: 42 passed (nenhum
  removido/enfraquecido).
- `tests/test_agendar_tiktok_time_layers.py`: 28 passed (nenhum
  removido/enfraquecido; helper de fixture corrigido para não travar).
- **Suíte completa do projeto (todos os arquivos `tests/test_*.py`,
  executados individualmente por arquivo neste ambiente para evitar
  limites de recursos do sandbox de desenvolvimento — não é uma restrição
  do ambiente real do usuário):** **1102 passed, 0 failed** (1083 da
  rodada anterior + 19 novos deste arquivo; nenhum teste existente foi
  removido, enfraquecido ou alterado em sua asserção original).
- `python3 -m compileall -q .`: saída limpa, exit code 0.

### Confirmação explícita de escopo (fora do que foi listado acima, NADA foi alterado)

- **Data/hora:** `set_schedule_datetime`, `read_observed_schedule_datetime`
  e toda a lógica de comparação/arredondamento — intocadas. Único ponto de
  contato: o helper de teste `_register_checks_passed` (só para não travar
  testes de data/hora que dependiam indiretamente da seção de
  verificações), sem nenhuma mudança na lógica de produção de data/hora.
- **YouTube:** `_sistema/agendar_youtube.py` — intocado nesta rodada.
- **Lógica de política já validada:** `aggregate_check_status`,
  `decide_tiktok_checks_outcome`, `wait_for_tiktok_checks`,
  `TikTokChecksBlockedError`, `TIKTOK_COPYRIGHT_WARNING_POLICY_*`,
  `TIKTOK_BLOCKED_ITEM_POLICY_*`, `_copyright_warning_policy_from_cfg`,
  `_blocked_item_policy_from_cfg` — todos intocados; `_gather_check_items`
  continua devolvendo a MESMA forma de dado (`{"label", "kind", "status"}`)
  que essas funções já consumiam antes.
- Nenhuma migration nova, nenhum m004. Nenhum Prompt 20. Nenhuma Parte 3.

### Riscos conhecidos / dívida técnica

1. **`is_visible()` vs. `data-show` literal:** a leitura estrutural
   confia primariamente em `is_visible()` real de cada variante,
   usando `data-show="true"` só como sinal secundário quando nenhuma
   variante estiver visivelmente ativa. Decisão explícita do usuário
   (não presumir que o atributo literal reflete o estado real em todo
   momento). Se o TikTok um dia atrasar a atualização visual em relação
   ao atributo (ou vice-versa) de um jeito não previsto, o sinal
   secundário pode divergir do que um humano veria na tela naquele
   instante exato — mitigado pelo polling determinístico já existente em
   `wait_for_tiktok_checks` (não alterado), que relê o estado
   periodicamente até resolver ou expirar.
2. **Deduplicação do Pass 2 por texto:** dois checks `OTHER` distintos
   com texto renderizado vazio/idêntico poderiam, em tese, ser
   sub-contados (Playwright não permite comparar identidade real de nó
   de DOM entre Locators construídos separadamente de forma simples).
   Não é um risco prático hoje (só existem os 2 checks conhecidos, sem
   nenhum "outro" real ainda) — documentado para quando o TikTok
   eventualmente adicionar um 3º check.
3. **Profundidade estrutural assumida para checks futuros:** o Pass 2
   assume que um 3º check seguirá a mesma profundidade (folha → grupo de
   status → container) dos dois conhecidos hoje; se o TikTok usar uma
   profundidade diferente, o item nunca desaparece (ainda é encontrado
   via `STATUS_RESULT_SELECTOR`), mas o texto do rótulo/dedup pode ficar
   menos preciso nesse cenário hipotético — ver docstring de
   `_status_group_check_container`.
4. **`_classify_check_kind`/`_classify_check_item` mantidos por
   compatibilidade:** não são mais o caminho primário (kind vem da
   identidade do Pass 1/Pass 2; status estrutural vem de
   `_read_status_group_state`), mas continuam existindo e sendo usados
   como fallback — nenhum teste os referenciava diretamente antes desta
   rodada (confirmado por grep), então essa mudança de papel não quebra
   nada externo.

### Validação manual esperada no Windows real (pendente de confirmação do usuário)

Repetir exatamente o vídeo que causou o bug relatado: a seção
"Verificações" deve mostrar só os 2 checks reais (sem nenhuma linha
`OTHER: DESCONHECIDO` fantasma) e o "Estado geral" deve resolver para
`PASSED`, avançando normalmente para o clique final de agendamento.

**Este Gate continua sem ser declarado concluído / sem ZIP gerado até essa
validação Windows real ser confirmada pelo usuário — igual às rodadas
anteriores.**

---

## 2026-09-20 (continuação 2) — GATE 19.5 — ESTÁGIO 2 — CORREÇÃO: escopo da
## seção "Verificações" nunca foi o card real (bug real Windows, 2ª ocorrência)

### O que aconteceu

A correção anterior (identidade estrutural para copyright/conteúdo +
descoberta por lista branca via grupo de 5 variantes `status-result`)
passou nos 1102 testes automatizados, mas travou de novo no Windows real
contra uma conta real, no MESMO sintoma final (`Estado geral: DESCONHECIDO`,
abort por "verificações não concluíram em 240s"), mesmo com as duas
verificações reais mostrando "Nenhum problema encontrado." na tela.

### Causa raiz (evidência: HTML real de diagnóstico, o mesmo que o programa
### já salva automaticamente no abort, parseado com parser HTML de verdade)

Os 3 seletores estruturais de `_find_checks_section()`
(`post_verifications`, `verification`, `check_list`) davam ZERO resultados
nesta versão real do TikTok Studio — sempre, não intermitente. O fallback
antigo (`get_by_text("Verificações") -> xpath=".."`) subia só 1 nível a
partir do título e resolvia para `div.jsx-1566941760.main` — o painel do
FORMULÁRIO INTEIRO de upload, que contém `cover_container`,
`caption_container`, `poi_container`, `schedule_container`,
`video_visibility_container`, `user_perm_container`,
`advanced_settings_container`, `aigc_container`,
`disclose_content_container` E `copyright_container` juntos. Com esse
escopo largo demais, `_find_other_check_containers()` (a descoberta
genérica "lista branca" da correção anterior, correta em si) encontrava
`div[class*="status-result"]` de TOGGLES não relacionados — por exemplo o
texto do toggle "Qualidade HD por padrão quando você publica a partir da
versão Web do Studio" — que viravam itens fantasmas `kind=OTHER
status=CHECK_UNKNOWN`, travando a agregação de novo. Mesmo sintoma final da
rodada anterior, mecanismo diferente: desta vez não foi um seletor de item
ganancioso, foi um ESCOPO ganancioso alimentando um mecanismo de descoberta
que, dentro de um escopo correto, já funciona bem.

Evidência adicional: pelo menos um `status-result status-success` também
foi encontrado fora do card real, associado a outro toggle — documentado
como risco (ver seção de riscos abaixo), já que a mesma lógica poderia, em
tese, mascarar ou bloquear indevidamente dependendo do texto/variante
desse elemento alheio, mesmo não sendo o caso observado nesta captura.

### Mecanismo exato usado para localizar o card real

A partir da MESMA identidade já confiável (`div[data-e2e="copyright_container"]`,
confirmada por HTML real), sobe pelos ancestrais REAIS até o primeiro
(mais próximo) `div` cuja classe contenha "card", via um único locator
XPath com o eixo `ancestor::`:

```
CHECKS_CARD_ANCESTOR_XPATH = 'xpath=ancestor::div[contains(@class, "card")][1]'
```

Critério de parada: o eixo `ancestor::` do XPath é um "eixo reverso" —
pela especificação XPath, a posição de proximidade desses eixos é
calculada em ordem reversa ao documento, ou seja, `[1]` significa o
ancestral MAIS PRÓXIMO do nó de contexto (`copyright_container`), não o
primeiro `div.card` do documento inteiro. Isso importa porque o HTML real
confirma que `div[class*="card"]` NÃO é único na página (4 ocorrências:
capa do vídeo, descrição, "Quando publicar" e o de verificações) — só 1
desses 4 contém `copyright_container` dentro dele, e é exatamente esse que
a busca por proximidade a partir da identidade encontra, nunca por posição
global.

Se `copyright_container` não existir na página (tela mudou/erro), a função
devolve `None` — o comportamento já existente de "seção não encontrada ->
lista vazia -> `CHECK_UNKNOWN` geral" foi preservado sem nenhum fallback
adicional além do estritamente necessário.

**O fallback de texto antigo foi REMOVIDO, não só "melhorado".** Está
provado (não suposto) que ele resolve para o formulário inteiro nesta
versão real do TikTok Studio, e não existe hoje uma forma comprovadamente
segura de mantê-lo dentro do escopo desta correção — por isso ele
simplesmente não existe mais em `_find_checks_section()`. Os 3 seletores
estruturais antigos foram mantidos tentados primeiro (não fazem mal, dão 0
hoje comprovadamente, mas um build futuro do TikTok pode voltar a
expô-los).

### Confirmação: escopo resolvido não contém nenhum data-e2e do formulário

`tests/test_agendar_tiktok_checks_card_scope.py::TikTokChecksCardScopeRegressionTests::test_6_escopo_resolvido_nao_contem_nenhum_data_e2e_de_outras_secoes_do_formulario`
prova, sobre o objeto REAL devolvido por `_find_checks_section()` (não
sobre o resultado final "deu certo por acaso"), que nenhum dos 9
`data-e2e` do formulário (`cover_container`, `caption_container`,
`poi_container`, `schedule_container`, `video_visibility_container`,
`user_perm_container`, `advanced_settings_container`, `aigc_container`,
`disclose_content_container`) é encontrável dentro do escopo. **PASSOU.**
`test_1` confirma que o objeto devolvido É o card (`assertIs`) e NÃO é o
formulário largo (`assertIsNot`). `test_3` confirma exatamente 2 itens
(nunca 3+) e que nenhum texto de toggle do formulário (`"Qualidade HD"`,
`"Quem pode ver esta publicação"`, `"Carregamentos de alta qualidade"`)
vaza para dentro de um item de verificação.

### Confirmação: `_read_status_group_state()` já lida com múltiplas
### ocorrências da mesma classe de variante coexistindo

Verificado explicitamente (seção 2 do prompt desta rodada): o card de
conteúdo pode ter mais de um elemento `status-ready` presente
simultaneamente no DOM (ex.: textos como "Você atingiu o limite de
verificações para hoje..." e "Este recurso não está disponível para contas
governamentais..."), mesmo que só um esteja de fato visível/ativo. A
função já lida com isso corretamente hoje, SEM PRECISAR DE MUDANÇA: ela
itera todas as ocorrências retornadas por `STATUS_RESULT_SELECTOR` (nunca
deduplica por nome de classe) e escolhe a que estiver realmente visível via
`is_visible()`, ignorando as demais instâncias inativas da mesma classe.
Prova, com evidência, em
`tests/test_agendar_tiktok_checks_card_scope.py::TikTokMultipleSameVariantClassCoexistingTests`
(3 testes: duas ocorrências inativas de `status-ready` não atrapalham a
ativa de outra classe; a ocorrência ativa de `status-ready` está entre
várias inativas da MESMA classe; nenhuma das ocorrências duplicadas visível
cai para o sinal secundário `data-show`). **Todos PASSARAM. Nenhuma
alteração de código foi necessária nesta função.**

### Arquivos modificados

- `_sistema/agendar_tiktok.py`:
  - `_find_checks_section()` reescrita: mantém os 3 seletores estruturais
    antigos tentados primeiro; REMOVE o fallback de texto
    (`get_by_text("Verificações") -> xpath=".."`); adiciona a nova
    estratégia primária por ancestralidade a partir de
    `copyright_container`. Nenhuma outra função tocada (confirmado abaixo).
  - Nova constante `CHECKS_CARD_ANCESTOR_XPATH`.
- `tests/fakes_playwright.py` — `FakeElement.locator()` ajustado (RETROCOMPATÍVEL):
  agora tenta um match exato em `_children` mesmo para seletores
  `"xpath=..."` ANTES de cair no fallback genérico de devolver
  `self.parent` -- necessário porque, nesta correção, o MESMO elemento de
  identidade (`copyright_container`) precisa responder a dois XPaths
  diferentes com destinos diferentes (um hop até o wrapper do check, vários
  hops até o card). Nenhum teste existente registra uma string `"xpath=..."`
  literal em `_children` hoje, então o comportamento de todo o resto da
  suíte (que depende do fallback genérico) fica intacto -- confirmado pela
  suíte completa continuando 100% verde.
- `tests/test_agendar_tiktok_checks_card_scope.py` — **NOVO**, 13 testes:
  regressão da 2ª ocorrência (7, incluindo os requisitos (a)/(b)/(c) da
  seção 3 do prompt), fail-closed preservado sem fallback novo (3),
  múltiplas ocorrências da mesma classe de variante coexistindo (3).

### Sobre o HTML real de diagnóstico (por que não foi usado literalmente)

O prompt desta rodada pede, preferencialmente, o uso do arquivo
`*_bloqueado_*.html` que o programa já salva automaticamente no abort
(`save_debug()`, em `_sistema/agendar_tiktok.py`, grava em `LOG_DIR =
ACCOUNT_PATHS.logs`). Esse arquivo NÃO estava disponível nesta sessão:
`LOG_DIR` fica na pasta de dados da CONTA (fora da pasta do código-fonte
conectada a este ambiente), e uma busca no HTML real não pôde ser feita —
a pasta conectada a este ambiente é só `C:\Users\Enzo\Desktop\teste`
(o código-fonte), e nenhuma busca por `*.html`/`*bloqueado*` encontrou o
arquivo ali. Por isso, seguindo a instrução explícita do prompt para esse
cenário, foi construída uma fixture SINTÉTICA reproduzindo FIELMENTE a
estrutura descrita na evidência textual da rodada: identidade
`copyright_container` no nível da página (não já escopada), os 3 seletores
estruturais antigos vazios, um card estreito contendo só os 2 checks reais,
e um "formulário largo" simulando `div.jsx-1566941760.main` com os 9
`data-e2e` reais do formulário inteiro E um elemento `status-result` órfão
com o texto real do toggle "Qualidade HD por padrão..." — a mesma estrutura
relatada, não uma simplificação que mascare o problema. Se o usuário puder
localizar e anexar esse arquivo `*_bloqueado_*.html` (verificar a pasta de
logs da conta, fora do código-fonte), um teste adicional com o HTML
literal pode ser adicionado em uma rodada futura para reforçar esta
cobertura sintética com o artefato real.

### Testes automatizados executados (rodada atual)

- `tests/test_agendar_tiktok_checks_card_scope.py`: **13 passed** (novo).
- `tests/test_agendar_tiktok_check_item_detection.py`: 19 passed, 8
  subtests passed (rodada anterior, sem alteração).
- `tests/test_agendar_tiktok_preflight_checks.py`: 29 passed (sem
  alteração nesta rodada).
- `tests/test_agendar_tiktok_copyright_policy.py`: 42 passed (sem
  alteração nesta rodada).
- `tests/test_agendar_tiktok_time_layers.py`: 28 passed (sem alteração
  nesta rodada).
- **Suíte completa do projeto (todos os `tests/test_*.py`, executados
  individualmente por arquivo neste ambiente para evitar limites de
  recursos do sandbox de desenvolvimento — não é uma restrição do
  ambiente real do usuário): 1115 passed, 0 failed** (1102 da rodada
  anterior + 13 novos deste arquivo; nenhum teste existente foi removido,
  enfraquecido ou teve sua asserção original alterada).
- `python3 -m compileall -q .`: saída limpa, exit code 0.

### Confirmação explícita de escopo (byte-idêntico fora do listado acima)

- **`aggregate_check_status`, `decide_tiktok_checks_outcome`,
  `wait_for_tiktok_checks`, `TikTokChecksBlockedError`, toda a lógica de
  política (`TIKTOK_COPYRIGHT_WARNING_POLICY_*`,
  `TIKTOK_BLOCKED_ITEM_POLICY_*`, `_copyright_warning_policy_from_cfg`,
  `_blocked_item_policy_from_cfg`) — não foram tocados nesta rodada.**
  Confirmado por leitura direta do trecho relevante do arquivo entregue
  (reproduzido no processo de revisão desta rodada) e pela suíte de
  política (`test_agendar_tiktok_copyright_policy.py`, 42 testes)
  continuar 100% verde sem nenhuma alteração de fixture.
- **`_gather_check_items`, `_classify_check_item`, `_classify_check_kind`,
  `_read_status_group_state`, `_classify_check_container`,
  `_find_copyright_check_container`, `_find_content_check_container`,
  `_container_is_copyright_check`, `_container_is_content_check`,
  `_status_group_check_container`, `_find_other_check_containers`** — da
  rodada anterior, também não foram tocados nesta rodada; só o PONTO DE
  ENTRADA (`_find_checks_section`) que alimenta essas funções com o
  `section` foi corrigido, exatamente como pedido no prompt ("ou o ponto
  de entrada equivalente usado por `_gather_check_items()`").
- **Data/hora** (`set_schedule_datetime`, `read_observed_schedule_datetime`
  e toda a lógica de comparação/arredondamento) — intocada, nenhum ponto
  de contato nesta rodada (nem indireto, ao contrário da rodada anterior).
- **YouTube** (`_sistema/agendar_youtube.py`) — intocado.
- Nenhuma migration nova, nenhum m004. Nenhum Prompt 20. Nenhuma Parte 3.

### Riscos conhecidos / dívida técnica

1. **Sem gate de visibilidade sobre a identidade/card:** os 3 seletores
   estruturais antigos checavam `is_visible()` do candidato antes de
   aceitá-lo; a nova busca por ancestralidade a partir de
   `copyright_container` NÃO faz esse gate (nem sobre a identidade, nem
   sobre o card resolvido) — confia na unicidade do atributo
   `data-e2e="copyright_container"` em vez de visibilidade. Se um dia
   existir mais de uma ocorrência desse atributo na página simultaneamente
   (ex.: um template oculto), `.first` pode escolher a errada. Não há
   evidência disso hoje (a `_find_copyright_check_container` já assume
   isso implicitamente desde a rodada anterior), mas fica documentado como
   risco a observar se um build futuro do TikTok duplicar esse elemento.
2. **Semântica do eixo XPath `ancestor::` validada por especificação, não
   por execução real:** o fake de testes (`FakeElement`) não interpreta
   XPath de verdade -- ele só devolve o que foi explicitamente registrado
   para aquele XPath exato. Os testes desta rodada provam que a PRODUÇÃO
   usa corretamente o resultado desse locator, não que o eixo `ancestor::`
   do Playwright/Chromium real se comporta como a especificação XPath diz
   (proximidade reversa, `[1]` = mais próximo). Isso só pode ser confirmado
   no teste manual Windows real desta rodada.
3. **HTML real de diagnóstico não usado literalmente** (ver seção acima) --
   a fixture sintética foi construída fielmente a partir da evidência
   textual fornecida, mas um teste com o artefato real (`*_bloqueado_*.html`)
   reforçaria ainda mais esta cobertura, se o usuário conseguir localizá-lo
   e anexá-lo em uma rodada futura.
4. **Risco documentado, não observado:** um `status-result status-success`
   fora do card real também foi confirmado no HTML de evidência, associado
   a outro toggle. Neste caso específico não causou bloqueio (a classificação
   deu PASSED, "por acaso" inofensivo), mas se o texto/variante desse
   elemento alheio fosse `status-warn`/`status-error`, teria bloqueado
   indevidamente um agendamento saudável ANTES desta correção -- com o
   escopo agora estreito ao card real, esse elemento nunca mais entra na
   varredura.
5. **`_find_other_check_containers`/`_status_group_check_container`
   (dívida técnica já declarada na rodada anterior, ainda válida):
   deduplicação por texto renderizado, e suposição de profundidade
   estrutural de 2 níveis para um 3º check futuro -- inalteradas, ver
   relatório da rodada anterior.

### Validação manual esperada no Windows real (pendente de confirmação do usuário)

Repetir exatamente o mesmo vídeo/conta que travou nesta 2ª ocorrência: a
seção "Verificações" deve mostrar só os 2 checks reais, sem nenhum item
fantasma originado de outro toggle do formulário, e o "Estado geral" deve
resolver para `PASSED`, avançando para o clique final. Se o programa ainda
travar com as duas verificações visivelmente ok na tela, é um bug a
reportar imediatamente, com o HTML de diagnóstico que o próprio programa
salva (`*_bloqueado_*.html`) anexado desta vez, para permitir um teste com
o artefato real.

**Este Gate continua sem ser declarado concluído / sem ZIP gerado até essa
validação Windows real ser confirmada pelo usuário — igual às rodadas
anteriores.**

---

## 2026-09-20 (continuação 3) — GATE 19.5 (Estágio 2) — pergunta única por execução/conta para a política de direitos autorais de terceiros (sem persistência em disco) — TIKTOK E YOUTUBE

### Contexto / decisão do usuário

Até esta rodada, `tiktok_copyright_warning_policy`/`tiktok_blocked_item_policy`
e `youtube_copyright_warning_policy`/`youtube_blocked_item_policy` só podiam
ser definidos editando `config_canal.json` à mão — inaceitável para um
produto comercial finalizado. O usuário decidiu, de forma explícita e não
negociável:

1. `main()` de **ambos** `agendar_tiktok.py` e `agendar_youtube.py` pergunta,
   em **toda** execução, sem exceção e sem nenhuma memória entre chamadas
   (mesmo dentro do mesmo processo/sessão/conta), se avisos de direitos
   autorais de terceiros devem ser ignorados **nesta execução**.
2. A resposta **nunca** é gravada em `CONFIG_FILE` — vale só para aquele
   lote daquela chamada. Isso revoga, apenas para o fluxo interativo, a
   exigência anterior de que essa política sobrevivesse a restart (o
   mecanismo antigo baseado em arquivo continua existindo e funcionando
   normalmente para uso programático/não-interativo).
3. Independentemente da resposta sobre direitos autorais, `*_blocked_item_
   policy` é sempre forçada para `SKIP_AND_CONTINUE` neste fluxo interativo
   — cobrindo tanto vídeos bloqueados por aviso de direitos autorais quanto
   problemas **reais** de verificação (rede, país restrito, timeout não
   conclusivo etc.). Um problema legítimo agora só pula aquele vídeo, nunca
   para o lote inteiro.
4. Exceções **não** relacionadas a verificação (erro de automação, DOM não
   encontrado, divergência de data/hora) continuam parando o lote
   exatamente como antes — inalterado.
5. A rodada só é aceita com cobertura equivalente e completa para TikTok
   **e** YouTube.

Nada de lógica de data/hora, `_find_checks_section`, `_gather_check_items`,
`_read_status_group_state`, `aggregate_check_status`,
`decide_tiktok_checks_outcome`, `wait_for_tiktok_checks` ou qualquer lógica
interna de leitura/agregação de verificação foi tocado nesta rodada.

### Arquivos modificados

- `_sistema/agendar_tiktok.py` (93.347 bytes,
  sha256 `777c81511d7ca334dedafcc8276425af717bb2214816dfdab2133505714a687e`)
- `_sistema/agendar_youtube.py` (86.172 bytes,
  sha256 `9d7385f92ff0bd38f2360263125379eb3a854a67a800198d09c0d6787e2556e7`)
- `tests/fakes_playwright.py` (11.123 bytes,
  sha256 `71ab55edb5e1f393f00cfa229134d04c68c1bc378a64fe2e96377c7e3890cacd`)

### Arquivos criados

- `tests/test_agendar_tiktok_interactive_copyright_policy.py` (13.400 bytes,
  sha256 `b9595c2b51ac8f0e3d1db1df5a86bd095933b97dcb2e92208b336f96a0672fb2`) — 20 testes.
- `tests/test_agendar_youtube_interactive_copyright_policy.py` (22.562 bytes,
  sha256 `a360f40d00a3599240fa4d1cb4159d379e785171b478cd5374170a886aab7553`) — 27 testes.

(Hashes acima são do estado local antes da entrega para o Windows real;
a seção "Entrega/verificação Windows" abaixo, quando executada, repete o
hash pós-`device_commit_files`/`device_stage_files` para prova byte a byte.)

### Comportamento novo

**TikTok (`_sistema/agendar_tiktok.py`)**

- `_ask_interactive_copyright_warning_policy(input_fn=input)`: pergunta
  "Ignorar avisos de direitos autorais NESTA execução? (não fica salvo,
  s/N): ". Aceita `s`/`sim`/`y`/`yes` (case-insensitive, com espaços) como
  `ALLOW`; qualquer outra coisa — vazio, lixo, ou uma exceção ao ler a
  entrada (ex.: stdin fechado) — cai em `BLOCK` (fail-closed). Sem estado
  de módulo, sem cache: cada chamada é independente.
- `_cfg_for_interactive_run(cfg, copyright_policy)`: devolve uma **cópia
  rasa** de `cfg` com `tiktok_copyright_warning_policy` = a resposta desta
  execução e `tiktok_blocked_item_policy` sempre forçada para
  `SKIP_AND_CONTINUE`. O `cfg` original nunca é mutado.
- `main(input_fn=input)`: assinatura ganhou `input_fn` injetável (testes
  nunca chamam o `input()` real). A pergunta acontece logo após
  `load_json(CONFIG_FILE, {})`, antes de qualquer uso de `cfg` relacionado
  a copyright. A cópia rasa (`cfg_for_batch`) só é construída depois que
  `ensure_timezone_config(cfg)` já resolveu `cfg["timezone_iana"]` — para
  não carregar um timezone desatualizado/ausente — e só ela (nunca o `cfg`
  original) é passada para `process_prepared_batch(page, cfg_for_batch,
  prepared, state)`. `cfg` original continua sendo o único dict jamais
  passado para `save_json()`.
- `process_prepared_batch()` (já existia antes desta rodada, lógica
  interna **inalterada**): só o `print` de política foi reformulado para
  "Política TikTok (valores usados nesta execução do lote):", deixando
  claro que reflete o `cfg` efetivamente recebido — seja a cópia rasa do
  fluxo interativo, seja um `cfg` carregado direto do disco por uma
  chamada programática.

**YouTube (`_sistema/agendar_youtube.py`)**

Diferença arquitetural real identificada e tratada nesta rodada: ao
contrário do TikTok, o YouTube **não tinha** uma função
`process_prepared_batch()` separada — o laço do lote (upload, verificação,
quarentena, reinício periódico do Chrome) estava escrito inline dentro de
`main()`. Isso impediria cumprir o requisito do usuário de "documentar
explicitamente se o mecanismo programático com STOP_BATCH continua
funcionando fora do menu interativo", porque não existia uma função
própria para chamar programaticamente. Por isso, nesta rodada:

- `_ask_interactive_copyright_warning_policy(input_fn=input)` e
  `_cfg_for_interactive_run(cfg, copyright_policy)` foram adicionadas com
  o mesmo texto/mesma lógica fail-closed do TikTok, usando as constantes
  próprias do módulo (`YOUTUBE_COPYRIGHT_WARNING_POLICY_ALLOW/BLOCK`,
  `YOUTUBE_BLOCKED_ITEM_POLICY_SKIP_AND_CONTINUE`).
- O laço de lote foi **extraído** de `main()` para uma função própria,
  `process_prepared_batch(cfg, pending, slots, titles, descriptions,
  state)` — cópia literal do código existente (abre/fecha o próprio Chrome
  via Playwright, cuida do reinício periódico `reiniciar_chrome_cada`,
  aplica `youtube_blocked_item_policy` via `_youtube_batch_action_for_
  blocked()` exatamente como antes). **Nenhuma lógica de decisão foi
  alterada** — é code motion puro, confirmado pela suíte de testes nova
  (seção D, abaixo) chamando esta função diretamente com
  `youtube_blocked_item_policy: "STOP_BATCH"` e obtendo o mesmo
  comportamento de antes desta rodada.
- `main(input_fn=input)`: mesma estrutura do TikTok — pergunta logo após
  `load_json(CONFIG_FILE, {})`, cópia rasa (`cfg_for_batch`) construída
  depois de `ensure_timezone_config(cfg)`, usada para todo o resto do fluxo
  (itens, títulos, prints de política, slots) e passada para
  `process_prepared_batch(cfg_for_batch, pending, slots, titles,
  descriptions, state)`. `cfg` original só é passado para `save_json()`
  no caso (inalterado) de `timezone_changed`.
- **Cobertura de FAILED/UNKNOWN reais, não só aviso de direitos autorais:**
  o YouTube já unificava as três causas de bloqueio (`failed` — restrição/
  país; `copyright_warning_block` — aviso de terceiros com política BLOCK;
  `unknown` — verificação não conclusiva) num único
  `YouTubeChecksBlockedError`, tratado por um único `except` que consulta
  `youtube_blocked_item_policy`. Como o fluxo interativo força essa
  política para `SKIP_AND_CONTINUE`, as três causas já são puladas (nunca
  só o aviso de copyright) sem precisar de nenhuma mudança adicional de
  código — confirmado pelos testes 21/22/24 da seção D.

### Persistência — mecanismo antigo confirmado intacto

`_youtube_copyright_warning_policy_from_cfg()`/`_youtube_blocked_item_
policy_from_cfg()` e seus equivalentes no TikTok continuam lendo
diretamente de `cfg` sem saber nada sobre a pergunta interativa. Chamar
`process_prepared_batch()` diretamente com um `cfg` carregado do disco
contendo `"youtube_blocked_item_policy": "STOP_BATCH"` (ou o equivalente
`tiktok_blocked_item_policy`) produz exatamente o comportamento de antes
desta rodada — provado pelos testes 21/23/27 (YouTube) e pelos testes
20/21/25 já existentes, inalterados, em
`test_agendar_tiktok_copyright_policy.py` (TikTok).

### Testes automatizados

**Novos — TikTok** (`tests/test_agendar_tiktok_interactive_copyright_policy.py`, 20 testes):
- A. `AskInteractiveCopyrightPolicyTests` (7) — sim/variações/não/vazio/lixo/exceção/ausência de memória entre chamadas.
- B. `CfgForInteractiveRunTests` (5) — força SKIP_AND_CONTINUE com ALLOW e com BLOCK; não muta o cfg original; é cópia independente; preserva demais chaves.
- C. `MainInteractiveEndToEndTests` (8) — `main()` de ponta a ponta com conta TikTok isolada em disco (tmp dir real, vídeo real, texto real gerado), `process_prepared_batch` mockeado (captura o cfg recebido) e Playwright substituído por um fake (`FakeSyncPlaywrightCM`/`install_fake_playwright`, novos em `fakes_playwright.py`):
  - "sim" → ALLOW no cfg do lote, `CONFIG_FILE` byte-idêntico antes/depois.
  - "não"/vazio/lixo → BLOCK no cfg do lote, `CONFIG_FILE` byte-idêntico antes/depois (3 testes).
  - `CONFIG_FILE` recarregado do disco após a execução continua com os valores ORIGINAIS (`BLOCK`/`STOP_BATCH`), nunca os da execução interativa.
  - duas chamadas sucessivas de `main()`, mesma conta, respostas diferentes, nas DUAS ordens (sim→não e não→sim): cada chamada usa só a própria resposta, sem vazamento.
  - `CONFIG_FILE` continua byte-idêntico após as duas chamadas.

**Novos — YouTube** (`tests/test_agendar_youtube_interactive_copyright_policy.py`, 27 testes):
- A/B/C — mesma cobertura acima, adaptada aos nomes/constantes do YouTube (12 + 8 testes).
- D. `ProcessPreparedBatchTests` (7) — chama `process_prepared_batch()` diretamente (função nova, extraída nesta rodada), com Playwright fake:
  - FAILED real + SKIP_AND_CONTINUE → pula o vídeo (nunca aparece em `state["scheduled"]`), move para quarentena (`bloqueados/direitos_autorais/`), lote continua e agenda o próximo.
  - UNKNOWN real + SKIP_AND_CONTINUE → mesmo resultado.
  - FAILED real + STOP_BATCH → para o lote, vídeo não é movido nem apagado, nada agendado.
  - aviso de direitos autorais (`copyright_warning_block`) + SKIP_AND_CONTINUE → também pula (comportamento já existente antes desta rodada).
  - erro genérico NÃO relacionado a verificação → sempre para o lote, mesmo com SKIP_AND_CONTINUE, vídeo intocado (prova que a exceção genérica nunca é interpretada como bloqueio pulável).
  - próximo vídeo processado normalmente depois de um pulo.
  - chamada direta/programática com `STOP_BATCH` vindo de um cfg "carregado do disco" (sem main(), sem pergunta interativa) reproduz o comportamento antigo.

**Infra nova reutilizável** (`tests/fakes_playwright.py`):
- `FakePlaywrightContext`/`FakePlaywrightChromium`/`FakeSyncPlaywrightHandle`/`FakeSyncPlaywrightCM` — substituem `sync_playwright()`/`BrowserContext` reais para testes de ponta a ponta de `main()`/`process_prepared_batch()` sem abrir Chrome.
- `install_fake_playwright(sync_playwright_callable)` — injeta os módulos fake em `sys.modules["playwright"]`/`sys.modules["playwright.sync_api"]` via `mock.patch.dict`, necessário porque `main()`/`process_prepared_batch()` fazem `from playwright.sync_api import sync_playwright` **localmente** (dentro da função); funciona independentemente de o pacote `playwright` estar instalado ou não no ambiente de teste.

**Nenhum teste existente foi removido ou enfraquecido.** Os testes 20-25 de
`TikTokBatchPolicyTests` em `test_agendar_tiktok_copyright_policy.py`
(FAILED/UNKNOWN skip-and-continue, STOP_BATCH, erro genérico sempre para)
continuam passando sem nenhuma alteração de código — são a prova
independente, já existente antes desta rodada, de que
`process_prepared_batch()` do TikTok não foi tocado na lógica.

### Resultado da suíte completa

```
$ for f in tests/test_*.py; do timeout 90 /root/.local/bin/pytest "$f" -q; done
```

35 arquivos de teste, **1162 testes passando** (1115 antes desta rodada +
47 novos: 20 TikTok + 27 YouTube), 0 falhas.

```
$ python3 -m compileall -q _sistema tests
```
Sem saída (compilação limpa).

### Decisões arquiteturais

1. **Extração de `process_prepared_batch()` para o YouTube não é um
   refactor amplo fora do escopo** — é a única forma de cumprir literalmente
   o requisito do usuário ("documentar explicitamente se o mecanismo
   programático com STOP_BATCH continua funcionando fora do menu
   interativo") quando essa função simplesmente não existia antes. É
   *code motion* puro: o corpo do laço foi movido sem alteração de nenhuma
   condição, exceto o texto de um `print`. A suíte nova prova
   comportamento idêntico ao de antes (STOP_BATCH, SKIP_AND_CONTINUE,
   reinício de Chrome, quarentena) chamando a função isoladamente.
2. **A cópia rasa (`cfg_for_batch`) é construída DEPOIS de
   `ensure_timezone_config(cfg)`** em ambas as plataformas — decisão
   deliberada para que o timezone já resolvido em `cfg["timezone_iana"]`
   apareça na cópia; construir antes arriscaria um `cfg_for_batch` com
   timezone ausente/desatualizado sendo usado pelo `build_slots()`.
3. **`cfg` original nunca é passado para `save_json()` com as chaves de
   política interativa** — ele só é salvo (inalterado, comportamento já
   existente) quando `timezone_changed` é verdadeiro, e nesse caso salva
   exatamente o mesmo `cfg` que só tem `timezone_iana` adicionado, nunca as
   chaves de copyright/blocked_item.
4. **O mecanismo antigo baseado em `CONFIG_FILE` não foi removido nem
   descontinuado** — `_youtube_copyright_warning_policy_from_cfg()` /
   `_youtube_blocked_item_policy_from_cfg()` (e os equivalentes do TikTok)
   continuam existindo, sendo lidos por `process_prepared_batch()`
   normalmente, e funcionam para qualquer chamador programático que não
   passe por `main()`. Ele simplesmente deixa de ser a ÚNICA forma de o
   usuário final mudar a política — o menu interativo passa a valer só
   para aquele lote.
5. **Decoplamento de "onde a resposta vem"**: `input_fn` é injetável
   deliberadamente para que, quando a UI real (não-terminal) chegar (ver
   `claude/CONTEXTO_AUDITORIA_PERMANENTE.md`, FASE 12), a mesma função
   `_ask_interactive_copyright_warning_policy()`/`main()` possa receber um
   `input_fn` que consulte um diálogo gráfico em vez do terminal, sem
   reescrever a lógica de política/não-persistência.

### Riscos conhecidos / dívida técnica

1. **Pergunta síncrona bloqueante no terminal** — como antes desta rodada
   não havia pergunta nenhuma, este é um comportamento genuinamente novo:
   `main()` agora bloqueia esperando uma resposta de teclado antes de abrir
   o Chrome. Aceito porque é exatamente o que o usuário pediu; quando a UI
   gráfica chegar, o `input_fn` injetável absorve a mudança sem tocar na
   regra de negócio.
2. **`process_prepared_batch()` do YouTube agora abre e fecha o próprio
   Chrome** (só ele decide reinício periódico) — arquitetura ligeiramente
   diferente da do TikTok (que recebe uma `page` já aberta de `main()`).
   Isso reflete uma diferença REAL já existente antes desta rodada (o
   YouTube sempre reiniciou o Chrome no meio do lote; o TikTok não tinha
   essa necessidade) — não foi introduzida artificialmente para uniformizar
   as duas plataformas.
3. **Teste manual Windows real pendente**, como em todas as rodadas
   anteriores: os testes automatizados provam a lógica de política e
   não-persistência com Playwright fake; a pergunta aparecendo de fato no
   terminal, de forma legível, antes do Chrome abrir, em ambas as
   plataformas, só pode ser confirmada rodando de verdade no Windows.

### Como testar manualmente

Rodar `agendar_tiktok.py` (ou `agendar_youtube.py`) pelo painel duas vezes
seguidas na mesma conta: a primeira respondendo "s" e a segunda "n" (ou
Enter/vazio) à pergunta "Ignorar avisos de direitos autorais NESTA
execução?". Confirmar que:
- a pergunta aparece nas DUAS execuções (nunca só na primeira);
- `config_canal.json` da conta não ganha nenhuma chave nova de política de
  direitos autorais em nenhuma das duas execuções (comparar o arquivo
  antes/depois);
- um vídeo que dispare um problema real de verificação (ex.: aviso de
  Content ID, ou uma falha transitória de rede durante a checagem) é
  pulado e o restante da fila continua, em vez de parar o programa.

**Este Gate continua sem ser declarado concluído / sem ZIP gerado até a
validação Windows real desta rodada ser confirmada pelo usuário — igual às
rodadas anteriores.**

================================================================
## RODADA 21/09/2026 -- GATE 19.5 CORREÇÃO CRÍTICA: timeout de 45s na
## verificação de direitos autorais do YouTube + falha estrutural ao
## descartar rascunho bloqueado (evidência: conta NextIdea, 006.mp4)
================================================================

### ATENÇÃO -- discrepância que precisa ser confirmada pelo usuário

Os dois arquivos HTML citados como anexos deste prompt
(`20260921_022119_bloqueado_direitos_autorais.html` e
`20260921_022121_falha_descartar_direitos_autorais.html`) **não estavam
presentes nesta sessão** -- busca exaustiva no filesystem (`/`,
`/mnt/user-data/uploads/`) não encontrou nenhum dos dois. Esta rodada foi
executada usando o texto literal que o próprio prompt já citava
verbatim (a frase exata "Faltam 10 minutos" para a Seção 1, e as
conclusões exatas de grep -- "0 ocorrências de Eliminar/Apagar/Descartar,
só substring de classe CSS" -- para a Seção 2). Se o usuário quiser que as
opções (a)/(b) da Seção 2 sejam investigadas de verdade (e não só a opção
(c), aceita por segurança), os arquivos reais (ou uma nova captura da tela
de listagem de conteúdo do Studio) precisam ser enviados numa rodada
futura.

### Seção 0 -- pular a verificação inteira quando a política é ALLOW
(YouTube e TikTok)

**YouTube** (`agendar_youtube.py`, `schedule_one()`): quando
`copyright_warning_policy == YOUTUBE_COPYRIGHT_WARNING_POLICY_ALLOW`, o
código não chama mais `_read_youtube_checks_status()` -- vai direto para
`click_next(page)`, imprimindo "Verificação de direitos autorais: PULADA
(política desta execução = ignorar avisos)." Com BLOCK, o comportamento de
espera/leitura permanece idêntico ao de antes.

**TikTok** (`agendar_tiktok.py`, `schedule_one()`): quando
`copyright_warning_policy == TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW`, os
DOIS pontos que chamam `wait_for_tiktok_checks()` (a tentativa original e
o retry pós-modal "Continuar publicando?") são pulados, com o mesmo estilo
de log. Confirmado por diff que esta foi a ÚNICA mudança em
`agendar_tiktok.py` nesta rodada (dois hunches, nada mais, nos 94.685
bytes do arquivo).

Evidência de teste (zero chamadas com ALLOW, chamada normal com BLOCK, via
spy que ainda executa a função real quando ela É chamada):
- `tests/test_agendar_youtube_copyright_policy.py::YoutubeScheduleOneSecurityTests`
  (test_8 a test_13, incluindo o caso FAILED sob ALLOW não bloqueando mais
  nada -- mudança de comportamento intencional desta rodada, documentada
  no teste renomeado `test_5_allow_agora_pula_a_verificacao_inteira_
  mesmo_com_texto_de_failed`).
- `tests/test_agendar_tiktok_skip_checks_on_allow.py` (novo arquivo, 4
  testes: zero chamadas com ALLOW; chamada normal com BLOCK; default sem a
  chave configurada continua sendo BLOCK/espera; as DUAS chamadas --
  original + retry pós-modal -- puladas juntas com ALLOW).

### Seção 1 -- timeout de 45s -> 660s + log de progresso (YouTube)

`YOUTUBE_COPYRIGHT_CHECK_TIMEOUT_PADRAO_SEGUNDOS = 660` (novo default;
antes 45, hardcoded há várias rodadas). Justificativa: a própria tela do
YouTube, capturada no momento do abort real (evidência citada pelo
usuário), avisa "Faltam 10 minutos" -- ou seja, até 600s é comportamento
NORMAL, não uma trava. 660s = 600s (teto anunciado) + 60s de margem.
Optei por manter um único `max_wait` maior e configurável em vez de
reestruturar em dois patamares (CHECK_PENDING vs. indeterminado) porque um
teto único, grande o bastante, já cobre o caso real observado sem
aumentar a complexidade da máquina de estados -- a classificação
("a verificar"/"faltam" -> CHECK_PENDING) já é tratada corretamente e não
foi tocada.

Log de progresso: enquanto o estado observado continua sendo
CHECK_PENDING, o terminal agora imprime "Ainda verificando direitos
autorais... (Xs decorridos)" a cada
`YOUTUBE_COPYRIGHT_CHECK_PROGRESSO_INTERVALO_SEGUNDOS = 30` segundos, para
não parecer travado numa espera de até 11 minutos.

Configurável pelo menu: **nesta investigação descobri que
`painel_oficial.py` não tinha NENHUMA tela de edição de configuração de
conta já criada -- todo campo, sem exceção, só podia ser mudado editando o
JSON na mão.** Isso contraria a premissa do prompt ("mesma tela/fluxo onde
outras opções... já são editadas" -- essa tela não existia para nenhum
campo). Em vez de construir um editor de configurações genérico (fora do
escopo desta correção, risco de overengineering), adicionei uma tela
mínima e focada: opção **8 - VERIFICAÇÃO DE DIREITOS AUTORAIS (YOUTUBE)**
no menu principal, função `configurar_verificacao_direitos_autorais_
youtube()` -- escolhe a conta, mostra o valor atual, pede um novo valor em
segundos (ENTER mantém, valor fora de 30-3600s é rejeitado), grava de
volta no `config_canal.json` da conta.

Evidência de teste (novo arquivo `tests/test_painel_oficial_youtube_
copyright_timeout_menu.py`, 7 testes, rodando o FLUXO REAL do menu --
`input()`/`print()` mockeados, não a função isolada de leitura/gravação):
fluxo do menu grava o novo valor no disco; ENTER vazio não altera nada;
valor não-numérico é rejeitado sem gravar; valor fora da faixa é
rejeitado; as demais chaves da conta são preservadas; a opção "8" do menu
principal chama esta tela; o default de conta nova já não é 45s.

Teste de regressão com o texto real de evidência ("A verificar se o seu
vídeo inclui conteúdo com direitos de autor... Faltam 10 minutos"): este
texto sozinho, sem nunca mudar, continua sendo tratado como CHECK_PENDING
durante toda a espera (nunca virando prematuramente PASSED nem UNKNOWN),
só expirando para UNKNOWN depois do novo prazo configurado -- confirmado
em `YoutubeChecksPollingTests` (test_7 a test_12).

`_classify_youtube_copyright_body`, `decide_youtube_checks_outcome`,
`YOUTUBE_CHECK_*_TERMS` e as duas funções de política -- confirmados
byte-idênticos (só a assinatura/default de `_read_youtube_checks_status`
e o corpo do ramo PENDING mudaram, para acrescentar o print periódico).

### Seção 2 -- `discard_current_upload()` nunca funciona nesta tela real

Evidência citada pelo usuário (grep literal no DOM real capturado no
momento exato da chamada): zero ocorrências de "Eliminar"/"Apagar"/
"Descartar"; as únicas ocorrências de "Delete"/"Discard" no HTML inteiro
são substring de nomes de classe CSS internos do YouTube (ex.:
`ytVideoFilesDialogDiscardButtonContainer`), nunca um botão real visível
com esse aria-label/texto. Ou seja: nesta etapa do assistente
("Verificações"), o botão de descartar simplesmente não existe -- a função
está condenada a sempre falhar aqui, não é um seletor ocasionalmente
desatualizado.

Como os dois arquivos HTML reais não estavam disponíveis nesta sessão para
investigar as opções (a) modal precisa ser fechado primeiro / draft
aparece na lista de conteúdo com um menu "⋮" por vídeo, ou (b) o ícone de
lixeira só existe em outra etapa do modal, escolhi a opção (c) que o
próprio prompt já autorizava como aceitável quando (a)/(b) não puderem ser
confirmadas com evidência: aceitar que o rascunho fica salvo como privado
no canal e avisar o usuário explicitamente, em vez de tentar (e falhar)
silenciosamente.

Comportamento novo: o retorno de `discard_current_upload()` agora É
verificado no ponto de chamada em `schedule_one()`. Quando `False`:
imprime `[AVISO] O rascunho deste vídeo ficou salvo como privado no canal
e não foi apagado automaticamente -- revise manualmente no YouTube
Studio.` no terminal, e o mesmo texto é embutido na mensagem da
`YouTubeChecksBlockedError` levantada em seguida -- o que faz esse aviso
fluir naturalmente para o log já existente `direitos_autorais_
bloqueados.txt` (reaproveitando o caminho de gravação de log já existente
em `process_prepared_batch()`, sem duplicar código de logging). Quando
`True` (draft removido com sucesso), nenhum aviso é impresso/logado.

Evidência de teste: `test_8`-`test_13` de
`YoutubeScheduleOneSecurityTests` (falha de discard produz `[AVISO]` +
texto embutido na exceção; sucesso de discard não produz aviso) e
`ProcessPreparedBatchTests::test_28_aviso_de_discard_falho_fica_
registrado_no_log_de_bloqueados` (confirma que o texto do aviso aparece
literalmente em `direitos_autorais_bloqueados.txt` depois de
`process_prepared_batch()` rodar).

`_find_checks_section()`, `_gather_check_items()`, escopo do card,
internals de `wait_for_tiktok_checks()`, `aggregate_check_status`,
`decide_tiktok_checks_outcome()` -- nenhum tocado.

### Testes e compilação desta rodada

- Suíte completa (pytest, por arquivo): **1186 testes, 0 falhas** (1179
  antes desta rodada + 7 novos em
  `test_painel_oficial_youtube_copyright_timeout_menu.py`; os +12 do
  YouTube e +4 do TikTok Seção 0 e +1 de integração já estavam contados no
  total anterior de 1179 -- a única mudança de contagem NESTA execução
  final foi o arquivo novo do menu do painel).
- `python3 -m compileall _sistema tests`: **sucesso, sem erros**.

### Arquivos modificados/criados

Modificados: `_sistema/agendar_youtube.py`, `_sistema/agendar_tiktok.py`
(só Seção 0, diff-confirmado), `_sistema/painel_oficial.py`,
`tests/test_agendar_youtube_copyright_policy.py`,
`tests/test_agendar_youtube_interactive_copyright_policy.py`.

Criados: `tests/test_agendar_tiktok_skip_checks_on_allow.py`,
`tests/test_painel_oficial_youtube_copyright_timeout_menu.py`.

### Entrega Windows

Todos os 7 arquivos entregues em
`C:\Users\Enzo\Desktop\teste\` e confirmados **byte-a-byte** (cmp + SHA-256
idênticos entre o que foi escrito localmente e o que foi lido de volta do
Windows depois do commit):

- `_sistema/agendar_youtube.py` -- 91.980 bytes --
  `1e25b77e1ecb5bbb06c5d8ea1f62efb948ab98dcfa5f10a9b10bd2c283c2ce14`
- `_sistema/agendar_tiktok.py` -- 94.685 bytes --
  `4c6dbd8f0562814a9e985e087856a951f8c46fcb0e4cbf0a40a7cefb65905c88`
- `_sistema/painel_oficial.py` -- 27.761 bytes --
  `6dfad331b64d057adc635fa476e0b7928030652ca1acdb88c2c0c516a9370beb`
- `tests/test_agendar_youtube_copyright_policy.py` -- 31.582 bytes --
  `8ad080a52ef14d3fb9fa7ce48aa3ffe489bddf98df251eff9c8e82f5081fe524`
- `tests/test_agendar_youtube_interactive_copyright_policy.py` -- 23.904
  bytes --
  `22a392da4a28a511f3917d831c84bf59075f1a170d5ef35b8d074a8191b19fe8`
- `tests/test_agendar_tiktok_skip_checks_on_allow.py` -- 7.817 bytes --
  `12058cc43329e32ebb7804e1f7793194b1a042791b326c4de560256c699eb980`
- `tests/test_painel_oficial_youtube_copyright_timeout_menu.py` -- 4.723
  bytes --
  `375c9290fad25bffb8471c36081f2e6a0b377511634865afea01b6e7f258ca60`

### Riscos conhecidos e dívida técnica

1. Com ALLOW, o programa não detecta MAIS NENHUM bloqueio real antes de
   agendar (ex.: CHECK_FAILED por restrição de país no YouTube; item
   "content" com problema real não-copyright no TikTok) -- risco aceito
   explicitamente pelo usuário nesta rodada, documentado no código.
2. A opção (c) para o rascunho órfão do YouTube (aviso + log, sem apagar)
   é a solução aceita nesta rodada por falta dos HTML reais -- ainda pode
   existir um mecanismo real de apagar que as opções (a)/(b) descobririam
   com os arquivos certos.
3. Tela de configuração do painel é mínima (só o campo de timeout) -- não
   é um editor de configurações genérico; os demais campos de conta
   continuam só editáveis via JSON.
4. **Nenhuma validação real no Windows com um vídeo que leve mais de 45s
   foi feita nesta rodada** -- só testes automatizados com Playwright fake.

**Este Gate NÃO está declarado concluído.** A validação final desta
correção específica é um teste real no Windows com um vídeo que
demonstravelmente leve mais de 45s para o YouTube confirmar (reproduzindo
o caso real do 006.mp4) -- essa confirmação só pode vir do usuário.

**NÃO iniciado: Prompt 20. NÃO iniciada: Parte 3.**

================================================================
## RODADA 21/09/2026 (2) -- GATE 19.5 CORREÇÃO: TikTok "Não encontrei o
## dia X no calendário" (evidência: conta @vem.na.bio69, 058.mp4)
================================================================

### Discrepância -- mesma observação da rodada anterior

Os dois HTMLs citados como anexados
(`20260921_131300_dia_nao_encontrado.html`,
`20260921_131355_erro_058.html`) **novamente não estavam presentes nesta
sessão** -- busca exaustiva não encontrou nenhum dos dois. Segui o mesmo
caminho da rodada anterior: usei o texto literal já citado verbatim no
próprio prompt (a contagem exata de "calendar" = 0 ocorrências, os
atributos `data-has-error="true"`/`aria-invalid="true"` descritos, a
mensagem "Agende a publicação com pelo menos 15 minutos de antecedência").
Se quiser que eu confirme via DOM real (em vez do texto já citado), preciso
que os arquivos sejam reenviados.

### O que foi corrigido

Confirmado no código real: `set_schedule_datetime()` preenchia HORA/MINUTO
ANTES da DATA. Isso bate exatamente com a hipótese do prompt -- com hora-alvo
(10:00) numericamente menor que a hora do relógio no momento da execução
(~13h) e a data do formulário ainda em "hoje", aplicar a hora primeiro faz
o TikTok considerar o agendamento sem os 15 minutos mínimos de
antecedência, entrando num estado de erro transitório -- e o HTML de
evidência confirma que o popover do calendário já tinha fechado nesse
momento (zero ocorrências de "calendar" no DOM capturado).

**Correção aplicada:** inverti a ordem em `set_schedule_datetime()`
(`_sistema/agendar_tiktok.py`) -- agora a DATA é clicada e confirmada
primeiro (abre o calendário, navega os meses se precisar, clica no dia),
e SÓ DEPOIS a hora e o minuto são selecionados. `find_date_time_inputs()`
já localiza `date_el`/`time_el` pelos atributos reais dos campos (não pela
ordem de preenchimento), então trocar a ordem de uso não muda como os
elementos são encontrados. O gate final já existente
(`read_observed_schedule_datetime` comparando `expected_dt == observed_dt`
antes do clique final) não foi alterado -- só o momento em que cada campo é
preenchido mudou.

Não implementei a alternativa (aguardar `data-has-error` desaparecer)
porque a inversão de ordem já resolve a causa raiz e é mais simples, como
o próprio prompt preferia.

### Investigação: `data-has-error` foi mesmo a causa do calendário fechar?

Não dá para provar com certeza absoluta só com o HTML estático pós-falha
(o calendário já tinha sumido do DOM no momento da captura, sem
transição observável) -- e os dois arquivos reais não estavam disponíveis
nesta sessão para uma investigação mais profunda (ex.: screenshots
intermediários com Playwright não-headless). Mas a causa raiz estrutural
identificada (hora aplicada antes da data, criando uma combinação
momentaneamente inválida) é suficiente para justificar a correção por si
só, como o próprio prompt reconhece -- e o padrão é sistemático (acontece
sempre que hora-alvo < hora real), não um caso raro.

### A política de copyright influencia esta etapa?

**Não, e isso está provado estruturalmente, não só por inspeção:**
`set_schedule_datetime()` nem recebe `cfg` como parâmetro (só `page` e
`target_dt`) -- não há caminho para a política alcançar esta função. Além
disso, no código-fonte de `schedule_one()`, a chamada a
`set_schedule_datetime()` acontece MUITO antes de qualquer uso de
`copyright_warning_policy`/`_copyright_warning_policy_from_cfg()` (que só
entra perto do fim, na seção "Verificações"). Dois testes novos provam
isso: um confirma a assinatura da função (sem `cfg`), outro confirma pela
posição real no código-fonte que a chamada de agendamento de data/hora vem
antes da leitura de política, e um terceiro roda `schedule_one()` ponta a
ponta com ALLOW e com BLOCK no mesmo cenário e confirma que a ordem de
cliques (DATA -> HORA) e o resultado são idênticos nos dois casos.

### Testes adicionados (novo arquivo `tests/test_agendar_tiktok_date_before_time_order.py`, 8 testes)

- Clique na DATA acontece antes do clique na HORA (spy de ordem).
- Reprodução do cenário real do 058.mp4 (hora-alvo 10:00, "agora"
  simulado 13:13) -- confirma que a nova ordem completa o fluxo sem erro.
- Hora-alvo MAIOR que a hora atual simulada -- confirma que não há
  regressão no caso que provavelmente já funcionava.
- Teste que FALHARIA se a ordem antiga (hora antes da data) fosse
  reintroduzida.
- Virada de mês (`month_delta` = 2) -- confirma que a navegação do
  calendário continua funcionando com a data sendo preenchida primeiro.
- Assinatura de `set_schedule_datetime()` sem `cfg` (prova estrutural de
  isolamento da política de copyright).
- Posição de `set_schedule_datetime()` antes da leitura de política no
  código-fonte de `schedule_one()`.
- `schedule_one()` ponta a ponta com ALLOW e com BLOCK produzindo a mesma
  ordem de cliques e o mesmo resultado.

### Testes e compilação desta rodada

- Suíte completa (pytest, por arquivo): **1194 testes, 0 falhas** (1186
  antes desta rodada + 8 novos).
- `python3 -m compileall _sistema tests`: **sucesso, sem erros**.
- Nenhum teste existente foi removido ou enfraquecido; os 28 testes de
  `tests/test_agendar_tiktok_time_layers.py` (que já cobriam
  `set_schedule_datetime()`) continuam passando sem alteração no próprio
  arquivo de teste.

### Confirmação: YouTube não foi tocado

`diff` byte-a-byte entre `_sistema/agendar_youtube.py` desta rodada e a
última cópia verificada no Windows: **nenhuma diferença**. `diff` de
`_sistema/painel_oficial.py`: **nenhuma diferença** (também não foi tocado
nesta rodada). A única mudança em todo o projeto foi um hunch contíguo em
`_sistema/agendar_tiktok.py`, dentro de `set_schedule_datetime()` (troca de
ordem DATA/HORA + comentários/prints associados) -- confirmado por diff.

### Arquivos modificados/criados

Modificado: `_sistema/agendar_tiktok.py` (só `set_schedule_datetime()`).
Criado: `tests/test_agendar_tiktok_date_before_time_order.py`.

### Riscos conhecidos e dívida técnica

1. A causa exata do calendário ter fechado não foi confirmada por
   transição observada (só por estrutura de código + texto já citado do
   HTML) -- os arquivos reais anexados não chegaram a esta sessão outra
   vez.
2. Continua sem validação real no Windows agendando um vídeo pra um
   horário do dia seguinte com hora MENOR que a hora atual do relógio (o
   caso real do 058.mp4).

**Este Gate NÃO está declarado concluído.** A validação final é um teste
real no Windows reproduzindo o caso do 058.mp4 -- confirmação só pode vir
do usuário.

**NÃO iniciado: Prompt 20. NÃO iniciada: Parte 3.**

================================================================
## RODADA 21/09/2026 (3) -- GATE 19.5 NOVA FUNCIONALIDADE: recomendação
## de quantidade/horários de postagem por tema do canal, via Ollama
## local, com editor manual por dia da semana -- YouTube e TikTok
================================================================

Esta é uma funcionalidade nova, não uma correção de bug -- tratada
separadamente das correções pendentes (timeout do YouTube/descarte de
rascunho, ordem data-antes-de-hora no TikTok), sem depender delas nem
alterá-las.

### O que existia antes (investigação desta rodada)

Confirmado: `add_account()` nunca perguntava tema, quantidade ou
horários -- toda conta nova recebia sempre os mesmos valores fixos
(`DAY_TIMES` pro YouTube, lista plana pro TikTok). Não havia nenhuma tela
para editar isso depois de criada a conta. `agendar_youtube.py`
`build_slots()` já suportava `horarios_por_dia`; `agendar_tiktok.py`
`build_slots()` só lia `horarios` (assimetria real confirmada). Os
helpers de Ollama (`ollama_alive`/`try_start_ollama`/`ollama_chat`/
`parse_json_text`) já existiam, testados, em `gerar_textos.py`.

### Decisão: duplicar os helpers de Ollama em painel_oficial.py

`gerar_textos.py` faz `BASE = account_dir_from_env(...)` a nível de
módulo -- o mesmo padrão que já impedia importar `agendar_youtube.py` em
`painel_oficial.py` (ver comentário existente sobre
`YOUTUBE_COPYRIGHT_CHECK_TIMEOUT_PADRAO_SEGUNDOS`). Importar
`gerar_textos.py` aqui resolveria esse `BASE` usando o env do próprio
processo do painel (sem `ACCOUNT_DIR` setado), o que é arquiteturalmente
errado. **Decisão: duplicar** `ollama_alive`/`try_start_ollama`/
`ollama_chat`/`parse_json_text` em `painel_oficial.py`, com o MESMO
comportamento (healthcheck, timeout, JSON validado, nunca travar o
fluxo) -- mas reaproveitando a descoberta do executável já existente
neste arquivo (`find_ollama()`) em vez de duplicá-la de novo dentro de
`try_start_ollama()`.

### Schema exato pedido ao Ollama e teto de horários/dia

```
{"segunda": ["HH:MM", ...], "terca": [...], "quarta": [...],
 "quinta": [...], "sexta": [...], "sabado": [...], "domingo": [...]}
```
Os 7 dias são obrigatórios (chaves sem acento, batendo com
`horarios_por_dia` já usado pelos dois motores). Cada dia precisa ter
entre 1 e **6** horários (`HORARIOS_POR_DIA_TETO`) no formato HH:MM
24h -- justificativa: 6 já é o dobro do maior caso real hoje no projeto
(`DAY_TIMES` usa 3/dia), suficiente pra qualquer estratégia razoável sem
aceitar uma alucinação tipo 40 horários num dia. A resposta é pedida com
`format:'json'` e validada com `parse_json_text()` + `_validar_
recomendacao_horarios()` -- OU está completa e dentro do schema, OU é
descartada por inteiro (nunca completa campo ausente com valor
arbitrário).

### Fluxo de criação de conta

Depois de nome/país/idioma/timezone e ANTES de salvar a config: pergunta
o tema (`perguntar_tema_canal()` -- campo obrigatório, ENTER vazio pede
confirmação explícita antes de pular, evitando tanto um loop sem saída
quanto perder a recomendação por ENTER acidental); tenta a recomendação
via Ollama (`gerar_recomendacao_horarios_ollama()` -- healthcheck,
tenta iniciar se preciso, nunca levanta, sempre devolve `None` em
qualquer falha); se veio, mostra formatada (mesmo padrão "Seg:"/"Ter:"
já usado no resumo do YouTube) e pergunta `[s/N]`.

**Decisão documentada, desviando da notação literal do prompt:** um
ENTER vazio ou resposta inválida na confirmação da recomendação NÃO
aceita -- cai no editor manual. O prompt sugeria a notação `[S/n]`
(que convencionalmente indicaria default=Sim), mas pediu explicitamente
"decidam o padrão mais seguro" -- aceitar uma sugestão de IA por omissão
nunca é o padrão mais seguro. Por isso a UI mostra `[s/N]` (não `[S/n]`),
para não prometer visualmente um comportamento que o código não tem.

Se recusada, indisponível ou inválida: cai SEMPRE no editor manual por
dia da semana (`editar_horarios_semana()`), que também pode ser
cancelado (digitando "cancelar" em qualquer dia, ou recusando a
confirmação final) sem travar nada -- nesse caso a conta é criada com o
`horarios_por_dia` default de `default_config()` (DAY_TIMES pro YouTube;
nenhum, pro TikTok -- ver decisão da seção 4 abaixo).

### Editor manual por dia da semana

7 perguntas (segunda a domingo), cada uma aceitando uma lista de
horários separados por vírgula; ENTER mantém o valor atual daquele dia;
horário inválido reper gunta só aquele dia (sem perder os demais já
digitados); mais que 6 horários no mesmo dia também reper gunta;
duplicados são deduplicados e ordenados; resumo final com confirmação
antes de salvar. Reaproveitável tanto na criação (seção 1) quanto na
edição posterior (seção 3, nova opção **9 - EDITAR HORÁRIOS DE POSTAGEM
(POR DIA DA SEMANA)** no menu principal -- escolhe plataforma, escolhe
conta, reabre pré-preenchido com `horarios_por_dia` atual ou o
`horarios`/DAY_TIMES vigente, salva de volta no `config_canal.json` da
conta certa).

### Extensão do TikTok para `horarios_por_dia`

`agendar_tiktok.py` `build_slots()`: mesmo padrão já usado pelo YouTube
(`day_names[d.weekday()]` -> `by_day.get(day_key, default_times)`),
preservando ordenação/corte por `dias_janela`/limite de `video_count`
byte-idênticos. Print de resumo (`process_prepared_batch`, ~linha 2222)
atualizado para mostrar "Seg:"/"Ter:"/etc. quando `horarios_por_dia`
está presente, no mesmo formato já usado pelo YouTube; senão continua
mostrando a linha plana "Horários/dia" de antes.

`default_config()` do TikTok NÃO ganhou um `horarios_por_dia` default
(diferente do YouTube, que já tinha `DAY_TIMES`) -- decisão documentada
no próprio código: não existe um "DAY_TIMES do TikTok" pré-existente
para herdar, então a chave só é adicionada quando a conta realmente
passa pelo fluxo de recomendação/editor e aceita/confirma um valor. Sem
isso, a conta continua usando só `horarios` (lista fixa) -- aditivo,
nunca regressivo.

### Confirmação: contas antigas sem `horarios_por_dia` continuam idênticas

`diff` confirma que a única mudança em `agendar_tiktok.py` foi a adição
da leitura de `horarios_por_dia` em `build_slots()` + o print de resumo
-- nada mais no arquivo mudou (38 linhas de diff, todas dentro desses
dois pontos). `agendar_youtube.py`: **nenhuma diferença** (não tocado
nesta rodada). Testes de regressão explícitos: `TiktokSlotTests` (já
existente, sem `horarios_por_dia`) continua passando sem alteração; novo
teste `test_sem_horarios_por_dia_configurado_comportamento_identico_ao_
de_antes` prova, lado a lado, que uma conta sem a chave nova se comporta
exatamente como antes.

### Confirmação: criação de conta nunca trava esperando o Ollama

`gerar_recomendacao_horarios_ollama()` nunca levanta -- todo caminho de
falha (indisponível, timeout, resposta inválida) devolve `None`
explicitamente, capturado com `except Exception` ao redor da chamada de
rede. Testado com os 5 cenários exigidos (resposta válida; não-JSON;
JSON fora do schema; Ollama indisponível mesmo após tentar iniciar;
timeout) e com um teste end-to-end (`test_add_account_nunca_trava_
esperando_ollama_indisponivel`) que roda `add_account()` de ponta a
ponta com Ollama indisponível E o editor manual cancelado, confirmando
que a conta ainda é criada.

### Testes adicionados

- `tests/test_painel_oficial_horarios_por_dia.py` (novo, 27 testes):
  `normalizar_hhmm()`/`_validar_recomendacao_horarios()` (validação
  estrita); os 5 cenários obrigatórios de
  `gerar_recomendacao_horarios_ollama()`; editor manual (linha vazia
  mantém, horário inválido reper gunta só o dia, cancelar aborta,
  dedup/ordenação, teto de horários); edição posterior de conta
  existente (pré-preenchida, salva na conta certa, não afeta outras
  contas, cancelamento não altera o arquivo); dois testes end-to-end de
  `add_account()` (recomendação aceita grava `horarios_por_dia`
  esperado; Ollama indisponível + editor cancelado não trava a criação).
- `tests/test_schedule_slots.py` (estendido, +3 testes em
  `TiktokHorariosPorDiaTests`): dias com entrada específica usam
  `horarios_por_dia` ordenado; dias ausentes caem no fallback
  `horarios`; regressão explícita sem `horarios_por_dia`.

### Testes e compilação desta rodada

- Suíte completa (pytest, por arquivo): **1224 testes, 0 falhas** (1194
  antes desta rodada + 27 novos em `test_painel_oficial_horarios_por_
  dia.py` + 3 novos em `test_schedule_slots.py`).
- `python3 -m compileall _sistema tests`: **sucesso, sem erros**.
- Nenhum teste existente foi removido ou enfraquecido.

### Arquivos modificados/criados

Modificados: `_sistema/agendar_tiktok.py` (`build_slots()` +
resumo do TikTok), `_sistema/painel_oficial.py` (helpers de Ollama,
validação, editor manual, fluxo de criação de conta, nova opção 9 do
menu, `default_config()` do TikTok documentado).
Criado: `tests/test_painel_oficial_horarios_por_dia.py`.
Estendido: `tests/test_schedule_slots.py`.
NÃO tocado: `_sistema/agendar_youtube.py` (confirmado por diff).

### Riscos conhecidos e dívida técnica

1. **Qualidade prática das recomendações do modelo local não é algo que
   dê pra garantir automaticamente por teste** -- os testes provam que o
   schema é respeitado e que a criação de conta nunca trava, não que os
   horários sugeridos sejam bons de verdade para aquele tema/mercado.
   Isso só se confirma na prática, com o usuário avaliando as sugestões
   reais.
2. Sem instalador ainda (fora de escopo desta rodada, roadmap Prompt
   63) -- o fallback manual é permanente e incondicional, exatamente
   como pedido, não uma dependência de um "modo instalado" que não
   existe.
3. O prompt do Ollama não valida se os horários sugeridos fazem sentido
   de fato para o fuso/mercado além do texto já enviado (país/idioma) --
   qualidade do julgamento fica inteiramente a cargo do modelo local.
4. Continua sem validação real no Windows: a UI do editor/recomendação
   sendo fluida na prática, e o TikTok realmente respeitando
   `horarios_por_dia` numa conta real, ainda não foram testados fora do
   Playwright fake.

**Este Gate NÃO está declarado concluído.** A validação final é um
teste real no Windows -- qualidade prática da recomendação, fluidez do
editor manual, e o TikTok respeitando `horarios_por_dia` numa conta real
-- confirmação só pode vir do usuário.

**NÃO iniciado: Prompt 20. NÃO iniciada: Parte 3.**

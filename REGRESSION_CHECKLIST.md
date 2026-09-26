# REGRESSION CHECKLIST

> Baseline funcional do pacote atual. Use antes/depois de futuras mudanças incrementais.
> Itens marcados como **LEGADO CONFLITANTE** descrevem o que existe hoje, mas não devem ser preservados após uma etapa explicitamente aprovada para adequação às regras permanentes.

## 1. Integridade do pacote

- [ ] `PAINEL_OFICIAL.bat` continua localizando `py -3` ou `python`.
- [ ] O painel inicia sem alterar contas existentes.
- [ ] Arquivos de estado existentes permanecem legíveis após a mudança.
- [ ] Migration de storage é versionada, idempotente e nunca sobrescreve conflito silenciosamente.
- [ ] Uma mudança em uma ferramenta não obriga execução das demais.

## 2. Menu / navegação

- [ ] Menu principal lista as funções disponíveis e retorna corretamente ao menu.
- [ ] `0 - SAIR` encerra sem exceção.
- [ ] `KeyboardInterrupt` não corrompe arquivos de estado.
- [ ] Seleção de pasta via Tkinter funciona quando disponível.
- [ ] Fallback por caminho digitado funciona quando Tkinter falha.

## 2A. Storage / caminhos centrais

- [ ] No Windows, a raiz padrão é `%LOCALAPPDATA%\PainelOficial`.
- [ ] Código instalado e dados mutáveis não compartilham a mesma raiz.
- [ ] `database`, `accounts`, `projects`, `cache`, `logs`, `temp`, `templates`, `backups`, `models` e `support` são resolvidos por `app_paths.py`.
- [ ] `PAINEL_DATA_ROOT` funciona como override explícito em teste/diagnóstico.
- [ ] Layout legado `contas/` migra para `accounts/` sem perda.
- [ ] Layout legado `_removidas/` migra para `backups/removed_accounts/`.
- [ ] Migration interrompida pode ser executada novamente.
- [ ] Conta é migrada como unidade; conflito em config, vídeo ou perfil Chrome não move nenhum outro arquivo da conta.
- [ ] Árvores origem/destino integralmente idênticas podem eliminar a duplicata legada sem alterar o destino.
- [ ] Cópia entre volumes usa staging completo + validação antes de publicar o diretório final.
- [ ] Staging incompleto de crash nunca aparece em `accounts/<plataforma>/<conta>`.
- [ ] `_removidas/` segue a mesma regra transacional por unidade.
- [ ] `support/storage_migration_v1.json` registra status/conflitos sem incluir conteúdo de arquivos.
- [ ] Scripts internos sem `ACCOUNT_DIR` não usam `_sistema/` como diretório de dados nem compartilham o mesmo fallback entre engines.

## 2B. Modelos de domínio

- [ ] `SourceAsset`, `Video`, `Project`, `Account`, `Job`, `Publication`, `Schedule`, `Template`, `Artifact` e `ErrorRecord` importam sem dependências de UI/plataforma.
- [ ] Toda nova entidade recebe UUID válido e distinto.
- [ ] `to_dict()` + `from_dict()` preservam o mesmo UUID.
- [ ] `from_dict()` rejeita payload persistido sem `id` em vez de gerar identidade nova silenciosamente.
- [ ] `from_dict()` rejeita `model_type` incompatível.
- [ ] Campos desconhecidos sobrevivem ao round-trip para compatibilidade de schema.
- [ ] `SourceAsset` (origem), `Project` (edição), `Job` (processamento) e `Publication` (publicação) permanecem entidades distintas.
- [ ] Um `Video` pode ser referenciado por múltiplos `Job` e múltiplas `Publication`.
- [ ] `Schedule` distingue `LOCAL_PENDING`, `REMOTE_SCHEDULED` e `UNKNOWN`; `UNKNOWN` nunca é tratado como `LOCAL_PENDING`.
- [ ] `Schedule.publication_id` é a relação canônica com `Publication` e aceita somente UUID válido quando preenchido.
- [ ] `Publication` não possui `schedule_id`, evitando relação circular/duas fontes de verdade.
- [ ] `Job` aceita somente os estados canônicos da State Machine central.
- [ ] Mudanças de estado de `Job` passam por `transition_to()`; atribuição direta não contorna validação.
- [ ] `PUBLISHED` e `CANCELLED` não retornam arbitrariamente para estados ativos.
- [ ] `PUBLISHING` não transita diretamente para `RETRY`.
- [ ] Resultado remoto incerto segue `PUBLISHING -> UNKNOWN -> RECOVERING`; somente após reconciliação segura pode seguir para `RETRY`.
- [ ] Falha conhecida/comprovada sem efeito remoto pode seguir `PUBLISHING -> FAILED -> RETRY`.
- [ ] `UNKNOWN` não transita diretamente para `RETRY` nem `PUBLISHING`; primeiro deve ir para `RECOVERING`.
- [ ] Reconciliação inconclusiva pode retornar `RECOVERING -> UNKNOWN` sem gerar retry automático.
- [ ] Nenhum modelo novo passa a ser fonte de verdade dos JSONs legados sem migration explicitamente autorizada.

## 2C. SQLite local

- [ ] Banco padrão fica em `database/painel.db`, fora da pasta instalada.
- [ ] Schema cria `videos`, `sources`, `projects`, `accounts`, `jobs`, `publications`, `schedules`, `templates`, `artifacts`, `errors`, `settings`, `audit_events` e `migrations`.
- [ ] Migration `001` congelada e migration `002_audit_append_only` são registradas uma única vez com checksum; `PRAGMA user_version` fica em 2.
- [ ] Reexecutar `initialize()` é idempotente, inclusive com várias instâncias inicializando simultaneamente o mesmo banco novo.
- [ ] A decisão de migration é tomada após `BEGIN IMMEDIATE`; a segunda instância relê o histórico sob lock e não reaplica DDL.
- [ ] Histórico aplicado forma prefixo contínuo das migrations conhecidas; gaps como `001,003` sem `002` são rejeitados.
- [ ] Banco com `PRAGMA user_version`/migration mais novo que o binário é recusado.
- [ ] Checksum divergente de migration aplicada interrompe abertura/migration de forma explícita.
- [ ] `PRAGMA foreign_keys=ON` em toda conexão e referências órfãs são rejeitadas.
- [ ] Deleção de pai referenciado não apaga filhos silenciosamente.
- [ ] WAL está ativo no banco local em arquivo e `busy_timeout` é configurado.
- [ ] Erro dentro de transação faz rollback integral, sem estado parcial.
- [ ] CRUD/round-trip preserva UUID e dados dos 10 modelos de domínio.
- [ ] `settings` suporta create/read/update/delete de JSON local.
- [ ] Writers concorrentes básicos concluem sem perda de linhas.
- [ ] `schedules.publication_id` aceita no máximo um Schedule por Publication quando não nulo; múltiplos NULL continuam permitidos.
- [ ] Engines legados continuam sem ler/escrever SQLite até migration de adoção explicitamente autorizada.
- [ ] `OperationalAuditLog.create_job()` grava Job + `JOB_CREATED` atomicamente.
- [ ] Toda transição persistida grava `JOB_STATE_CHANGED` com `from_state`/`to_state` na mesma transaction do Job.
- [ ] Pause/resume/cancel/retry/processamento concluído/upload/confirmação/reconciliação/erro/recovery possuem eventos operacionais explícitos.
- [ ] Falha durante gravação de evento semântico faz rollback do estado e dos eventos anteriores da mesma operação.
- [ ] `audit_events` não aceita UPDATE/DELETE nem reutilização de ID via `INSERT OR REPLACE`, inclusive por conexão sqlite3 externa aos helpers; append com UUID novo continua permitido.
- [ ] Upgrade de banco schema 1 para 2 cria `PRE_MIGRATION` válido antes do DDL, aplica somente a 002 e é idempotente na segunda inicialização.
- [ ] Histórico de Job pode ser reconstruído após reabrir o banco e cadeias inconsistentes são rejeitadas.


## 2D. Backup / restore operacional SQLite

- [ ] Snapshot é criado com SQLite Backup API e permanece consistente mesmo com WAL.
- [ ] Cada backup publicado possui `painel.db` + `manifest.json` válidos.
- [ ] Manifest registra ID, UTC, motivo, método, validation status, path lógico, schema, migrations, tamanho, SHA-256 e `integrity_check=ok`.
- [ ] Metadata do manifest é allowlistada por gatilho; cookies/tokens/credentials/perfis não podem ser persistidos por mapping arbitrário.
- [ ] Diretório `.staging` incompleto nunca aparece como backup válido.
- [ ] Backup periódico respeita a janela configurada somente quando existe PERIODIC recente **válido**; snapshot/hash/manifest/integrity inválido não bloqueia nova proteção.
- [ ] Criar 25+ backups não remove nenhum automaticamente, mesmo que exista `BackupRetentionPolicy` configurada.
- [ ] `prune_backups()` só remove quando chamado explicitamente; defaults preservam tudo.
- [ ] Migration futura `vN -> vN+1` cria backup `PRE_MIGRATION` antes do DDL.
- [ ] Update importante deve chamar `backup_before_update()` antes de substituir código/schema.
- [ ] Restore valida backup fonte antes de qualquer operação destrutiva.
- [ ] Restore cria e valida `PRE_RESTORE` persistente do banco atual; falha desse safety backup aborta antes de staging/swap.
- [ ] Restore bem-sucedido mantém o `PRE_RESTORE` e ele contém exatamente o estado imediatamente anterior.
- [ ] Journal de restore persiste fases antes de cada passo crítico; crash em `OLD_MOVED` nunca cria banco vazio.
- [ ] Crash em `NEW_PUBLISHED` valida o candidato oficial e finaliza/rollback deterministicamente.
- [ ] Restore/recovery distingue schema do source e schema do previous; backup antigo suportado pode ser restaurado sobre DB mais novo e depois migrado por `LocalDatabase.initialize()`.
- [ ] `LocalDatabase.initialize()` executa recovery antes de abrir/criar `painel.db`.
- [ ] Artefato de restore sem journal suficiente gera `RestoreRecoveryRequiredError`, sem apagar ou escolher arquivo por data.
- [ ] Dois restores concorrentes (inclusive processos independentes) não executam dois swaps simultâneos.
- [ ] Sidecars `restore_tmp/restore_previous` são limpos somente após fase conhecida; WAL/SHM operacional não é apagado no início por heurística.
- [ ] Backup corrompido continua recusado mesmo se tamanho/SHA do manifest forem recalculados para o arquivo corrompido.
- [ ] Vídeos originais, renders, perfis Chrome, cache, temp e models não são copiados pelo backup operacional.

## 3. Contas

- [ ] Criar conta YouTube cria `videos`, `dados`, `logs`, `perfil_youtube`, `bloqueados` e config.
- [ ] Criar conta TikTok cria `videos`, `dados`, `logs`, `perfil_tiktok` e config.
- [ ] Nomes inválidos de Windows são sanitizados.
- [ ] Conta duplicada em diferença apenas de maiúsculas/minúsculas não é duplicada.
- [ ] Cada conta mantém estado independente.
- [ ] Cada conta usa perfil Chrome independente.
- [ ] Remover conta move para `backups/removed_accounts` em vez de apagar.
- [ ] Importação legada preserva vídeos/estado esperado sem substituir plataforma/idioma novo indevidamente.
- [ ] Não executar duas instâncias simultâneas sobre a mesma conta enquanto não existir lock formal.

## 4. Login / perfil Chrome

- [ ] YouTube abre `studio.youtube.com` com o `perfil_youtube` correto.
- [ ] TikTok abre TikTok Studio com o `perfil_tiktok` correto.
- [ ] Fechar o Chrome faz `login_conta.py` retornar ao painel.
- [ ] Reabrir a mesma conta mantém a sessão.
- [ ] Perfil de uma conta nunca é reutilizado por outra conta.
- [ ] CAPTCHA/2FA/challenge nunca é contornado automaticamente.

## 5. Tratamento de vídeo / FFmpeg

- [ ] Originais nunca são sobrescritos.
- [ ] Entrada aceita as extensões previstas.
- [ ] Pasta vazia retorna erro amigável.
- [ ] Ausência de FFmpeg/ffprobe retorna erro amigável.
- [ ] `ffprobe` detecta resolução, FPS, duração e áudio.
- [ ] Arquivo com áudio mantém áudio sincronizado após mudança de velocidade.
- [ ] Arquivo sem áudio não recebe stream de áudio artificial.
- [ ] Saída usa H.264/AAC compatível e `yuv420p`.
- [ ] Apenas uma saída é criada por original único.
- [ ] Numeração começa/continua em `001`, `002`, ... sem sobrescrever existentes.
- [ ] Numeração considera histórico de postagem mesmo se MP4 antigo tiver sido apagado.
- [ ] Mesmo original renomeado/movido não gera nova saída se fingerprint já estiver `done`.
- [ ] Duplicado idêntico dentro do mesmo lote é ignorado.
- [ ] Crash com item `pending` reaproveita o mesmo número na retomada.
- [ ] Falha FFmpeg remove saída parcial detectada pelo processo.
- [ ] Falha individual não encerra os demais vídeos do lote.
- [ ] Retry serial usa o mesmo número reservado.
- [ ] Estado é salvo depois de cada conclusão/falha.
- [ ] `FFMPEG_ERROS.log` e `falharam.txt` são produzidos somente quando necessário.

### LEGADO CONFLITANTE — validar somente até a etapa autorizada de substituição

- [ ] O preset atual aplica zoom/ajustes/noise/speed/volume conforme baseline.
- [ ] O preset atual remove metadata original e adiciona metadata Apple/iPhone/GPS/data simulada.
- [ ] Quando `KEEP/CLEAN/PROFILE` for implementado, estes checks devem ser substituídos pelos novos contratos e não mantidos por compatibilidade cega.

## 6. Fingerprints

- [ ] O mesmo arquivo gera o mesmo fingerprint em painel, textos, YouTube e TikTok.
- [ ] Arquivos diferentes usados nos testes não colidem.
- [ ] Alterar conteúdo relevante muda o fingerprint nos casos de teste normais.
- [ ] Futura mudança de algoritmo deve ter versionamento/migration; não trocar silenciosamente.

## 7. Geração de textos / Whisper

- [ ] Ausência de FFmpeg/ffprobe bloqueia a etapa antes do lote.
- [ ] Ausência de Ollama bloqueia a etapa antes do lote.
- [ ] Ausência de faster-whisper bloqueia a etapa antes do lote.
- [ ] Whisper roda localmente.
- [ ] Transcrição de um vídeo é salva em `dados/transcricoes`.
- [ ] Segunda execução reaproveita a transcrição cacheada.
- [ ] Vídeo sem fala cai no caminho de visão.
- [ ] Frames são extraídos localmente.
- [ ] Frames temporários são removidos ao final normal/falha tratada.
- [ ] Ollama visão não inventa contexto no prompt baseline.
- [ ] Ollama texto devolve JSON parseável ou o item é marcado como falha.
- [ ] Falha em um vídeo não encerra os vídeos seguintes.
- [ ] `textos_postagem.json` é salvo após cada item.
- [ ] `textos_postagem.csv` permanece derivável do JSON.
- [ ] Segunda execução processa somente pendentes/desatualizados.
- [ ] `--forcar` força regeneração quando explicitamente solicitado.
- [ ] Edição manual persiste e aparece no CSV.
- [ ] YouTube recebe título/descrição/# do fingerprint correto.
- [ ] TikTok recebe caption/# do fingerprint correto.

## 8. YouTube — validação pré-upload

- [ ] Horizontal é rejeitado quando `exigir_formato_short=True`.
- [ ] Duração acima do limite configurado é rejeitada.
- [ ] Falha de ffprobe gera aviso/estado esperado, sem crash silencioso.
- [ ] Rejeitados aparecem em `youtube_nao_shorts.txt`.
- [ ] Vídeo >60 s gera o aviso baseline configurado.

## 9. YouTube — upload e campos

- [ ] Perfil persistente correto é aberto.
- [ ] Login ausente é detectado.
- [ ] Modal de upload é localizado/aberto.
- [ ] `Filedata` é localizado e recebe o arquivo correto.
- [ ] Tela de detalhes é detectada após anexar.
- [ ] Título correto é preenchido.
- [ ] Descrição correta + hashtags são preenchidas.
- [ ] “Não é conteúdo para crianças” é marcado quando configurado.
- [ ] Botões Próxima avançam pelas etapas esperadas.

## 10. YouTube — Content ID / direitos autorais

- [ ] Texto “Nenhum problema encontrado” não gera falso positivo.
- [ ] Termos fixos da seção “copyright” sozinhos não geram falso positivo.
- [ ] Claim real detectado duas vezes consecutivas gera bloqueio.
- [ ] Item bloqueado não é salvo como agendado.
- [ ] Item bloqueado é movido para `bloqueados/direitos_autorais` quando possível.
- [ ] Log `direitos_autorais_bloqueados.txt` é atualizado.
- [ ] Falha ao descartar remotamente fica diagnosticável.
- [ ] Fila continua após `CopyrightClaimError`.

## 11. YouTube — data/hora e conclusão

- [ ] Data correta é preenchida.
- [ ] Hora correta é selecionada.
- [ ] Seleção de hora não fecha acidentalmente o diálogo inteiro.
- [ ] Botão final só é clicado quando disponível.
- [ ] Limite diário é detectado nos pontos previstos.
- [ ] `confirm_success()` não aceita somente fechamento de modal.
- [ ] Rascunho é tratado como falha.
- [ ] Programado/Scheduled/Agendado é reconhecido.
- [ ] Estado local só é atualizado após confirmação positiva do fluxo atual.

### Testes obrigatórios de risco antes de liberar mudanças de publicação

- [ ] Dois vídeos com título idêntico não podem confirmar/reconciliar um ao outro.
- [ ] Crash simulado depois da confirmação remota e antes do save local não pode causar repost automático.
- [ ] JSON de estado corrompido não pode ser tratado como fila vazia.
- [ ] Resultado incerto deve virar `UNKNOWN` quando esse estado for implementado.

## 12. TikTok — upload e caption

- [ ] Login ausente é detectado.
- [ ] Input file aparece/é localizado.
- [ ] Arquivo correto é anexado.
- [ ] Editor de caption é localizado.
- [ ] Caption do fingerprint correto é persistida na UI.
- [ ] Rádio `schedule` é marcado.
- [ ] Autorização de agendamento inicial, quando presente, é tratada.
- [ ] Campos de data/hora aparecem após Programar.
- [ ] Hora e minuto corretos são escolhidos.
- [ ] Data correta é escolhida no calendário.
- [ ] Botão final fica habilitado antes do clique.

## 13. TikTok — challenge

- [ ] CAPTCHA/challenge é detectado.
- [ ] Sistema pausa e exige ação humana.
- [ ] Nenhum código tenta resolver/burlar o challenge.
- [ ] Após ação humana, o fluxo pode continuar.
- [ ] Em arquitetura futura, validar persistência `USER_ACTION_REQUIRED` através de reinício.

## 14. TikTok — conclusão

### Baseline atual

- [ ] Texto explícito de agendamento é reconhecido quando aparece.
- [ ] Estado é salvo após `schedule_one()` retornar.

### Testes obrigatórios antes de considerar produto comercial

- [ ] **Não** marcar concluído quando só houve clique sem confirmação.
- [ ] Crash pós-clique/pré-save não pode repostar automaticamente.
- [ ] Novo `001.mp4` com fingerprint novo não pode ser ignorado só porque o nome existiu em lote antigo.
- [ ] JSON corrompido não pode ser interpretado como zero publicados.

## 15. Slots/agendamento e timezone

- [ ] Primeira execução começa amanhã quando configurado, no timezone IANA da conta.
- [ ] Execuções seguintes começam após o último instante UTC persistido.
- [ ] Nenhum slot passado é usado.
- [ ] Slots respeitam horários configurados.
- [ ] `build_slots()` nunca retorna mais itens que a quantidade solicitada.
- [ ] YouTube respeita horários por dia da semana.
- [ ] TikTok respeita janela de dias configurada.
- [ ] Registro novo preserva `scheduled_local`, `timezone_iana`, `scheduled_utc` e `time_origin`.
- [ ] Brasil resolve para `America/Sao_Paulo` e Estados Unidos para `America/New_York`.
- [ ] Todos os presets oficiais atuais resolvem para timezone IANA válido.
- [ ] Mercado desconhecido/Personalizado sem timezone explícito falha claramente e nunca vira UTC.
- [ ] `timezone_iana="UTC"` explicitamente configurado permanece válido.
- [ ] Timezone explícito sempre vence o default do país.
- [ ] `Schedule` com `scheduled_local` ou `scheduled_utc` exige `timezone_iana`; Schedule ainda sem horário pode existir sem timezone.
- [ ] `MANUAL` e `RECOMMENDED` são aceitos como origem; nenhuma IA de recomendação é presumida.
- [ ] Alterar o timezone atual do Windows não reinterpreta slot já persistido.
- [ ] Reiniciar em outro timezone mantém o mesmo instante UTC e a intenção local original.
- [ ] Horário inexistente por DST não é deslocado silenciosamente.
- [ ] Horário ambíguo preserva o instante UTC escolhido.
- [ ] TikTok Connector deve validar separadamente que o horário canônico calculado foi realmente aplicado na interface; este risco continua aberto. No Prompt 59: converter o Schedule canônico para o timezone efetivo da UI quando necessário, preencher, reler, converter o valor aplicado para instante e comparar com `scheduled_utc` antes do clique final.

## 16. Resume / interrupção

### O que existe hoje

- [ ] Limpeza retoma por `limpeza_estado.json`.
- [ ] Textos retomam por `textos_postagem.json`.
- [ ] Publicadores retomam por listas `scheduled`.
- [ ] `KeyboardInterrupt` não apaga arquivos originais.

### O que precisa entrar quando Job Engine for implementado

- [ ] pause persistente;
- [ ] resume persistente;
- [ ] stop_after_current;
- [ ] cancel;
- [ ] crash recovery;
- [ ] `UNKNOWN` sem retry automático;
- [ ] `USER_ACTION_REQUIRED` persistente;
- [ ] locks/leases de conta e recurso;
- [ ] retry policy por tipo de erro.

## 17. Dados e privacidade

- [ ] Nenhum teste envia perfil Chrome para rede externa.
- [ ] Nenhum teste envia cookies/tokens a backend próprio.
- [ ] Transcrições permanecem locais.
- [ ] Vídeos permanecem locais exceto upload explícito à plataforma escolhida.
- [ ] Logs brutos permanecem locais.
- [ ] Qualquer telemetria futura usa payload sanitizado e allowlist.
- [ ] Perfis em `backups/removed_accounts` são tratados como segredo; `_removidas` é apenas legado de migração.

## 18. Dependências

- [ ] Python 3.10+.
- [ ] `playwright` importável.
- [ ] `faster_whisper` importável.
- [ ] FFmpeg disponível.
- [ ] FFprobe disponível.
- [ ] Chrome disponível.
- [ ] Ollama disponível quando textos forem usados.
- [ ] `llama3.2` disponível.
- [ ] `moondream` disponível.
- [ ] Futuras releases usam versões pinadas/testadas.

## 19. Testes estáticos mínimos por mudança

- [ ] `python -m py_compile` em todos os `.py`.
- [ ] importar módulos sem iniciar `main()`.
- [ ] executar testes automatizados existentes.
- [ ] se não houver suíte, registrar explicitamente “nenhuma suíte existente”.
- [ ] validar que nenhum arquivo original fora do escopo mudou por hash/diff.

## 20. Teste manual mínimo de smoke

Em uma conta de teste isolada e sem conteúdo importante:

1. abrir painel;
2. listar contas;
3. criar uma conta de teste;
4. abrir/fechar login;
5. processar 1 vídeo curto;
6. interromper e retomar limpeza com outro lote de teste;
7. gerar texto para 1 vídeo;
8. rodar novamente e confirmar cache/resume;
9. editar texto manualmente;
10. em ambiente de teste da plataforma, validar um agendamento com confirmação real;
11. confirmar JSON de estado e arquivos de log esperados;
12. nunca usar conta de produção para testar mudanças de publicação sem reconciliação segura.


## Migration JSON legado -> SQLite

- [ ] descoberta considera somente os JSONs operacionais allowlistados; nunca varre perfis Chrome;
- [ ] antes de qualquer write SQLite, existe snapshot byte-a-byte dos JSONs com SHA-256;
- [ ] backup da migration não duplica `videos/`, renders ou perfis;
- [ ] JSON corrompido falha de forma explícita e não publica estado parcial no SQLite;
- [ ] todos os JSONs migrados permanecem fisicamente intactos no local original;
- [ ] `config_canal.json` produz Account determinístico;
- [ ] SourceAsset original nunca é sobrescrito por output/render de estado de publicação;
- [ ] `pending` com output reservado não cria Artifact;
- [ ] `done` sem output existente e >0 bytes não vira READY e gera warning/ErrorRecord;
- [ ] `done` com output válido vira READY + Artifact com tamanho correto;
- [ ] duas migrations concorrentes em processos independentes resultam em `MIGRATED` + `NOOP`, sem duplicatas;
- [ ] zero JSON retorna `NO_DATA`, sem marker/audit, e não bloqueia migração futura;
- [ ] publicação legada é importada como UNKNOWN, sem promover confirmação remota inexistente;
- [ ] horário legado sem timezone suficiente nunca inventa UTC;
- [ ] payloads integrais ficam preservados em `settings/legacy_json/v1/...` com checksum;
- [ ] contagens são verificadas antes do commit;
- [ ] falha após writes faz rollback de todos os registros da migration;
- [ ] primeira execução retorna `MIGRATED`; segunda execução idêntica retorna `NOOP`;
- [ ] a segunda execução não cria backup, entidade ou audit event duplicado;
- [ ] mudança dos JSONs depois da conclusão é recusada em vez de mesclada silenciosamente;
- [ ] relatório final contém digest do conjunto, backup e contagens importadas.


## Lifecycle SQLite / Windows

- [ ] `LocalDatabase.initialize()` fecha deterministicamente a `sqlite3.Connection` mesmo quando migration/validação falha.
- [ ] `LocalDatabase.connection()` e `transaction()` continuam fechando conexão no `finally`.
- [ ] conexões SQLite diretas em backup/tests usam `close()` determinístico (`try/finally` ou `contextlib.closing`).
- [ ] após `initialize()` repetido e operações normais, `painel.db` pode ser renomeado e a árvore temporária removida sem `PermissionError`/WinError 32.
- [ ] `tests/test_backup_restore.py` termina sem manter handles em snapshots/bancos temporários.
- [ ] fallback standalone de YouTube/TikTok é comparado semanticamente por `pathlib.Path`, sem assumir `/` como separador.

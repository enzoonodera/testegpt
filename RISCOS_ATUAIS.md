# RISCOS ATUAIS

> Auditoria do estado atual, sem correções nesta etapa.
> Severidade usada: **P0 crítica**, **P1 alta**, **P2 média**, **P3 baixa**.

## 1. Resumo prioritário

Os riscos mais relevantes hoje estão concentrados em publicação/agendamento e persistência de estado. A limpeza/render e a geração de textos já possuem checkpoint por item razoável, mas YouTube/TikTok não possuem um protocolo transacional capaz de diferenciar com segurança `não enviado`, `enviado`, `confirmado` e `resultado desconhecido` após crash.

## 2. P0 — corrupção de JSON pode virar estado vazio e causar repost

### Evidência

`load_json()` em vários módulos captura qualquer exceção e retorna um default. Nos agendadores, o default é um estado com `scheduled: []`.

Referências:

- `_sistema/agendar_youtube.py:41-47`, `1436`;
- `_sistema/agendar_tiktok.py:40-46`, `673`.

### Cenário

1. `estado_youtube.json` ou `estado_tiktok.json` fica inválido/corrompido;
2. `load_json()` devolve estado vazio sem diferenciar corrupção de primeira execução;
3. todos os vídeos parecem pendentes;
4. o sistema pode tentar publicar novamente itens já publicados.

### Regra permanente afetada

`UNKNOWN nunca pode gerar repost automático` e crash recovery sem duplicação.

## 3. P0 — janela de crash entre ação remota e persistência local

### YouTube

`schedule_one()` pode confirmar Programado e retornar. Só depois o loop adiciona o item ao estado e salva `estado_youtube.json`.

Referências: `_sistema/agendar_youtube.py:1365-1375`, `1604-1664`.

### TikTok

O clique remoto ocorre dentro de `schedule_one()`; o registro local é salvo depois.

Referências: `_sistema/agendar_tiktok.py:611-621`, `752-770`.

### Cenário

Se houver queda de energia/crash após a plataforma aceitar a operação e antes de `save_json()`, a próxima execução não sabe que a publicação aconteceu e pode reenviar.

### Ausência estrutural

Não há estado pré-clique `SUBMITTING`, attempt id, remote id, idempotency key ou `UNKNOWN` persistido.

## 4. P0 — TikTok tem falsa confirmação explícita

### Evidência

Após o clique final, se o body não contiver confirmação reconhecida, o código ainda imprime `OK: clique de agendamento concluído` e retorna sem erro. O loop então salva o item como agendado.

Referências: `_sistema/agendar_tiktok.py:611-621`, `762-769`.

### Impacto

Pode haver:

- item marcado como concluído sem ter sido agendado;
- perda silenciosa de publicação;
- impossível distinguir `CONFIRMED` de `UNKNOWN`.

### Regra permanente afetada

“Nunca considerar upload/publicação/agendamento concluído apenas porque um botão foi clicado.”

## 5. P0 — reconciliação de rascunhos do YouTube é baseada em título

### Evidência

`reconcile_visible_drafts()` cria um mapa por título e, se uma linha de rascunho contiver esse título, remove do estado todos os registros locais associados ao mesmo título.

Referências: `_sistema/agendar_youtube.py:1376-1427`.

### Impacto

Títulos podem se repetir. Um rascunho com título comum pode remover registros de vídeos realmente agendados, fazendo-os voltar para a fila e possibilitando duplicação.

## 6. P0 — confirmação YouTube pode associar a linha errada quando títulos repetem

### Evidência

`confirm_success()` procura linhas que contenham o título esperado e aceita Programado/Scheduled. Não existe ID remoto único.

Referências: `_sistema/agendar_youtube.py:1024-1073`.

### Impacto

Com títulos repetidos, uma linha antiga/agendada pode satisfazer a confirmação do vídeo atual. Isso cria falsa confirmação e agrava a janela transacional.

## 7. P0 — script de exclusão do Windows Defender conflita com regra permanente

### Evidência

`defender_setup.ps1` chama `Add-MpPreference -ExclusionPath` e pode elevar via UAC.

Referências: `_sistema/defender_setup.ps1:1-39`.

### Estado atual

Não foi encontrada chamada ao script no pacote; portanto parece órfão/latente. Mesmo assim, sua execução manual faria exatamente o que as regras permanentes proíbem.

## 8. P1 — “limpar metadata” altera vídeo e injeta metadata fabricada

### Evidência

Além de remover metadata, o script altera pixels, velocidade e áudio e adiciona data/GPS/modelo Apple/iPhone/iOS aleatórios.

Referências: `_sistema/limpar_metadados_oficial.py:163-230`, `345-459`.

### Impacto

- não corresponde semanticamente a `CLEAN`;
- não existe `KEEP`;
- não existe `PROFILE` explícito configurado pelo usuário;
- comportamento pode ser interpretado como disfarce de origem;
- não é uma ferramenta de metadata independente do restante das transformações.

## 9. P1 — ausência de locks permite duas instâncias da mesma conta

### Evidência

Há atomic replace, mas nenhum lock de conta, job ou arquivo.

### Cenários

Dois painéis/processos podem simultaneamente:

- reservar o mesmo número;
- ler o mesmo JSON antigo e sobrescrever atualizações um do outro;
- publicar o mesmo próximo vídeo;
- usar o mesmo perfil Chrome ao mesmo tempo.

### Impacto

Duplicação, estado perdido e corrupção lógica.

## 10. P1 — TikTok deduplica também por nome de arquivo

### Evidência

Um item é ignorado se `p.name in done_names OR fp in done_fps`.

Referências: `_sistema/agendar_tiktok.py:679-686`.

### Impacto

Se um novo lote trouxer um novo `001.mp4` com conteúdo diferente, mas o histórico antigo já contiver `001.mp4`, ele será pulado mesmo com fingerprint novo.

Isso contradiz a intenção multilot/multivídeo e pode causar perda de postagem.

## 11. P1 — fingerprint parcial pode colidir logicamente

### Evidência

Hash usa tamanho + primeiro 1 MiB + último 1 MiB, não o conteúdo inteiro.

### Impacto

Dois vídeos com mesmo tamanho e mesmas extremidades, mas região central diferente, serão tratados como o mesmo item em limpeza/textos/publicação.

Probabilidade natural é pequena, mas o efeito de uma colisão é alto: skip, texto errado ou associação de histórico errada.

## 12. P1 — logs HTML/PNG podem conter dados pessoais e identificadores

### Evidência

Agendadores salvam screenshot full-page e HTML completo.

Referências: `_sistema/agendar_youtube.py:84-97`, `_sistema/agendar_tiktok.py:92-105`.

### Impacto

Mesmo sendo locais hoje, esses logs não podem ser reutilizados futuramente como telemetria/suporte remoto bruto. Precisam de sanitização e política de retenção antes de qualquer upload.

## 13. P1 — perfis Chrome são segredos locais completos

### Evidência

Cada conta usa `user_data_dir` persistente, e a importação de legado copia o perfil inteiro.

Referências: `_sistema/login_conta.py:45-60`, `_sistema/painel_oficial.py:186-194`.

### Impacto

- cookies/tokens/sessões ficam no storage local mutável da conta, fora da pasta instalada;
- backup manual, zip do projeto ou suporte remoto pode vazar credenciais;
- `backups/removed_accounts/` retém esses dados indefinidamente.

Não há envio ao servidor no código atual, o que está alinhado à regra de privacidade.

## 14. P1 — Content ID “inconclusivo” é tratado como permissão para seguir

### Evidência

Após timeout, `check_copyright_claims()` retorna `(False, "Verificação não conclusiva... seguindo em frente")`.

Referência: `_sistema/agendar_youtube.py:1223`.

### Impacto

O estado “não sei” é colapsado para “sem problema”. Pode ser aceitável como escolha operacional futura, mas precisa ser explícito; hoje não é modelado nem persistido.

## 15. P1 — possível rascunho remoto órfão após bloqueio de direitos autorais

### Evidência

`discard_current_upload()` é best-effort e admite que o rascunho pode precisar ser apagado manualmente. Mesmo assim, o arquivo local é movido para `bloqueados/direitos_autorais` e a fila continua.

Referências: `_sistema/agendar_youtube.py:1226-1285`, `1606-1629`.

### Impacto

Estado remoto e local podem divergir.

## 16. P1 — desafio TikTok não vira `USER_ACTION_REQUIRED` persistente

### Evidência

`pause_for_human()` bloqueia o terminal com `input()`.

Referências: `_sistema/agendar_tiktok.py:107-112`, `570-571`, `598-599`.

### Impacto

- não sobrevive reinicialização;
- não pode ser apresentado por frontend/job engine de forma consistente;
- um processo headless/background ficaria bloqueado.

A política de não bypass está correta; falta modelagem de estado.

## 17. P1 — ausência de Job Engine / Resource Manager

### Evidência

`run_engine()` apenas chama um subprocesso síncrono. O paralelismo do FFmpeg é local ao próprio script.

Referências: `_sistema/painel_oficial.py:153-158`, `_sistema/limpar_metadados_oficial.py:645-660`.

### Impacto em 200–500+ vídeos

- não há fila global;
- não há prioridade;
- não há limite conjunto CPU/RAM/GPU/VRAM/disco/navegador;
- não há pause/stop_after_current/cancel;
- não há backpressure entre etapas.

## 18. P1 — cache de transcrição sem versão/configuração

### Evidência

`transcricoes/<fp>.json` armazena texto/idioma/data, mas não modelo Whisper nem parâmetros. Referências: `_sistema/gerar_textos.py:169-177`.

### Impacto

Trocar `small` por outro modelo, mudar parâmetros ou corrigir pipeline não invalida transcrições antigas automaticamente.

## 19. P1 — assinatura do pipeline de textos não inclui Whisper

### Evidência

`metadata_signature()` inclui plataforma, locale, país, modelo texto e modelo visão, mas não `whisper_model_size`. Referências: `_sistema/gerar_textos.py:194-199`.

### Impacto

O sistema pode considerar texto “atual” mesmo após uma mudança relevante no estágio de transcrição.

## 20. P1 — TikTok pode aplicar no DOM um horário diferente do slot calculado

### Evidência

O cálculo/persistência agora usa `scheduled_local + timezone_iana + scheduled_utc + time_origin`, e `build_slots()` respeita a quantidade solicitada. Porém o Connector TikTok ainda seleciona data/hora por automação da interface e essa etapa não recebeu reconciliação entre o slot canônico e o valor efetivamente aplicado no DOM.

Referências: `_sistema/time_utils.py`, `_sistema/agendar_tiktok.py` (`build_slots` e preenchimento da interface).

### Impacto

Um horário pode estar correto no estado/cálculo e ainda ser selecionado incorretamente na interface do TikTok. Esse risco pertence ao Connector/DOM e **não** deve ser considerado resolvido pela padronização temporal deste prompt.

**Contrato reservado para o Prompt 59:** o Connector deverá descobrir/considerar o timezone efetivamente usado pela UI/plataforma, converter o `Schedule` canônico para esse timezone quando necessário, preencher data/hora, reler o valor aplicado, convertê-lo/compará-lo como instante contra `scheduled_utc` e somente depois permitir o clique final. O DOM do TikTok não é corrigido nesta etapa.

## 21. P1 — dependências não são reproduzíveis

### Evidência

`requirements.txt` não fixa versões e o painel executa `pip install --upgrade`.

Referências: `requirements.txt:1-2`, `_sistema/painel_oficial.py:414-416`.

### Impacto

Uma atualização de Playwright/faster-whisper pode quebrar o protótipo sem mudança no repositório.

## 22. P2 — versões de estado inconsistentes e sem migrations

### Evidência

O painel cria `estado_youtube.json` version 8, enquanto o agendador usa fallback version 7. Referências: `_sistema/painel_oficial.py:103`, `_sistema/agendar_youtube.py:1436`.

Nenhum código valida `version` ou executa migrations.

## 23. P2 — fallback TikTok pode abrir Chromium diferente sobre o mesmo perfil

### Evidência

Primeiro tenta Playwright com `channel="chrome"`; se falhar, tenta sem channel usando o mesmo `PROFILE_DIR`. Referências: `_sistema/agendar_tiktok.py:737-749`.

### Impacto

Perfis de Chrome/Chromium podem divergir em versão/locking/compatibilidade. Deve ser testado antes de virar produto.

## 24. P2 — instalador baixa Chromium mas YouTube exige canal Chrome

O painel instala `playwright chromium`, mas o YouTube abre `channel="chrome"` sem fallback. Referências: `_sistema/painel_oficial.py:415-418`, `_sistema/agendar_youtube.py:1546-1555`.

Isso aumenta dependências instaladas sem garantir que o fallback instalado seja usado.

## 25. P2 — estado `done` da limpeza só valida existência e tamanho > 0

### Evidência

Um item `done` é reaproveitado se o destino existe e `st_size > 0`. Referência: `_sistema/limpar_metadados_oficial.py:594-600`.

### Impacto

Corrupção posterior do MP4 pode não ser detectada. Não há ffprobe pós-render/hash da saída persistido.

## 26. P2 — saída FFmpeg é escrita diretamente no caminho final

`ffmpeg -y ... <destino>` grava direto no arquivo reservado. Referências: `_sistema/limpar_metadados_oficial.py:403-459`.

Em crash, o estado normalmente permanece pending e a próxima execução sobrescreve o arquivo, o que reduz risco. Ainda assim, um padrão `.part` + validação + rename seria mais transacional em arquitetura futura.

## 27. P2 — `posts_por_dia` não governa necessariamente os slots reais

No YouTube, `build_slots()` usa todos os horários de `horarios_por_dia`; `posts_por_dia` é usado principalmente para cálculo aproximado de dias. Referências: `_sistema/agendar_youtube.py:205-269`, `1529`.

Configurações incoerentes podem produzir uma fila diferente do que o campo sugere.

## 28. P2 — UI/seletores são inerentemente frágeis

YouTube/TikTok são automatizados por DOM/texto da interface, com muitos fallbacks. Mudanças das plataformas podem quebrar upload, data/hora, botões e confirmação.

Os scripts têm bons diagnósticos, mas não existe camada de versão/adaptador/testes de contrato da UI.

## 29. P2 — mensagens e caminhos legados estão desatualizados

Exemplos:

- TikTok menciona `1_LOGIN_TIKTOK_NORMAL.bat`, que não existe no pacote;
- YouTube menciona `1_LOGIN_YOUTUBE_NORMAL.bat`, que não existe no pacote;
- docstring da limpeza menciona `ABRIR_TRATADOR_SPEED.bat` e `limpa_metadados_v4_bala_noise_speed.py`, também ausentes.

Referências: `_sistema/agendar_tiktok.py:118-135`, `_sistema/agendar_youtube.py:486-490`, `_sistema/limpar_metadados_oficial.py:19-27`.

## 30. P2 — código duplicado pode divergir silenciosamente

Persistência/fingerprint/debug estão duplicados em vários scripts. Uma correção futura aplicada em apenas um módulo pode criar regras diferentes por plataforma.

## 31. P2 — código morto/vestigial

Não foram encontradas chamadas para:

- `numero_par_acima()`;
- `destino_para()`;
- `extract_one_frame()`;
- `fill_input_force()`;
- `DailyLimitAfterScheduleError`.

Não é defeito funcional imediato, mas aumenta ambiguidade e custo de manutenção.

## 32. Pontos que encerram toda a fila

### Painel

`run_engine()` é síncrono; o engine atual monopoliza o fluxo até terminar/retornar. Referência: `_sistema/painel_oficial.py:153-158`.

### Limpeza

Encerra antes do lote se:

- FFmpeg/ffprobe ausente;
- pasta de entrada inválida;
- nenhum vídeo encontrado.

Falha individual não encerra o lote; há retry e as demais continuam. Referências: `_sistema/limpar_metadados_oficial.py:548-565`, `660-737`, `782-814`.

### Geração de textos

Encerra antes do lote se:

- plataforma inválida;
- FFmpeg/ffprobe ausente;
- Ollama indisponível;
- faster-whisper ausente.

Falha individual é registrada e o próximo vídeo continua. Referências: `_sistema/gerar_textos.py:252-325`.

### YouTube

Encerra a fila atual em:

- próximo vídeo sem texto quando nenhum anterior pronto;
- limite diário detectado;
- qualquer erro genérico de um vídeo.

Direitos autorais são exceção: movem o item e continuam. Referências: `_sistema/agendar_youtube.py:1507-1518`, `1604-1652`.

### TikTok

Encerra a fila em:

- próximo vídeo sem texto quando nenhum anterior pronto;
- qualquer exceção em `schedule_one()`.

Referências: `_sistema/agendar_tiktok.py:720-725`, `752-760`.

## 33. Risco de perda de estado

Reduzido, mas não eliminado, por `.tmp` + replace. As principais perdas possíveis são:

- concorrência entre duas instâncias sem lock;
- corrupção silenciosamente convertida em default;
- crash na janela remota→persistência;
- cópia/importação de perfil/estado durante uso;
- ausência de journal de tentativas.

## 34. Risco de corrupção

- JSON: atomic replace ajuda contra escrita interrompida, mas não há validação/schema/backup rotativo;
- perfis Chrome: uso concorrente/cópia de perfil pode gerar inconsistência;
- MP4: não há validação persistida pós-render;
- estados legados: cópia direta sem migration/version check.

## 35. Vazamento de segredo

Não foi encontrado envio de segredo para servidor próprio, mas existem superfícies locais sensíveis:

1. perfis Chrome completos;
2. `backups/removed_accounts/` com perfis antigos;
3. HTML/screenshot de páginas autenticadas;
4. caminhos locais em logs;
5. transcrições completas.

## 36. Prioridade recomendada para etapas futuras

Sem implementar agora, a ordem técnica mais segura é:

1. modelar tentativa/publicação com `UNKNOWN` e reconciliação;
2. impedir corrupção/default vazio e concorrência por conta;
3. remover/neutralizar comportamento de Defender em etapa autorizada;
4. separar metadata em `KEEP/CLEAN/PROFILE` sem transformações implícitas;
5. criar Job Engine/Resource Manager persistente;
6. migrar de forma controlada os estados legados para a camada SQLite local já criada;
7. só então ampliar automação e frontend.

Esta seção é uma ordem de redução de risco, não uma implementação nesta etapa.

## 43. Storage V1 — risco de dados mutáveis dentro da pasta instalada mitigado

A raiz de contas e backups foi retirada da pasta do código e centralizada por `_sistema/app_paths.py` em `%LOCALAPPDATA%\PainelOficial` por padrão. O layout legado `contas/` e `_removidas/` é migrado na inicialização sem sobrescrever conflitos.

Riscos residuais:

- conflito entre uma conta/unidade legada e um destino diferente exige intervenção/revisão; a migração agora bloqueia a unidade inteira em vez de produzir merge parcial;
- migração entre volumes pode ser mais lenta que rename no mesmo volume porque exige staging e validação integral antes do commit;
- perfis Chrome e vídeos continuam podendo ocupar muito espaço, agora sob LocalAppData;
- ainda falta política formal de retenção/limite para cache, temp, logs e backups.

O ganho arquitetural é que uma atualização futura do código não precisa tocar na árvore que contém contas, projetos, cache, backups, modelos ou suporte.


## 44. Modelos de domínio V1 — contrato criado, integração ainda pendente

A camada `_sistema/domain/` formaliza `SourceAsset`, `Video`, `Project`, `Account`, `Job`, `Publication`, `Schedule`, `Template`, `Artifact` e `ErrorRecord` com UUIDs persistíveis e relações por ID. `Job` também possui State Machine central com transições explícitas; `PUBLISHING -> RETRY` é proibido, incerteza remota exige `PUBLISHING -> UNKNOWN -> RECOVERING` antes de qualquer retry, e `PUBLISHED`/`CANCELLED` são terminais.

Risco residual: os engines atuais continuam usando JSONs legados e ainda não persistem nem reconciliam essas entidades. A State Machine é contrato de domínio, não Job Engine persistente. Portanto **não existem duas fontes de verdade em runtime nesta etapa**, mas uma integração futura mal planejada poderia criar esse problema. Qualquer adoção operacional desses modelos deve vir acompanhada de schema/migration explícitos e testes de compatibilidade; não se deve simplesmente escrever modelos novos em paralelo aos JSONs e presumir consistência.


## 45. SQLite local V1 — camada criada, dupla fonte de verdade evitada

A camada `_sistema/storage/` cria um banco exclusivamente local com schema v2, migrations 001/002 numeradas/checksum, foreign keys, índices, WAL, transações, settings e audit trail. A inicialização agora é serializada por `BEGIN IMMEDIATE` antes de decidir migrations pendentes, com retry limitado para a negociação concorrente do WAL; o histórico aplicado precisa ser um prefixo contínuo e íntegro. `schedules.publication_id` impõe cardinalidade 0..1 por Publication via índice UNIQUE parcial. CRUD e concorrência básica estão cobertos por testes.

Riscos residuais:

- os engines ainda usam JSONs legados; o SQLite **não** é fonte de verdade operacional nesta etapa, embora exista migration explícita de snapshot/importação dos estados atuais;
- a migration não habilita escrita dupla: depois dela, os engines continuam escrevendo apenas JSON. Integrar engines escrevendo simultaneamente em JSON + SQLite sem cutover/commit coordenado continua proibido;
- backup/restore operacional do SQLite existe com snapshot WAL-consistente, manifest allowlistado, checksum, `integrity_check`, `PRE_RESTORE`, journal crash-safe e lock de restore entre processos; nenhuma retenção roda automaticamente; ainda falta integração com o ciclo real de update e com a futura adoção operacional do banco pelos engines;
- política de checkpoint/compactação do WAL ainda não foi integrada ao ciclo de vida do aplicativo;
- concorrência testada é básica entre writers locais; Job Engine/locks de negócio continuam pendentes;
- migrations futuras devem permanecer imutáveis depois de aplicadas; checksum/nome divergente, gaps no histórico e inconsistência com `PRAGMA user_version` são tratados como falha de integridade.


## 46. Backup operacional V1 — riscos residuais

- O backup atual protege o **estado persistido no SQLite**; como os engines legados ainda usam JSONs, ele não deve ser apresentado como proteção integral do runtime atual até a migration de adoção do banco.
- Vídeos originais e perfis Chrome são excluídos deliberadamente; restauração do banco recompõe estado/metadados, não recria mídia removida do disco.
- Nenhum backup é apagado automaticamente nesta etapa. `prune_backups()` existe apenas como helper explícito para política futura autorizada; isso favorece segurança, mas pode consumir disco até uma política de retenção ser aprovada.
- Restore cria `PRE_RESTORE` e usa journal + lock de kernel entre processos. O journal diferencia schema/hash do source restaurado e do previous/safety, permitindo restore de schema antigo ainda suportado sem confundir recovery. Journal/artefatos ambíguos bloqueiam startup com erro explícito em vez de criar base vazia.
- `backup_before_update()` já existe, mas o updater ainda não existe para invocá-lo automaticamente.
- `maybe_create_periodic()` implementa a decisão de periodicidade usando somente backups integralmente válidos; backups inválidos são preservados para diagnóstico e não contam como proteção. O futuro lifecycle/maintenance service ainda deverá chamar essa função regularmente.
- O lock atual serializa **restores** entre processos. Ele não é um lock global de todas as operações de negócio/SQLite; quando o banco virar fonte operacional, lifecycle/Job Engine deverá impedir que novos writers legítimos sejam iniciados durante a janela de restore.


## 47. Risco residual após migration explícita JSON -> SQLite

A migration V1 é intencionalmente **one-shot por conjunto de checksums**. Rodar novamente com os mesmos arquivos é NOOP; se algum JSON legado mudar depois de uma conclusão, a ferramenta recusa merge incremental automático. Isso evita sobrescrever/mesclar duas fontes de verdade sem política de cutover, mas significa que a adoção efetiva do SQLite pelos engines ainda precisa de uma etapa futura coordenada.

Os JSONs originais são preservados e também copiados para um snapshot local antes da transação. Isso aumenta uso de disco apenas para JSONs, não para vídeos/perfis. `textos_postagem.json` é preservado integralmente como raw state no SQLite porque ainda não existe uma entidade de domínio própria para esse conteúdo. `transcricoes/*.json` não faz parte desta migration de estado operacional e continua sob a política de cache local existente.


## 48. Prompt 10 — riscos fechados na migration JSON -> SQLite

A migration agora serializa a operação inteira entre processos e relê o marker após adquirir o lock, eliminando a corrida que podia duplicar o audit event. `SourceAsset` não é degradado por referências derivadas de publicação. O campo legado `output` é apenas reserva de nome até existir evidência física de saída concluída; Artifact só nasce para `done` + arquivo regular >0 bytes. Ausência total de JSON retorna `NO_DATA` sem marcar a migration como concluída.

Os cinco loaders legados também foram endurecidos: arquivo inexistente continua retornando o default; arquivo existente porém inválido/ilegível gera `StateJsonReadError` e é preservado intacto. Assim, corrupção não pode mais parecer fila vazia.

## 49. Audit log operacional append-only — cobertura e risco residual

Existe agora uma camada `OperationalAuditLog` sobre `audit_events`. Criação e transições de Job podem ser gravadas atomicamente com seus eventos, e o histórico é reconstruível por UUID usando a ordem de append e as transições da State Machine. `UPDATE`, `DELETE` e reutilização de ID via `INSERT OR REPLACE` são bloqueados persistentemente no SQLite por triggers da migration 002; o authorizer de `LocalDatabase` permanece como defesa adicional.

Risco residual: os engines legados ainda não usam essa API, portanto os eventos operacionais reais de publicação/processamento existentes **ainda não são automaticamente auditados**. O audit log está pronto para integração pelo futuro Job Engine/camada de aplicação; conectar engines diretamente agora criaria adoção parcial fora de escopo. A proteção append-only é persistente no schema para SQL comum, inclusive conexões SQLite externas; ela não pretende proteger contra um administrador com acesso ao disco que deliberadamente remova triggers/altere o arquivo fora do produto. Backups/restores continuam sendo operações de banco inteiro e preservam o audit trail como parte do snapshot.


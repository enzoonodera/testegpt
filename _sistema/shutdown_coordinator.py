# -*- coding: utf-8 -*-
"""ShutdownCoordinator central — fechamento seguro do núcleo operacional
(FASE 3 / PROMPT 16, revisado após revisão adversarial pós-implementação).

Este módulo é deliberadamente independente de apresentação e de integrações
remotas, no mesmo espírito de ``job_engine.py``/``control_manager.py``/
``recovery_manager.py``:

- não importa nem conhece UI/frontend;
- não importa nem conhece YouTube, TikTok, Instagram ou qualquer outro
  Connector;
- não importa ``job_engine.py`` nem ``recovery_manager.py``: assim como o
  ``ControlManager`` (PROMPT 15), o ``ShutdownCoordinator`` funciona sozinho.
  O inverso é que integra: ``JobEngine`` pode opcionalmente receber um
  ``shutdown_coordinator`` (ver ``job_engine.py``) para recusar
  reivindicações novas enquanto admissão estiver bloqueada;
- lê e persiste todo o seu estado operacional exclusivamente através de
  ``LocalDatabase`` (SQLite local — chave ``settings`` ``lifecycle:draining``
  e ``audit_events``, NENHUMA migration nova), mais um único arquivo de lock
  de SO dedicado (``lifecycle_instance.lock``, ver "OWNERSHIP" abaixo) que
  não é um dado de negócio — apenas prova de processo vivo.

DRAINING É LIFECYCLE DO SISTEMA, NUNCA UM Job.status
-------------------------------------------------------
Um ``Job`` nunca "está DRAINING": ele continua exatamente no que já era
(PENDING, PROCESSING, PUBLISHING, ...); é o **sistema** que para de
reivindicar trabalho novo. Isso evita duas fontes de verdade conflitantes
sobre o motivo de um Job não avançar e mantém DRAINING consultável de forma
única e centralizada, no mesmo espírito dos flags de
``pause``/``stop_after_current`` do ``ControlManager``.

BLOQUEADOR 1 DA REVISÃO — ADMISSÃO NUNCA REABRE SÓ PORQUE shutdown() RETORNOU
------------------------------------------------------------------------------
A implementação original usava um único campo ``active`` que
``shutdown()`` desligava ao final do próprio método — inclusive quando o
timeout expirava com Jobs ainda ativos. Isso permitia, no MESMO processo,
depois de ``shutdown()`` retornar (drenado ou não), uma nova reivindicação
de Job ser aceita — quebrando a garantia central do Prompt: uma vez que o
processo decidiu fechar, ele não pode voltar a aceitar trabalho novo sozinho.

A correção separa dois conceitos que antes estavam artificialmente
colapsados em um único campo:

- ``admission_blocked`` (bool): é o que ``is_draining()`` lê, e o que
  bloqueia ``JobEngine._claim()``. Uma vez ``True`` (setado por
  ``_begin_draining()``), permanece ``True`` **pelo resto da vida do
  processo**, independentemente de o drain ter completado ou estourado o
  timeout — ``shutdown()`` NUNCA o limpa. Só existem dois jeitos de ele
  voltar a ``False``:
  1. ``abort_shutdown()`` — uma ação EXPLÍCITA chamada pelo mesmo processo,
     permitida somente enquanto nenhum cleanup destrutivo rodou ainda (ver
     BLOQUEADOR do timeout abaixo) — nunca automática;
  2. ``startup_reconcile()`` — chamado por um processo NOVO, e só depois de
     provar (ver OWNERSHIP abaixo) que nenhum processo anterior continua
     vivo segurando este mesmo banco.
- ``drain_status`` (``IN_PROGRESS`` -> ``DRAINED``/``TIMED_OUT`` -> opcional
  ``ABORTED``/``ABANDONED_RESOLVED_AT_STARTUP``): descreve o RESULTADO da
  tentativa de esvaziar pontos seguros, mas nunca por si só controla
  admissão.

Ou seja: ``shutdown()`` retornar (com ou sem timeout) significa "este
processo decidiu fechar e já fez o que pôde/devia" — não "pode voltar a
aceitar trabalho". Ver ``tests/test_shutdown_coordinator.py`` para os três
testes obrigatórios desta correção.

OWNERSHIP: A PROVA DE EXCLUSIVIDADE (BLOQUEADOR 2 DA PRIMEIRA REVISÃO)
------------------------------------------------------------------------------
A implementação original assumia "se encontrei DRAINING ``active=True`` no
início de um processo, o dono morreu" — essa suposição é falsa sem prova de
exclusividade: duas instâncias/processos podem perfeitamente estar vivos ao
mesmo tempo contra o mesmo arquivo SQLite (é exatamente o cenário que todo o
resto do produto — ``ControlManager``, ``JobEngine`` — já trata como normal).
Foi reproduzido: instância A inicia DRAINING e continua viva; instância B
chama ``startup_reconcile()`` e apaga o DRAINING de A, que nunca pediu isso.

A correção introduz um conceito mínimo de **ownership de instância**,
implementado com a MESMA técnica já aprovada e em produção desde o PROMPT 9
(``storage/backup.py::_InterProcessRestoreLock``): um lock de arquivo cuja
exclusão é garantida pelo kernel do SO (``msvcrt`` no Windows, ``fcntl.flock``
em POSIX) e **liberado automaticamente pelo próprio SO quando o processo
morre — de qualquer forma** (saída normal, crash, ``kill -9``, Task Manager,
queda de energia), sem depender de nenhum ``finally``/cleanup Python rodar.
Isso é deliberadamente reimplementado aqui como ``_InstanceOwnershipLock``
(não importado de ``storage/backup.py``, cujo lock é privado e dedicado a
restore) — mesma técnica de independência de módulo já documentada em
``control_manager.py`` — e usa um arquivo de lock PRÓPRIO e dedicado
(``lifecycle_instance.lock``, ao lado do arquivo do banco), nunca
compartilhando o lock de restore.

Por que isso resolve PID reuse/Windows/crash/duas instâncias/processo
reiniciado sem precisar comparar PIDs: a exclusividade não vem de comparar
um número (PID) armazenado — que pode ser reciclado pelo SO para outro
processo completamente diferente depois de um crash — vem de o SO recusar
uma segunda aquisição do MESMO lock enquanto o handle da primeira
aquisição continuar aberto em um processo vivo. Não há "arquivo de lock
travado para sempre": a mera existência do arquivo no disco nunca significa
lock ativo (mesmo comentário já documentado em
``_InterProcessRestoreLock``) — só o handle do SO aberto importa.

DETALHE DE CONTRATO (precisão exigida pela segunda revisão adversarial):
o lock prova apenas que a exclusividade daquele lock ESTÁ DISPONÍVEL neste
exato instante — nunca prova, por si só, "o dono anterior morreu". As duas
afirmações só são equivalentes SOB O CONTRATO de que o processo operacional
retém o lock durante TODA a sua vida operacional relevante (do passo 1 ao
passo 5 abaixo) e nunca o libera enquanto ainda considera este banco seu.
É esse contrato — não o PID, não o conteúdo do arquivo, não nenhuma
heurística — que torna "consegui adquirir o lock" equivalente, na prática,
a "nenhum processo anterior continua vivo segurando este banco". Um
processo que violasse o contrato (ex.: liberasse o lock cedo demais e
continuasse operando) invalidaria essa equivalência; nada neste módulo
pode detectar essa violação por fora — a correção depende do chamador
seguir o contrato de uso descrito abaixo.

Contrato de uso:

1. ``acquire_ownership()`` — DEVE ser chamado uma única vez, cedo na
   inicialização do processo (depois de abrir/migrar o banco, antes de
   ``RecoveryManager.recover_at_startup()``). Levanta
   ``OwnershipAlreadyHeldError`` se outra instância/processo vivo já é dono
   — nesse caso este processo NÃO deve prosseguir como se fosse o dono
   operacional deste banco (ex.: deveria recusar iniciar, ou operar em modo
   somente leitura/diagnóstico — decisão de um bootstrap futuro, fora de
   escopo aqui).
2. ``RecoveryManager.recover_at_startup()``.
3. ``startup_reconcile()`` — EXIGE ``owns_instance is True`` (levanta
   ``OwnershipNotHeldError`` caso contrário) — nunca resolve DRAINING às
   cegas. Como este processo acabou de provar exclusividade no passo 1,
   qualquer DRAINING ``admission_blocked=True`` encontrado aqui É, por
   construção, evidência de um ciclo anterior (nenhum outro processo pode
   estar vivo segurando o mesmo lock ao mesmo tempo). Além disso, ANTES de
   liberar ``admission_blocked``, valida por leitura direta da tabela
   ``jobs`` que nenhum Job continua em ``_RAW_ABANDONED_STATES``
   (PROCESSING/PUBLISHING/INTERRUPTED/UNKNOWN) — ver "BLOQUEADOR 2 DA
   SEGUNDA REVISÃO" abaixo.
4. Operação normal (``JobEngine.run_pending()`` etc.), com o lock retido
   pelo processo inteiro.
5. No fechamento: ``shutdown()`` (drena, cleanup se seguro) e, só então,
   ``release_ownership()`` — ou simplesmente deixar o processo terminar
   (o SO libera o lock de qualquer forma). ``hold_ownership()`` (context
   manager) encapsula os passos 1 e 5 para o caso comum.

Esta integração automática no bootstrap real do produto ainda não existe —
mesma pendência explícita já documentada por ``RecoveryManager`` desde o
PROMPT 14; esta classe entrega apenas o contrato e o comportamento a serem
usados quando essa integração existir.

BLOQUEADOR 1 DA SEGUNDA REVISÃO — MUTADORES DE LIFECYCLE TAMBÉM EXIGEM OWNERSHIP
-----------------------------------------------------------------------------------
A primeira correção de ownership só protegia ``startup_reconcile()``. Foi
reproduzido com dois processos reais que ``abort_shutdown()`` e
``force_cleanup_after_timeout()`` continuavam operando SEM prova de
ownership: processo A adquire o lock e inicia DRAINING, continua vivo;
processo B (cujo ``acquire_ownership()`` corretamente falhou com
``OwnershipAlreadyHeldError``, então ``B.owns_instance is False``) ainda
assim conseguia, através de ``B.abort_shutdown()``, reabrir a admissão do
lifecycle de A — e, através de ``B.force_cleanup_after_timeout()``, rodar
os PRÓPRIOS hooks de B e marcar ``cleanup_attempted=True`` no lifecycle de A.
Isso quebra o próprio conceito de ownership introduzido na primeira
correção: só o dono comprovado pode coordenar o shutdown deste banco.

A correção centraliza a checagem em um único método,
``_require_ownership(operation=...)``, chamado como a PRIMEIRA instrução de
``shutdown()``, ``abort_shutdown()`` e ``force_cleanup_after_timeout()``
(``startup_reconcile()`` já fazia a checagem equivalente inline; agora
reutiliza o mesmo método). Nenhum desses métodos adquire ownership
silenciosamente por conta própria — se chamado sem ``acquire_ownership()``
bem-sucedido antes, falha explicitamente com ``OwnershipNotHeldError``, sem
tocar em nenhum estado persistido. O dono legítimo continua operando
normalmente; um não-dono nunca consegue abortar, forçar cleanup ou
conduzir o shutdown de outro processo.

BLOQUEADOR 2 DA SEGUNDA REVISÃO — startup_reconcile() VALIDA RECOVERY ANTES DE LIBERAR ADMISSÃO
-----------------------------------------------------------------------------------------------------
A segurança de ``startup_reconcile()`` dependia inteiramente da ORDEM de
chamada documentada (ownership -> RecoveryManager -> startup_reconcile),
sem nenhuma validação em tempo de execução. Foi reproduzido: um Job fica
``PROCESSING`` (abandonado por um crash anterior), DRAINING persiste
``admission_blocked=True``; um novo processo adquire ownership mas o
``RecoveryManager`` não roda (ou falha parcialmente) antes de
``startup_reconcile()`` ser chamado mesmo assim — resultado:
``resolved=True``, admissão liberada, e o Job permanece cru em
``PROCESSING`` sem nenhuma evidência de que qualquer recovery de fato
aconteceu.

A correção NÃO acopla ``ShutdownCoordinator`` a ``RecoveryManager`` (nem
importa, nem chama, nem cria uma segunda autoridade de recovery — essa
continua sendo exclusivamente do ``RecoveryManager``). Em vez disso,
``startup_reconcile()`` faz uma leitura pura do SQLite antes de liberar
``admission_blocked``: se qualquer Job estiver em ``_RAW_ABANDONED_STATES``
(PROCESSING, PUBLISHING, INTERRUPTED, UNKNOWN — estados que um recovery
completo SEMPRE transiciona para outro estado), devolve
``StartupDrainingResolution(resolved=False, blocked_reason=...,
raw_abandoned_job_ids=...)`` e preserva o lifecycle intacto para uma nova
tentativa. ``RECOVERING`` é deliberadamente EXCLUÍDO dessa lista: é o
destino legítimo e intencionalmente não resolvido que o próprio
``RecoveryManager`` já atribui a trabalho de origem remota/ambígua — tratar
``RECOVERING`` como bloqueio recriaria exatamente o deadlock que o
``RecoveryManager`` foi desenhado para evitar (ver ``recovery_manager.py``,
``_classify_recovering_origin``). Ou seja: encontrar Jobs em
``RECOVERING`` nunca impede a reconciliação; encontrar Jobs em
PROCESSING/PUBLISHING/INTERRUPTED/UNKNOWN sempre impede.

MAJOR DA SEGUNDA REVISÃO — CLEANUP EXATAMENTE UMA VEZ POR shutdown_id
--------------------------------------------------------------------------
``force_cleanup_after_timeout()`` já verificava ``cleanup_attempted`` antes de
rodar os hooks, mas ``shutdown()`` não: chamar ``shutdown()`` duas vezes
para o MESMO ciclo já drenado (``cleanup_attempted=True`` persistido) reexecutava
os hooks de limpeza registrados, quebrando a idempotência prometida por
``register_cleanup()`` (que exige hooks idempotentes, mas nunca prometeu
proteção contra reexecução do próprio coordinator). A correção faz
``shutdown()`` verificar o ``cleanup_attempted`` persistido para o
``shutdown_id`` atual — tanto antes de esperar pontos seguros quanto de
novo, sob o mesmo lock, logo antes de rodar os hooks (cobrindo também duas
chamadas concorrentes no mesmo processo/instância) — e, se já rodou,
devolve o ``ShutdownReport`` já persistido sem reexecutar nada.

BLOQUEADOR 1 DA TERCEIRA REVISÃO — startup_reconcile() NÃO PODE DESFAZER O PRÓPRIO SHUTDOWN DO PROCESSO
-------------------------------------------------------------------------------------------------------------
O ownership de SO (lock de arquivo) prova EXCLUSIVIDADE ATUAL — mas não
prova, sozinho, que o DRAINING persistido pertence a um PROCESSO ANTERIOR.
Foi reproduzido: o MESMO processo chama ``acquire_ownership()``,
``shutdown()`` (drena, roda cleanup destrutivo, ``admission_blocked=True``)
e, sem nunca ter encerrado, chama ``startup_reconcile()`` — que resolvia
``resolved=True``/``admission_blocked=False``, permitindo ao ``JobEngine``
aceitar Jobs novos no MESMO processo que acabara de fechar (inclusive já
com cleanup destrutivo executado) — burlando a regra já aprovada de que só
``abort_shutdown()`` reabre admissão no mesmo processo, e só antes de
cleanup destrutivo.

A correção introduz uma identidade de SESSÃO OPERACIONAL DO PROCESSO
(``_PROCESS_SESSION_ID``, ver constante no topo do módulo): um UUID gerado
UMA ÚNICA VEZ quando o módulo é importado, constante pelo resto da vida do
processo, e COMPARTILHADO por todo ``ShutdownCoordinator`` criado neste
processo — nunca um UUID por objeto/chamada (ver "IMPORTANTE — IDENTIDADE
DE SESSÃO" abaixo). ``_begin_draining()`` persiste ``owner_session_id`` no
lifecycle. ``startup_reconcile()`` recusa resolver (``resolved=False``,
``belongs_to_current_session=True``, lifecycle intocado) sempre que
``owner_session_id`` do lifecycle ativo for igual à sessão do processo
chamador — mesmo que ``owns_instance`` seja ``True``. Um processo NOVO de
verdade (novo interpretador Python — inclusive via ``subprocess`` com
``sys.executable``, que sempre reimporta o módulo do zero) recebe uma
``_PROCESS_SESSION_ID`` diferente automaticamente, e só então
``startup_reconcile()`` pode prosseguir para a validação de recovery
(Bloqueador 2 da segunda revisão) e, se aprovada, liberar admissão. Isso
nunca usa PID (reciclável pelo SO) como fonte de verdade — é um UUID gerado
em memória, análogo em espírito ao ownership de SO, mas resolvendo uma
pergunta diferente: não "este processo é exclusivo agora?" (isso já é o
lock), e sim "este DRAINING foi criado por ESTE processo ou por outro?".

BLOQUEADOR 2 DA TERCEIRA REVISÃO — TOCTOU ENTRE VERIFICAR OWNERSHIP E MUTAR O LIFECYCLE
-------------------------------------------------------------------------------------------
Foi reproduzido deterministicamente: ``_require_ownership()`` era chamado
ANTES de adquirir ``_shutdown_lock``. Uma thread A passava na verificação,
mas antes de A conseguir escrever o lifecycle (``_begin_draining()``/
mutação), uma thread B no MESMO coordinator/processo chamava
``release_ownership()`` — que liberava o lock de SO imediatamente, sem
esperar A terminar — permitindo a outro processo adquirir ownership
enquanto A ainda escrevia. A verificação e a mutação, feitas em dois passos
não atômicos, deixavam de significar a mesma coisa.

A correção elimina a janela de duas formas combinadas: (1) TODA verificação
de ownership (``_require_ownership()``) agora acontece DENTRO do mesmo
``_shutdown_lock`` que protege a mutação em ``shutdown()``,
``abort_shutdown()``, ``force_cleanup_after_timeout()`` e
``startup_reconcile()`` — verificar e mutar tornam-se uma única seção
crítica atômica, nunca dois passos separados; (2) ``release_ownership()``
agora TAMBÉM adquire o MESMO ``_shutdown_lock`` antes de liberar o lock de
SO — então não pode completar enquanto qualquer uma dessas operações
críticas ainda estiver em andamento nesta instância. Propriedade resultante:
depois que uma operação crítica validou ownership dentro do lock, nenhuma
liberação de ownership (e, por extensão, nenhuma aquisição por outro
processo) pode acontecer até essa operação terminar — porque
``release_ownership()`` bloqueia esperando o mesmo lock. Não há
reentrância: nenhum destes métodos chama outro que também adquira
``_shutdown_lock``, então não há risco de deadlock por reentrância na mesma
thread.

IMPORTANTE — IDENTIDADE DE SESSÃO NÃO É IDENTIDADE DE OBJETO
------------------------------------------------------------------
``instance_id`` (um UUID por OBJETO ``ShutdownCoordinator``, já existente
desde a primeira versão, usado só para diagnóstico) nunca deve ser
confundido com ``process_session_id`` (compartilhado por todo objeto criado
no mesmo processo). Criar um novo objeto ``ShutdownCoordinator`` dentro do
MESMO processo NUNCA representa, por si só, um "processo novo" — se
representasse, bastaria instanciar um objeto novo para burlar o Bloqueador
1 acima. Por padrão (``process_session_id=None`` no construtor),
``process_session_id`` sempre resolve para a MESMA ``_PROCESS_SESSION_ID``
real do processo, não importa quantos objetos ``ShutdownCoordinator`` sejam
criados. O parâmetro ``process_session_id`` explícito do construtor existe
exclusivamente para os testes simularem, de forma barata, múltiplas
sessões operacionais distintas sem precisar de um ``subprocess`` real para
cada cenário — o bootstrap real do produto nunca o usa.

BLOQUEADOR DA QUARTA REVISÃO — FALHA DE CLEANUP NUNCA VIRA SUCESSO EM CHAMADA REPETIDA
-------------------------------------------------------------------------------------------
Foi reproduzido: um hook de cleanup falhava no ``shutdown()`` original
(``report1.cleanup_ok is False``, corretamente); como ``cleanup_attempted``
(então ``cleanup_ran``) já ficava ``True`` persistido, uma segunda chamada
para o MESMO ``shutdown_id`` corretamente não reexecutava o hook — mas o
relatório devolvido (``report2``) vinha com ``cleanup_results=()``, e
``cleanup_ok`` (calculado por ``all()`` sobre uma tupla vazia) virava
``True`` — um falso positivo de sucesso que apagava a evidência real da
falha anterior. O mesmo problema afetava ``force_cleanup_after_timeout()``:
depois de executado (inclusive com falha), ``shutdown()`` chamado de novo
continuava relatando ``requires_forced_action=True`` como se a decisão
forçada nunca tivesse sido tomada, porque ``drain_status`` (histórico,
``TIMED_OUT``) era a única fonte usada para essa pergunta.

A correção separa explicitamente QUATRO perguntas que antes um único bool
(``cleanup_ran``) tentava responder de uma vez (ver docstring de
``ShutdownReport``): (1) ``drain_status`` — resultado histórico do drain,
nunca reescrito por uma tentativa de cleanup; (2) ``cleanup_attempted`` —
uma tentativa real (normal ou forçada) já aconteceu para este
``shutdown_id``?; (3) ``cleanup_ok``/``cleanup_results`` — se tentado,
terminou sem erros ou com erros, persistido de forma DURÁVEL (sobrevive a
fechar/reabrir o ``LocalDatabase``, porque é gravado em
``lifecycle:draining``, o mesmo JSON em ``settings`` de sempre — nenhuma
migration nova); (4) ``forced_action_taken`` — uma decisão forçada já foi
tomada, independente de ``drain_status`` continuar ``TIMED_OUT`` para
sempre como registro histórico.

Nenhuma chamada repetida (``shutdown()`` ou ``force_cleanup_after_timeout()``
idempotentes) reexecuta hooks — isso continua garantido exatamente como nas
revisões anteriores — mas agora, em vez de devolver um relato vazio quando
pula a reexecução, cada uma reconstrói o ``ShutdownReport``/tupla de
resultados a partir do que está persistido em ``cleanup_results`` (uma
lista JSON de ``{name, ok, error_type}`` — ver "SEGURANÇA — TRUNCAR ERRO
NÃO É SANITIZAR SEGREDO" logo abaixo para o formato exato e por que a
mensagem original da exceção nunca é persistida). O resultado é honesto em
qualquer sequência de chamadas e sobrevive a reabrir o processo inteiro
(``LocalDatabase`` novo, mesmo arquivo).

BLOQUEADOR DA QUINTA REVISÃO — SESSÃO NOVA NÃO PODE ASSUMIR LIFECYCLE DE SESSÃO ANTERIOR SEM RECONCILIAR
-----------------------------------------------------------------------------------------------------------
Foi reproduzido com processo real: A adquire ownership, inicia DRAINING
(``owner_session_id=A``) com um Job ainda ``PROCESSING``, e morre
abruptamente (``os._exit``) — o lock de SO é liberado pelo kernel. B
adquire ownership normalmente (nenhum outro processo vivo segura mais o
lock) — mas, SEM rodar ``RecoveryManager``/``startup_reconcile()``,
``B.abort_shutdown()`` reabria admissão de qualquer forma (o Job antigo
continuava cru, ``PROCESSING``, e um Job novo passava a ser reivindicado
normalmente); e ``B.shutdown()`` reutilizava o ``shutdown_id`` de A
(``_begin_draining()`` é idempotente por design) e rodava os PRÓPRIOS hooks
de B sobre um ciclo que B não criou, marcando-o ``DRAINED`` com
``owner_session_id`` continuando, incoerentemente, sendo o de A.

A causa raiz: ownership de SO prova exclusividade ATUAL (nenhum outro
processo vivo com o mesmo lock agora), mas isso é uma pergunta
COMPLETAMENTE DIFERENTE de "o recovery daquele ciclo específico já
aconteceu?" — só ``startup_reconcile()`` (depois de
``RecoveryManager.recover_at_startup()``) tem autoridade para responder a
segunda pergunta, e só ele pode legitimamente apagar/resolver um lifecycle
cujo ``owner_session_id`` não é o da sessão atual.

A correção adiciona uma validação central única,
``_require_not_previous_session_lifecycle()``, chamada logo após
``_require_ownership()`` (mas ainda antes de qualquer mutação) em
``shutdown()`` (dentro de ``_begin_draining()``, no mesmo ``BEGIN
IMMEDIATE`` que decide se reutiliza ou cria um ``shutdown_id`` — atômico
com a própria leitura, sem TOCTOU), ``abort_shutdown()`` e
``force_cleanup_after_timeout()``. Regra: se existe um lifecycle com
``admission_blocked=True`` cujo ``owner_session_id`` é diferente da sessão
atual, nenhuma dessas três operações prossegue — levanta
``PreviousSessionLifecycleRequiresReconcileError`` sem tocar em NADA
(nunca "adquire" a sessão antiga, nunca reescreve ``owner_session_id`` para
esconder o problema, nunca roda um hook desta sessão sobre o
``shutdown_id`` antigo). O único caminho permanece exatamente o já
aprovado: ``RecoveryManager.recover_at_startup()`` seguido de
``startup_reconcile()`` (que já valida ``_RAW_ABANDONED_STATES`` antes de
liberar admissão — ver Bloqueador 2 da segunda revisão). Depois que
``startup_reconcile()`` resolve com sucesso, ``admission_blocked`` volta a
``False`` e a sessão B pode chamar ``shutdown()`` normalmente no futuro —
``_begin_draining()`` então cria um ciclo genuinamente NOVO, com
``shutdown_id``/``owner_session_id`` de B.

SEXTA REVISÃO — error_type NÃO É ALLOWLIST (ERA SÓ O NOME CRU DA CLASSE)
------------------------------------------------------------------------
A correção da quinta revisão parou de persistir ``str(exc)``, o que estava
certo, mas persistia diretamente ``type(exc).__name__`` chamando isso de
"allowlisted". Foi reproduzido: uma classe de exceção criada dinamicamente
(``type("Bearer_SK_SUPER_SECRET_XYZ", (Exception,), {})``) faz
``cleanup_results[0]["error_type"]`` ser literalmente
``"Bearer_SK_SUPER_SECRET_XYZ"`` — o nome da classe é conteúdo tão
arbitrário quanto a mensagem da exceção (quem define/lança a exceção
escolhe o nome livremente, inclusive em runtime), então usá-lo cru não é
allowlist nenhuma, é só outro canal para o mesmo vazamento que a quinta
revisão já tinha fechado para ``str(exc)``.

A correção introduz ``SAFE_ERROR_TYPES``: um conjunto FECHADO e ESTÁTICO de
nomes de exceções conhecidas e inofensivas (vocabulário padrão da stdlib —
``RuntimeError``, ``TimeoutError``, ``OSError``, ``ValueError`` etc.).
``_safe_error_type()`` devolve o nome original SOMENTE se ele pertence a
este conjunto; qualquer outro nome (inclusive um criado dinamicamente com
conteúdo sensível embutido) vira a categoria interna fixa
``_UNRECOGNIZED_ERROR_TYPE`` (``"CLEANUP_HOOK_ERROR"``) — nunca uma versão
"limpa"/redigida do nome original por regex (a mesma fraqueza de tentar
sanitizar ``str(exc)`` por regex se aplicaria a um nome de classe
arbitrário). Esta tradução acontece tanto na serialização
(``_serialize_cleanup_results()``, o que de fato é escrito no SQLite) quanto
na desserialização (``_deserialize_cleanup_results()``, defesa em
profundidade contra um valor gravado por uma versão anterior do código ou
fora do fluxo normal) — em nenhum dos dois pontos um nome de classe fora da
allowlist sobrevive até ``lifecycle:draining``, ``cleanup_results`` ou
``audit_events``. O valor cru de ``type(exc).__name__`` continua existindo
apenas no ``ResourceCleanupResult`` EM MEMÓRIA devolvido pela chamada que
efetivamente rodou o hook agora (mesmo regime já aplicado a ``error`` desde
a quinta revisão) — nunca em nada persistido ou reconstruído.

CONSISTÊNCIA — REASON DE CHAMADA REPETIDA NÃO REESCREVE O MOTIVO HISTÓRICO
----------------------------------------------------------------------------
Foi reproduzido: ``shutdown(reason="ORIGINAL")`` seguido de
``shutdown(reason="CHANGED")`` no MESMO ``shutdown_id`` (ciclo já em
andamento ou já concluído) devolvia ``report2.reason == "CHANGED"``, embora
o lifecycle persistido continuasse ``reason == "ORIGINAL"`` — uma chamada
idempotente reescrevia conceitualmente, no relato devolvido ao chamador, o
motivo histórico do shutdown, mesmo sem tocar o valor persistido.

A correção: ``reason`` de um ``ShutdownReport`` SEMPRE vem do valor
persistido em ``current["reason"]`` (gravado uma única vez, quando
``_begin_draining()`` cria o ciclo) — nunca do argumento ``reason=`` da
chamada específica que devolve o relato. Isso vale para toda reconstrução
via ``_shutdown_report_from_persisted()`` (chamada repetida já com
``cleanup_attempted``, timeout, ou corrida concorrente) E para o relato
construído diretamente pela chamada que de fato executa os hooks agora
(quando esta chamada reaproveita um ``shutdown_id`` já existente, criado por
uma chamada anterior com um ``reason`` diferente — ex.: primeira chamada deu
``TIMED_OUT`` com ``reason="A"``, segunda chamada com ``reason="B"`` desta
vez drena a tempo e roda os hooks: o relato final ainda reporta
``reason="A"``, o motivo original do ciclo). ``_begin_draining()`` em si já
nunca sobrescrevia ``reason`` ao reutilizar um ciclo existente (só o grava
na criação) — o bug estava inteiramente na camada de RELATO, nunca na
persistência.

SEGURANÇA — TRUNCAR MENSAGEM DE ERRO NÃO É SANITIZAR SEGREDO
------------------------------------------------------------------
A correção da quarta revisão truncava ``str(exc)`` em 300 caracteres antes
de persistir — foi reproduzido que uma exceção como
``RuntimeError("Authorization: Bearer sk-...")`` persistia o token inteiro
(300 caracteres é mais que suficiente para a maioria dos segredos comuns).
Truncar limita TAMANHO, não CONTEÚDO — nunca foi sanitização. Hooks de
cleanup são fornecidos pelo chamador e podem envolver qualquer coisa (rede,
subprocesso, arquivo, SDKs de terceiros); suas exceções são, por natureza,
arbitrárias — nenhuma lista de regex de redação garante capturar todo
padrão de segredo existente (e uma nova variante surge a qualquer momento).

A correção abandona qualquer forma de "sanitizar" ``str(exc)`` para
persistência: ``lifecycle:draining.cleanup_results`` (e, por extensão,
``audit_events``, que já não incluía texto de erro) NUNCA mais recebe a
mensagem original da exceção — só informação estrutural ALLOWLISTED: nome
do hook, sucesso/falha, e ``error_type`` (o nome da classe da exceção, ex.
``"RuntimeError"`` — não seu conteúdo). Ao reconstruir um
``ResourceCleanupResult`` a partir do persistido, ``error`` recebe uma
mensagem genérica fixa (``_GENERIC_CLEANUP_FAILURE_SUMMARY``), nunca
derivada da exceção. A mensagem completa de ``str(exc)`` continua
disponível SOMENTE no valor em memória devolvido pela própria chamada que
rodou o hook agora (útil para o operador diagnosticar imediatamente,
naquele processo, sem nada ser escrito em disco) — nunca sobrevive a uma
chamada repetida, a um reinício, nem aparece em qualquer lugar persistido.

CONTRATO DE PONTO SEGURO
----------------------------
``JobEngine.advance()`` é síncrono e não cooperativo — nada aqui interrompe
um handler em execução. "Esperar um ponto seguro" (PROMPT 15/16) significa:
um DRAINING ativo bloqueia toda NOVA reivindicação (``JobEngine._claim()``,
mesma transaction ``BEGIN IMMEDIATE`` de ``is_draining(connection=...)``
compartilhada — nenhum lock em memória é a fonte dessa garantia, ela vem
inteiramente do SQLite, válida entre processos diferentes), mas nunca toca o
``status`` de um Job já reivindicado. ``shutdown()`` apenas espera (poll no
SQLite, nunca em memória) até que nenhum Job permaneça em
``PROCESSING``/``PUBLISHING``/``RECOVERING`` (``_ACTIVE_CLAIM_STATES``,
deliberadamente reescrito aqui em vez de importado de
``job_engine.CLAIMABLE_STATES`` — mesma técnica de independência de módulo)
ou até o timeout. PUBLISHING/RECOVERING/UNKNOWN nunca são tocados por este
módulo — cabe exclusivamente ao ``RecoveryManager`` decidir o destino deles,
exatamente como já faz para um crash comum.

BLOQUEADOR DO TIMEOUT — CLEANUP NUNCA RODA SOBRE RECURSO AINDA EM USO
------------------------------------------------------------------------
A implementação original rodava os hooks de limpeza registrados mesmo
quando ``still_active_job_ids`` não estava vazio — um recurso (ex.: um
subprocesso FFmpeg de um Job ainda ``PROCESSING``) podia ser desmontado
enquanto o Job que o usa continuava ativo. A documentação também sugeria,
incorretamente, que o chamador poderia "cancelar o fechamento e continuar
rodando" depois do timeout — inseguro se cleanup já tivesse rodado.

A correção separa DRAIN REQUEST de DRAINED/TIMED_OUT:

- se todo Job ativo esvaziar dentro do timeout: ``drain_status="DRAINED"``,
  os hooks de limpeza rodam normalmente, um checkpoint best-effort do WAL é
  feito, e ``ShutdownReport.requires_forced_action=False``.
- se o timeout expirar com Jobs ainda ativos: ``drain_status="TIMED_OUT"``,
  **nenhum hook de limpeza roda**, nenhum Job tem seu ``status`` alterado, e
  ``ShutdownReport.requires_forced_action=True`` deixa explícito ao chamador
  que uma decisão ainda é necessária. Duas ações explícitas (nunca
  automáticas) ficam disponíveis a partir daqui:
  - ``abort_shutdown()``: desiste do fechamento, reabre admissão (só
    possível porque nada destrutivo rodou ainda);
  - ``force_cleanup_after_timeout()``: o chamador decide conscientemente
    seguir em frente mesmo com Jobs ativos (ex.: o SO está prestes a matar o
    processo de qualquer jeito) — roda os hooks de limpeza mesmo assim,
    nunca toca ``Job.status`` (o(s) Job(s) ainda ativos ficam exatamente
    como um crash comum os deixaria, e ``RecoveryManager`` os resolve no
    próximo startup), e a partir daí ``abort_shutdown()`` deixa de ser
    permitido (``cleanup_attempted=True`` é persistido e verificado).

RECURSOS/PROCESSOS FILHOS: REGISTRO GENÉRICO, NUNCA ANTECIPANDO O
RESOURCE MANAGER (PROMPT 18)
-----------------------------------------------------------------------
``register_cleanup(name, callback)`` mantém um registro pequeno e
independente de hooks (fechar handle, terminar subprocesso de teste, etc.),
executados em ORDEM REVERSA de registro. Cada hook é isolado: uma exceção em
um NUNCA impede a execução dos demais. Cada hook precisa ser IDEMPOTENTE por
contrato do chamador — ``shutdown()``/``force_cleanup_after_timeout()`` só
rodam os hooks quando ``cleanup_attempted`` ainda não estava marcado, mas dois
CICLOS de shutdown diferentes (ex.: depois de um ``abort_shutdown()``) podem
rodá-los de novo. Não implementa ``ResourceManager`` (PROMPT 18): não há
orçamento de CPU/RAM/GPU/disco, nem conhecimento de FFmpeg/Whisper/
Ollama/browsers — apenas o contrato genérico de registro/ordem/isolamento.

DOIS PEDIDOS SIMULTÂNEOS DE SHUTDOWN
----------------------------------------
Um ``threading.Lock`` de instância serializa execuções de ``shutdown()``
dentro do MESMO processo. Entre processos/instâncias diferentes contra o
mesmo arquivo, a garantia vem do SQLite: ``_begin_draining()`` é idempotente
sob ``BEGIN IMMEDIATE`` — reutiliza o MESMO ``shutdown_id`` enquanto
``admission_blocked`` continuar ``True`` (``IN_PROGRESS`` ou ``TIMED_OUT``,
ainda não resolvido) — nenhum outro coordinator, em nenhum processo, pode
fazer a admissão voltar sozinha nesse meio tempo, porque nenhum caminho
"automático" limpa ``admission_blocked`` (ver Bloqueador 1 acima).

SHUTDOWN GRACIOSO != KILL FORÇADO
--------------------------------------
Este módulo implementa exclusivamente o caminho GRACIOSO. Não tem (nem pode
ter) nenhum efeito sobre um encerramento forçado (Task Manager, ``os._exit``,
``kill -9``, queda de energia) — nesses casos não existe garantia de que
``finally``/cleanup Python execute (inclusive ``release_ownership()``: é por
isso que o lock de ownership é um lock de SO, não uma chamada Python), e a
segurança vem inteiramente do que já estava persistido no SQLite antes do
kill, mais o ``RecoveryManager`` na próxima abertura.

O QUE ESTE MÓDULO DELIBERADAMENTE NÃO FAZ
-----------------------------------------------
Não implementa ``BatchEngine`` (PROMPT 17), ``ResourceManager`` (PROMPT 18),
Circuit Breaker, RetryPolicy, idempotência completa, connectors, nem
qualquer Engine de FFmpeg/Whisper/Ollama/browsers. Não mata thread Python de
forma insegura. Não decide o resultado de uma reconciliação remota. Não cria
nenhuma migration nova. Não integra automaticamente a nenhum bootstrap real
do produto.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

from .domain import Job, JOB_INTERRUPTED, JOB_PROCESSING, JOB_PUBLISHING, JOB_RECOVERING, JOB_UNKNOWN
from .storage import LocalDatabase
from .time_utils import utc_now_iso


LIFECYCLE_DRAINING_KEY = "lifecycle:draining"
INSTANCE_LOCK_FILENAME = "lifecycle_instance.lock"

DEFAULT_DRAIN_TIMEOUT_SECONDS = 30.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.05
DEFAULT_OWNERSHIP_ACQUIRE_TIMEOUT_SECONDS = 0.5

_EVENT_DRAINING_STARTED = "LIFECYCLE_DRAINING_STARTED"
_EVENT_DRAINING_DRAINED = "LIFECYCLE_DRAINING_DRAINED"
_EVENT_DRAINING_TIMED_OUT = "LIFECYCLE_DRAINING_TIMED_OUT"
_EVENT_DRAINING_FORCED_CLEANUP = "LIFECYCLE_DRAINING_FORCED_CLEANUP"
_EVENT_DRAINING_ABORTED = "LIFECYCLE_DRAINING_ABORTED"
_EVENT_DRAINING_ABANDONED_RESOLVED = "LIFECYCLE_DRAINING_ABANDONED_RESOLVED"
_LIFECYCLE_ENTITY_TYPE = "Lifecycle"

STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_DRAINED = "DRAINED"
STATUS_TIMED_OUT = "TIMED_OUT"
STATUS_ABORTED = "ABORTED"
STATUS_ABANDONED_RESOLVED = "ABANDONED_RESOLVED_AT_STARTUP"

# Estados de trabalho ativo/incerto que um shutdown gracioso espera esvaziar
# antes do timeout. Deliberadamente reescrito aqui (não importado de
# ``job_engine.CLAIMABLE_STATES``) para preservar a independência entre os
# dois módulos — mesma técnica documentada em ``control_manager.py``.
_ACTIVE_CLAIM_STATES = frozenset({JOB_PROCESSING, JOB_PUBLISHING, JOB_RECOVERING})

# Estados "crus" que só existem enquanto um Job abandonado ainda não passou
# pelo RecoveryManager (PROMPT 14) — usados exclusivamente por
# ``startup_reconcile()`` como evidência de que a recuperação de Jobs ainda
# não rodou/terminou (ver "BLOQUEADOR 2" na docstring do módulo).
# Deliberadamente NÃO inclui RECOVERING: um Job pode legitimamente
# permanecer em RECOVERING depois de um recovery bem-sucedido (origem
# REMOTA, aguardando um Connector futuro; ou origem AMBÍGUA, que o
# RecoveryManager decide não resolver sozinho) — RECOVERING nunca pode, por
# si só, bloquear o produto para sempre. PROCESSING/PUBLISHING/INTERRUPTED/
# UNKNOWN, ao contrário, são sempre transitórios: um recovery completo
# sempre os transiciona para outro estado (ver ``recovery_manager.py``),
# então encontrar qualquer um deles ainda persistido é prova direta de que
# a recuperação não rodou (ou não terminou) para aquele Job.
_RAW_ABANDONED_STATES = frozenset({JOB_PROCESSING, JOB_PUBLISHING, JOB_INTERRUPTED, JOB_UNKNOWN})

# Identidade da SESSÃO OPERACIONAL deste processo (terceira revisão
# adversarial, "BLOQUEADOR 1"). Gerada UMA ÚNICA VEZ quando este módulo é
# importado pela primeira vez em um processo Python — permanece constante
# pelo resto da vida do processo, e é COMPARTILHADA por todo
# ``ShutdownCoordinator`` criado neste processo (nunca um UUID por objeto:
# ver "IMPORTANTE — IDENTIDADE DE SESSÃO" na docstring do módulo). Um novo
# processo real (novo interpretador Python, inclusive via ``subprocess`` com
# ``sys.executable``) sempre reimporta o módulo do zero e recebe um valor
# novo — nunca baseado em PID (que pode ser reciclado pelo SO).
_PROCESS_SESSION_ID = str(uuid4())


class ShutdownCoordinatorError(RuntimeError):
    """Erro de contrato do ShutdownCoordinator central."""


class OwnershipAlreadyHeldError(ShutdownCoordinatorError):
    """Outra instância/processo vivo já é dono do lifecycle deste banco.

    O lock é de SO (liberado automaticamente quando o dono anterior morre,
    de qualquer forma) — ver docstring do módulo, seção OWNERSHIP.
    """


class OwnershipNotHeldError(ShutdownCoordinatorError):
    """Uma operação que muda o ciclo de shutdown (``shutdown()``,
    ``abort_shutdown()``, ``force_cleanup_after_timeout()``,
    ``startup_reconcile()``) foi chamada sem ``acquire_ownership()``
    bem-sucedido antes nesta instância. Nunca resolve, aborta ou força
    cleanup de um lifecycle sem prova de exclusividade — ver docstring do
    módulo, seção OWNERSHIP e "BLOQUEADOR 1" da segunda revisão."""


class RecoveryIncompleteError(ShutdownCoordinatorError):
    """``startup_reconcile()`` encontrou Job(s) em estado bruto abandonado
    (``_RAW_ABANDONED_STATES``) — evidência direta de que o
    ``RecoveryManager`` ainda não rodou ou não terminou para este banco.
    Não é seguro liberar ``admission_blocked`` nessas condições. Ver
    docstring do módulo, "BLOQUEADOR 2 DA SEGUNDA REVISÃO"."""


class PreviousSessionLifecycleRequiresReconcileError(ShutdownCoordinatorError):
    """``shutdown()``, ``abort_shutdown()`` ou ``force_cleanup_after_timeout()``
    foi chamado enquanto o lifecycle ativo (``admission_blocked=True``)
    pertence a uma SESSÃO OPERACIONAL ANTERIOR (``owner_session_id``
    diferente da sessão atual) — mesmo que esta sessão nova tenha provado
    ownership do lock de SO (ownership de SO prova exclusividade ATUAL,
    nunca por si só que o recovery daquele ciclo já foi feito). O único
    caminho autorizado para resolver ou reabrir esse lifecycle é
    ``startup_reconcile()``, depois de
    ``RecoveryManager.recover_at_startup()`` — ver "BLOQUEADOR DA QUINTA
    REVISÃO" na docstring do módulo."""


class _InstanceOwnershipLock:
    """Lock de SO liberado automaticamente quando o processo morre.

    Mesma técnica já aprovada em ``storage/backup.py::_InterProcessRestoreLock``
    (``msvcrt`` no Windows, ``fcntl.flock`` em POSIX), deliberadamente
    reimplementada aqui — arquivo de lock PRÓPRIO e dedicado, nunca o mesmo
    do restore — para preservar a independência de módulo já documentada
    neste projeto (ver docstring do módulo).
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle = None

    def _try_lock(self, handle) -> bool:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def acquire(self, *, timeout_seconds: float) -> None:
        if self._handle is not None:
            return  # já adquirido por esta instância (idempotente)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        deadline = time.monotonic() + max(float(timeout_seconds), 0.0)
        while True:
            if self._try_lock(handle):
                self._handle = handle
                return
            if time.monotonic() >= deadline:
                handle.close()
                raise OwnershipAlreadyHeldError(
                    f"outra instância/processo vivo já é dona do lifecycle de {self.path.parent}"
                )
            time.sleep(0.02)

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                try:
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


@dataclass(frozen=True)
class ResourceCleanupResult:
    """Resultado isolado de um hook de limpeza registrado.

    ``error`` é preenchido com a mensagem COMPLETA de ``str(exc)`` apenas no
    valor devolvido EM MEMÓRIA por uma execução fresca de
    ``_run_cleanup_hooks()`` (o retorno imediato de ``shutdown()``/
    ``force_cleanup_after_timeout()`` na chamada que de fato rodou os
    hooks) — nunca é isto que chega ao disco. ``error_type`` (nome da
    classe da exceção, ex. ``"RuntimeError"``) é a única informação sobre o
    erro que é PERSISTIDA (ver "SEGURANÇA — TRUNCAR ERRO NÃO É SANITIZAR
    SEGREDO" na docstring do módulo); um resultado reconstruído a partir do
    lifecycle persistido (chamada repetida, ou depois de reabrir o
    ``LocalDatabase``) tem ``error`` preenchido com uma mensagem GENÉRICA e
    fixa, nunca com o texto original da exceção."""

    name: str
    ok: bool
    error: str | None = None
    error_type: str | None = None


# Quinta revisão adversarial — TRUNCAR não é SANITIZAR: uma mensagem de
# exceção arbitrária pode conter um token, senha ou cookie inteiros mesmo
# depois de cortada em 300 caracteres. Hooks de cleanup são fornecidos pelo
# chamador (podem envolver qualquer coisa: rede, subprocesso, arquivo) e
# suas exceções são arbitrárias por natureza — nenhuma regex de redação
# garante capturar todo padrão de segredo. A regra adotada para o que é
# PERSISTIDO em ``lifecycle:draining`` (SQLite) é mais simples e muito mais
# segura: nunca persistir ``str(exc)``. Persistimos só informação
# estrutural allowlisted (nome do hook, sucesso/falha, e o NOME DA CLASSE
# da exceção — nunca seu conteúdo) mais uma mensagem genérica fixa. A
# mensagem completa da exceção continua disponível apenas no valor em
# memória devolvido pela chamada que efetivamente rodou o hook (ver
# docstring de ``ResourceCleanupResult``) — nunca no que é lido de volta do
# lifecycle persistido, nem em ``audit_events`` (que já só registra
# contagens/booleans, nunca texto de erro).
_GENERIC_CLEANUP_FAILURE_SUMMARY = "cleanup hook failed (see application logs for detail)"

# Sexta revisão adversarial — ``type(exc).__name__`` NÃO é, por si só, um
# valor allowlisted: é conteúdo escolhido livremente por quem define a
# classe da exceção (inclusive dinamicamente, via ``type(nome, ...)``), e
# portanto tão arbitrário quanto ``str(exc)`` — só que sem parecer
# arbitrário. Uma classe chamada ``Bearer_SK_SUPER_SECRET_XYZ`` faria o
# "nome seguro" carregar o segredo direto para o SQLite. A allowlist REAL é
# um conjunto FECHADO e ESTÁTICO de nomes de tipos conhecidos e inofensivos
# do próprio vocabulário de exceções da stdlib/domínio — nunca uma tentativa
# de "limpar" ou validar por regex um nome arbitrário (isso teria a mesma
# fraqueza de tentar sanitizar ``str(exc)`` por regex). Qualquer nome de
# classe que não esteja neste conjunto fechado é substituído por
# ``_UNRECOGNIZED_ERROR_TYPE`` — uma categoria interna fixa, nunca derivada
# do valor original.
SAFE_ERROR_TYPES = frozenset(
    {
        "RuntimeError",
        "TimeoutError",
        "OSError",
        "IOError",
        "PermissionError",
        "FileNotFoundError",
        "FileExistsError",
        "ConnectionError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "ConnectionRefusedError",
        "BrokenPipeError",
        "ValueError",
        "TypeError",
        "KeyError",
        "IndexError",
        "AttributeError",
        "NotImplementedError",
        "StopIteration",
        "MemoryError",
        "RecursionError",
        "ArithmeticError",
        "ZeroDivisionError",
        "InterruptedError",
        "ProcessLookupError",
        "ChildProcessError",
        "BlockingIOError",
        "EOFError",
        "LookupError",
        "UnicodeError",
        "UnicodeDecodeError",
        "UnicodeEncodeError",
        "Exception",
    }
)
_UNRECOGNIZED_ERROR_TYPE = "CLEANUP_HOOK_ERROR"


def _safe_error_type(raw_error_type: str | None) -> str:
    """Traduz um nome de classe de exceção ARBITRÁRIO (pode ter sido
    definido dinamicamente pelo autor do hook, inclusive com conteúdo
    sensível embutido no próprio nome) para um valor de uma allowlist REAL
    e fechada. Nunca devolve o valor original quando ele não está
    explicitamente permitido — sem tentativa de sanitização/regex sobre o
    nome, só pertence-ou-não-pertence ao conjunto fixo."""
    if raw_error_type in SAFE_ERROR_TYPES:
        return raw_error_type
    return _UNRECOGNIZED_ERROR_TYPE


def _serialize_cleanup_results(results: tuple["ResourceCleanupResult", ...]) -> list[dict[str, Any]]:
    """Serializa para persistência — deliberadamente NUNCA inclui
    ``result.error`` (a mensagem completa e arbitrária da exceção), e o
    ``error_type`` persistido passa por ``_safe_error_type()`` (allowlist
    fechada) em vez do nome de classe cru — ver "SEXTA REVISÃO -- error_type
    NÃO É ALLOWLIST" na docstring do módulo. Só o nome do hook, o
    sucesso/falha, e o tipo de erro (allowlisted, nunca arbitrário)
    atravessam para o JSON persistido."""
    entries: list[dict[str, Any]] = []
    for result in results:
        entry: dict[str, Any] = {"name": result.name, "ok": bool(result.ok)}
        if not result.ok:
            entry["error_type"] = _safe_error_type(result.error_type)
        entries.append(entry)
    return entries


def _deserialize_cleanup_results(data: Any) -> tuple["ResourceCleanupResult", ...]:
    """Reconstrói a partir do que foi persistido — ``error`` (quando
    presente) é sempre a mensagem genérica fixa, nunca o texto original da
    exceção (que nunca foi persistido em primeiro lugar). ``error_type``
    passa por ``_safe_error_type()`` de novo aqui (defesa em profundidade):
    mesmo que ``lifecycle:draining`` tenha sido escrito por uma versão
    anterior do código ou editado fora do fluxo normal, este ponto de
    leitura nunca devolve um nome de classe fora da allowlist fechada."""
    if not isinstance(data, list):
        return ()
    results: list[ResourceCleanupResult] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        ok = bool(item.get("ok", False))
        error_type = _safe_error_type(item.get("error_type")) if not ok else None
        results.append(
            ResourceCleanupResult(
                name=str(item.get("name", "")),
                ok=ok,
                error=None if ok else _GENERIC_CLEANUP_FAILURE_SUMMARY,
                error_type=error_type,
            )
        )
    return tuple(results)


@dataclass(frozen=True)
class ShutdownReport:
    """Resultado de uma chamada a ``ShutdownCoordinator.shutdown()``.

    ``drained_completely=False``/``requires_forced_action=True`` NUNCA deve
    ser interpretado como falha do shutdown em si — é um relato honesto de
    que nem todo trabalho ativo chegou a um ponto seguro dentro do timeout,
    e que os hooks de limpeza deliberadamente NÃO rodaram (ver docstring do
    módulo). Nenhum ``Job`` listado em ``still_active_job_ids`` teve seu
    ``status`` alterado por este processo. Admissão de Jobs novos permanece
    bloqueada de qualquer forma (drenado ou não) até ``abort_shutdown()``
    (mesmo processo, só antes de cleanup destrutivo) ou
    ``startup_reconcile()`` (processo novo, com ownership provada).

    Quatro perguntas independentes (quarta revisão adversarial — nunca
    colapsadas em um único bool):

    1. ``drain_status``: o RESULTADO do drain em si (``DRAINED``/
       ``TIMED_OUT``/...) — histórico, nunca reescrito por uma tentativa de
       cleanup posterior.
    2. ``cleanup_attempted``: o cleanup chegou a ser tentado (normal OU
       forçado) para este ``shutdown_id``? Uma chamada repetida que NUNCA
       reexecuta hooks ainda assim reflete honestamente se uma tentativa
       ANTERIOR aconteceu.
    3. ``cleanup_ok``/``cleanup_results``: se tentado, terminou sem erros
       ou com erros — sobrevive a chamadas repetidas e a reabrir o
       ``LocalDatabase`` (persistido em ``lifecycle:draining``, nunca
       reconstruído como vazio só porque os hooks não rodaram de novo).
    4. ``requires_forced_action``: uma decisão forçada (``abort_shutdown()``
       ou ``force_cleanup_after_timeout()``) ainda está pendente? Fica
       ``False`` assim que qualquer uma delas for de fato executada — uma
       chamada repetida nunca volta a pedir a mesma decisão como se nada
       tivesse acontecido.
    """

    shutdown_id: str
    reason: str | None
    requested_at: str
    finished_at: str
    drain_status: str
    drained_completely: bool
    requires_forced_action: bool
    still_active_job_ids: tuple[str, ...]
    cleanup_attempted: bool
    cleanup_results: tuple[ResourceCleanupResult, ...]
    already_in_progress: bool

    @property
    def cleanup_ok(self) -> bool | None:
        """``None`` se cleanup nunca foi tentado (nem normal, nem forçado)
        para este ``shutdown_id`` — nunca ``True`` por vacuidade nesse caso,
        para não sugerir falsamente um sucesso que não aconteceu. ``True``/
        ``False`` normalmente, inclusive em relatos de chamadas repetidas
        (lido do persistido, nunca de uma reexecução)."""
        if not self.cleanup_attempted:
            return None
        return all(result.ok for result in self.cleanup_results)


@dataclass(frozen=True)
class StartupDrainingResolution:
    """Resultado de ``ShutdownCoordinator.startup_reconcile()``.

    ``resolved=False`` com ``blocked_reason`` preenchido significa que a
    reconciliação foi RECUSADA — por duas razões possíveis, distinguidas por
    ``belongs_to_current_session``:

    - ``belongs_to_current_session=True``: o DRAINING pertence à sessão
      operacional ATUAL deste processo (nunca perdeu ownership) — ver
      "BLOQUEADOR 1 DA TERCEIRA REVISÃO" na docstring do módulo;
    - ``belongs_to_current_session=False`` (padrão) com ``blocked_reason``
      preenchido: ainda existe evidência de recovery incompleto
      (``raw_abandoned_job_ids`` não vazio) — ver "BLOQUEADOR 2 DA SEGUNDA
      REVISÃO".

    Em ambos os casos ``admission_blocked`` permanece ``True``
    intencionalmente, e o lifecycle persistido não é alterado (para uma
    nova tentativa/diagnóstico). ``resolved=False`` sem ``blocked_reason``
    significa apenas que não havia DRAINING ativo para resolver (no-op
    comum).
    """

    resolved: bool
    shutdown_id: str | None = None
    previous_status: str | None = None
    blocked_reason: str | None = None
    raw_abandoned_job_ids: tuple[str, ...] = ()
    belongs_to_current_session: bool = False


class ShutdownCoordinator:
    """Fechamento seguro do núcleo operacional (PROMPT 16).

    Ver a docstring do módulo para o contrato completo de DRAINING,
    ownership, ponto seguro, timeout e reconciliação na inicialização.
    """

    def __init__(
        self,
        database: LocalDatabase,
        *,
        drain_timeout_seconds: float = DEFAULT_DRAIN_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        ownership_lock_path: Path | str | None = None,
        process_session_id: str | None = None,
    ) -> None:
        self.database = database
        self.drain_timeout_seconds = float(drain_timeout_seconds)
        if self.drain_timeout_seconds < 0:
            raise ValueError("drain_timeout_seconds deve ser >= 0")
        self.poll_interval_seconds = float(poll_interval_seconds)
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds deve ser > 0")
        lock_path = (
            Path(ownership_lock_path)
            if ownership_lock_path is not None
            else self.database.path.parent / INSTANCE_LOCK_FILENAME
        )
        self._ownership_lock = _InstanceOwnershipLock(lock_path)
        self._owns_instance = False
        self._instance_id = str(uuid4())
        # ``process_session_id`` é deliberadamente um parâmetro de
        # composição, não usado pelo bootstrap real do produto (que sempre
        # usa o padrão ``None`` -> identidade real e única do processo,
        # ``_PROCESS_SESSION_ID``). Existe para que os TESTES possam simular
        # de forma barata múltiplas sessões operacionais distintas dentro do
        # mesmo processo de teste, sem precisar de um ``subprocess`` real
        # para cada cenário — as garantias fim-a-fim continuam cobertas
        # pelos testes de processo real (ver docstring do módulo,
        # "BLOQUEADOR 1 DA TERCEIRA REVISÃO").
        self._process_session_id = process_session_id if process_session_id is not None else _PROCESS_SESSION_ID
        self._cleanup_hooks: list[tuple[str, Callable[[], None]]] = []
        self._shutdown_lock = threading.Lock()

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def process_session_id(self) -> str:
        """Identidade da sessão operacional deste processo — ver
        "BLOQUEADOR 1 DA TERCEIRA REVISÃO" na docstring do módulo. Diferente
        de ``instance_id``: é compartilhada por todo ``ShutdownCoordinator``
        criado neste mesmo processo (a menos que sobrescrita explicitamente
        para fins de teste via ``process_session_id=`` no construtor)."""
        return self._process_session_id

    @property
    def owns_instance(self) -> bool:
        return self._owns_instance

    # -- Ownership (prova de processo vivo) ----------------------------------

    def acquire_ownership(self, *, timeout_seconds: float = DEFAULT_OWNERSHIP_ACQUIRE_TIMEOUT_SECONDS) -> None:
        """Adquire o lock de SO exclusivo deste banco. Idempotente dentro da
        mesma instância. Levanta ``OwnershipAlreadyHeldError`` se outro
        processo vivo já é dono — ver docstring do módulo, seção OWNERSHIP,
        para o contrato completo de quando isto deve ser chamado."""
        self._ownership_lock.acquire(timeout_seconds=timeout_seconds)
        self._owns_instance = True

    def release_ownership(self) -> None:
        """Libera o lock de ownership. Idempotente. Nunca obrigatório
        chamar explicitamente — o SO libera automaticamente quando o
        processo morre, de qualquer forma (ver docstring do módulo).

        Serializado pelo MESMO lock de instância (``_shutdown_lock``) usado
        pelas operações críticas de lifecycle (``shutdown()``,
        ``abort_shutdown()``, ``force_cleanup_after_timeout()``,
        ``startup_reconcile()``): não pode liberar o lock de SO enquanto uma
        dessas operações ainda está em andamento nesta instância — ver
        "BLOQUEADOR 2 DA TERCEIRA REVISÃO" na docstring do módulo. Se uma
        operação crítica está em andamento, esta chamada BLOQUEIA até ela
        terminar antes de liberar."""
        with self._shutdown_lock:
            self._ownership_lock.release()
            self._owns_instance = False

    @contextmanager
    def hold_ownership(self, *, timeout_seconds: float = DEFAULT_OWNERSHIP_ACQUIRE_TIMEOUT_SECONDS) -> Iterator["ShutdownCoordinator"]:
        """Context manager de conveniência: adquire ownership, devolve
        ``self``, e libera ao sair (inclusive em exceção)."""
        self.acquire_ownership(timeout_seconds=timeout_seconds)
        try:
            yield self
        finally:
            self.release_ownership()

    def _require_ownership(self, *, operation: str) -> None:
        """Validação central única (segunda revisão adversarial,
        "BLOQUEADOR 1"): TODA operação que muda o ciclo de shutdown deste
        banco — ``shutdown()``, ``abort_shutdown()``,
        ``force_cleanup_after_timeout()``, ``startup_reconcile()`` — chama
        isto como primeira instrução, e nada mais. Nunca adquire ownership
        silenciosamente aqui (isso esconderia um bootstrap incorreto do
        chamador) — apenas verifica o que já deveria ter sido provado por
        ``acquire_ownership()`` antes. Levanta ``OwnershipNotHeldError`` se
        esta instância não é a dona comprovada do lifecycle."""
        if not self._owns_instance:
            raise OwnershipNotHeldError(
                f"{operation}() exige acquire_ownership() bem-sucedido antes nesta instância "
                "— nenhum processo sem ownership pode abortar, forçar cleanup ou conduzir o "
                "shutdown de outro dono (ver docstring do módulo, seção OWNERSHIP)"
            )

    def _require_not_previous_session_lifecycle(
        self, current: dict[str, Any] | None, *, operation: str
    ) -> None:
        """Validação central única (quinta revisão adversarial,
        "BLOQUEADOR"): TODA operação que MUTA um lifecycle já existente —
        ``shutdown()`` (via ``_begin_draining()``), ``abort_shutdown()``,
        ``force_cleanup_after_timeout()`` — chama isto logo após confirmar
        ownership, e antes de tocar em qualquer coisa. Ownership de SO
        prova exclusividade ATUAL (nenhum outro processo vivo segura o
        lock), mas NUNCA prova, por si só, que o recovery daquele ciclo já
        aconteceu — essa é uma pergunta inteiramente diferente, e a única
        resposta autorizada para ela é ``startup_reconcile()`` (depois de
        ``RecoveryManager.recover_at_startup()``). Se o lifecycle ativo
        pertence a uma sessão diferente da atual, levanta
        ``PreviousSessionLifecycleRequiresReconcileError`` sem tocar em
        nada — nunca adquire a sessão antiga, nunca reescreve
        ``owner_session_id`` para esconder o problema, nunca roda hooks da
        sessão nova sobre o ``shutdown_id`` antigo. Ver "BLOQUEADOR DA
        QUINTA REVISÃO" na docstring do módulo."""
        if not isinstance(current, dict) or not current.get("admission_blocked"):
            return
        owner_session_id = current.get("owner_session_id")
        if owner_session_id is not None and owner_session_id != self._process_session_id:
            raise PreviousSessionLifecycleRequiresReconcileError(
                f"{operation}() recusado: o lifecycle ativo (shutdown_id="
                f"{current.get('shutdown_id')!r}) pertence a uma sessão operacional ANTERIOR "
                "(owner_session_id diferente da sessão atual) — só startup_reconcile(), "
                "depois de RecoveryManager.recover_at_startup(), pode resolver ou reabrir "
                "esse ciclo; esta sessão nunca assume, aborta, completa ou executa cleanup "
                "de um shutdown_id que não criou."
            )

    # -- Consulta de DRAINING ---------------------------------------------

    def is_draining(self, *, connection: sqlite3.Connection | None = None) -> bool:
        """``True`` se admissão de Jobs novos está bloqueada por um
        fechamento gracioso — permanece ``True`` mesmo depois de
        ``shutdown()`` retornar (ver Bloqueador 1 na docstring do módulo).

        ``connection``: quando informada, permite ao ``JobEngine`` compor
        esta leitura sob o MESMO ``BEGIN IMMEDIATE`` da própria
        reivindicação (ver docstring do módulo e ``job_engine.py``).
        """
        value = self.database.get_setting(LIFECYCLE_DRAINING_KEY, connection=connection)
        return bool(isinstance(value, dict) and value.get("admission_blocked"))

    def get_draining_state(self) -> dict[str, Any] | None:
        """Consulta o registro persistido de DRAINING mais recente, ou
        ``None`` se nenhum shutdown foi pedido ainda nesta instalação."""
        value = self.database.get_setting(LIFECYCLE_DRAINING_KEY)
        return dict(value) if isinstance(value, dict) else None

    # -- Registro de recursos/cleanup --------------------------------------

    def register_cleanup(self, name: str, callback: Callable[[], None]) -> None:
        """Registra um hook de limpeza idempotente, executado em ordem
        REVERSA de registro (ver docstring do módulo). Registrar de novo
        com o mesmo ``name`` substitui o hook anterior (mantendo sua
        posição original de registro)."""
        if not callable(callback):
            raise TypeError("callback de cleanup deve ser chamável")
        name = str(name).strip()
        if not name:
            raise ValueError("name do cleanup não pode ser vazio")
        self._cleanup_hooks = [(n, c) for n, c in self._cleanup_hooks if n != name]
        self._cleanup_hooks.append((name, callback))

    def unregister_cleanup(self, name: str) -> None:
        """Remove um hook de limpeza registrado, se existir. Nunca levanta
        erro se ``name`` não estiver registrado (idempotente)."""
        name = str(name).strip()
        self._cleanup_hooks = [(n, c) for n, c in self._cleanup_hooks if n != name]

    def _run_cleanup_hooks(self) -> tuple[ResourceCleanupResult, ...]:
        """Roda os hooks registrados (ordem reversa, isolados). O ``error``
        aqui é a mensagem COMPLETA de ``str(exc)`` — válida apenas em
        memória, para esta chamada; ver docstring de ``ResourceCleanupResult``
        e "SEGURANÇA — TRUNCAR ERRO NÃO É SANITIZAR SEGREDO": o que
        eventualmente é PERSISTIDO (``_serialize_cleanup_results()``) nunca
        inclui esta mensagem, só ``error_type`` (nome da classe)."""
        results: list[ResourceCleanupResult] = []
        for name, callback in reversed(self._cleanup_hooks):
            try:
                callback()
                results.append(ResourceCleanupResult(name=name, ok=True, error=None))
            except Exception as exc:  # noqa: BLE001 - isola falha de UM recurso dos demais
                results.append(
                    ResourceCleanupResult(name=name, ok=False, error=str(exc), error_type=type(exc).__name__)
                )
        return tuple(results)

    # -- Início/registro de DRAINING (persistência crash-safe) --------------

    def _begin_draining(self, *, reason: str | None) -> tuple[str, bool, str]:
        """Ativa ``admission_blocked`` atomicamente. Retorna
        ``(shutdown_id, already_in_progress, requested_at)``.

        Idempotente: enquanto ``admission_blocked`` continuar ``True``
        (``IN_PROGRESS`` ou ``TIMED_OUT`` ainda não resolvido), reutiliza o
        MESMO ``shutdown_id`` em vez de criar um ciclo novo — ver Bloqueador
        1 na docstring do módulo. Um novo ciclo só nasce depois de
        ``abort_shutdown()`` (admissão reaberta explicitamente) ou de nunca
        ter havido um shutdown anterior.
        """
        with self.database.transaction() as conn:
            existing = self.database.get_setting(LIFECYCLE_DRAINING_KEY, connection=conn)
            if isinstance(existing, dict) and existing.get("admission_blocked"):
                self._require_not_previous_session_lifecycle(existing, operation="shutdown")
                return str(existing.get("shutdown_id")), True, str(existing.get("requested_at"))

            shutdown_id = str(uuid4())
            requested_at = utc_now_iso()
            self.database.set_setting(
                LIFECYCLE_DRAINING_KEY,
                {
                    "admission_blocked": True,
                    "drain_status": STATUS_IN_PROGRESS,
                    "cleanup_attempted": False,
                    "cleanup_ok": None,
                    "cleanup_results": [],
                    "forced_action_taken": False,
                    "shutdown_id": shutdown_id,
                    "owner_session_id": self._process_session_id,
                    "reason": reason,
                    "requested_at": requested_at,
                    "drain_completed_at": None,
                    "still_active_job_ids": [],
                },
                connection=conn,
            )
            self.database.append_audit_event(
                _EVENT_DRAINING_STARTED,
                entity_type=_LIFECYCLE_ENTITY_TYPE,
                data={"shutdown_id": shutdown_id, "reason": reason},
                connection=conn,
            )
            return shutdown_id, False, requested_at

    def _record_drain_outcome(
        self,
        shutdown_id: str,
        *,
        drain_status: str,
        still_active_job_ids: tuple[str, ...],
        cleanup_attempt: tuple[ResourceCleanupResult, ...] | None = None,
        forced_action_taken: bool = False,
        event: str,
    ) -> None:
        """Persiste o resultado de uma etapa do ciclo de shutdown.

        ``cleanup_attempt``: ``None`` quando NENHUMA tentativa de cleanup
        aconteceu nesta chamada (ex.: timeout) — nesse caso
        ``cleanup_attempted``/``cleanup_ok``/``cleanup_results`` já
        persistidos são preservados EXATAMENTE como estavam (nunca
        apagados/zerados por uma chamada que não tentou nada — ver "BLOQUEADOR
        — FALHA DE CLEANUP VIRA SUCESSO" na docstring do módulo). Quando uma
        tupla (mesmo vazia, se não havia hooks registrados) é passada, uma
        tentativa REAL aconteceu agora: grava ``cleanup_attempted=True``,
        ``cleanup_ok`` e ``cleanup_results`` (sanitizados) a partir dela —
        substituindo o que estava antes, porque é exatamente isso que essa
        chamada específica acabou de medir.

        ``forced_action_taken``: quando ``True``, marca permanentemente que
        uma decisão forçada (``force_cleanup_after_timeout()``) já foi
        tomada para este ``shutdown_id`` — nunca revertido por uma chamada
        posterior.
        """
        with self.database.transaction() as conn:
            current = self.database.get_setting(LIFECYCLE_DRAINING_KEY, connection=conn)
            if not isinstance(current, dict) or current.get("shutdown_id") != shutdown_id:
                # Outro ciclo já assumiu o registro; inofensivo não sobrescrever.
                return
            updated = dict(current)
            updated.update(
                {
                    "drain_status": drain_status,
                    "drain_completed_at": utc_now_iso(),
                    "still_active_job_ids": list(still_active_job_ids),
                }
            )
            if cleanup_attempt is not None:
                updated["cleanup_attempted"] = True
                updated["cleanup_ok"] = all(result.ok for result in cleanup_attempt)
                updated["cleanup_results"] = _serialize_cleanup_results(cleanup_attempt)
            if forced_action_taken:
                updated["forced_action_taken"] = True
            # ``admission_blocked`` deliberadamente NUNCA é tocado aqui —
            # ver Bloqueador 1 na docstring do módulo.
            self.database.set_setting(LIFECYCLE_DRAINING_KEY, updated, connection=conn)
            self.database.append_audit_event(
                event,
                entity_type=_LIFECYCLE_ENTITY_TYPE,
                data={
                    "shutdown_id": shutdown_id,
                    "drain_status": drain_status,
                    "still_active_count": len(still_active_job_ids),
                    "cleanup_attempted": updated.get("cleanup_attempted", False),
                    "cleanup_ok": updated.get("cleanup_ok"),
                },
                connection=conn,
            )

    @staticmethod
    def _requires_forced_action(current: dict[str, Any]) -> bool:
        """``True`` somente enquanto o drain terminou em ``TIMED_OUT`` E
        nenhuma decisão forçada (``force_cleanup_after_timeout()``) já foi
        tomada para este ciclo. Calculado a partir de DOIS campos
        independentes (nunca de um único bool) — ver "MAJOR — FORCED
        CLEANUP JÁ EXECUTADO" na docstring do módulo: depois que
        ``force_cleanup_after_timeout()`` roda, ``drain_status`` permanece
        ``TIMED_OUT`` (resultado histórico do drain), mas
        ``forced_action_taken`` passa a ``True`` e esta função passa a
        devolver ``False`` — uma chamada repetida nunca volta a pedir a
        mesma decisão como se nada tivesse acontecido."""
        return current.get("drain_status") == STATUS_TIMED_OUT and not current.get("forced_action_taken", False)

    def _shutdown_report_from_persisted(
        self,
        *,
        shutdown_id: str,
        requested_at: str,
        current: dict[str, Any],
        already_in_progress: bool,
    ) -> ShutdownReport:
        """Reconstrói um ``ShutdownReport`` honesto a partir do lifecycle
        JÁ PERSISTIDO — usado por toda chamada que não reexecuta hooks
        (idempotência), para que o relato nunca "esqueça" uma falha de
        cleanup anterior nem reafirme uma decisão forçada já tomada.

        Sexta revisão adversarial — CONSISTÊNCIA DO REASON: ``reason``
        SEMPRE vem de ``current["reason"]`` (o motivo persistido quando o
        ``shutdown_id`` foi originalmente criado por ``_begin_draining()``),
        nunca de um parâmetro separado. Uma chamada repetida do MESMO ciclo
        com um ``reason=`` diferente no argumento da chamada não pode
        reescrever conceitualmente o motivo histórico do shutdown — ver
        "CONSISTÊNCIA — REASON DE CHAMADA REPETIDA" na docstring do
        módulo."""
        drain_status = str(current.get("drain_status", STATUS_DRAINED))
        return ShutdownReport(
            shutdown_id=shutdown_id,
            reason=current.get("reason"),
            requested_at=requested_at,
            finished_at=str(current.get("drain_completed_at") or utc_now_iso()),
            drain_status=drain_status,
            drained_completely=(drain_status == STATUS_DRAINED),
            requires_forced_action=self._requires_forced_action(current),
            still_active_job_ids=tuple(current.get("still_active_job_ids") or ()),
            cleanup_attempted=bool(current.get("cleanup_attempted", False)),
            cleanup_results=_deserialize_cleanup_results(current.get("cleanup_results")),
            already_in_progress=already_in_progress,
        )

    # -- Espera por ponto seguro ---------------------------------------------

    def _wait_for_safe_points(self, *, deadline: float) -> tuple[str, ...]:
        """Faz polling no SQLite (nunca em memória) até que nenhum Job
        permaneça em um estado de trabalho ativo/incerto, ou até
        ``deadline`` (``time.monotonic()``) ser alcançado."""
        while True:
            active = tuple(
                sorted(job.id for job in self.database.list(Job) if job.status in _ACTIVE_CLAIM_STATES)
            )
            if not active:
                return ()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return active
            time.sleep(min(self.poll_interval_seconds, remaining))

    def _checkpoint_database(self) -> None:
        """Passo best-effort: dobra o WAL de volta ao arquivo principal.
        Nunca é condição de correção; falha aqui é silenciosamente ignorada.
        Só é chamado quando o drain foi seguro (nunca sobre timeout, para
        manter simétrica a regra de "nada acontece sobre trabalho ainda
        ativo" — ver docstring do módulo)."""
        try:
            with self.database.connection() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.DatabaseError:
            pass

    # -- API principal --------------------------------------------------------

    def shutdown(self, *, reason: str | None = None, drain_timeout_seconds: float | None = None) -> ShutdownReport:
        """Executa o fechamento gracioso:

        1. ativa ``admission_blocked`` atomicamente (permanece ativo pelo
           resto da vida do processo — ver Bloqueador 1);
        2. espera (poll no SQLite) até que nenhum Job permaneça em
           PROCESSING/PUBLISHING/RECOVERING, ou até o timeout;
        3. se drenou a tempo: roda os hooks de limpeza (ordem reversa,
           isolados), faz um checkpoint best-effort do WAL, marca
           ``DRAINED``;
        4. se o timeout expirar: NENHUM hook de limpeza roda, NENHUM Job é
           tocado, marca ``TIMED_OUT`` e ``requires_forced_action=True`` —
           ver ``abort_shutdown()``/``force_cleanup_after_timeout()``.

        Nunca levanta exceção por causa de Jobs que não drenaram a tempo,
        nem por falha de um hook de limpeza isolado — ambos são relatados em
        ``ShutdownReport``, nunca escondidos. Idempotente/seguro para
        chamadas concorrentes (mesmo processo: serializado por um lock de
        instância; processos diferentes: serializado pelo SQLite — ver
        docstring do módulo). Cleanup roda EXATAMENTE UMA VEZ por
        ``shutdown_id``: uma chamada repetida (mesmo ciclo já drenado, com
        ``cleanup_attempted=True`` persistido) NUNCA reexecuta os hooks — devolve
        o resultado já persistido em vez de inventar um novo ciclo (ver
        "MAJOR — CLEANUP DO MESMO CICLO" na docstring do módulo).

        Levanta ``OwnershipNotHeldError`` se esta instância não é a dona
        comprovada do lifecycle (``acquire_ownership()`` não foi chamado
        com sucesso antes) — nunca conduz ou modifica o shutdown de outro
        dono (ver "BLOQUEADOR 1 DA SEGUNDA REVISÃO"). A verificação de
        ownership é feita DENTRO do mesmo ``_shutdown_lock`` que protege a
        própria mutação (nunca antes de adquiri-lo) — ver "BLOQUEADOR 2 DA
        TERCEIRA REVISÃO" na docstring do módulo: sem isso, outra thread
        poderia liberar ownership entre a checagem e a escrita.

        Levanta ``PreviousSessionLifecycleRequiresReconcileError`` se já
        existe um lifecycle ativo (``admission_blocked=True``) criado por
        uma SESSÃO OPERACIONAL ANTERIOR (``owner_session_id`` diferente
        desta) — mesmo que esta sessão nova prove ownership do lock de SO.
        Nunca assume/reutiliza o ``shutdown_id`` de outra sessão; o único
        caminho autorizado para aquele lifecycle é ``startup_reconcile()``,
        depois de ``RecoveryManager.recover_at_startup()`` (ver "BLOQUEADOR
        DA QUINTA REVISÃO" na docstring do módulo).
        """
        timeout = self.drain_timeout_seconds if drain_timeout_seconds is None else float(drain_timeout_seconds)
        if timeout < 0:
            raise ValueError("drain_timeout_seconds deve ser >= 0")

        with self._shutdown_lock:
            self._require_ownership(operation="shutdown")
            shutdown_id, already_in_progress, requested_at = self._begin_draining(reason=reason)

            if already_in_progress:
                current = self.get_draining_state() or {}
                if current.get("shutdown_id") == shutdown_id and current.get("cleanup_attempted"):
                    # Mesmo shutdown_id já teve uma tentativa de cleanup
                    # (normal ou forçada) em uma chamada anterior — nunca
                    # reexecuta hooks: devolve o resultado JÁ PERSISTIDO
                    # (inclusive uma falha anterior, nunca mascarada como
                    # sucesso por um relato vazio).
                    return self._shutdown_report_from_persisted(
                        shutdown_id=shutdown_id,
                        requested_at=requested_at,
                        current=current,
                        already_in_progress=True,
                    )

            deadline = time.monotonic() + timeout
            still_active = self._wait_for_safe_points(deadline=deadline)

            if still_active:
                self._record_drain_outcome(
                    shutdown_id,
                    drain_status=STATUS_TIMED_OUT,
                    still_active_job_ids=still_active,
                    cleanup_attempt=None,  # nenhuma tentativa aconteceu -- nada a preservar/sobrescrever
                    event=_EVENT_DRAINING_TIMED_OUT,
                )
                current = self.get_draining_state() or {}
                return self._shutdown_report_from_persisted(
                    shutdown_id=shutdown_id,
                    requested_at=requested_at,
                    current=current,
                    already_in_progress=already_in_progress,
                )

            # Checagem final, dentro do mesmo _shutdown_lock, contra uma
            # segunda chamada concorrente no MESMO processo que já rodou o
            # cleanup deste shutdown_id enquanto esta chamada esperava por
            # pontos seguros — garante exatamente-uma-vez também sob
            # concorrência (não apenas em chamadas sequenciais).
            current = self.get_draining_state() or {}
            if current.get("shutdown_id") == shutdown_id and current.get("cleanup_attempted"):
                return self._shutdown_report_from_persisted(
                    shutdown_id=shutdown_id,
                    requested_at=requested_at,
                    current=current,
                    already_in_progress=True,
                )

            # ``reason`` do relato final vem do que foi PERSISTIDO quando o
            # ciclo nasceu (``current["reason"]``, gravado por
            # ``_begin_draining()``), nunca do argumento ``reason=`` desta
            # chamada especifica -- mesmo quando esta chamada e quem de fato
            # executa os hooks (ciclo reaproveitado apos timeout, por
            # exemplo). Ver "CONSISTENCIA -- REASON DE CHAMADA REPETIDA".
            persisted_reason = current.get("reason", reason)

            cleanup_results = self._run_cleanup_hooks()
            self._checkpoint_database()
            self._record_drain_outcome(
                shutdown_id,
                drain_status=STATUS_DRAINED,
                still_active_job_ids=(),
                cleanup_attempt=cleanup_results,
                event=_EVENT_DRAINING_DRAINED,
            )
            return ShutdownReport(
                shutdown_id=shutdown_id,
                reason=persisted_reason,
                requested_at=requested_at,
                finished_at=utc_now_iso(),
                drain_status=STATUS_DRAINED,
                drained_completely=True,
                requires_forced_action=False,
                still_active_job_ids=(),
                cleanup_attempted=True,
                cleanup_results=cleanup_results,
                already_in_progress=already_in_progress,
            )

    def force_cleanup_after_timeout(self) -> tuple[ResourceCleanupResult, ...]:
        """Ação EXPLÍCITA (nunca automática) para depois de um ``shutdown()``
        que retornou ``requires_forced_action=True``: o chamador decide
        conscientemente rodar os hooks de limpeza mesmo com Jobs ainda
        ativos (ex.: o processo hospedeiro vai morrer de qualquer forma).
        NUNCA toca ``Job.status`` — Jobs ainda ativos ficam exatamente como
        um crash comum os deixaria, e o ``RecoveryManager`` os resolve no
        próximo startup. Idempotente: se cleanup já foi TENTADO (normal ou
        forçado) para o ciclo atual, é um no-op que devolve os resultados
        JÁ PERSISTIDOS daquela tentativa (nunca reexecuta hooks, nunca
        devolve ``()`` só porque não rodou de novo — isso apagaria a
        evidência de uma falha anterior, ver "BLOQUEADOR — FALHA DE
        CLEANUP VIRA SUCESSO" na docstring do módulo). Levanta
        ``ShutdownCoordinatorError`` se não houver DRAINING ativo.

        Levanta ``OwnershipNotHeldError`` se esta instância não é a dona
        comprovada do lifecycle — nunca força cleanup do ciclo de outro
        dono (ver "BLOQUEADOR 1 DA SEGUNDA REVISÃO"). Verificação de
        ownership feita DENTRO do ``_shutdown_lock`` (ver "BLOQUEADOR 2 DA
        TERCEIRA REVISÃO"). Levanta
        ``PreviousSessionLifecycleRequiresReconcileError`` se o lifecycle
        ativo pertence a uma sessão operacional ANTERIOR — nunca roda os
        hooks desta sessão sobre o ciclo de outra (ver "BLOQUEADOR DA
        QUINTA REVISÃO")."""
        with self._shutdown_lock:
            self._require_ownership(operation="force_cleanup_after_timeout")
            current = self.get_draining_state()
            if not current or not current.get("admission_blocked"):
                raise ShutdownCoordinatorError("nenhum DRAINING ativo para forçar cleanup")
            self._require_not_previous_session_lifecycle(current, operation="force_cleanup_after_timeout")
            if current.get("cleanup_attempted"):
                # Já tentado antes (normal ou forçado) -- nunca reexecuta;
                # devolve o resultado JÁ PERSISTIDO daquela tentativa.
                return _deserialize_cleanup_results(current.get("cleanup_results"))
            shutdown_id = str(current["shutdown_id"])
            still_active = tuple(current.get("still_active_job_ids") or ())
            cleanup_results = self._run_cleanup_hooks()
            self._checkpoint_database()
            self._record_drain_outcome(
                shutdown_id,
                drain_status=current.get("drain_status", STATUS_TIMED_OUT),
                still_active_job_ids=still_active,
                cleanup_attempt=cleanup_results,
                forced_action_taken=True,
                event=_EVENT_DRAINING_FORCED_CLEANUP,
            )
            return cleanup_results

    def abort_shutdown(self, *, reason: str | None = None) -> bool:
        """Única ação capaz de reabrir admissão de Jobs no MESMO processo
        (ver Bloqueador 1). Só permitida enquanto nenhum cleanup destrutivo
        foi tentado ainda para o ciclo ativo (``cleanup_attempted=False``) —
        depois de ``DRAINED`` ou de ``force_cleanup_after_timeout()``, reabrir seria
        inseguro (recursos podem já ter sido fechados) e este método se
        recusa, devolvendo ``False``. Também devolve ``False`` se não havia
        nenhum DRAINING ativo para abortar. Devolve ``True`` quando de fato
        reabriu admissão.

        Levanta ``OwnershipNotHeldError`` se esta instância não é a dona
        comprovada do lifecycle — nunca aborta o shutdown de outro dono
        vivo (ver "BLOQUEADOR 1 DA SEGUNDA REVISÃO"). Verificação de
        ownership feita DENTRO do ``_shutdown_lock`` (ver "BLOQUEADOR 2 DA
        TERCEIRA REVISÃO"). Levanta
        ``PreviousSessionLifecycleRequiresReconcileError`` se o lifecycle
        ativo pertence a uma sessão operacional ANTERIOR — nunca aborta um
        ciclo que esta sessão não criou (ver "BLOQUEADOR DA QUINTA
        REVISÃO")."""
        with self._shutdown_lock:
            self._require_ownership(operation="abort_shutdown")
            with self.database.transaction() as conn:
                current = self.database.get_setting(LIFECYCLE_DRAINING_KEY, connection=conn)
                if not isinstance(current, dict) or not current.get("admission_blocked"):
                    return False
                self._require_not_previous_session_lifecycle(current, operation="abort_shutdown")
                if current.get("cleanup_attempted"):
                    return False
                shutdown_id = current.get("shutdown_id")
                updated = dict(current)
                updated.update(
                    {
                        "admission_blocked": False,
                        "drain_status": STATUS_ABORTED,
                        "drain_completed_at": utc_now_iso(),
                    }
                )
                self.database.set_setting(LIFECYCLE_DRAINING_KEY, updated, connection=conn)
                self.database.append_audit_event(
                    _EVENT_DRAINING_ABORTED,
                    entity_type=_LIFECYCLE_ENTITY_TYPE,
                    data={"shutdown_id": shutdown_id, "reason": reason},
                    connection=conn,
                )
                return True

    def startup_reconcile(self) -> StartupDrainingResolution:
        """Resolve um DRAINING abandonado por um ciclo de processo anterior.

        DEVE ser chamado uma única vez durante a inicialização do produto,
        DEPOIS de ``acquire_ownership()`` ter tido sucesso e de
        ``RecoveryManager.recover_at_startup()``, e ANTES de qualquer
        ``JobEngine.run_pending()``/``advance()``. Levanta
        ``OwnershipNotHeldError`` se ``owns_instance`` for ``False`` — nunca
        resolve DRAINING sem prova de exclusividade (ver docstring do
        módulo, seção OWNERSHIP). Idempotente: se ``admission_blocked`` já
        é ``False``, é um no-op que devolve ``resolved=False``.

        Antes de liberar ``admission_blocked``, valida — por leitura pura da
        tabela ``jobs`` via SQLite, SEM importar nem acoplar a
        ``RecoveryManager`` — que nenhum Job permanece em um estado bruto
        abandonado (``_RAW_ABANDONED_STATES``: PROCESSING/PUBLISHING/
        INTERRUPTED/UNKNOWN de um ciclo anterior). Encontrar qualquer um
        deles aqui é evidência direta de que o ``RecoveryManager`` ainda não
        rodou ou não terminou; ``RECOVERING`` remoto/ambíguo (destino
        legítimo do próprio ``RecoveryManager``) nunca é tratado como
        bloqueio — ver "BLOQUEADOR 2 DA SEGUNDA REVISÃO" na docstring do
        módulo. Quando bloqueado, devolve ``resolved=False`` com
        ``blocked_reason``/``raw_abandoned_job_ids`` preenchidos e NÃO
        altera o lifecycle persistido (preservado para nova tentativa após
        recovery real rodar).

        NUNCA resolve um DRAINING que pertence à SESSÃO OPERACIONAL ATUAL
        deste mesmo processo (``owner_session_id`` == ``process_session_id``
        desta instância) — mesmo que ``owns_instance`` seja ``True`` (o
        próprio processo nunca perdeu o lock). Isso fecha a brecha em que o
        MESMO processo que fez ``shutdown()`` (inclusive já com cleanup
        destrutivo executado) chamaria ``startup_reconcile()`` para se
        auto-reabrir sem nunca ter terminado — ver "BLOQUEADOR 1 DA TERCEIRA
        REVISÃO" na docstring do módulo. Devolve ``resolved=False`` com
        ``belongs_to_current_session=True`` nesse caso, sem alterar nada.

        Verificação de ownership feita DENTRO do ``_shutdown_lock`` (ver
        "BLOQUEADOR 2 DA TERCEIRA REVISÃO").
        """
        with self._shutdown_lock:
            self._require_ownership(operation="startup_reconcile")
            with self.database.transaction() as conn:
                current = self.database.get_setting(LIFECYCLE_DRAINING_KEY, connection=conn)
                if not isinstance(current, dict) or not current.get("admission_blocked"):
                    return StartupDrainingResolution(resolved=False)

                owner_session_id = current.get("owner_session_id")
                if owner_session_id is not None and owner_session_id == self._process_session_id:
                    # Mesma sessão operacional que criou este DRAINING ainda
                    # está viva (nunca terminou) — startup_reconcile() jamais
                    # desfaz o próprio shutdown do processo que o criou.
                    return StartupDrainingResolution(
                        resolved=False,
                        shutdown_id=str(current.get("shutdown_id")) if current.get("shutdown_id") else None,
                        previous_status=str(current.get("drain_status")) if current.get("drain_status") else None,
                        belongs_to_current_session=True,
                        blocked_reason=(
                            "este lifecycle pertence à sessão operacional ATUAL deste processo "
                            "(mesmo owner_session_id) — startup_reconcile() nunca desfaz o próprio "
                            "shutdown do processo que o criou; use abort_shutdown() antes de cleanup "
                            "destrutivo rodar, ou encerre e reabra o processo de verdade"
                        ),
                    )

                raw_abandoned_ids = tuple(
                    sorted(
                        job.id
                        for job in self.database.list(Job)
                        if job.status in _RAW_ABANDONED_STATES
                    )
                )
                if raw_abandoned_ids:
                    return StartupDrainingResolution(
                        resolved=False,
                        shutdown_id=str(current.get("shutdown_id")) if current.get("shutdown_id") else None,
                        previous_status=str(current.get("drain_status")) if current.get("drain_status") else None,
                        blocked_reason=(
                            "recovery incompleto: "
                            f"{len(raw_abandoned_ids)} Job(s) ainda em estado bruto abandonado "
                            "(PROCESSING/PUBLISHING/INTERRUPTED/UNKNOWN) — rode "
                            "RecoveryManager.recover_at_startup() antes de reconciliar"
                        ),
                        raw_abandoned_job_ids=raw_abandoned_ids,
                    )

                shutdown_id = current.get("shutdown_id")
                previous_status = current.get("drain_status")
                updated = dict(current)
                updated.update(
                    {
                        "admission_blocked": False,
                        "drain_status": STATUS_ABANDONED_RESOLVED,
                        "drain_completed_at": utc_now_iso(),
                    }
                )
                self.database.set_setting(LIFECYCLE_DRAINING_KEY, updated, connection=conn)
                self.database.append_audit_event(
                    _EVENT_DRAINING_ABANDONED_RESOLVED,
                    entity_type=_LIFECYCLE_ENTITY_TYPE,
                    data={"shutdown_id": shutdown_id, "previous_status": previous_status},
                    connection=conn,
                )
                return StartupDrainingResolution(
                    resolved=True,
                    shutdown_id=str(shutdown_id) if shutdown_id is not None else None,
                    previous_status=str(previous_status) if previous_status is not None else None,
                )


__all__ = [
    "ShutdownCoordinator",
    "ShutdownCoordinatorError",
    "OwnershipAlreadyHeldError",
    "OwnershipNotHeldError",
    "RecoveryIncompleteError",
    "PreviousSessionLifecycleRequiresReconcileError",
    "ShutdownReport",
    "ResourceCleanupResult",
    "StartupDrainingResolution",
    "LIFECYCLE_DRAINING_KEY",
    "INSTANCE_LOCK_FILENAME",
    "DEFAULT_DRAIN_TIMEOUT_SECONDS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_OWNERSHIP_ACQUIRE_TIMEOUT_SECONDS",
    "STATUS_IN_PROGRESS",
    "STATUS_DRAINED",
    "STATUS_TIMED_OUT",
    "STATUS_ABORTED",
    "STATUS_ABANDONED_RESOLVED",
    "SAFE_ERROR_TYPES",
]

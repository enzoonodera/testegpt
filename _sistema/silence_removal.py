# -*- coding: utf-8 -*-
"""SilenceRemoval -- Prompt 34 (Fase 5: Editor Modular Não Destrutivo).

TEXTO LITERAL DO ROADMAP (implementado exatamente):

    Crie SilenceRemoval como ferramenta independente.
    Configurar: threshold; minimum duration; padding.
    Padrão conservador.
    Depois dos cortes: corrigir vídeo, áudio, captions.
    Criar testes de sincronização.

=======================================================================
0. VISÃO GERAL -- TERCEIRO MÓDULO DA FASE 5 COM PROCESSAMENTO REAL, MAS
   QUE NUNCA REINVENTA O SCHEMA DE CORTES JÁ EXISTENTE (PROMPT 28)
=======================================================================

Mesma estrutura em duas partes dos Prompts 31/33 (nunca o padrão puro de
decisão dos Prompts 27-30/32):

--- PARTE A -- DECISÃO ---
Configuração (``threshold_db``, ``minimum_duration_seconds``,
``padding_seconds``), persistida via ``EditProjectManager`` numa categoria
NOVA ``SILENCE_REMOVAL`` (``"silence_removal"``) -- não havia categoria
reservada para isso em ``edit_project.py`` (confirmado por leitura; as
nove constantes de conveniência lá cobrem ``cuts``/``speed``/``crop``/
``reframe``/``audio_settings``/``captions``/``template``/``text_layers``/
``metadata_mode``, nenhuma para remoção de silêncio). Mesmo padrão do
Prompt 32 (``captions_style.py`` define ``CAPTIONS_STYLE`` localmente, sem
tocar ``edit_project.py`` -- ``_validate_category_name`` já aceita
QUALQUER string não vazia, documentado na própria docstring daquele
módulo). Nenhuma migration nova, nenhuma mudança em ``edit_project.py``.

--- PARTE B -- PROCESSAMENTO REAL ---
Detecção real de silêncio no áudio (mesma exceção já aberta pelos Prompts
31/33 -- nunca o padrão puro de decisão) via um backend injetável (seção
0.2), seguida da APLICAÇÃO do resultado como cortes reais na timeline --
usando EXCLUSIVAMENTE a API pública já existente de ``TimelineEditor``
(Prompt 28), NUNCA escrevendo na categoria ``CUTS`` por conta própria.

=======================================================================
0.1 DECISÃO DE ARQUITETURA CONFIRMADA -- ``TimelineEditor`` É REAPROVEITADO,
    NUNCA REIMPLEMENTADO
=======================================================================

Investigação (releitura completa de ``timeline_editor.py``, seções 0-2 da
sua própria docstring) confirma: o schema da categoria ``CUTS`` (lista
ORDENADA de segmentos ``{segment_id, start, end, speed,
source_segment_id}``, SEMPRE relativos ao vídeo ORIGINAL) e as operações
estruturais (``trim``/``split``/``remove_range``/``remove_segment``/
``merge``/``reorder``/``set_speed``) já existem, já são atômicas (via
``EditProjectManager.update_category``) e já foram adversarialmente
testadas (GATE 6 do Prompt 28 -- concorrência real sobre o mesmo SQLite).

``remove_range(project_id, segment_id, start, end)`` sozinho JÁ cobre os
três casos que "remover um trecho de silêncio detectado" precisa (trecho
no meio, trecho tocando uma borda, trecho == segmento inteiro) -- ver
seção 1.5 da docstring de ``timeline_editor.py``. Nenhum método novo
precisou ser proposto para lá: ``initialize_timeline`` (para o caso da
timeline ainda não existir -- seção 0.2 abaixo) e ``remove_range``/
``get_timeline`` (para aplicar/consultar os cortes) já bastam.

Por isso este módulo IMPORTA ``timeline_editor.py`` DIRETAMENTE -- exceção
DELIBERADA e DOCUMENTADA à regra de "zero acoplamento entre módulos
irmãos de decisão PARALELA" usada para ``captions_style.py``/
``visual_editor.py``/``audio_engine.py``/``auto_reframe.py`` (esses são
decisões INDEPENDENTES entre si, sem relação estrutural; ``cuts`` e
``silence_removal`` têm uma relação real e intencional: um é o
MECANISMO de corte, o outro é uma FONTE de decisão sobre o que cortar).
Reimplementar remoção de trecho por conta própria aqui duplicaria uma
lógica já madura e testada, e arriscaria as duas categorias
dessincronizarem (dois algoritmos calculando "o que sobrou da timeline"
de formas sutilmente diferentes). Confirmado por teste AST dedicado que
``timeline_editor`` é a ÚNICA exceção à lista de módulos irmãos proibidos
(seção 0.6 abaixo).

=======================================================================
0.2 TIMELINE AUTOMATICAMENTE INICIALIZADA QUANDO AUSENTE
=======================================================================

Diferente de um uso manual de ``TimelineEditor`` (onde o chamador decide
quando inicializar), ``SilenceRemoval`` é uma "ferramenta independente"
(Princípio A do CLAUDE.md: nenhuma ferramenta deve obrigar o usuário a
passar pelas demais) -- o usuário não deveria precisar abrir um editor de
timeline manualmente antes de rodar remoção de silêncio. Decisão: se
``TimelineEditor.get_timeline(project_id)`` devolver vazio (timeline nunca
tocada -- ou esvaziada, ver seção 1.4B de ``timeline_editor.py``, mesma
ambiguidade deliberada já aceita lá), o handler chama
``TimelineEditor.initialize_timeline(project_id, duration_seconds=<real,
via MediaProbe>)`` -- SEMPRE através da API pública, nunca escrevendo
``cuts`` diretamente. Se a timeline JÁ tem segmentos (edição manual
anterior, ou uma rodada anterior deste mesmo Job), ``initialize_timeline``
NUNCA é chamado -- evita ``TimelineJaInicializadaError``.

=======================================================================
0.3 BACKEND DE DETECÇÃO DE SILÊNCIO -- INFRAESTRUTURA PRIMEIRO (MESMA
    DECISÃO DOS PROMPTS 31/33, ALTERNATIVA (a))
=======================================================================

Investigação confirmada nesta rodada: ``requirements.txt`` não lista
nenhuma biblioteca de análise de áudio. Duas alternativas foram
explicitamente aceitas pelo próprio Prompt:

    (a) um backend plugável com um stub padrão documentado -- aqui um stub
        que "nunca encontra silêncio" É seguro e coerente com "padrão
        conservador" (diferente do Prompt 33, onde um stub sempre-fallback
        também era seguro pela MESMA razão: nunca inventa evidência);
    (b) usar o filtro ``silencedetect`` do próprio ``ffmpeg`` via
        ``subprocess`` (dependência já existente em produção).

DECISÃO: alternativa (a). ``MediaProbe`` (Prompt 26) foi relido nesta
rodada e NÃO expõe hoje nenhuma extração de níveis de volume/energia de
áudio ao longo do tempo (só metadados: duração/fps/resolução/presença de
áudio) -- confirmado por leitura completa de ``media_probe.py``, nada
reaproveitável para detecção de silêncio existe hoje. A alternativa (b)
exigiria: parsear a saída de ``stderr`` do filtro ``silencedetect`` (texto
não estruturado, formato específico de versão do ffmpeg), decidir
timeout/semântica de processo em Windows (seção 11 do GATE ADVERSARIAL) e
testar isso de forma determinística -- trabalho real e não trivial, igual
em espírito ao que ``faster-whisper``/detecção visual (Prompts 31/33)
adiaram por falta de informação suficiente hoje sobre qual seria a
integração definitiva. Por isso esta rodada entrega a INFRAESTRUTURA
COMPLETA (Job/cache/Artifact/aplicação de cortes/checkpoint) com um
backend de detecção PLUGÁVEL (``silence_detection_backend`` no
construtor, mesmo padrão de ``transcription_backend``/
``detection_backend``), com um stub de produção padrão
(``_default_detect_silences``) que SEMPRE devolve nenhuma silêncio
encontrado (tupla vazia) -- nunca finge detectar algo. Em produção, SEM um
backend real injetado, ``SilenceRemoval`` nunca corta nada -- comportamento
seguro e coerente com "padrão conservador" por construção, documentado
como esperado até que um backend real (candidato óbvio: ``ffmpeg
silencedetect``, opção (b) acima, adiada) seja integrado numa rodada
futura com informação suficiente. Import de qualquer biblioteca real
futura seria LOCAL à função de backend real (nunca no topo do módulo),
mesmo padrão de ``_default_transcribe``/``_default_detect``.

Contrato do backend injetável (``SilenceDetectionBackend``)::

    Callable[[str, float], tuple[tuple[float, float], ...]]
    (local_path, duration_seconds) -> tupla de intervalos BRUTOS
    ``(start, end)`` em segundos, relativos ao vídeo ORIGINAL, SEM
    filtragem por ``minimum_duration``/``padding`` (essa filtragem é
    responsabilidade do HANDLER, seção 0.5 -- o backend só reporta o que
    encontrou, a política de "o que conta como removível" é do módulo).

Chamado UMA ÚNICA VEZ por Job (não amostra a amostra como o Prompt 33 --
decisão documentada: diferente de detecção visual por frame, análise de
volume de áudio é naturalmente contínua ao longo de todo o trecho, o
próprio backend decide internamente sua granularidade de análise).
Qualquer exceção do backend é capturada pelo handler (``try/except
Exception``, nunca propagada) e tratada como "nenhum silêncio encontrado"
-- ver seção 0.7 sobre por que essa é a decisão correta AQUI (diferente do
Prompt 31).

=======================================================================
0.4 "PADRÃO CONSERVADOR" -- VALORES PADRÃO E POR QUÊ
=======================================================================

Quando um campo de configuração não foi explicitamente definido pelo
usuário, o handler usa uma constante conservadora (nunca ``None``/erro):

- ``DEFAULT_THRESHOLD_DB = -50.0``: nível de volume abaixo do qual um
  trecho conta como "silêncio", em dBFS (decibéis relativos ao full
  scale digital -- unidade padrão de medição de nível de áudio digital,
  onde ``0 dBFS`` é o pico máximo possível e valores mais negativos são
  mais silenciosos). ``-50 dBFS`` é um limiar BEM baixo (bem mais
  silencioso que fala normal, que tipicamente fica entre ``-30`` e
  ``-15 dBFS`` mesmo em trechos mais suaves) -- erra propositalmente para
  o lado de "só corta silêncio genuíno", nunca fala baixa/sussurrada.
- ``DEFAULT_MINIMUM_DURATION_SECONDS = 0.6``: um silêncio precisa durar
  pelo menos 600ms para ser candidato a remoção -- pausas respiratórias e
  entre-palavras naturais tipicamente duram bem menos que isso; erra para
  o lado de PRESERVAR pausas curtas/naturais.
- ``DEFAULT_PADDING_SECONDS = 0.1``: 100ms de folga preservada em CADA
  ponta do intervalo de silêncio detectado ANTES de virar um corte --
  evita cortar a cauda de uma sílaba ou o início de uma respiração colada
  ao silêncio (seção 0.5 detalha o mecanismo exato).

Estes valores nunca são inventados sem justificativa: documentados aqui
para que uma revisão futura baseada em dados reais de uso possa ajustá-los
CONSCIENTEMENTE (mesmo espírito da faixa de ``speed`` em
``timeline_editor.py``, seção 2 daquele módulo).

=======================================================================
0.5 PADDING -- APLICADO ANTES DE DECIDIR O CORTE FINAL
=======================================================================

Para cada intervalo BRUTO ``(start, end)`` devolvido pelo backend:

1. Sanitização: descarta intervalos com tipo/valor inválido (não-finito,
   ``end <= start``) ou totalmente fora de ``[0, duration]``; recorta
   (``clip``) intervalos parcialmente fora de ``[0, duration]`` para
   dentro dessa faixa -- o backend nunca é confiado cegamente, mesmo
   sendo um "backend interno" (mesma disciplina de
   ``_validate_and_normalize_segments`` do Prompt 31).
2. Filtro por ``minimum_duration_seconds``: um intervalo (já sanitizado)
   com duração ``< minimum_duration_seconds`` é DESCARTADO -- nunca vira
   candidato a corte (roadmap: "minimum duration").
3. ``padding_seconds`` é aplicado SÓ AGORA, encolhendo as DUAS pontas do
   intervalo sobrevivente: ``padded_start = start + padding_seconds``,
   ``padded_end = end - padding_seconds``. Se ``padded_end <= padded_start``
   (padding consumiu o intervalo inteiro -- só pode acontecer se
   ``2 * padding_seconds >= (end - start)``, e como
   ``(end - start) >= minimum_duration_seconds`` já foi garantido no passo
   2, isso só ocorre com uma combinação deliberadamente agressiva de
   configuração), o intervalo é DESCARTADO nesta etapa -- nunca um corte
   de duração zero/negativa é proposto ao ``TimelineEditor`` (que já
   rejeitaria isso, mas a checagem aqui evita até tentar).

O resultado dessa pipeline (sanitização → filtro por duração mínima →
encolhimento por padding) é a lista de INTERVALOS CANDIDATOS -- o que este
módulo efetivamente tenta aplicar como cortes reais (seção 0.6).

=======================================================================
0.6 APLICAÇÃO DOS CORTES -- POR QUE PODE HAVER INTERVALOS "PULADOS"
    (NUNCA UM ERRO)
=======================================================================

Os intervalos candidatos são relativos ao vídeo ORIGINAL -- MESMO
referencial dos segmentos de ``cuts`` (seção 0.1). Para cada intervalo
candidato, em ordem crescente de ``start``:

1. Relê a timeline ATUAL (``TimelineEditor.get_timeline``) -- necessário
   porque cada ``remove_range`` aplicado muda o conjunto de segmentos
   (pode dividir um segmento em dois, ou encolher uma borda).
2. Procura um ÚNICO segmento atual que contenha o intervalo INTEIRO
   (``segmento.start <= candidato.start`` e ``candidato.end <=
   segmento.end``).
3. Se encontrado: chama ``TimelineEditor.remove_range(project_id,
   segmento.segment_id, candidato.start, candidato.end)``.
4. Se NÃO encontrado (o intervalo já foi removido por uma execução
   anterior deste mesmo Job/config -- idempotência, seção 0.8; ou o
   intervalo cruza um limite entre dois segmentos já existentes por causa
   de uma edição manual anterior; ou uma instância CONCORRENTE já removeu
   esse trecho primeiro -- seção 0.9): o intervalo é registrado como
   PULADO (``skipped_intervals``, com motivo), NUNCA um erro, NUNCA falha
   o Job. Decisão deliberada de NÃO estender ``timeline_editor.py`` para
   suportar "remover um intervalo que cruza vários segmentos" -- nenhuma
   necessidade concreta demonstrada disso (CLAUDE.md: não fazer
   overengineering sem benefício demonstrado); um intervalo que cruza
   segmentos só pode surgir de uma composição incomum de edições manuais
   prévias com a detecção automática, e "pular e documentar" é uma
   resposta segura e honesta a essa situação, nunca uma tentativa
   arriscada de adivinhar a intenção do usuário.

Uma chamada a ``remove_range`` que MESMO ASSIM levantar um erro
estruturado de ``timeline_editor`` (``SegmentoNaoEncontradoError``/
``PontoDeCorteInvalidoError``/``SegmentoInvalidoError`` -- por exemplo,
uma instância concorrente removeu o segmento ENTRE o passo 2 e o passo 3
acima, uma janela de corrida genuína) é CAPTURADA pelo handler e também
tratada como "pulado" (motivo ``race_condition``) -- nunca falha o Job.
Isto é DIFERENTE da decisão do Prompt 33 ("nunca bloquear Job por falha de
DETECTOR"): aqui é "nunca bloquear Job por um corte que não pôde mais ser
aplicado exatamente como planejado" -- ambos os módulos compartilham o
espírito de "padrão conservador"/"não travar o lote por uma situação já seguramente
contornável", mas por razões estruturais distintas (documentado
explicitamente para não confundir as duas justificativas).

=======================================================================
0.7 FALHA DO BACKEND DE DETECÇÃO -- JOB NUNCA FALHA POR ISSO (DECISÃO
    DOCUMENTADA, DIFERENTE DO PROMPT 31)
=======================================================================

"Nunca bloquear Job por falha de tracking" NÃO é texto literal deste
Prompt (é específico do Prompt 33) -- mas o mesmo espírito de "padrão
conservador" (texto literal DESTE Prompt) leva à MESMA conclusão aqui,
por uma razão estrutural própria: o "fallback seguro" de uma falha de
detecção de silêncio é TRIVIAL -- não cortar nada. Diferente do Prompt 31
(uma transcrição que falhou não tem substituto seguro -- é o próprio
produto) e mais parecido com o Prompt 33 (um crop central é um substituto
seguro e óbvio para uma curva de tracking), aqui "nenhum corte aplicado"
é um resultado PERFEITAMENTE válido e seguro -- o vídeo simplesmente
continua com a timeline que já tinha (ou uma timeline inicializada sem
nenhum corte). Por isso: qualquer exceção lançada pelo
``silence_detection_backend`` é capturada pelo handler
(``try/except Exception``, uma ÚNICA vez, já que o backend é chamado uma
única vez por Job -- seção 0.3) e tratada EXATAMENTE como "o backend
devolveu uma tupla vazia" -- Job termina ``JOB_READY``, com
``applied_intervals=[]``/``skipped_intervals=[]``, nunca ``JOB_FAILED``.

ISTO É DISTINTO de falha ESTRUTURAL (``SourceAsset``/``Video`` ausentes,
arquivo ilegível no disco, ``probe`` inválido, ``project_id`` ausente) --
essas continuam falhando o Job normalmente, verificadas ANTES de chamar o
backend, exatamente como em todos os módulos anteriores com Parte B real.

=======================================================================
0.8 CACHE -- ARTIFACT PRÓPRIO (DECISÃO: SIM, REAPROVEITANDO O MECANISMO
    JÁ EXISTENTE)
=======================================================================

DECISÃO: o resultado da detecção (intervalos candidatos, seção 0.5) É
persistido como um ``Artifact`` real (``kind="silence_removal_track"``),
com ``fingerprint`` = chave de cache -- reaproveitando o MESMO mecanismo
de cache dos Prompts 31/33 (``compute_cache_key``/consulta por
``video_id + kind + fingerprint``, reimplementado localmente, NUNCA
importado de ``captions_engine``/``auto_reframe`` -- zero acoplamento
entre módulos irmãos de PROCESSAMENTO, distinto da exceção documentada
para ``timeline_editor`` na seção 0.1). Motivo de NÃO confiar só na
revisão/fingerprint de ``cuts`` (a alternativa considerada): detectar
silêncio é o trabalho CARO (chamar o backend); reprocessar o MESMO áudio
com a MESMA configuração não deveria rodar a detecção de novo só porque
os CORTES specific já podem ter sido total ou parcialmente "consumidos"
pela timeline (seção 0.6) -- sem um Artifact próprio, não haveria como
saber "quais intervalos essa configuração decide remover" sem rechamar o
backend. A chave de cache inclui::

    {"source_fingerprint": <SourceAsset.fingerprint>,
     "threshold_db": <float>,
     "minimum_duration_seconds": <float>,
     "padding_seconds": <float>,
     "detector_backend_version": <constante deste módulo>}

Em cache HIT: o backend NUNCA é chamado de novo; os intervalos candidatos
persistidos no ``Artifact`` são relidos do arquivo e reaplicados à
timeline ATUAL do Job (seção 0.6 -- naturalmente idempotente: intervalos
já removidos em uma execução anterior aparecem como "pulados" desta vez,
nunca um erro, nunca uma duplicação de corte).

=======================================================================
0.9 CANCELAMENTO E CONCORRÊNCIA
=======================================================================

Mesmo padrão dos Prompts 31/33: se ``control_manager`` foi fornecido,
checa ``is_job_cancel_requested`` em dois pontos -- ANTES de chamar o
backend de detecção (evita trabalho desperdiçado) e DEPOIS de calcular os
intervalos candidatos mas ANTES de aplicar qualquer corte na timeline
(evita publicar/aplicar evidência de um trabalho que será descartado).

Concorrência: duas instâncias processando o MESMO projeto+config
concorrentemente serializam naturalmente na escrita de ``cuts`` (mesma
garantia de ``BEGIN IMMEDIATE`` de ``EditProjectManager.update_category``,
já testada pelo GATE 6 do Prompt 28) -- a segunda instância a chegar em
cada ``remove_range`` individual verá um estado já mutado pela primeira e,
na pior das hipóteses, cai no caminho "pulado"/"race_condition" da seção
0.6, nunca corrompendo nem duplicando um corte. A MESMA corrida existe na
inicialização da timeline (seção 0.2): duas instâncias podem ambas
observar ``get_timeline`` vazio e tentar ``initialize_timeline`` -- a
PRIMEIRA a chegar vence, a segunda recebe ``TimelineJaInicializadaError``
(capturado explicitamente, tratado como benigno -- segue usando a
timeline que a outra instância já criou), NUNCA propagado como falha do
Job. Achado e corrigido pelo GATE ADVERSARIAL desta rodada (teste
``test_duas_instancias_concorrentes_processando_o_mesmo_video``). Cada instância ainda
insere sua PRÓPRIA linha de ``Artifact`` de cache (mesmo padrão
``overwrite=True`` na promoção física do Prompt 31/33 -- conteúdo
determinístico a partir da mesma entrada é seguro de sobrescrever).

=======================================================================
0.10 "CORRIGIR VÍDEO, ÁUDIO, CAPTIONS" -- O QUE ISSO SIGNIFICA NESTE
     PRODUTO (DECISÃO MAIS IMPORTANTE DO PROMPT, COM PROVA POR TESTE)
=======================================================================

Investigação: este produto NÃO separa vídeo/áudio em trilhas/arquivos
distintos -- é tudo a MESMA timeline de ``cuts`` (seção 1 de
``timeline_editor.py``); "corrigir vídeo" e "corrigir áudio" são a MESMA
coisa aqui, e já são cobertos estruturalmente pelo próprio mecanismo de
``cuts`` (um corte removido da timeline remove vídeo E áudio daquele
trecho simultaneamente, por definição -- não existe um jeito de cortar só
um dos dois neste schema). ``audio_engine.py`` (Prompt 30) é configuração
PURA (fade/normalização/etc.), sem nenhum timestamp próprio a
reconciliar -- nada ali precisa de correção quando ``cuts`` muda.

O ponto real de risco é CAPTIONS (Prompt 31): os segmentos de
transcrição (``TranscriptionSegment``, artifact ``transcript_internal``)
têm ``start``/``end`` relativos ao vídeo ORIGINAL -- EXATAMENTE o mesmo
referencial de ``cuts`` (seção 0.1 acima). CONCLUSÃO INVESTIGADA E PROVADA
POR TESTE (``tests/test_silence_removal.py``, seção "sincronização"):
como os dois referenciais já são idênticos, um trecho de silêncio
removido da timeline NUNCA precisa remapear timestamp NENHUM de legenda
-- uma legenda cujo intervalo cai INTEIRAMENTE fora de qualquer intervalo
removido continua válida no MESMO timestamp original de sempre (uma etapa
de renderização/composição futura só precisa saber "este timestamp
original ainda existe em `cuts`?", nunca recalcular nada). Prova: os
objetos de legenda (``TranscriptionSegment``) usados no teste permanecem
BYTE-A-BYTE idênticos antes/depois de rodar ``SilenceRemoval`` -- porque
este módulo NUNCA lê nem escreve nenhum artefato de ``captions_engine.py``
(não importa esse módulo -- ver seção 0.1: a única exceção documentada é
``timeline_editor``).

CASO GENUÍNO DE RISCO (coberto por teste dedicado, "decisão sem
truncamento"): um intervalo de silêncio detectado e efetivamente cortado
que INTERSECTA parcialmente o intervalo de uma legenda existente (só pode
acontecer se o detector encontrar "silêncio" no MEIO de um segmento de
fala transcrito -- ex.: uma pausa longa dentro de uma frase). DECISÃO:
este Prompt NÃO implementa nenhum mecanismo de truncamento/remapeamento de
legenda -- ``SilenceRemoval`` nunca consulta nem depende de artefatos de
``captions_engine.py`` ao decidir cortes (mantém a independência entre
ferramentas, Princípio A do CLAUDE.md); a legenda em si permanece
ARMAZENADA EXATAMENTE como estava (nenhum artefato de captions é tocado).
A CONSEQUÊNCIA de reprodução (uma legenda cujo trecho de fala foi
parcialmente cortado apareceria "adiantada" ou "cortada" na reprodução
final) é um problema de uma etapa de RENDERIZAÇÃO/COMPOSIÇÃO futura
reconciliar (decidir: pular a legenda inteira, truncar o texto, ou manter
como está) -- exatamente o mesmo tipo de decisão que qualquer edição
manual de corte via ``timeline_editor.py`` já teria que enfrentar
independente deste Prompt (cortar um trecho manualmente no MEIO de uma
fala transcrita tem o MESMO efeito colateral; não é uma situação nova
introduzida por SilenceRemoval). Fora de escopo aqui -- nenhum Render
Engine existe ainda no produto (documentado como pendência, mesma
disciplina de ``timeline_editor.py`` seção 0.3: "se uma etapa de
renderização futura precisar confrontar Edit Decisions com [algo], ela
decide isso por conta própria").

=======================================================================
0.11 CHECKPOINT -- POR QUE ``EDIT_PLANNED`` (NÃO ``MEDIA_PROCESSED``)
=======================================================================

``domain/checkpoints.py`` é vocabulário FECHADO. Confirmado por grep
nesta rodada: ``CHECKPOINT_TRANSCRIBED`` (Prompt 31) e
``CHECKPOINT_MEDIA_PROCESSED`` (Prompt 33) já estão em uso;
``CHECKPOINT_EDIT_PLANNED`` está LIVRE. DECISÃO: reaproveitar
``CHECKPOINT_EDIT_PLANNED`` -- distinto conscientemente de
``MEDIA_PROCESSED`` (que o Prompt 33 usou para uma curva de ANÁLISE de
mídia, sem produzir uma decisão de edição por si própria) porque o
produto central deste módulo é literalmente uma decisão de EDIÇÃO
aplicada (``cuts`` mutados) -- "um plano de edição foi computado e
aplicado" é o encaixe semântico direto de ``EDIT_PLANNED``, mais preciso
que reutilizar ``MEDIA_PROCESSED`` só porque já foi usado antes por outro
módulo. Nenhum checkpoint novo foi proposto/necessário.

=======================================================================
0.12 ``media_catalog.py`` -- NÃO TOCADO
=======================================================================

Confirmado por leitura: não existe, hoje, nenhum badge dedicado a
"silêncio removido" em ``SYSTEM_BADGES`` (os 17 badges existentes não
incluem nada equivalente) -- nada precisou ser confirmado como
"continua zero", porque nunca existiu um badge para isso. Este Prompt NÃO
modifica ``_sistema/media_catalog.py`` -- se um badge desse tipo for
desejado no futuro, é trabalho de uma rodada de "Catalog Wiring"
dedicada, incluindo a decisão de vocabulário (fora de escopo aqui, e o
roadmap deste Prompt não pede isso).

=======================================================================
1. PARTE A -- USO
=======================================================================

    engine = SilenceRemovalEngine(manager, database=db, storage_manager=sm,
                                   app_paths=paths, timeline_editor=te)
    engine.set_config(project_id, threshold_db=-45.0,
                       minimum_duration_seconds=0.8, padding_seconds=0.15)
    state = engine.get_config(project_id)

=======================================================================
2. PARTE B -- USO
=======================================================================

    job_engine.register_handler(
        SilenceRemovalEngine.OPERATION,
        engine.handle_silence_removal_job,
        claims_status=JOB_PROCESSING,
    )
    job = audit_log.create_job(Job(video_id=video.id, project_id=project.id,
                                    operation=SilenceRemovalEngine.OPERATION))
    job_engine.advance(job.id)
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ClassVar, Mapping

from .app_paths import AppPaths
from .control_manager import ControlManager
from .domain import (
    Artifact,
    CHECKPOINT_EDIT_PLANNED,
    Job,
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_READY,
    SourceAsset,
    Video,
)
from .edit_project import EditProjectManager, ProjectNaoEncontradoError
from .job_engine import JobStepResult
from .media_probe import MediaProbe
from .storage.audit import AUDIT_JOB_PROCESSING_COMPLETED, OperationalAuditLog
from .storage.database import LocalDatabase
from .storage_manager import StorageManager
from .timeline_editor import TimelineEditor, TimelineEditorError

# ---------------------------------------------------------------------
# Vocabulário -- Parte A
# ---------------------------------------------------------------------

# Categoria NOVA -- não reservada em edit_project.py (ver seção 0 da
# docstring do módulo). Qualquer string não vazia é aceita por
# EditProjectManager; nomenclatura local, mesmo padrão de
# captions_style.py:CAPTIONS_STYLE.
SILENCE_REMOVAL = "silence_removal"

THRESHOLD_DB_RANGE = (-90.0, -10.0)
MINIMUM_DURATION_SECONDS_RANGE = (0.05, 30.0)
PADDING_SECONDS_RANGE = (0.0, 5.0)

DEFAULT_THRESHOLD_DB = -50.0
DEFAULT_MINIMUM_DURATION_SECONDS = 0.6
DEFAULT_PADDING_SECONDS = 0.1

OPERATION_SILENCE_REMOVAL = "SILENCE_REMOVAL"

ARTIFACT_KIND_SILENCE_TRACK = "silence_removal_track"

DETECTOR_BACKEND_VERSION = "v1"


# ---------------------------------------------------------------------
# Erros estruturados -- Parte A
# ---------------------------------------------------------------------

class SilenceRemovalError(RuntimeError):
    """Classe base de todos os erros deste módulo."""

    code: "ClassVar[str]" = "SILENCE_REMOVAL_ERRO"


class CampoInvalidoError(SilenceRemovalError):
    """Um campo de configuração está fora do formato/range aceito."""

    code: "ClassVar[str]" = "CAMPO_INVALIDO"


# ---------------------------------------------------------------------
# Dataclasses -- Parte A
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class SilenceRemovalConfigState:
    """Snapshot somente-leitura da categoria ``SILENCE_REMOVAL`` de um
    Project. Todo campo ``None`` até que o usuário decida explicitamente
    -- o handler (Parte B) substitui ausências pelos padrões
    conservadores (seção 0.4 da docstring do módulo), nunca aqui."""

    threshold_db: "float | None" = None
    minimum_duration_seconds: "float | None" = None
    padding_seconds: "float | None" = None


# ---------------------------------------------------------------------
# Dataclasses -- Parte B
# ---------------------------------------------------------------------

# Backend de detecção injetável: (local_path, duration_seconds) -> tupla
# de intervalos BRUTOS (start, end) em segundos -- ver seção 0.3 da
# docstring do módulo. Nunca chamado diretamente pelos testes deste
# módulo com o backend real.
SilenceDetectionBackend = Callable[[str, float], "tuple[tuple[float, float], ...]"]


# ---------------------------------------------------------------------
# Helpers -- cache key (canônico, reimplementado -- nunca importado de
# captions_engine.py/auto_reframe.py, zero acoplamento entre módulos
# irmãos de PROCESSAMENTO -- ver seção 0.8)
# ---------------------------------------------------------------------

def _canonical_json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_cache_key(
    source_fingerprint: str,
    threshold_db: float,
    minimum_duration_seconds: float,
    padding_seconds: float,
) -> str:
    """Chave de cache determinística -- ver seção 0.8 da docstring do
    módulo. ``sort_keys=True`` garante que a ordem de construção do dict
    de configuração nunca influencia o resultado."""
    payload = {
        "source_fingerprint": str(source_fingerprint),
        "threshold_db": float(threshold_db),
        "minimum_duration_seconds": float(minimum_duration_seconds),
        "padding_seconds": float(padding_seconds),
        "detector_backend_version": DETECTOR_BACKEND_VERSION,
    }
    canonical = _canonical_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------
# Helpers -- validação de campos (Parte A)
# ---------------------------------------------------------------------

def _validate_numeric_range(value: Any, field_name: str, value_range: "tuple[float, float]") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CampoInvalidoError(f"{field_name} deve ser numérico (recebido: {value!r})")
    result = float(value)
    if not math.isfinite(result):
        raise CampoInvalidoError(f"{field_name} deve ser finito (recebido: {value!r})")
    lo, hi = value_range
    if not (lo <= result <= hi):
        raise CampoInvalidoError(
            f"{field_name} deve estar em [{lo}, {hi}] (recebido: {value!r})"
        )
    return result


def _validate_threshold_db(value: Any) -> float:
    return _validate_numeric_range(value, "threshold_db", THRESHOLD_DB_RANGE)


def _validate_minimum_duration_seconds(value: Any) -> float:
    return _validate_numeric_range(value, "minimum_duration_seconds", MINIMUM_DURATION_SECONDS_RANGE)


def _validate_padding_seconds(value: Any) -> float:
    return _validate_numeric_range(value, "padding_seconds", PADDING_SECONDS_RANGE)


_FIELD_VALIDATORS: "dict[str, Any]" = {
    "threshold_db": _validate_threshold_db,
    "minimum_duration_seconds": _validate_minimum_duration_seconds,
    "padding_seconds": _validate_padding_seconds,
}


def _validate_config_fields(fields: Mapping[str, Any]) -> "dict[str, Any]":
    unknown = set(fields) - set(_FIELD_VALIDATORS)
    if unknown:
        raise CampoInvalidoError(f"campo(s) desconhecido(s): {sorted(unknown)}")
    validated: "dict[str, Any]" = {}
    for name, value in fields.items():
        validated[name] = _FIELD_VALIDATORS[name](value)
    return validated


# ---------------------------------------------------------------------
# Helpers -- pipeline de intervalos (Parte B, seção 0.5)
# ---------------------------------------------------------------------

def _sanitize_and_clip(
    raw_intervals: "tuple[tuple[float, float], ...]", duration: float
) -> "list[tuple[float, float]]":
    sanitized: "list[tuple[float, float]]" = []
    for item in raw_intervals:
        try:
            start, end = item
        except (TypeError, ValueError):
            continue
        if isinstance(start, bool) or isinstance(end, bool):
            continue
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        start = float(start)
        end = float(end)
        if not (math.isfinite(start) and math.isfinite(end)):
            continue
        if end <= start:
            continue
        clipped_start = max(0.0, min(start, duration))
        clipped_end = max(0.0, min(end, duration))
        if clipped_end <= clipped_start:
            continue
        sanitized.append((clipped_start, clipped_end))
    return sanitized


def _apply_minimum_duration_and_padding(
    intervals: "list[tuple[float, float]]",
    *,
    minimum_duration_seconds: float,
    padding_seconds: float,
) -> "list[tuple[float, float]]":
    candidates: "list[tuple[float, float]]" = []
    for start, end in intervals:
        if (end - start) < minimum_duration_seconds:
            continue
        padded_start = start + padding_seconds
        padded_end = end - padding_seconds
        if padded_end <= padded_start:
            continue
        candidates.append((padded_start, padded_end))
    candidates.sort(key=lambda pair: pair[0])
    return candidates


# ---------------------------------------------------------------------
# Backend real (produção) -- stub explícito, ver seção 0.3
# ---------------------------------------------------------------------

def _default_detect_silences(local_path: str, duration_seconds: float) -> "tuple[tuple[float, float], ...]":
    """Backend de produção padrão -- nenhuma análise real de áudio está
    disponível/integrada nesta etapa (ver seção 0.3). Sempre devolve uma
    tupla vazia (nenhum silêncio encontrado) -- nunca finge detectar algo,
    comportamento seguro e coerente com "padrão conservador". Import
    local reservado para quando um backend real (ex.: ``ffmpeg
    silencedetect``) for integrado (mesmo padrão de
    ``_default_transcribe``/``_default_detect``) -- hoje não há nenhum
    import a isolar."""
    return ()


# ---------------------------------------------------------------------
# SilenceRemovalEngine
# ---------------------------------------------------------------------

class SilenceRemovalEngine:
    """Parte A (configuração) + Parte B (detecção real + aplicação de
    cortes via ``TimelineEditor``) -- ver seção 0 da docstring do módulo
    para o contrato completo."""

    OPERATION: "ClassVar[str]" = OPERATION_SILENCE_REMOVAL

    def __init__(
        self,
        manager: EditProjectManager,
        *,
        database: LocalDatabase,
        storage_manager: StorageManager,
        app_paths: AppPaths,
        timeline_editor: "TimelineEditor | None" = None,
        audit_log: "OperationalAuditLog | None" = None,
        silence_detection_backend: "SilenceDetectionBackend | None" = None,
        control_manager: "ControlManager | None" = None,
        media_probe: "MediaProbe | None" = None,
    ) -> None:
        if not isinstance(manager, EditProjectManager):
            raise TypeError(f"manager deve ser EditProjectManager (recebido: {type(manager)!r})")
        if not isinstance(database, LocalDatabase):
            raise TypeError(f"database deve ser LocalDatabase (recebido: {type(database)!r})")
        if not isinstance(storage_manager, StorageManager):
            raise TypeError(
                f"storage_manager deve ser StorageManager (recebido: {type(storage_manager)!r})"
            )
        if not isinstance(app_paths, AppPaths):
            raise TypeError(f"app_paths deve ser AppPaths (recebido: {type(app_paths)!r})")
        if timeline_editor is not None and not isinstance(timeline_editor, TimelineEditor):
            raise TypeError(
                f"timeline_editor deve ser TimelineEditor (recebido: {type(timeline_editor)!r})"
            )
        if silence_detection_backend is not None and not callable(silence_detection_backend):
            raise TypeError("silence_detection_backend deve ser chamável")
        if control_manager is not None and not isinstance(control_manager, ControlManager):
            raise TypeError(
                f"control_manager deve ser ControlManager (recebido: {type(control_manager)!r})"
            )
        if media_probe is not None and not isinstance(media_probe, MediaProbe):
            raise TypeError(f"media_probe deve ser MediaProbe (recebido: {type(media_probe)!r})")

        self._manager = manager
        self._database = database
        self._storage = storage_manager
        self._app_paths = app_paths
        self._timeline = timeline_editor if timeline_editor is not None else TimelineEditor(database)
        self._audit_log = audit_log if audit_log is not None else OperationalAuditLog(database)
        self._backend: SilenceDetectionBackend = silence_detection_backend or _default_detect_silences
        self._control_manager = control_manager
        self._probe = media_probe if media_probe is not None else MediaProbe()

    # ===================================================================
    # PARTE A -- configuração
    # ===================================================================

    def set_config(self, project_id: str, **fields: Any) -> SilenceRemovalConfigState:
        validated = _validate_config_fields(fields)

        def mutator(current_data: Any) -> dict:
            merged = dict(current_data) if isinstance(current_data, Mapping) else {}
            merged.update(validated)
            return merged

        try:
            self._manager.update_category(project_id, SILENCE_REMOVAL, mutator)
        except ProjectNaoEncontradoError:
            raise
        except ValueError as exc:
            raise CampoInvalidoError(f"project_id inválido: {project_id!r}") from exc
        return self.get_config(project_id)

    def get_config(self, project_id: str) -> SilenceRemovalConfigState:
        try:
            state = self._manager.get_category(project_id, SILENCE_REMOVAL)
        except ValueError as exc:
            raise CampoInvalidoError(f"project_id inválido: {project_id!r}") from exc
        if state is None:
            return SilenceRemovalConfigState()
        data = state.data if isinstance(state.data, Mapping) else {}

        def _read(name: str, validator) -> Any:
            value = data.get(name)
            if value is None:
                return None
            try:
                return validator(value)
            except SilenceRemovalError as exc:
                raise CampoInvalidoError(
                    f"{name} persistido é inválido (recebido: {value!r})"
                ) from exc

        return SilenceRemovalConfigState(
            threshold_db=_read("threshold_db", _validate_threshold_db),
            minimum_duration_seconds=_read("minimum_duration_seconds", _validate_minimum_duration_seconds),
            padding_seconds=_read("padding_seconds", _validate_padding_seconds),
        )

    # ===================================================================
    # PARTE B -- handler de Job (detecção real + aplicação de cortes)
    # ===================================================================

    def handle_silence_removal_job(self, job: Job) -> JobStepResult:
        if job.video_id is None:
            return JobStepResult(target_status=JOB_FAILED, data={"reason": "job_missing_video_id"})
        if job.project_id is None:
            # A configuração e os cortes vivem NO Project -- sem
            # project_id não há de onde ler a config nem onde aplicar os
            # cortes (mesma razão estrutural do Prompt 33).
            return JobStepResult(target_status=JOB_FAILED, data={"reason": "job_missing_project_id"})

        try:
            config = self.get_config(job.project_id)
        except ProjectNaoEncontradoError:
            return JobStepResult(
                target_status=JOB_FAILED,
                data={"reason": "project_not_found", "project_id": job.project_id},
            )

        threshold_db = config.threshold_db if config.threshold_db is not None else DEFAULT_THRESHOLD_DB
        minimum_duration_seconds = (
            config.minimum_duration_seconds
            if config.minimum_duration_seconds is not None
            else DEFAULT_MINIMUM_DURATION_SECONDS
        )
        padding_seconds = (
            config.padding_seconds if config.padding_seconds is not None else DEFAULT_PADDING_SECONDS
        )

        video = self._database.get(Video, job.video_id)
        if video is None or video.source_asset_id is None:
            return JobStepResult(
                target_status=JOB_FAILED,
                data={"reason": "video_or_source_asset_id_missing", "video_id": job.video_id},
            )
        source = self._database.get(SourceAsset, video.source_asset_id)
        if source is None or not source.local_path:
            return JobStepResult(
                target_status=JOB_FAILED, data={"reason": "source_asset_missing_or_no_local_path"}
            )
        if not source.fingerprint:
            return JobStepResult(
                target_status=JOB_FAILED, data={"reason": "source_asset_missing_fingerprint"}
            )

        local_path = str(source.local_path)
        cache_key = compute_cache_key(
            source.fingerprint, threshold_db, minimum_duration_seconds, padding_seconds
        )

        if self._cancel_requested(job.id):
            return JobStepResult(
                target_status=JOB_CANCELLED, data={"reason": "cancelled_before_detection"}
            )

        cached_artifact = self._find_cached_artifact(job.video_id, cache_key)
        if cached_artifact is not None:
            candidate_intervals = self._read_candidate_intervals(cached_artifact)
            return self._apply_candidates_and_finish(
                job, cache_key, candidate_intervals, cache_hit=True, artifact=cached_artifact
            )

        probe_result = self._probe.probe(local_path)
        if not probe_result.valid or not probe_result.duration:
            return JobStepResult(
                target_status=JOB_FAILED,
                data={"reason": "probe_invalid", "error_code": probe_result.error_code},
            )
        duration = float(probe_result.duration)

        # Trabalho pesado real -- UMA única chamada por Job (seção 0.3).
        # Qualquer exceção do backend NUNCA propaga (seção 0.7): vira
        # "nenhum silêncio encontrado".
        try:
            raw_intervals = self._backend(local_path, duration)
            if not isinstance(raw_intervals, tuple):
                raw_intervals = tuple(raw_intervals) if raw_intervals else ()
        except Exception:
            raw_intervals = ()

        sanitized = _sanitize_and_clip(raw_intervals, duration)
        candidate_intervals = _apply_minimum_duration_and_padding(
            sanitized,
            minimum_duration_seconds=minimum_duration_seconds,
            padding_seconds=padding_seconds,
        )

        if self._cancel_requested(job.id):
            return JobStepResult(
                target_status=JOB_CANCELLED, data={"reason": "cancelled_after_detection"}
            )

        artifact = self._write_and_register_artifact(job, cache_key, candidate_intervals)
        return self._apply_candidates_and_finish(
            job, cache_key, candidate_intervals, cache_hit=False, artifact=artifact, duration=duration
        )

    # -- helpers internos de Parte B ------------------------------------

    def _apply_candidates_and_finish(
        self,
        job: Job,
        cache_key: str,
        candidate_intervals: "list[tuple[float, float]]",
        *,
        cache_hit: bool,
        artifact: Artifact,
        duration: "float | None" = None,
    ) -> JobStepResult:
        if not self._timeline.get_timeline(job.project_id):
            if duration is None:
                probe_result = self._probe.probe(self._source_local_path(job))
                if not probe_result.valid or not probe_result.duration:
                    return JobStepResult(
                        target_status=JOB_FAILED,
                        data={"reason": "probe_invalid", "error_code": probe_result.error_code},
                    )
                duration = float(probe_result.duration)
            try:
                self._timeline.initialize_timeline(job.project_id, duration_seconds=duration)
            except TimelineEditorError:
                # Corrida genuína: uma instância CONCORRENTE já
                # inicializou a timeline entre a checagem acima e esta
                # chamada (TimelineJaInicializadaError) -- benigno, nunca
                # falha o Job; simplesmente segue com a timeline que a
                # outra instância já criou (seção 0.9 da docstring do
                # módulo).
                pass

        removed: "list[tuple[float, float]]" = []
        skipped: "list[dict[str, Any]]" = []
        for start, end in candidate_intervals:
            segments = self._timeline.get_timeline(job.project_id)
            target = None
            for segment in segments:
                if segment.start <= start and end <= segment.end:
                    target = segment
                    break
            if target is None:
                skipped.append({"start": start, "end": end, "reason": "not_contained_in_single_segment"})
                continue
            try:
                self._timeline.remove_range(job.project_id, target.segment_id, start, end)
                removed.append((start, end))
            except TimelineEditorError:
                skipped.append({"start": start, "end": end, "reason": "race_condition"})

        self._audit_log.record_checkpoint(
            job.id,
            CHECKPOINT_EDIT_PLANNED,
            data={"cache_hit": cache_hit, "cache_key": cache_key, "removed_count": len(removed)},
        )
        return JobStepResult(
            target_status=JOB_READY,
            semantic_event=AUDIT_JOB_PROCESSING_COMPLETED,
            data={
                "cache_hit": cache_hit,
                "cache_key": cache_key,
                "artifact_id": artifact.id,
                "removed_intervals": removed,
                "skipped_intervals": skipped,
            },
        )

    def _source_local_path(self, job: Job) -> str:
        video = self._database.get(Video, job.video_id)
        source = self._database.get(SourceAsset, video.source_asset_id)
        return str(source.local_path)

    def _cancel_requested(self, job_id: str) -> bool:
        if self._control_manager is None:
            return False
        return self._control_manager.is_job_cancel_requested(job_id)

    def _find_cached_artifact(self, video_id: str, cache_key: str) -> "Artifact | None":
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT id FROM artifacts WHERE video_id = ? AND kind = ? AND fingerprint = ? "
                "ORDER BY created_at LIMIT 1",
                (video_id, ARTIFACT_KIND_SILENCE_TRACK, cache_key),
            ).fetchone()
        if row is None:
            return None
        return self._database.get(Artifact, row[0])

    def _read_candidate_intervals(self, artifact: Artifact) -> "list[tuple[float, float]]":
        payload = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
        return [(float(item["start"]), float(item["end"])) for item in payload.get("candidate_intervals", [])]

    def _final_path_for(self, video_id: str, cache_key: str) -> Path:
        return Path(self._app_paths.projects) / "silence_removal" / video_id / f"{cache_key}.json"

    def _write_and_register_artifact(
        self, job: Job, cache_key: str, candidate_intervals: "list[tuple[float, float]]"
    ) -> Artifact:
        payload = {
            "version": 1,
            "detector_backend_version": DETECTOR_BACKEND_VERSION,
            "candidate_intervals": [{"start": s, "end": e} for s, e in candidate_intervals],
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2)

        temp_path = self._storage.allocate_temp(suffix="_silence.json", create=True)
        try:
            temp_path.write_text(content, encoding="utf-8")
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

        final_path = self._final_path_for(job.video_id, cache_key)
        try:
            promoted = self._storage.promote_to_final(temp_path, final_path, overwrite=True)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

        artifact = Artifact(
            video_id=job.video_id,
            project_id=job.project_id,
            job_id=job.id,
            kind=ARTIFACT_KIND_SILENCE_TRACK,
            path=str(promoted),
            fingerprint=cache_key,
            size_bytes=len(content.encode("utf-8")),
        )
        self._database.insert(artifact)
        return artifact


__all__ = [
    "SILENCE_REMOVAL",
    "THRESHOLD_DB_RANGE",
    "MINIMUM_DURATION_SECONDS_RANGE",
    "PADDING_SECONDS_RANGE",
    "DEFAULT_THRESHOLD_DB",
    "DEFAULT_MINIMUM_DURATION_SECONDS",
    "DEFAULT_PADDING_SECONDS",
    "OPERATION_SILENCE_REMOVAL",
    "ARTIFACT_KIND_SILENCE_TRACK",
    "DETECTOR_BACKEND_VERSION",
    "SilenceRemovalError",
    "CampoInvalidoError",
    "SilenceRemovalConfigState",
    "SilenceDetectionBackend",
    "compute_cache_key",
    "SilenceRemovalEngine",
]

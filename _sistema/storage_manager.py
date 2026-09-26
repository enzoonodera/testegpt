# -*- coding: utf-8 -*-
"""StorageManager central — segurança de disco/armazenamento (PROMPT 19).

ESCOPO DELIBERADO: O QUE O STORAGEMANAGER NÃO É
--------------------------------------------------
Assim como ``ResourceManager`` (Prompt 18) é um controlador de CAPACIDADE de
CPU/RAM/GPU em memória, ``StorageManager`` é um controlador de CAPACIDADE de
DISCO em memória, e nada além disso. Ele explicitamente NÃO é:

- uma segunda fila/State Machine de Job (``JobEngine``/``OperationalAuditLog``
  continuam sendo a única autoridade sobre ``Job.status``);
- um mecanismo de persistência (reservas são runtime-only — ver "POR QUE
  NENHUMA TABELA SQLITE" abaixo);
- um pipeline de FFmpeg/render, um scanner de startup automático, um
  Circuit Breaker ou um framework genérico de retry;
- um scheduler de limpeza automática (nada aqui roda sozinho em background
  nesta etapa; ``build_cleanup_plan``/``execute_cleanup`` só agem quando um
  chamador explícito os invoca).

CONTRATO CENTRAL — "PROTECTED WINS" (invariante absoluta desta etapa)
-----------------------------------------------------------------------
``StorageManager`` nunca apaga, sobrescreve ou modifica um arquivo que o
chamador classificou/registrou como SOURCE, FINAL_ARTIFACT, DATABASE,
BACKUP, BROWSER_PROFILE, MODEL, TEMPLATE, USER_FILE, LOG, ou um arquivo
desconhecido/não classificado — independentemente de qual categoria um
candidato de limpeza tente reivindicar para o MESMO caminho, independente de
pressão de disco cheio, independente de flags de limpeza agressiva. Em caso
de dúvida, o arquivo é sempre pulado, nunca apagado.

CORREÇÃO PÓS-1ª REVISÃO ADVERSARIAL — CATEGORIA ALEGADA NUNCA É AUTORIDADE
-----------------------------------------------------------------------------
A 1ª revisão adversarial reproduziu que ``CleanupCandidate.category`` (a
categoria que o CHAMADOR alega) era, na prática, tratada como suficiente
para apagar um arquivo, inclusive arquivos reais dentro de
``backups``/``models``/``templates``/``projects``/``accounts`` só porque o
chamador mentiu ``category=TEMP``. Isso foi corrigido: a decisão de apagar
passa a ter uma única autoridade central, ``_authorize_cleanup_locked``,
chamada tanto por ``build_cleanup_plan`` (planejamento) quanto por
``execute_cleanup`` (revalidação/execução), com esta ORDEM DE PRECEDÊNCIA
estrita (nenhum nível posterior nunca vence um anterior):

1. fora dos managed roots -> recusado;
2. estruturalmente protegido (``painel.db``/``-wal``/``-shm``) -> recusado;
3. registrado como protegido (``register_protected_path``) -> recusado;
4. caminho ativo (``mark_active``/``active_path``) -> recusado;
5. já reivindicado por outra execução de limpeza concorrente -> recusado;
6. POLÍTICA DE ROOT: o managed root mais específico que contém o caminho
   precisa ter categoria estrutural em ``CLEANABLE_CATEGORIES`` (hoje só
   ``cache``/``temp``) — roots como ``database``/``accounts``/``projects``/
   ``logs``/``templates``/``backups``/``models``/``support``/
   ``removed_accounts`` SEMPRE recusam limpeza aqui, INDEPENDENTEMENTE da
   categoria alegada pelo chamador; esta etapa não implementa nenhuma
   sub-root "disposable" que escape dessa regra;
7. PROVA DE OWNERSHIP: precisa existir um registro (``allocate_temp`` ou
   ``register_disposable_path``) marcando o caminho resolvido como
   descartável — um arquivo desconhecido dentro de ``cache``/``temp``, sem
   esse registro, é SEMPRE preservado ("está em temp, então é nosso" nunca
   é presumido).

A categoria que o candidato alega (``CleanupCandidate.category``) só decide
se o item é sequer considerado (precisa estar em ``CLEANABLE_CATEGORIES``
para entrar no funil acima) — a categoria EFETIVA usada no ``CleanupPlanItem``
e reportada é sempre a do registro de ownership, nunca a alegada.

``promote_to_final`` recebeu a mesma correção do lado da ORIGEM
(``temp_path``), não só do destino — ver docstring do próprio método
(BLOQUEADOR 2 da 1ª revisão: um SOURCE registrado não podia mais ser
protegido no destino, mas podia ser passado como ``temp_path`` e ser movido
via ``os.replace``; agora ``temp_path`` também precisa estar dentro dos
managed roots, não ser protegido, e possuir ownership com categoria em
``PROMOTABLE_TEMP_CATEGORIES``).

LINEARIZAÇÃO CLEANUP VS ACTIVE/PROTECTED (BLOQUEADOR 3 da 1ª revisão)
-----------------------------------------------------------------------
Havia uma janela TOCTOU entre "``execute_cleanup`` decidiu que o arquivo não
está protegido/ativo" e "``execute_cleanup`` de fato apaga o arquivo": outra
thread podia chamar ``mark_active``/``register_protected_path`` nessa janela
e o cleanup ainda assim apagava o arquivo "recém-protegido". Corrigido com
um "cleanup claim" (``self._cleanup_claims``, um ``set[Path]`` protegido
pelo mesmo ``self._lock``): ``execute_cleanup`` decide E reivindica o
caminho na MESMA aquisição de lock (``_claim_cleanup``), antes de soltar o
lock para fazer qualquer I/O real; ``mark_active``/``register_protected_path``
checam esse mesmo conjunto, sob o mesmo lock, e recusam explicitamente
(``StorageCleanupError``, ``reason="cleanup_claim_in_progress"``) se um
cleanup já reivindicou aquele caminho — nunca um sucesso silencioso seguido
de delete. Quem adquire o lock primeiro decide o resultado, em ambas as
direções. O claim é liberado no ``finally`` de ``execute_cleanup``, e o lock
nunca é mantido durante ``stat``/``unlink`` reais.

LINEARIZAÇÃO DA PROMOÇÃO (bloqueador da 3ª revisão adversarial)
-----------------------------------------------------------------------
A mesma classe de corrida do parágrafo acima existia em ``promote_to_final``:
havia uma janela entre "validou proteção/ownership/categoria" e o
``os.replace``/``_cross_volume_publish`` físico onde outra thread podia
``register_protected_path`` o destino, ou ``register_disposable_path``
reclassificar a origem para uma categoria não promovível, ou um
``execute_cleanup`` concorrente reivindicar a origem — e a promoção antiga
ainda assim publicava por cima, violando "PROTECTED WINS" e a
autoridade/geração de ownership.

Corrigido com um "promotion claim" (``self._promotion_claims``, um
``set[Path]`` protegido pelo MESMO ``self._lock`` de tudo mais neste
módulo): ``_claim_promotion`` decide TODA a autorização — destino não
protegido/fora de root/já reivindicado, origem não protegida/fora de
root/root-policy/já reivindicada por cleanup ou por outra promoção,
ownership presente com categoria promovível — E reivindica atomicamente
AMBOS os caminhos (origem e destino) na MESMA aquisição de lock, antes de
soltar o lock para qualquer I/O real (``stat``, callback ``validate``,
``os.replace``, ``shutil.copy2``). ``mark_active``/
``register_protected_path``/``register_disposable_path`` e
``_authorize_cleanup_locked`` (usada por ``build_cleanup_plan``/
``execute_cleanup``) todos checam ``self._promotion_claims`` sob o mesmo
lock e recusam explicitamente (``reason="promotion_claim_in_progress"``) se
uma promoção já reivindicou aquele caminho — nunca um sucesso silencioso
seguido da promoção publicar/apagar por cima. Na direção oposta, se
proteção/reclassificação/cleanup-claim ganhar o lock primeiro, é a PRÓPRIA
``_claim_promotion`` que recusa (a promoção nunca chega a reivindicar nada,
então nunca publica). Quem adquire o lock primeiro decide o resultado, nas
duas direções, para os quatro tipos de operação concorrente (proteção,
reclassificação, cleanup, outra promoção).

Os claims de origem+destino são liberados juntos no ``finally`` de
``promote_to_final`` — inclusive quando ``validate`` recusa/lança, o temp
está ausente/vazio, a identidade diverge, o destino já existe sem
overwrite, ou o próprio ``os.replace``/``shutil.copy2`` falha. Nenhum claim
fica órfão bloqueando operações futuras.

``_consume_allocation`` (chamada após publicação bem-sucedida) foi
substituída por ``_consume_allocation_if_generation_matches``: só remove o
registro se ``allocation_id`` ainda for exatamente o mesmo que a promoção
validou — como o claim já impede qualquer reclassificação concorrente
durante a promoção, isso é defesa em profundidade (nunca consome uma
geração diferente da que foi de fato validada), não uma correção de uma
corrida que ainda existisse depois do claim.

Honestidade explícita: isto serializa apenas operações feitas ATRAVÉS deste
mesmo ``StorageManager``, dentro deste mesmo processo — exatamente a mesma
limitação já documentada em "MULTI-PROCESSO" acima. Se outro PROCESSO (fora
deste ``StorageManager``) substituir o arquivo de origem diretamente no
filesystem entre a validação e o ``os.replace``, a revalidação de
identidade (``_bind_or_check_identity``, chamada o mais próximo possível do
commit físico, depois do claim) tem uma chance best-effort de perceber via
``dev``/``ino``, mas isto não é — e nunca é apresentado como — uma garantia
cross-processo.

IDENTIDADE DE ARQUIVO NA REVALIDAÇÃO TOCTOU (BLOQUEADOR 4 da 1ª revisão)
-----------------------------------------------------------------------------
``size``+``mtime`` sozinhos não são identidade suficiente: um arquivo
substituído por outro com MESMO tamanho e MESMO mtime (forjado via
``os.utime``) não era detectado. ``CleanupPlanItem`` agora também guarda
``dev``/``ino`` (melhor esforço; sempre confiáveis em POSIX, no Windows é o
índice de arquivo do NTFS quando o Python o expõe) E ``ctime`` (o sinal mais
forte: o chamador não consegue forjá-lo como forja ``mtime`` via
``os.utime``, e ele avança mesmo quando o filesystem reaproveita o MESMO
número de inode para o arquivo novo — cenário real observado neste próprio
sandbox de teste). ``execute_cleanup`` compara TODOS — size, mtime, dev,
ino, ctime — imediatamente antes de apagar, pulando com
``"changed_since_plan"`` se qualquer um divergir. Isso NÃO
elimina a janela cross-processo (outro processo, fora deste
``StorageManager``, ainda pode trocar o arquivo entre o ``stat`` e o
``unlink`` — ver "MULTI-PROCESSO" abaixo); dentro do mesmo processo, o
cleanup claim acima já serializa contra os próprios escritores que passam
por este ``StorageManager``.

Dois mecanismos garantem a proteção absoluta, sempre avaliados ANTES de
qualquer decisão de apagar:

1. Registro explícito de proteção (``register_protected_path``): mapa em
   memória caminho-real-resolvido -> categoria. Um candidato de limpeza cujo
   caminho resolvido bate com uma entrada aqui é recusado, não importa qual
   categoria o PRÓPRIO candidato alega ter — o registro sempre vence.
2. Proteção estrutural implícita: ``painel.db``/``painel.db-wal``/
   ``painel.db-shm`` (a partir do caminho configurado, nunca hardcoded) e
   qualquer caminho que resolva para FORA de um managed root são sempre
   protegidos, mesmo sem registro explícito.

``build_cleanup_plan``/``execute_cleanup`` só consideram apagável um
candidato cuja categoria esteja em ``CLEANABLE_CATEGORIES`` — todas as
demais categorias (inclusive ``CATEGORY_UNKNOWN``) são recusadas só por
categoria, mesmo antes de checar o registro. "Dúvida" vira recusa, não
suposição de segurança.

RESERVAS SÃO RUNTIME-ONLY, NUNCA PERSISTIDAS (itens 13/14/15/32 do gate)
-----------------------------------------------------------------------
Deliberado, não esquecido: reservas de espaço (``reserve``/``reserve_compound``)
existem inteiramente em memória, protegidas por um único ``threading.Lock``.
Não existe tabela ``storage_reservations`` no SQLite, nenhuma migration nova
foi criada (m001/m002/m003 permanecem intocadas; nenhuma m004 existe).
Motivo: uma reserva é uma promessa de capacidade para uma operação que ainda
vai rodar NESTE processo, AGORA — ela não tem sentido sobreviver a um
crash/restart (o processo que ia consumir aquele espaço não existe mais) e
persistir/reidratar reservas fantasmas depois de um crash seria pior que não
persistir nada: um restart limpo já libera toda capacidade "presa" da
execução anterior, exatamente como ``ResourceManager`` faz com CPU/RAM/GPU.
O espaço em disco físico em si, obviamente, é sempre lido ao vivo via
``shutil.disk_usage`` — só a CONTABILIDADE de reservas concorrentes deste
processo é que é runtime-only.

MULTI-PROCESSO: SEM PROTEÇÃO CRUZADA (honestidade explícita)
-----------------------------------------------------------------
Este lock e este accounting protegem apenas reservas feitas DENTRO deste
mesmo processo/instância de ``StorageManager``. Duas instâncias de processos
diferentes do produto (ex.: dois workers, ou o app + uma ferramenta CLI)
NÃO enxergam as reservas uma da outra — cada uma só sabe do que ELA MESMA
reservou. ``available_for_app`` sempre relê ``shutil.disk_usage`` ao vivo, o
que dá alguma proteção indireta (a outra instância também vê o espaço físico
já consumido pela primeira, uma vez que os bytes tenham sido efetivamente
escritos), mas a MARGEM de segurança e o orçamento de "quanto ainda cabe"
não são coordenados entre processos. Isso não é um bug escondido: é uma
limitação documentada deste Prompt. Um lock distribuído entre processos está
fora de escopo aqui (ver "FORA DE ESCOPO" abaixo).

LOCKS (REVIEW DE LOCKS OBRIGATÓRIA — item do CLAUDE.md)
-----------------------------------------------------------
Um único ``threading.Lock`` (``self._lock``) protege TODO o estado mutável
em memória deste módulo: reservas por volume (``_reserved_by_volume``),
amostra de caminho por volume para ``snapshot`` (``_volume_sample_path``),
reservas ativas (``_active_reservations``), caminhos ativos/"em uso"
(``_active_paths``), registro de proteção (``_protected_paths``) e registro
de alocações de temp (``_allocations``). Regras:

- quem adquire: qualquer thread chamando ``reserve``/``reserve_compound``/
  ``StorageReservation.release``/``active_path``/``mark_active``/
  ``mark_inactive``/``register_protected_path``/``allocate_temp``/
  ``snapshot``/os métodos internos de proteção.
- este lock NUNCA é mantido durante I/O de sistema de arquivos
  (``shutil.disk_usage``, ``stat``, ``unlink``, ``os.replace``,
  ``shutil.copy2``, varredura de diretório) nem durante nenhum callback
  externo (``validate`` de ``promote_to_final``). Em ``reserve``, a chamada
  de ``disk_usage`` (syscall) é feita ANTES de adquirir o lock; a checagem
  "essa nova reserva ainda cabe no available atual" + o incremento do
  contador acontecem juntos, atomicamente, DENTRO do lock — é exatamente
  essa combinação que impede a corrida de overcommit entre reservas
  concorrentes (ver teste adversarial de overcommit).
- só existe este lock neste módulo. Ele nunca é adquirido junto de nenhum
  lock/transaction de ``LocalDatabase``/``ControlManager``/
  ``ShutdownCoordinator``/``JobEngine``/``BatchEngine``/``ResourceManager``
  — este módulo nunca abre uma transaction SQLite e nenhum desses módulos é
  chamado de dentro da seção crítica deste lock. Não há, portanto, caminho
  conhecido de deadlock/ordem circular com os locks já aprovados.

ORDEM RECOMENDADA COM RESOURCEMANAGER (documentação, sem integração real)
-----------------------------------------------------------------------------
Este Prompt NÃO integra ``StorageManager`` com ``JobEngine``/``BatchEngine``/
``ResourceManager``/``ControlManager``/``ShutdownCoordinator`` — nenhum
desses arquivos foi tocado. Para uma etapa futura que precise das duas
proteções (espaço em disco E capacidade de CPU/RAM/GPU) ao mesmo tempo, a
recomendação documentada aqui é:

    1. ``StorageManager.reserve(...)`` (preflight de espaço) primeiro —
       falhar cedo por falta de disco é mais barato que já ter tomado um
       slot de CPU/GPU para nada.
    2. ``ResourceManager.acquire_for(...)`` em seguida.
    3. Executar o trabalho pesado.
    4. Liberar a lease de recurso (``ResourceLease.release``).
    5. Liberar a reserva de armazenamento (``StorageReservation.release``)
       por último, só depois que o resultado final já foi
       publicado/promovido (ver ``promote_to_final``).

Essa ordem evita seguramente disco reservado + recurso pesado nunca
adquirido (ao contrário: nunca acontece o inverso, recurso pesado rodando
sem nenhuma garantia de espaço). Nenhum código deste Prompt impõe essa
ordem — é só uma recomendação para quem vier integrar depois.

DIFERENÇAS DE PLATAFORMA / WINDOWS (item 11 do gate)
-----------------------------------------------------------------
Este sandbox de desenvolvimento é Linux; o produto final é Windows (ver
``CLAUDE.md``). Onde há diferença relevante:

- Symlinks/junctions: o código nunca segue um link ao decidir apagar (usa
  ``Path.is_symlink()``/``os.walk(..., followlinks=False)`` e pula qualquer
  entrada marcada como link). No Windows, ``DirEntry.is_symlink()``/
  ``os.path.islink`` também detectam link simbólico NTFS, mas reparse
  points "puros" (ex.: junctions criadas via ``mklink /J``, ou pontos de
  montagem OneDrive) têm semântica própria do NTFS que este sandbox Linux
  não pode reproduzir fielmente — os testes deste módulo provam a LÓGICA de
  path-safety (nunca seguir, sempre pular por segurança), não o
  comportamento exato do NTFS para cada tipo de reparse point. Validação
  manual em Windows real continua necessária antes de release.
- ``os.replace``/``os.rename`` são atômicos INCONDICIONALMENTE em POSIX —
  mesmo sob concorrência real, ``rename()`` nunca falha por outra chamada
  concorrente mirando o mesmo destino. **CORRIGIDO** (a afirmação anterior
  aqui estava factualmente incorreta — provado pela evidência abaixo, não
  apenas suposição): em Windows, para o MESMO destino, sob concorrência
  REAL (duas chamadas genuinamente simultâneas de ``os.replace`` visando o
  mesmo ``final_path``), a chamada pode falhar TRANSITORIAMENTE com
  ``PermissionError``/``WinError 5`` — reproduzido no Windows real do dono
  do produto (Windows 10 19045) via
  ``tests/test_captions_engine.py::
  test_duas_transcricoes_concorrentes_do_mesmo_video_e_config_nao_quebram``
  (duas execuções concorrentes do MESMO ``Job`` de transcrição, mesmo
  ``cache_key``, ambas promovendo para o MESMO ``final_path``
  determinístico) e por um script de diagnóstico isolado. Causa raiz: a
  "LINEARIZAÇÃO DA PROMOÇÃO" via ``self._lock``/``_claim_promotion`` é um
  ``threading.Lock`` POR INSTÂNCIA de ``StorageManager`` — duas instâncias
  concorrentes (duas execuções "simulando dois processos", mesmo padrão
  já usado em toda a suíte) têm ``self._lock`` OBJETOS DIFERENTES, que não
  se protegem mutuamente; a suposição de que ``os.replace`` sozinho já
  bastava para o caso mesmo-volume estava incorreta. Corrigido:
  ``promote_to_final`` agora envolve essa chamada num retry curto e
  limitado (``_replace_with_bounded_retry``, poucas tentativas, backoff
  total de poucas dezenas de milissegundos — NUNCA um framework de retry
  genérico) que cobre essa janela transitória sem nunca engolir uma falha
  de permissão persistente real. Entre volumes diferentes, nunca
  reivindicamos atomicidade: copiamos para um arquivo de staging no volume
  destino primeiro (``shutil.copy2``), e só então fazemos ``os.replace``
  dentro do MESMO volume destino (ver ``_cross_volume_publish``) — esse
  ``os.replace`` final também passa pelo mesmo retry curto, pelo mesmo
  motivo.
- ``st_nlink`` (hardlinks) e reparse points/junctions têm comportamento
  próprio no NTFS; a checagem de conservadorismo por hardlink
  (``st_nlink > 1`` -> pular) é a mesma em ambas plataformas via
  ``os.stat``, mas o significado exato de "hardlink" para uma junction
  específica no NTFS deve ser revalidado manualmente no Windows real.
- ``promote_to_final`` documenta explicitamente que NÃO pode verificar se o
  chamador já fechou todos os handles do arquivo temporário antes de
  chamar — no Windows, um handle aberto pode fazer ``os.replace``/
  ``rename`` falhar com sharing violation (``OSError``/``PermissionError``);
  isso é responsabilidade do chamador, documentada aqui, nunca escondida.

FORA DE ESCOPO NESTA ETAPA (declarado explicitamente, nunca implementado)
-------------------------------------------------------------------------------
Circuit Breaker; framework genérico de retry (só um retry curto e limitado,
1-2 tentativas, para uma única operação de delete — não é um framework);
pipeline de FFmpeg/render; UI; scan automático de temp órfão no startup que
apague sozinho (classificar sim, no futuro; apagar sem prova de dono +
período de graça, não); poda automática de backup; limpeza de perfil de
navegador; limpeza de modelo; rotação agressiva de log; nova tabela SQLite
ou migration (ver acima); lock distribuído entre processos/máquinas (ver
acima).

DESVIOS DO DESENHO SUGERIDO PELO PROMPT (documentados, não escondidos)
-----------------------------------------------------------------------------
- Symlink como candidato de limpeza: o Prompt permite decidir apagar o link
  em si (nunca o alvo) como alternativa. Optamos pela política mais
  conservadora possível para v1: qualquer candidato cujo caminho literal seja
  um symlink é sempre pulado (nunca apagado, nem o link nem o alvo) — ver
  "symlink_candidate_skipped" em ``build_cleanup_plan``/``execute_cleanup``.
  Apagar o link em si fica como limitação conhecida/dívida técnica.
- Remoção de diretórios agora-vazios após limpeza NÃO foi implementada
  nesta etapa (o Prompt marcava isso como opcional). ``execute_cleanup``
  cuida apenas de arquivos regulares; diretórios órfãos ficam para uma
  etapa futura, documentado aqui como dívida técnica.
- ``allocate_temp`` NÃO cria o arquivo no disco por padrão (``create=False``)
  — só reserva o nome/registro em memória. Passar ``create=True`` cria um
  arquivo vazio. Ambos os caminhos são exercitados nos testes.

ORPHAN TEMP / GRACE PERIOD — DECISÃO EXPLÍCITA (BLOQUEADOR 6 da 1ª revisão)
-------------------------------------------------------------------------------
A 1ª revisão adversarial exigiu escolher, sem ambiguidade, entre (A) um
manifest crash-safe de ownership persistido em disco, ou (B) preservar TODO
órfão nesta versão e declarar honestamente que limpeza automática de órfão
não está implementada. Escolhemos (B), deliberadamente, para não introduzir
um novo formato de arquivo/serialização persistida sem necessidade
demonstrada nesta etapa (ver "Não fazer overengineering" no CLAUDE.md) — e
porque a correção do BLOQUEADOR 1 (ownership é sempre exigido, nunca
presumido por localização) já torna essa escolha segura por construção, não
apenas por promessa:

- Ownership (``self._allocations``) é inteiramente RUNTIME-ONLY, como já
  documentado acima para reservas. Um restart cria uma NOVA instância de
  ``StorageManager`` com ``self._allocations`` vazio.
- Como ``_authorize_cleanup_locked`` (passo 7) SEMPRE exige um registro de
  ownership antes de autorizar limpeza, qualquer arquivo temp que sobreviva
  a um restart perde automaticamente sua prova de ownership e fica
  PRESERVADO — nunca "porque está em temp, presumimos que é nosso". Não
  existe nenhum scanner de startup que reclassifique arquivos órfãos como
  descartáveis: se um futuro Prompt quiser implementar reconciliação de
  startup, ela precisará re-registrar ownership explicitamente (via
  ``register_disposable_path``) para cada arquivo que reconhecer, sujeito
  às mesmas regras de política de root acima — nada nesta etapa faz isso
  sozinho.
- ``build_cleanup_plan`` aceita um ``min_age_seconds`` opcional (grace
  period best-effort baseado no ``mtime`` do arquivo, dentro do MESMO
  processo/instância) para o caso de uso "descartável mas ainda recente
  demais para apagar com segurança" — não é um mecanismo de startup/crash,
  é só uma restrição adicional que quem chama pode aplicar.
- Isso significa que testes nomeados "orphan sem ownership → preservado" e
  "restart não transforma unknown em disposable" são verdadeiros por
  CONSTRUÇÃO (efeito colateral direto do BLOQUEADOR 1), e são exercitados
  usando uma NOVA instância de ``StorageManager``/``AppPaths`` para simular
  o restart de verdade — nunca reutilizando o objeto em memória (ver item 4
  do gate adversarial obrigatório no ``CLAUDE.md``).
"""
from __future__ import annotations

import errno
import os
import shutil
import stat as stat_module
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace as _dc_replace
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping
from uuid import uuid4

from .app_paths import AppPaths
from .storage.database import DEFAULT_DATABASE_FILENAME


# ---------------------------------------------------------------------------
# Categorias (item: categorias + PROTECTED/CLEANABLE)
# ---------------------------------------------------------------------------

CATEGORY_SOURCE = "SOURCE"
CATEGORY_FINAL_ARTIFACT = "FINAL_ARTIFACT"
CATEGORY_INTERMEDIATE_DISPOSABLE = "INTERMEDIATE_DISPOSABLE"
CATEGORY_INTERMEDIATE_RECOVERABLE = "INTERMEDIATE_RECOVERABLE"
CATEGORY_TEMP = "TEMP"
CATEGORY_CACHE = "CACHE"
CATEGORY_FRAME_CACHE = "FRAME_CACHE"
CATEGORY_MODEL = "MODEL"
CATEGORY_DATABASE = "DATABASE"
CATEGORY_BACKUP = "BACKUP"
CATEGORY_LOG = "LOG"
CATEGORY_BROWSER_PROFILE = "BROWSER_PROFILE"
CATEGORY_TEMPLATE = "TEMPLATE"
CATEGORY_USER_FILE = "USER_FILE"
CATEGORY_UNKNOWN = "UNKNOWN"

CATEGORIES = frozenset(
    {
        CATEGORY_SOURCE,
        CATEGORY_FINAL_ARTIFACT,
        CATEGORY_INTERMEDIATE_DISPOSABLE,
        CATEGORY_INTERMEDIATE_RECOVERABLE,
        CATEGORY_TEMP,
        CATEGORY_CACHE,
        CATEGORY_FRAME_CACHE,
        CATEGORY_MODEL,
        CATEGORY_DATABASE,
        CATEGORY_BACKUP,
        CATEGORY_LOG,
        CATEGORY_BROWSER_PROFILE,
        CATEGORY_TEMPLATE,
        CATEGORY_USER_FILE,
        CATEGORY_UNKNOWN,
    }
)

CLEANABLE_CATEGORIES = frozenset(
    {CATEGORY_TEMP, CATEGORY_CACHE, CATEGORY_FRAME_CACHE, CATEGORY_INTERMEDIATE_DISPOSABLE}
)

# Tudo que não é explicitamente descartável é protegido — inclusive
# CATEGORY_UNKNOWN (dúvida vira proteção, nunca permissão para apagar) e
# CATEGORY_INTERMEDIATE_RECOVERABLE (reaproveitável, nunca descartável sem
# decisão explícita de outra camada).
PROTECTED_CATEGORIES = frozenset(CATEGORIES - CLEANABLE_CATEGORIES)

# Categorias cuja ownership autoriza um caminho a ser usado como ORIGEM
# (``temp_path``) de ``promote_to_final`` (BLOQUEADOR 2 da 1ª revisão
# adversarial). Deliberadamente mais estrito que ``CLEANABLE_CATEGORIES``:
# CACHE/FRAME_CACHE são conteúdo regenerável, não o produto de um trabalho
# que está prestes a virar entrega final — promovê-los soaria um bug de
# outra camada, então não entram aqui.
PROMOTABLE_TEMP_CATEGORIES = frozenset(
    {CATEGORY_TEMP, CATEGORY_INTERMEDIATE_DISPOSABLE, CATEGORY_INTERMEDIATE_RECOVERABLE}
)


# Retry curto e limitado em torno do ``os.replace`` de mesmo-volume em
# ``promote_to_final`` (correção pós-evidência real de Windows — ver
# ``_replace_with_bounded_retry``): NÃO é um framework de retry genérico,
# só cobre uma janela de corrida transitória e curta.
_FINAL_REPLACE_RETRY_ATTEMPTS = 3
_FINAL_REPLACE_RETRY_BACKOFF_SECONDS = 0.01

# Mensagem/recuperabilidade para cada ``reason`` que ``_promotion_authorize_
# locked`` pode devolver quando recusa um claim de promoção (bloqueador da
# 3ª revisão adversarial) — usada por ``promote_to_final`` para montar o
# ``StoragePublishError`` sem duplicar as mensagens em dois lugares.
_PROMOTION_CLAIM_FAILURE_INFO: dict[str, tuple[str, bool]] = {
    "destination_protected": (
        "destino é um caminho protegido; promoção recusada mesmo com overwrite=True",
        False,
    ),
    "destination_outside_managed_roots": ("destino fora dos managed roots", False),
    "temp_outside_managed_roots": ("origem (temp) fora dos managed roots", False),
    "temp_is_protected": (
        "origem (temp) é um caminho protegido (SOURCE/FINAL_ARTIFACT/"
        "DATABASE/BACKUP/etc.); promoção recusada mesmo com overwrite=True",
        False,
    ),
    "temp_root_not_cleanable": (
        "origem (temp) está fora de um managed root estruturalmente "
        "promovível (política de root recusa, independente de qualquer "
        "registro de ownership existente)",
        False,
    ),
    "temp_not_owned": (
        "origem (temp) não possui prova de ownership do StorageManager "
        "(nunca alocada via allocate_temp/register_disposable_path); "
        "promoção recusada",
        True,
    ),
    "temp_category_not_promotable": (
        "origem (temp) possui ownership, mas a categoria registrada não é promovível",
        True,
    ),
    "cleanup_claim_in_progress": (
        "não é possível promover: uma limpeza já está em andamento para a "
        "origem ou o destino (cleanup claim ativo)",
        True,
    ),
    "promotion_claim_in_progress": (
        "não é possível promover: outra promoção já reivindicou a origem "
        "ou o destino (promotion claim ativo)",
        True,
    ),
    "source_equals_destination": (
        "origem e destino resolvem para o mesmo caminho físico; promoção "
        "recusada — nunca consome a ownership de um arquivo que "
        "permaneceria parado no mesmo pathname",
        True,
    ),
    "destination_active": (
        "não é possível promover: o destino está marcado como ativo "
        "(active_path/mark_active) — arquivo em uso não pode ser "
        "sobrescrito por uma promoção",
        True,
    ),
    "temp_active": (
        "não é possível promover: a origem (temp) está marcada como "
        "ativa (active_path/mark_active) — arquivo em uso não pode ser "
        "movido/consumido por uma promoção",
        True,
    ),
    "destination_root_cleanable": (
        "destino está dentro de um managed root estruturalmente "
        "cleanable (temp/cache) — promote_to_final nunca publica dentro "
        "de um root que existe para ser limpo/gerenciado, isso criaria "
        "um arquivo órfão sem ownership",
        False,
    ),
}


# ---------------------------------------------------------------------------
# Erros estruturados
# ---------------------------------------------------------------------------


class StorageError(Exception):
    """Base de erro do StorageManager. Carrega atributos estruturados —
    nunca ``str(exc)``/``repr(exc)``/traceback de uma exceção capturada
    internamente (item 10 do CLAUDE.md)."""

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        category: str | None = None,
        volume: str | None = None,
        required_bytes: int | None = None,
        available_bytes: int | None = None,
        recoverable: bool = True,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.category = category
        self.volume = volume
        self.required_bytes = required_bytes
        self.available_bytes = available_bytes
        self.recoverable = recoverable
        self.reason = reason


class InsufficientStorageError(StorageError):
    """``reserve``/``reserve_compound`` recusado: capacidade insuficiente."""


class StorageVolumeUnavailableError(StorageError):
    """``disk_usage`` não conseguiu consultar o volume (nunca inventa
    números)."""


class UnsafeStoragePathError(StorageError):
    """Um caminho escapou (ou escaparia) dos managed roots."""


class StorageReservationError(StorageError):
    """Erro de contrato/accounting em reservas (nunca deixa um contador
    negativo nem "inventa" capacidade de volta)."""


class StorageCleanupError(StorageError):
    """Erro de contrato na etapa de limpeza (planejamento/execução)."""


class StoragePublishError(StorageError):
    """``promote_to_final`` recusado — proteção sempre vence, mesmo com
    ``overwrite=True``."""


# ---------------------------------------------------------------------------
# Dataclasses de valor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiskUsage:
    total: int
    used: int
    free: int


@dataclass(frozen=True)
class SafetyMarginPolicy:
    """Margem de segurança centralizada e injetável — nenhum número mágico
    espalhado pelo resto do módulo.

    ``min_absolute_bytes`` (default 2 GiB): piso absoluto conservador o
    bastante para não deixar um disco pequeno/quase cheio operar "no fio da
    navalha", mas pequeno o bastante para não travar discos pequenos
    legítimos por completo.
    ``percent_of_total`` (default 5%): em discos grandes, uma margem fixa em
    bytes fica pequena demais relativa ao volume — 5% cresce com o disco sem
    reservar uma fração absurda.

    A margem efetiva é sempre ``max(min_absolute_bytes, percent_of_total *
    total)`` — o maior dos dois nunca deixa a margem cair abaixo do piso
    absoluto em discos pequenos, nem abaixo de uma fração razoável em
    discos grandes.
    """

    min_absolute_bytes: int = 2 * 1024 ** 3
    percent_of_total: float = 0.05

    def compute(self, total_bytes: int) -> int:
        total_bytes = max(0, int(total_bytes))
        return max(int(self.min_absolute_bytes), int(self.percent_of_total * total_bytes))


@dataclass(frozen=True)
class StorageRequirement:
    """Um requisito de espaço para ``reserve_compound``. ``estimated_bytes``
    pode ser ``None`` para representar UNKNOWN explicitamente — isso NUNCA é
    tratado silenciosamente como 0 (ver ``StorageManager.reserve_compound``:
    ``None`` sempre levanta ``StorageReservationError``, nunca reserva 0
    bytes silenciosamente)."""

    path_or_volume: Any
    estimated_bytes: int | None
    category: str | None = None
    temporary: bool = False


@dataclass(frozen=True)
class CleanupCandidate:
    path: Any
    category: str
    reason: str
    expected_bytes: int | None = None


@dataclass(frozen=True)
class CleanupPlanItem:
    path: Path
    category: str
    reason: str
    size: int
    mtime: float
    # Identidade adicional do arquivo (BLOQUEADOR 4 da 1ª revisão
    # adversarial): size+mtime sozinhos são insuficientes — um arquivo
    # substituído por outro de MESMO tamanho e MESMO mtime forjado não é
    # detectado só por eles. ``dev``/``ino`` são melhor-esforço (sempre
    # disponíveis em POSIX; no Windows, o índice de arquivo do NTFS quando o
    # Python o fornecer) e nunca uma garantia cross-processo perfeita — ver
    # docstring do módulo.
    dev: int = 0
    ino: int = 0
    # Geração do registro de ownership usada para autorizar este item no
    # momento do plano — ``execute_cleanup`` revalida a autorização do zero
    # (nunca confia cegamente neste valor), mas o guarda para diagnóstico.
    allocation_id: str | None = None
    # ``st_ctime`` (POSIX: hora de mudança de metadado; Windows: hora de
    # criação) é o sinal mais forte dos quatro: ao contrário de
    # size/mtime/ino, o CHAMADOR não consegue forjá-lo (não existe
    # ``os.utime`` para ctime) — qualquer ``unlink``+recriação, mesmo que
    # reaproveite o MESMO número de inode (comum em filesystems com giro
    # rápido) e mesmo que ``mtime`` seja forjado de propósito para bater com
    # o antigo, sempre avança ``ctime`` para "agora". Ver teste adversarial
    # "mesmo size/mtime/inode reaproveitado, ctime denuncia a substituição".
    ctime: float = 0.0


@dataclass(frozen=True)
class CleanupPlan:
    items: tuple[CleanupPlanItem, ...]
    skipped: tuple[tuple[Path, str], ...]


@dataclass(frozen=True)
class CleanupResult:
    deleted: tuple[Path, ...]
    skipped: tuple[tuple[Path, str], ...]
    failed: tuple[tuple[Path, str], ...]
    bytes_freed: int


@dataclass(frozen=True)
class _AllocationRecord:
    category: str
    disposable: bool
    created_at: float
    pid: int
    label: str | None
    # BLOQUEADOR 2 da 2ª revisão adversarial ("ABA de identidade de
    # arquivo"): ``allocation_id`` identifica esta GERAÇÃO específica de
    # ownership (nunca reaproveitado); ``dev``/``ino``/``ctime`` são a
    # identidade física do arquivo capturada no momento em que a ownership
    # foi estabelecida, SE o arquivo já existia naquele momento (``None``
    # em todos os três quando ainda não existia — ex.:
    # ``allocate_temp(create=False)`` — nesse caso não há identidade física
    # para vincular ainda, e nenhuma é inventada). Quando a identidade está
    # capturada, ela precisa bater exatamente com o ``stat`` atual no
    # instante do uso (cleanup/promoção) — ver ``_identity_matches``.
    allocation_id: str
    dev: int | None = None
    ino: int | None = None
    ctime: float | None = None


# ---------------------------------------------------------------------------
# Helpers de path/volume (nível de módulo — deliberadamente monkeypatcháveis
# via o método de instância ``StorageManager._volume_key``; ver docstring do
# módulo e testes de "volumes independentes")
# ---------------------------------------------------------------------------


def _nearest_existing_ancestor(path: Path) -> Path:
    """Sobe a árvore até achar um ancestral que já existe no disco — usado
    tanto para ``disk_usage`` de um caminho que ainda não existe (ex.: um
    arquivo final que ainda vai ser criado) quanto para identidade de
    volume."""
    resolved = Path(path).resolve(strict=False)
    for candidate in (resolved, *resolved.parents):
        try:
            if candidate.exists():
                return candidate
        except OSError:
            continue
    anchor = resolved.anchor
    return Path(anchor) if anchor else resolved


def _volume_key(path: Any) -> str:
    """Identidade de volume best-effort: ``splitdrive`` primeiro (funciona
    para caminhos estilo Windows, ``C:\\...``), com fallback para
    ``st_dev`` do ancestral existente mais próximo em POSIX. Em Linux, isso
    normalmente reduz a "um único volume" para todo o sandbox de teste — por
    isso os testes que precisam provar isolamento entre volumes DIFERENTES
    monkeypatcham ``StorageManager._volume_key`` (ver docstring dos testes).
    """
    p = Path(path)
    drive, _tail = os.path.splitdrive(str(p))
    if drive:
        return drive.upper()
    ancestor = _nearest_existing_ancestor(p)
    try:
        return f"dev:{os.stat(ancestor).st_dev}"
    except OSError:
        return f"path:{ancestor}"


def _sanitize_suffix(suffix: str) -> str:
    """Sanitiza um sufixo/extensão fornecido pelo chamador (ex.: ``.mp4``)
    para uso em ``allocate_temp``: remove separadores de caminho e
    sequências ``..`` (nunca permite traversal via sufixo), preserva
    Unicode/acentuação do resto, e limita o tamanho."""
    suffix = str(suffix or "")
    suffix = suffix.replace("\\", "").replace("/", "")
    while ".." in suffix:
        suffix = suffix.replace("..", ".")
    return suffix[:32]


def classify_os_error(exc: OSError) -> str:
    """Classifica um ``OSError`` como disco-cheio ou não. Nunca confunde um
    ``PermissionError``/``OSError`` genérico (ex.: arquivo aberto por outro
    processo) com disco cheio — ver teste adversarial dedicado."""
    if not isinstance(exc, OSError):
        return "UNKNOWN"
    errno_value = getattr(exc, "errno", None)
    if errno_value in (errno.ENOSPC, getattr(errno, "EDQUOT", None)):
        return "DISK_FULL"
    if getattr(exc, "winerror", None) == 112:
        return "DISK_FULL"
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Reservas (runtime-only)
# ---------------------------------------------------------------------------


class StorageReservation:
    """Handle devolvido por ``StorageManager.reserve``. ``release()`` é
    idempotente: chamadas após a primeira são no-op seguro (nunca lança,
    nunca corrompe o accounting) — mesmo padrão de ``ResourceLease`` em
    ``resource_manager.py``."""

    __slots__ = (
        "manager",
        "reservation_id",
        "volume_key",
        "amount",
        "category",
        "label",
        "_released",
        "_release_lock",
    )

    def __init__(
        self,
        *,
        manager: "StorageManager",
        reservation_id: str,
        volume_key: str,
        amount: int,
        category: str | None,
        label: str | None,
    ) -> None:
        self.manager = manager
        self.reservation_id = reservation_id
        self.volume_key = volume_key
        self.amount = amount
        self.category = category
        self.label = label
        self._released = False
        self._release_lock = threading.Lock()

    @property
    def released(self) -> bool:
        with self._release_lock:
            return self._released

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
        self.manager._release_reservation(self)

    def __enter__(self) -> "StorageReservation":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.release()
        return False

    def __repr__(self) -> str:  # pragma: no cover - diagnóstico
        return (
            f"StorageReservation(id={self.reservation_id!r}, volume={self.volume_key!r}, "
            f"amount={self.amount!r}, released={self.released})"
        )


class CompoundReservation:
    """Reserva composta all-or-nothing entre múltiplos volumes/paths. Ver
    ``StorageManager.reserve_compound``."""

    __slots__ = ("reservations",)

    def __init__(self, reservations: tuple[StorageReservation, ...]) -> None:
        self.reservations = reservations

    def release(self) -> None:
        for reservation in self.reservations:
            reservation.release()

    def __enter__(self) -> "CompoundReservation":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.release()
        return False


# ---------------------------------------------------------------------------
# StorageManager
# ---------------------------------------------------------------------------


class StorageManager:
    """Controlador central de segurança de armazenamento em memória. Ver a
    docstring do módulo para o contrato completo (proteção absoluta,
    reservas runtime-only, locks, limitações multi-processo)."""

    def __init__(
        self,
        app_paths: AppPaths,
        *,
        database_path: Path | str | None = None,
        safety_margin: SafetyMarginPolicy | None = None,
    ) -> None:
        self._app_paths = app_paths
        self._safety_margin = safety_margin or SafetyMarginPolicy()

        self._managed_roots: tuple[Path, ...] = tuple(
            Path(root).resolve(strict=False) for root in app_paths.mutable_dirs()
        )

        db_path = (
            Path(database_path)
            if database_path is not None
            else Path(app_paths.database) / DEFAULT_DATABASE_FILENAME
        )
        db_resolved = db_path.resolve(strict=False)
        self._structural_protected: frozenset[Path] = frozenset(
            {
                db_resolved,
                db_resolved.with_name(db_resolved.name + "-wal"),
                db_resolved.with_name(db_resolved.name + "-shm"),
            }
        )

        # Único lock para todo o estado mutável em memória — ver "LOCKS" na
        # docstring do módulo.
        self._lock = threading.Lock()
        self._protected_paths: dict[Path, str] = {}
        self._reserved_by_volume: dict[str, int] = {}
        self._volume_sample_path: dict[str, Path] = {}
        self._active_reservations: dict[str, StorageReservation] = {}
        self._active_paths: dict[Path, int] = {}
        self._allocations: dict[Path, _AllocationRecord] = {}
        # "Cleanup claim": um caminho que um ``execute_cleanup`` em andamento
        # já decidiu (atomicamente, sob ``self._lock``) que vai apagar. Ver
        # "LINEARIZAÇÃO CLEANUP VS ACTIVE/PROTECTED" na docstring do módulo —
        # corrige o BLOQUEADOR 3 da 1ª revisão adversarial do Prompt 19.
        self._cleanup_claims: set[Path] = set()
        # "Promotion claim": os caminhos de ORIGEM e DESTINO que uma
        # ``promote_to_final`` em andamento já reivindicou atomicamente sob
        # ``self._lock`` — ver "LINEARIZAÇÃO DA PROMOÇÃO" na docstring do
        # módulo. Corrige o bloqueador da 3ª revisão adversarial: sem isso,
        # existia uma janela entre a validação e o ``os.replace``/
        # ``_cross_volume_publish`` onde outra chamada podia
        # proteger/reclassificar/apagar o mesmo caminho e a promoção antiga
        # ainda assim publicava por cima.
        self._promotion_claims: set[Path] = set()

    # -- Identidade / managed roots ---------------------------------------

    @property
    def managed_roots(self) -> tuple[Path, ...]:
        return self._managed_roots

    def _resolve_path(self, path: Any) -> Path:
        return Path(path).resolve(strict=False)

    def _inside_managed_roots(self, resolved: Path) -> bool:
        for root in self._managed_roots:
            if resolved == root or root in resolved.parents:
                return True
        return False

    def is_safe_managed_path(self, path: Any) -> bool:
        try:
            resolved = self._resolve_path(path)
        except OSError:
            return False
        return self._inside_managed_roots(resolved)

    def assert_safe_managed_path(self, path: Any) -> Path:
        resolved = self._resolve_path(path)
        if not self._inside_managed_roots(resolved):
            raise UnsafeStoragePathError(
                "caminho fora dos managed roots do produto",
                operation="path_safety",
                recoverable=False,
            )
        return resolved

    # -- Registro de proteção ---------------------------------------------

    def register_protected_path(self, path: Any, category: str) -> Path:
        if category not in PROTECTED_CATEGORIES:
            raise ValueError(f"categoria não é protegida: {category!r}")
        resolved = self._resolve_path(path)
        with self._lock:
            if resolved in self._cleanup_claims:
                # BLOQUEADOR 3 (1ª revisão adversarial): um cleanup já
                # comprometido a apagar este caminho está em andamento —
                # nunca permitir que o chamador pense que "protegeu" um
                # arquivo que está sendo removido nesse exato instante.
                # Falha explícita, nunca sucesso silencioso seguido de
                # delete.
                raise StorageCleanupError(
                    "não é possível registrar proteção: uma limpeza já está "
                    "em andamento para este caminho (cleanup claim ativo)",
                    operation="register_protected_path",
                    category=category,
                    recoverable=True,
                    reason="cleanup_claim_in_progress",
                )
            if resolved in self._promotion_claims:
                # Bloqueador da 3ª revisão adversarial: uma promoção em
                # andamento já reivindicou este caminho (origem ou
                # destino) — falha explícita em vez de "proteger" um
                # arquivo que a promoção ainda pode publicar/sobrescrever.
                raise StorageCleanupError(
                    "não é possível registrar proteção: uma promoção já "
                    "está em andamento para este caminho (promotion claim "
                    "ativo)",
                    operation="register_protected_path",
                    category=category,
                    recoverable=True,
                    reason="promotion_claim_in_progress",
                )
            self._protected_paths[resolved] = category
        return resolved

    def unregister_protected_path(self, path: Any) -> None:
        resolved = self._resolve_path(path)
        with self._lock:
            self._protected_paths.pop(resolved, None)

    def protected_category_of(self, path: Any) -> str | None:
        resolved = self._resolve_path(path)
        with self._lock:
            return self._protected_paths.get(resolved)

    def _is_structurally_protected(self, resolved: Path) -> bool:
        return resolved in self._structural_protected

    def _is_registered_protected(self, resolved: Path) -> bool:
        with self._lock:
            return resolved in self._protected_paths

    def _protection_skip_reason(self, resolved: Path) -> str | None:
        """Checagem de PROTEÇÃO pura (sem autoridade de limpeza) — usada por
        ``promote_to_final`` para o caminho de origem (``temp_path``) além
        das checagens estruturais/registradas já feitas explicitamente para
        o destino. NÃO é mais usada por ``build_cleanup_plan``/
        ``execute_cleanup`` — ver ``_authorize_cleanup_locked`` para a
        autoridade completa de limpeza (proteção + política de root +
        ownership), que corrige o BLOQUEADOR 1 da 1ª revisão adversarial."""
        if not self._inside_managed_roots(resolved):
            return "outside_managed_roots"
        if self._is_structurally_protected(resolved):
            return "structurally_protected"
        if self._is_registered_protected(resolved):
            return "registered_protected"
        if self._is_active(resolved):
            return "active_path"
        return None

    # -- Política de root / ownership (BLOQUEADOR 1 da 1ª revisão) ----------

    def _root_category_for(self, resolved: Path) -> str:
        """Categoria estrutural do managed root mais específico que contém
        ``resolved`` (maior número de segmentos de path vence, para lidar
        corretamente com roots aninhados). Retorna ``CATEGORY_UNKNOWN`` se
        ``resolved`` não estiver sob nenhum root mapeado (chamador deve
        checar ``_inside_managed_roots`` separadamente)."""
        root_map = self._root_category_map()
        best_category = CATEGORY_UNKNOWN
        best_len = -1
        for root, category in root_map.items():
            try:
                matches = resolved == root or root in resolved.parents
            except (OSError, ValueError):
                continue
            if not matches:
                continue
            length = len(root.parts)
            if length > best_len:
                best_len = length
                best_category = category
        return best_category

    def _authorize_cleanup_locked(
        self, resolved: Path, claimed_category: str
    ) -> tuple[bool, str | None, str | None, str | None]:
        """Autoridade ÚNICA e central para decidir se ``resolved`` pode ser
        apagado por limpeza. DEVE ser chamada com ``self._lock`` já
        adquirido (nunca faz I/O de disco). Ordem de precedência (nenhum
        nível posterior nunca vence um anterior — ``claimed_category``, a
        categoria que o CHAMADOR alega, é deliberadamente a ÚLTIMA e mais
        fraca autoridade, nunca decisiva sozinha):

        1. fora dos managed roots -> recusado;
        2. estruturalmente protegido (painel.db/-wal/-shm) -> recusado;
        3. registrado como protegido (``register_protected_path``) -> recusado;
        4. caminho ativo (``active_path``/``mark_active``) -> recusado;
        5. já reivindicado por outra limpeza concorrente -> recusado;
        6. política de root: o managed root mais específico que contém o
           caminho precisa ter categoria estrutural CLEANABLE (hoje: cache/
           temp) — roots como database/accounts/projects/logs/templates/
           backups/models/support/removed_accounts SEMPRE recusam limpeza
           aqui, independentemente da categoria alegada pelo chamador, e
           esta etapa não implementa nenhuma sub-root "disposable" que
           escape dessa regra;
        7. prova de ownership: precisa existir um ``_AllocationRecord``
           para o caminho resolvido (via ``allocate_temp`` ou
           ``register_disposable_path``), com ``disposable=True`` e
           categoria em ``CLEANABLE_CATEGORIES`` — um arquivo desconhecido
           dentro de cache/temp, sem esse registro, é sempre preservado
           ("está em temp, então é nosso" nunca é presumido).

        Retorna ``(autorizado, motivo_recusa_ou_None, categoria_efetiva,
        allocation_id_ou_None)``. ``categoria_efetiva`` é a categoria do
        REGISTRO DE OWNERSHIP, nunca a ``claimed_category`` do chamador — a
        categoria alegada nunca é autoridade (correção central do
        BLOQUEADOR 1). ``allocation_id`` identifica a GERAÇÃO exata do
        registro usado nesta decisão — o chamador usa isso para
        vincular/checar identidade física (``_bind_or_check_identity_locked``)
        sem reintroduzir uma corrida (BLOQUEADOR 2 da 2ª revisão)."""
        if not self._inside_managed_roots(resolved):
            return False, "outside_managed_roots", None, None
        if self._is_structurally_protected(resolved):
            return False, "structurally_protected", None, None
        if resolved in self._protected_paths:
            return False, "registered_protected", None, None
        if resolved in self._active_paths:
            return False, "active_path", None, None
        if resolved in self._cleanup_claims:
            return False, "cleanup_already_claimed", None, None
        if resolved in self._promotion_claims:
            # Bloqueador da 3ª revisão adversarial: uma promoção em
            # andamento já reivindicou este caminho (como origem ou
            # destino) — nunca deixar um cleanup apagar um arquivo que uma
            # promoção pode ainda publicar/sobrescrever.
            return False, "promotion_claim_in_progress", None, None

        root_category = self._root_category_for(resolved)
        if root_category not in CLEANABLE_CATEGORIES:
            return False, "root_not_cleanable", None, None

        record = self._allocations.get(resolved)
        if record is None:
            return False, "no_ownership_proof", None, None
        if not record.disposable or record.category not in CLEANABLE_CATEGORIES:
            return False, "ownership_not_disposable", None, None

        return True, None, record.category, record.allocation_id

    def _check_cleanup_authorized(
        self, resolved: Path, claimed_category: str
    ) -> tuple[bool, str | None, str | None, str | None]:
        """Checagem SOMENTE de leitura (não reivindica nada) — usada por
        ``build_cleanup_plan`` para planejar sem se comprometer."""
        with self._lock:
            return self._authorize_cleanup_locked(resolved, claimed_category)

    def _claim_cleanup(
        self, resolved: Path, claimed_category: str
    ) -> tuple[bool, str | None, str | None, str | None]:
        """Checa e, se autorizado, RESERVA atomicamente ``resolved`` para
        limpeza no mesmo ``with self._lock`` — é essa atomicidade (decisão +
        marcação, sem soltar o lock entre as duas) que impede a corrida do
        BLOQUEADOR 3: nenhuma chamada a ``mark_active``/
        ``register_protected_path``/``register_disposable_path`` que
        adquira o lock DEPOIS desta consegue "vencer" um cleanup já
        comprometido, e vice-versa — quem adquire o lock primeiro decide o
        resultado."""
        with self._lock:
            authorized, reason, category, allocation_id = self._authorize_cleanup_locked(
                resolved, claimed_category
            )
            if authorized:
                self._cleanup_claims.add(resolved)
            return authorized, reason, category, allocation_id

    def _release_cleanup_claim(self, resolved: Path) -> None:
        with self._lock:
            self._cleanup_claims.discard(resolved)

    # -- Linearização da promoção (bloqueador da 3ª revisão adversarial) ---

    def _promotion_authorize_locked(
        self, temp_resolved: Path, final_resolved: Path
    ) -> tuple[bool, str | None, str | None, str | None]:
        """Autoridade ÚNICA para decidir (e, em ``_claim_promotion``,
        reivindicar) se uma promoção pode prosseguir. DEVE ser chamada com
        ``self._lock`` já adquirido — nenhuma etapa aqui faz I/O de disco
        (nada de ``stat``/``exists``/``validate``/``os.replace``; essas
        etapas continuam acontecendo DEPOIS, com o claim já em mãos e o
        lock já liberado — ver "LINEARIZAÇÃO DA PROMOÇÃO" na docstring do
        módulo). Por isso, exatamente como ``_authorize_cleanup_locked``,
        NUNCA chama ``_is_registered_protected``/``_is_active``/
        ``protected_category_of`` (todas adquirem ``self._lock``
        internamente e o lock não é reentrante) — só checagens brutas de
        dict/set.

        Ordem de precedência (nenhum nível posterior vence um anterior):
        0. origem e destino resolvem para o MESMO caminho físico ->
           recusado (nunca consome ownership de um arquivo que
           permaneceria parado no mesmo pathname — bloqueador da
           auditoria independente pós-correção cross-volume);
        1. destino estruturalmente ou registrado como protegido -> recusado;
        2. destino fora dos managed roots -> recusado;
        3. destino já reivindicado (por um cleanup OU por outra promoção
           em andamento) -> recusado explicitamente, nunca sucesso
           silencioso seguido de publicação por cima;
        4. destino marcado como ativo (``mark_active``/``active_path``) ->
           recusado — mesma simetria já aplicada à origem, para nunca
           sobrescrever um arquivo "em uso" (bloqueador da auditoria
           independente);
        5. destino está dentro de um managed root estruturalmente
           CLEANABLE (hoje: cache/temp) -> recusado — ``promote_to_final``
           nunca publica sozinho dentro de um root que existe para ser
           limpo/gerenciado (bloqueador da auditoria independente: isso
           criava um arquivo final órfão, sem ownership, dentro de
           temp/cache);
        6. origem fora dos managed roots -> recusado;
        7. origem estruturalmente ou registrada como protegida -> recusado;
        8. política de root da origem não é estruturalmente cleanable ->
           recusado (defesa em profundidade, mesma regra usada por
           ``register_disposable_path``);
        9. origem já reivindicada (por um cleanup OU por outra promoção em
           andamento) -> recusado explicitamente;
        10. origem marcada como ativa -> recusado (bloqueador da auditoria
            independente: ``mark_active`` já recusava se um promotion claim
            estivesse ativo, mas a volta — promoção vencendo um
            ``active_path`` já existente — não era checada);
        11. prova de ownership da origem precisa existir;
        12. categoria da ownership precisa estar em
            ``PROMOTABLE_TEMP_CATEGORIES``.

        Retorna ``(autorizado, motivo_ou_None, categoria_ou_None,
        allocation_id_ou_None)``. Nada aqui reivindica sozinho — quem
        reivindica é ``_claim_promotion``, na MESMA aquisição de lock
        desta chamada."""
        if temp_resolved == final_resolved:
            return False, "source_equals_destination", None, None

        if self._is_structurally_protected(final_resolved) or (
            final_resolved in self._protected_paths
        ):
            category = self._protected_paths.get(final_resolved)
            return False, "destination_protected", category, None
        if not self._inside_managed_roots(final_resolved):
            return False, "destination_outside_managed_roots", None, None
        if final_resolved in self._cleanup_claims:
            return False, "cleanup_claim_in_progress", None, None
        if final_resolved in self._promotion_claims:
            return False, "promotion_claim_in_progress", None, None
        if final_resolved in self._active_paths:
            return False, "destination_active", None, None
        destination_root_category = self._root_category_for(final_resolved)
        if destination_root_category in CLEANABLE_CATEGORIES:
            return False, "destination_root_cleanable", destination_root_category, None

        if not self._inside_managed_roots(temp_resolved):
            return False, "temp_outside_managed_roots", None, None
        if self._is_structurally_protected(temp_resolved) or (
            temp_resolved in self._protected_paths
        ):
            category = self._protected_paths.get(temp_resolved)
            return False, "temp_is_protected", category, None
        temp_root_category = self._root_category_for(temp_resolved)
        if temp_root_category not in CLEANABLE_CATEGORIES:
            return False, "temp_root_not_cleanable", temp_root_category, None
        if temp_resolved in self._cleanup_claims:
            return False, "cleanup_claim_in_progress", None, None
        if temp_resolved in self._promotion_claims:
            return False, "promotion_claim_in_progress", None, None
        if temp_resolved in self._active_paths:
            return False, "temp_active", None, None

        record = self._allocations.get(temp_resolved)
        if record is None:
            return False, "temp_not_owned", None, None
        if record.category not in PROMOTABLE_TEMP_CATEGORIES:
            return False, "temp_category_not_promotable", record.category, record.allocation_id

        return True, None, record.category, record.allocation_id

    def _claim_promotion(
        self, temp_resolved: Path, final_resolved: Path
    ) -> tuple[bool, str | None, str | None, str | None]:
        """Checa e, se autorizado, RESERVA atomicamente ORIGEM e DESTINO
        para esta promoção no mesmo ``with self._lock`` — mesma disciplina
        de ``_claim_cleanup``: decisão + marcação sem soltar o lock entre
        as duas, o que fecha a corrida do bloqueador da 3ª revisão nas
        duas direções (proteção/reclassificação/cleanup vencendo o claim
        recusam o claim; o claim vencendo eles faz proteção/reclassificação/
        cleanup falharem explicitamente depois) — quem adquire o lock
        primeiro decide o resultado."""
        with self._lock:
            authorized, reason, category, allocation_id = self._promotion_authorize_locked(
                temp_resolved, final_resolved
            )
            if authorized:
                self._promotion_claims.add(temp_resolved)
                self._promotion_claims.add(final_resolved)
            return authorized, reason, category, allocation_id

    def _release_promotion_claim(self, temp_resolved: Path, final_resolved: Path) -> None:
        with self._lock:
            self._promotion_claims.discard(temp_resolved)
            self._promotion_claims.discard(final_resolved)

    def register_disposable_path(
        self, path: Any, category: str, *, label: str | None = None
    ) -> Path:
        """API explícita de OWNERSHIP para um arquivo cleanable/promovível
        que não foi criado via ``allocate_temp`` (ex.: um frame cache
        escrito diretamente por um worker, ou um intermediário promovível
        produzido por outra camada). Aceita tanto categorias
        ``CLEANABLE_CATEGORIES`` (torna o caminho elegível para
        ``execute_cleanup``) quanto ``PROMOTABLE_TEMP_CATEGORIES`` (torna o
        caminho elegível como origem de ``promote_to_final``) — as duas
        listas se sobrepõem em ``TEMP``/``INTERMEDIATE_DISPOSABLE``.
        Registrar aqui NÃO basta sozinho para tornar o caminho limpável: a
        política de root (managed root CLEANABLE) continua sendo checada em
        ``_authorize_cleanup_locked`` — registrar ownership sobre um
        arquivo dentro de ``backups``/``models``/etc. não abre nenhuma
        brecha, porque a checagem de root acontece ANTES da checagem de
        ownership (ver BLOQUEADOR 1).

        CORREÇÃO PÓS-2ª REVISÃO ADVERSARIAL (BLOQUEADOR 1): a proteção
        acima protegia CLEANUP, mas não PROMOTION — era possível registrar
        um arquivo real dentro de ``backups``/``models``/``templates``/
        ``projects``/``accounts`` como ``CATEGORY_TEMP`` e depois passá-lo
        por ``promote_to_final`` com sucesso, porque só a existência do
        registro era checada lá, nunca a política estrutural de root da
        ORIGEM. Agora esta função RECUSA a registrar ownership sobre
        qualquer caminho cujo managed root mais específico não seja
        estruturalmente CLEANABLE (hoje: só ``cache``/``temp``) — não
        existe nenhuma sub-root "disposable" dentro de
        ``projects``/``backups``/etc. nesta etapa. ``promote_to_final``
        TAMBÉM revalida essa mesma política de root de forma independente
        (defesa em profundidade — nunca confia apenas em este registro ter
        sido usado corretamente, nem mesmo se um ``_AllocationRecord``
        malformado aparecer manualmente em ``_allocations``).

        BLOQUEADOR 3 (linearização): se uma limpeza já reivindicou este
        caminho (``_cleanup_claims``), esta chamada falha explicitamente
        (``StorageCleanupError``, ``reason="cleanup_claim_in_progress"``)
        em vez de silenciosamente reclassificar uma entidade que está
        sendo apagada neste exato instante — mesma semântica de
        ``mark_active``/``register_protected_path``.

        BLOQUEADOR 2 (identidade/geração): se o arquivo já existe neste
        momento, sua identidade física (dev/ino/ctime) é capturada e
        vinculada a esta geração de ownership — um arquivo DIFERENTE que
        apareça depois no mesmo pathname nunca herda esta ownership (ver
        ``_identity_matches``)."""
        allowed = CLEANABLE_CATEGORIES | PROMOTABLE_TEMP_CATEGORIES
        if category not in allowed:
            raise ValueError(
                f"categoria não é descartável nem promovível: {category!r}"
            )
        resolved = self.assert_safe_managed_path(path)

        root_category = self._root_category_for(resolved)
        if root_category not in CLEANABLE_CATEGORIES:
            raise UnsafeStoragePathError(
                "não é possível registrar ownership descartável/promovível: o "
                "managed root deste caminho não é estruturalmente cleanable "
                "(sem sub-root 'disposable' nesta etapa)",
                operation="register_disposable_path",
                category=category,
                recoverable=False,
                reason="root_not_cleanable",
            )

        identity: tuple[int, int, float] | None = None
        try:
            st = resolved.stat()
            identity = (st.st_dev, st.st_ino, st.st_ctime)
        except OSError:
            identity = None

        with self._lock:
            if resolved in self._cleanup_claims:
                raise StorageCleanupError(
                    "não é possível registrar/reclassificar ownership: uma "
                    "limpeza já está em andamento para este caminho (cleanup "
                    "claim ativo)",
                    operation="register_disposable_path",
                    category=category,
                    recoverable=True,
                    reason="cleanup_claim_in_progress",
                )
            if resolved in self._promotion_claims:
                # Bloqueador da 3ª revisão adversarial: uma promoção em
                # andamento já reivindicou este caminho (origem ou
                # destino) — recusar a reclassificação explicitamente em
                # vez de deixar a promoção antiga consumir uma geração de
                # ownership diferente da que ela validou.
                raise StorageCleanupError(
                    "não é possível registrar/reclassificar ownership: uma "
                    "promoção já está em andamento para este caminho "
                    "(promotion claim ativo)",
                    operation="register_disposable_path",
                    category=category,
                    recoverable=True,
                    reason="promotion_claim_in_progress",
                )
            self._allocations[resolved] = _AllocationRecord(
                category=category,
                disposable=category in CLEANABLE_CATEGORIES,
                created_at=time.time(),
                pid=os.getpid(),
                label=label,
                allocation_id=str(uuid4()),
                dev=identity[0] if identity else None,
                ino=identity[1] if identity else None,
                ctime=identity[2] if identity else None,
            )
        return resolved

    # -- Volume / disk usage -----------------------------------------------

    def _volume_key(self, path: Any) -> str:
        """Ponto único de identidade de volume — sobrescrevível por
        instância em testes (``manager._volume_key = lambda p: ...``) para
        simular volumes distintos deterministicamente neste sandbox Linux
        de single-filesystem (ver docstring dos testes de "volumes
        independentes")."""
        return _volume_key(path)

    def disk_usage(self, path: Any) -> DiskUsage:
        """Melhor esforço via ``shutil.disk_usage``; sobe até o ancestral
        existente mais próximo quando ``path`` ainda não existe. Qualquer
        falha vira ``StorageVolumeUnavailableError`` estruturado — nunca
        inventa números."""
        ancestor = _nearest_existing_ancestor(Path(path))
        try:
            usage = shutil.disk_usage(str(ancestor))
        except OSError:
            raise StorageVolumeUnavailableError(
                "não foi possível consultar uso de disco para este volume",
                operation="disk_usage",
                volume=self._volume_key(ancestor),
                recoverable=False,
            ) from None
        return DiskUsage(total=usage.total, used=usage.used, free=usage.free)

    def available_for_app(self, path: Any) -> int:
        """``free - safety_margin - reservado_no_volume``, nunca negativo
        (grampeado em 0)."""
        path = Path(path)
        ancestor = _nearest_existing_ancestor(path)
        disk = self.disk_usage(ancestor)
        margin = self._safety_margin.compute(disk.total)
        volume = self._volume_key(path)
        with self._lock:
            reserved = self._reserved_by_volume.get(volume, 0)
        available = disk.free - margin - reserved
        return available if available > 0 else 0

    def reserved_bytes(self, path_or_volume: Any) -> int:
        volume = self._volume_key(Path(path_or_volume))
        with self._lock:
            return self._reserved_by_volume.get(volume, 0)

    # -- Reservas (runtime-only) --------------------------------------------

    def reserve(
        self,
        path_or_volume: Any,
        num_bytes: int,
        *,
        category: str | None = None,
        label: str | None = None,
    ) -> StorageReservation:
        """Reserva atômica: checagem-e-incremento sob um único lock. O
        ``disk_usage`` (syscall) é lido FORA do lock; a decisão "ainda cabe"
        + o commit do contador acontecem juntos, DENTRO do lock — é essa
        combinação que impede overcommit entre reservas concorrentes (ver
        docstring do módulo, seção "LOCKS")."""
        if num_bytes is None:
            raise StorageReservationError(
                "num_bytes não pode ser None (UNKNOWN nunca vira 0 silenciosamente)",
                operation="reserve",
                category=category,
                recoverable=False,
            )
        num_bytes = int(num_bytes)
        if num_bytes < 0:
            raise StorageReservationError(
                "num_bytes deve ser >= 0",
                operation="reserve",
                category=category,
                recoverable=False,
            )

        path = Path(path_or_volume)
        volume = self._volume_key(path)
        ancestor = _nearest_existing_ancestor(path)
        disk = self.disk_usage(ancestor)
        margin = self._safety_margin.compute(disk.total)

        with self._lock:
            self._volume_sample_path.setdefault(volume, ancestor)
            reserved = self._reserved_by_volume.get(volume, 0)
            available = disk.free - margin - reserved
            if available < 0:
                available = 0
            if num_bytes > available:
                raise InsufficientStorageError(
                    "espaço insuficiente para completar a reserva",
                    operation="reserve",
                    category=category,
                    volume=volume,
                    required_bytes=num_bytes,
                    available_bytes=available,
                    recoverable=True,
                )
            self._reserved_by_volume[volume] = reserved + num_bytes
            reservation = StorageReservation(
                manager=self,
                reservation_id=str(uuid4()),
                volume_key=volume,
                amount=num_bytes,
                category=category,
                label=label,
            )
            self._active_reservations[reservation.reservation_id] = reservation
        return reservation

    def _release_reservation(self, reservation: StorageReservation) -> None:
        with self._lock:
            current = self._reserved_by_volume.get(reservation.volume_key, 0)
            updated = current - reservation.amount
            if updated < 0:
                raise StorageReservationError(
                    "accounting de reserva corrompido: contador ficaria negativo",
                    operation="release",
                    category=reservation.category,
                    volume=reservation.volume_key,
                    recoverable=False,
                )
            self._reserved_by_volume[reservation.volume_key] = updated
            self._active_reservations.pop(reservation.reservation_id, None)

    def reserve_compound(
        self, requirements: Iterable[StorageRequirement]
    ) -> CompoundReservation:
        """Tudo-ou-nada entre múltiplos volumes/paths — REVISADO no
        BLOQUEADOR 5 da 1ª revisão adversarial: a versão anterior chamava
        ``reserve()`` uma a uma, o que expunha estado PARCIAL (ex.: volume A
        já commitado, volume B ainda não) para qualquer outra thread que
        lesse ``reserved_bytes``/``snapshot`` entre as chamadas.

        Agora o commit inteiro acontece em UMA ÚNICA aquisição de
        ``self._lock``: toda leitura de disco (``disk_usage``, que é
        syscall) é feita FORA do lock primeiro, agregada por volume; dentro
        do lock só acontece a checagem "cabe?" + o incremento de TODOS os
        volumes envolvidos, atomicamente, sem nunca soltar o lock no meio.
        Isso significa que nenhuma outra thread pode observar um estado
        onde só parte da reserva composta foi aplicada — ou nenhuma
        aparece, ou todas aparecem juntas no mesmo instante (ver teste
        adversarial de visibilidade de reserva composta)."""
        requirements = list(requirements)
        if not requirements:
            return CompoundReservation(())

        prepared: list[tuple[StorageRequirement, str, Path, int]] = []
        for requirement in requirements:
            if requirement.estimated_bytes is None:
                raise StorageReservationError(
                    "StorageRequirement.estimated_bytes não pode ser None em "
                    "reserve_compound (UNKNOWN nunca é tratado como 0)",
                    operation="reserve_compound",
                    category=requirement.category,
                    recoverable=False,
                )
            num_bytes = int(requirement.estimated_bytes)
            if num_bytes < 0:
                raise StorageReservationError(
                    "num_bytes deve ser >= 0",
                    operation="reserve_compound",
                    category=requirement.category,
                    recoverable=False,
                )
            path = Path(requirement.path_or_volume)
            volume = self._volume_key(path)
            ancestor = _nearest_existing_ancestor(path)
            prepared.append((requirement, volume, ancestor, num_bytes))

        # Agrega demanda por volume ANTES de tocar o lock — mais de um
        # requirement pode mirar o mesmo volume.
        needed_by_volume: dict[str, int] = {}
        ancestor_by_volume: dict[str, Path] = {}
        for _requirement, volume, ancestor, num_bytes in prepared:
            needed_by_volume[volume] = needed_by_volume.get(volume, 0) + num_bytes
            ancestor_by_volume.setdefault(volume, ancestor)

        # ``disk_usage`` (syscall real) é sempre lido FORA do lock — ver
        # docstring do módulo, seção "LOCKS".
        disk_by_volume: dict[str, DiskUsage] = {}
        for volume, ancestor in ancestor_by_volume.items():
            disk_by_volume[volume] = self.disk_usage(ancestor)

        with self._lock:
            # Passo 1: valida TODOS os volumes antes de commitar QUALQUER
            # um — all-or-nothing real, não sequencial.
            for volume, needed in needed_by_volume.items():
                disk = disk_by_volume[volume]
                margin = self._safety_margin.compute(disk.total)
                reserved = self._reserved_by_volume.get(volume, 0)
                available = disk.free - margin - reserved
                if available < 0:
                    available = 0
                if needed > available:
                    raise InsufficientStorageError(
                        "espaço insuficiente para completar a reserva composta "
                        "(nenhum volume foi alterado)",
                        operation="reserve_compound",
                        volume=volume,
                        required_bytes=needed,
                        available_bytes=available,
                        recoverable=True,
                    )

            # Passo 2: tudo cabe -> commita TODOS os volumes na mesma seção
            # crítica, sem soltar o lock entre eles.
            for volume, needed in needed_by_volume.items():
                self._reserved_by_volume[volume] = (
                    self._reserved_by_volume.get(volume, 0) + needed
                )
                self._volume_sample_path.setdefault(volume, ancestor_by_volume[volume])

            acquired: list[StorageReservation] = []
            for requirement, volume, _ancestor, num_bytes in prepared:
                reservation = StorageReservation(
                    manager=self,
                    reservation_id=str(uuid4()),
                    volume_key=volume,
                    amount=num_bytes,
                    category=requirement.category,
                    label=None,
                )
                self._active_reservations[reservation.reservation_id] = reservation
                acquired.append(reservation)

        return CompoundReservation(tuple(acquired))

    # -- Active paths (lease de arquivo / proteção contra limpeza) --------

    def mark_active(self, path: Any) -> Path:
        resolved = self._resolve_path(path)
        with self._lock:
            if resolved in self._cleanup_claims:
                # BLOQUEADOR 3 (1ª revisão adversarial): um cleanup já
                # reivindicou este caminho atomicamente sob o mesmo lock —
                # falhar explicitamente em vez de deixar o chamador achar
                # que protegeu um arquivo que está sendo apagado agora.
                raise StorageCleanupError(
                    "não é possível marcar como ativo: uma limpeza já está "
                    "em andamento para este caminho (cleanup claim ativo)",
                    operation="mark_active",
                    recoverable=True,
                    reason="cleanup_claim_in_progress",
                )
            if resolved in self._promotion_claims:
                # Bloqueador da 3ª revisão adversarial: uma promoção em
                # andamento já reivindicou este caminho — falha explícita
                # em vez de deixar o chamador achar que marcou como ativo
                # um arquivo que a promoção ainda pode publicar/
                # sobrescrever.
                raise StorageCleanupError(
                    "não é possível marcar como ativo: uma promoção já "
                    "está em andamento para este caminho (promotion claim "
                    "ativo)",
                    operation="mark_active",
                    recoverable=True,
                    reason="promotion_claim_in_progress",
                )
            self._active_paths[resolved] = self._active_paths.get(resolved, 0) + 1
        return resolved

    def mark_inactive(self, path: Any) -> None:
        resolved = self._resolve_path(path)
        with self._lock:
            count = self._active_paths.get(resolved, 0) - 1
            if count <= 0:
                self._active_paths.pop(resolved, None)
            else:
                self._active_paths[resolved] = count

    def _is_active(self, resolved: Path) -> bool:
        with self._lock:
            return resolved in self._active_paths

    @contextmanager
    def active_path(self, path: Any) -> Iterator[Path]:
        resolved = self.mark_active(path)
        try:
            yield resolved
        finally:
            self.mark_inactive(path)

    # -- Alocação de temp ----------------------------------------------------

    def allocate_temp(
        self,
        category: str = CATEGORY_TEMP,
        *,
        suffix: str = "",
        label: str | None = None,
        create: bool = False,
    ) -> Path:
        """Nome único baseado em UUID4 sob o ``temp`` root gerenciado — nunca
        derivado de nome fornecido pelo chamador (evita colisão e
        traversal). ``suffix`` (ex.: ``.mp4``) é sanitizado, nunca usado
        para navegação de diretório. Não cria o arquivo por padrão — ver
        "DESVIOS" na docstring do módulo."""
        if category not in CATEGORIES:
            raise ValueError(f"categoria desconhecida: {category!r}")
        safe_suffix = _sanitize_suffix(suffix)
        name = f"{uuid4().hex}{safe_suffix}"
        path = (Path(self._app_paths.temp) / name).resolve(strict=False)

        # Se ``create=True``, o arquivo passa a existir AGORA — captura sua
        # identidade física (dev/ino/ctime) imediatamente, vinculando-a a
        # esta geração de ownership (ver BLOQUEADOR 2 da 2ª revisão
        # adversarial, "ABA de identidade de arquivo"). Se ``create=False``,
        # nenhuma identidade é inventada — o nome UUID4 por si só já torna
        # colisão com um arquivo externo pré-existente praticamente
        # impossível, e a identidade real será capturada na primeira vez
        # que o caminho for efetivamente usado (ver
        # ``register_disposable_path`` para o caso comum de arquivo já
        # existente).
        identity: tuple[int, int, float] | None = None
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=False)
            try:
                st = path.stat()
                identity = (st.st_dev, st.st_ino, st.st_ctime)
            except OSError:
                identity = None

        with self._lock:
            self._allocations[path] = _AllocationRecord(
                category=category,
                disposable=category in CLEANABLE_CATEGORIES,
                created_at=time.time(),
                pid=os.getpid(),
                label=label,
                allocation_id=str(uuid4()),
                dev=identity[0] if identity else None,
                ino=identity[1] if identity else None,
                ctime=identity[2] if identity else None,
            )
        return path

    def allocation_category_of(self, path: Any) -> str | None:
        resolved = self._resolve_path(path)
        with self._lock:
            record = self._allocations.get(resolved)
        return record.category if record else None

    @staticmethod
    def _identity_matches(record: "_AllocationRecord", st: Any) -> bool:
        """``True`` se o registro NÃO tem identidade vinculada ainda (nada a
        checar) OU se ``dev``/``ino`` vinculados batem com o ``stat`` atual.
        ``False`` só quando existe identidade vinculada E ela diverge —
        sinal de ABA (arquivo antigo sumiu, outro apareceu no mesmo
        pathname).

        DELIBERADAMENTE só ``dev``/``ino`` aqui, NUNCA ``ctime`` — ao
        contrário da revalidação TOCTOU de curta janela em
        ``execute_cleanup`` (plano -> execução, onde nenhuma escrita
        legítima é esperada nesse meio-tempo, então ``ctime`` é um sinal
        útil ali), esta vinculação de ownership cobre uma janela
        POTENCIALMENTE LONGA entre o registro e o uso — nesse intervalo, o
        próprio dono legítimo pode escrever mais conteúdo no arquivo
        (``allocate_temp`` + escrita do produtor) ou chamar ``os.utime``,
        e ambas as operações sempre avançam ``ctime`` mesmo sendo
        completamente legítimas. Exigir ``ctime`` igual aqui geraria falsos
        positivos rejeitando promoções/limpezas legítimas. ``dev``/``ino``
        continuam estáveis através de qualquer escrita/utime no MESMO
        inode, e só mudam numa troca real (unlink+recriação) — a
        (rara, documentada) exceção é reaproveitamento do número de inode
        pelo filesystem logo após o unlink, uma limitação best-effort
        conhecida (ver docstring do módulo, "MULTI-PROCESSO")."""
        if record.dev is None or record.ino is None:
            return True
        return record.dev == getattr(st, "st_dev", None) and record.ino == getattr(
            st, "st_ino", None
        )

    def _bind_or_check_identity(
        self, resolved: Path, expected_allocation_id: str, st: Any
    ) -> bool:
        """Adquire o lock internamente (chamado depois do ``stat`` real,
        que é I/O e não pode acontecer dentro do lock). Se o registro AINDA
        é a mesma geração (``allocation_id`` bate) e ainda não tem
        identidade vinculada, vincula agora — primeira utilização real do
        caminho para cleanup/promoção. Se já tem identidade, apenas
        confere. Se a geração mudou (outro registro/reclassificação
        aconteceu entre a autorização e agora) ou a identidade diverge,
        retorna ``False`` — trate como ownership inválida para este uso,
        nunca apague/promova."""
        with self._lock:
            current = self._allocations.get(resolved)
            if current is None or current.allocation_id != expected_allocation_id:
                return False
            if current.dev is None or current.ino is None or current.ctime is None:
                self._allocations[resolved] = _dc_replace(
                    current, dev=st.st_dev, ino=st.st_ino, ctime=st.st_ctime
                )
                return True
            return self._identity_matches(current, st)

    def _consume_allocation(self, resolved: Path) -> None:
        """Remove o ``_AllocationRecord`` de ``resolved`` — chamado depois
        de um delete ou promoção bem-sucedidos (BLOQUEADOR 2 da 2ª revisão:
        ownership nunca sobrevive à entidade física que a justificava). Um
        arquivo NOVO que apareça depois no mesmo pathname não herda
        ownership nenhuma — precisa de um registro explícito novo."""
        with self._lock:
            self._allocations.pop(resolved, None)

    def _consume_allocation_if_generation_matches(
        self, resolved: Path, expected_allocation_id: str | None
    ) -> None:
        """Variante geração-consciente de ``_consume_allocation``: só
        remove o registro se ele AINDA for a mesma geração
        (``allocation_id``) que a operação em curso validou. Corrige o
        Caso B da 3ª revisão adversarial: como o promotion claim já
        impede qualquer reclassificação concorrente da origem enquanto a
        promoção está em andamento, isto é defesa em profundidade — nunca
        consome uma geração de ownership diferente da que foi de fato
        validada por esta operação, mesmo que ``_consume_allocation``
        tivesse sido chamada com um path errado ou fora de ordem. Se
        ``expected_allocation_id`` for ``None`` (chamador não tinha um
        allocation_id para comparar), o comportamento é um no-op seguro:
        nunca remove nada sem um generation id conhecido."""
        if expected_allocation_id is None:
            return
        with self._lock:
            current = self._allocations.get(resolved)
            if current is not None and current.allocation_id == expected_allocation_id:
                self._allocations.pop(resolved, None)

    # -- Cleanup: plan / execute com revalidação TOCTOU ---------------------

    def build_cleanup_plan(
        self, candidates: Iterable[CleanupCandidate], *, min_age_seconds: float = 0.0
    ) -> CleanupPlan:
        """Planeja a limpeza. ``candidate.category`` é apenas uma
        DECLARAÇÃO/intenção do chamador — NUNCA é autoridade final (ver
        ``_authorize_cleanup_locked``, correção do BLOQUEADOR 1 da 1ª
        revisão adversarial): a categoria efetivamente usada para decidir é
        sempre a do registro de OWNERSHIP (``allocate_temp``/
        ``register_disposable_path``), nunca a alegada aqui.

        ``min_age_seconds`` (grace period, opcional — ver "GRACE PERIOD" na
        docstring do módulo): quando > 0, um candidato cujo ``mtime`` for
        mais recente que ``min_age_seconds`` é pulado com
        ``"too_recent_for_cleanup"``, mesmo que já tenha prova de
        ownership. Nunca reduz proteção — só adiciona uma restrição extra."""
        items: list[CleanupPlanItem] = []
        skipped: list[tuple[Path, str]] = []
        now = time.time()

        for candidate in candidates:
            raw_path = Path(candidate.path)

            if candidate.category not in CLEANABLE_CATEGORIES:
                try:
                    reported = raw_path.resolve(strict=False)
                except OSError:
                    reported = raw_path
                skipped.append((reported, "category_not_cleanable"))
                continue

            try:
                is_symlink_candidate = raw_path.is_symlink()
            except OSError:
                skipped.append((raw_path, "stat_failed"))
                continue

            try:
                resolved = raw_path.resolve(strict=False)
            except OSError:
                skipped.append((raw_path, "path_resolution_failed"))
                continue

            # Política conservadora v1: o candidato NUNCA é seguido nem
            # apagado se ele mesmo for um link — ver "DESVIOS" na docstring
            # do módulo.
            if is_symlink_candidate:
                skipped.append((resolved, "symlink_candidate_skipped"))
                continue

            # Autoridade única de limpeza: proteção + política de root +
            # ownership. NUNCA usa ``candidate.category`` para decidir —
            # só como um rótulo informativo caso autorizado (ver
            # ``_check_cleanup_authorized``).
            authorized, reason, effective_category, allocation_id = self._check_cleanup_authorized(
                resolved, candidate.category
            )
            if not authorized:
                skipped.append((resolved, reason or "not_authorized"))
                continue

            try:
                st = resolved.stat()
            except OSError:
                skipped.append((resolved, "missing_or_unreadable"))
                continue

            if not stat_module.S_ISREG(st.st_mode):
                skipped.append((resolved, "not_a_regular_file"))
                continue

            if st.st_nlink > 1:
                skipped.append((resolved, "hardlink_conservatism"))
                continue

            # BLOQUEADOR 2 da 2ª revisão adversarial ("ABA de identidade de
            # arquivo"): vincula (primeiro uso real) ou confere a
            # identidade física do registro de ownership contra o ``stat``
            # atual. Um arquivo DIFERENTE que apareça no mesmo pathname
            # depois que a entidade original foi consumida/substituída
            # nunca herda a ownership antiga.
            if allocation_id is not None and not self._bind_or_check_identity(
                resolved, allocation_id, st
            ):
                skipped.append((resolved, "ownership_identity_mismatch"))
                continue

            if min_age_seconds > 0 and (now - st.st_mtime) < min_age_seconds:
                skipped.append((resolved, "too_recent_for_cleanup"))
                continue

            items.append(
                CleanupPlanItem(
                    path=resolved,
                    category=effective_category or candidate.category,
                    reason=candidate.reason,
                    size=st.st_size,
                    mtime=st.st_mtime,
                    dev=getattr(st, "st_dev", 0),
                    ino=getattr(st, "st_ino", 0),
                    ctime=getattr(st, "st_ctime", 0.0),
                    allocation_id=allocation_id,
                )
            )

        return CleanupPlan(items=tuple(items), skipped=tuple(skipped))

    def _unlink(self, path: Path) -> None:
        """Primitiva de delete isolada em seu próprio método só para
        permitir que testes monkeypatchem a falha de UM caminho específico
        sem afetar os demais (ver teste de "arquivo travado não derruba os
        outros")."""
        path.unlink()

    def _delete_with_bounded_retry(self, path: Path, *, attempts: int = 2) -> None:
        """Retry curto e limitado (não é um framework de retry genérico —
        ver "FORA DE ESCOPO" na docstring do módulo)."""
        last_exc: OSError | None = None
        for attempt in range(attempts):
            try:
                self._unlink(path)
                return
            except OSError as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    time.sleep(0.01)
        assert last_exc is not None
        raise last_exc

    def execute_cleanup(self, plan: CleanupPlan, *, dry_run: bool = False) -> CleanupResult:
        """Revalida TUDO de ``build_cleanup_plan`` de novo, item a item,
        imediatamente antes de apagar (proteção contra TOCTOU): existência,
        ainda dentro do managed root, ainda não protegido/ativo, ainda com
        prova de ownership válida, e identidade completa (tamanho + mtime +
        dev/ino) ainda batendo com o snapshot do plano (BLOQUEADOR 4 — um
        arquivo substituído por outro de mesmo tamanho/mtime forjados, mas
        inode diferente, é detectado e pulado).

        A decisão de autorização (proteção + política de root + ownership)
        é feita via ``_claim_cleanup``, que reivindica o caminho
        ATOMICAMENTE sob o mesmo lock usado por ``mark_active``/
        ``register_protected_path`` — isso fecha a corrida do BLOQUEADOR 3:
        quem adquire o lock primeiro (o cleanup ou um novo
        active/protected) decide o resultado; nunca um delete "vence" um
        active_path que apareceu depois nem vice-versa de forma
        inconsistente. O claim é liberado no ``finally``, e o lock NUNCA é
        mantido durante ``stat``/``unlink`` (I/O real acontece sempre fora
        dele).

        Falhas de delete (``PermissionError``, sharing violation do
        Windows, qualquer ``OSError``) são capturadas POR ITEM e viram
        ``failed`` — nunca derrubam a chamada inteira nem os demais
        itens."""
        deleted: list[Path] = []
        skipped: list[tuple[Path, str]] = list(plan.skipped)
        failed: list[tuple[Path, str]] = []
        bytes_freed = 0

        for item in plan.items:
            path = item.path

            try:
                if path.is_symlink():
                    skipped.append((path, "symlink_candidate_skipped"))
                    continue
            except OSError:
                skipped.append((path, "revalidation_failed"))
                continue

            claimed, reason, _effective_category, allocation_id = self._claim_cleanup(
                path, item.category
            )
            if not claimed:
                skipped.append((path, reason or "not_authorized"))
                continue

            try:
                try:
                    st = path.stat()
                except OSError:
                    skipped.append((path, "missing_since_plan"))
                    continue

                if not stat_module.S_ISREG(st.st_mode):
                    skipped.append((path, "not_a_regular_file"))
                    continue

                if st.st_nlink > 1:
                    skipped.append((path, "hardlink_conservatism"))
                    continue

                identity_changed = (
                    st.st_size != item.size
                    or abs(st.st_mtime - item.mtime) > 1e-6
                    or (item.ino and getattr(st, "st_ino", 0) != item.ino)
                    or (item.dev and getattr(st, "st_dev", 0) != item.dev)
                    or (item.ctime and abs(getattr(st, "st_ctime", 0.0) - item.ctime) > 1e-6)
                )
                if identity_changed:
                    skipped.append((path, "changed_since_plan"))
                    continue

                # BLOQUEADOR 2 da 2ª revisão adversarial: além da
                # identidade "desde o plano" acima, revalida a identidade
                # "desde que a ownership foi estabelecida" (vincula na
                # primeira vez, se ainda não vinculada). Uma geração de
                # ownership diferente (reclassificada por outra chamada
                # entre o plano e agora) também é tratada como
                # inconsistente.
                if allocation_id is not None and not self._bind_or_check_identity(
                    path, allocation_id, st
                ):
                    skipped.append((path, "ownership_identity_mismatch"))
                    continue

                if dry_run:
                    deleted.append(path)
                    bytes_freed += st.st_size
                    continue

                try:
                    self._delete_with_bounded_retry(path)
                except OSError as exc:
                    failed.append((path, classify_os_error(exc)))
                    continue

                deleted.append(path)
                bytes_freed += st.st_size
                # BLOQUEADOR 2: ownership nunca sobrevive à entidade física
                # que a justificava — um arquivo novo que apareça depois no
                # mesmo pathname não herda esta ownership. Geração-
                # consciente (``_consume_allocation_if_generation_matches``)
                # como defesa em profundidade: só remove a MESMA geração
                # que este delete revalidou.
                self._consume_allocation_if_generation_matches(path, allocation_id)
            finally:
                self._release_cleanup_claim(path)

        return CleanupResult(
            deleted=tuple(deleted), skipped=tuple(skipped), failed=tuple(failed), bytes_freed=bytes_freed
        )

    # -- Promoção atômica temp -> final --------------------------------------

    def _same_volume(self, a: Path, b: Path) -> bool:
        return self._volume_key(_nearest_existing_ancestor(a)) == self._volume_key(
            _nearest_existing_ancestor(b)
        )

    def _cross_volume_publish(self, temp_path: Path, final_resolved: Path) -> None:
        """Cópia + replace no volume destino — "tão atômico quanto
        possível" entre volumes diferentes. NUNCA reivindicamos atomicidade
        cross-volume real: o ``os.replace`` final só acontece depois que a
        cópia inteira já está fisicamente presente no volume destino.

        Esse ``os.replace`` final é MESMO-VOLUME por construção (``staging``
        e ``final_resolved`` estão sempre no volume destino) — sujeito à
        MESMA janela de corrida transitória de Windows sob concorrência real
        documentada em ``_replace_with_bounded_retry``/"DIFERENÇAS DE
        PLATAFORMA" (duas publicações cross-volume concorrentes visando o
        mesmo ``final_path`` colidem exatamente como duas promoções
        mesmo-volume concorrentes) — por isso reaproveita o MESMO retry
        curto e limitado, nunca uma implementação paralela."""
        staging = final_resolved.with_name(final_resolved.name + f".crossvol_{uuid4().hex}.tmp")
        try:
            shutil.copy2(str(temp_path), str(staging))
            self._replace_with_bounded_retry(staging, final_resolved)
        finally:
            if staging.exists():
                try:
                    staging.unlink()
                except OSError:
                    pass

    def _cleanup_cross_volume_origin(
        self, temp_resolved: Path, allocation_id: str | None
    ) -> None:
        """Depois de uma publicação cross-volume BEM-SUCEDIDA (o destino
        já está fisicamente commitado), tenta remover a origem física
        para não deixar um temporário órfão sem prova de ownership
        (bloqueador reportado pela auditoria independente pós-3ª revisão
        adversarial: ``_cross_volume_publish`` nunca tocava a origem, mas
        a ownership era consumida incondicionalmente mesmo assim).

        Invariante mantida, sempre:

            origem ausente
            OU
            origem ainda existe E ainda possui ownership válida

        NUNCA:

            origem existe E ownership foi consumida.

        Isto NUNCA transforma uma publicação já concluída em falha total
        — o destino já foi publicado com sucesso antes desta função ser
        chamada, e nenhuma exceção daqui se propaga para o chamador.
        Revalida identidade (``allocation_id`` + dev/ino) e tipo (regular,
        não-symlink, ``nlink<=1``) antes de apagar — nunca remove um
        arquivo DIFERENTE que tenha aparecido no mesmo pathname depois da
        cópia (mesma disciplina TOCTOU de ``execute_cleanup``). Se a
        remoção falhar (ex.: arquivo aberto/travado no Windows), a
        ownership NÃO é consumida — o temporário continua elegível para
        ``execute_cleanup`` tratar depois."""
        try:
            st = temp_resolved.lstat()
        except FileNotFoundError:
            # ÚNICO caso em que a AUSÊNCIA física está confirmada — nada
            # para apagar. Só aqui é seguro consumir a ownership.
            self._consume_allocation_if_generation_matches(temp_resolved, allocation_id)
            return
        except OSError:
            # Auditoria independente (bloqueador adicional pós-correção
            # cross-volume): QUALQUER outro ``OSError`` no ``lstat``
            # (``PermissionError``, access denied, I/O error, etc.) é
            # ESTADO DESCONHECIDO, não ausência confirmada — o arquivo
            # pode muito bem continuar lá. Converter "não consegui ler o
            # estado" em "o arquivo sumiu" recriava exatamente o órfão
            # sem ownership que esta função existe para evitar. Preserva
            # a ownership sempre que a ausência não está comprovada —
            # nunca mexe, nunca consome.
            return

        if stat_module.S_ISLNK(st.st_mode) or not stat_module.S_ISREG(st.st_mode):
            # Não é mais o arquivo regular esperado — não mexe, não
            # consome (preserva por segurança; não é o caso normal).
            return

        if st.st_nlink > 1:
            # Mesmo conservadorismo de hardlink do restante do módulo —
            # não mexe, não consome.
            return

        if allocation_id is not None and not self._bind_or_check_identity(
            temp_resolved, allocation_id, st
        ):
            # Identidade divergente desde a cópia — pode ser um arquivo
            # NOVO que apareceu no mesmo pathname; preserva, não consome.
            return

        try:
            self._delete_with_bounded_retry(temp_resolved)
        except OSError:
            # Falha ao apagar a origem DEPOIS que o destino já foi
            # publicado com sucesso — não é uma falha da promoção (que já
            # aconteceu); a ownership PERMANECE válida para que
            # ``execute_cleanup`` trate este temporário mais tarde.
            return

        # Só consome a ownership da origem se ela de fato foi removida.
        self._consume_allocation_if_generation_matches(temp_resolved, allocation_id)

    def _replace_with_bounded_retry(self, temp_path: Path, final_resolved: Path) -> None:
        """Retry curto e limitado em torno de ``os.replace`` -- NUNCA um
        framework de retry genérico (ver "FORA DE ESCOPO" na docstring do
        módulo; mesmo espírito e mesma forma de ``_delete_with_bounded_
        retry`` acima, reaproveitado como PADRÃO, não como código
        importado de outro módulo — ``retry_policy.py`` é infraestrutura
        de retry de ``Job`` inteiro, um domínio completamente diferente
        deste método de storage de baixo nível, e não é importado aqui).

        Cobre uma janela de corrida ESPECÍFICA e CURTA do Windows,
        confirmada por EVIDÊNCIA REAL (não suposição): duas chamadas
        genuinamente concorrentes de ``promote_to_final`` visando o MESMO
        ``final_path`` (mesmo ``cache_key`` determinístico -- cenário
        legítimo e testado de propósito, não uma falha de
        ``_claim_promotion``) podem colidir transitoriamente em
        ``os.replace`` com ``PermissionError``/``WinError 5`` no Windows
        real -- reproduzido no Windows do dono do produto (Windows 10
        19045) via ``tests/test_captions_engine.py::
        test_duas_transcricoes_concorrentes_do_mesmo_video_e_config_
        nao_quebram`` e um script de diagnóstico isolado. Ver
        "DIFERENÇAS DE PLATAFORMA" na docstring do módulo para o
        contrato corrigido.

        Retry roda em TODA plataforma, nunca condicionado a
        ``sys.platform == "win32"`` -- decisão: em POSIX, onde
        ``os.rename``/``os.replace`` é incondicionalmente atômico mesmo
        sob concorrência real, a falha nunca ocorre, então o laço sempre
        termina na 1ª tentativa (custo extra: um único bloco try/except
        já necessário de qualquer forma, zero overhead prático). Manter
        UM único caminho de código para as duas plataformas evita
        introduzir uma ramificação condicional por SO só para pular uma
        proteção que é inofensiva onde não é necessária -- mesmo
        raciocínio já aplicado a ``_delete_with_bounded_retry``, que
        também roda sem checagem de plataforma.

        Esgotado o orçamento de tentativas, a falha NUNCA é engolida:
        propaga como ``StoragePublishError`` estruturada (nunca
        ``str(exc)``/``repr(exc)``/traceback da exceção original
        capturada -- item 10 do CLAUDE.md, mesma disciplina de toda
        ``StorageError`` deste módulo) -- indistinguível de uma falha
        persistente real de permissão, que TEM que continuar sendo
        reportada como erro."""
        last_exc: OSError | None = None
        for attempt in range(_FINAL_REPLACE_RETRY_ATTEMPTS):
            try:
                os.replace(str(temp_path), str(final_resolved))
                return
            except OSError as exc:
                last_exc = exc
                if attempt + 1 < _FINAL_REPLACE_RETRY_ATTEMPTS:
                    time.sleep(_FINAL_REPLACE_RETRY_BACKOFF_SECONDS * (attempt + 1))
        assert last_exc is not None
        raise StoragePublishError(
            "não foi possível promover o arquivo ao destino final via "
            f"os.replace após {_FINAL_REPLACE_RETRY_ATTEMPTS} tentativas -- "
            "falha persistente (não uma janela de corrida transitória, que "
            "o retry acima já cobre)",
            operation="promote_to_final",
            recoverable=True,
            reason="final_replace_retries_exhausted",
        ) from None

    def promote_to_final(
        self,
        temp_path: Any,
        final_path: Any,
        *,
        overwrite: bool = False,
        validate: Callable[[Path], bool] | None = None,
    ) -> Path:
        """Promove um arquivo temporário a final. Proteção sempre vence:
        recusa se ``final_path`` resolver para um caminho protegido (SOURCE
        etc.), MESMO com ``overwrite=True``. Recusa temp ausente/vazio
        (tamanho 0) incondicionalmente — isso é uma checagem de
        armazenamento, não uma validação de mídia (essa é responsabilidade
        de outra camada, futura). O chamador é responsável por já ter
        fechado qualquer handle aberto do arquivo temporário antes de
        chamar isto (ver "DIFERENÇAS DE PLATAFORMA" na docstring do
        módulo) — o StorageManager não tem como verificar isso a nível de
        SO.

        BLOQUEADOR 2 (1ª revisão adversarial): a proteção antiga só
        validava o DESTINO. Um SOURCE/FINAL_ARTIFACT/DATABASE/BACKUP/etc.
        registrado como protegido podia ser passado como ``temp_path`` e
        ser efetivamente MOVIDO/apagado do seu caminho original via
        ``os.replace``. Agora ``temp_path`` também precisa provar: (a)
        está dentro de um managed root; (b) NÃO é um caminho estrutural ou
        registrado como protegido; (c) possui prova de OWNERSHIP
        (``allocate_temp``/``register_disposable_path``) com categoria em
        ``PROMOTABLE_TEMP_CATEGORIES``. Nenhuma dessas checagens é
        contornável com ``overwrite=True`` — ``overwrite`` só afeta se o
        DESTINO pode já existir.

        LINEARIZAÇÃO DA PROMOÇÃO (bloqueador da 3ª revisão adversarial):
        toda a validação acima (e a validação da origem) é decidida e
        RESERVADA atomicamente sob ``self._lock`` em ``_claim_promotion``
        ANTES de qualquer I/O (nada de ``stat``/``validate``/
        ``os.replace``/``shutil.copy2`` acontece com o lock preso). Só
        depois do claim é que o restante roda — e o claim garante que
        nenhuma outra chamada a ``register_protected_path``/
        ``register_disposable_path``/``mark_active``/``execute_cleanup``
        pode agir sobre a origem ou o destino enquanto esta promoção está
        em andamento; elas falham explicitamente
        (``reason="promotion_claim_in_progress"``) em vez de um sucesso
        silencioso seguido desta promoção publicar por cima. Ver
        "LINEARIZAÇÃO DA PROMOÇÃO" na docstring do módulo para o contrato
        completo. O claim (origem + destino) é sempre liberado no
        ``finally``, mesmo se qualquer etapa abaixo falhar."""
        temp_path = Path(temp_path)

        try:
            final_resolved = Path(final_path).resolve(strict=False)
        except OSError:
            raise StoragePublishError(
                "não foi possível resolver o caminho de destino",
                operation="promote_to_final",
                recoverable=False,
                reason="destination_resolution_failed",
            ) from None

        try:
            temp_resolved = temp_path.resolve(strict=False)
        except OSError:
            raise StoragePublishError(
                "não foi possível resolver o caminho de origem (temp)",
                operation="promote_to_final",
                recoverable=False,
                reason="temp_resolution_failed",
            ) from None

        claimed, reason, category, allocation_id = self._claim_promotion(
            temp_resolved, final_resolved
        )
        if not claimed:
            message, recoverable = _PROMOTION_CLAIM_FAILURE_INFO.get(
                reason, ("promoção recusada", True)
            )
            raise StoragePublishError(
                message,
                operation="promote_to_final",
                category=category,
                recoverable=recoverable,
                reason=reason or "not_authorized",
            )

        try:
            try:
                # Auditoria independente pós-3ª revisão adversarial:
                # ``lstat`` (nunca ``stat``) — a checagem precisa observar o
                # PATHNAME original de ``temp_path`` sem seguir
                # silenciosamente um symlink/reparse point para o que ele
                # aponta. ``stat`` seguiria o link e validaria/publicaria o
                # ALVO, não a origem declarada.
                temp_stat = temp_path.lstat()
            except OSError:
                raise StoragePublishError(
                    "arquivo temporário ausente",
                    operation="promote_to_final",
                    recoverable=True,
                    reason="temp_missing",
                ) from None

            if stat_module.S_ISLNK(temp_stat.st_mode):
                raise StoragePublishError(
                    "origem (temp) é um symlink/reparse point; promoção "
                    "recusada — a origem de uma promoção precisa ser o "
                    "arquivo físico em si, nunca um link para outro "
                    "caminho",
                    operation="promote_to_final",
                    recoverable=True,
                    reason="temp_is_symlink",
                )

            if not stat_module.S_ISREG(temp_stat.st_mode):
                raise StoragePublishError(
                    "origem (temp) não é um arquivo regular (ex.: um "
                    "diretório); promoção recusada",
                    operation="promote_to_final",
                    recoverable=True,
                    reason="temp_not_regular_file",
                )

            # Auditoria independente pós-3ª revisão — conservadorismo de
            # hardlink, mesma regra já aplicada por ``execute_cleanup``:
            # uma origem com múltiplos hardlinks (``st_nlink > 1``)
            # compartilha a mesma identidade física de OUTRO caminho no
            # filesystem (possivelmente um SOURCE protegido registrado sob
            # outro pathname). Publicar por cima do destino usando
            # ``os.replace``/cópia a partir dessa origem arrisca modificar
            # fisicamente esse outro caminho através da mesma identidade —
            # violação direta de PROTECTED WINS. Recusado
            # incondicionalmente, mesmo com ``overwrite=True``, e ANTES de
            # qualquer publicação.
            if temp_stat.st_nlink > 1:
                raise StoragePublishError(
                    "origem (temp) possui múltiplos hardlinks (nlink > 1); "
                    "promoção recusada para nunca publicar a partir de um "
                    "caminho que compartilha identidade física com outro "
                    "arquivo (possivelmente protegido)",
                    operation="promote_to_final",
                    recoverable=True,
                    reason="hardlink_conservatism",
                )

            if temp_stat.st_size <= 0:
                raise StoragePublishError(
                    "arquivo temporário vazio não pode ser promovido",
                    operation="promote_to_final",
                    recoverable=True,
                    reason="temp_empty",
                )

            # BLOQUEADOR 2 da 2ª revisão adversarial ("ABA de identidade de
            # arquivo"): vincula (primeiro uso real) ou confere a
            # identidade física do registro de ownership contra o ``stat``
            # atual. Com o promotion claim já ativo, nenhuma reclassificação
            # concorrente da origem pode mais acontecer enquanto estamos
            # aqui — isto é defesa em profundidade, revalidando o mais
            # perto possível do commit físico (ver "MULTI-PROCESSO" na
            # docstring do módulo para o limite honesto que resta: uma
            # substituição feita diretamente por outro PROCESSO, fora
            # deste StorageManager, entre o claim e o ``stat`` abaixo).
            if not self._bind_or_check_identity(temp_resolved, allocation_id, temp_stat):
                raise StoragePublishError(
                    "origem (temp) não corresponde mais à identidade física "
                    "vinculada ao registro de ownership (arquivo substituído "
                    "desde o registro); promoção recusada",
                    operation="promote_to_final",
                    recoverable=True,
                    reason="temp_ownership_identity_mismatch",
                )

            if final_resolved.exists() and not overwrite:
                raise StoragePublishError(
                    "destino já existe e overwrite=False",
                    operation="promote_to_final",
                    recoverable=True,
                    reason="final_exists_no_overwrite",
                )

            if validate is not None:
                try:
                    ok = validate(temp_path)
                except Exception:
                    raise StoragePublishError(
                        "callback de validação lançou exceção antes de qualquer promoção",
                        operation="promote_to_final",
                        recoverable=True,
                        reason="validate_raised",
                    ) from None
                if not ok:
                    raise StoragePublishError(
                        "callback de validação recusou a promoção",
                        operation="promote_to_final",
                        recoverable=True,
                        reason="validate_rejected",
                    )

            final_resolved.parent.mkdir(parents=True, exist_ok=True)

            if self._same_volume(temp_path, final_resolved):
                # ``_replace_with_bounded_retry`` cobre a janela de corrida
                # transitória de Windows confirmada por evidência real (ver
                # docstring do método e "DIFERENÇAS DE PLATAFORMA" acima) —
                # nunca reivindicamos atomicidade incondicional de
                # ``os.replace`` sozinho sob concorrência real nesse SO.
                self._replace_with_bounded_retry(temp_path, final_resolved)
                # ``os.replace`` já MOVEU a origem atomicamente — ela não
                # existe mais fisicamente em ``temp_resolved``. Seguro
                # consumir a ownership incondicionalmente (ainda
                # geração-consciente como defesa em profundidade).
                self._consume_allocation_if_generation_matches(temp_resolved, allocation_id)
            else:
                self._cross_volume_publish(temp_path, final_resolved)
                # Auditoria independente pós-3ª revisão adversarial: a
                # publicação cross-volume é copy2+replace — ela NUNCA
                # remove a origem física (ao contrário de ``os.replace``
                # no mesmo volume). Consumir a ownership aqui sem
                # remover fisicamente o arquivo criaria um vazamento
                # permanente de armazenamento (arquivo órfão sem prova de
                # ownership para nenhum cleanup futuro tratar). Ver
                # ``_cleanup_cross_volume_origin`` para a invariante
                # completa: origem ausente OU origem existe com ownership
                # válida — nunca origem existe com ownership consumida.
                self._cleanup_cross_volume_origin(temp_resolved, allocation_id)
            return final_resolved
        finally:
            self._release_promotion_claim(temp_resolved, final_resolved)

    # -- Observabilidade --------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Estado barato para diagnóstico — NUNCA varre o disco (isso é
        ``usage_by_category``). Por volume com reserva/atividade conhecida:
        total/free/safety_margin/reserved/available_for_app (relê
        ``disk_usage`` por volume — não é "big scan"); contagens globais de
        caminhos ativos e reservas em aberto. Nunca inclui conteúdo de
        arquivo, cookies, tokens ou credenciais."""
        with self._lock:
            reserved_by_volume = dict(self._reserved_by_volume)
            sample_paths = dict(self._volume_sample_path)
            active_count = len(self._active_paths)
            reservation_count = len(self._active_reservations)

        volumes: dict[str, dict[str, Any]] = {}
        for volume, reserved in reserved_by_volume.items():
            sample = sample_paths.get(volume)
            disk: DiskUsage | None
            try:
                disk = self.disk_usage(sample) if sample is not None else None
            except StorageVolumeUnavailableError:
                disk = None
            if disk is None:
                volumes[volume] = {"reserved": reserved, "unavailable": True}
                continue
            margin = self._safety_margin.compute(disk.total)
            available = disk.free - margin - reserved
            volumes[volume] = {
                "total": disk.total,
                "free": disk.free,
                "safety_margin": margin,
                "reserved": reserved,
                "available_for_app": available if available > 0 else 0,
            }

        return {
            "volumes": volumes,
            "active_paths_count": active_count,
            "outstanding_reservations": reservation_count,
        }

    def _root_category_map(self) -> dict[Path, str]:
        ap = self._app_paths
        return {
            Path(ap.database).resolve(strict=False): CATEGORY_DATABASE,
            Path(ap.accounts).resolve(strict=False): CATEGORY_USER_FILE,
            Path(ap.projects).resolve(strict=False): CATEGORY_USER_FILE,
            Path(ap.cache).resolve(strict=False): CATEGORY_CACHE,
            Path(ap.logs).resolve(strict=False): CATEGORY_LOG,
            Path(ap.temp).resolve(strict=False): CATEGORY_TEMP,
            Path(ap.templates).resolve(strict=False): CATEGORY_TEMPLATE,
            Path(ap.backups).resolve(strict=False): CATEGORY_BACKUP,
            Path(ap.models).resolve(strict=False): CATEGORY_MODEL,
            Path(ap.support).resolve(strict=False): CATEGORY_UNKNOWN,
            Path(ap.removed_accounts).resolve(strict=False): CATEGORY_BACKUP,
        }

    def usage_by_category(self, roots: Iterable[Any] | None = None) -> dict[str, int]:
        """Varredura REAL de diretório — custo O(n) no número de arquivos.
        Nunca chamada implicitamente por ``snapshot()``. Nunca segue
        symlinks/junctions (``os.walk(..., followlinks=False)`` + pula
        qualquer entrada marcada como link)."""
        target_roots = (
            [self._resolve_path(root) for root in roots]
            if roots is not None
            else list(self._managed_roots)
        )
        root_category_map = self._root_category_map()
        with self._lock:
            protected_snapshot = dict(self._protected_paths)

        totals: dict[str, int] = {}
        for root in target_roots:
            if not root.exists():
                continue
            default_category = root_category_map.get(root, CATEGORY_UNKNOWN)
            for current, dirs, files in os.walk(str(root), followlinks=False):
                current_path = Path(current)
                dirs[:] = [d for d in dirs if not (current_path / d).is_symlink()]
                for name in files:
                    entry = current_path / name
                    if entry.is_symlink():
                        continue
                    try:
                        resolved = entry.resolve(strict=False)
                        size = entry.stat().st_size
                    except OSError:
                        continue
                    category = protected_snapshot.get(resolved, default_category)
                    totals[category] = totals.get(category, 0) + size
        return totals


__all__ = [
    "CATEGORY_SOURCE",
    "CATEGORY_FINAL_ARTIFACT",
    "CATEGORY_INTERMEDIATE_DISPOSABLE",
    "CATEGORY_INTERMEDIATE_RECOVERABLE",
    "CATEGORY_TEMP",
    "CATEGORY_CACHE",
    "CATEGORY_FRAME_CACHE",
    "CATEGORY_MODEL",
    "CATEGORY_DATABASE",
    "CATEGORY_BACKUP",
    "CATEGORY_LOG",
    "CATEGORY_BROWSER_PROFILE",
    "CATEGORY_TEMPLATE",
    "CATEGORY_USER_FILE",
    "CATEGORY_UNKNOWN",
    "CATEGORIES",
    "CLEANABLE_CATEGORIES",
    "PROTECTED_CATEGORIES",
    "PROMOTABLE_TEMP_CATEGORIES",
    "StorageError",
    "InsufficientStorageError",
    "StorageVolumeUnavailableError",
    "UnsafeStoragePathError",
    "StorageReservationError",
    "StorageCleanupError",
    "StoragePublishError",
    "DiskUsage",
    "SafetyMarginPolicy",
    "StorageRequirement",
    "CleanupCandidate",
    "CleanupPlanItem",
    "CleanupPlan",
    "CleanupResult",
    "StorageReservation",
    "CompoundReservation",
    "classify_os_error",
    "StorageManager",
]

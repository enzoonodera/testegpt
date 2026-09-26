"""``TemplateEngine`` -- Prompt 36 (Template Engine) + Prompt 37 adaptado
(2 templates oficiais built-in).

====================================================================
0. CONTRATO
====================================================================

Este módulo expõe um serviço de CRUD validado sobre a entidade de
catálogo ``Template`` (``_sistema/domain/models.py``), já migrada desde
``m001_initial.py`` mas nunca consumida por nenhum módulo até este
Prompt. Ele NÃO é um editor visual (isso é o futuro "Layout Mapper",
Prompt 39) e NÃO renderiza nada (não existe Render Engine no produto
ainda -- ``BADGE_TEMPLATE_APPLIED`` continua ``"0"`` em
``media_catalog.py``, arquivo que este módulo não toca).

``Template`` é uma entidade de CATÁLOGO independente de ``Video``/
``Project`` -- sem ``video_id``/``project_id``. Um Template descreve uma
composição reutilizável (background + zonas de vídeo/legenda/texto IA/
logo, ordem de camadas, estilo) em coordenadas normalizadas 0..1, para
funcionar em qualquer resolução final.

====================================================================
1. INFRAESTRUTURA PRÉ-EXISTENTE REUTILIZADA (NUNCA DUPLICADA)
====================================================================

Confirmado por leitura direta ANTES de codificar (nenhuma divergência
encontrada em relação ao que o Prompt descreve -- logo, nenhuma
migration nova é necessária e ``LATEST_SCHEMA_VERSION`` permanece 9):

1. ``_sistema/domain/models.py`` já define ``Template(Entity)``:
   ``name: str``, ``template_type: str = "CUSTOM"``,
   ``source_path: str | None``, ``layout: dict[str, Any]``,
   ``enabled: bool = True`` -- já exportado em ``domain/__init__.py``.
2. A tabela SQLite ``templates`` já existe desde
   ``storage/migrations/m001_initial.py``: colunas
   ``id, schema_version, created_at, updated_at, name, template_type,
   source_path, layout_json, enabled, extra_json`` -- sem FK para
   ``videos``/``projects``. Já mapeada via
   ``EntitySpec(Template, "templates", frozenset({"layout"}),
   frozenset({"enabled"}))`` em ``storage/database.py``. Este módulo usa
   ``LocalDatabase.insert``/``save``/``get``/``list`` genéricos --
   nenhum SQL de escrita além da consulta de idempotência de ingestão
   (ver seção 4).
3. ``AppPaths.templates`` já existe como diretório reservado (mesmo
   nível de ``AppPaths.projects``/``AppPaths.cache``) -- é onde os
   arquivos de imagem de template vivem.
4. ``StorageManager`` já reconhece esse diretório como categoria
   protegida própria (``CATEGORY_TEMPLATE``, mapeada em
   ``_root_category_map`` para ``ap.templates``) -- os mesmos
   ``allocate_temp``/``promote_to_final`` já respeitam essa categoria.
   Este módulo NUNCA escreve diretamente em ``AppPaths.templates`` via
   ``open()``/``shutil`` cru -- todo arquivo gerenciado passa por
   ``allocate_temp(create=True)`` + ``promote_to_final`` (mesmo padrão
   de ``auto_reframe.py``/``captions_engine.py``/``silence_removal.py``).
5. ``media_catalog.py`` já reserva ``BADGE_TEMPLATE_APPLIED``
   (hardcoded ``"0"``) para quando um Template for de fato aplicado a
   um vídeo no futuro -- nenhum badge novo é criado, este arquivo não é
   tocado.

====================================================================
2. ESCOPO DESTE PROMPT (E O QUE FICA DE FORA, DE PROPÓSITO)
====================================================================

DENTRO do escopo:
  - CRUD validado de ``Template`` (create/read/update/list) com
    ``layout`` validado contra o esquema da seção 3 antes de persistir.
  - Vocabulário fechado de ``template_type``: ``BUILT_IN``/``CUSTOM``.
  - Ingestão idempotente dos 2 templates oficiais fornecidos pelo dono
    do produto (seção 4).
  - Metadata de preview/thumbnail (seção 5).

FORA do escopo deste Prompt (rounds futuros do roadmap, citados
explicitamente para não serem antecipados -- princípio 8 do CLAUDE.md):
  - Layout Mapper visual (arrastar/redimensionar zonas numa tela) --
    Prompt 39. Este módulo entrega apenas o MODELO DE DADOS validado.
  - Importador genérico de template (usuário sobe seu próprio PNG/JPG
    e configura zonas na hora) -- Prompt 38. Este módulo só ingere os
    2 arquivos oficiais como semente ``BUILT_IN``, usando o MESMO
    ``TemplateEngine`` genérico que o futuro Importer usará.
  - Render Engine (aplicar de fato um Template a um vídeo) -- ainda
    não existe no produto (confirmado relendo os docstrings de
    ``checkpoints.py``/``captions_engine.py``/``silence_removal.py``/
    ``metadata_manager.py``, todos concordam que isso é trabalho
    futuro).

O código explicitamente NÃO depende de "exatamente 2" templates: a
ingestão itera sobre ``BUILTIN_TEMPLATE_SPECS``, uma tupla de
especificações -- adicionar um 3º/4º/N-ésimo template built-in no
futuro (via update) é apenas adicionar mais uma entrada nessa tupla.
``TemplateEngine.create_template`` aceita qualquer ``template_type``
válido para qualquer template ``CUSTOM`` adicional, coexistindo
normalmente com os ``BUILT_IN`` (prova em
``test_terceiro_template_custom_coexiste_sem_hardcode_de_dois``).

====================================================================
3. ESQUEMA DE ``layout`` (DECISÃO ARQUITETURAL, COM JUSTIFICATIVA)
====================================================================

``Template.layout`` (persistido em ``layout_json``) é um dict com uma
única chave de topo permitida:

    {"zones": [<zone>, ...]}

Fechado deliberadamente a só essa chave (mesma disciplina de
"reject unknown top-level fields" já usada em
``metadata_manager._validate_config_fields``) -- evita que campos
inventados/typos sejam persistidos silenciosamente.

Cada ``<zone>`` é um dict:

    {
        "zone_type": "BACKGROUND" | "VIDEO" | "CAPTION" | "AI_TEXT" | "LOGO",
        "x": float,      # 0.0 .. 1.0
        "y": float,      # 0.0 .. 1.0
        "width": float,  # > 0, x + width <= 1.0 (+ epsilon)
        "height": float, # > 0, y + height <= 1.0 (+ epsilon)
        "style": {...},  # opcional, ver abaixo -- default {}
    }

Geometria validada com o MESMO estilo de
``_sistema/visual_editor.py::_validate_crop``/``_validate_range``
(reimplementado aqui, nunca importado -- ``visual_editor.py`` valida
crop de VÍDEO, um domínio diferente; a disciplina de validação é o que
é reaproveitado, não o código): ``x``/``y`` em ``[0.0, 1.0]`` via
``_validate_range``; ``width``/``height`` estritamente ``> 0``; overflow
de borda (``x + width <= 1.0 + 1e-9``, mesma tolerância de ponto
flutuante do precedente) rejeitado com o mesmo tipo de erro estrutural
que ``visual_editor.py`` usa para geometria inválida.

ORDEM DE CAMADAS (``z_index``): DECIDIDO como IMPLÍCITA pela posição na
lista ``zones`` (índice 0 = camada mais baixa/fundo, último índice =
camada mais alta/topo) -- NÃO um campo explícito ``z_index`` no dict de
cada zona. Justificativa: um campo explícito duplicaria a mesma
informação que a ordem da lista já representa de forma inequívoca (a
lista É ordenada -- Python preserva ordem de listas/JSON arrays), e um
campo redundante criaria uma segunda fonte de verdade que poderia
divergir da posição real na lista (ex.: ``z_index=5`` numa lista de 3
elementos, ou dois elementos com o mesmo ``z_index``) -- exatamente o
tipo de "segunda fonte de verdade" que o item 7 do GATE ADVERSARIAL
(CLAUDE.md) pede para evitar. A ordem-por-posição é validada
estruturalmente: a PRIMEIRA zona da lista deve ser ``BACKGROUND``
(camada mais baixa, conceitualmente correto -- é o fundo de tudo).

CARDINALIDADE por ``zone_type`` (decidida a partir da inspeção visual
dos 2 templates oficiais fornecidos, sem travar a arquitetura numa
contagem que o roadmap não pediu):
  - ``BACKGROUND``: exatamente 1, sempre a primeira zona da lista.
    Geometria IMPOSTA (não livre) a cobrir o frame inteiro
    (``x=0, y=0, width=1, height=1``) -- decisão: um "background" é
    definicionalmente a tela inteira nesta versão do produto (os 2
    templates oficiais confirmam isso -- ambos são uma imagem de fundo
    de quadro inteiro); permitir um background parcial livre
    adicionaria um grau de liberdade que nenhum dos 2 templates
    fornecidos precisa e que o roadmap não pediu explicitamente
    (overengineering sem benefício concreto, item vedado pelo
    CLAUDE.md). Se uma necessidade real de background parcial surgir
    num Prompt futuro, isso é uma migração de schema decidida com
    justificativa própria naquele momento, não hoje.
  - ``VIDEO``: exatamente 1. Um Template sem zona de vídeo não faz
    sentido neste produto (é um editor de vídeo curto) -- reforçado
    pelo princípio K/J do CLAUDE.md, onde Template sempre existe em
    função de um vídeo a ser posicionado.
  - ``CAPTION``, ``AI_TEXT``, ``LOGO``: 0..N cada (zero, uma, ou
    múltiplas). Nenhum dos 2 templates oficiais fornecidos define uma
    posição dedicada de legenda/texto-IA/logo -- por isso a proposta
    inicial de layout de ambos (seção 4) contém apenas ``BACKGROUND`` +
    ``VIDEO``. A cardinalidade 0..N (em vez de travar em exatamente 1)
    é o que permite, por exemplo, múltiplas zonas de logo para
    multi-marca, ou zero zonas de legenda quando o usuário prefere
    burn-in de legenda fora do Template -- sem exigir uma migração de
    schema para isso no futuro.

ESTILO (``style``, opcional por zona, default ``{}``): o roadmap pede
apenas "styling" de forma vaga, sem especificar campos -- o CLAUDE.md
veda inventar campos sem fundamento. O esquema mínimo defensável
escolhido, fechado (chaves desconhecidas rejeitadas, mesmo padrão de
``_validate_config_fields``):
  - ``opacity``: float em ``[0.0, 1.0]``, opcional.
  - ``background_color``: string hex ``#RRGGBB`` ou ``#RRGGBBAA``,
    opcional -- MESMO regex/normalização (uppercase) de
    ``_sistema/captions_style.py::_validate_color`` (reimplementado
    aqui pelo mesmo motivo de ``visual_editor.py`` acima: nunca importar
    um módulo irmão de decisão/processamento -- só o padrão é
    reaproveitado).
Ambos são aplicáveis a qualquer ``zone_type`` (ex.: uma zona de legenda
pode querer uma placa de fundo translúcida; uma zona de logo pode
querer opacidade reduzida) -- generalidade suficiente sem inventar
campos específicos de um único tipo de zona que o roadmap não pediu.
Nenhum outro campo de estilo (fonte, animação, etc.) é aceito nesta
etapa -- esses já têm um precedente dedicado e MUITO mais rico em
``captions_style.py`` para o domínio de legenda especificamente, e
inventar um equivalente aqui para Template seria overengineering
antecipado de uma funcionalidade (estilização fina de texto dentro de
um Template) que nenhum Prompt pediu ainda.

====================================================================
4. INGESTÃO DOS 2 TEMPLATES OFICIAIS (BUILT_IN)
====================================================================

Os 2 arquivos PNG originais (941x1672px, 9:16) fornecidos pelo dono do
produto foram copiados para ``assets/templates_oficiais/`` na raiz do
repositório (ao lado de ``_sistema/``/``tests/`` -- mesmo nível
sugerido no próprio Prompt). Essa pasta é o INSUMO de ingestão (fonte
de verdade dos bytes originais), NUNCA o destino final gerenciado --
o destino gerenciado é ``AppPaths.templates`` (categoria
``CATEGORY_TEMPLATE`` do ``StorageManager``).

MECANISMO DE CÓPIA (decisão, investigação prévia obrigatória): reli
``_sistema/source_import.py`` -- ``LocalFileImporter`` hoje só
REFERENCIA o arquivo original (nunca copia); ``UrlImporter`` copia para
storage gerenciado. Como o Prompt exige que os 2 arquivos originais
NUNCA sejam sobrescritos/alterados, o padrão correto é COPIAR (nunca
referenciar in-place) -- mesmo padrão de
``allocate_temp(create=True)`` + escrita + ``promote_to_final``
já usado por ``auto_reframe.py``/``captions_engine.py``/
``silence_removal.py`` para todo artefato que se torna
``FINAL_ARTIFACT``-equivalente. Aqui o artefato final é
``CATEGORY_TEMPLATE`` em vez de ``FINAL_ARTIFACT``, mas o mecanismo de
``StorageManager`` é o mesmo: nunca ``open()``/``shutil`` diretamente
num caminho inventado sob ``AppPaths.templates``.

NOME DE ARQUIVO NO DESTINO GERENCIADO: DETERMINÍSTICO
(``builtin_<slug>.png``), DELIBERADAMENTE DIFERENTE do padrão UUID4 de
``allocate_temp`` (que é só para o TEMP intermediário, nunca para o
nome final) e do padrão de nome-por-cache-key usado por artefatos
derivados por job (``captions_engine.py``/``silence_removal.py``).
Justificativa: os 2 templates built-in são um catálogo pequeno e FIXO
(não um artefato por-job/por-vídeo derivado de um cache key de
conteúdo) -- um nome determinístico e legível é apropriado aqui, e
continua seguro porque ``promote_to_final`` já garante que o destino é
resolvido dentro da categoria protegida ``CATEGORY_TEMPLATE`` e nunca
pode colidir com um caminho estrutural/protegido de outra categoria.

IDEMPOTÊNCIA: cada especificação em ``BUILTIN_TEMPLATE_SPECS`` carrega
um ``slug`` estável (ex.: ``"moldura_tech_azul"``), persistido em
``Template.extra["builtin_slug"]`` -- NUNCA no campo mutável ``name``
(que o usuário/Layout Mapper futuro pode livremente renomear sem
quebrar a identidade de ingestão). A checagem "já ingerido?" roda sob
UMA transação ``BEGIN IMMEDIATE`` (``LocalDatabase.transaction()``,
mesma disciplina do item 2 do GATE ADVERSARIAL -- leitura + decisão +
escrita atômicas, testado com duas instâncias concorrentes em
``test_ingestao_concorrente_duas_instancias_nao_duplica``):
  - Se um ``Template`` com ``template_type == "BUILT_IN"`` e
    ``extra["builtin_slug"] == slug`` já existe: NENHUM campo é
    sobrescrito (nome/layout/enabled preservados como estão -- um
    futuro Layout Mapper pode já ter ajustado o layout, e re-rodar a
    ingestão não pode descartar esse ajuste silenciosamente). Apenas
    garante que o arquivo gerenciado ainda existe fisicamente
    (auto-cura: se foi apagado por fora, é recopiado do arquivo
    original em ``assets/templates_oficiais/``).
  - Se não existe: cria o arquivo gerenciado + insere a linha
    ``Template`` nova, com ``template_type="BUILT_IN"``,
    ``source_path`` apontando para a cópia gerenciada,
    ``layout`` inicial (zonas ``BACKGROUND`` + ``VIDEO`` propostas --
    ver abaixo), ``enabled=True``.

``enabled=True`` por padrão para os built-ins: decisão direta do texto
literal do roadmap ("Eles devem vir prontos para uso").

ZONA ``VIDEO`` INICIAL POR TEMPLATE (proposta editável, NÃO definitiva
-- é o futuro Layout Mapper que permitirá ajuste fino visual; aqui é
só uma proposta inicial razoável derivada de inspeção visual direta
dos 2 arquivos):
  - "Moldura Tech Azul" (fundo escuro com frame neon azul concentrado
    nos 4 cantos, área central aberta): ``VIDEO`` proposta em
    ``x=0.04, y=0.05, width=0.92, height=0.90`` -- deixa margem para os
    acentos decorativos de canto não serem cobertos pelo vídeo.
  - "Moldura Branca Clássica" (fundo branco liso com borda fina preta
    inset): ``VIDEO`` proposta em ``x=0.03, y=0.02, width=0.94,
    height=0.96`` -- a área útil é quase o frame inteiro, respeitando
    apenas a borda fina.

====================================================================
5. DECISÃO: PREVIEW/THUMBNAIL (METADATA-ONLY, NÃO ARQUIVO NOVO)
====================================================================

Investigação prévia obrigatória: confirmado que ``requirements.txt``
não tem Pillow/PIL. Confirmado por teste direto que ``MediaProbe``
REJEITA imagem estática (``valid=False``,
``error_code='DURACAO_INVALIDA'`` -- ``MediaProbe`` espera duração de
vídeo, que uma imagem estática não tem) -- não reaproveitável sem
alterar ``media_probe.py`` (vedado -- nunca tocar módulo irmão).
Confirmado por teste direto que ``ffprobe`` (já uma dependência de
produção comprovada, mesmo espírito "infra-primeiro" de todo Part B
anterior) lê largura/altura de PNG estático normalmente.

DUAS LEITURAS AVALIADAS:
  (a) Gerar um arquivo de thumbnail redimensionado via
      ``ffmpeg -i entrada.png -vf scale=W:H saida.png`` (mesmo comando
      já citado no próprio Prompt como precedente).
  (b) Persistir apenas METADATA de preview (dimensões + caminho para o
      original de resolução plena) sem gerar um novo arquivo físico
      nesta etapa.

DECISÃO: (b), metadata-only. Justificativa: não existe hoje NENHUMA
tela de UI que consuma um thumbnail físico -- gerar um arquivo extra
agora seria overengineering sem consumidor real (vedado pelo
CLAUDE.md, seção "REVISÃO CRÍTICA"), e a resolução fornecida
(941x1672px) já é perfeitamente adequada para uma futura UI
redimensionar via CSS/framework diretamente a partir do
``source_path`` -- não há ganho concreto em pré-gerar uma cópia menor
antes de existir uma tela que precise disso. Se um Prompt futuro
introduzir uma tela de galeria de templates com uma necessidade real
de thumbnail pré-computado (ex.: performance com centenas de templates
customizados), essa é a hora de revisitar esta decisão com
justificativa própria -- não antecipar agora.
``TemplateEngine.get_preview_info`` expõe ``source_path``, ``width``,
``height`` (dimensões lidas via ``ffprobe`` no momento da ingestão e
persistidas em ``Template.extra`` -- nunca recalculadas a cada leitura).

====================================================================
6. VOCABULÁRIO FECHADO
====================================================================

``TEMPLATE_TYPES = {"BUILT_IN", "CUSTOM"}`` -- mínimo pedido
explicitamente pelo Prompt. ``BUILT_IN`` é reservado para templates
que vêm prontos com o produto (só escritos por
``ingest_builtin_templates``); ``CUSTOM`` é o default para qualquer
template criado via ``create_template`` (inclusive o futuro Importer
genérico do Prompt 38).

``ZONE_TYPES = {"BACKGROUND", "VIDEO", "CAPTION", "AI_TEXT", "LOGO"}``
-- exatamente a lista literal do roadmap ("background; video zone;
caption zone; AI text zone; logo/image zones").

====================================================================
7. ISOLAMENTO (mesma disciplina AST de todo Prompt anterior)
====================================================================

Este módulo NÃO importa nenhum módulo irmão de decisão/processamento
paralelo (``visual_editor.py``, ``captions_style.py``,
``audio_engine.py``, ``auto_reframe.py``, ``captions_engine.py``,
``silence_removal.py``, ``metadata_manager.py``) nem
``media_catalog.py`` nem nada de Geração 1
(``limpar_metadados_oficial.py``). Reaproveita apenas os padrões de
validação/storage já documentados acima, reimplementados localmente.

====================================================================
8. PROMPT 39 -- LAYOUT MAPPER (OPERAÇÕES ATÔMICAS DE ZONA, SEM UI)
====================================================================

Este Prompt adiciona ao ``TemplateEngine`` operações INDIVIDUAIS e
atômicas sobre zonas de um ``layout`` já existente (adicionar/mover/
redimensionar/remover/reordenar UMA zona por vez), em vez de exigir que
o chamador sempre substitua o ``layout`` inteiro via
``update_template(layout=...)``. Continua SEM qualquer frontend/UI
visual (arrastar/soltar numa tela) -- isso permanece Prompt 64, como o
próprio Prompt 39 deixa explícito. O que sai daqui é só o MOTOR de
posicionamento: uma API programática que qualquer UI futura vai chamar.

--------------------------------------------------------------------
8.1 -- DECISÃO CENTRAL: COMO ENDEREÇAR UMA ZONA ESPECÍFICA
--------------------------------------------------------------------

Investigação prévia confirmou dois fatos que tornam esta decisão
obrigatória (não presumível): (1) zonas hoje NÃO têm nenhum campo de
identidade -- ``_validate_zone`` reconstrói um dict novo só com
``zone_type/x/y/width/height/style``, descartando silenciosamente
qualquer campo desconhecido; (2) a cardinalidade de ``CAPTION``/
``AI_TEXT``/``LOGO`` é 0..N -- ou seja, já HOJE é possível ter, por
exemplo, duas zonas ``LOGO`` na mesma lista, e não existe nenhuma forma
de dizer "mova a segunda zona LOGO" sem ambiguidade.

Três opções avaliadas (como o próprio Prompt pede):

  (a) Campo de identidade estável ``zone_id`` (UUID), gerado na
      criação da zona e persistido dentro do próprio dict da zona em
      ``layout_json``.
  (b) Endereçamento por índice posicional na lista, mitigado por
      sempre reler a lista atual DENTRO da mesma transação
      ``BEGIN IMMEDIATE`` antes de aplicar a operação.
  (c) Uma terceira abordagem alternativa.

DECISÃO: **(a) -- ``zone_id`` UUID estável por zona.**

Justificativa: a opção (b) resolve APENAS a corrida entre leitura e
escrita dentro de uma única chamada (o que a transação atômica já
resolveria de qualquer forma, ver 8.3) -- ela NÃO resolve o problema
de fundo, que é IDENTIDADE ao longo de VÁRIAS chamadas separadas de
API. Um índice é inerentemente instável entre chamadas: se o chamador
guarda "a zona LOGO que eu quero mover é o índice 3" e, antes da
próxima chamada, outra operação (sua ou de outro processo) insere ou
remove uma zona antes do índice 3, o índice 3 passa a apontar para uma
zona DIFERENTE -- um "mover a zona errada" silencioso, exatamente o
tipo de bug que uma UI de arrastar/soltar (Prompt 64) dispararia o
tempo todo em uso normal (usuário adiciona uma zona LOGO nova, depois
tenta mover a zona LOGO antiga usando um índice que a UI guardou antes
da adição). Reler a lista dentro da transação (mitigação de (b)) não
resolve isso -- resolve corrida de ESCRITA CONCORRENTE, não resolve
"qual zona o CHAMADOR quis dizer" quando a própria lista mudou de forma
legítima entre a exibição na UI e o clique do usuário.

``zone_id`` é o padrão padrão exatamente para esta classe de problema
(identidade estável através de múltiplas interações, como uma chave
primária de linha de banco, ou uma "key" de componente de UI React) --
e o custo de implementação é baixo: ``new_uuid()``/``_validate_uuid``
já existem em ``_sistema/domain/models.py`` (``new_uuid`` já exportado
em ``domain/__init__.py``, reaproveitado aqui via import direto;
``_validate_uuid`` é privado do módulo domain, então sua DISCIPLINA de
validação -- não o código -- é reimplementada localmente como
``_validate_zone_id``, mesmo padrão de isolamento já usado em todo o
resto deste arquivo).

``validate_layout``/``_validate_zone`` foram estendidos (retrocompatível)
para aceitar um campo opcional ``zone_id`` por zona: se ausente, um
novo UUID é gerado (``new_uuid()``); se presente, validado como UUID
textual e PRESERVADO (nunca regenerado) -- o que torna o campo estável
através de sucessivas validações/escritas do mesmo layout (ex.:
``update_template(layout=<layout já lido de volta>)`` preserva os
mesmos ids). ``_validate_zones`` rejeita ``zone_id`` duplicado dentro
do mesmo layout (``LayoutInvalidoError``) -- unicidade é parte da
garantia de identidade estável. Todo caller PRÉ-EXISTENTE
(``create_template``, ``ingest_builtin_templates``,
``TemplateImporter.import_template`` do Prompt 38) continua funcionando
sem nenhuma mudança -- nenhum deles jamais passava ``zone_id``, então
todos simplesmente passam a ganhar um automaticamente, de forma
transparente.

--------------------------------------------------------------------
8.2 -- DECISÃO: ONDE VIVEM AS NOVAS OPERAÇÕES (MÓDULO)
--------------------------------------------------------------------

Avaliado por analogia direta ao Prompt 38 (``TemplateImporter`` virou
um módulo NOVO, ``_sistema/template_importer.py``, que DEPENDE de
``TemplateEngine`` em vez de estender a classe). A mesma escolha NÃO se
aplica aqui -- decisão: **estender ``TemplateEngine`` diretamente**
(métodos novos na mesma classe, mesmo arquivo), não criar um
``_sistema/layout_mapper.py`` separado.

Justificativa: ``TemplateImporter`` nunca precisou de acesso
transacional de leitura-decisão-escrita contra a tabela ``templates``
-- ele só chamava ``TemplateEngine.create_template`` (um INSERT único,
sem leitura prévia da MESMA linha). As operações de zona são o oposto:
cada uma é um ler-a-linha-decidir-escrever-a-MESMA-linha sob UMA
transação ``BEGIN IMMEDIATE`` (ver 8.3) -- ou seja, precisam de acesso
direto a ``self._database``/``self._database.transaction()``, os
MESMOS recursos que ``update_template``/``ingest_builtin_templates`` já
usam. Colocar isso num módulo separado obrigaria esse módulo a manter
seu PRÓPRIO acesso a ``self._database`` (ou a reimplementar
``update_template`` por fora, chamando ``get_template``+
``update_template`` como duas chamadas separadas -- reintroduzindo
exatamente a janela de corrida que este Prompt existe para fechar). O
item 1 do GATE ADVERSARIAL (CLAUDE.md) pede explicitamente para nunca
ter "duas facades diferentes" escrevendo na mesma tabela -- um módulo
``layout_mapper.py`` que também tivesse seu próprio
``LocalDatabase.transaction()`` contra ``templates`` seria exatamente
essa segunda facade. Portanto: ``TemplateEngine`` continua sendo o
ÚNICO dono de leitura/escrita da entidade ``Template`` (mesmo princípio
já documentado na seção 0 deste módulo), e as 5 operações de zona são
métodos novos dessa mesma classe.

--------------------------------------------------------------------
8.3 -- CORREÇÃO: ``update_template`` NÃO ERA ATÔMICO (TOCTOU)
--------------------------------------------------------------------

Investigação prévia (item 2 do GATE ADVERSARIAL) encontrou que
``update_template`` fazia ``self.get_template(template_id)`` (uma
leitura, SEM transação) seguido de ``self._database.save(entity)``
(uma escrita separada, SEM transação) -- duas operações SQLite
distintas, não atômicas entre si. Duas chamadas concorrentes de
``update_template``/qualquer operação de zona contra o MESMO
``template_id`` podiam se intercalar (leitura de A, leitura de B,
escrita de A, escrita de B -- a escrita de B, baseada numa leitura
ANTES da escrita de A, silenciosamente descarta a mudança de A:
last-write-wins sem aviso). Isso já era uma janela de corrida
pré-existente em ``update_template`` de forma geral (bug real, não
hipotético), mas o Prompt aponta corretamente que ela se torna MUITO
mais fácil de disparar na prática assim que existe uma API de mover
UMA zona por vez (múltiplas chamadas pequenas e frequentes contra o
mesmo template, ex.: uma UI de arrastar-e-soltar futura disparando uma
chamada por frame de arraste).

CORREÇÃO (mínima, sem mudar a assinatura pública nem o comportamento
observável de ``update_template`` -- só a atomicidade interna):
``update_template`` agora abre ``self._database.transaction()`` e faz
a leitura (``self._database.get(Template, template_id, connection=conn)``)
E a escrita (``self._database.save(entity, connection=conn)``) dentro
da MESMA ``BEGIN IMMEDIATE``, mesmo padrão já comprovado em
``ingest_builtin_templates``. As 5 operações novas de zona reaproveitam
o MESMO helper interno (``_read_modify_write``) -- nenhuma duplicação
de lógica de transação entre ``update_template`` e as operações de
zona. Testado explicitamente com duas instâncias concorrentes usando
``threading.Barrier`` (nunca ``time.sleep``), mesmo estilo de
``test_ingestao_concorrente_duas_instancias_nao_duplica``.

--------------------------------------------------------------------
8.4 -- DECISÃO: MOVER/REDIMENSIONAR A ZONA ``BACKGROUND``
--------------------------------------------------------------------

DECISÃO: **rejeitar explicitamente** (erro estruturado
``ZonaProtegidaError``), nunca um no-op silencioso.

Justificativa: um no-op silencioso violaria a mesma disciplina de
"nunca falhar silenciosamente"/"sempre erro estruturado quando uma
operação não pode ser honrada" usada em TODO o resto deste projeto
(ex.: ``TemplateImportError`` com ``.code`` em vez de aceitar e
ignorar um arquivo inválido). Um futuro chamador (a UI de arrastar do
Prompt 64) que tentasse mover a zona BACKGROUND precisa de um jeito
programático de SABER que a operação não é permitida (para, por
exemplo, desabilitar a alça de arraste daquela zona especificamente),
não descobrir por tentativa-e-erro que "nada aconteceu". A mesma
rejeição explícita se aplica a ``remove_zone`` (remover a única
``BACKGROUND``/``VIDEO`` sempre violaria a cardinalidade mínima
obrigatória -- rejeitado, nunca permitido) e a ``reorder_zone`` sobre a
zona ``BACKGROUND`` (que deve permanecer sempre a primeira da lista --
ver seção 3). A geometria da zona ``BACKGROUND`` continua travada ao
frame inteiro por ``_validate_zone`` independentemente disso -- a
rejeição aqui é uma camada adicional, mais cedo e com uma mensagem mais
específica (``ZonaProtegidaError`` em vez de ``GeometriaInvalidaError``
genérico), sobre a INTENÇÃO da operação, não só sobre o resultado.

--------------------------------------------------------------------
8.5 -- AS 5 OPERAÇÕES
--------------------------------------------------------------------

Todas seguem o mesmo formato: recebem ``template_id`` + os campos
relevantes, retornam o ``Template`` atualizado (contrato uniforme).
Cada uma constrói uma lista de zonas CANDIDATA (com a mutação aplicada)
e chama ``validate_layout({"zones": candidata})`` -- ou seja, TODA a
validação de geometria/vocabulário/cardinalidade já existente é 100%
reaproveitada, nunca reimplementada em paralelo para o caso "zona
única". Isso também significa que qualquer regra futura adicionada a
``validate_layout`` automaticamente se aplica a estas operações sem
nenhuma mudança aqui.

  - ``add_zone(template_id, zone, *, index=None)``: insere uma zona
    nova. Nunca aceita ``zone_type="BACKGROUND"`` (cardinalidade já
    trava em 1, imposta desde a criação do template -- rejeitado como
    ``ZonaProtegidaError`` antes mesmo de tentar validar o layout
    inteiro). ``index=None`` insere no fim da lista; um ``index``
    explícito é sempre ajustado para nunca ficar antes da posição 1
    (posição 0 é reservada à ``BACKGROUND``).
  - ``move_zone(template_id, zone_id, *, x, y)``: atualiza só
    ``x``/``y`` da zona identificada por ``zone_id``; ``width``/
    ``height``/``style``/``zone_type`` preservados. Rejeitado para
    ``BACKGROUND`` (8.4).
  - ``resize_zone(template_id, zone_id, *, width, height)``: simétrico
    a ``move_zone`` para ``width``/``height``. Rejeitado para
    ``BACKGROUND`` (8.4).
  - ``remove_zone(template_id, zone_id)``: remove a zona identificada.
    Rejeitado para ``BACKGROUND``/``VIDEO`` (cardinalidade mínima,
    8.4) -- como cada uma tem cardinalidade EXATA 1, remover a única
    existente sempre violaria a regra, então a rejeição é direta pelo
    ``zone_type``, sem precisar nem tentar revalidar o layout
    resultante.
  - ``reorder_zone(template_id, zone_id, *, new_index)``: move a zona
    para uma nova posição na lista. Rejeitado para ``BACKGROUND``
    (deve continuar sempre primeira) e rejeitado se ``new_index == 0``
    ao mover outra zona (posição 0 é reservada à ``BACKGROUND`` --
    mover qualquer outra zona para lá deslocaria a ``BACKGROUND`` para
    fora da posição 0, quebrando o invariante da seção 3).

Endereçar um ``zone_id`` inexistente no template levanta
``ZonaNaoEncontradaError`` (novo erro estruturado, distinto de
``TemplateNaoEncontradoError`` -- este último é sobre o TEMPLATE não
existir, aquele é sobre a ZONA dentro de um template existente não
existir).

====================================================================
9. PROMPT 40 -- DYNAMIC CONTENT (texto dinâmico em zona CAPTION/AI_TEXT)
====================================================================

Este Prompt adiciona um sub-schema opcional ``content`` (DynamicContent)
anexável a uma zona ``CAPTION``/``AI_TEXT`` -- rejeitado estruturalmente
em ``BACKGROUND``/``VIDEO``/``LOGO``. Continua SEM qualquer chamada real
de IA (nenhum ``ContentEngine``/Ollama existe no produto ainda -- Fase 7
do roadmap, não antecipada) e SEM renderização (sem Render Engine, mesma
fronteira já estabelecida desde o Prompt 36).

--------------------------------------------------------------------
9.1 -- VOCABULÁRIO E CAMPOS OBRIGATÓRIOS/PROIBIDOS POR ``content_type``
--------------------------------------------------------------------

``CONTENT_TYPES = {"FIXED_TEXT", "AI_HOOK", "AI_TITLE", "AI_SUMMARY",
"AI_QUESTION", "AI_CTA", "CUSTOM_AI"}`` -- exatamente a lista literal do
roadmap. Qualquer zona ``CAPTION``/``AI_TEXT`` pode usar QUALQUER
``content_type`` -- não há mapeamento fixo 1:1 entre ``zone_type`` e
``content_type`` (um ``AI_TEXT`` pode conter um ``AI_CTA``, uma
``CAPTION`` pode conter um ``FIXED_TEXT``, etc.).

Dois campos de texto distintos, cada um exigido/proibido conforme o
``content_type`` (decisão, justificada pelo próprio texto do roadmap:
"os demais AI_* NÃO têm texto nenhum ainda"):
  - ``text``: o texto LITERAL a exibir agora. Exigido APENAS para
    ``FIXED_TEXT`` -- o único ``content_type`` cujo valor final já é
    conhecido nesta etapa. PROIBIDO para todos os outros.
  - ``instruction``: uma instrução/prompt livre que vai guiar o futuro
    ``ContentEngine`` (fora de escopo). Exigido APENAS para
    ``CUSTOM_AI`` -- o único ``content_type`` que já carrega uma
    instrução customizada do usuário hoje, mesmo sem geração real ainda.
    PROIBIDO para todos os outros.
  - ``AI_HOOK``/``AI_TITLE``/``AI_SUMMARY``/``AI_QUESTION``/``AI_CTA``:
    NENHUM dos dois campos é aceito -- o valor final desses tipos só
    existirá quando o ``ContentEngine`` rodar (fora de escopo); inventar
    um texto de exemplo agora seria dado fictício persistido como se
    fosse real, exatamente o tipo de risco que a seção "REVISÃO CRÍTICA"
    do CLAUDE.md pede para evitar.

Presença de ``text``/``instruction`` onde é proibido é rejeitada
ESTRUTURALMENTE (``CampoInvalidoError``), nunca descartada em silêncio
-- mesma disciplina de "reject unknown field" já usada em
``_validate_style``/``validate_layout``.

--------------------------------------------------------------------
9.2 -- DEMAIS CAMPOS: ``max_lines``/``max_chars``/``font_size``/
``font_size_min``/``alignment``
--------------------------------------------------------------------

``max_lines`` (int, faixa ``(1, 10)``) e ``max_chars`` (int, faixa
``(1, 500)``): faixas PRÓPRIAS deste módulo, reimplementadas (nunca
importadas) a partir da mesma disciplina de
``captions_style.py::LINES_RANGE``/``MAX_WORDS_RANGE`` -- mas com
limites mais generosos, porque o domínio é diferente: legenda
queimada (``captions_style.py``) é deliberadamente curta e animada
palavra-por-palavra (1-5 linhas, 1-20 palavras), enquanto uma zona de
Template pode carregar um ``AI_SUMMARY`` inteiro (um parágrafo curto),
não só uma legenda. ``font_size``/``font_size_min`` (float, faixa
``(8.0, 200.0)``, mesma ordem de grandeza de
``captions_style.py::SIZE_RANGE``, reimplementada) -- ``font_size_min``
é sempre rejeitado estruturalmente se maior que ``font_size``
(``CampoInvalidoError``). Todos os 4 são OBRIGATÓRIOS quando ``content``
está presente -- nenhum default silencioso para um limite que afeta
diretamente "nunca deixar texto sair do canvas" (mesmo espírito de
x/y/width/height de zona, que também não têm default).

``alignment`` (``"LEFT"``/``"CENTER"``/``"RIGHT"``, reimplementado a
partir do mesmo vocabulário de ``captions_style.py::ALIGNMENTS``, nunca
importado): ÚNICO campo com default (``"CENTER"``) -- puramente
cosmético (ao contrário dos 4 limites acima, não afeta a garantia de
"nunca sair do canvas"), então um default razoável não é uma decisão de
alto risco.

--------------------------------------------------------------------
9.3 -- AUTO-FIT: HEURÍSTICA DETERMINÍSTICA, NUNCA MEDIÇÃO REAL
--------------------------------------------------------------------

Investigação prévia confirmou: ``requirements.txt`` não tem Pillow nem
qualquer biblioteca de métrica de fonte real, e não existe Render Engine
no produto. Logo "auto-fit" aqui NÃO pode ser medição real de
pixels/glifos -- é uma função PURA e determinística
(``apply_auto_fit``), documentada como HEURÍSTICA, nunca como
renderização, para ser consumida pelo futuro Render Engine.

MODELO: capacidade de caracteres por linha é INVERSAMENTE proporcional
ao ``font_size``, calibrada por uma referência arbitrária documentada
(``_AUTOFIT_REFERENCE_FONT_SIZE = 32.0`` -> ``_AUTOFIT_REFERENCE_
CHARS_PER_LINE = 18`` caracteres/linha -- mesma ordem de grandeza de
``captions_style.py::MAX_WORDS_RANGE`` para uma linha curta de
legenda). Não há informação de largura real de pixel da zona
disponível nesta etapa (``width`` do zone é uma FRAÇÃO normalizada do
frame, não pixels de uma resolução final concreta) -- por isso o
modelo é parametrizado só por ``font_size``, nunca por geometria de
zona, e documentado explicitamente como uma aproximação a ser
substituída por medição real quando o Render Engine existir.

ALGORITMO (3 passos, sempre nesta ordem):
  1. Corte duro por ``max_chars`` -- SEMPRE aplicado primeiro,
     independente de ``font_size``, com um marcador de truncamento
     (``"…"``) substituindo o último caractere quando corta. Garante
     ``len(resultado) <= max_chars`` incondicionalmente.
  2. Se o texto (já cortado pelo passo 1) precisar de mais linhas que
     ``max_lines`` no ``font_size`` atual, reduz em passos controlados
     de ``_AUTOFIT_FONT_STEP = 1.0`` até ``font_size_min``,
     recalculando a capacidade a cada passo, parando assim que couber.
  3. Se AINDA não couber em ``max_lines`` mesmo em ``font_size_min``
     (texto extremamente longo), corta mais uma vez para o número exato
     de caracteres que cabe em ``max_lines`` linhas naquele tamanho de
     fonte -- garantia matemática (`ceil(a/b) <= m` sse `a <= b*m`) de
     que o resultado final NUNCA excede ``max_lines``.

GARANTIA PROVADA POR TESTE (a tradução testável de "nunca deixar texto
sair do canvas" nesta etapa, sem render engine para overflow visual
real): para QUALQUER texto de entrada, ``len(resultado.text) <=
max_chars`` E ``resultado.lines <= max_lines`` SEMPRE, incluindo o caso
adversarial de um texto gigante muito maior que qualquer limite.

``apply_auto_fit`` roda sobre texto ``FIXED_TEXT`` fornecido agora (ou
qualquer string sintética de teste) -- para os ``content_type`` ``AI_*``
sem texto ainda, a função continua disponível e será aplicada no
futuro sobre o texto que o ``ContentEngine`` gerar; nenhum texto de
exemplo é inventado aqui para "testar" esses tipos.

DECISÃO: ``apply_auto_fit`` NÃO é chamada automaticamente ao
validar/persistir ``content`` -- fica como uma função PURA exportada,
standalone, consumida sob demanda (pelos testes deste Prompt, e pelo
futuro Render Engine). Justificativa: armazenar um resultado de
auto-fit dentro de ``layout_json`` seria um valor DERIVADO cacheado sem
nenhum consumidor real ainda (mesmo raciocínio já documentado na seção
5 -- decisão de preview metadata-only: "não existe hoje nenhuma tela
que consuma isso, gerar/persistir antecipadamente seria overengineering
sem benefício concreto, vedado pelo CLAUDE.md"). Quando o Render Engine
existir, ele chama ``apply_auto_fit(texto_resolvido, **content)`` no
momento de renderizar -- nunca um valor persistido que poderia ficar
desatualizado se ``font_size``/``max_lines`` forem editados depois.

--------------------------------------------------------------------
9.4 -- INTEGRAÇÃO COM AS OPERAÇÕES DE ZONA (PROMPT 39)
--------------------------------------------------------------------

``TemplateEngine.set_zone_content(template_id, zone_id, content)`` --
nova operação atômica, MESMO padrão das 5 operações do Prompt 39:
reaproveita ``_read_modify_write``, reconstrói a lista de zonas com o
``content`` da zona endereçada substituído, e revalida o layout INTEIRO
via ``validate_layout`` (reaproveitamento total, nunca validação
paralela). Rejeita com ``ZonaProtegidaError`` (reaproveitado -- mesma
semântica de "operação não permitida para este tipo de zona" já usada
em ``move_zone``/``resize_zone`` para ``BACKGROUND``, nunca um erro
novo e quase-duplicado) se a zona endereçada não for ``CAPTION``/
``AI_TEXT``. ``content=None`` remove o ``content`` existente da zona
(decisão: suportar limpar o conteúdo é tão necessário quanto atribuí-lo
-- sem isso não haveria como reverter uma zona para "sem conteúdo
ainda" via API).

``add_zone`` já aceita ``content`` opcional na criação SEM nenhuma
mudança de assinatura -- ``content`` é só mais um campo do dict ``zone``
passado (exatamente como ``style`` já funciona desde o Prompt 36),
validado pelo mesmo ``_validate_zone`` que ``add_zone`` já chama por
dentro de ``validate_layout``. Nenhum parâmetro novo foi necessário.

RETROCOMPATIBILIDADE: toda zona ``CAPTION``/``AI_TEXT`` já existente
(criada antes deste Prompt) simplesmente não tem a chave ``content`` --
continua válida sem nenhuma migração ou re-validação forçada
(``content`` é opcional, ausência é o estado padrão, mesmo padrão já
usado para ``zone_id`` no Prompt 39).
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID

from .app_paths import AppPaths
from .domain import Template, new_uuid
from .storage.database import LocalDatabase
from .storage_manager import StorageManager

# ---------------------------------------------------------------------------
# Vocabulários fechados
# ---------------------------------------------------------------------------

TEMPLATE_TYPE_BUILT_IN = "BUILT_IN"
TEMPLATE_TYPE_CUSTOM = "CUSTOM"
TEMPLATE_TYPES = frozenset({TEMPLATE_TYPE_BUILT_IN, TEMPLATE_TYPE_CUSTOM})

ZONE_TYPE_BACKGROUND = "BACKGROUND"
ZONE_TYPE_VIDEO = "VIDEO"
ZONE_TYPE_CAPTION = "CAPTION"
ZONE_TYPE_AI_TEXT = "AI_TEXT"
ZONE_TYPE_LOGO = "LOGO"
ZONE_TYPES = frozenset(
    {ZONE_TYPE_BACKGROUND, ZONE_TYPE_VIDEO, ZONE_TYPE_CAPTION, ZONE_TYPE_AI_TEXT, ZONE_TYPE_LOGO}
)

# Zonas que podem carregar DynamicContent -- ver seção 9 da docstring do módulo.
_CONTENT_ELIGIBLE_ZONE_TYPES = frozenset({ZONE_TYPE_CAPTION, ZONE_TYPE_AI_TEXT})

_GEOMETRY_EPSILON = 1e-9
_STYLE_FIELDS = frozenset({"opacity", "background_color"})
_COLOR_RE_6 = re.compile(r"^#[0-9A-Fa-f]{6}$")
_COLOR_RE_8 = re.compile(r"^#[0-9A-Fa-f]{8}$")

_FFPROBE_TIMEOUT_SECONDS = 20

# ---------------------------------------------------------------------------
# Vocabulário/faixas de DynamicContent (Prompt 40) -- ver seção 9 da
# docstring do módulo para a justificativa de cada faixa.
# ---------------------------------------------------------------------------

CONTENT_TYPE_FIXED_TEXT = "FIXED_TEXT"
CONTENT_TYPE_AI_HOOK = "AI_HOOK"
CONTENT_TYPE_AI_TITLE = "AI_TITLE"
CONTENT_TYPE_AI_SUMMARY = "AI_SUMMARY"
CONTENT_TYPE_AI_QUESTION = "AI_QUESTION"
CONTENT_TYPE_AI_CTA = "AI_CTA"
CONTENT_TYPE_CUSTOM_AI = "CUSTOM_AI"
CONTENT_TYPES = frozenset(
    {
        CONTENT_TYPE_FIXED_TEXT,
        CONTENT_TYPE_AI_HOOK,
        CONTENT_TYPE_AI_TITLE,
        CONTENT_TYPE_AI_SUMMARY,
        CONTENT_TYPE_AI_QUESTION,
        CONTENT_TYPE_AI_CTA,
        CONTENT_TYPE_CUSTOM_AI,
    }
)

# content_type -> qual campo de texto é exigido (nenhum dos dois
# conjuntos se sobrepõe; qualquer content_type fora de ambos não aceita
# nenhum campo de texto -- ver seção 9.1).
_CONTENT_TEXT_REQUIRED_TYPES = frozenset({CONTENT_TYPE_FIXED_TEXT})
_CONTENT_INSTRUCTION_REQUIRED_TYPES = frozenset({CONTENT_TYPE_CUSTOM_AI})

CONTENT_ALIGNMENTS = ("LEFT", "CENTER", "RIGHT")
_CONTENT_DEFAULT_ALIGNMENT = "CENTER"

MAX_LINES_RANGE = (1, 10)
MAX_CHARS_RANGE = (1, 500)
CONTENT_FONT_SIZE_RANGE = (8.0, 200.0)
_CONTENT_TEXT_FIELD_MAX_LEN = 2000

_AUTOFIT_REFERENCE_FONT_SIZE = 32.0
_AUTOFIT_REFERENCE_CHARS_PER_LINE = 18
_AUTOFIT_FONT_STEP = 1.0
_AUTOFIT_TRUNCATION_MARKER = "…"

# Diretório-fonte dos 2 PNGs oficiais fornecidos pelo dono do produto,
# ao lado de ``_sistema``/``tests`` na raiz do repositório -- ver seção 4.
DEFAULT_BUILTIN_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "templates_oficiais"


# ---------------------------------------------------------------------------
# Erros
# ---------------------------------------------------------------------------


class TemplateEngineError(RuntimeError):
    """Base de todos os erros estruturados deste módulo."""


class CampoInvalidoError(TemplateEngineError):
    """Um campo obrigatório está ausente, com tipo errado, ou vazio."""


class VocabularioInvalidoError(TemplateEngineError):
    """Valor fora do vocabulário fechado (``template_type``/``zone_type``)."""


class GeometriaInvalidaError(TemplateEngineError):
    """Geometria normalizada de zona fora de ``[0, 1]`` ou com overflow de borda."""


class LayoutInvalidoError(TemplateEngineError):
    """``layout`` com estrutura/cardinalidade de zonas inválida."""


class TemplateNaoEncontradoError(TemplateEngineError):
    """``template_id`` não corresponde a nenhuma linha existente."""


class ArquivoOficialAusenteError(TemplateEngineError):
    """Um arquivo oficial esperado em ``assets/templates_oficiais/`` não foi
    encontrado -- nunca substituído por uma arte genérica (vedado pelo
    roadmap: "Não criar artes genéricas substitutas se os arquivos
    oficiais forem fornecidos")."""


class ZonaNaoEncontradaError(TemplateEngineError):
    """``zone_id`` não corresponde a nenhuma zona do ``layout`` do
    template (distinto de ``TemplateNaoEncontradoError``, que é sobre o
    TEMPLATE, não sobre uma zona dentro dele). Ver seção 8.5."""


class ZonaProtegidaError(TemplateEngineError):
    """Uma operação de zona foi rejeitada por alvejar a zona
    ``BACKGROUND`` (geometria travada ao frame inteiro, sempre primeira
    da lista) ou por violar a cardinalidade mínima obrigatória
    (``BACKGROUND``/``VIDEO`` nunca podem ser removidas). Ver seção
    8.4 -- decisão deliberada de rejeitar explicitamente em vez de um
    no-op silencioso."""


# ---------------------------------------------------------------------------
# Validadores de geometria/estilo (estilo de ``visual_editor.py``, nunca
# importado -- reimplementado para o domínio de zonas de Template).
# ---------------------------------------------------------------------------


def _as_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CampoInvalidoError(f"{field_name} deve ser numérico (recebido: {value!r})")
    result = float(value)
    if result != result or result in (float("inf"), float("-inf")):
        raise CampoInvalidoError(f"{field_name} deve ser um número finito (recebido: {value!r})")
    return result


def _validate_range(value: Any, field_name: str, bounds: tuple[float, float]) -> float:
    result = _as_float(value, field_name)
    low, high = bounds
    if not (low - _GEOMETRY_EPSILON <= result <= high + _GEOMETRY_EPSILON):
        raise GeometriaInvalidaError(
            f"{field_name} deve estar entre {low} e {high} (recebido: {result!r})"
        )
    return result


def _validate_color(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise CampoInvalidoError(f"{field_name} deve ser string hex (recebido: {value!r})")
    if not (_COLOR_RE_6.match(value) or _COLOR_RE_8.match(value)):
        raise CampoInvalidoError(
            f"{field_name} deve ser #RRGGBB ou #RRGGBBAA (recebido: {value!r})"
        )
    return value.upper()


def _validate_style(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CampoInvalidoError(f"style deve ser um mapeamento (recebido: {type(value)!r})")
    unknown = set(value.keys()) - _STYLE_FIELDS
    if unknown:
        raise CampoInvalidoError(f"style contém campo(s) desconhecido(s): {sorted(unknown)!r}")
    result: dict[str, Any] = {}
    if "opacity" in value:
        result["opacity"] = _validate_range(value["opacity"], "style.opacity", (0.0, 1.0))
    if "background_color" in value:
        result["background_color"] = _validate_color(value["background_color"], "style.background_color")
    return result


# ---------------------------------------------------------------------------
# DynamicContent (Prompt 40) -- validadores reimplementados a partir da
# MESMA disciplina de ``captions_style.py`` (``_as_int``/``_as_str``/
# ``_validate_int_range``), nunca importados através de módulos irmãos
# -- ver seção 9 da docstring do módulo.
# ---------------------------------------------------------------------------


def _as_content_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CampoInvalidoError(f"{field_name} deve ser um inteiro (recebido: {value!r})")
    return value


def _validate_content_int_range(value: Any, field_name: str, bounds: tuple[int, int]) -> int:
    validated = _as_content_int(value, field_name)
    low, high = bounds
    if not (low <= validated <= high):
        raise CampoInvalidoError(f"{field_name} deve estar entre {low} e {high} (recebido: {validated!r})")
    return validated


def _validate_content_float_range(value: Any, field_name: str, bounds: tuple[float, float]) -> float:
    validated = _as_float(value, field_name)
    low, high = bounds
    if not (low <= validated <= high):
        raise CampoInvalidoError(f"{field_name} deve estar entre {low} e {high} (recebido: {validated!r})")
    return validated


def _validate_content_text_field(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CampoInvalidoError(f"{field_name} deve ser uma string não vazia (recebido: {value!r})")
    if len(value) > _CONTENT_TEXT_FIELD_MAX_LEN:
        raise CampoInvalidoError(
            f"{field_name} excede o comprimento máximo de {_CONTENT_TEXT_FIELD_MAX_LEN} caracteres"
        )
    return value


def _validate_content_type(value: Any) -> str:
    if not isinstance(value, str) or value not in CONTENT_TYPES:
        raise VocabularioInvalidoError(
            f"content_type deve ser um de {sorted(CONTENT_TYPES)!r} (recebido: {value!r})"
        )
    return value


def _validate_content_alignment(value: Any) -> str:
    if value is None:
        return _CONTENT_DEFAULT_ALIGNMENT
    if not isinstance(value, str) or value not in CONTENT_ALIGNMENTS:
        raise VocabularioInvalidoError(
            f"content.alignment deve ser um de {CONTENT_ALIGNMENTS!r} (recebido: {value!r})"
        )
    return value


def _validate_dynamic_content(value: Any) -> dict[str, Any]:
    """Valida e normaliza o sub-schema ``content`` (DynamicContent) de
    UMA zona -- ver seção 9 da docstring do módulo. Só chamado quando
    ``value`` não é ``None`` -- ausência de ``content`` é sempre válida
    (retrocompatibilidade, seção 9.4)."""
    if not isinstance(value, Mapping):
        raise CampoInvalidoError(f"content deve ser um mapeamento (recebido: {type(value)!r})")

    content_type = _validate_content_type(value.get("content_type"))
    has_text = "text" in value
    has_instruction = "instruction" in value

    if content_type in _CONTENT_TEXT_REQUIRED_TYPES:
        if not has_text:
            raise CampoInvalidoError(f"content.text é obrigatório para content_type={content_type!r}")
        if has_instruction:
            raise CampoInvalidoError(
                f"content.instruction não é permitido para content_type={content_type!r}"
            )
    elif content_type in _CONTENT_INSTRUCTION_REQUIRED_TYPES:
        if not has_instruction:
            raise CampoInvalidoError(f"content.instruction é obrigatório para content_type={content_type!r}")
        if has_text:
            raise CampoInvalidoError(f"content.text não é permitido para content_type={content_type!r}")
    else:
        if has_text:
            raise CampoInvalidoError(
                f"content.text não é permitido para content_type={content_type!r} -- "
                "este tipo ainda não tem texto até o futuro ContentEngine (fora de escopo)"
            )
        if has_instruction:
            raise CampoInvalidoError(
                f"content.instruction não é permitido para content_type={content_type!r}"
            )

    allowed_keys = {"content_type", "max_lines", "max_chars", "font_size", "font_size_min", "alignment"}
    if content_type in _CONTENT_TEXT_REQUIRED_TYPES:
        allowed_keys = allowed_keys | {"text"}
    if content_type in _CONTENT_INSTRUCTION_REQUIRED_TYPES:
        allowed_keys = allowed_keys | {"instruction"}
    unknown = set(value.keys()) - allowed_keys
    if unknown:
        raise CampoInvalidoError(f"content contém campo(s) desconhecido(s): {sorted(unknown)!r}")

    max_lines = _validate_content_int_range(value.get("max_lines"), "content.max_lines", MAX_LINES_RANGE)
    max_chars = _validate_content_int_range(value.get("max_chars"), "content.max_chars", MAX_CHARS_RANGE)
    font_size = _validate_content_float_range(value.get("font_size"), "content.font_size", CONTENT_FONT_SIZE_RANGE)
    font_size_min = _validate_content_float_range(
        value.get("font_size_min"), "content.font_size_min", CONTENT_FONT_SIZE_RANGE
    )
    if font_size_min > font_size:
        raise CampoInvalidoError(
            f"content.font_size_min ({font_size_min!r}) não pode ser maior que "
            f"content.font_size ({font_size!r})"
        )
    alignment = _validate_content_alignment(value.get("alignment"))

    result: dict[str, Any] = {
        "content_type": content_type,
        "max_lines": max_lines,
        "max_chars": max_chars,
        "font_size": font_size,
        "font_size_min": font_size_min,
        "alignment": alignment,
    }
    if content_type in _CONTENT_TEXT_REQUIRED_TYPES:
        result["text"] = _validate_content_text_field(value.get("text"), "content.text")
    if content_type in _CONTENT_INSTRUCTION_REQUIRED_TYPES:
        result["instruction"] = _validate_content_text_field(value.get("instruction"), "content.instruction")
    return result


# ---------------------------------------------------------------------------
# Auto-fit determinístico (Prompt 40) -- função PURA, nunca lê/escreve
# estado, não é chamada automaticamente pela validação -- ver seção 9.3
# da docstring do módulo para a heurística e a garantia provada por teste.
# ---------------------------------------------------------------------------


def _autofit_capacity_chars_per_line(font_size: float) -> int:
    """HEURÍSTICA determinística -- NUNCA medição real de pixels/glifos
    (sem Pillow, sem render engine). Capacidade de caracteres por linha
    inversamente proporcional a ``font_size``, calibrada por uma
    referência arbitrária documentada na seção 9.3."""
    if not isinstance(font_size, (int, float)) or isinstance(font_size, bool) or font_size <= 0:
        raise CampoInvalidoError(f"font_size deve ser numérico e > 0 (recebido: {font_size!r})")
    capacity = round(_AUTOFIT_REFERENCE_CHARS_PER_LINE * (_AUTOFIT_REFERENCE_FONT_SIZE / font_size))
    return max(1, capacity)


def _autofit_required_lines(text: str, font_size: float) -> int:
    if not text:
        return 1
    capacity = _autofit_capacity_chars_per_line(font_size)
    return max(1, math.ceil(len(text) / capacity))


def _autofit_truncate(text: str, limit: int) -> tuple[str, bool]:
    """Trunca deterministicamente para no máximo ``limit`` caracteres,
    reservando o último caractere para o marcador de truncamento quando
    necessário -- resultado SEMPRE com ``len(resultado) <= limit``."""
    if limit <= 0:
        return "", bool(text)
    if len(text) <= limit:
        return text, False
    if limit == 1:
        return _AUTOFIT_TRUNCATION_MARKER, True
    return text[: limit - 1] + _AUTOFIT_TRUNCATION_MARKER, True


@dataclass(slots=True, frozen=True)
class AutoFitResult:
    """Resultado puro de ``apply_auto_fit`` -- ver seção 9.3 da
    docstring do módulo. ``lines``/``len(text)`` NUNCA excedem os
    ``max_lines``/``max_chars`` fornecidos (garantia provada por
    teste)."""

    text: str
    font_size: float
    lines: int
    truncated: bool
    reduced: bool


def apply_auto_fit(
    text: str,
    *,
    max_lines: int,
    max_chars: int,
    font_size: float,
    font_size_min: float,
) -> AutoFitResult:
    """Função PURA e determinística (nenhum I/O, nenhum estado) -- ver
    seção 9.3 da docstring do módulo para o algoritmo de 3 passos e a
    garantia provada por teste: o resultado nunca excede
    ``max_lines``/``max_chars``, para qualquer texto de entrada."""
    if not isinstance(text, str):
        raise CampoInvalidoError(f"text deve ser string (recebido: {type(text)!r})")
    _validate_content_int_range(max_lines, "max_lines", MAX_LINES_RANGE)
    _validate_content_int_range(max_chars, "max_chars", MAX_CHARS_RANGE)
    _validate_content_float_range(font_size, "font_size", CONTENT_FONT_SIZE_RANGE)
    _validate_content_float_range(font_size_min, "font_size_min", CONTENT_FONT_SIZE_RANGE)
    if font_size_min > font_size:
        raise CampoInvalidoError(
            f"font_size_min ({font_size_min!r}) não pode ser maior que font_size ({font_size!r})"
        )

    # Passo 1: corte duro por max_chars -- sempre primeiro, independente
    # de font_size (seção 9.3).
    working_text, truncated_by_chars = _autofit_truncate(text, max_chars)

    # Passo 2: reduz font_size em passos controlados até caber em
    # max_lines, ou até font_size_min.
    current_size = font_size
    lines = _autofit_required_lines(working_text, current_size)
    while lines > max_lines and current_size > font_size_min:
        current_size = max(font_size_min, current_size - _AUTOFIT_FONT_STEP)
        lines = _autofit_required_lines(working_text, current_size)

    # Passo 3: se mesmo em font_size_min ainda não coube, corta mais --
    # garantia matemática (ceil(a/b) <= m sse a <= b*m) de que o
    # resultado final nunca excede max_lines.
    truncated_by_lines = False
    if lines > max_lines:
        capacity = _autofit_capacity_chars_per_line(current_size)
        hard_limit = capacity * max_lines
        working_text, truncated_by_lines = _autofit_truncate(working_text, hard_limit)
        lines = _autofit_required_lines(working_text, current_size)

    return AutoFitResult(
        text=working_text,
        font_size=current_size,
        lines=lines,
        truncated=truncated_by_chars or truncated_by_lines,
        reduced=current_size < font_size,
    )


def _validate_zone_type(value: Any) -> str:
    if not isinstance(value, str) or value not in ZONE_TYPES:
        raise VocabularioInvalidoError(
            f"zone_type deve ser um de {sorted(ZONE_TYPES)!r} (recebido: {value!r})"
        )
    return value


def _validate_zone_id(value: Any) -> str:
    """Gera um ``zone_id`` novo (``new_uuid()``) quando ausente, ou
    valida e PRESERVA um valor já existente -- nunca regenera um id já
    presente (identidade estável através de revalidações/escritas
    sucessivas do mesmo layout). Disciplina de validação reimplementada
    localmente a partir de ``domain.models._validate_uuid`` (privado,
    nunca importado através de módulos -- mesmo padrão de isolamento já
    usado em todo este arquivo), nunca o código em si. Ver seção 8.1."""
    if value is None:
        return new_uuid()
    if not isinstance(value, str) or not value:
        raise CampoInvalidoError(f"zone_id deve ser um UUID textual não vazio (recebido: {value!r})")
    try:
        UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise CampoInvalidoError(f"zone_id deve ser um UUID válido (recebido: {value!r})") from exc
    return value


def _validate_geometry(zone: Mapping[str, Any], *, zone_type: str) -> tuple[float, float, float, float]:
    x = _validate_range(zone.get("x"), "x", (0.0, 1.0))
    y = _validate_range(zone.get("y"), "y", (0.0, 1.0))
    width = _as_float(zone.get("width"), "width")
    height = _as_float(zone.get("height"), "height")
    if width <= 0:
        raise GeometriaInvalidaError(f"width deve ser > 0 (recebido: {width!r})")
    if height <= 0:
        raise GeometriaInvalidaError(f"height deve ser > 0 (recebido: {height!r})")
    if x + width > 1.0 + _GEOMETRY_EPSILON:
        raise GeometriaInvalidaError(
            f"zona {zone_type!r} ultrapassa a borda direita: x({x}) + width({width}) > 1.0"
        )
    if y + height > 1.0 + _GEOMETRY_EPSILON:
        raise GeometriaInvalidaError(
            f"zona {zone_type!r} ultrapassa a borda inferior: y({y}) + height({height}) > 1.0"
        )
    return x, y, width, height


def _validate_zone(zone: Any, *, index: int) -> dict[str, Any]:
    if not isinstance(zone, Mapping):
        raise CampoInvalidoError(f"zona[{index}] deve ser um mapeamento (recebido: {type(zone)!r})")
    zone_type = _validate_zone_type(zone.get("zone_type"))
    if zone_type == ZONE_TYPE_BACKGROUND:
        # BACKGROUND é imposto a cobrir o frame inteiro -- ver seção 3
        # da docstring do módulo para a justificativa da decisão.
        for key, expected in (("x", 0.0), ("y", 0.0), ("width", 1.0), ("height", 1.0)):
            got = zone.get(key, expected)
            got_float = _as_float(got, key)
            if abs(got_float - expected) > _GEOMETRY_EPSILON:
                raise GeometriaInvalidaError(
                    f"zona BACKGROUND deve cobrir o frame inteiro (x=0,y=0,width=1,height=1) -- "
                    f"{key} recebido: {got!r}"
                )
        x, y, width, height = 0.0, 0.0, 1.0, 1.0
    else:
        x, y, width, height = _validate_geometry(zone, zone_type=zone_type)
    style = _validate_style(zone.get("style"))
    zone_id = _validate_zone_id(zone.get("zone_id"))
    result = {
        "zone_id": zone_id,
        "zone_type": zone_type,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "style": style,
    }
    if "content" in zone and zone.get("content") is not None:
        if zone_type not in _CONTENT_ELIGIBLE_ZONE_TYPES:
            raise CampoInvalidoError(
                f"content só é permitido em zonas {sorted(_CONTENT_ELIGIBLE_ZONE_TYPES)!r} "
                f"(recebido em uma zona {zone_type!r})"
            )
        result["content"] = _validate_dynamic_content(zone["content"])
    return result


def _validate_zones(zones: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(zones, Sequence) or isinstance(zones, (str, bytes)):
        raise LayoutInvalidoError(f"zones deve ser uma lista (recebido: {type(zones)!r})")
    if len(zones) == 0:
        raise LayoutInvalidoError("zones não pode ser vazia -- todo Template exige BACKGROUND + VIDEO")
    validated = tuple(_validate_zone(zone, index=idx) for idx, zone in enumerate(zones))

    zone_ids = [zone["zone_id"] for zone in validated]
    if len(zone_ids) != len(set(zone_ids)):
        raise LayoutInvalidoError(f"zone_id duplicado entre zonas do mesmo layout: {zone_ids!r}")

    counts: dict[str, int] = {}
    for zone in validated:
        counts[zone["zone_type"]] = counts.get(zone["zone_type"], 0) + 1

    if counts.get(ZONE_TYPE_BACKGROUND, 0) != 1:
        raise LayoutInvalidoError(
            f"exatamente 1 zona BACKGROUND é obrigatória (encontradas: {counts.get(ZONE_TYPE_BACKGROUND, 0)})"
        )
    if validated[0]["zone_type"] != ZONE_TYPE_BACKGROUND:
        raise LayoutInvalidoError(
            "a zona BACKGROUND deve ser a primeira da lista (camada mais baixa -- "
            "z_index é implícito pela posição, ver seção 3 da docstring do módulo)"
        )
    if counts.get(ZONE_TYPE_VIDEO, 0) != 1:
        raise LayoutInvalidoError(
            f"exatamente 1 zona VIDEO é obrigatória (encontradas: {counts.get(ZONE_TYPE_VIDEO, 0)})"
        )
    return validated


def validate_layout(layout: Any) -> dict[str, Any]:
    """Valida e normaliza um ``layout`` completo -- único ponto de entrada
    de validação de layout, usado tanto por ``create_template`` quanto por
    ``update_template`` e pela ingestão dos built-ins."""
    if not isinstance(layout, Mapping):
        raise LayoutInvalidoError(f"layout deve ser um mapeamento (recebido: {type(layout)!r})")
    unknown = set(layout.keys()) - {"zones"}
    if unknown:
        raise LayoutInvalidoError(f"layout contém campo(s) de topo desconhecido(s): {sorted(unknown)!r}")
    if "zones" not in layout:
        raise LayoutInvalidoError("layout deve conter a chave 'zones'")
    zones = _validate_zones(layout["zones"])
    return {"zones": [dict(zone) for zone in zones]}


def _validate_template_type(value: Any) -> str:
    if not isinstance(value, str) or value not in TEMPLATE_TYPES:
        raise VocabularioInvalidoError(
            f"template_type deve ser um de {sorted(TEMPLATE_TYPES)!r} (recebido: {value!r})"
        )
    return value


def _validate_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CampoInvalidoError(f"name deve ser uma string não vazia (recebido: {value!r})")
    return value.strip()


def _validate_enabled(value: Any) -> bool:
    if not isinstance(value, bool):
        raise CampoInvalidoError(f"enabled deve ser bool (recebido: {value!r})")
    return value


def _validate_source_path(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CampoInvalidoError(f"source_path deve ser string não vazia ou None (recebido: {value!r})")
    return value


# ---------------------------------------------------------------------------
# Leitura de dimensões de imagem (ffprobe, isolado -- nunca via MediaProbe,
# que rejeita imagem estática -- ver seção 5 da docstring do módulo).
# ---------------------------------------------------------------------------


def _read_image_dimensions(path: "Path | str") -> tuple[int, int]:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=_FFPROBE_TIMEOUT_SECONDS, text=True
        )
    except subprocess.TimeoutExpired:
        raise TemplateEngineError("timeout ao executar ffprobe para ler dimensões da imagem") from None
    except OSError as exc:
        raise TemplateEngineError(f"falha ao iniciar ffprobe: {exc}") from exc
    if result.returncode != 0:
        raise TemplateEngineError(f"ffprobe terminou com código {result.returncode} lendo {path!r}")
    try:
        payload = json.loads(result.stdout)
        stream = payload["streams"][0]
        return int(stream["width"]), int(stream["height"])
    except (KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
        raise TemplateEngineError(f"saída inesperada do ffprobe ao ler {path!r}: {exc}") from exc


# ---------------------------------------------------------------------------
# Especificações dos templates oficiais built-in -- ver seção 4 da
# docstring do módulo. Uma TUPLA, nunca um número fixo hardcoded em
# lugar algum da lógica de ingestão -- adicionar um novo built-in no
# futuro é só adicionar uma entrada aqui.
# ---------------------------------------------------------------------------

BUILTIN_TEMPLATE_SPECS: tuple[dict[str, Any], ...] = (
    {
        "slug": "moldura_tech_azul",
        "source_filename": "template_moldura_tech_azul.png",
        "name": "Moldura Tech Azul",
        "video_zone": {"x": 0.04, "y": 0.05, "width": 0.92, "height": 0.90},
    },
    {
        "slug": "moldura_branca_classica",
        "source_filename": "template_moldura_branca_classica.png",
        "name": "Moldura Branca Clássica",
        "video_zone": {"x": 0.03, "y": 0.02, "width": 0.94, "height": 0.96},
    },
)


def _initial_layout_for_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    return validate_layout(
        {
            "zones": [
                {"zone_type": ZONE_TYPE_BACKGROUND, "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
                {"zone_type": ZONE_TYPE_VIDEO, **spec["video_zone"]},
            ]
        }
    )


@dataclass(slots=True)
class PreviewInfo:
    """Metadata de preview -- ver seção 5 (decisão metadata-only, sem
    arquivo de thumbnail físico nesta etapa)."""

    source_path: str | None
    width: int | None
    height: int | None


# ---------------------------------------------------------------------------
# TemplateEngine
# ---------------------------------------------------------------------------


class TemplateEngine:
    """CRUD validado sobre ``Template`` + ingestão idempotente dos
    templates oficiais built-in -- ver docstring do módulo para o
    contrato completo."""

    def __init__(
        self,
        database: LocalDatabase,
        *,
        storage_manager: StorageManager,
        app_paths: AppPaths,
    ) -> None:
        if not isinstance(database, LocalDatabase):
            raise TypeError(f"database deve ser LocalDatabase (recebido: {type(database)!r})")
        if not isinstance(storage_manager, StorageManager):
            raise TypeError(
                f"storage_manager deve ser StorageManager (recebido: {type(storage_manager)!r})"
            )
        if not isinstance(app_paths, AppPaths):
            raise TypeError(f"app_paths deve ser AppPaths (recebido: {type(app_paths)!r})")
        self._database = database
        self._storage = storage_manager
        self._app_paths = app_paths

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_template(
        self,
        *,
        name: str,
        layout: Mapping[str, Any],
        template_type: str = TEMPLATE_TYPE_CUSTOM,
        source_path: str | None = None,
        enabled: bool = True,
        extra: Mapping[str, Any] | None = None,
    ) -> Template:
        validated_name = _validate_name(name)
        validated_type = _validate_template_type(template_type)
        validated_layout = validate_layout(layout)
        validated_source_path = _validate_source_path(source_path)
        validated_enabled = _validate_enabled(enabled)
        extra_dict = dict(extra) if extra else {}

        entity = Template(
            name=validated_name,
            template_type=validated_type,
            source_path=validated_source_path,
            layout=validated_layout,
            enabled=validated_enabled,
            extra=extra_dict,
        )
        return self._database.insert(entity)

    def get_template(self, template_id: str) -> Template:
        entity = self._database.get(Template, template_id)
        if entity is None:
            raise TemplateNaoEncontradoError(f"template não encontrado: {template_id!r}")
        return entity

    def _read_modify_write(self, template_id: str, mutator: Callable[[Template], None]) -> Template:
        """Único ponto de leitura-decisão-escrita atômica contra a
        entidade ``Template`` -- reaproveitado por ``update_template`` e
        pelas 5 operações de zona (seção 8.3 da docstring do módulo:
        correção do TOCTOU pré-existente em ``update_template``, nunca
        duplicado em paralelo). ``mutator`` recebe a entidade já lida
        (dentro da transação) e a modifica in-place; ``entity.touch()``
        e ``save`` acontecem sempre dentro da MESMA ``BEGIN IMMEDIATE``."""
        with self._database.transaction() as conn:
            entity = self._database.get(Template, template_id, connection=conn)
            if entity is None:
                raise TemplateNaoEncontradoError(f"template não encontrado: {template_id!r}")
            mutator(entity)
            entity.touch()
            return self._database.save(entity, connection=conn)

    def update_template(self, template_id: str, **fields: Any) -> Template:
        allowed = {"name", "layout", "template_type", "source_path", "enabled", "extra"}
        unknown = set(fields.keys()) - allowed
        if unknown:
            raise CampoInvalidoError(f"campo(s) não suportado(s) em update_template: {sorted(unknown)!r}")

        def mutator(entity: Template) -> None:
            if "name" in fields:
                entity.name = _validate_name(fields["name"])
            if "layout" in fields:
                entity.layout = validate_layout(fields["layout"])
            if "template_type" in fields:
                entity.template_type = _validate_template_type(fields["template_type"])
            if "source_path" in fields:
                entity.source_path = _validate_source_path(fields["source_path"])
            if "enabled" in fields:
                entity.enabled = _validate_enabled(fields["enabled"])
            if "extra" in fields:
                value = fields["extra"]
                if not isinstance(value, Mapping):
                    raise CampoInvalidoError(f"extra deve ser um mapeamento (recebido: {type(value)!r})")
                entity.extra = dict(value)

        return self._read_modify_write(template_id, mutator)

    # ------------------------------------------------------------------
    # Prompt 39 -- operações atômicas individuais de zona (seção 8 da
    # docstring do módulo). Cada operação reconstrói a lista de zonas
    # candidata e chama ``validate_layout`` -- toda a validação de
    # geometria/vocabulário/cardinalidade é 100% reaproveitada, nunca
    # reimplementada aqui.
    # ------------------------------------------------------------------

    @staticmethod
    def _find_zone_index(zones: list[dict[str, Any]], zone_id: str) -> int:
        for idx, zone in enumerate(zones):
            if zone.get("zone_id") == zone_id:
                return idx
        raise ZonaNaoEncontradaError(f"zona não encontrada: {zone_id!r}")

    def add_zone(self, template_id: str, zone: Mapping[str, Any], *, index: int | None = None) -> Template:
        if not isinstance(zone, Mapping):
            raise CampoInvalidoError(f"zone deve ser um mapeamento (recebido: {type(zone)!r})")
        if zone.get("zone_type") == ZONE_TYPE_BACKGROUND:
            raise ZonaProtegidaError(
                "não é possível adicionar uma segunda zona BACKGROUND -- cardinalidade "
                "exatamente 1, imposta desde a criação do template"
            )

        def mutator(entity: Template) -> None:
            zones = [dict(z) for z in entity.layout.get("zones", [])]
            insert_at = len(zones) if index is None else index
            insert_at = max(1, min(insert_at, len(zones)))
            new_zone = dict(zone)
            new_zone.pop("zone_id", None)  # sempre gera um zone_id novo para a zona adicionada
            zones = zones[:insert_at] + [new_zone] + zones[insert_at:]
            entity.layout = validate_layout({"zones": zones})

        return self._read_modify_write(template_id, mutator)

    def move_zone(self, template_id: str, zone_id: str, *, x: float, y: float) -> Template:
        def mutator(entity: Template) -> None:
            zones = [dict(z) for z in entity.layout.get("zones", [])]
            idx = self._find_zone_index(zones, zone_id)
            if zones[idx]["zone_type"] == ZONE_TYPE_BACKGROUND:
                raise ZonaProtegidaError(
                    "a zona BACKGROUND tem geometria travada ao frame inteiro e não pode ser movida"
                )
            zones[idx] = {**zones[idx], "x": x, "y": y}
            entity.layout = validate_layout({"zones": zones})

        return self._read_modify_write(template_id, mutator)

    def resize_zone(self, template_id: str, zone_id: str, *, width: float, height: float) -> Template:
        def mutator(entity: Template) -> None:
            zones = [dict(z) for z in entity.layout.get("zones", [])]
            idx = self._find_zone_index(zones, zone_id)
            if zones[idx]["zone_type"] == ZONE_TYPE_BACKGROUND:
                raise ZonaProtegidaError(
                    "a zona BACKGROUND tem geometria travada ao frame inteiro e não pode ser redimensionada"
                )
            zones[idx] = {**zones[idx], "width": width, "height": height}
            entity.layout = validate_layout({"zones": zones})

        return self._read_modify_write(template_id, mutator)

    def remove_zone(self, template_id: str, zone_id: str) -> Template:
        def mutator(entity: Template) -> None:
            zones = [dict(z) for z in entity.layout.get("zones", [])]
            idx = self._find_zone_index(zones, zone_id)
            zone_type = zones[idx]["zone_type"]
            if zone_type in (ZONE_TYPE_BACKGROUND, ZONE_TYPE_VIDEO):
                raise ZonaProtegidaError(
                    f"não é possível remover a única zona {zone_type} -- cardinalidade mínima obrigatória"
                )
            del zones[idx]
            entity.layout = validate_layout({"zones": zones})

        return self._read_modify_write(template_id, mutator)

    def reorder_zone(self, template_id: str, zone_id: str, *, new_index: int) -> Template:
        def mutator(entity: Template) -> None:
            zones = [dict(z) for z in entity.layout.get("zones", [])]
            idx = self._find_zone_index(zones, zone_id)
            if zones[idx]["zone_type"] == ZONE_TYPE_BACKGROUND:
                raise ZonaProtegidaError(
                    "a zona BACKGROUND deve permanecer sempre a primeira -- não pode ser reordenada"
                )
            if new_index == 0:
                raise ZonaProtegidaError(
                    "índice 0 é reservado à zona BACKGROUND -- não é possível mover outra zona para lá"
                )
            item = zones.pop(idx)
            insert_at = max(1, min(new_index, len(zones)))
            zones.insert(insert_at, item)
            entity.layout = validate_layout({"zones": zones})

        return self._read_modify_write(template_id, mutator)

    def set_zone_content(
        self, template_id: str, zone_id: str, content: Mapping[str, Any] | None
    ) -> Template:
        """Atribui/atualiza o DynamicContent (``content``) de uma zona
        ``CAPTION``/``AI_TEXT`` -- ver seção 9.4 da docstring do módulo.
        ``content=None`` remove o ``content`` existente da zona."""

        def mutator(entity: Template) -> None:
            zones = [dict(z) for z in entity.layout.get("zones", [])]
            idx = self._find_zone_index(zones, zone_id)
            zone_type = zones[idx]["zone_type"]
            if zone_type not in _CONTENT_ELIGIBLE_ZONE_TYPES:
                raise ZonaProtegidaError(
                    f"zona {zone_type!r} não aceita DynamicContent -- apenas "
                    f"{sorted(_CONTENT_ELIGIBLE_ZONE_TYPES)!r}"
                )
            updated = dict(zones[idx])
            if content is None:
                updated.pop("content", None)
            else:
                updated["content"] = dict(content)
            zones[idx] = updated
            entity.layout = validate_layout({"zones": zones})

        return self._read_modify_write(template_id, mutator)

    def list_templates(
        self, *, template_type: str | None = None, enabled_only: bool = False
    ) -> list[Template]:
        if template_type is not None:
            _validate_template_type(template_type)
        entities = self._database.list(Template)
        if template_type is not None:
            entities = [entity for entity in entities if entity.template_type == template_type]
        if enabled_only:
            entities = [entity for entity in entities if entity.enabled]
        return entities

    def get_preview_info(self, template_id: str) -> PreviewInfo:
        entity = self.get_template(template_id)
        width = entity.extra.get("width") if isinstance(entity.extra, Mapping) else None
        height = entity.extra.get("height") if isinstance(entity.extra, Mapping) else None
        return PreviewInfo(
            source_path=entity.source_path,
            width=int(width) if isinstance(width, (int, float)) else None,
            height=int(height) if isinstance(height, (int, float)) else None,
        )

    # ------------------------------------------------------------------
    # Ingestão dos templates oficiais built-in -- ver seção 4 da docstring
    # ------------------------------------------------------------------

    def _find_builtin_by_slug(self, slug: str, *, connection: Any) -> Template | None:
        row = connection.execute(
            "SELECT id FROM templates WHERE template_type = ? "
            "AND json_extract(extra_json, '$.builtin_slug') = ? "
            "ORDER BY created_at LIMIT 1",
            (TEMPLATE_TYPE_BUILT_IN, slug),
        ).fetchone()
        if row is None:
            return None
        return self._database.get(Template, row[0], connection=connection)

    def _managed_path_for_slug(self, slug: str) -> Path:
        return Path(self._app_paths.templates) / f"builtin_{slug}.png"

    def _copy_into_managed_storage(self, source_path: Path, slug: str) -> Path:
        final_path = self._managed_path_for_slug(slug)
        temp_path = self._storage.allocate_temp(suffix=".png", create=True)
        temp_path.write_bytes(source_path.read_bytes())
        promoted = self._storage.promote_to_final(temp_path, final_path, overwrite=True)
        return Path(promoted)

    def ingest_builtin_templates(
        self, *, assets_dir: "Path | str | None" = None
    ) -> list[Template]:
        """Ingestão idempotente dos templates oficiais built-in. Copia os
        arquivos originais (NUNCA sobrescritos/alterados -- só lidos) de
        ``assets_dir`` (default: ``DEFAULT_BUILTIN_ASSETS_DIR``) para o
        armazenamento gerenciado (``AppPaths.templates`` via
        ``StorageManager``), criando/atualizando as linhas ``Template``
        correspondentes. Repetir esta chamada é seguro -- nenhuma linha
        duplicada, nenhum layout já ajustado é sobrescrito."""
        source_dir = Path(assets_dir) if assets_dir is not None else DEFAULT_BUILTIN_ASSETS_DIR

        results: list[Template] = []
        for spec in BUILTIN_TEMPLATE_SPECS:
            slug = spec["slug"]
            source_path = source_dir / spec["source_filename"]
            if not source_path.is_file():
                raise ArquivoOficialAusenteError(
                    f"arquivo oficial ausente para o template built-in {slug!r}: {source_path!s} -- "
                    f"não substituído por arte genérica, conforme exigido pelo roadmap."
                )

            with self._database.transaction() as conn:
                existing = self._find_builtin_by_slug(slug, connection=conn)
                if existing is not None:
                    results.append(existing)
                    continue

                width, height = _read_image_dimensions(source_path)
                managed_path = self._copy_into_managed_storage(source_path, slug)
                entity = Template(
                    name=spec["name"],
                    template_type=TEMPLATE_TYPE_BUILT_IN,
                    source_path=str(managed_path),
                    layout=_initial_layout_for_spec(spec),
                    enabled=True,
                    extra={"builtin_slug": slug, "width": width, "height": height},
                )
                inserted = self._database.insert(entity, connection=conn)
                results.append(inserted)

        # Auto-cura: se uma linha BUILT_IN já existente perdeu seu arquivo
        # gerenciado (apagado por fora do produto), recopia do original --
        # fora da transação acima porque é puramente uma operação de
        # arquivo, não de linha do banco (evita segurar o BEGIN IMMEDIATE
        # mais tempo do que o necessário para a decisão atômica da linha).
        for spec, entity in zip(BUILTIN_TEMPLATE_SPECS, results):
            managed_path = self._managed_path_for_slug(spec["slug"])
            if not managed_path.is_file():
                source_path = source_dir / spec["source_filename"]
                self._copy_into_managed_storage(source_path, spec["slug"])

        return results


__all__ = [
    "TemplateEngine",
    "TemplateEngineError",
    "CampoInvalidoError",
    "VocabularioInvalidoError",
    "GeometriaInvalidaError",
    "LayoutInvalidoError",
    "TemplateNaoEncontradoError",
    "ArquivoOficialAusenteError",
    "ZonaNaoEncontradaError",
    "ZonaProtegidaError",
    "TEMPLATE_TYPE_BUILT_IN",
    "TEMPLATE_TYPE_CUSTOM",
    "TEMPLATE_TYPES",
    "ZONE_TYPE_BACKGROUND",
    "ZONE_TYPE_VIDEO",
    "ZONE_TYPE_CAPTION",
    "ZONE_TYPE_AI_TEXT",
    "ZONE_TYPE_LOGO",
    "ZONE_TYPES",
    "validate_layout",
    "BUILTIN_TEMPLATE_SPECS",
    "DEFAULT_BUILTIN_ASSETS_DIR",
    "PreviewInfo",
    "CONTENT_TYPE_FIXED_TEXT",
    "CONTENT_TYPE_AI_HOOK",
    "CONTENT_TYPE_AI_TITLE",
    "CONTENT_TYPE_AI_SUMMARY",
    "CONTENT_TYPE_AI_QUESTION",
    "CONTENT_TYPE_AI_CTA",
    "CONTENT_TYPE_CUSTOM_AI",
    "CONTENT_TYPES",
    "CONTENT_ALIGNMENTS",
    "MAX_LINES_RANGE",
    "MAX_CHARS_RANGE",
    "CONTENT_FONT_SIZE_RANGE",
    "AutoFitResult",
    "apply_auto_fit",
]

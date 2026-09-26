# -*- coding: utf-8 -*-
"""RenderEngine -- Prompt 42 (Fase 5: Editor Modular Não Destrutivo).

TEXTO LITERAL DO ROADMAP (implementado exatamente):

    Crie RenderEngine separado da UI.

    Entrada:
    - Source
    - Edit Decisions
    - Template opcional
    - Captions opcionais
    - Dynamic text
    - Images/assets
    - Audio settings
    - Metadata settings

    Saída: MP4 final.

    Construir pipeline FFmpeg de forma modular.
    Usar arquivos temporários + rename atômico.
    Nunca considerar render completo enquanto output não for validado.

    EVIDÊNCIA PARA CATÁLOGO
    Render concluído só pode produzir RENDERED quando o Artifact
    correspondente existir e passar pelas validações exigidas.
    A existência de arquivo parcial não basta.

===========================================================================
0. VISÃO GERAL -- O MÓDULO MAIS AMPLO DESTA FASE
===========================================================================

Este é o primeiro módulo de Geração 2 que efetivamente escreve o arquivo
de vídeo FINAL, integrando NOVE fontes de decisão diferentes já
construídas em Prompts anteriores (Source, CUTS/SPEED, CROP/VISUAL_
ADJUSTMENTS, REFRAME automático, TEMPLATE, CAPTIONS, TEXT_LAYERS, AUDIO_
SETTINGS, METADATA_MODE). Segue a MESMA estrutura em duas partes já
estabelecida por ``captions_engine.py``/``auto_reframe.py``:

--- PARTE A -- RESOLUÇÃO ---
Lê TODAS as fontes de decisão já persistidas de um ``Project``, sem
inventar nenhum schema novo -- cada categoria é lida através do módulo
que já a possui (``TimelineEditor``/``VisualEditor``/``AudioEngine``/
``CaptionsEngine``/``AutoReframeEngine``/``TemplateSelector``+
``TemplateEngine``), nunca por leitura direta e duplicada de
``EditProjectManager.get_category`` quando um acessor tipado já existe.

--- PARTE B -- PROCESSAMENTO REAL ---
Constrói um pipeline FFmpeg MODULAR (seção 2), executa via um backend
FFmpeg injetável e testável (seção 0.2), valida o resultado via
``MediaProbe`` ANTES de promover (seção 0.5 -- exigência literal do
roadmap), e só então grava ``CHECKPOINT_RENDERED`` + ``Artifact``
``kind="render_output"`` (seção 0.6).

===========================================================================
0.1 INFRAESTRUTURA REAPROVEITADA -- NADA REINVENTADO
===========================================================================

- ``JobEngine``: registra um handler (``RenderEngine.handle_render_job``,
  contrato ``JobHandler = Callable[[Job], JobStepResult]``) para a nova
  ``operation`` ``RENDER_VIDEO`` (``claims_status=JOB_PROCESSING`` --
  trabalho local, sem efeito remoto observável). Mesmo mecanismo de
  ``captions_engine.py``/``auto_reframe.py``/``metadata_manager.py``,
  nenhum loop de execução próprio.
- ``TimelineEditor.get_timeline``/``VisualEditor.get_frame``/
  ``.get_adjustments``/``AudioEngine.get_audio_settings``/
  ``CaptionsEngine.get_mode``/``AutoReframeEngine.get_config``/
  ``TemplateSelector.get_template_selection``/``TemplateEngine.get_template``:
  cada fonte de decisão é lida através do módulo que já a possui --
  nunca uma segunda leitura/validação paralela de
  ``EditProjectManager.get_category``.
- ``MediaProbe.probe``/``probe_deep``: usado tanto para ler a resolução
  do vídeo de origem (geometria de crop/reframe/template) quanto para a
  validação pós-render obrigatória (seção 0.5).
- ``StorageManager.allocate_temp``/``promote_to_final``: mesmo padrão de
  escrita atômica de todo o resto do projeto -- cada etapa do pipeline
  escreve em um arquivo temporário novo; só o resultado final validado é
  promovido ao caminho determinístico por ``cache_key``.
- ``OperationalAuditLog.record_checkpoint``: grava ``CHECKPOINT_RENDERED``
  (ver seção 0.6 -- decisão já dada pelo Prompt, não ambígua).
- ``ControlManager.is_job_cancel_requested``: mesmo padrão de dois pontos
  de checagem já usado em ``captions_engine.py``/``auto_reframe.py``.

===========================================================================
0.2 BACKEND FFMPEG -- INJETÁVEL, TESTÁVEL SEM FFMPEG REAL
===========================================================================

Ao contrário de ``captions_engine.py`` (Whisper) e ``auto_reframe.py``
(detecção de visão computacional), FFmpeg em si ESTÁ disponível neste
ambiente e É o próprio objeto deste Prompt ("Construir pipeline FFmpeg de
forma modular") -- não há aqui a mesma desculpa de "biblioteca
indisponível" que justificou um backend-stub em ``auto_reframe.py``. Este
módulo constrói comandos FFmpeg REAIS, corretos, não um stub.

A separação testável fica em OUTRO eixo: a CONSTRUÇÃO de cada comando
(qual lista de argumentos ``ffmpeg`` representa uma decisão) é feita por
FUNÇÕES PURAS de nível de módulo (``build_*_command``, seção 2) que
recebem parâmetros já resolvidos (floats/ints/strings/tuplas) e devolvem
``list[str] | None`` -- testáveis por completo, milhares de combinações,
SEM nunca invocar um processo real. A EXECUÇÃO desse comando (rodar o
subprocesso de verdade) é isolada atrás de um backend injetável, mesmo
padrão exato de ``_default_ffmpeg_strip_backend``
(``metadata_manager.py``)/``_default_transcribe``:

    FFmpegBackend = Callable[[list[str]], None]

``RenderEngine.__init__`` aceita ``ffmpeg_backend: FFmpegBackend | None``
(default ``_default_ffmpeg_backend``, que faz
``shutil.which("ffmpeg")``+``subprocess.run(cmd, capture_output=True,
timeout=...)``, nunca deixa ``TimeoutExpired``/``OSError`` cru escapar).
Testes de UNIDADE injetam um backend fake determinístico (nunca chamam
FFmpeg de verdade) para testar a ORQUESTRAÇÃO (cache/Job/Artifact/
validação/badge). Testes de INTEGRAÇÃO (poucos, marcados e puláveis --
seção 6 do relatório) usam o backend PADRÃO com FFmpeg real, pulados via
``shutil.which("ffmpeg") is None`` quando o binário não está disponível
no ambiente (mesmo padrão de robustez de ``media_probe.py``).

===========================================================================
0.3 RESOLUÇÃO DE CADA ENTRADA DO ROADMAP
===========================================================================

--- Source ---
``Video.source_asset_id`` -> ``SourceAsset.local_path``/``.fingerprint``.
NUNCA aberto em modo de escrita -- toda etapa do pipeline usa
``SourceAsset.local_path`` apenas como leitura (``-i`` do FFmpeg nunca
grava no arquivo de entrada) e escreve SEMPRE em um novo arquivo alocado
por ``StorageManager.allocate_temp``. Teste dedicado prova hash/mtime do
original inalterados (seção matriz de testes).

--- Edit Decisions (CUTS/SPEED/CROP/REFRAME) ---
``CUTS``/``SPEED``: ``TimelineEditor.get_timeline(project_id)`` ->
``tuple[Segment, ...]`` (``speed`` já embutido por segmento -- ver
``timeline_editor.py`` seção 1.1: a categoria ``SPEED`` nunca é usada
separadamente). Lista vazia = sem cortes = vídeo inteiro, sem nenhuma
mudança de velocidade (etapa 1 do pipeline inteiramente pulada, seção
2.1).

``CROP``/``VISUAL_ADJUSTMENTS``: ``VisualEditor.get_frame``/
``.get_adjustments(project_id)`` -> ``FrameState``/``AdjustmentsState``
(decisões MANUAIS do usuário).

``REFRAME``: ``AutoReframeEngine.get_config(project_id)`` -> decisão
automática. PRECEDÊNCIA (decisão deste Prompt, documentada): um ``CROP``
manual (``FrameState.crop`` não-``None``) SEMPRE tem precedência sobre
``REFRAME`` automático, mesmo que ``REFRAME.enabled`` seja ``True`` --
uma decisão explícita do usuário nunca é silenciosamente sobrescrita por
um sistema automático. ``REFRAME`` só é aplicado quando NENHUM ``crop``
manual está presente E ``REFRAME.enabled is True`` E existe um
``Artifact`` ``kind="reframe_track"`` válido para o ``cache_key`` da
configuração atual de REFRAME (reaproveita ``auto_reframe.compute_cache_key``
e o mesmo padrão de lookup por ``(video_id, kind, fingerprint)`` --
nunca uma segunda implementação paralela do cálculo). Se ``REFRAME``
está habilitado mas NENHUM ``Artifact`` de tracking existe ainda (Job de
reenquadramento nunca rodou para esta configuração), o render segue SEM
reenquadramento automático -- nunca bloqueia, nunca falha por causa
disso (mesmo espírito do roadmap "nunca bloquear Job por falha de
tracking", aqui estendido para "ausência de tracking").

Aplicação do REFRAME: a curva de tracking (``keyframes``: lista de
``{"t", "x", "y"}``, coordenadas normalizadas do CENTRO da região de
interesse) é resumida a uma ÚNICA posição ESTÁTICA (média aritmética de
``x``/``y`` de todos os keyframes) usada como centro de um crop
proporção-alvo (``9:16``) fixo durante todo o vídeo. DECISÃO DOCUMENTADA
(não escondida): esta é uma simplificação deliberada -- aplicar a curva
INTEIRA como reenquadramento DINÂMICO por frame (via expressão de tempo
no filtro ``crop``) é tecnicamente possível mas substancialmente mais
complexo (equivalente a um segundo eixo de complexidade completo,
comparável em escopo a todo o resto deste Prompt) e não é exigido pelo
texto literal deste Prompt (que lista ``REFRAME`` genericamente como
"Edit Decision" de entrada, sem especificar aplicação frame-a-frame).
Registrado como dívida técnica explícita (seção 9 do relatório) para uma
etapa futura dedicada.

--- Template opcional ---
``TemplateSelector.get_template_selection(project_id)`` -> ``str | None``.
Quando ``None``: pipeline produz o vídeo processado diretamente, sem
nenhuma etapa de composição (seção 2.4 pulada por completo). Quando
presente: ``TemplateEngine.get_template(template_id)`` resolve o
``Template`` (``source_path``=arquivo de imagem de fundo real,
``extra["width"]``/``extra["height"]``=resolução real do canvas em
pixels -- MEDIDA do arquivo real via ``ffprobe`` na ingestão, nunca
inventada aqui -- e ``layout["zones"]`` com a zona ``VIDEO`` cuja
geometria normalizada, multiplicada pelo canvas, define a caixa onde o
vídeo processado é sobreposto). Modo de encaixe do vídeo na zona:
"cover" (preenche a caixa inteira, cortando o excedente -- decisão
documentada, seção 2.4) -- não há campo de ``fit_mode`` por zona no
schema de ``Template`` hoje (isso é uma propriedade de ``CROP``, não de
``Template``), então um modo fixo e sensato é necessário; "cover" evita
barras vazias dentro do template, o resultado visualmente esperado para
um vídeo curto em uma moldura.

--- Captions opcionais ---
Investigado: ``captions_style.py`` (``CAPTIONS_STYLE``) NUNCA decide
"SE" embutir -- só a APARÊNCIA de uma legenda que já será embutida. Quem
decide "se" é ``CAPTIONS.mode`` (``captions_engine.py``, Prompt 31):
``OFF``/ausente = sem legenda nenhuma no vídeo final; ``SEPARATE`` =
legenda permanece como arquivo entregável à parte (SRT/VTT já produzidos
por ``captions_engine.py`` Parte B), NUNCA embutida nos pixels;
``BURNED`` = a legenda É embutida nos pixels do render final (a decisão
"deve ser queimada" já estava tomada desde o Prompt 31 -- este é
exatamente "o Render Engine futuro" citado na docstring daquele módulo
como quem executa a queima real).

Quando ``mode == BURNED``: qual ``Artifact`` ``captions_srt`` usar? Não
existe hoje nenhuma categoria que registre COM QUAL configuração
(``model_size``/``language``) a transcrição do vídeo foi feita -- só o
``mode`` de exibição é uma decisão de Project. DECISÃO (documentada,
única razoável sem inventar uma categoria nova fora de escopo): usa o
``Artifact`` ``kind="captions_srt"`` MAIS RECENTE (``created_at``
``DESC``) para o ``video_id`` -- "a transcrição mais recente é a atual"
é a interpretação natural na ausência de um vínculo explícito de
configuração. Se nenhum ``Artifact`` de legenda existir ainda para este
vídeo, ``BURNED`` sem nenhuma legenda disponível é tratado como "sem
legenda para queimar ainda" -- o render prossegue SEM legenda (nunca
falha por isso; mesmo espírito de "nunca bloquear por falha de etapa
auxiliar"), e o resultado é reportado em ``data`` do ``JobStepResult``
(``"captions_requested_but_unavailable": True``) para diagnóstico, sem
impedir a conclusão do render.

Estilo de queima (``force_style`` do filtro ``subtitles`` do FFmpeg):
``CaptionsStyleEngine.get_style(project_id)`` fornece
``font``/``size``/``alignment``/``stroke``/``shadow``. Mapeados para
ASS ``force_style``: ``FontName``/``FontSize``/``Alignment`` (LEFT=1,
CENTER=2, RIGHT=3 -- linha inferior do padrão ASS)/``Outline``+
``OutlineColour`` (de ``stroke``)/``Shadow`` (de ``shadow.enabled``).
LIMITAÇÃO DOCUMENTADA (dívida técnica, seção 9): ``background``/
``position``/``animation``/``current_word_highlight`` NÃO têm mapeamento
direto e completo em ``force_style`` do filtro ``subtitles`` simples
(exigiriam gerar um arquivo ``.ass`` completo com estilos por palavra --
escopo substancialmente maior, fora deste Prompt) -- ``position`` É
aproximada via margens (``MarginV``) quando fornecida; os demais campos
são deliberadamente ignorados nesta etapa, documentados, não
silenciados.

--- Dynamic text ---
``TEXT_LAYERS`` -- lido via ``EditProjectManager.get_category(project_id,
TEXT_LAYERS)`` (categoria ainda SEM NENHUM produtor real, confirmado por
grep -- ver evidência do Prompt). A leitura existe e está pronta (nunca
lança erro se ausente, nunca assume um schema específico), mas hoje
SEMPRE devolve ``None``/ausente na prática -- nenhuma composição de
texto dinâmico é de fato realizada nesta etapa (não há schema para
compor). Documentado explicitamente como "leitura pronta, sem produtor
ainda" -- nunca um schema inventado aqui.

--- Images/assets ---
Investigado: nenhuma fonte real de "imagens/assets" existe hoje além do
próprio arquivo de imagem de fundo de um ``Template`` (``source_path``).
Não há uma categoria/Artifact de "imagem solta" anexável a um Project.
Tratado como já coberto pelo item Template (seção acima) -- nenhuma
categoria nova inventada.

--- Audio settings ---
``AudioEngine.get_audio_settings(project_id)`` -> ``AudioSettingsState``.
Estado totalmente ``None`` (nunca configurado) = etapa de áudio
inteiramente pulada (seção 2.3).

--- Metadata settings ---
Investigado ``metadata_manager.py``: hoje processa uma CÓPIA do
ORIGINAL (``SourceAsset.local_path``), produzindo um ``Artifact``
``metadata_clean_output`` próprio, INDEPENDENTE de qualquer render --
isso continua válido como um caso de uso próprio ("exportar o vídeo
ORIGINAL com metadata limpa, sem nenhuma edição"). Mas agora que um
render real existe, o metadata do ENTREGÁVEL final (o MP4 renderizado,
não o original) também precisa refletir ``METADATA_MODE`` quando o
usuário tiver decisões de edição aplicadas. DECISÃO: ``RenderEngine``
NÃO chama ``metadata_manager.py``/seu Job -- ao invés disso, dobra a
remoção de metadata (quando ``METADATA_MODE.mode == CLEAN``) DENTRO da
própria etapa de encode final (seção 2.6), usando exatamente as mesmas
flags FFmpeg já usadas por ``_default_ffmpeg_strip_backend``
(``-map_metadata -1`` + limpeza dos campos de texto conhecidos).
Motivo de NÃO encadear um segundo passo separado sobre o output do
render: re-encodar um MP4 já finalizado apenas para trocar metadata
seria uma segunda geração de perda de qualidade (double lossy
re-encode) sem necessidade -- ``-map_metadata -1`` é barato de aplicar
JUNTO do encode final que de qualquer forma já vai acontecer (seção
2.6 roda incondicionalmente). ``mode == KEEP``: nenhuma flag de remoção
é aplicada (metadata do container final segue o que o FFmpeg grava por
padrão -- nunca uma cópia bit-a-bit do original, já que o conteúdo
sempre passa por pelo menos um re-encode nesta etapa). ``mode ==
PROFILE``: tratado IGUAL a ``KEEP`` nesta etapa (mesma decisão de
adiamento já tomada por ``metadata_manager.py`` Prompt 35 --
``"profile_processing_deferred"`` -- nenhum perfil é aplicado aqui,
documentado, não reimplementado).

===========================================================================
0.4 RESOURCEMANAGER -- DECISÃO: ADIAR (NÃO INTEGRAR NESTA RODADA)
===========================================================================

Investigado: ``ResourceManager`` existe (``resource_manager.py``,
Prompt 18) mas está 100% não-instanciado em todo o projeto hoje
(confirmado por grep -- só referenciado como parâmetro opcional tipado
em ``JobEngine.__init__``, nunca construído). ``RenderEngine`` seria a
PRIMEIRA integração real.

DECISÃO: adiar. Motivos:
1. Seria a primeira integração real de ``ResourceManager`` em produção --
   um risco não-trivial de revelar lacunas de API que merecem sua própria
   rodada dedicada (ex.: qual perfil de hardware usar, como
   ``acquire``/``acquire_for`` interage exatamente com
   ``JobEngine.admit_heavy_work``, quais são os valores reais de
   CPU/RAM a reservar por render -- nenhum desses números tem evidência
   real hoje).
2. Overengineering: adicionar uma integração não-trivial "porque parece
   certo arquiteturalmente" sem um caso de uso real testado violaria o
   princípio do CLAUDE.md contra abstração/complexidade sem necessidade
   demonstrada AGORA.
3. Este Prompt já tem escopo máximo (nove fontes de decisão + pipeline
   FFmpeg real) -- adicionar uma décima superfície de integração não
   testada no mesmo Prompt aumentaria desnecessariamente o risco de
   qualquer uma delas.

RISCO DOCUMENTADO (não mitigado nesta rodada): múltiplos renders
concorrentes (ex.: um lote futuro renderizando vários Projects ao mesmo
tempo) hoje NÃO têm nenhum controle de CPU/RAM/GPU -- o sistema
operacional decide a escalonagem sem nenhuma reserva do produto. Isso é
o comportamento ATUAL de todo o projeto (nenhum módulo usa
``ResourceManager`` ainda), não uma regressão introduzida aqui. Nenhum
mecanismo alternativo/paralelo de controle de recursos foi inventado --
zero reserva, exatamente como hoje.

===========================================================================
0.5 VALIDAÇÃO PÓS-RENDER -- EXIGÊNCIA LITERAL DO ROADMAP
===========================================================================

"Nunca considerar render completo enquanto output não for validado."
Após a ÚLTIMA etapa do pipeline (encode final, seção 2.6) e ANTES de
``promote_to_final``/gravar checkpoint/Artifact, o arquivo TEMPORÁRIO
resultante é validado via ``MediaProbe.probe_deep`` (não ``probe`` raso
-- decisão: este é o ENTREGÁVEL final ao usuário, não uma geometria
intermediária como em ``auto_reframe.py`` seção 0.3; ``probe_deep``
efetivamente decodifica uma janela do arquivo, a validação mais forte
disponível sem processar o vídeo inteiro de novo). Checagens mínimas
exigidas, todas obrigatórias:

1. ``result.valid is True`` (nenhum erro estrutural/de decodificação).
2. ``result.duration is not None and result.duration > 0`` (não é um
   container vazio/corrompido).
3. ``result.width`` e ``result.height`` presentes e ``> 0`` (existe
   stream de vídeo decodificável -- ``MediaProbeResult`` não tem um
   campo booleano dedicado de "tem stream de vídeo"; ausência de
   largura/altura é o sinal equivalente, confirmado por leitura de
   ``media_probe.py``).

Se QUALQUER checagem falhar: o arquivo temporário é apagado, o render
FALHA de forma estruturada (``JobStepResult(target_status=JOB_FAILED,
data={"reason": "post_render_validation_failed", "error_code": ...})``)
-- NUNCA promove, NUNCA grava ``CHECKPOINT_RENDERED``, NUNCA registra
``Artifact``. Esta validação é SANITÁRIA (arquivo não corrompido/vazio),
deliberadamente DIFERENTE e mais restrita que a futura validação de
qualidade mais ampla do Prompt 43 (``FinalMediaValidator``,
``CHECKPOINT_VALIDATED``/``BADGE_VALIDATED``, fora de escopo aqui --
nada equivalente é implementado neste módulo).

===========================================================================
0.6 CHECKPOINT/BADGE -- DECISÕES JÁ DADAS PELO PROMPT
===========================================================================

``CHECKPOINT_RENDERED`` -- confirmado 100% livre hoje (nenhum módulo
grava). ``CHECKPOINT_VALIDATED``/``BADGE_VALIDATED`` NUNCA são tocados
por este módulo (reservados para o Prompt 43).

``BADGE_RENDERED`` ganha expressão SQL real em ``media_catalog.py``,
seguindo EXATAMENTE o padrão duplo de ``BADGE_REFRAMED_9_16``:
checkpoint ``CHECKPOINT_RENDERED`` E ``Artifact`` ``kind="render_output"``
com arquivo físico real no disco (``_CATALOG_FS_FUNCTION_NAME(a.path) = 1``)
-- checkpoint sozinho nunca basta (exigência literal do roadmap: "a
existência de arquivo parcial não basta").

===========================================================================
0.7 CACHE_KEY -- COMPOSIÇÃO DETERMINÍSTICA
===========================================================================

Base: ``SourceAsset.fingerprint`` + ``EditProjectManager.aggregate_fingerprint
(project_id)`` (reaproveitado sem modificação -- já deriva
deterministicamente de TODOS os fingerprints por categoria, incluindo
``TEMPLATE``/``CROP``/``CUTS``/``AUDIO_SETTINGS``/``CAPTIONS``/``REFRAME``
automaticamente quando qualquer um deles muda).

Dois componentes ADICIONAIS, fechando lacunas reais que o agregado
sozinho NÃO cobre (analisado explicitamente, não presumido):

1. ``captions_artifact.fingerprint`` (o ``cache_key`` da transcrição
   usada para queima, quando ``CAPTIONS.mode == BURNED`` e um
   ``Artifact`` foi resolvido -- seção 0.3). LACUNA REAL fechada: a
   categoria ``CAPTIONS`` só armazena ``{"mode": ...}`` -- ela NUNCA
   muda se o usuário simplesmente regerar a transcrição com um
   ``model_size``/``language`` diferente (nenhuma categoria de Project
   registra essa config). Sem incluir o fingerprint do Artifact
   efetivamente selecionado, o render ficaria cacheado incorretamente
   (cache hit obsoleto) sempre que uma NOVA transcrição virasse "a mais
   recente" sem nenhuma outra mudança de Project. Incluí-lo fecha essa
   lacuna por completo (uma nova transcrição = um novo
   ``Artifact.fingerprint`` = um novo ``cache_key`` de render).
2. ``compute_template_content_fingerprint(template)`` (quando um
   ``Template`` está selecionado) -- hash determinístico de
   ``source_path``/``layout``/``extra``. LACUNA REAL fechada:
   ``TEMPLATE`` (a categoria) só armazena ``{"template_id": ...}`` -- se
   o MESMO ``template_id`` tiver seu ``layout``/``source_path``
   alterados via ``TemplateEngine.update_template`` (o ``Template`` é
   uma entidade independente, mutável por design), a categoria
   ``TEMPLATE`` do Project NÃO muda (o id é o mesmo), mas o resultado
   visual do render mudaria. CORREÇÃO ADVERSARIAL (achada por teste, não
   presumida): a primeira versão usava ``template.updated_at`` para
   fechar essa lacuna, mas ``updated_at`` tem resolução de SEGUNDO
   INTEIRO (strings ISO sem frações de segundo) -- dois
   ``update_template`` no mesmo segundo produziriam o MESMO
   ``updated_at``, deixando o cache_key idêntico mesmo com o template
   fisicamente alterado. Um fingerprint de CONTEÚDO fecha a lacuna de
   forma robusta a qualquer granularidade de timestamp.
   ``REFRAME`` NÃO precisa de um componente equivalente: sua config
   inteira (``enabled``/``target_aspect_ratio``/``smoothing_strength``/
   ``fallback_strategy``) já vive DENTRO da categoria ``REFRAME``, já
   coberta pelo agregado -- qualquer mudança de config já muda o
   ``cache_key`` do PRÓPRIO ``reframe_track`` (e, por consequência, o
   agregado do Project), sem precisar de reforço extra.

``RENDER_PIPELINE_VERSION`` (mesmo espírito de
``DETECTOR_BACKEND_VERSION`` em ``auto_reframe.py``): incrementado
manualmente sempre que a LÓGICA de construção do pipeline (não os dados
de entrada) mudar de forma que invalide renders já promovidos.

===========================================================================
0.8 CACHE-HIT -- ESCOPO POR PROJECT, NÃO POR VIDEO
===========================================================================

Diferente de ``captions_engine.py``/``auto_reframe.py`` (que são
operações de VÍDEO, independentes de qualquer Project), um render
depende inteiramente das decisões de edição de um Project específico --
dois Projects do MESMO vídeo podem produzir renders diferentes. A
consulta de cache-hit é portanto por ``(project_id, kind, fingerprint)``,
NUNCA por ``video_id`` sozinho (uma correção deliberada sobre o padrão
video-only dos módulos irmãos, justificada pela natureza do dado -- não
uma duplicação cega do precedente). Mesma disciplina de "cache-hit é
100% por linha na base, nunca por existência de arquivo no disco" dos
módulos irmãos (``captions_engine.py``/``auto_reframe.py``) -- a
verificação de arquivo real é responsabilidade da expressão SQL do
badge (defesa em profundidade), não do cache-hit em si. Risco herdado
documentado (não introduzido aqui): se o arquivo físico for apagado
externamente sem remover a linha ``Artifact``, um cache-hit reportaria
sucesso sem um arquivo real -- mesmo risco já aceito por
``captions_engine.py``/``auto_reframe.py`` hoje.

===========================================================================
1. ORDEM DO PIPELINE -- JUSTIFICATIVA
===========================================================================

    1. CUTS/SPEED (cortes + velocidade por segmento)
    2. CROP/VISUAL_ADJUSTMENTS/REFRAME (enquadramento + ajustes visuais)
    3. AUDIO_SETTINGS (áudio)
    4. TEMPLATE (composição -- moldura externa)
    5. CAPTIONS (queima de legenda, mode=BURNED)
    6. Encode final + METADATA_MODE (sempre executa)

Cortes/velocidade primeiro: as demais etapas trabalham sobre o conteúdo
JÁ NA DURAÇÃO/ORDEM final -- aplicar crop/áudio/template antes de cortar
processaria trechos que seriam descartados, desperdiçando trabalho.
Crop/ajustes antes de áudio: são independentes entre si (vídeo vs.
áudio), ordem entre os dois não importa tecnicamente, mas manter vídeo
antes de áudio segue a ordem natural de leitura do roadmap. Template por
ÚLTIMO entre as etapas de composição visual: é a "moldura externa" que
envolve o conteúdo já pronto (cortado, enquadrado, com áudio definido)
-- compor antes disso processaria/reencodaria a moldura estática
repetidamente a cada mudança de uma decisão interna, sem necessidade.
Legenda por último (antes do encode final): a posição da legenda
(``captions_style.CaptionsStyleState.position``) é expressa em
coordenadas 0.0-1.0 do FRAME FINAL -- só faz sentido aplicá-la depois
que o frame final (já com o template, se houver) está definido; queimar
a legenda antes do template a colocaria no espaço de coordenadas errado
(o vídeo sozinho, não o canvas do template). Encode final sempre por
último e sempre executado incondicionalmente: garante que o render
SEMPRE produz um MP4 padronizado e validável, mesmo quando todas as
etapas 1-5 foram puladas (Project sem nenhuma decisão de edição --
"vídeo pronto -> exportar").

===========================================================================
2. PIPELINE MODULAR -- CADA ETAPA COMO FUNÇÃO PURA
===========================================================================

Cada ``build_*_command`` é uma função de módulo (não um método), recebe
parâmetros já resolvidos (nunca acessa banco/arquivo/rede) e devolve
``list[str] | None`` (``None`` = etapa pulada, o arquivo de entrada
segue para a próxima etapa sem nenhuma invocação de processo). Isso
satisfaz a exigência do Prompt: cada etapa é testável isoladamente, sem
FFmpeg real, e a ausência de qualquer entrada opcional produz um
pipeline correto (menos etapas, nunca um erro).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ClassVar, Mapping, Sequence

from .app_paths import AppPaths
from .audio_engine import AudioEngine, AudioSettingsState
from .auto_reframe import (
    ARTIFACT_KIND_REFRAME_TRACK,
    ASPECT_RATIO_9_16,
    AutoReframeEngine,
    FALLBACK_CENTER_CROP,
    compute_cache_key as _reframe_compute_cache_key,
)
from .captions_engine import ARTIFACT_KIND_SRT, CaptionsEngine, MODE_BURNED
from .captions_style import CaptionsStyleEngine, CaptionsStyleState
from .control_manager import ControlManager
from .domain import (
    Artifact,
    CHECKPOINT_RENDERED,
    Job,
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_READY,
    SourceAsset,
    Template,
    Video,
)
from .edit_project import EditProjectManager, METADATA_MODE, TEXT_LAYERS
from .job_engine import JobStepResult
from .media_probe import MediaProbe, MediaProbeResult
from .metadata_manager import MODE_CLEAN as METADATA_MODE_CLEAN
from .storage.audit import AUDIT_JOB_PROCESSING_COMPLETED, OperationalAuditLog
from .storage.database import LocalDatabase
from .storage_manager import StorageManager
from .template_engine import TemplateEngine, ZONE_TYPE_VIDEO
from .template_selector import TemplateSelector
from .visual_editor import AdjustmentsState, FrameState, VisualEditor

__all__ = [
    "OPERATION_RENDER_VIDEO",
    "ARTIFACT_KIND_RENDER_OUTPUT",
    "RENDER_PIPELINE_VERSION",
    "RenderEngineError",
    "CampoInvalidoError",
    "FFmpegBackend",
    "compute_cache_key",
    "compute_template_content_fingerprint",
    "build_trim_speed_command",
    "build_crop_reframe_command",
    "build_audio_command",
    "build_template_compose_command",
    "build_captions_burn_command",
    "build_final_encode_command",
    "RenderEngine",
]

# ---------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------

OPERATION_RENDER_VIDEO = "RENDER_VIDEO"
ARTIFACT_KIND_RENDER_OUTPUT = "render_output"

# Incrementar sempre que a LÓGICA de construção do pipeline mudar de
# forma que invalide renders já promovidos (ver seção 0.7).
RENDER_PIPELINE_VERSION = 1

_FFMPEG_TIMEOUT_SECONDS = 900.0
_INTERMEDIATE_CRF = 18
_FINAL_CRF = 20
_ROTATIONS = (0, 90, 180, 270)
_FIT_MODES = ("FIT", "FILL", "STRETCH")
_ALIGNMENT_TO_ASS = {"LEFT": 1, "CENTER": 2, "RIGHT": 3}
_METADATA_STRIP_ARGS = [
    "-map_metadata", "-1",
    "-map_chapters", "-1",
    "-metadata", "title=",
    "-metadata", "comment=",
    "-metadata", "artist=",
    "-metadata", "copyright=",
    "-metadata", "description=",
]


# ---------------------------------------------------------------------
# Erros estruturados
# ---------------------------------------------------------------------

class RenderEngineError(RuntimeError):
    """Classe base de todos os erros deste módulo."""

    code: ClassVar[str] = "RENDER_ENGINE_ERRO"


class CampoInvalidoError(RenderEngineError):
    """Um argumento recebeu um valor de tipo errado ou inválido."""

    code: ClassVar[str] = "CAMPO_INVALIDO"


class FFmpegBackendError(RenderEngineError):
    """O backend FFmpeg falhou (processo, timeout etc.)."""

    code: ClassVar[str] = "BACKEND_FALHOU"


class FFmpegBackendUnavailableError(FFmpegBackendError):
    """``ffmpeg`` não está disponível neste ambiente."""

    code: ClassVar[str] = "BACKEND_INDISPONIVEL"


# ---------------------------------------------------------------------
# Backend FFmpeg injetável -- ver seção 0.2
# ---------------------------------------------------------------------

FFmpegBackend = Callable[["list[str]"], None]


def _default_ffmpeg_backend(cmd: "list[str]") -> None:
    """Backend de produção padrão -- ``shutil.which`` + ``subprocess.run``
    com timeout, nunca deixa exceção crua do ``subprocess`` escapar
    (mesmo padrão de ``_default_ffmpeg_strip_backend`` em
    ``metadata_manager.py``)."""
    if shutil.which("ffmpeg") is None:
        raise FFmpegBackendUnavailableError("ffmpeg não encontrado no PATH")
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=_FFMPEG_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise FFmpegBackendError("timeout ao executar ffmpeg") from None
    except OSError:
        raise FFmpegBackendError("falha ao iniciar o processo ffmpeg") from None
    if result.returncode != 0:
        raise FFmpegBackendError(f"ffmpeg terminou com código {result.returncode}")


# ---------------------------------------------------------------------
# cache_key -- ver seção 0.7
# ---------------------------------------------------------------------

def _canonical_json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_cache_key(
    source_fingerprint: str,
    aggregate_fingerprint: str,
    *,
    captions_artifact_fingerprint: "str | None" = None,
    template_content_fingerprint: "str | None" = None,
) -> str:
    """Chave de cache determinística do render -- ver seção 0.7 da
    docstring do módulo para a justificativa de cada componente.

    ``template_content_fingerprint`` -- CORREÇÃO adversarial (achada por
    teste, não presumida): a primeira versão deste componente usava
    ``Template.updated_at`` diretamente. ``updated_at`` tem resolução de
    SEGUNDO INTEIRO (``created_at``/``updated_at`` são strings ISO sem
    frações de segundo -- ver ``domain/models.py``) -- dois
    ``update_template`` no MESMO segundo (trivial em um teste, e não
    impossível em uso real com automação/lote) produzem o MESMO
    ``updated_at``, silenciosamente deixando o cache_key idêntico mesmo
    com o template fisicamente alterado. Substituído por um fingerprint
    de CONTEÚDO (hash determinístico de ``source_path``/``layout``/
    ``extra`` -- os únicos campos que afetam o resultado visual do
    render), que muda sempre que o conteúdo muda, independente de
    timestamp -- e nunca muda para um ``update_template`` que efetivamente
    não altera nada (idempotência mais forte que um timestamp poderia
    garantir)."""
    payload = {
        "source_fingerprint": str(source_fingerprint),
        "aggregate_fingerprint": str(aggregate_fingerprint),
        "captions_artifact_fingerprint": captions_artifact_fingerprint,
        "template_content_fingerprint": template_content_fingerprint,
        "pipeline_version": RENDER_PIPELINE_VERSION,
    }
    canonical = _canonical_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_template_content_fingerprint(template: Template) -> str:
    """Hash determinístico do conteúdo VISUALMENTE relevante de um
    ``Template`` (``source_path``/``layout``/``extra``) -- usado para
    fechar a lacuna de cache descrita em ``compute_cache_key`` acima.
    Função de módulo pura (testável isoladamente, sem construir
    ``RenderEngine``)."""
    payload = {
        "source_path": template.source_path,
        "layout": template.layout,
        "extra": template.extra,
    }
    canonical = _canonical_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------
# Helpers geométricos puros -- reusados pelos builders de comando
# ---------------------------------------------------------------------

def _round_even(value: float) -> int:
    """Arredonda para o inteiro par mais próximo, mínimo 2 -- usado
    exclusivamente para LARGURA/ALTURA de saída (H.264 exige dimensões
    pares; item 11 do GATE: comportamento consistente em toda
    plataforma, não só Windows, mas testado aqui por segurança). NUNCA
    usar para deslocamentos (x/y) -- ``0`` é um deslocamento válido e
    comum, que este arredondamento forçaria incorretamente para ``2``."""
    n = int(round(value))
    if n < 2:
        n = 2
    if n % 2 != 0:
        n += 1
    return n


def _round_offset(value: float) -> int:
    """Arredonda um DESLOCAMENTO (x/y) para o inteiro mais próximo, sem
    mínimo e sem forçar paridade -- ``0`` é um valor legítimo e comum
    (ex.: crop começando na borda esquerda/superior do frame)."""
    return int(round(value))


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------
# Etapa 1 -- CUTS/SPEED (trim + concat)
# ---------------------------------------------------------------------

def _atempo_chain(speed: float) -> "list[str]":
    """``atempo`` só aceita fatores em ``[0.5, 2.0]`` por instância --
    decompõe qualquer fator dentro de ``[0.25, 4.0]`` (faixa validada por
    ``timeline_editor.py``) numa cadeia de fatores válidos."""
    if math.isclose(speed, 1.0, abs_tol=1e-9):
        return []
    factors: "list[float]" = []
    remaining = speed
    while remaining > 2.0 + 1e-9:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5 - 1e-9:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)
    return [f"atempo={f:.6f}" for f in factors]


def build_trim_speed_command(
    input_path: str,
    output_path: str,
    segments: "Sequence[tuple[float, float, float]]",
) -> "list[str] | None":
    """``segments``: tuplas ``(start, end, speed)`` em ordem de
    reprodução, ``start``/``end`` relativos ao vídeo ORIGINAL (mesma
    convenção de ``timeline_editor.py``). Lista vazia -> ``None``
    (etapa pulada, vídeo inteiro segue inalterado -- ver seção 0.3)."""
    if not segments:
        return None

    filter_parts: "list[str]" = []
    v_labels: "list[str]" = []
    a_labels: "list[str]" = []
    for i, (start, end, speed) in enumerate(segments):
        v_label = f"v{i}"
        a_label = f"a{i}"
        video_chain = f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS"
        if not math.isclose(speed, 1.0, abs_tol=1e-9):
            video_chain += f",setpts=(PTS-STARTPTS)/{speed:.6f}"
        video_chain += f"[{v_label}]"
        filter_parts.append(video_chain)

        audio_chain = f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS"
        for atempo in _atempo_chain(speed):
            audio_chain += f",{atempo}"
        audio_chain += f"[{a_label}]"
        filter_parts.append(audio_chain)

        v_labels.append(f"[{v_label}]")
        a_labels.append(f"[{a_label}]")

    concat_inputs = "".join(f"{v}{a}" for v, a in zip(v_labels, a_labels))
    filter_parts.append(
        f"{concat_inputs}concat=n={len(segments)}:v=1:a=1[outv][outa]"
    )
    filter_complex = ";".join(filter_parts)

    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(_INTERMEDIATE_CRF),
        "-c:a", "aac", "-b:a", "192k",
        output_path,
    ]


# ---------------------------------------------------------------------
# Etapa 2 -- CROP/VISUAL_ADJUSTMENTS/REFRAME
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class ReframeBox:
    """Caixa de crop ESTÁTICA derivada da curva de tracking (média dos
    keyframes) -- ver seção 0.3 (simplificação documentada)."""

    center_x: float  # normalizado 0.0-1.0
    center_y: float  # normalizado 0.0-1.0
    target_aspect_ratio: str  # ex. "9:16"


def _fit_scale_pad_filters(width: int, height: int, fit_mode: str) -> str:
    if fit_mode == "STRETCH":
        return f"scale={width}:{height}"
    if fit_mode == "FILL":
        return f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
    # FIT (default)
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
    )


def _rotation_filter(rotation: int) -> "str | None":
    if rotation == 90:
        return "transpose=1"
    if rotation == 180:
        return "transpose=1,transpose=1"
    if rotation == 270:
        return "transpose=2"
    return None


def _aspect_ratio_wh(target_aspect_ratio: str, frame_width: int, frame_height: int) -> "tuple[int, int]":
    """Maior caixa possível com a proporção-alvo que caiba dentro do
    frame original (``frame_width``/``frame_height``)."""
    num_str, _, den_str = target_aspect_ratio.partition(":")
    num, den = float(num_str), float(den_str)
    target_ratio = num / den  # ex. 9/16 = 0.5625

    candidate_w = frame_height * target_ratio
    if candidate_w <= frame_width:
        return _round_even(candidate_w), _round_even(frame_height)
    candidate_h = frame_width / target_ratio
    return _round_even(frame_width), _round_even(candidate_h)


def build_crop_reframe_command(
    input_path: str,
    output_path: str,
    *,
    input_width: int,
    input_height: int,
    frame: "FrameState | None",
    adjustments: "AdjustmentsState | None",
    reframe_box: "ReframeBox | None",
) -> "list[str] | None":
    """Constrói o filtro de vídeo combinando crop manual OU reframe
    automático (nunca os dois -- precedência do manual, ver seção 0.3),
    resize/fit_mode, zoom/position, rotação e ajustes visuais.
    ``None`` quando nada foi decidido (nenhum crop/reframe/ajuste)."""
    vf_parts: "list[str]" = []
    crop_payload = frame.crop if frame is not None else None

    if crop_payload is not None:
        x = float(crop_payload.get("x", 0.0))
        y = float(crop_payload.get("y", 0.0))
        w = float(crop_payload.get("width", 1.0))
        h = float(crop_payload.get("height", 1.0))
        crop_w = _round_even(w * input_width)
        crop_h = _round_even(h * input_height)
        crop_x = _round_offset(x * input_width)
        crop_y = _round_offset(y * input_height)
        vf_parts.append(f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y}")
    elif reframe_box is not None:
        box_w, box_h = _aspect_ratio_wh(reframe_box.target_aspect_ratio, input_width, input_height)
        cx_px = reframe_box.center_x * input_width
        cy_px = reframe_box.center_y * input_height
        crop_x = _clamp(cx_px - box_w / 2.0, 0.0, max(0.0, input_width - box_w))
        crop_y = _clamp(cy_px - box_h / 2.0, 0.0, max(0.0, input_height - box_h))
        vf_parts.append(f"crop={box_w}:{box_h}:{_round_offset(crop_x)}:{_round_offset(crop_y)}")

    if frame is not None and frame.zoom is not None and not math.isclose(frame.zoom, 1.0, abs_tol=1e-9):
        zoom = float(frame.zoom)
        position = frame.position or {}
        px = float(position.get("x", 0.0))
        py = float(position.get("y", 0.0))
        # Recorte central escalado de volta ao tamanho original, deslocado
        # por ``position`` (fração -1.0..1.0 do espaço de deslocamento
        # disponível) -- ver seção 0.3 para a fórmula documentada.
        vf_parts.append(
            f"crop=iw/{zoom:.6f}:ih/{zoom:.6f}:"
            f"(iw-ow)/2+({px:.6f})*(iw-ow)/2:"
            f"(ih-oh)/2+({py:.6f})*(ih-oh)/2,"
            f"scale=iw*{zoom:.6f}:ih*{zoom:.6f}"
        )

    if frame is not None and frame.resize is not None:
        resize = frame.resize
        width = int(resize.get("width", input_width))
        height = int(resize.get("height", input_height))
        fit_mode = frame.fit_mode or "FIT"
        if fit_mode not in _FIT_MODES:
            fit_mode = "FIT"
        vf_parts.append(_fit_scale_pad_filters(width, height, fit_mode))

    if frame is not None and frame.rotation:
        rotation_filter = _rotation_filter(int(frame.rotation))
        if rotation_filter is not None:
            vf_parts.append(rotation_filter)

    if adjustments is not None:
        eq_terms: "list[str]" = []
        if adjustments.brightness is not None:
            eq_terms.append(f"brightness={float(adjustments.brightness):.6f}")
        if adjustments.contrast is not None:
            eq_terms.append(f"contrast={float(adjustments.contrast):.6f}")
        if adjustments.saturation is not None:
            eq_terms.append(f"saturation={float(adjustments.saturation):.6f}")
        if adjustments.gamma is not None:
            eq_terms.append(f"gamma={float(adjustments.gamma):.6f}")
        if eq_terms:
            vf_parts.append("eq=" + ":".join(eq_terms))
        if adjustments.sharpen is not None and adjustments.sharpen > 0.0:
            vf_parts.append(
                f"unsharp=luma_msize_x=5:luma_msize_y=5:luma_amount={float(adjustments.sharpen):.6f}"
            )
        if adjustments.noise is not None and adjustments.noise > 0.0:
            level = _clamp(float(adjustments.noise) * 100.0, 0.0, 100.0)
            vf_parts.append(f"noise=alls={level:.6f}:allf=t")

    if not vf_parts:
        return None

    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-vf", ",".join(vf_parts),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(_INTERMEDIATE_CRF),
        "-c:a", "copy",
        output_path,
    ]


# ---------------------------------------------------------------------
# Etapa 3 -- AUDIO_SETTINGS
# ---------------------------------------------------------------------

def build_audio_command(
    input_path: str,
    output_path: str,
    *,
    audio: "AudioSettingsState | None",
    duration_seconds: "float | None",
) -> "list[str] | None":
    """``None`` quando ``audio`` é ``AudioSettingsState()`` (nenhum
    campo definido) -- etapa pulada por completo."""
    if audio is None or audio == AudioSettingsState():
        return None

    af_parts: "list[str]" = []

    if audio.gain is not None:
        af_parts.append(f"volume={float(audio.gain):.6f}dB")

    if audio.mute is True:
        af_parts.append("volume=0")
    elif audio.volume is not None:
        af_parts.append(f"volume={float(audio.volume):.6f}")

    if audio.normalization_enabled is True:
        target_lufs = audio.normalization_target_lufs
        if target_lufs is None:
            target_lufs = -14.0
        af_parts.append(f"loudnorm=I={float(target_lufs):.6f}:TP=-1.5:LRA=11")

    if audio.noise_reduction_enabled is True:
        intensity = audio.noise_reduction_intensity if audio.noise_reduction_intensity is not None else 0.0
        nr = _clamp(float(intensity) * 97.0, 0.01, 97.0)
        af_parts.append(f"afftdn=nr={nr:.6f}:nf=-25")

    if audio.fade_in_seconds is not None and audio.fade_in_seconds > 0.0:
        af_parts.append(f"afade=t=in:st=0:d={float(audio.fade_in_seconds):.6f}")

    if audio.fade_out_seconds is not None and audio.fade_out_seconds > 0.0 and duration_seconds is not None:
        start = max(0.0, duration_seconds - float(audio.fade_out_seconds))
        af_parts.append(f"afade=t=out:st={start:.6f}:d={float(audio.fade_out_seconds):.6f}")

    if audio.clipping_protection is True:
        af_parts.append("alimiter=limit=1.0")

    if not af_parts:
        return None

    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-af", ",".join(af_parts),
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        output_path,
    ]


# ---------------------------------------------------------------------
# Etapa 4 -- TEMPLATE (composição)
# ---------------------------------------------------------------------

def build_template_compose_command(
    input_path: str,
    output_path: str,
    *,
    background_path: "str | None",
    canvas_width: "int | None",
    canvas_height: "int | None",
    video_box_px: "tuple[int, int, int, int] | None",
) -> "list[str] | None":
    """``video_box_px``: ``(x, y, width, height)`` em pixels -- a caixa
    da zona ``VIDEO`` dentro do canvas do template. ``None`` quando
    nenhum template está selecionado -- etapa pulada por completo."""
    if background_path is None or canvas_width is None or canvas_height is None or video_box_px is None:
        return None

    box_x, box_y, box_w, box_h = video_box_px
    box_w = _round_even(box_w)
    box_h = _round_even(box_h)

    # "cover": preenche a caixa inteira, cortando o excedente -- ver
    # seção 0.3 para a justificativa da escolha.
    filter_complex = (
        f"[1:v]{_fit_scale_pad_filters(box_w, box_h, 'FILL')}[vidbox];"
        f"[0:v][vidbox]overlay={box_x}:{box_y}:shortest=1[outv]"
    )

    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-i", background_path,
        "-i", input_path,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "1:a?",
        "-shortest",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(_INTERMEDIATE_CRF),
        "-c:a", "aac", "-b:a", "192k",
        output_path,
    ]


# ---------------------------------------------------------------------
# Etapa 5 -- CAPTIONS (queima)
# ---------------------------------------------------------------------

def _escape_subtitles_path(path: str) -> str:
    """Escape do caminho para o filtro ``subtitles`` do FFmpeg -- ``:``
    e ``\\`` precisam ser escapados (sensível em Windows, item 11 do
    GATE -- caminhos com letra de unidade, ex. ``C:\\...``, contêm ``:``)."""
    escaped = path.replace("\\", "\\\\").replace(":", "\\:")
    return escaped


def _force_style_from(style: "CaptionsStyleState | None") -> "str | None":
    if style is None:
        return None
    parts: "list[str]" = []
    if style.font:
        parts.append(f"FontName={style.font}")
    if style.size is not None:
        parts.append(f"FontSize={style.size:.0f}")
    if style.alignment and style.alignment in _ALIGNMENT_TO_ASS:
        parts.append(f"Alignment={_ALIGNMENT_TO_ASS[style.alignment]}")
    if style.stroke and style.stroke.get("enabled"):
        parts.append(f"Outline={float(style.stroke.get('width', 2.0)):.2f}")
    if style.shadow and style.shadow.get("enabled"):
        parts.append("Shadow=1")
    else:
        parts.append("Shadow=0")
    if style.position and isinstance(style.position, Mapping):
        y = style.position.get("y")
        if y is not None:
            # Aproximação: margem vertical proporcional -- ver seção
            # 0.3 (limitação documentada, não um mapeamento exato).
            margin_v = int(_clamp((1.0 - float(y)) * 40.0, 0.0, 200.0))
            parts.append(f"MarginV={margin_v}")
    if not parts:
        return None
    return ",".join(parts)


def build_captions_burn_command(
    input_path: str,
    output_path: str,
    *,
    captions_path: "str | None",
    style: "CaptionsStyleState | None",
) -> "list[str] | None":
    """``None`` quando não há legenda a queimar (``captions_path`` é
    ``None``)."""
    if captions_path is None:
        return None

    escaped = _escape_subtitles_path(captions_path)
    force_style = _force_style_from(style)
    subtitles_filter = f"subtitles='{escaped}'"
    if force_style:
        subtitles_filter += f":force_style='{force_style}'"

    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-vf", subtitles_filter,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(_INTERMEDIATE_CRF),
        "-c:a", "copy",
        output_path,
    ]


# ---------------------------------------------------------------------
# Etapa 6 -- encode final (sempre executa) + METADATA_MODE
# ---------------------------------------------------------------------

def build_final_encode_command(
    input_path: str,
    output_path: str,
    *,
    strip_metadata: bool,
) -> "list[str]":
    """Nunca devolve ``None`` -- esta etapa SEMPRE executa (ver seção
    1). ``strip_metadata=True`` quando ``METADATA_MODE.mode == CLEAN``
    (ver seção 0.3 -- dobrado aqui, nunca via ``metadata_manager.py``)."""
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-c:v", "libx264", "-preset", "medium", "-crf", str(_FINAL_CRF),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
    ]
    if strip_metadata:
        cmd.extend(_METADATA_STRIP_ARGS)
    cmd.append(output_path)
    return cmd


# ---------------------------------------------------------------------
# RenderEngine
# ---------------------------------------------------------------------

class RenderEngine:
    """Job handler de renderização -- ver docstring do módulo para o
    contrato completo (Parte A: resolução de decisões; Parte B: pipeline
    FFmpeg real + validação + publicação)."""

    OPERATION: "ClassVar[str]" = OPERATION_RENDER_VIDEO

    def __init__(
        self,
        manager: EditProjectManager,
        *,
        database: LocalDatabase,
        storage_manager: StorageManager,
        app_paths: AppPaths,
        template_engine: TemplateEngine,
        audit_log: "OperationalAuditLog | None" = None,
        ffmpeg_backend: "FFmpegBackend | None" = None,
        media_probe: "MediaProbe | None" = None,
        control_manager: "ControlManager | None" = None,
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
        if not isinstance(template_engine, TemplateEngine):
            raise TypeError(
                f"template_engine deve ser TemplateEngine (recebido: {type(template_engine)!r})"
            )
        if ffmpeg_backend is not None and not callable(ffmpeg_backend):
            raise TypeError("ffmpeg_backend deve ser chamável")
        if media_probe is not None and not isinstance(media_probe, MediaProbe):
            raise TypeError(f"media_probe deve ser MediaProbe (recebido: {type(media_probe)!r})")
        if control_manager is not None and not isinstance(control_manager, ControlManager):
            raise TypeError(
                f"control_manager deve ser ControlManager (recebido: {type(control_manager)!r})"
            )

        self._manager = manager
        self._database = database
        self._storage = storage_manager
        self._app_paths = app_paths
        self._template_engine = template_engine
        self._audit_log = audit_log if audit_log is not None else OperationalAuditLog(database)
        self._backend: FFmpegBackend = ffmpeg_backend or _default_ffmpeg_backend
        self._probe = media_probe if media_probe is not None else MediaProbe()
        self._control_manager = control_manager

        self._template_selector = TemplateSelector(manager, template_engine)
        self._timeline = None  # construído sob demanda (precisa de database)
        self._visual = None
        self._audio_engine = AudioEngine(manager)
        self._captions_style_engine = CaptionsStyleEngine(manager)
        self._captions_engine = CaptionsEngine(
            manager, database=database, storage_manager=storage_manager,
            app_paths=app_paths, audit_log=self._audit_log, control_manager=control_manager,
        )
        self._reframe_engine = AutoReframeEngine(
            manager, database=database, storage_manager=storage_manager,
            app_paths=app_paths, audit_log=self._audit_log, control_manager=control_manager,
            media_probe=self._probe,
        )

    # -- resolução de dependências construídas sob demanda ---------------

    @property
    def _timeline_editor(self):
        if self._timeline is None:
            from .timeline_editor import TimelineEditor
            self._timeline = TimelineEditor(self._database)
        return self._timeline

    @property
    def _visual_editor(self):
        if self._visual is None:
            self._visual = VisualEditor(self._database)
        return self._visual

    # -- cancelamento --------------------------------------------------

    def _cancel_requested(self, job_id: str) -> bool:
        if self._control_manager is None:
            return False
        return self._control_manager.is_job_cancel_requested(job_id)

    # -- cache/Artifact lookup (por project_id -- ver seção 0.8) --------

    def _find_cached_artifact(self, project_id: str, cache_key: str) -> "Artifact | None":
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT id FROM artifacts WHERE project_id = ? AND kind = ? AND fingerprint = ? "
                "ORDER BY created_at LIMIT 1",
                (project_id, ARTIFACT_KIND_RENDER_OUTPUT, cache_key),
            ).fetchone()
        if row is None:
            return None
        return self._database.get(Artifact, row[0])

    def _find_latest_artifact(self, video_id: str, kind: str) -> "Artifact | None":
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT id FROM artifacts WHERE video_id = ? AND kind = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (video_id, kind),
            ).fetchone()
        if row is None:
            return None
        return self._database.get(Artifact, row[0])

    def _find_reframe_artifact(self, video_id: str, cache_key: str) -> "Artifact | None":
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT id FROM artifacts WHERE video_id = ? AND kind = ? AND fingerprint = ? "
                "ORDER BY created_at LIMIT 1",
                (video_id, ARTIFACT_KIND_REFRAME_TRACK, cache_key),
            ).fetchone()
        if row is None:
            return None
        return self._database.get(Artifact, row[0])

    def _final_path_for(self, project_id: str, cache_key: str) -> Path:
        return Path(self._app_paths.projects) / "renders" / project_id / f"{cache_key}.mp4"

    # ===================================================================
    # Job handler
    # ===================================================================

    def handle_render_job(self, job: Job) -> JobStepResult:
        if job.video_id is None:
            return JobStepResult(target_status=JOB_FAILED, data={"reason": "job_missing_video_id"})
        if job.project_id is None:
            return JobStepResult(target_status=JOB_FAILED, data={"reason": "job_missing_project_id"})

        project_id = job.project_id
        video_id = job.video_id

        video = self._database.get(Video, video_id)
        if video is None or video.source_asset_id is None:
            return JobStepResult(
                target_status=JOB_FAILED,
                data={"reason": "video_or_source_asset_id_missing", "video_id": video_id},
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

        try:
            aggregate_fingerprint = self._manager.aggregate_fingerprint(project_id)
        except Exception:
            return JobStepResult(
                target_status=JOB_FAILED, data={"reason": "project_not_found", "project_id": project_id}
            )

        # -- resolução das decisões opcionais (captions/template) só o
        # suficiente para compor o cache_key ANTES de decidir cache-hit
        # (ver seção 0.7 -- lacunas fechadas).
        captions_mode_state = self._captions_engine.get_mode(project_id)
        captions_artifact = None
        if captions_mode_state.mode == MODE_BURNED:
            captions_artifact = self._find_latest_artifact(video_id, ARTIFACT_KIND_SRT)

        template_id = self._template_selector.get_template_selection(project_id)
        template_entity: "Template | None" = None
        if template_id is not None:
            template_entity = self._template_engine.get_template(template_id)

        cache_key = compute_cache_key(
            source.fingerprint,
            aggregate_fingerprint,
            captions_artifact_fingerprint=(
                captions_artifact.fingerprint if captions_artifact is not None else None
            ),
            template_content_fingerprint=(
                compute_template_content_fingerprint(template_entity) if template_entity is not None else None
            ),
        )

        if self._cancel_requested(job.id):
            return JobStepResult(target_status=JOB_CANCELLED, data={"reason": "cancelled_before_render"})

        cached = self._find_cached_artifact(project_id, cache_key)
        if cached is not None:
            self._audit_log.record_checkpoint(
                job.id, CHECKPOINT_RENDERED, data={"cache_hit": True, "cache_key": cache_key}
            )
            return JobStepResult(
                target_status=JOB_READY,
                semantic_event=AUDIT_JOB_PROCESSING_COMPLETED,
                data={"cache_hit": True, "cache_key": cache_key, "reused_artifact_id": cached.id},
            )

        # -- resolução completa das demais decisões ---------------------
        local_path = str(source.local_path)
        probe_initial = self._probe.probe(local_path)
        if not probe_initial.valid or not probe_initial.width or not probe_initial.height:
            return JobStepResult(
                target_status=JOB_FAILED,
                data={"reason": "source_probe_invalid", "error_code": probe_initial.error_code},
            )

        segments = self._timeline_editor.get_timeline(project_id)
        segment_tuples = tuple((s.start, s.end, s.speed) for s in segments)

        frame_state = self._visual_editor.get_frame(project_id)
        adjustments_state = self._visual_editor.get_adjustments(project_id)
        audio_state = self._audio_engine.get_audio_settings(project_id)

        reframe_box = None
        if frame_state.crop is None:
            reframe_config = self._reframe_engine.get_config(project_id)
            if reframe_config.enabled is True:
                target_aspect_ratio = reframe_config.target_aspect_ratio or ASPECT_RATIO_9_16
                smoothing_strength = (
                    reframe_config.smoothing_strength if reframe_config.smoothing_strength is not None else 0.0
                )
                fallback_strategy = reframe_config.fallback_strategy or FALLBACK_CENTER_CROP
                reframe_cache_key = _reframe_compute_cache_key(
                    source.fingerprint, target_aspect_ratio, smoothing_strength, fallback_strategy
                )
                reframe_artifact = self._find_reframe_artifact(video_id, reframe_cache_key)
                if reframe_artifact is not None and os.path.isfile(reframe_artifact.path):
                    try:
                        payload = json.loads(Path(reframe_artifact.path).read_text(encoding="utf-8"))
                        keyframes = payload.get("keyframes") or []
                        if keyframes:
                            avg_x = sum(k["x"] for k in keyframes) / len(keyframes)
                            avg_y = sum(k["y"] for k in keyframes) / len(keyframes)
                            reframe_box = ReframeBox(
                                center_x=avg_x, center_y=avg_y, target_aspect_ratio=target_aspect_ratio
                            )
                    except (OSError, ValueError, KeyError, json.JSONDecodeError):
                        reframe_box = None

        captions_style_state = None
        captions_requested_but_unavailable = False
        if captions_mode_state.mode == MODE_BURNED:
            if captions_artifact is not None and os.path.isfile(captions_artifact.path):
                captions_style_state = self._captions_style_engine.get_style(project_id)
            else:
                captions_requested_but_unavailable = True

        # Projeto já confirmado existente (aggregate_fingerprint acima),
        # e METADATA_MODE é um nome de categoria válido -- get_category
        # não lança aqui; ausência de estado é representada por None.
        metadata_state = self._manager.get_category(project_id, METADATA_MODE)
        strip_metadata = bool(
            metadata_state is not None
            and isinstance(metadata_state.data, Mapping)
            and metadata_state.data.get("mode") == METADATA_MODE_CLEAN
        )

        # ``TEXT_LAYERS`` -- lida, sem produtor ainda (ver seção 0.3).
        self._manager.get_category(project_id, TEXT_LAYERS)

        template_background_path = None
        canvas_width = canvas_height = None
        video_box_px = None
        if template_entity is not None:
            template_background_path = template_entity.source_path
            extra = template_entity.extra if isinstance(template_entity.extra, Mapping) else {}
            canvas_width = extra.get("width")
            canvas_height = extra.get("height")
            if canvas_width and canvas_height:
                video_zone = next(
                    (z for z in template_entity.layout.get("zones", []) if z.get("zone_type") == ZONE_TYPE_VIDEO),
                    None,
                )
                if video_zone is not None:
                    video_box_px = (
                        _round_offset(float(video_zone["x"]) * canvas_width),
                        _round_offset(float(video_zone["y"]) * canvas_height),
                        _round_even(float(video_zone["width"]) * canvas_width),
                        _round_even(float(video_zone["height"]) * canvas_height),
                    )

        if self._cancel_requested(job.id):
            return JobStepResult(target_status=JOB_CANCELLED, data={"reason": "cancelled_before_pipeline"})

        # -- execução do pipeline (etapas 1-6, ver seção 1) --------------
        temp_files: "list[Path]" = []
        try:
            current_path = local_path
            is_original = True

            trim_cmd = build_trim_speed_command(current_path, "__placeholder__", segment_tuples)
            if trim_cmd is not None:
                out = self._storage.allocate_temp(suffix="_render_trim.mp4", create=True)
                trim_cmd[trim_cmd.index("__placeholder__")] = str(out)
                self._backend(trim_cmd)
                temp_files.append(out)
                current_path = str(out)
                is_original = False

            crop_cmd = build_crop_reframe_command(
                current_path, "__placeholder__",
                input_width=probe_initial.width, input_height=probe_initial.height,
                frame=frame_state, adjustments=adjustments_state, reframe_box=reframe_box,
            )
            if crop_cmd is not None:
                out = self._storage.allocate_temp(suffix="_render_crop.mp4", create=True)
                crop_cmd[crop_cmd.index("__placeholder__")] = str(out)
                self._backend(crop_cmd)
                temp_files.append(out)
                current_path = str(out)
                is_original = False

            duration_for_audio = probe_initial.duration
            if not is_original:
                requick = self._probe.probe(current_path)
                if requick.valid and requick.duration:
                    duration_for_audio = requick.duration

            audio_cmd = build_audio_command(
                current_path, "__placeholder__", audio=audio_state, duration_seconds=duration_for_audio
            )
            if audio_cmd is not None:
                out = self._storage.allocate_temp(suffix="_render_audio.mp4", create=True)
                audio_cmd[audio_cmd.index("__placeholder__")] = str(out)
                self._backend(audio_cmd)
                temp_files.append(out)
                current_path = str(out)
                is_original = False

            if self._cancel_requested(job.id):
                return JobStepResult(target_status=JOB_CANCELLED, data={"reason": "cancelled_mid_pipeline"})

            template_cmd = build_template_compose_command(
                current_path, "__placeholder__",
                background_path=template_background_path,
                canvas_width=canvas_width, canvas_height=canvas_height,
                video_box_px=video_box_px,
            )
            if template_cmd is not None:
                out = self._storage.allocate_temp(suffix="_render_template.mp4", create=True)
                template_cmd[template_cmd.index("__placeholder__")] = str(out)
                self._backend(template_cmd)
                temp_files.append(out)
                current_path = str(out)
                is_original = False

            captions_path = (
                captions_artifact.path
                if (captions_style_state is not None and captions_artifact is not None)
                else None
            )
            captions_cmd = build_captions_burn_command(
                current_path, "__placeholder__", captions_path=captions_path, style=captions_style_state
            )
            if captions_cmd is not None:
                out = self._storage.allocate_temp(suffix="_render_captions.mp4", create=True)
                captions_cmd[captions_cmd.index("__placeholder__")] = str(out)
                self._backend(captions_cmd)
                temp_files.append(out)
                current_path = str(out)
                is_original = False

            final_cmd = build_final_encode_command(
                current_path, "__placeholder__", strip_metadata=strip_metadata
            )
            final_out = self._storage.allocate_temp(suffix="_render_final.mp4", create=True)
            final_cmd[final_cmd.index("__placeholder__")] = str(final_out)
            self._backend(final_cmd)
            temp_files.append(final_out)
        except FFmpegBackendError as exc:
            for t in temp_files:
                t.unlink(missing_ok=True)
            return JobStepResult(
                target_status=JOB_FAILED, data={"reason": "ffmpeg_backend_failed", "error_code": exc.code}
            )
        except BaseException:
            for t in temp_files:
                t.unlink(missing_ok=True)
            raise

        # -- validação pós-render obrigatória (ver seção 0.5) -----------
        validation = self._probe.probe_deep(final_out)
        if not validation.valid or not validation.duration or validation.duration <= 0 or not validation.width or not validation.height:
            for t in temp_files:
                t.unlink(missing_ok=True)
            return JobStepResult(
                target_status=JOB_FAILED,
                data={"reason": "post_render_validation_failed", "error_code": validation.error_code},
            )

        # -- publicação atômica + Artifact + checkpoint ------------------
        final_path = self._final_path_for(project_id, cache_key)
        try:
            promoted = self._storage.promote_to_final(final_out, final_path, overwrite=True)
        except BaseException:
            for t in temp_files:
                t.unlink(missing_ok=True)
            raise
        finally:
            for t in temp_files:
                if t != final_out:
                    t.unlink(missing_ok=True)

        artifact = Artifact(
            video_id=video_id,
            project_id=project_id,
            job_id=job.id,
            kind=ARTIFACT_KIND_RENDER_OUTPUT,
            path=str(promoted),
            fingerprint=cache_key,
            size_bytes=validation.file_size,
        )
        self._database.insert(artifact)

        self._audit_log.record_checkpoint(
            job.id, CHECKPOINT_RENDERED, data={"cache_hit": False, "cache_key": cache_key}
        )

        result_data = {
            "cache_hit": False,
            "cache_key": cache_key,
            "artifact_id": artifact.id,
        }
        if captions_requested_but_unavailable:
            result_data["captions_requested_but_unavailable"] = True

        return JobStepResult(
            target_status=JOB_READY,
            semantic_event=AUDIT_JOB_PROCESSING_COMPLETED,
            data=result_data,
        )

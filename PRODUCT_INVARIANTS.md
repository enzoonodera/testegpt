# PRODUCT INVARIANTS

> Documento normativo do produto.
>
> Estes invariantes definem propriedades que nenhuma alteração futura pode quebrar.
> Eles valem para novas features, correções, refatorações, otimizações, migrations,
> atualizações, mudanças de UI e integrações externas.
>
> **Escopo desta etapa:** documentação somente. Este arquivo não afirma que o protótipo
> atual já satisfaz todos os invariantes. Lacunas atuais continuam documentadas em
> `RISCOS_ATUAIS.md`, `ARQUITETURA_ATUAL.md`, `MAPA_DE_DADOS.md` e
> `REGRESSION_CHECKLIST.md`.

## 1. Regra de precedência

1. Um comportamento que já funciona deve ser preservado, exceto quando conflitar com um
   invariante de segurança, privacidade, integridade de dados ou confirmação remota.
2. Uma feature nova não pode exigir a quebra de um invariante como atalho de implementação.
3. Se uma implementação não consegue respeitar um invariante, ela deve ser redesenhada antes
   de ser integrada.
4. Alterações devem ser incrementais, compatíveis, testáveis e reversíveis quando aplicável.
5. Nenhuma etapa futura deve ser considerada concluída sem verificar os invariantes afetados.

---

# INV-001 — Funções independentes

## Regra permanente

Cada ferramenta do produto deve continuar utilizável de forma independente das demais,
quando tecnicamente aplicável.

Isso inclui, entre outras:

- editar vídeo;
- cortar vídeo;
- Smart Clip;
- transcrever;
- gerar legenda;
- aplicar template;
- KEEP/CLEAN/PROFILE de metadata;
- melhorar áudio;
- reenquadrar para 9:16;
- gerar textos com IA;
- processar em lote;
- agendar;
- publicar;
- automatizar pasta;
- importar por URL.

## Deve permanecer verdadeiro

- Aplicar template não pode exigir passar pelo editor.
- Gerar legenda não pode exigir Smart Clip.
- Smart Clip não pode exigir publicação.
- Publicação não pode exigir geração de texto por IA se o usuário já fornecer o texto.
- Metadata não pode exigir transcrição.
- Um vídeo já pronto pode entrar diretamente em qualquer etapa compatível.
- O modo automático pode combinar ferramentas, mas não pode tornar esse encadeamento
  obrigatório para o uso individual delas.
- Estado e resultado de uma ferramenta devem ser consumíveis por outra por contratos claros,
  sem dependência oculta de ordem de execução.

## Regressão proibida

Criar um pipeline monolítico no qual o usuário seja obrigado a executar etapas não relacionadas
para acessar uma função específica.

---

# INV-002 — Operação em lote e alto volume

## Regra permanente

Toda função compatível com lote deve manter a mesma semântica com:

- 1 vídeo;
- 10 vídeos;
- 200 vídeos;
- 500 ou mais vídeos.

A quantidade de itens pode afetar tempo e concorrência, mas não pode mudar o significado da
operação nem exigir um fluxo diferente do usuário.

## Deve permanecer verdadeiro

- Alto volume não significa executar todos os itens simultaneamente.
- A fila não pode depender de todos os vídeos caberem ao mesmo tempo em RAM ou VRAM.
- CPU, RAM, GPU, VRAM, disco e navegadores devem ser tratados como recursos limitados.
- O Job Engine e o Resource Manager devem limitar concorrência de forma centralizada quando
  forem implementados.
- Cada item deve possuir estado individual persistível.
- Falha de um item não deve encerrar toda a fila, salvo quando continuar criaria risco de
  corrupção, duplicação, violação de segurança ou estado remoto incerto.
- A ordem da fila, quando relevante, deve ser estável e recuperável.

## Regressão proibida

Implementar recursos que funcionem apenas em lotes pequenos, dependam de simultaneidade
ilimitada ou percam rastreabilidade individual dos itens.

---

# INV-003 — Não destrutividade

## Regra permanente

O vídeo original do usuário nunca deve ser modificado pelo fluxo normal de processamento.

## Deve permanecer verdadeiro

- O original permanece byte a byte intacto enquanto estiver sob controle do produto.
- Decisões de edição ficam em projeto/estado separado do arquivo original.
- Renderização/exportação gera um novo arquivo.
- Alterar legenda não exige repetir transcrição ainda válida.
- Alterar template não exige repetir análise ainda válida.
- Alterar texto não exige repetir etapas anteriores ainda válidas.
- Artefatos intermediários reutilizáveis devem ser versionados/invalidados por dependências
  reais, e não por simples repetição do fluxo.
- Exclusão do original somente pode ocorrer como ação explícita e separada do usuário, nunca
  como efeito implícito de editar/renderizar.

## Regressão proibida

Sobrescrever o arquivo original ou usar o original como arquivo temporário de renderização.

---

# INV-004 — Persistência e recuperação após crash

## Regra permanente

Nenhuma operação importante pode existir apenas em memória.

Queda de energia, encerramento forçado, crash do processo, reinicialização do Windows ou falha
isolada de um worker não pode apagar toda a fila nem fazer o sistema esquecer operações já
confirmadas.

## Deve permanecer verdadeiro

- Jobs importantes possuem estado persistente.
- Checkpoints são gravados em fronteiras seguras de operação.
- O sistema consegue distinguir item ainda não iniciado, em andamento, concluído, falhado,
  cancelado, aguardando usuário e resultado remoto incerto.
- Após reinício, o sistema reconstrói a fila a partir do armazenamento persistente.
- Escritas críticas devem ser atômicas/transacionais conforme o armazenamento utilizado.
- Estado corrompido não pode ser silenciosamente interpretado como fila vazia.
- Falha na leitura do estado deve gerar diagnóstico e recuperação controlada.
- Uma operação remota cuja conclusão não possa ser comprovada deve voltar como estado incerto,
  nunca como “não executada”.

## Regressão proibida

Recomeçar toda a fila após crash ou reconstruir estado crítico apenas por inferência frágil de
nomes de arquivos.

---

# INV-005 — Idempotência e operações repetíveis com segurança

## Regra permanente

Reexecutar uma operação após retry, resume ou crash não pode produzir efeitos duplicados quando
o efeito anterior já foi confirmado.

## Deve permanecer verdadeiro

- Jobs devem possuir identificadores estáveis.
- Itens devem possuir identidade estável independente apenas do nome físico do arquivo.
- Operações locais concluídas devem ser reconhecidas como concluídas antes de serem repetidas.
- Retries automáticos só podem ocorrer quando o efeito anterior for comprovadamente inexistente
  ou quando a operação for idempotente por contrato.
- Publicação/agendamento remoto exige chave/identidade local suficiente para reconciliação.
- `UNKNOWN` nunca pode ser convertido automaticamente em retry de publicação.
- Duplicidade deve ser impedida por estado e reconciliação, não por temporizadores ou pelo fato
  de um botão ter sido clicado.

## Regressão proibida

Retry cego de upload, publicação ou agendamento após timeout, crash, perda de conexão ou dúvida
sobre o resultado remoto.

---

# INV-006 — Pause, resume, stop_after_current e cancel

## Regra permanente

O Job Engine deve suportar controles operacionais persistentes:

- `pause`;
- `resume`;
- `stop_after_current`;
- `cancel`;
- crash recovery.

## Semântica mínima

### pause

- Não inicia novos trabalhos após atingir um ponto seguro.
- Não deve corromper o item atual.
- O estado de pausa sobrevive a reinício quando aplicável.

### resume

- Retoma a partir do último checkpoint válido.
- Não repete automaticamente efeitos já confirmados.

### stop_after_current

- Permite concluir o item corrente em um ponto seguro.
- Impede iniciar o próximo item.
- Deve ser distinguível de cancelamento imediato.

### cancel

- Cancela itens que ainda não iniciaram.
- Para item em execução, respeita fronteiras seguras e semântica específica da ferramenta.
- Não pode fingir que uma operação remota foi desfeita quando não houver confirmação disso.

## Regressão proibida

Implementar “parar” como encerramento bruto do processo sem checkpoint ou tratar `cancel`,
`pause` e `stop_after_current` como o mesmo estado.

---

# INV-007 — Privacidade e segurança

## Regra permanente

Dados sensíveis e conteúdo do usuário permanecem locais salvo quando o próprio objetivo da
operação exigir envio ao serviço escolhido pelo usuário.

## Nunca enviar ao backend próprio

- cookies;
- perfis completos de navegador;
- tokens;
- credenciais;
- vídeos;
- transcrições;
- conteúdo pessoal;
- logs brutos.

## Deve permanecer verdadeiro

- Logs externos são sanitizados por allowlist, não por tentativa genérica de “remover segredos”.
- Perfis de navegador são classificados como segredos locais.
- CAPTCHA, challenge e 2FA geram `USER_ACTION_REQUIRED`.
- O software não tenta contornar CAPTCHA, challenge, 2FA ou mecanismos de segurança.
- O software não desabilita antivírus.
- O software não cria exclusões automáticas no Windows Defender.
- Segredos não devem aparecer em logs, telemetria, relatórios de crash ou mensagens de erro
  enviadas ao backend próprio.
- Vídeos só saem da máquina quando uma ação explícita de publicação/upload para a plataforma
  selecionada exigir isso.

## Regressão proibida

Adicionar telemetria, suporte remoto, diagnóstico ou conveniência de login que copie dados
sensíveis para infraestrutura própria.

---

# INV-008 — Separação estrita entre local e cloud

## Regra permanente

O banco operacional do produto é local. O backend online tem responsabilidades separadas e não
é a fonte primária do estado operacional de conteúdo.

## Local — SQLite

O SQLite local pode armazenar:

- vídeos;
- projetos;
- jobs;
- filas;
- schedules;
- publicações;
- templates;
- histórico;
- erros;
- configurações operacionais.

## Cloud — backend online

O backend online pode cuidar de:

- licenças;
- dispositivos;
- planos;
- feature flags;
- releases;
- updates;
- configurações remotas;
- telemetria autorizada e sanitizada.

## Deve permanecer verdadeiro

- Perder conexão com o backend não pode apagar ou tornar ilegível o estado local do usuário.
- O backend não recebe cópia integral do SQLite operacional.
- Cloud não armazena cookies, vídeos, transcrições ou perfis de navegador como atalho de produto.
- Feature flags não podem transformar uma operação local concluída em estado inválido.
- Estado operacional local e estado comercial/licenciamento remoto possuem contratos separados.

## Regressão proibida

Transformar o backend em banco de conteúdo do usuário ou sincronizar indiscriminadamente a base
local para a nuvem.

---

# INV-009 — Confirmação e reconciliação de publicação

## Regra permanente

Upload, publicação e agendamento nunca são considerados concluídos apenas porque um botão foi
clicado ou uma automação chegou ao final sem exceção.

## Estados mínimos conceituais

Uma operação remota deve poder representar pelo menos:

- pendente;
- em execução;
- confirmada;
- falhou antes do efeito remoto;
- `UNKNOWN`/resultado remoto incerto;
- `USER_ACTION_REQUIRED` quando aplicável.

## Deve permanecer verdadeiro

- Sucesso exige evidência positiva ou reconciliação posterior.
- Fechamento de modal, mudança de página ou clique no botão não bastam isoladamente.
- Timeout após ação remota não equivale a falha segura.
- Crash entre ação remota e save local deve ser reconciliado antes de qualquer retry.
- `UNKNOWN` nunca gera repost automático.
- Reconciliação não pode depender apenas de título quando títulos podem se repetir.
- Identidade deve combinar sinais suficientes para evitar confirmar o item errado.
- Falha de confirmação deve preservar o máximo possível de contexto para reconciliação humana ou
  automática posterior.

## Regressão proibida

Registrar “publicado/agendado” por clique e também registrar “não publicado” automaticamente
quando o sistema não conseguiu observar a confirmação.

---

# INV-010 — Templates independentes do editor

## Regra permanente

Template é uma ferramenta independente. O produto não será um Canva completo.

## Deve permanecer verdadeiro

O produto inicia com 3 templates oficiais prontos e pode permitir importação de PNG/JPG com
configuração visual de:

- área do vídeo;
- área da legenda;
- área do texto IA;
- áreas de imagem/logo quando suportadas.

Um template pode ser aplicado:

- antes da edição;
- depois da edição;
- em vídeo já pronto;
- em lote;
- depois do Smart Clip.

## Requisitos de independência

- Template não deve depender da timeline do editor para existir.
- Projeto de template deve possuir seu próprio estado/versionamento.
- Trocar template não deve invalidar transcrição ou análise sem relação com layout.
- Aplicação em lote deve usar o mesmo contrato da aplicação em um único vídeo.

## Regressão proibida

Acoplar templates ao editor de forma que um vídeo pronto precise ser “reeditado” apenas para
receber um layout.

---

# INV-011 — Smart Clip local e semanticamente honesto

## Regra permanente

O Smart Clip principal funciona localmente e não depende de YouTube Analytics nem de API paga.

## Sinais principais permitidos

- transcrição;
- áudio;
- frames;
- contexto;
- IA local.

## Sinais adicionais

Se a origem corresponder automaticamente a uma conta conectada e já existirem sinais adicionais
legitimamente disponíveis, eles podem melhorar a análise internamente.

## Deve permanecer verdadeiro

- O usuário não precisa configurar Analytics/API para usar Smart Clip.
- O software não pergunta “Esse vídeo é seu?” como requisito operacional.
- A ausência de dados de Analytics não desabilita o Smart Clip principal.
- Estimativa local não é apresentada como dado observado de audiência.
- A UI usa termos como “corte recomendado”, “alto potencial” ou equivalentes honestos.
- Processamento local é preferido sempre que possível.

## Regressão proibida

Chamar uma estimativa de IA de “trecho mais assistido” ou tornar Analytics/API externa uma
dependência obrigatória da função principal.

---

# INV-012 — Compatibilidade de dados antigos

## Regra permanente

Atualizações futuras não podem invalidar silenciosamente projetos, filas, históricos, contas,
configurações ou estados criados por versões anteriores suportadas.

## Deve permanecer verdadeiro

- Schemas persistidos possuem versão explícita quando evoluem.
- Mudanças incompatíveis usam migrations determinísticas e testadas.
- Migration não deve depender de o usuário lembrar como configurou a versão antiga.
- Dados desconhecidos não devem ser descartados silenciosamente.
- Antes de migration destrutiva deve existir backup recuperável.
- Migration deve ser idempotente ou possuir marcador transacional que permita recuperação.
- Falha de migration deve impedir abertura destrutiva da base e oferecer rollback/recuperação.
- Mudanças no algoritmo de fingerprint, cache ou identidade de item exigem estratégia de
  compatibilidade/migration; não podem ser trocadas silenciosamente.
- Importações de formatos legados suportados devem permanecer cobertas por testes enquanto esses
  formatos estiverem dentro da política de suporte.

## Regressão proibida

Tratar dado antigo como vazio/default só porque o parser novo não reconheceu o formato.

---

# INV-013 — Update seguro

## Regra permanente

O mecanismo de atualização deve ser desenhado para falhar com segurança e permitir recuperação.

## Canais obrigatórios

- DEV;
- BETA;
- STABLE.

## Requisitos obrigatórios

Toda arquitetura de update deve contemplar:

- backup pré-update;
- pacote assinado;
- verificação de assinatura;
- hash de integridade;
- migrations testadas;
- rollback;
- rollout progressivo;
- kill switch;
- separação por canal;
- versionamento explícito;
- registro do resultado da atualização.

## Operação crítica

Update nunca pode começar durante uma operação crítica.

Operações críticas incluem, no mínimo, janelas em que interromper o processo possa:

- corromper estado;
- deixar arquivo parcialmente produzido sem checkpoint seguro;
- perder resultado remoto;
- causar publicação duplicada;
- interromper migration;
- deixar credencial/perfil em estado inconsistente.

## Deve permanecer verdadeiro

- Update falho mantém versão anterior utilizável ou permite rollback seguro.
- Migration e binário devem ser compatíveis como conjunto.
- Um kill switch pode impedir rollout de release problemática sem exigir distribuir outro binário.
- Stable não recebe build não promovida/testada pelo fluxo definido.

## Regressão proibida

Atualização in-place sem backup/verificação, execução de update no meio de publicação ou release
que torne dados antigos ilegíveis sem migration e rollback.

---

# INV-014 — Importação desacoplada e sem bypass de proteção

## Regra permanente

Importação por URL é uma capacidade independente do restante do produto.

## Deve permanecer verdadeiro

- Importar não obriga editar, transcrever, aplicar template ou publicar.
- O resultado importado entra no mesmo contrato de mídia das demais origens quando compatível.
- Não implementar quebra de DRM, paywall ou mecanismos de proteção.
- O produto pode estabelecer nas condições gerais que o usuário deve possuir permissão para
  processar o conteúdo, sem interromper cada vídeo com perguntas de propriedade.

## Regressão proibida

Acoplar downloader a publicação automática obrigatória ou implementar técnicas de contorno de
proteções de acesso.

---

# INV-015 — Metadata com semântica explícita

## Regra permanente

Metadata deve ser tratada como escolha funcional transparente, não como mecanismo para enganar
sistemas de plataforma.

## Modos obrigatórios

- `KEEP`: manter metadata original quando tecnicamente possível;
- `CLEAN`: remover metadata não necessária;
- `PROFILE`: aplicar um perfil configurado e explicitamente selecionado.

## Deve permanecer verdadeiro

- A interface não promete “enganar algoritmo”, “parecer outro aparelho” ou equivalentes.
- Quando uma decisão for necessária e não houver preferência salva, o usuário recebe uma escolha
  simples e pode optar por lembrar a preferência.
- Mudança de metadata é separável de mudanças visuais, áudio, velocidade ou edição.
- Uma função chamada CLEAN não deve silenciosamente alterar conteúdo audiovisual.

## Regressão proibida

Misturar limpeza de metadata com transformações ocultas de vídeo/áudio ou gerar metadata falsa
com objetivo declarado de disfarçar origem perante plataforma.

---

# INV-016 — Abstração técnica e experiência comercial

## Regra permanente

O usuário escolhe o resultado desejado; o software escolhe a tecnologia adequada.

## Deve permanecer verdadeiro

O fluxo normal não exige que o usuário saiba o que são:

- Ollama;
- Whisper;
- FFmpeg;
- API;
- Google Analytics;
- modelo de IA;
- codec.

Detalhes técnicos podem existir em diagnóstico/avançado quando forem úteis, mas não devem ser
pré-requisito para o uso comum.

O frontend final deve parecer software desktop comercial internacional, com modo automático por
padrão e opções avançadas somente quando necessárias.

## Regressão proibida

Transformar a interface principal em painel de desenvolvedor, expor escolhas técnicas sem
necessidade ou exigir conhecimento de infraestrutura para tarefas comuns.

---

# INV-017 — Scheduler local versus remoto

## Regra permanente

O produto deve distinguir de forma explícita tarefas que já foram entregues e aceitas pela
plataforma remota de tarefas que ainda dependem da máquina local para acontecer.

Os nomes internos podem evoluir, mas a semântica deve preservar, no mínimo, a distinção entre:

- `REMOTE_SCHEDULED`: a plataforma remota já recebeu e confirmou o agendamento;
- `LOCAL_PENDING`: a execução ainda depende do computador, do aplicativo ou de recursos locais.

## Deve permanecer verdadeiro

- `REMOTE_SCHEDULED` significa que desligar o computador não cancela por si só a publicação já
  agendada na plataforma.
- `LOCAL_PENDING` significa que a tarefa ainda precisa do PC/software em execução no momento
  necessário; a interface não pode prometer execução com o computador desligado.
- O usuário deve conseguir entender, sem conhecer detalhes técnicos, se uma publicação depende
  ou não da máquina local.
- Reinicialização do Windows ou do aplicativo deve preservar corretamente os dois estados.
- Um item não pode ser promovido de `LOCAL_PENDING` para `REMOTE_SCHEDULED` apenas porque a
  tentativa de agendamento foi iniciada ou um botão foi clicado.
- A transição para `REMOTE_SCHEDULED` exige confirmação/reconciliação positiva da plataforma.
- Se houver dúvida sobre se o agendamento remoto foi aceito, o estado deve refletir incerteza
  (`UNKNOWN` ou equivalente) até reconciliação; nunca deve voltar automaticamente para
  `LOCAL_PENDING` de modo que possa ocorrer agendamento duplicado.
- Cancelamento local de um registro `REMOTE_SCHEDULED` não pode fingir que cancelou o agendamento
  remoto; deve existir confirmação da plataforma para considerar o cancelamento concluído.
- Mudanças de relógio, timezone, horário de verão ou reinicialização não podem transformar uma
  tarefa local em remota nem alterar silenciosamente a semântica do agendamento.
- Todo agendamento deve preservar a intenção local (`scheduled_local`), um timezone IANA explícito
  (`timezone_iana`) e o instante UTC correspondente (`scheduled_utc`) quando houver data/hora definida.
- Ausência de timezone nunca pode ser convertida silenciosamente em UTC. Defaults só podem existir para
  mercados/presets oficialmente mapeados; `UTC` é válido apenas quando explicitamente configurado.
- A origem da escolha do horário deve ser explícita e extensível; os modos canônicos iniciais são
  `MANUAL` e `RECOMMENDED`. `RECOMMENDED` não implica, por si só, que exista IA/recomendador implementado.
- O instante UTC persistido, e não o timezone atual do Windows, é a referência para comparação e
  continuidade de filas através de reinício ou mudança de timezone da máquina.

## Regressão proibida

Misturar tarefas remotas já confirmadas com tarefas ainda dependentes do PC, prometer execução
offline para `LOCAL_PENDING`, ou reenviar automaticamente um agendamento remoto cujo resultado
ficou incerto.

---

# INV-018 — Segurança de recursos e armazenamento

## Regra permanente

Nenhum lote pode iniciar concorrência ilimitada. CPU, RAM, GPU/VRAM, navegadores e disco são
recursos globais finitos e devem ser coordenados centralmente.

## Deve permanecer verdadeiro

- O Job Engine não pode criar workers sem limite com base apenas no tamanho da fila.
- CPU, RAM, GPU, VRAM, navegadores e capacidade de I/O de disco devem participar das decisões de
  concorrência do Resource Manager.
- Tarefas pesadas concorrentes devem respeitar orçamento global de recursos, inclusive quando
  pertencem a ferramentas ou filas diferentes.
- Antes de iniciar tarefas grandes, o sistema deve verificar espaço livre suficiente para os
  arquivos temporários, intermediários e de saída esperados, com margem de segurança.
- Cache e arquivos temporários devem possuir política de limpeza, retenção e limite de uso de
  espaço.
- Limpeza automática nunca pode remover originais do usuário nem artefatos ainda necessários
  para projeto, recovery, publicação pendente ou rollback.
- Disco cheio ou falha de escrita deve interromper a operação em ponto seguro e produzir erro
  recuperável; não pode transformar arquivo parcial em saída válida.
- Escritas críticas devem evitar corrupção por meio de estratégia apropriada, como arquivo
  temporário + commit/rename atômico ou transação equivalente.
- Falta de espaço não pode corromper projeto, banco local ou vídeo original.
- O software deve conseguir diferenciar arquivo completo, temporário, parcial e abandonado para
  que crash recovery não trate lixo de processamento como resultado concluído.
- O sistema deve aplicar backpressure: quando recursos atingirem limites seguros, novos trabalhos
  aguardam em vez de disputar recursos indefinidamente.

## Regressão proibida

Disparar um processo por item sem limite global, iniciar renderizações grandes sem considerar
espaço livre, permitir crescimento indefinido de cache/temp ou aceitar como válida uma saída
parcial produzida após disco cheio.

---

# INV-019 — Instalador comercial autocontido (sem dependência de Python/pip/pytest no cliente)

## Regra permanente

O cliente final que instala o software comercial **não pode precisar** de Python, pip, pytest,
`tzdata` ou qualquer outra ferramenta/dependência de desenvolvimento pré-instalada em sua máquina.
Python/pip/pytest/venv são ferramentas de **desenvolvimento e teste interno** deste repositório —
nunca um requisito do produto entregue ao usuário.

O instalador comercial final deve embutir todo o runtime necessário (interpretador, bibliotecas,
dados de timezone quando aplicável, e demais dependências de execução) de forma autocontida, e
deve ser validado rodando em uma máquina Windows limpa, sem Python pré-instalado.

## Deve permanecer verdadeiro

- Nenhuma tela, fluxo ou mensagem do produto final pode instruir o usuário a instalar Python,
  pip, `pytest` ou pacotes via linha de comando.
- O empacotamento/instalador comercial deve embutir o runtime Python (ou equivalente compilado)
  necessário para rodar o software, sem depender de nada pré-existente no sistema do usuário.
- Dados de timezone IANA (equivalente a `tzdata`) necessários em runtime devem estar embutidos no
  pacote final, nunca instalados via `pip` no momento do primeiro uso.
- Scripts de desenvolvimento como `INSTALAR_DEPENDENCIAS_TESTE.bat`, `RODAR_TESTES.bat` e
  qualquer ambiente virtual de teste (ex.: `.venv-test`) são ferramentas internas do repositório
  de desenvolvimento — nunca podem ser copiados para o instalador comercial nem para a pasta de
  instalação do produto no computador do cliente.
- Antes de qualquer release, validar a instalação/execução do produto final em um Windows limpo
  (sem Python, pip ou qualquer ferramenta de desenvolvimento pré-instalada) como critério de
  bloqueio de release.

## Regressão proibida

Fazer o instalador comercial, o primeiro uso do produto, ou qualquer fluxo do usuário final
depender de um Python de sistema, de `pip install`, de `pytest` ou de instalação manual de
`tzdata`; distribuir `.venv-test` ou scripts de desenvolvimento dentro do instalador/pacote
comercial.

---

# 19. Contratos de estado que futuras implementações devem preservar

Os nomes concretos podem mudar durante a evolução da arquitetura, mas a semântica não pode ser
perdida.

## Jobs locais

Devem distinguir, de forma equivalente:

- `PENDING`;
- `RUNNING`;
- `PAUSED`;
- `COMPLETED`;
- `FAILED`;
- `CANCELLED`;
- `USER_ACTION_REQUIRED`;
- estado de interrupção segura/stop-after-current quando necessário.

## Efeitos remotos

Devem distinguir, de forma equivalente:

- não enviado;
- envio iniciado;
- confirmado remotamente;
- falhou antes de efeito remoto;
- `UNKNOWN` quando o resultado não puder ser determinado;
- `USER_ACTION_REQUIRED` quando a plataforma exigir intervenção humana.

Um estado local genérico `FAILED` não pode substituir `UNKNOWN` quando existe possibilidade real
de o efeito remoto ter ocorrido.

---

# 20. Regras de invalidação de cache e artefatos

Caches e resultados intermediários devem ser reutilizados quando válidos, porém nunca de forma
cega.

## Deve permanecer verdadeiro

- Cada artefato reutilizável deve ter dependências identificáveis.
- Mudança que não afeta um artefato não deve invalidá-lo.
- Mudança que altera sua semântica deve invalidá-lo ou migrá-lo.
- Versão de pipeline/modelo/parâmetros relevantes deve participar da validade quando puder mudar
  o resultado materialmente.
- Cache corrompido deve gerar recomputação segura ou erro explícito, não dado silenciosamente
  incorreto.

Exemplo: trocar apenas o template não deve obrigar nova transcrição; mudar parâmetros que alteram
a própria transcrição pode exigir nova transcrição.

---

# 21. Regras mínimas para mudanças futuras

Antes de aceitar uma alteração que toque qualquer área deste documento:

1. identificar quais invariantes são afetados;
2. demonstrar que o comportamento anterior compatível permanece funcionando;
3. executar testes de sintaxe/imports/testes existentes;
4. adicionar ou atualizar testes automatizados para o novo contrato quando houver infraestrutura
   para isso;
5. testar recuperação em falha quando houver persistência ou efeito remoto;
6. testar compatibilidade com dados de versão anterior quando houver schema persistido;
7. confirmar que nenhum segredo novo entra em logs/telemetria;
8. confirmar que nenhuma publicação `UNKNOWN` é repetida automaticamente;
9. registrar migrations, riscos e dívida técnica;
10. atualizar documentação arquitetural quando o comportamento real mudar.

---

# 22. Critérios de bloqueio de release

Uma release não deve ser promovida se houver regressão conhecida que:

- altere ou apague originais;
- perca fila após crash;
- possa duplicar publicação por retry cego;
- marque publicação como concluída sem confirmação/reconciliação;
- envie segredo/conteúdo ao backend próprio fora do contrato de privacidade;
- torne dados antigos ilegíveis sem migration segura;
- execute update durante operação crítica;
- remova independência de ferramenta sem justificativa arquitetural compatível;
- quebre lotes grandes por desenho de simultaneidade/memória;
- torne Smart Clip dependente de Analytics/API paga;
- acople templates obrigatoriamente ao editor;
- contorne CAPTCHA, challenge, 2FA, DRM, paywall ou proteção equivalente;
- desabilite antivírus ou crie exclusões automáticas no Windows Defender;
- confunda `REMOTE_SCHEDULED` com `LOCAL_PENDING` ou prometa execução local com o computador
  desligado;
- permita concorrência ilimitada, ignore espaço livre crítico ou trate saída parcial por disco
  cheio como concluída;
- exija Python, pip, pytest ou instalação manual de `tzdata` no computador do cliente final, ou
  não tenha sido validada rodando em um Windows limpo sem Python pré-instalado (ver INV-019).

---

# 23. Relação com o protótipo atual

Este documento é normativo para a evolução do produto, mas **não reclassifica automaticamente o
protótipo atual como conforme**.

Pontos já conhecidos que precisam de correção em etapas futuras continuam registrados em
`RISCOS_ATUAIS.md`. Entre eles existem comportamentos atuais que não atendem ainda a todos os
invariantes, como confirmação insuficiente de publicação, ausência do Job Engine persistente
com todos os estados definidos, persistência baseada principalmente em JSON e comportamentos
legados relacionados a metadata/Defender.

Esses pontos não foram alterados nesta etapa.

---

# 24. Invariante do próprio documento

`PRODUCT_INVARIANTS.md` deve ser tratado como contrato de produto.

Uma alteração futura neste arquivo não deve ser usada para “legalizar” uma regressão já
implementada. Mudanças nos invariantes precisam ser deliberadas, justificadas e aprovadas antes
de qualquer implementação que dependa delas.

# TESTE MANUAL NO WINDOWS — GATE 19.5 — ESTÁGIO 2

Este roteiro é para ser executado por alguém **sem conhecimento de Python**, no
computador Windows real onde o produto roda no dia a dia. Siga as etapas em
ordem. Cada etapa diz se é **SEGURO / NÃO PUBLICA** ou se **PODE PUBLICAR —
EXIGE CONTA DE TESTE E CONFIRMAÇÃO**.

Antes de começar: use uma conta de teste (YouTube e TikTok), nunca uma conta
real de cliente, para qualquer etapa que possa chegar perto de publicar.

---

## A. Abrir o painel — SEGURO / NÃO PUBLICA
Dê dois cliques em `PAINEL_OFICIAL.bat`. Confirme que o menu aparece sem
erro no terminal.

## B. Selecionar/criar canal — SEGURO / NÃO PUBLICA
No menu, crie uma conta de teste nova (ou selecione uma já existente marcada
como teste). Confirme que uma pasta nova aparece dentro da área de dados do
programa, com subpastas `videos`, `dados`, `logs`.

## C. Colocar vídeo — SEGURO / NÃO PUBLICA
Copie 1 vídeo curto de teste (MP4, alguns segundos) para a pasta `videos` da
conta criada no passo B.

## D. Limpeza/processamento — SEGURO / NÃO PUBLICA
No menu, escolha a opção de limpar/tratar vídeos. Confirme que:
- aparece uma barra de progresso/log no terminal;
- ao final, existe 1 arquivo novo (`001.mp4`) na pasta de saída;
- o vídeo original em `videos` continua existindo e abre normalmente.

Repita rodando a MESMA opção de novo sem adicionar vídeos novos — confirme que
a mensagem diz "nada novo para tratar" (não reprocessa).

## E. Gerar título/descrição/hashtags — SEGURO / NÃO PUBLICA
Com Ollama rodando localmente (o programa tenta iniciar sozinho se não
estiver), escolha a opção de gerar textos. Confirme que aparece um resultado
com título, descrição e hashtags no idioma configurado.

## F. Validar idioma — SEGURO / NÃO PUBLICA
Confira que o texto gerado está realmente no idioma/mercado configurado para
esta conta (ex.: se a conta está configurada como `pt-BR`/Brasil, o texto
deve estar em português do Brasil, não em inglês).

## G. Fechar e reabrir — SEGURO / NÃO PUBLICA
Feche completamente o `PAINEL_OFICIAL.bat` (feche a janela do terminal).
Abra de novo.

## H. Confirmar persistência — SEGURO / NÃO PUBLICA
Volte para a mesma conta. Confirme que:
- o vídeo tratado no passo D continua marcado como concluído (não pede para
  tratar de novo);
- o texto gerado no passo E continua salvo e aparece do mesmo jeito.

Anote explicitamente o que persistiu e o que não persistiu — se algo que
deveria persistir sumiu, isso é um bug a reportar, não algo para "consertar
na hora".

## I. Preparar/agendar YouTube — PODE PUBLICAR — EXIGE CONTA DE TESTE E CONFIRMAÇÃO

> **Correção em relação à versão anterior deste roteiro:** a versão anterior
> mandava você "parar antes do clique final" manualmente. Isso estava errado
> — o fluxo normal (`schedule_one()`) clica automaticamente, não dá para
> confiar em reflexo humano para interromper a automação a tempo. Agora o
> próprio programa tem essa checagem embutida: antes de clicar no botão
> final, ele relê o horário/data que ficaram exibidos na tela e SÓ prossegue
> se baterem exatamente com o horário calculado. Se divergirem, o programa
> aborta sozinho, sem publicar nada, e mostra um erro no terminal.

> **Novidade desta rodada (pergunta de direitos autorais a cada execução):**
> assim que você escolhe agendar YouTube, ANTES de qualquer outra coisa
> (antes até de abrir o Chrome), o terminal pergunta:
> ```
> Ignorar avisos de direitos autorais NESTA execução? (não fica salvo, s/N):
> ```
> Essa pergunta aparece **toda vez** que você roda o agendamento, mesmo se
> você já respondeu antes na mesma sessão — não existe "lembrar a resposta".
> Responder `s`/`sim` faz o programa ignorar avisos de direitos autorais de
> terceiros só nesta execução; qualquer outra resposta (incluindo apertar
> Enter sem digitar nada) faz o programa bloquear esses avisos, também só
> nesta execução. **A resposta nunca é salva no arquivo de configuração da
> conta** — confira abrindo `config_canal.json` da conta antes e depois de
> rodar: ele não deve ganhar nenhuma linha nova por causa dessa pergunta,
> em nenhuma das duas respostas. Além disso, independente da resposta, um
> vídeo com problema real de verificação (não só aviso de direitos
> autorais — inclui, por exemplo, uma falha de rede durante a checagem)
> agora é sempre pulado nesta execução, e a fila continua com o próximo
> vídeo, em vez de parar tudo.

Faça login com a conta de teste do YouTube (opção de login no menu). Rode a
opção de agendar YouTube com um horário futuro suficiente para cancelar
depois, caso ele confirme.

- **Se o programa completar o agendamento normalmente:** ele já validou
  sozinho que o horário exibido bateu com o calculado antes de confirmar.
  Verifique no YouTube Studio (aba Conteúdo) que o horário programado é
  mesmo o esperado, e cancele/exclua o agendamento de teste depois.
- **Se o programa abortar com uma mensagem começando com "Abortado ANTES do
  clique final":** isto significa que o programa detectou uma divergência
  real de horário/data na UI do YouTube Studio e corretamente **não
  publicou nada**. Isto NÃO é o comportamento antigo (Candidato C) — é a
  proteção nova funcionando. Tire um print da tela e da mensagem de erro e
  reporte mesmo assim, porque ainda vale a pena entender por que a UI
  divergiu (pode ser um formato de data/hora novo que o parser não
  reconhece, por exemplo).
- **Se o programa travar/travesse de outro jeito** (erro diferente, sem
  mensagem clara de horário): isto é um bug novo a reportar, não relacionado
  ao Candidato C.

> **Novidade desta rodada (política de direitos autorais do YouTube):** o
> terminal agora mostra, no início do processamento, duas linhas com a
> política ativa, por exemplo:
> ```
> Política desta execução (não salva): direitos autorais = BLOCK, itens
> bloqueados = SKIP_AND_CONTINUE
> ...
> Política YouTube (valores usados nesta execução do lote):
> Avisos de direitos autorais: BLOCK
> Problemas impeditivos: SKIP_AND_CONTINUE
> ```
> (o primeiro valor vem direto da resposta que você deu à pergunta acima;
> "itens bloqueados" agora é sempre `SKIP_AND_CONTINUE` neste fluxo,
> independente do que estiver configurado no arquivo da conta)
> Se o YouTube sinalizar reivindicação de terceiros (sem restrição de país)
> e a política `youtube_copyright_warning_policy` estiver em `BLOCK`
> (padrão), o programa NÃO agenda esse vídeo. Se estiver em `ALLOW`, o
> programa agenda automaticamente e mostra `COPYRIGHT WARNING` / `POLICY =
> ALLOW` / `ACTION = CONTINUE`. Se o YouTube sinalizar "restrito/bloqueado
> em alguns países", o programa NUNCA agenda automaticamente, mesmo com
> `ALLOW` configurado — isso é tratado como bloqueio real, não aviso.
>
> Por padrão (sem configurar nada), o comportamento ao encontrar um vídeo
> bloqueado continua o mesmo de antes desta rodada: o vídeo é movido para
> uma pasta de quarentena (`bloqueados\direitos_autorais`) e a fila continua
> normalmente com o próximo vídeo. Se você configurar
> `youtube_blocked_item_policy=STOP_BATCH`, o comportamento muda: o
> programa para a fila inteira em vez de pular o vídeo.

## J. Preparar/agendar TikTok — PODE PUBLICAR — EXIGE CONTA DE TESTE E CONFIRMAÇÃO

> **Novidade desta rodada (pergunta de direitos autorais a cada execução):**
> igual ao descrito no passo I para o YouTube — a mesma pergunta ("Ignorar
> avisos de direitos autorais NESTA execução? (não fica salvo, s/N): ")
> aparece toda vez que você roda o agendamento do TikTok, antes de abrir o
> Chrome, nunca é lembrada entre execuções, e nunca é salva em
> `config_canal.json`. Confirme isso abrindo o arquivo da conta antes e
> depois de rodar, com as duas respostas possíveis. Um vídeo com problema
> real de verificação (não só aviso de direitos autorais) agora é sempre
> pulado nesta execução, com a fila continuando no próximo vídeo.

Mesma lógica do passo I, agora para o TikTok: o programa relê data e hora do
seletor do TikTok Studio antes de clicar no botão final ("Programar") e
aborta sozinho, sem publicar, se divergirem do horário calculado (já
considerando o arredondamento de 5 em 5 minutos que o TikTok usa).

> **Novidade desta rodada (bug real Windows de 19/09/2026):** depois de
> confirmar data/hora, o programa agora também ESPERA o TikTok terminar as
> verificações de direitos autorais/conteúdo antes de clicar no botão final.
> Durante essa espera, o terminal mostra `Aguardando verificações do
> TikTok...`. Isso pode levar alguns minutos — não é travamento, é o
> programa esperando de propósito, exatamente o oposto do bug antigo (que
> podia clicar cedo demais). O programa NUNCA clica em "Publicar agora" no
> modal "Continuar publicando?" que o TikTok mostra quando a verificação
> ainda está incompleta; se esse modal aparecer, o programa clica
> "Cancelar" sozinho, espera a verificação de novo, e tenta o clique final
> mais uma vez (só isso — nunca mais que 1 retry).

- **Se completar normalmente:** confira no TikTok Studio que o agendamento
  ficou no horário certo, e cancele/exclua depois.
- **Se abortar com "Abortado ANTES do clique final: a data/hora exibida..."**
  a proteção de horário (Candidato C) funcionou — nada foi publicado. Tire
  print e reporte a causa da divergência.
- **Se abortar com "Abortado ANTES do clique final: as verificações do
  TikTok... não concluíram"** ou menção a "aviso (WARNING)" / "problema
  (FAILED)": a proteção de verificação de direitos autorais/conteúdo desta
  rodada funcionou — o programa preferiu não publicar em vez de arriscar.
  Isso é esperado se o vídeo de teste realmente demorar/tiver algum alerta;
  reporte de qualquer forma, com o tempo que levou, para calibrarmos o
  timeout se necessário.
- **Se o modal "Continuar publicando?" aparecer na tela e o programa NÃO
  clicar em nada automaticamente (ficar parado nele):** isto é um bug a
  reportar — a expectativa é que o programa detecte e cancele esse modal
  sozinho, nunca fique parado esperando um clique manual nele.

> **Novidade desta rodada (política de aviso de direitos autorais):** se a
> verificação de direitos autorais terminar como "aviso" (o vídeo ainda
> pode ser publicado, mas fica silenciado) e todas as outras verificações
> já tiverem terminado, o comportamento depende da configuração da conta:
> - `tiktok_copyright_warning_policy=BLOCK` (padrão, se a chave não existir
>   na config): o programa aborta ANTES do clique final, sem agendar, e
>   mostra uma mensagem mencionando "aviso" de direitos autorais — isto é
>   esperado, não um bug.
> - `tiktok_copyright_warning_policy=ALLOW`: o programa agenda
>   automaticamente mesmo com esse aviso, e mostra no terminal
>   `COPYRIGHT WARNING` / `POLICY = ALLOW` / `ACTION = CONTINUE`. Isto só
>   deve acontecer se você mesmo configurou `ALLOW` de propósito.
>
> Em qualquer um dos dois casos, se OUTRA verificação (ex.: conteúdo) ainda
> estiver em andamento, o programa continua esperando — um aviso de
> direitos autorais isolado nunca decide nada enquanto outra verificação
> não tiver terminado.

> **Novidade desta rodada (correção de detecção de itens, GATE 19.5 —
> continuação): se em rodadas anteriores este mesmo passo travava sempre em
> "Abortado ANTES do clique final: as verificações do TikTok... não
> concluíram" mesmo com as duas verificações aparecendo com sucesso/verde
> na tela do TikTok Studio, era um bug real corrigido nesta rodada — o
> programa contava elementos decorativos da tela (como uma divisória
> invisível sempre presente) como se fossem verificações desconhecidas, o
> que travava tudo. Repita agora o MESMO vídeo que travava antes:**
> - **Esperado:** a espera termina normalmente e o programa avança para o
>   clique final, sem nenhuma linha extra tipo "OTHER: DESCONHECIDO" nos
>   logs — só os dois checks reais (direitos autorais + conteúdo).
> - **Se ainda travar do mesmo jeito com as duas verificações visivelmente
>   ok na tela:** é um bug a reportar imediatamente, com print da tela de
>   "Verificações" do TikTok Studio no momento do travamento.

## K. Lote pequeno — PODE PUBLICAR — EXIGE CONTA DE TESTE E CONFIRMAÇÃO
Coloque 3 vídeos de teste na pasta de vídeos, gere texto para os 3, e rode o
agendamento em lote. Confirme:
- os 3 horários calculados seguem a ordem/janela configurada, sem repetir
  horário já usado;
- se você cancelar/fechar o Chrome no meio do 2º vídeo, o 1º continua
  marcado como agendado e o 3º não foi pulado nem duplicado ao rodar de novo.

> **Novidade desta rodada (controle de lote):** no início do lote, o
> terminal agora mostra duas linhas fixas confirmando a política ativa,
> por exemplo:
> ```
> Política TikTok (valores usados nesta execução do lote):
> Avisos de direitos autorais: BLOCK
> Problemas impeditivos: STOP_BATCH
> ```
> Com `tiktok_blocked_item_policy=STOP_BATCH` (padrão), um vídeo bloqueado
> continua parando o lote inteiro, como sempre. Se você configurar
> `tiktok_blocked_item_policy=SKIP_AND_CONTINUE`, um vídeo bloqueado (aviso/
> problema nas verificações) é pulado — nunca marcado como agendado — e o
> programa segue automaticamente para o próximo vídeo da fila, sem parar.
>
> **Atenção:** esses valores de `STOP_BATCH`/`BLOCK` só valem para quando o
> agendamento é chamado de forma programática (sem passar pela pergunta
> interativa do passo J). No uso normal pelo menu, a pergunta de direitos
> autorais do passo J sempre força `Problemas impeditivos =
> SKIP_AND_CONTINUE`, mesmo que o arquivo da conta tenha `STOP_BATCH`
> configurado.

## L. Ollama desligado — SEGURO / NÃO PUBLICA
Feche o Ollama (ou impeça-o de iniciar) e tente gerar texto para um vídeo
novo. Confirme que o programa mostra um erro claro (não trava, não finge que
gerou um texto).

## M. Vídeo sem áudio — SEGURO / NÃO PUBLICA
Use um vídeo de teste sem faixa de áudio (ou com áudio mudo). Rode limpeza e
depois geração de texto. Confirme que o texto gerado vem da descrição visual
(baseada nos frames), não trava nem gera texto genérico sem sentido.

## N. Arquivo corrompido — SEGURO / NÃO PUBLICA
Pegue um arquivo `.mp4` de teste e corrompa-o deliberadamente (ex.: abra num
editor de texto e apague metade do conteúdo, salve). Coloque na pasta de
vídeos e rode a limpeza. Confirme que o programa pula esse arquivo com uma
mensagem de erro clara, sem travar o restante do lote e sem apagar o arquivo
original corrompido.

---

## Depois de rodar o roteiro

Anote, para cada letra A-N: passou / falhou / observação. Preste atenção
especial às letras **I** e **J** — são as que verificam a correção do
CANDIDATO C (divergência de horário) descrita em
`GATE_19_5_ESTAGIO2_RELATORIO.md`. Diferente da versão anterior deste
roteiro, o programa agora tem a proteção embutida (não depende mais de você
interromper manualmente) — o que este roteiro confirma é se essa proteção
funciona de verdade contra o YouTube Studio e o TikTok Studio reais, e se
algum agendamento chega a ser publicado com horário errado (o que não
deveria mais acontecer).

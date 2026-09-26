# Roteiro de teste manual — Windows real

Este roteiro não exige conhecimento de programação. Siga na ordem. Para
cada teste: faça a AÇÃO, confira o RESULTADO ESPERADO, e se algo diferente
acontecer, copie/envie o que está indicado em "SE DER ERRO".

Abra `PAINEL_OFICIAL.bat` antes de começar.

---

## TESTE A — Instalar/verificar dependências

AÇÃO: no menu, escolha a opção **7 - INSTALAR / VERIFICAR DEPENDÊNCIAS**.

RESULTADO ESPERADO: o processo roda sem travar e termina indicando que as
dependências (Playwright, faster-whisper, tzdata) estão instaladas/OK.

SE DER ERRO: copie toda a saída do terminal desde a linha onde você
escolheu a opção 7 até o final.

---

## TESTE B — Adicionar conta

AÇÃO: menu, opção **4 - ADICIONAR CONTA**. Escolha YouTube ou TikTok,
escolha o país/idioma da conta, faça login no Chrome que abrir.

RESULTADO ESPERADO: a conta aparece depois na opção 6 (VER CONTAS
CADASTRADAS), com o país/idioma corretos.

SE DER ERRO: copie a saída do terminal e diga em qual passo do login
travou (abriu o Chrome? pediu 2FA/captcha? fechou sozinho?).

---

## TESTE C — Limpar metadados / numerar vídeos

AÇÃO: coloque 2 ou 3 vídeos `.mp4` na pasta de vídeos da conta (a pasta
`videos\` dentro da conta — veja `LEIA_ME_PRIMEIRO.txt` para o caminho
exato). Rode a opção **1 - LIMPAR META DADOS OFICIAL**.

RESULTADO ESPERADO: os vídeos aparecem renomeados/numerados
(001.mp4, 002.mp4, ...) sem duplicar números já usados. Rodar a opção de
novo, sem adicionar vídeo novo, não deve reprocessar o que já foi feito.

SE DER ERRO: copie a saída do terminal e diga quantos vídeos você colocou
e com quais nomes originais.

---

## TESTE D — Gerar título/descrição/hashtags (texto com IA local)

AÇÃO: com os vídeos numerados do TESTE C, rode a opção **2 - GERAR /
EDITAR TEXTOS DAS POSTAGENS**.

RESULTADO ESPERADO: para cada vídeo, é gerado título (YouTube) ou caption
(TikTok) + hashtags, salvos incrementalmente — se você interromper no meio
(Ctrl+C) e rodar de novo, só o que falta é gerado, o que já foi feito não
é refeito.

SE DER ERRO: copie a saída do terminal. Se a mensagem mencionar Ollama,
confirme se o Ollama está instalado/rodando (`ollama --version` numa
janela cmd separada) e informe o resultado.

---

## TESTE E — Agendar / postar

AÇÃO: com títulos/descrições já gerados (TESTE D), rode a opção
**3 - POSTAR / AGENDAR**.

RESULTADO ESPERADO: o navegador abre, faz upload do vídeo, preenche
título/descrição/hashtags e agenda no horário calculado. Depois de
concluído, a opção 6 ou o estado da conta reflete o vídeo como agendado
(não fica "pendente" para sempre).

SE DER ERRO: copie a saída do terminal e, se possível, um print da tela
onde travou no navegador.

---

## TESTE F — Fechar e abrir de novo (persistência)

AÇÃO: depois do TESTE E (ou C/D), feche completamente `PAINEL_OFICIAL.bat`
(feche a janela). Abra de novo. Veja a opção 6 (VER CONTAS CADASTRADAS) e
rode a opção 1 de novo na mesma conta.

RESULTADO ESPERADO: nada do que já foi processado/postado é perdido ou
refeito do zero. A numeração continua de onde parou.

SE DER ERRO: descreva o que sumiu ou o que foi refeito indevidamente.

---

## TESTE G — Lote pequeno (vários vídeos de uma vez)

AÇÃO: repita os TESTES C e D com 5-10 vídeos de uma vez, em vez de 2-3.

RESULTADO ESPERADO: mesmo comportamento do TESTE C/D, só que para todos os
vídeos, um a um, sem travar nem misturar dados de um vídeo com outro.

SE DER ERRO: diga quantos vídeos tinha, qual falhou (número/nome) e copie
a saída do terminal.

---

## TESTE H — Cancelamento no meio do processo

AÇÃO: durante o TESTE D ou E (gerando texto ou postando), aperte Ctrl+C
no meio do processamento de um vídeo.

RESULTADO ESPERADO: o programa encerra sem corromper o vídeo original nem
deixar arquivo pela metade sendo tratado como se estivesse completo.
Rodar de novo deve continuar de onde parou, nunca reprocessar o que já
tinha sido confirmado.

SE DER ERRO: copie a saída do terminal de antes e depois do Ctrl+C.

---

## TESTE I — Serviço de IA local desligado (falha simulada)

AÇÃO: feche/pare o Ollama (ou não o inicie) e rode a opção 2 (GERAR /
EDITAR TEXTOS) mesmo assim.

RESULTADO ESPERADO: o programa avisa claramente que o Ollama não está
disponível (não trava sem explicação, não gera texto genérico fingindo
sucesso, não marca o vídeo como concluído).

SE DER ERRO: copie a mensagem exata mostrada.

---

## TESTE J — Vídeo problemático

AÇÃO: tente processar (TESTE C ou D) um arquivo que não é um vídeo válido
(por exemplo, renomeie um `.txt` para `.mp4`) ou um vídeo sem áudio.

RESULTADO ESPERADO: esse arquivo específico falha com mensagem clara, mas
os OUTROS vídeos do lote continuam sendo processados normalmente (uma
falha não derruba o lote inteiro).

SE DER ERRO: copie a saída do terminal completa desse lote.

---

Depois de rodar o roteiro, me envie: quais testes passaram como esperado,
quais tiveram resultado diferente, e as saídas de terminal pedidas em
"SE DER ERRO" para os que falharam.

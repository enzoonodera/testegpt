Você está trabalhando em um protótipo funcional que será transformado em um software desktop comercial para Windows, voltado para preparação, edição, automação e publicação de conteúdo curto.

Antes de modificar qualquer coisa:
1. Leia este CLAUDE.md.
2. Examine somente os arquivos, módulos, testes e documentação necessários para a etapa atual.
3. Entenda o comportamento atual da área que será modificada antes de alterar código.
4. Preserve tudo que já funciona.
5. Não reescreva o projeto inteiro sem necessidade.
6. Faça mudanças incrementais, compatíveis e testáveis.
7. Não avance para outra etapa sem autorização.
8. Não implemente antecipadamente funcionalidades de prompts futuros.
9. Evite ler novamente arquivos já compreendidos na mesma sessão sem necessidade.
10. Ignore artefatos irrelevantes como __pycache__, .pytest_cache, *.pyc, temporários, outputs e caches, salvo se forem necessários para diagnosticar um problema.
==================================================
GATE ADVERSARIAL OBRIGATÓRIO ANTES DE QUALQUER ENTREGA
==================================================

Antes de declarar qualquer Prompt concluído, faça uma segunda passagem
adversarial sobre a própria implementação.

Não apenas releia os testes pedidos no Prompt.
Tente quebrar a solução.

Para TODO novo componente/engine/coordinator, revisar obrigatoriamente:

1. STORAGE / SPLIT-BRAIN
- Todos os colaboradores apontam para o mesmo SQLite lógico?
- Uma dependência interna possui audit_log/storage próprio diferente?
- Duas facades diferentes do mesmo path funcionam?
- Bancos realmente diferentes são rejeitados antes de mutar objetos?

2. TRANSAÇÕES / TOCTOU
Procure padrões:
    read()
    decide()
    transaction/write()

Se a decisão precisa ser atômica, leitura + decisão + escrita devem estar
na mesma BEGIN IMMEDIATE.

Testar explicitamente duas instâncias concorrentes.

3. CRASH WINDOWS
Para cada operação:
    persistir intenção
    executar efeito
    persistir conclusão

perguntar o que acontece se o processo morrer:
- antes da intenção;
- depois da intenção;
- no meio dos efeitos;
- depois do efeito mas antes da conclusão.

Nunca confundir RuntimeError capturado com crash real.

4. RESTART
Reabrir usando NOVAS instâncias de:
- LocalDatabase;
- engine;
- managers/coordinators.

Não reutilizar objetos em memória para chamar isso de teste de restart.

5. IDEMPOTÊNCIA
Repetir a mesma operação:
- duas vezes sequencialmente;
- duas vezes simultaneamente;
- depois de crash.

O resultado histórico original não pode mudar silenciosamente.

6. CONCORRÊNCIA
Testar duas instâncias contra o mesmo SQLite.

Evitar testes probabilísticos quando uma Barrier/Event pode tornar a corrida
determinística.

7. AUTORIDADE ÚNICA
Verificar se não existem dois componentes diferentes tomando decisão sobre:
- Job.status;
- claim;
- cancel;
- retry;
- recovery;
- lifecycle;
- resource admission.

Não criar segunda fonte de verdade.

8. CONSTRUÇÃO DOS OBJETOS
Se __init__ pode rejeitar configuração:
- validar tudo ANTES de modificar dependências fornecidas pelo chamador.

Uma construção que falha não pode deixar objetos parcialmente mutados.

9. HISTÓRICO
Em operações retomáveis:
- reason original;
- request_id/shutdown_id/batch_id;
- timestamps;
- status histórico;

não podem ser substituídos por argumentos de uma chamada de retomada.

10. SEGREDOS
Nada persistente pode receber:
- str(exc);
- repr(exc);
- traceback;
- token;
- cookie;
- password;
- Authorization header;
- conteúdo arbitrário vindo de exceção.

11. WINDOWS
Se houver:
- arquivos abertos;
- locks;
- subprocessos;
- rename/delete;
- tempfile;

revisar semântica específica de Windows.

12. TESTE HONESTO
O nome do teste deve corresponder ao que ele realmente prova.

Exemplo:
"crash" precisa sair da execução antes da conclusão.
Uma Exception capturada pelo próprio código NÃO é crash.

13. FALHA DE HANDLER
Todo caminho no qual o handler lança exceção deve terminar em estado
persistente seguro ou recovery explícito.

Nunca deixar PROCESSING/PUBLISHING acidentalmente por causa de uma dependência
mal configurada.

14. DOIS CHAMADORES
Para toda operação pública relevante, perguntar:

"O que acontece se duas instâncias chamarem isto ao mesmo tempo?"

15. PROVA FINAL
Antes da entrega:
- rodar testes específicos;
- rodar suíte completa;
- compileall;
- revisar diff;
- confirmar arquivos modificados;
- confirmar migrations congeladas;
- fazer uma lista dos ataques adversariais adicionais executados.

Somente então declarar o Prompt concluído.


PRINCÍPIOS OBRIGATÓRIOS

A. MODULARIDADE

Cada ferramenta deve funcionar separadamente.

Exemplos:
- editar vídeo;
- cortar vídeo;
- Smart Clip;
- gerar legenda;
- aplicar template;
- limpar/manter metadata;
- melhorar áudio;
- reenquadrar para 9:16;
- gerar textos com IA;
- processar vídeos em lote;
- agendar;
- publicar;
- automatizar pasta.

Nenhuma dessas funções deve obrigar o usuário a passar pelas demais.

Ao mesmo tempo, todas devem poder ser combinadas no modo automático.

B. PROCESSAMENTO NÃO DESTRUTIVO

Nunca modificar o vídeo original.

Salvar decisões de edição em projeto/estado separado.

Só gerar um novo arquivo na renderização/exportação.

Trocar legenda, template ou texto não deve obrigar a repetir transcrição, análise ou etapas anteriores que ainda sejam válidas.

C. ALTO VOLUME

Toda ferramenta compatível com lote deve funcionar da mesma forma com:
1 vídeo,
10 vídeos,
200 vídeos,
500+ vídeos.

Isso NÃO significa processar todos simultaneamente.

Usar Job Engine + Resource Manager para respeitar CPU, RAM, GPU, VRAM, disco e limites de navegador.

D. PERSISTÊNCIA E RECUPERAÇÃO

Nenhuma operação importante pode existir somente em memória.

Persistir checkpoints.

Queda de energia, encerramento do processo, reinicialização do Windows ou crash não pode fazer o usuário perder toda a fila.

Implementar:
- pause;
- resume;
- stop_after_current;
- cancel;
- crash recovery.

Nunca repetir automaticamente uma publicação cujo resultado esteja incerto.

E. DADOS

SQLite é exclusivamente LOCAL.

Ele armazena:
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

O backend ONLINE é separado e cuida de:
- licenças;
- dispositivos;
- planos;
- feature flags;
- releases;
- updates;
- configurações remotas;
- telemetria autorizada.

F. PRIVACIDADE E SEGURANÇA

Nunca enviar para nosso servidor:
- cookies;
- perfis completos de navegador;
- tokens;
- credenciais;
- vídeos;
- transcrições;
- conteúdo pessoal;
- logs brutos.

Logs enviados externamente precisam ser sanitizados.

CAPTCHA, challenge e 2FA devem gerar USER_ACTION_REQUIRED.
Não tentar contornar mecanismos de segurança.

Não desabilitar antivírus.
Não adicionar exclusões automáticas ao Windows Defender.

G. CONTEÚDO E IA

Whisper, Ollama, FFmpeg e demais processamentos devem ser locais sempre que possível.

O usuário normal não precisa saber:
- o que é Ollama;
- o que é Whisper;
- o que é FFmpeg;
- o que é API;
- o que é Google Analytics;
- qual modelo está rodando;
- qual codec está sendo utilizado.

O usuário escolhe o resultado.
O software escolhe a tecnologia.

H. SMART CLIP

O Smart Clip principal NÃO deve depender de YouTube Analytics nem de API paga.

Ele deve funcionar localmente com:
- transcrição;
- áudio;
- frames;
- contexto;
- IA local.

Se a origem corresponder automaticamente a uma conta conectada e existirem sinais extras já disponíveis, eles podem melhorar a análise internamente.

Nunca perguntar:
“Esse vídeo é seu?”

Nunca exigir que o usuário configure Analytics/API.

Não apresentar uma estimativa de IA como “trecho mais assistido”.
Usar termos como “corte recomendado” ou “alto potencial”.

I. IMPORTAÇÃO

Importação por URL deve ser desacoplada do restante do programa.

Não implementar técnicas para quebrar DRM, paywalls ou mecanismos de proteção.

As condições gerais do software podem estabelecer que o usuário deve possuir permissão para processar o conteúdo, sem interromper cada vídeo perguntando sobre propriedade.

J. TEMPLATES

NÃO construir um Canva completo.

O programa terá inicialmente 3 templates oficiais prontos.

O usuário também poderá IMPORTAR PNG/JPG e configurar visualmente:
- área do vídeo;
- área da legenda;
- área do texto IA;
- áreas de imagem/logo quando suportadas.

Template é independente do editor.

Pode ser aplicado:
- antes da edição;
- depois da edição;
- em vídeo já pronto;
- em lote;
- depois de Smart Clip.

K. METADATA

Disponibilizar:
KEEP = manter metadata original.
CLEAN = limpar metadata.
PROFILE = aplicar perfil configurado.

Não apresentar metadata como mecanismo para enganar sistemas de plataforma.

Quando a preparação do vídeo exigir decisão e não houver preferência definida, apresentar escolha simples ao usuário e permitir lembrar a preferência.

L. FRONTEND

Frontend final deve parecer software comercial internacional.

Evitar aparência de:
- painel Python;
- ferramenta de desenvolvedor;
- sistema cheio de opções técnicas.

Automático por padrão.
Avançado somente quando necessário.

M. PUBLICAÇÃO

Nunca considerar upload/publicação/agendamento concluído apenas porque um botão foi clicado.

Sempre confirmar ou reconciliar.

UNKNOWN nunca pode gerar repost automático.

N. ATUALIZAÇÕES

Updates precisam:
- DEV;
- BETA;
- STABLE;
- backup pré-update;
- assinatura;
- hash;
- rollback;
- migrations testadas;
- rollout progressivo;
- kill switch.

Update nunca pode acontecer durante operação crítica.

O. ENTREGA DE CADA ETAPA

P. EFICIÊNCIA DE CONTEXTO

O roadmap completo não precisa ser carregado integralmente para cada tarefa.

Ao receber um Prompt:
- trabalhar somente na etapa solicitada;
- consultar documentação adicional apenas quando necessária;
- não explorar módulos sem relação com a alteração;
- não reler arquivos grandes sem necessidade;
- preferir testes específicos durante o desenvolvimento;
- executar a suíte completa ao final da etapa;
- usar saída concisa dos testes quando possível;
- investigar logs detalhados somente quando houver falha.

Não sacrificar correção, segurança, testes ou compatibilidade para economizar contexto.

## REVISÃO CRÍTICA E QUALIDADE COMERCIAL

Este projeto deve ser desenvolvido com padrão de software comercial
preparado para grande quantidade de usuários.

Não presuma que uma solicitação do proprietário está tecnicamente correta.

Antes de implementar uma decisão relevante, avalie:

- impacto arquitetural;
- risco de regressão;
- escalabilidade;
- segurança;
- privacidade;
- recuperação após falhas;
- compatibilidade futura;
- manutenção;
- experiência do usuário;
- custo operacional;
- possibilidade de perda ou corrupção de dados;
- duplicação de ações remotas;
- observabilidade e diagnóstico.

Se a solicitação apresentar risco relevante, conflito arquitetural,
solução inferior ou possibilidade de prejudicar etapas futuras:

1. não ignore o problema;
2. explique objetivamente o risco;
3. apresente uma alternativa melhor;
4. peça confirmação antes de uma mudança arquitetural relevante.

Não fazer overengineering sem benefício concreto.

Não adicionar abstrações, dependências ou complexidade apenas
"para o futuro" sem necessidade arquitetural demonstrável.

### CONFIABILIDADE

Não existe operação crítica baseada apenas em sucesso presumido.

Toda operação crítica deve, conforme aplicável, possuir:

- validação;
- persistência;
- tratamento de erro;
- timeout;
- retry controlado;
- idempotência;
- reconciliação;
- logging estruturado;
- recuperação;
- testes;
- rollback ou estratégia segura de falha.

Falhas devem ser isoladas sempre que possível.

Falha de:
- um vídeo não derruba o lote;
- uma conta não derruba outras contas;
- uma plataforma não derruba outras plataformas;
- uma feature não inutiliza todo o produto.

Nenhuma otimização deve sacrificar integridade de dados,
segurança ou previsibilidade do sistema.



Ao concluir cada prompt, informe:

1. arquivos criados;
2. arquivos modificados;
3. comportamento novo;
4. decisões arquiteturais;
5. migrations;
6. testes automatizados executados;
7. como testar manualmente;
8. riscos conhecidos;
9. dívida técnica criada;
10. pendências.

Execute testes de sintaxe/imports/testes existentes antes de considerar concluído.{\rtf1}
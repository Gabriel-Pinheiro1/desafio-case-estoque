# Estoque GoBeauté: plano de compras de 90 dias, estoque parado e nível de serviço

Solução para o **Problema 3 (Estoque)** do Desafio AI-First do Gogroup. É um app web (FastAPI + HTML/JS) que:

- limpa os dados e declara o que limpou;
- corrige a demanda nos dias de ruptura;
- monta o plano de compras dos próximos 90 dias dentro do orçamento, com MOQ, unidade de compra e pedidos em aberto;
- mostra o capital parado e o que liquidar;
- compara políticas de nível de serviço.

Os parâmetros são editáveis na tela e tudo é recalculado em menos de 1 segundo.

## Como rodar

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (Linux/Mac: source .venv/bin/activate)
pip install -r requirements-dev.txt
python app.py                   # abre em http://127.0.0.1:8767
pytest -q                       # 29 testes
```

O primeiro carregamento lê os CSVs (cerca de 3 s) e grava `dados/base_padrao.json.gz`. Os seguintes usam esse cache, validado pelo hash dos CSVs.

## O que a tela mostra

| Aba | Conteúdo |
| --- | --- |
| Visão executiva | Capital parado, receita perdida por ruptura, necessidade x orçamento, plano, achados com números e gráficos |
| Plano de compras | O que comprar, quanto (na unidade de compra, em múltiplos do MOQ), quando emitir, quando chega e o custo. Mostra o critério de corte, a curva de retorno do orçamento e o status de cada pedido (mantido, reduzido, cortado ou não econômico). Exporta CSV |
| Estoque parado | Descontinuados e excesso, ação por SKU, preço de liquidação dentro do desconto máximo e valor recuperável. Exporta CSV |
| Nível de serviço | Curva caixa x serviço, meta de 98% x por classe x econômico por SKU, fill rate projetado com o plano e lead time por fornecedor (distribuição completa) |
| Ruptura e demanda | Receita perdida, SKUs que mais perderam, média simples x demanda corrigida e índice de sazonalidade |
| Recomendações | Separadas em compra, encalhe e processo/política, com critério, dono e ganho estimado. São geradas a partir dos números atuais |
| Qualidade dos dados | O que foi limpo, quantas linhas e por quê, além das premissas |

**Parâmetros editáveis:**

- orçamento, custo de capital, armazenagem e desconto máximo de liquidação;
- início e duração do horizonte;
- política de nível de serviço (uniforme, por classe A/B/C ou econômica por SKU);
- campanhas (nome, datas e multiplicador; em branco usa o estimado no histórico);
- cobertura por pedido e até quando as compras devem cobrir a demanda;
- critério de corte (padrão ou "proteger a classe A");
- limites de excesso e de liquidação;
- premissas da liquidação.

**Upload:** é possível subir qualquer subconjunto dos 5 CSVs no mesmo formato; os que não forem enviados vêm de `dados/`. O navegador compacta cada arquivo em gzip antes de enviar: vendas e estoque passam de 4,6 MB para 0,6 e 0,85 MB, abaixo do limite de 4,5 MB da Vercel. O servidor também aceita `.csv.gz`, separador `;` e decimal `,`.

## Principais números (parâmetros de `parametros.md`, referência 29/09/2026)

| Pergunta | Resposta |
| --- | --- |
| Capital parado hoje | **R$ 2,14 mi** de R$ 6,78 mi em estoque (32%). São R$ 1,69 mi em 40 SKUs ativos com mais de 180 dias de cobertura e R$ 454 mil em 53 descontinuados. Carregar isso custa **R$ 695 mil/ano** (18% de capital + 14,4% de armazenagem). Um único SKU, Femme Protein 450G, tem R$ 533 mil parados (cobertura de 12.500 dias) |
| Venda perdida por ruptura | **R$ 796 mil** de receita em 12 meses (R$ 715 mil em ativos; 1,0% da receita), em 71 SKUs. Margem bruta perdida: R$ 641 mil. O pico foi em dez/2025, logo depois da Black Friday (R$ 441 mil no mês). Os SKUs do fornecedor F05 respondem por 52% da perda |
| Necessidade x orçamento | A demanda prevista, menos estoque e trânsito, dá **R$ 2,24 mi**. Com estoque de segurança, **R$ 2,79 mi**. Com arredondamento de MOQ, a compra que se paga é de **R$ 4,19 mi**. O orçamento de R$ 1,97 mi cobre 47% dela |
| Plano de 90 dias | **177 pedidos em 166 SKUs, R$ 1,97 mi.** Sem comprar nada, a receita em risco no horizonte seria R$ 9,5 mi; com o plano, R$ 1,9 mi (fill rate projetado de 93%). No corte, cada R$ 1 a mais ainda protege R$ 1,39 de margem |
| Orçamento de R$ 1,37 mi | 139 pedidos. A receita em risco sobe para R$ 3,66 mi (fill rate de 87%) e o corte passa a R$ 1,99 de margem por real |
| Encalhe | Liquidar 135.900 unidades (custo de R$ 1,94 mi) com até 40% de desconto. São esperados **R$ 1,09 mi** de volta no site em 90 dias. Outros R$ 1,55 mi a custo não têm demanda no site e precisam de outro canal |
| Nível de serviço | 98% para todos: segurança de R$ 648 mil e custo total (carregar + margem perdida) de R$ 68,7 mil no trimestre. A 99% / B 98% / C 95%: R$ 685 mil e R$ 65,4 mil. 33 SKUs ativos têm nível econômico abaixo de 90%. Mas **quem define o serviço neste trimestre é o orçamento, não a meta** |
| Lead time | F05: mediana de 32 dias, p90 de 74 e CV de 0,45. A média de 39,5 dias esconde uma distribuição bimodal. A variabilidade custa R$ 95 mil de estoque de segurança no F05 e R$ 481 mil somando todos os fornecedores |

## Método

### 1. Limpeza (aba "Qualidade dos dados")

- **656 vendas duplicadas** (linhas idênticas) removidas. A prova: o saldo de estoque só fecha sem a cópia.
- **120 saldos negativos** (de -1 a -8, isolados no meio de saldos de milhares) e os dias sem posição dentro do histórico: **2.599 dias recalculados** pelo balanço `estoque_ontem - vendas + recebimentos`. O balanço fecha em 100% dos dias com registro, então a reconstrução é confiável.
- Compras em **"cx 12 un"** convertidas para unidades para comparar com o estoque, e o pedido volta para caixas na saída.
- **90 pedidos em aberto** entram como trânsito. A chegada vem do lead time condicional do fornecedor: a média dos lead times históricos maiores que a idade do pedido.
- 6 vendas antes do primeiro registro de estoque e 319 SKUs lançados depois de 01/04/2025 (saldo de abertura sem pedido de compra) foram tratados sem inventar recebimento.

### 2. Demanda (sem confiar na média de vendas)

- **Ruptura total:** o dia começou com zero e nada chegou, então a demanda não foi observada. **Ruptura parcial:** o produto acabou no dia, e a venda é só um piso.
- **Índice de sazonalidade diário** do portfólio: venda por SKU-dia com estoque ÷ mediana móvel de 91 dias. Captura campanhas e dia da semana.
- **Demanda atual por SKU:** média dos últimos 56 dias *com estoque*, sem o efeito do índice. A janela é ampliada se houver menos de 14 dias. Nos SKUs que romperam nos últimos 90 dias, a média simples subestima a demanda em 17%.
- **Venda perdida:** taxa local do SKU × índice do dia, valorizada pelo preço praticado (a BF 2025 teve 25% de desconto).
- **Campanhas:** o multiplicador é estimado no mesmo período do ano anterior, contra os 35 dias anteriores à primeira campanha. Deu BF **3,01x** e Natal **1,47x**. Uma checagem com os mesmos SKUs nas duas janelas deu 3,2x e 1,6x, então o Natal pode estar um pouco conservador; o multiplicador é editável.

### 3. Plano de compras

- **Lead time pela distribuição:** a demanda no lead time é simulada (Monte Carlo, 2.000 sorteios por SKU) com o lead time empírico do fornecedor e a variação diária da demanda. Ponto de pedido = quantil do nível de serviço; estoque de segurança = quantil − média.
- **Simulação diária do horizonte:** se a posição (estoque + trânsito) fica abaixo da demanda no lead time + estoque de segurança, emite um pedido que cobre o lead time + 30 dias. O pedido é arredondado para cima em múltiplos do MOQ, na unidade de compra. A demanda já inclui a Black Friday e o Natal, então os pedidos saem antes, de acordo com o lead time de cada fornecedor. Descontinuados não são recomprados.
- **Valor de cada lote (múltiplo de MOQ):** margem × probabilidade de a demanda até a próxima reposição chegar até aquela unidade, menos o custo de carregar (capital + armazenagem) pelo tempo que ela fica parada. Lotes de valor negativo não entram nem com orçamento sobrando: é o caso de MOQ grande demais para o giro.
- **Orçamento: o que sai primeiro.** Os lotes entram em ordem de margem protegida por R$ investido até o orçamento acabar. Saem primeiro o colchão de segurança de itens de giro lento e os lotes que só existem por causa de MOQ alto. Há um critério alternativo, "proteger a classe A", mas ele perde mais: R$ 2,17 mi de receita em risco contra R$ 1,90 mi do padrão.
- **Até quando cobrir:** por padrão, até o fim do horizonte. A opção "fim do horizonte + lead time" protege o começo de janeiro e eleva a necessidade.

### 4. Estoque parado e liquidação

- **Capital parado:** todo o estoque dos descontinuados + o excedente dos ativos acima de 180 dias de cobertura.
- **Quando liquidar:** para um ativo, liquidar com 40% de desconto só compensa financeiramente se a unidade levaria mais de `desconto × preço / (custo × 32,4% a.a.)` para vender (cerca de 5 anos, porque o preço é ~4x o custo). Antes disso, o que decide é o risco de validade. Por isso a regra é liquidar o que passa de 365 dias de cobertura (editável) e todos os descontinuados. Entre 180 e 365 dias: parar de comprar e escoar nas campanhas e em kits.
- **Valor recuperável:** unidades que o site escoa em 90 dias, com a venda 2x maior pelo desconto (premissa), × preço com desconto. O resto aparece separado como "sem demanda no site".

### 5. Nível de serviço

- **Curva caixa x serviço:** estoque de segurança (R$) contra receita em risco por falta, de 80% a 99,5%.
- **Nível econômico por SKU:** `1 − h·c·Q / (margem·D anual)`, ou seja, o ponto em que carregar mais uma unidade custa o mesmo que a margem que ela protege.
- **Fill rate projetado** com o plano, sem comprar nada e com a compra econômica completa.

## Premissas e limitações

- O custo do cadastro vale para compras novas. O orçamento é a custo, e só os pedidos novos do horizonte consomem orçamento; os pedidos em aberto já estavam comprometidos.
- A previsão não projeta tendência de crescimento: a base recente já reflete o patamar atual. SKUs lançados há pouco têm histórico curto, e a previsão deles é menos confiável.
- A projeção do estoque usa a previsão média. A incerteza entra só no estoque de segurança, e o fill rate projetado ignora a variabilidade (é otimista).
- O valor de cada lote supõe que os pedidos anteriores do mesmo SKU foram feitos. A perda projetada é recalculada com os pedidos finais.
- Não há validade nem data de fabricação nos dados. O limite de 365 dias para liquidar é premissa. O aumento de venda na liquidação também é: a BF 2025, com 25% de desconto e mídia, teve ~3x. A liquidação de ativos canibaliza parte da venda a preço cheio, e isso não está descontado.
- O multiplicador de campanha é o mesmo para todos os SKUs. Por categoria, a BF 2025 variou de 2,7x a 3,4x.
- O lead time é por fornecedor (há poucos pedidos por SKU). Os lead times recentes do F05 estão subestimados, porque os pedidos lentos ainda não chegaram (viés de sobrevivência).

## Publicar na Vercel

Não publiquei, como pedido no exercício. O projeto já está pronto para isso:

1. `app.py` expõe `app` (FastAPI), e a Vercel detecta isso sem configuração. Ela usa `.python-version` (3.12) e `requirements.txt` (só produção, com versões fixadas). `.vercelignore` tira `.venv`, `tests` e `notebooks`.
2. Os dados originais estão em `dados/`. `dados/base_padrao.json.gz` (42 KB) evita reprocessar 9 MB de CSV no cold start. O hash dos CSVs garante que ele não fica velho, e `.gitattributes` mantém os bytes dos CSVs idênticos no checkout.
3. **Sem estado:** cada `/api/calcular` recebe os parâmetros no corpo. O `/api/upload` devolve a base pré-agregada (~400 KB), que o navegador guarda e reenvia nos recálculos. O recálculo leva 0,4 a 0,8 s.
4. **Limite de 4,5 MB:** o upload aceita só os arquivos que mudaram, compactados em gzip no navegador (`CompressionStream`), e a tela bloqueia envios acima de 4,4 MB.
5. Comandos: `npm i -g vercel`, depois `vercel` (preview) e `vercel --prod`. Ou importe o repositório no painel da Vercel.

## Estrutura

```
app.py            API e página (FastAPI)
analise.py        motor de análise (limpeza, demanda, lead time, plano, parado, serviço, recomendações)
static/index.html interface (Chart.js via CDN)
tests/            pytest: demanda com ruptura, lead time, MOQ/unidade, orçamento, API e upload
dados/            CSVs originais + cache da base pré-agregada
notebooks/        scripts da exploração inicial (não vão para produção)
```

## Como usei a IA

Fiz tudo com o **Claude Code** como ferramenta principal, em ciclos de "levantar hipótese → testar nos dados → decidir":

- **Exploração:** pedi scripts de pandas para perfilar os arquivos (em `notebooks/`). Daí saíram as vendas duplicadas, os saldos negativos, os dias faltando, os pedidos em caixa, os pedidos em aberto e o lead time bimodal do F05. A hipótese de que o saldo fecha pelo balanço foi testada e confirmada (100%), e isso virou o método de limpeza e de reconstrução.
- **Correções de rota que vieram dos números:**
  - A primeira versão do plano cobria a demanda além do horizonte e pedia R$ 5,6 mi. Separei "necessidade líquida", "com segurança" e "com MOQ" e passei a cobrir até o fim do horizonte, com a outra opção disponível.
  - O multiplicador de Natal saía baixo (1,40x) porque a mediana móvel fica "contaminada" pela própria campanha. Passei a medir contra a base pré-campanha (1,47x) e documentei a diferença para a checagem com os mesmos SKUs.
  - A primeira estimativa de liquidação supunha 50% de escoamento e era otimista demais. Troquei por uma estimativa baseada na demanda e separei o que não tem demanda no site.
  - O nível por classe que eu tinha imaginado (A 98 / B 95 / C 90) saiu *pior* que 98% para todos na conta de custo total, por causa da margem alta. Revisei para A 99 / B 98 / C 95, com exceções por MOQ, e deixei explícito que a diferença é pequena.
- **Código e testes:** o Claude Code escreveu o motor, a API, a interface e os 29 testes. Os testes de demanda e de lead time usam cenários sintéticos com resposta conhecida.
- **Validação visual:** usei screenshots do Chrome headless para revisar cada aba e corrigir problemas de layout.
- A solução não usa nenhum LLM em tempo de execução: é 100% determinística.

# Resumo: Problema 3 (Estoque)

**Problema:** cerca de 500 SKUs GoBeauté (441 ativos, 59 descontinuados), comprados pela média de vendas. Ao mesmo tempo, falta produto e sobra capital parado. Construí um app (FastAPI + HTML) que limpa os dados, corrige a demanda pela ruptura, monta o plano de compras dos próximos 90 dias (01/10 a 29/12/2026) dentro do orçamento e propõe o que fazer com o encalhe. Tudo é recalculado na hora quando um parâmetro muda ou quando se sobem CSVs novos.

## Números (parâmetros de `parametros.md`)

- **Capital parado: R$ 2,14 mi** dos R$ 6,78 mi em estoque. São R$ 1,69 mi em 40 SKUs ativos com mais de 180 dias de cobertura e R$ 454 mil em 53 descontinuados. Custo de carregar: R$ 695 mil/ano. Femme Protein 450G sozinho tem R$ 533 mil parados (12.500 dias de cobertura).
- **Receita perdida por ruptura: R$ 796 mil em 12 meses** (R$ 715 mil em ativos; 1% da receita; margem de R$ 641 mil). O pico foi dez/2025, pós-Black Friday. 52% da perda vem do F05, cujo lead time tem mediana de 32 dias e p90 de 74.
- **A média simples subestima a demanda em 17%** nos SKUs que romperam, porque dia sem estoque vira "venda zero".
- **Campanhas:** a Black Friday multiplica a demanda por 3,0x e o Natal por 1,5x (estimados em 2025).
- **Necessidade x orçamento:** a necessidade líquida é de R$ 2,24 mi; com estoque de segurança, R$ 2,79 mi; com MOQ, R$ 4,19 mi. O orçamento de **R$ 1,97 mi** cobre 47%.
- **Plano:** 177 pedidos e 166 SKUs por R$ 1,97 mi. A receita em risco no trimestre cai de R$ 9,5 mi (sem comprar) para R$ 1,9 mi, e o fill rate projetado fica em 93%. Com R$ 1,37 mi: 139 pedidos, R$ 3,66 mi em risco e fill rate de 87%.
- **Critério de corte:** saem primeiro os lotes de MOQ que protegem menos margem por real investido. No corte, cada R$ 1 a mais ainda protege R$ 1,39 de margem.

## Agente de IA

A aba "Agente de IA" tem um agente de compras com ferramentas. Ele responde "e se" recalculando o plano de verdade, explica por que um SKU saiu e monta o rascunho da ordem de compra por fornecedor, na unidade de compra e com MOQ.

- **Exemplo:** "E se o orçamento cair para R$ 1,37 mi?" A resposta foi 139 pedidos, R$ 3,66 mi de receita em risco e fill rate de 86,6%, igual ao `/api/calcular`. A tela oferece o botão "Aplicar este cenário".
- **Números:** todo número vem das ferramentas, e cada resposta lista as ferramentas usadas.
- **OC:** a ordem de compra é sempre rascunho, pendente de aprovação humana. Nada é enviado.
- **Sem chave:** sem `AI_API_KEY`, o app funciona igual.

## Recomendações

**Compra**

1. Emitir o plano: a data de emissão de cada pedido já considera o lead time real e a antecipação da Black Friday. Dono: Compras.
2. Pedir à diretoria até R$ 2,2 mi a mais de orçamento, financiado em parte pela liquidação, porque o retorno marginal ainda está acima de 1. Donos: Financeiro e Compras.

**Encalhe**

1. Liquidar 135.900 unidades (R$ 1,94 mi a custo) com até 40% de desconto. São esperados cerca de R$ 1,09 mi de volta no site em 90 dias. Os outros R$ 1,55 mi precisam de outlet, B2B, kits ou devolução. Donos: Comercial e Marketing.
2. Excesso de 180 a 365 dias: não recomprar e escoar na Black Friday, no Natal e em kits. Dono: Planejamento.

**Processo e política**

1. Trocar a meta de 98% para todos por A 99% / B 98% / C 95%, com exceção para os 33 SKUs cujo MOQ torna o nível econômico menor que 90%. Descontinuados ficam sem meta. O ganho é pequeno: R$ 65 mil contra R$ 69 mil de custo total no trimestre. Neste trimestre, o orçamento pesa mais que a meta.
2. Cobrar estabilidade do F05: a variabilidade do lead time custa R$ 95 mil de estoque de segurança e é a maior causa de ruptura.
3. Renegociar o MOQ de 100 SKUs cujo lote mínimo cobre mais de 180 dias de venda.
4. Prever pela demanda corrigida, não pela média.
5. Corrigir na origem as vendas duplicadas e os saldos negativos.

**Limites:** não há validade nos dados, então o limite de 365 dias para liquidar é premissa. O aumento de venda na liquidação também é premissa. A previsão não projeta crescimento. O fill rate projetado ignora a variabilidade da demanda e é otimista.

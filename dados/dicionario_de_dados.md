# Dicionário de dados: Estoque

Os nomes de produtos seguem o portfólio GoBeauté (parte deles é fictícia). Códigos de SKU, custos, preços, volumes e fornecedores são sintéticos.

## skus.csv

| Campo | Descrição |
| --- | --- |
| sku | Código do SKU |
| marca, descricao, categoria | Identificação do produto |
| status | ativo ou descontinuado |
| custo_unitario | Custo unitário (R$) |
| preco_venda | Preço de venda cheio (R$) |
| fornecedor | Código do fornecedor (ver fornecedores.csv) |
| unidade_compra | Unidade em que o SKU é comprado (un ou cx 12 un) |
| moq | Lote mínimo de compra, na unidade de compra |

## vendas_diarias.csv

| Campo | Descrição |
| --- | --- |
| sku | Código do SKU |
| data | Dia |
| unidades_vendidas | Unidades vendidas no dia |
| receita | Receita do dia (R$), já com descontos de campanha |

Dias sem venda não aparecem no arquivo.

## estoque_diario.csv

| Campo | Descrição |
| --- | --- |
| sku | Código do SKU |
| data | Dia |
| estoque_disponivel | Posição de estoque ao final do dia, em unidades |

O primeiro registro de cada SKU é o saldo de abertura. Para SKUs que entraram no site depois de 01/04/2025, esse saldo inicial não tem pedido de compra correspondente em compras.csv.

## compras.csv

| Campo | Descrição |
| --- | --- |
| po_id | Identificador do pedido de compra |
| fornecedor | Código do fornecedor |
| sku | Código do SKU |
| data_pedido | Data de emissão do pedido de compra |
| data_recebimento | Data de recebimento (vazia se ainda não recebido) |
| quantidade | Quantidade pedida, na unidade indicada |
| unidade | Unidade da quantidade (un ou cx 12 un) |

## fornecedores.csv

| Campo | Descrição |
| --- | --- |
| fornecedor | Código |
| nome_fornecedor | Nome |

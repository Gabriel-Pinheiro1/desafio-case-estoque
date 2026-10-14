# Parâmetros de negócio: Estoque GoBeauté

Data de referência: 29/09/2026 (último dia com dados). Histórico desde 01/04/2025.
O canal considerado é o e-commerce próprio (site), para todas as marcas GoBeauté: 500 SKUs, entre ativos e descontinuados.

## Financeiro

| Item | Valor |
| --- | --- |
| Orçamento de compras para os próximos 90 dias (01/10 a 29/12/2026) | R$ 1.970.000 |
| Custo de capital | 18% ao ano |
| Custo de armazenagem | 1,2% do custo do estoque, por mês |
| Desconto máximo permitido em liquidação | 40% sobre o preço de venda |

## Metas da diretoria

- Nível de serviço desejado: 98% para todos os SKUs.

## Calendário comercial dos próximos 90 dias

- Black Friday: 27/11/2026, com campanha de 20/11 a 30/11.
- Natal: campanha de 01/12 a 22/12.

## Compras

- O pedido de compra só pode ser emitido em múltiplos do lote mínimo (moq) do SKU, na unidade de compra indicada em skus.csv.
- Compras com data_recebimento vazia ainda não foram recebidas.
- Os lead times históricos podem ser calculados a partir de compras.csv.

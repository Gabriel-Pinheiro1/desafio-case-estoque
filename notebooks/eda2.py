import pandas as pd, numpy as np
pd.set_option('display.width',220); pd.set_option('display.max_columns',30); pd.set_option('display.max_rows',200)
D='dados/'
s=pd.read_csv(D+'skus.csv'); v=pd.read_csv(D+'vendas_diarias.csv',parse_dates=['data']); e=pd.read_csv(D+'estoque_diario.csv',parse_dates=['data']); c=pd.read_csv(D+'compras.csv',parse_dates=['data_pedido','data_recebimento'])
print(s.marca.unique(), s.categoria.unique())
m=c.merge(s[['sku','unidade_compra','moq','status','fornecedor']],on='sku',suffixes=('','_sku'))
print('unit mismatch', (m.unidade!=m.unidade_compra).sum()); print(m[m.unidade!=m.unidade_compra].head(10))
print('forn mismatch', (m.fornecedor!=m.fornecedor_sku).sum())
print('qty not multiple of moq', (m.quantidade % m.moq !=0).sum()); print(m[m.quantidade % m.moq !=0].head(10))
m['ltd']=(m.data_recebimento-m.data_pedido).dt.days
print('lt<0', (m.ltd<0).sum(), m[m.ltd<0].head())
print(m.groupby('fornecedor').ltd.describe(percentiles=[.1,.5,.9,.95]))
print('open POs'); op=m[m.data_recebimento.isna()]; print(op.groupby(['status']).size()); print(op.data_pedido.describe()); print(op.sort_values('data_pedido').head(10))
print('open older than 2026-06-01', (op.data_pedido<'2026-06-01').sum())
e=e.sort_values(['sku','data'])
g=e.groupby('sku').data.agg(['min','max','count'])
g['span']=(g['max']-g['min']).dt.days+1; g['missing']=g.span-g['count']
print(g.describe()); print('skus starting after 2025-04-01', (g['min']>'2025-04-01').sum()); print('end before ref', (g['max']<'2026-09-29').sum())
print(g.missing.sum())
neg=e[e.estoque_disponivel<0]; print('neg', neg.estoque_disponivel.value_counts().head(), neg.sku.nunique())
vf=v.groupby('sku').data.min(); print('sales before stock start', (vf<g['min']).sum())
d=v[v.duplicated(keep=False)]; print(d.head(6))
v['pu']=v.receita/v.unidades_vendidas
vv=v.merge(s[['sku','preco_venda','custo_unitario']],on='sku')
vv['r']=vv.pu/vv.preco_venda
print(vv.r.describe(percentiles=[.01,.05,.5,.95,.99]))
print(vv.r.round(2).value_counts().head(20))
print(vv[vv.r>1.01].head(10)); print((vv.r>1.01).sum(), (vv.r<0.5).sum())

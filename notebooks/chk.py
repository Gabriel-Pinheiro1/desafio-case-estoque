import pandas as pd, numpy as np, analise
b=analise.base_padrao(); sk=pd.DataFrame(b['skus'])
s=pd.read_csv('dados/skus.csv'); c=pd.read_csv('dados/compras.csv',parse_dates=['data_pedido'])
c=c.merge(s[['sku','custo_unitario']],on='sku'); c['q']=np.where(c.unidade.str.startswith('cx'),c.quantidade*12,c.quantidade); c['v']=c.q*c.custo_unitario
print(c.groupby(c.data_pedido.dt.to_period('Q')).v.sum())
v=pd.read_csv('dados/vendas_diarias.csv',parse_dates=['data']).drop_duplicates().merge(s[['sku','custo_unitario','status']],on='sku')
v['cogs']=v.unidades_vendidas*v.custo_unitario
print(v.groupby(v.data.dt.to_period('Q'))[['cogs','receita']].sum())
a=sk[sk.status=='ativo']
print('base_d sum', a.base_d.sum(), 'cost/day', (a.base_d*a.custo).sum(), 'media simples', a.media_simples_90d.sum(), (a.media_simples_90d*a.custo).sum())
idx=pd.DataFrame(b['indice']); print(idx.tail(60).idx.describe()); print(idx.tail(10))

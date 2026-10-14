import pandas as pd, numpy as np
pd.set_option('display.width',220); pd.set_option('display.max_columns',30); pd.set_option('display.max_rows',300)
D='dados/'
s=pd.read_csv(D+'skus.csv')
x=pd.read_pickle('notebooks/grid.pkl').merge(s,on='sku')
vraw=pd.read_csv(D+'vendas_diarias.csv',parse_dates=['data'])
dups=vraw[vraw.duplicated()]; print('dups by sku', dups.sku.nunique(), 'dates', dups.data.min(), dups.data.max())
# does balance fail with dups? check a dup row
r=dups.iloc[0]; print(x[(x.sku==r.sku)&(x.data==r.data)][['estoque_disponivel','prev','unidades_vendidas']])
# outliers: sales vs rolling median
x=x.sort_values(['sku','data'])
x['med28']=x.groupby('sku').unidades_vendidas.transform(lambda s: s.rolling(29,center=True,min_periods=10).median())
x['ratio']=x.unidades_vendidas/(x.med28.clip(lower=1))
camp=((x.data>='2025-11-20')&(x.data<='2025-12-22'))|((x.data>='2025-05-01')&(x.data<='2025-05-14'))|((x.data>='2026-05-01')&(x.data<='2026-05-14'))
o=x[(x.ratio>6)&(x.unidades_vendidas>=30)&~camp]
print('outliers non camp', len(o)); print(o[['sku','data','unidades_vendidas','med28','estoque_disponivel','status']].head(40))
print(o.data.value_counts().head(10))
# status
last=x[x.data=='2026-09-29']
print(last.groupby('status').agg(n=('sku','size'),stock=('estoque_disponivel','sum')))
last['val']=last.estoque_disponivel*last.custo_unitario
print(last.groupby('status').val.sum())
# discontinued sales over time
dsc=x[x.status=='descontinuado']; print(dsc.groupby(dsc.data.dt.to_period('M')).unidades_vendidas.sum())
# active skus with zero sales last 90d
l90=x[x.data>'2026-07-01'].groupby('sku').agg(u=('unidades_vendidas','sum'))
print('skus zero sales last 90d', (l90.u==0).sum())
# stockout days fraction per sku last 180
x['so']=(x.estoque_disponivel<=0)
print(x[x.data>'2026-04-01'].groupby('sku').so.mean().describe(percentiles=[.5,.75,.9,.95]))
# launch info
first=x.groupby('sku').data.min(); print(first.dt.to_period('M').value_counts().sort_index())

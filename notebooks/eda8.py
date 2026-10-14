import pandas as pd, numpy as np
pd.set_option('display.width',220); pd.set_option('display.max_columns',30); pd.set_option('display.max_rows',300)
D='dados/'
s=pd.read_csv(D+'skus.csv')
x=pd.read_pickle('notebooks/grid.pkl').merge(s,on='sku')
last=x[x.data=='2026-09-29'].copy()
r90=x[x.data>'2026-06-30'].groupby('sku').unidades_vendidas.mean()
last=last.set_index('sku'); last['d']=r90; last['cov']=last.estoque_disponivel/last.d.clip(lower=0.01)
last['val']=last.estoque_disponivel*last.custo_unitario
print(last.groupby('status')['cov'].describe(percentiles=[.25,.5,.75,.9]))
bins=[-1,0,30,60,90,180,365,1e9]
last['bin']=pd.cut(last['cov'],bins)
print(last.groupby(['status','bin']).agg(n=('val','size'),val=('val','sum')))
# moq vs demand
last['moq_un']=np.where(last.unidade_compra.str.startswith('cx'),last.moq*12,last.moq)
last['moq_days']=last.moq_un/last.d.clip(lower=0.01)
print(last[last.status=='ativo'].moq_days.describe(percentiles=[.5,.75,.9,.95]))
print((last[last.status=='ativo'].moq_days>180).sum(), (last[last.status=='ativo'].moq_days>365).sum())
print('currently out', (last.estoque_disponivel<=0).sum())
print(last[last.estoque_disponivel<=0][['status','d','fornecedor']])

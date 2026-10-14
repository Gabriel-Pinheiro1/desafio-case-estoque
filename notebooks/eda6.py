import pandas as pd, numpy as np
pd.set_option('display.width',220); pd.set_option('display.max_columns',30); pd.set_option('display.max_rows',300)
D='dados/'
s=pd.read_csv(D+'skus.csv'); c=pd.read_csv(D+'compras.csv',parse_dates=['data_pedido','data_recebimento'])
x=pd.read_pickle('notebooks/grid.pkl').merge(s,on='sku')
c['ltd']=(c.data_recebimento-c.data_pedido).dt.days
f5=c[c.fornecedor=='F05']; print(f5.groupby(f5.data_pedido.dt.to_period('M')).ltd.agg(['count','mean','median','max']))
print(np.histogram(f5.ltd.dropna(),bins=[0,20,30,40,50,60,70,80,90]))
for fz in ['F01','F02']:
    ff=c[c.fornecedor==fz]; print(fz, ff.groupby(ff.data_pedido.dt.to_period('Q')).ltd.agg(['count','mean','median','max']))
# lt by sku within F05?
print(f5.groupby('sku').ltd.agg(['count','mean','min','max']).sort_values('mean').tail(15))
# old skus growth
first=x.groupby('sku').data.min()
old=first[first=='2025-04-01'].index
xo=x[x.sku.isin(old)&(x.estoque_disponivel>0)]
mo=xo.groupby([xo.data.dt.to_period('M')]).agg(u=('unidades_vendidas','sum'),n=('sku','size'))
mo['per_skuday']=mo.u/mo.n; print(mo)
# by status
xo2=xo.groupby([xo.status,xo.data.dt.to_period('M')]).agg(u=('unidades_vendidas','sum'),n=('sku','size')); xo2['r']=xo2.u/xo2.n; print(xo2.r.unstack(0))

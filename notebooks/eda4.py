import pandas as pd, numpy as np
pd.set_option('display.width',220); pd.set_option('display.max_columns',30); pd.set_option('display.max_rows',300)
D='dados/'
s=pd.read_csv(D+'skus.csv'); v=pd.read_csv(D+'vendas_diarias.csv',parse_dates=['data']).drop_duplicates(); e=pd.read_csv(D+'estoque_diario.csv',parse_dates=['data']); c=pd.read_csv(D+'compras.csv',parse_dates=['data_pedido','data_recebimento'])
c=c.merge(s[['sku','unidade_compra']],on='sku'); c['qun']=np.where(c.unidade.str.startswith('cx'),c.quantidade*12,c.quantidade)
rec=c.dropna(subset=['data_recebimento']).groupby(['sku','data_recebimento']).qun.sum().rename('rec')
rec.index.names=['sku','data']
# full grid
rows=[]
g=e.groupby('sku').data.agg(['min'])
grid=[]
for sku,r in g.iterrows():
    grid.append(pd.DataFrame({'sku':sku,'data':pd.date_range(r['min'],'2026-09-29')}))
grid=pd.concat(grid)
x=grid.merge(e,on=['sku','data'],how='left').merge(v[['sku','data','unidades_vendidas']],on=['sku','data'],how='left').merge(rec.reset_index(),on=['sku','data'],how='left')
x[['unidades_vendidas','rec']]=x[['unidades_vendidas','rec']].fillna(0)
x=x.sort_values(['sku','data'])
x['prev']=x.groupby('sku').estoque_disponivel.shift()
x['pred']=x.prev-x.unidades_vendidas+x.rec
ok=x.dropna(subset=['estoque_disponivel','prev'])
ok['diff']=ok.estoque_disponivel-ok.pred
print('reconcile diff dist'); print(ok['diff'].describe(percentiles=[.01,.05,.5,.95,.99])); print((ok['diff']!=0).sum(), len(ok))
print(ok[ok['diff']!=0].head(30))
# stockouts
print('zero stock days', (x.estoque_disponivel==0).sum(), 'neg', (x.estoque_disponivel<0).sum())
z=x[x.estoque_disponivel<=0]; print('sales on zero days', (z.unidades_vendidas>0).sum(), z.unidades_vendidas.describe())
# missing stock days: sales?
mm=x[x.estoque_disponivel.isna()]; print('missing stock days', len(mm), 'with sales', (mm.unidades_vendidas>0).sum())
print(x[x.sku==x[x.estoque_disponivel<0].sku.iloc[0]].iloc[0:0])
neg=x[x.estoque_disponivel<0]
print(neg.head(10))
sk=neg.sku.iloc[0]; d=neg.data.iloc[0]
print(x[(x.sku==sk)&(x.data>=d-pd.Timedelta(days=4))&(x.data<=d+pd.Timedelta(days=6))])
x.to_pickle('notebooks/grid.pkl')

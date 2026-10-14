import pandas as pd, numpy as np
pd.set_option('display.width',220); pd.set_option('display.max_columns',30); pd.set_option('display.max_rows',300)
D='dados/'
s=pd.read_csv(D+'skus.csv')
x=pd.read_pickle('notebooks/grid.pkl').merge(s,on='sku')
x=x[(x.status=='ativo')]
x['instock']=(x.prev>0)&(x.estoque_disponivel>0)
y=x[x.instock]
def rate(d0,d1,g=None):
    z=y[(y.data>=d0)&(y.data<=d1)]
    if g: return z.groupby(g).unidades_vendidas.sum()/z.groupby(g).size()
    return z.unidades_vendidas.sum()/len(z)
# common SKUs in both windows
base=('2025-10-16','2025-11-19'); bf=('2025-11-20','2025-11-30'); nat=('2025-12-01','2025-12-22'); pos=('2025-12-23','2025-12-31'); jan=('2026-01-01','2026-01-31')
def sku_rate(d0,d1):
    z=y[(y.data>=d0)&(y.data<=d1)]; return z.groupby('sku').unidades_vendidas.mean()
b=sku_rate(*base)
for nm,w in [('bf',bf),('nat',nat),('pos',pos),('jan',jan),('maes26',('2026-05-01','2026-05-10')),('base_maes',('2026-04-01','2026-04-28'))]:
    r=sku_rate(*w); j=pd.concat([b,r],axis=1,keys=['b','r']).dropna(); j=j[j.b>0.5]
    print(nm, 'agg ratio', j.r.sum()/j.b.sum(), 'median sku ratio', (j.r/j.b).median(), 'n', len(j))
j=pd.concat([b,sku_rate(*bf),sku_rate(*nat)],axis=1,keys=['b','bf','nat']).dropna().join(s.set_index('sku')[['categoria','marca']])
j=j[j.b>0.5]
gc=j.groupby('categoria')[['b','bf','nat']].sum(); gc['m_bf']=gc.bf/gc.b; gc['m_nat']=gc.nat/gc.b; gc['n']=j.groupby('categoria').size(); print(gc)
gm=j.groupby('marca')[['b','bf','nat']].sum(); gm['m_bf']=gm.bf/gm.b; gm['m_nat']=gm.nat/gm.b; print(gm)
# daily profile around BF
t=y[(y.data>='2025-11-10')&(y.data<='2026-01-10')].groupby('data').unidades_vendidas.mean(); print(t.round(2).to_string())
# DOW
z=y[(y.data>='2026-06-01')]; print(z.groupby(z.data.dt.dayofweek).unidades_vendidas.mean())
# dispersion: var/mean per sku last 90 in-stock nocamp
z=y[(y.data>='2026-07-01')]; st=z.groupby('sku').unidades_vendidas.agg(['mean','var','count']); st['vmr']=st['var']/st['mean']; print(st.vmr.describe()); print(st['mean'].describe(percentiles=[.1,.25,.5,.75,.9]))

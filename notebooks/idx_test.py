import time, pandas as pd, numpy as np, analise
dfs=analise.ler_arquivos(None); d,q=analise.limpar(dfs); g,ref=analise.montar_painel(d,q); s=d['skus']
ativos=set(s.loc[s.status=='ativo','sku'])
t=time.time()
z=g[g.sku.isin(ativos)].copy()
obs=z.unidades_vendidas.where(z.disponivel)
z['lvl']=obs.groupby(z.sku).transform(lambda x: x.rolling(91,center=True,min_periods=20).median())
w=z[z.disponivel & z.lvl.notna()]
idx=(w.groupby('data').unidades_vendidas.sum()/w.groupby('data').lvl.sum())
print(time.time()-t)
for a,b in [('2025-11-20','2025-11-30'),('2025-12-01','2025-12-22'),('2025-12-23','2025-12-31'),('2026-08-01','2026-09-29'),('2025-10-01','2025-11-15')]:
    print(a,b, idx[a:b].mean().round(3))
old=analise.indice_diario(g,s)
for a,b in [('2025-11-20','2025-11-30'),('2025-12-01','2025-12-22'),('2026-08-01','2026-09-29')]:
    print('old',a,b, old[a:b].mean().round(3))

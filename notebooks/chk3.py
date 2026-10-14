import numpy as np, pandas as pd, analise
b=analise.base_padrao(); sk=pd.DataFrame(b['skus'])
tr=pd.DataFrame(b['abertas']).groupby('sku').qtd_un.sum(); sk['tr']=sk.sku.map(tr).fillna(0)
r=analise.calcular(b,{'cobertura_ate':'horizonte','orcamento':1e9})
pl=pd.DataFrame(r['plano']); g=pl.groupby('sku').agg(cost=('custo_sugerido','sum'),q=('qtd_sugerida_un','sum'),n=('sku','size'))
a=sk.set_index('sku').join(g,how='inner')
m=(90+11*2.87+22*1.4)/90
a['Dh']=a.base_d*90*m; a['gap']=(a.Dh-a.estoque-a.tr).clip(lower=0)
a['over']=a.q-a.gap
print('total',a.cost.sum(),'gap', (a.gap*a.custo).sum(), 'over cost',(a.over*a.custo).sum())
print(a.sort_values('over',ascending=False)[['base_d','estoque','tr','Dh','gap','q','n','moq_un','fornecedor']].head(25))

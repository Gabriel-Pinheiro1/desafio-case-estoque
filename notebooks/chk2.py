import numpy as np, pandas as pd, analise
b=analise.base_padrao(); sk=pd.DataFrame(b['skus'])
tr=pd.DataFrame(b['abertas']).groupby('sku').qtd_un.sum()
sk['tr']=sk.sku.map(tr).fillna(0)
a=sk[sk.status=='ativo'].copy()
m=(90+11*1.87+22*0.4)/90
a['Dh']=a.base_d*90*m
a['DhL']=a.base_d*(90*m+27)
for col in ['Dh','DhL']:
    a['gap']=(a[col]-a.estoque-a.tr).clip(lower=0)
    print(col,'demand cost',(a[col]*a.custo).sum(),'gap cost',(a.gap*a.custo).sum(), 'skus with gap',(a.gap>0).sum())
a['cov']=(a.estoque+a.tr)/a.base_d
print(a['cov'].describe(percentiles=[.1,.25,.5,.75,.9]))
print('stock+transit cost ativos',((a.estoque+a.tr)*a.custo).sum())

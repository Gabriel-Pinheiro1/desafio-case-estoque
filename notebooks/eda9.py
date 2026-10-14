import pandas as pd, numpy as np
D='dados/'
s=pd.read_csv(D+'skus.csv')
x=pd.read_pickle('notebooks/grid.pkl').merge(s,on='sku')
x['so']=x.estoque_disponivel<=0
print(x[x.so].groupby([x.data.dt.to_period('M'),'status']).size().unstack())
print(x[x.so&(x.status=='ativo')].sku.nunique())
print(s.groupby('fornecedor').agg(n=('sku','size'),moq=('moq','median'),cx=('unidade_compra',lambda u:(u!='un').sum())))

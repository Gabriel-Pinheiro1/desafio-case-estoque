import pandas as pd, numpy as np
pd.set_option('display.width',220); pd.set_option('display.max_columns',30); pd.set_option('display.max_rows',300)
D='dados/'
s=pd.read_csv(D+'skus.csv'); v=pd.read_csv(D+'vendas_diarias.csv',parse_dates=['data']).drop_duplicates(); e=pd.read_csv(D+'estoque_diario.csv',parse_dates=['data']); c=pd.read_csv(D+'compras.csv',parse_dates=['data_pedido','data_recebimento'])
v=v.merge(s[['sku','preco_venda']],on='sku'); v['disc']=(v.receita/v.unidades_vendidas/v.preco_venda)<0.9
dd=v[v.disc].groupby('data').size(); print('discount dates'); print(dd)
# daily totals
t=v.groupby('data').unidades_vendidas.sum()
w=t.resample('W').sum(); print(w.to_string())

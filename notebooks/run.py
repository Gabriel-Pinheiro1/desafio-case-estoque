import time, json, analise
t=time.time(); b=analise.preparar_base(None); print('base', time.time()-t, len(json.dumps(b,default=str))/1e3,'KB')
t=time.time(); r=analise.calcular(b, {}); print('calc', time.time()-t)
k=r['kpis']
for kk,v in k.items():
    if kk!='multiplicadores': print(kk, v)
print(k['multiplicadores'])
for q in r['qualidade']: print(q)
import pandas as pd
pl=pd.DataFrame(r['plano']); print(pl.status.value_counts()); print(pl.head(20).to_string())
print(pd.DataFrame(r['lead_times'])[['fornecedor','pedidos','media','p50','p90','custo_variabilidade']])
print(pd.DataFrame(r['servico']['curva'])); print(r['servico']['ponto_classe']); print(pd.DataFrame(r['servico']['por_classe']))

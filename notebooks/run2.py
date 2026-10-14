import time, json, analise, pandas as pd
b=analise.base_padrao()
for prm in [{}, {'cobertura_ate':'horizonte'}, {'orcamento':1370000}, {'orcamento':1e8}]:
    t=time.time(); r=analise.calcular(b, prm); k=r['kpis']
    print(prm, round(time.time()-t,2), {x:k[x] for x in ['necessidade_politica','necessidade_economica','plano_custo','plano_pedidos','pedidos_cortados','pedidos_reduzidos','ruptura_proj_sem_compra','ruptura_proj_plano','ruptura_proj_economico','receita_protegida','corte_margem_por_real','urgentes']})

import analise
b=analise.base_padrao()
for prm in [{'cobertura_ate':'horizonte'},{}]:
    k=analise.calcular(b,prm)['kpis']; print(prm,{x:k[x] for x in ['necessidade_liquida','necessidade_liquida_es','necessidade_economica','necessidade_politica','es_custo']})

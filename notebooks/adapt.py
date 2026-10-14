import json, gzip, io, time, urllib.request, pandas as pd
U='http://127.0.0.1:8767'
def post(path, body):
    t=time.time(); r=urllib.request.urlopen(urllib.request.Request(U+path, data=json.dumps(body).encode(), headers={'Content-Type':'application/json'})); j=json.loads(r.read()); return j, time.time()-t
def resumo(nome, j, dt):
    k=j['resultado']['kpis']; pl=j['resultado']['plano']
    print(f"--- {nome} ({dt:.2f}s)")
    print({x:k[x] for x in ['orcamento','necessidade_economica','plano_custo','plano_pedidos','plano_skus','pedidos_cortados','pedidos_reduzidos','ruptura_proj_plano','receita_protegida','corte_margem_por_real','es_custo','necessidade_politica']})
    print('fill', [(x['classe'], x['plano']) for x in j['resultado']['servico']['projetado']])
    return j
t=time.time(); j=json.loads(urllib.request.urlopen(U+'/api/padrao').read()); base=resumo('padrao', j, time.time()-t)
j2,dt=post('/api/calcular',{'params':{'orcamento':1370000}}); resumo('orcamento 1.37M', j2, dt)
# what left
a={(l['sku'],l['data_emissao']):l for l in base['resultado']['plano']}; b={(l['sku'],l['data_emissao']):l for l in j2['resultado']['plano']}
saiu=[(k, a[k]['custo']-b[k]['custo'], a[k]['margem_por_real'], a[k]['classe']) for k in a if k in b and b[k]['custo']<a[k]['custo']]
print('linhas reduzidas/cortadas vs 1.97M:', len(saiu), 'R$', round(sum(x[1] for x in saiu)), 'margem/R$ media das que sairam', round(sum(x[2] for x in saiu)/max(len(saiu),1),2))
print('por classe R$ saiu', pd.DataFrame(saiu,columns=['k','r','m','c']).groupby('c').r.sum().round().to_dict())
for ns in [{'modo_servico':'uniforme','nivel_servico_uniforme':0.90},{'modo_servico':'uniforme','nivel_servico_uniforme':0.98},{'modo_servico':'uniforme','nivel_servico_uniforme':0.995}]:
    j3,dt=post('/api/calcular',{'params':ns}); resumo(str(ns), j3, dt)
    j4,dt=post('/api/calcular',{'params':{**ns,'orcamento':1e9}}); resumo(str(ns)+' sem limite', j4, dt)

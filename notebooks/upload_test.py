import gzip, io, json, time, uuid, urllib.request, pandas as pd
U='http://127.0.0.1:8767'
def multipart(files, fields):
    b='----x'+uuid.uuid4().hex; out=io.BytesIO()
    for k,v in fields.items(): out.write(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k,(fn,data) in files.items():
        out.write(f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{fn}"\r\nContent-Type: application/gzip\r\n\r\n'.encode()); out.write(data); out.write(b'\r\n')
    out.write(f'--{b}--\r\n'.encode()); return out.getvalue(), b
s=pd.read_csv('dados/skus.csv'); sub=s[s.marca.isin(['Kokeshi','Lescent','Barbour\'s'])]
v=pd.read_csv('dados/vendas_diarias.csv'); v=v[v.sku.isin(sub.sku)]
e=pd.read_csv('dados/estoque_diario.csv'); e=e[e.sku.isin(sub.sku)]
files={'skus':('skus.csv.gz',gzip.compress(sub.to_csv(index=False).encode())),
       'vendas_diarias':('vendas_diarias.csv.gz',gzip.compress(v.to_csv(index=False).encode())),
       'estoque_diario':('estoque_diario.csv.gz',gzip.compress(e.to_csv(index=False).encode()))}
body,b=multipart(files,{'params':json.dumps({'orcamento':800000})})
print('payload MB', len(body)/1e6)
t=time.time(); r=urllib.request.urlopen(urllib.request.Request(U+'/api/upload',data=body,headers={'Content-Type':'multipart/form-data; boundary='+b})); j=json.loads(r.read())
k=j['resultado']['kpis']; print('upload ok %.1fs'%(time.time()-t), 'skus', k['skus'], 'plano', k['plano_custo'], 'pedidos', k['plano_pedidos'], 'parado', k['capital_parado'], 'base KB', len(json.dumps(j['base']))/1e3)
t=time.time(); req=urllib.request.Request(U+'/api/calcular',data=json.dumps({'params':{'orcamento':400000},'base':j['base']}).encode(),headers={'Content-Type':'application/json'})
k2=json.loads(urllib.request.urlopen(req).read())['resultado']['kpis']; print('recalc com base enviada %.2fs'%(time.time()-t), k2['skus'], k2['plano_custo'])
# full-size files gz size check
for n in ['vendas_diarias','estoque_diario']:
    raw=open(f'dados/{n}.csv','rb').read(); print(n, len(raw)/1e6,'MB -> gz', len(gzip.compress(raw))/1e6,'MB')

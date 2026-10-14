"""
App web (FastAPI) do Problema 3 - Estoque.

Sem estado entre requisições (pronto para a Vercel):
- GET  /api/padrao      -> resultado com os dados originais e os parâmetros padrão
- POST /api/calcular    -> {"params": {...}, "base": {...opcional}}; recalcula na hora
- POST /api/upload      -> multipart com os CSVs que mudaram (podem vir em gzip) + params;
                           devolve a "base" pré-agregada, que o navegador guarda e reenvia
- GET  /api/plano.csv   -> plano de compras (dados originais, parâmetros na query ?params=JSON)
- GET  /api/agente/status -> se o agente de IA está ligado (AI_API_KEY definida)
- POST /api/agente      -> {"pergunta", "historico", "params", "base"}; agente de compras com ferramentas

Rodar localmente:  python app.py   (porta 8767)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

import agente
import analise

RAIZ = Path(__file__).resolve().parent
agente.carregar_env(RAIZ / ".env")   # local; na Vercel valem as Environment Variables do projeto
limitador = agente.LimitadorPorIP()
app = FastAPI(title="Estoque GoBeauté - plano de compras", version="1.0")


def _resposta(res: dict, base: dict | None = None) -> JSONResponse:
    corpo = {"resultado": analise.para_json(res)}
    if base is not None:
        corpo["base"] = base
    return JSONResponse(corpo)


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse((RAIZ / "static" / "index.html").read_text(encoding="utf-8"))


@app.get("/api/saude")
def saude():
    return {"ok": True}


@app.get("/api/padrao")
def padrao():
    base = analise.base_padrao()
    res = analise.calcular(base, {})
    res["parametros_padrao"] = analise.PARAMS_PADRAO
    return _resposta(res)


@app.post("/api/calcular")
async def calcular(req: Request):
    try:
        corpo = await req.json()
    except Exception:
        raise HTTPException(400, "Corpo JSON inválido")
    params = corpo.get("params") or {}
    base = corpo.get("base") or analise.base_padrao()
    try:
        res = analise.calcular(base, params)
    except Exception as e:  # erro de parâmetro vira mensagem legível
        raise HTTPException(422, f"Não foi possível calcular: {e}")
    return _resposta(res)


@app.post("/api/upload")
async def upload(
    params: str = Form("{}"),
    skus: UploadFile | None = File(None),
    vendas_diarias: UploadFile | None = File(None),
    estoque_diario: UploadFile | None = File(None),
    compras: UploadFile | None = File(None),
    fornecedores: UploadFile | None = File(None),
):
    enviados = {}
    for nome, arq in [("skus", skus), ("vendas_diarias", vendas_diarias), ("estoque_diario", estoque_diario),
                      ("compras", compras), ("fornecedores", fornecedores)]:
        if arq is not None:
            conteudo = await arq.read()
            if conteudo:
                enviados[nome] = conteudo
    if not enviados:
        raise HTTPException(400, "Nenhum arquivo enviado")
    try:
        base = analise.para_json(analise.preparar_base(enviados))
        base["arquivos_enviados"] = sorted(enviados)
        res = analise.calcular(base, json.loads(params or "{}"))
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(422, f"Não foi possível processar os arquivos: {e}")
    return _resposta(res, base)


@app.get("/api/plano.csv")
def plano_csv(params: str = "{}"):
    res = analise.calcular(analise.base_padrao(), json.loads(params))
    return Response(analise.plano_csv(res), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=plano_compras.csv"})


@app.get("/api/agente/status")
def agente_status():
    cfg = agente.config()
    return {"ligado": cfg.ligado, "modelo": cfg.modelo if cfg.ligado else None,
            "limites": {"passos": agente.MAX_PASSOS, "chamadas_por_minuto": agente.CHAMADAS_POR_MINUTO,
                        "pergunta_max": agente.MAX_PERGUNTA}}


def _ip(req: Request) -> str:
    fwd = req.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (req.client.host if req.client else "?")


@app.post("/api/agente")
async def agente_rota(req: Request):
    if int(req.headers.get("content-length") or 0) > agente.MAX_CORPO_BYTES:
        raise HTTPException(413, "Requisição grande demais para o agente")
    bruto = await req.body()
    if len(bruto) > agente.MAX_CORPO_BYTES:
        raise HTTPException(413, "Requisição grande demais para o agente")
    try:
        corpo = json.loads(bruto or b"{}")
        assert isinstance(corpo, dict)
    except Exception:
        raise HTTPException(400, "Corpo JSON inválido")
    if not agente.config().ligado:
        return JSONResponse(agente.responder(corpo.get("pergunta")))
    if not limitador.permitir(_ip(req)):
        raise HTTPException(429, f"Muitas perguntas em pouco tempo (limite de {agente.CHAMADAS_POR_MINUTO} por minuto). "
                                 "Espere um pouco e tente de novo.")
    out = await run_in_threadpool(agente.responder, corpo.get("pergunta"), corpo.get("historico"),
                                  corpo.get("params"), corpo.get("base"))
    return JSONResponse(out)


if __name__ == "__main__":
    import uvicorn

    porta = int(os.environ.get("PORT", "8767"))
    print(f"Abrindo em http://127.0.0.1:{porta}")
    uvicorn.run(app, host="127.0.0.1", port=porta)

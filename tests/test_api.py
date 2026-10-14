"""Testes dos endpoints (sem estado entre requisições)."""
import gzip
import io
import json
import sys
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
from app import app  # noqa: E402

cli = TestClient(app)


def test_pagina_e_padrao():
    assert cli.get("/").status_code == 200
    r = cli.get("/api/padrao")
    assert r.status_code == 200
    res = r.json()["resultado"]
    assert res["kpis"]["plano_custo"] <= res["kpis"]["orcamento"]
    assert res["qualidade"] and res["plano"] and res["parado"]


def test_calcular_recebe_parametros_na_requisicao():
    r = cli.post("/api/calcular", json={"params": {"orcamento": 1_370_000}})
    assert r.status_code == 200
    k = r.json()["resultado"]["kpis"]
    assert k["orcamento"] == 1_370_000 and k["plano_custo"] <= 1_370_000


def test_upload_parcial_compactado_e_recalculo_com_a_base_devolvida():
    skus = pd.read_csv(RAIZ / "dados" / "skus.csv")
    sub = skus[skus["marca"].isin(["Kokeshi", "Lescent"])]
    vendas = pd.read_csv(RAIZ / "dados" / "vendas_diarias.csv")
    vendas = vendas[vendas["sku"].isin(sub["sku"])]
    arquivos = {
        "skus": ("skus.csv.gz", gzip.compress(sub.to_csv(index=False).encode()), "application/gzip"),
        "vendas_diarias": ("vendas_diarias.csv.gz", gzip.compress(vendas.to_csv(index=False).encode()), "application/gzip"),
    }
    r = cli.post("/api/upload", files=arquivos, data={"params": json.dumps({"orcamento": 500_000})})
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert len(corpo["base"]["skus"]) == len(sub)
    assert corpo["resultado"]["kpis"]["plano_custo"] <= 500_000
    # recálculo stateless: a base volta junto com os novos parâmetros
    r2 = cli.post("/api/calcular", json={"params": {"orcamento": 200_000}, "base": corpo["base"]})
    assert r2.status_code == 200
    assert r2.json()["resultado"]["kpis"]["skus"] == len(sub)


def test_upload_com_arquivo_quebrado_da_mensagem():
    r = cli.post("/api/upload", files={"skus": ("skus.csv", b"a,b\n1,2\n", "text/csv")})
    assert r.status_code == 422
    assert "colunas obrigatórias" in r.json()["detail"]


def test_export_csv():
    r = cli.get("/api/plano.csv")
    assert r.status_code == 200
    linhas = r.text.strip().splitlines()
    assert linhas[0].startswith("sku;descricao")
    assert len(linhas) > 10

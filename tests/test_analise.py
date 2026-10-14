"""Testes do motor de análise. Rodar: pytest -q"""
import gzip
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analise  # noqa: E402


# ---------------------------------------------------------------------------
# Dados sintéticos pequenos, no mesmo formato dos originais
# ---------------------------------------------------------------------------

def _csv(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def cenario_ruptura(demanda=10, dias=120, ruptura=(60, 70), lead_times=(20, 22, 24, 40)):
    """Um SKU que vende `demanda`/dia. O estoque zera no fim do dia ruptura[0]-1,
    fica zerado até ruptura[1]-1 (sem venda) e a reposição chega no dia ruptura[1]."""
    datas = pd.date_range("2026-01-01", periods=dias)
    ini, fim = ruptura
    saldo = demanda * ini                      # abertura: dura exatamente até o dia ini-1
    estoque, vendas = [], []
    for k, d in enumerate(datas):
        if k == fim:
            saldo += 2000
        vend = min(demanda, saldo)
        saldo -= vend
        ds = d.strftime("%Y-%m-%d")
        estoque.append({"sku": "AAA-1", "data": ds, "estoque_disponivel": int(saldo)})
        if vend > 0:
            vendas.append({"sku": "AAA-1", "data": ds, "unidades_vendidas": int(vend), "receita": vend * 50.0})
    skus = pd.DataFrame([{"sku": "AAA-1", "marca": "X", "descricao": "Produto A", "categoria": "Hair Care", "status": "ativo",
                          "custo_unitario": 10.0, "preco_venda": 50.0, "fornecedor": "F01", "unidade_compra": "un", "moq": 100}])
    compras = [{"po_id": f"PO-{i}", "fornecedor": "F01", "sku": "AAA-1",
                "data_pedido": (datas[0] - pd.Timedelta(days=200 - i * 30)).strftime("%Y-%m-%d"),
                "data_recebimento": (datas[0] - pd.Timedelta(days=200 - i * 30 - lt)).strftime("%Y-%m-%d"),
                "quantidade": 100, "unidade": "un"} for i, lt in enumerate(lead_times)]
    compras.append({"po_id": "PO-R", "fornecedor": "F01", "sku": "AAA-1",
                    "data_pedido": (datas[fim] - pd.Timedelta(days=25)).strftime("%Y-%m-%d"),
                    "data_recebimento": datas[fim].strftime("%Y-%m-%d"), "quantidade": 2000, "unidade": "un"})
    forn = pd.DataFrame([{"fornecedor": "F01", "nome_fornecedor": "Fornecedor 1"}])
    return {"skus": _csv(skus), "vendas_diarias": _csv(pd.DataFrame(vendas)), "estoque_diario": _csv(pd.DataFrame(estoque)),
            "compras": _csv(pd.DataFrame(compras)), "fornecedores": _csv(forn)}


# ---------------------------------------------------------------------------
# Demanda com ruptura
# ---------------------------------------------------------------------------

def test_demanda_corrigida_ignora_dias_sem_estoque():
    base = analise.preparar_base(cenario_ruptura(demanda=10))
    sku = base["skus"][0]
    # média simples dos últimos 90 dias inclui 10 dias zerados -> fica abaixo de 10
    assert sku["media_simples_90d"] < 9.5
    # a demanda corrigida usa só dias com estoque -> perto de 10 (o índice diário pode mexer um pouco)
    assert sku["base_d"] == pytest.approx(10, rel=0.08)
    assert sku["dias_ruptura_365d"] >= 10


def test_venda_perdida_estimada_nos_dias_de_ruptura():
    base = analise.preparar_base(cenario_ruptura(demanda=10, ruptura=(60, 70)))
    sku = base["skus"][0]
    # 10 dias inteiros sem estoque a ~10/dia -> cerca de 100 unidades perdidas
    assert 85 <= sku["perda_un_365d"] <= 115
    assert sku["perda_receita_365d"] == pytest.approx(sku["perda_un_365d"] * 50, rel=0.05)


def test_duplicata_de_venda_removida_e_saldo_negativo_reconstruido():
    arq = cenario_ruptura()
    v = pd.read_csv(pd.io.common.BytesIO(arq["vendas_diarias"]))
    v = pd.concat([v, v.iloc[[5]]])                        # linha duplicada
    e = pd.read_csv(pd.io.common.BytesIO(arq["estoque_diario"]))
    real = e.loc[30, "estoque_disponivel"]
    e.loc[30, "estoque_disponivel"] = -5                    # erro de registro
    arq["vendas_diarias"], arq["estoque_diario"] = _csv(v), _csv(e)
    d, q = analise.limpar(analise.ler_arquivos(arq))
    assert len(d["vendas"]) == len(v) - 1
    g, _ = analise.montar_painel(d, q)
    assert g.loc[30, "estoque"] == real                     # recalculado pelo balanço
    problemas = " ".join(x["problema"] for x in q)
    assert "duplicadas" in problemas and "negativo" in problemas


def test_aceita_csv_gzip_e_ponto_e_virgula():
    arq = cenario_ruptura()
    s = pd.read_csv(pd.io.common.BytesIO(arq["skus"]))
    arq["skus"] = gzip.compress(s.to_csv(index=False, sep=";", decimal=",").encode("utf-8"))
    base = analise.preparar_base(arq)
    assert base["skus"][0]["custo"] == 10.0


def test_coluna_faltando_da_erro_claro():
    arq = cenario_ruptura()
    s = pd.read_csv(pd.io.common.BytesIO(arq["skus"])).drop(columns=["moq"])
    arq["skus"] = _csv(s)
    with pytest.raises(ValueError, match="moq"):
        analise.ler_arquivos(arq)


# ---------------------------------------------------------------------------
# Lead time
# ---------------------------------------------------------------------------

def test_lead_time_vem_das_compras_recebidas():
    base = analise.preparar_base(cenario_ruptura(lead_times=(20, 22, 24, 40)))
    assert sorted(base["lead_times"]["F01"]) == [20, 22, 24, 25, 40]


def test_pedido_aberto_usa_lead_time_condicional():
    amostra = np.array([20, 22, 24, 40, 60], dtype=float)
    dias, atrasado = analise.chegada_pedido_aberto(amostra, idade=10)
    assert dias == round(amostra.mean() - 10) and not atrasado
    # já com 30 dias: só contam os lead times > 30 (40 e 60) -> média 50 -> faltam 20
    dias, atrasado = analise.chegada_pedido_aberto(amostra, idade=30)
    assert dias == 20 and not atrasado
    # mais velho que qualquer histórico: marca atraso
    dias, atrasado = analise.chegada_pedido_aberto(amostra, idade=90)
    assert atrasado and dias == 7


def test_variabilidade_do_lead_time_aumenta_estoque_de_seguranca():
    rng = np.random.default_rng(0)
    d, sig = np.array([10.0]), np.array([3.0])
    fixo = analise.demanda_no_lead_time(d, sig, [np.array([30.0])], rng)
    variavel = analise.demanda_no_lead_time(d, sig, [np.array([20.0, 25, 30, 35, 70])], rng)
    es_fixo, _ = analise.estoque_seguranca(fixo, d, np.array([30]), np.array([0.98]))
    es_var, _ = analise.estoque_seguranca(variavel, d, np.array([36]), np.array([0.98]))
    assert es_var[0] > 2 * es_fixo[0]
    es_95, _ = analise.estoque_seguranca(variavel, d, np.array([36]), np.array([0.95]))
    assert es_95[0] < es_var[0]


# ---------------------------------------------------------------------------
# Unidade de compra e MOQ
# ---------------------------------------------------------------------------

def test_fator_da_unidade_de_compra():
    assert analise.fator_unidade("un") == 1
    assert analise.fator_unidade("cx 12 un") == 12


@pytest.mark.parametrize("necessidade,moq_un,esperado", [
    (1, 500, 500), (500, 500, 500), (501, 500, 1000), (0, 500, 0), (-3, 500, 0),
    (13, 12, 24),    # caixa de 12 com MOQ de 1 caixa: 13 unidades viram 2 caixas
    (7, 1, 7),
])
def test_arredondamento_para_multiplo_do_moq(necessidade, moq_un, esperado):
    assert analise.arredondar_lote(necessidade, moq_un) == esperado


# ---------------------------------------------------------------------------
# Plano com os dados reais: orçamento, MOQ e coerência
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def base_real():
    return analise.base_padrao()


@pytest.mark.parametrize("orcamento", [1_970_000, 1_370_000, 300_000])
def test_plano_cabe_no_orcamento(base_real, orcamento):
    r = analise.calcular(base_real, {"orcamento": orcamento})
    gasto = sum(l["custo"] for l in r["plano"])
    assert gasto <= orcamento + 1e-6
    assert r["kpis"]["plano_custo"] <= orcamento + 1


def test_orcamento_menor_corta_os_lotes_de_menor_retorno(base_real):
    alto = analise.calcular(base_real, {"orcamento": 1_970_000})
    baixo = analise.calcular(base_real, {"orcamento": 1_370_000})
    assert baixo["kpis"]["plano_custo"] < alto["kpis"]["plano_custo"]
    # o corte fica num retorno maior (sai primeiro o que protege menos margem por real)
    assert baixo["kpis"]["corte_margem_por_real"] >= alto["kpis"]["corte_margem_por_real"]
    assert baixo["kpis"]["ruptura_proj_plano"] >= alto["kpis"]["ruptura_proj_plano"]
    cortados = [l for l in baixo["plano"] if l["status"] != "mantido"]
    assert cortados


def test_pedidos_respeitam_moq_e_unidade_de_compra(base_real):
    r = analise.calcular(base_real, {})
    skus = {s["sku"]: s for s in base_real["skus"]}
    assert r["plano"]
    for l in r["plano"]:
        s = skus[l["sku"]]
        assert l["qtd_un"] % s["moq_un"] == 0
        assert l["qtd_compra"] * s["fator_un"] == l["qtd_un"]
        assert l["qtd_compra"] == 0 or l["qtd_compra"] >= s["moq"]
        assert s["status"] == "ativo"               # descontinuado não é recomprado


def test_nivel_de_servico_maior_pede_mais_compra(base_real):
    baixo = analise.calcular(base_real, {"modo_servico": "uniforme", "nivel_servico_uniforme": 0.90, "orcamento": 1e9})
    alto = analise.calcular(base_real, {"modo_servico": "uniforme", "nivel_servico_uniforme": 0.99, "orcamento": 1e9})
    assert alto["kpis"]["es_custo"] > baixo["kpis"]["es_custo"]
    assert alto["kpis"]["necessidade_politica"] > baixo["kpis"]["necessidade_politica"]


def test_campanha_aumenta_a_demanda_prevista(base_real):
    sem = analise.calcular(base_real, {"campanhas": []})
    com = analise.calcular(base_real, {})
    assert com["kpis"]["receita_prevista_horizonte"] > sem["kpis"]["receita_prevista_horizonte"] * 1.1
    bf = next(c for c in com["kpis"]["multiplicadores"] if c["nome"] == "Black Friday")
    assert 2.0 < bf["estimado_hist"] < 4.0          # estimado do mesmo período de 2025


def test_desconto_de_liquidacao_muda_valor_recuperavel(base_real):
    a = analise.calcular(base_real, {"desconto_max_liquidacao": 0.40})
    b = analise.calcular(base_real, {"desconto_max_liquidacao": 0.20})
    assert b["kpis"]["liquidacao"]["valor_estimado"] > a["kpis"]["liquidacao"]["valor_estimado"]

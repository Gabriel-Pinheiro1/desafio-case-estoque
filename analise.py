"""
Motor de análise de estoque (Problema 3 - Desafio AI-First Gogroup).

Duas etapas, pensadas para rodar sem estado (Vercel):

1. ``preparar_base(arquivos)``: etapa pesada. Lê os CSVs, limpa, reconstrói o saldo
   diário, corrige a demanda nos dias de ruptura e pré-agrega tudo por SKU. Devolve
   um dicionário "base" serializável em JSON (cerca de 200 KB).
2. ``calcular(base, params)``: etapa leve (< 1 s). Recebe a base e os parâmetros de
   negócio e devolve previsão, plano de compras dentro do orçamento, estoque parado,
   política de nível de serviço, recomendações e KPIs.

Nenhuma função depende de variável global mutável; o cache em ``base_padrao()`` é só
uma otimização (se a instância for nova, recalcula).
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

PASTA_DADOS = Path(__file__).resolve().parent / "dados"
ARQUIVOS = ["skus", "vendas_diarias", "estoque_diario", "compras", "fornecedores"]

COLUNAS = {
    "skus": ["sku", "marca", "descricao", "categoria", "status", "custo_unitario",
             "preco_venda", "fornecedor", "unidade_compra", "moq"],
    "vendas_diarias": ["sku", "data", "unidades_vendidas", "receita"],
    "estoque_diario": ["sku", "data", "estoque_disponivel"],
    "compras": ["po_id", "fornecedor", "sku", "data_pedido", "data_recebimento",
                "quantidade", "unidade"],
    "fornecedores": ["fornecedor", "nome_fornecedor"],
}

PARAMS_PADRAO: dict[str, Any] = {
    "orcamento": 1_970_000.0,
    "custo_capital_aa": 0.18,          # 18% ao ano
    "armazenagem_am": 0.012,           # 1,2% do custo por mês
    "desconto_max_liquidacao": 0.40,   # 40% sobre o preço
    "inicio_horizonte": "2026-10-01",
    "dias_horizonte": 90,
    "modo_servico": "classe",          # "classe" ou "uniforme"
    "nivel_servico_uniforme": 0.98,
    "nivel_servico_classe": {"A": 0.99, "B": 0.98, "C": 0.95},
    "campanhas": [
        {"nome": "Black Friday", "inicio": "2026-11-20", "fim": "2026-11-30", "multiplicador": None},
        {"nome": "Natal", "inicio": "2026-12-01", "fim": "2026-12-22", "multiplicador": None},
    ],
    "cobertura_pedido_dias": 30,       # quanto cada pedido cobre além do lead time
    "cobertura_ate": "horizonte",   # compras da janela cobrem até o fim do horizonte + lead time
    "janela_base_dias": 56,            # janela para medir a demanda atual
    "dias_excesso": 180,               # acima disso, é capital parado
    "giro_max_dias": 365,              # acima disso, liquidar (risco de validade)
    "lift_liquidacao": 2.0,            # quanto a venda diária sobe com o desconto de liquidação
    "janela_liquidacao_dias": 90,      # prazo para escoar no site
}

Z_SIM = 2000          # sorteios de Monte Carlo para a demanda no lead time
SEED = 42


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def norm_cdf(x):
    """CDF da normal padrão (aproximação de Abramowitz-Stegun 7.1.26, erro < 1,5e-7)."""
    x = np.asarray(x, dtype=float)
    t = 1.0 / (1.0 + 0.2316419 * np.abs(x))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    pdf = np.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)
    cdf = 1.0 - pdf * poly
    return np.where(x >= 0, cdf, 1.0 - cdf)


def fator_unidade(unidade: str) -> int:
    """Quantas unidades tem uma unidade de compra. 'un' -> 1, 'cx 12 un' -> 12."""
    if not isinstance(unidade, str):
        return 1
    m = re.search(r"(\d+)", unidade)
    if unidade.strip().lower().startswith("cx") and m:
        return int(m.group(1))
    return 1


def arredondar_lote(qtd_un: float, moq_un: int) -> int:
    """Arredonda uma necessidade (em unidades) para cima, em múltiplos do MOQ (em unidades)."""
    if qtd_un <= 0:
        return 0
    moq_un = max(int(moq_un), 1)
    return int(math.ceil(qtd_un / moq_un - 1e-9) * moq_un)


def _ler_csv(conteudo: bytes | str | Path) -> pd.DataFrame:
    if isinstance(conteudo, (str, Path)) and Path(conteudo).exists():
        conteudo = Path(conteudo).read_bytes()
    if isinstance(conteudo, str):
        conteudo = conteudo.encode("utf-8")
    if conteudo[:2] == b"\x1f\x8b":            # gzip (o navegador compacta antes de enviar)
        conteudo = gzip.decompress(conteudo)
    try:
        texto = conteudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = conteudo.decode("latin-1")
    cab = texto.split("\n", 1)[0]
    sep = ";" if cab.count(";") > cab.count(",") else ","
    df = pd.read_csv(io.StringIO(texto), sep=sep, decimal="," if sep == ";" else ".")
    df.columns = [c.strip().lower() for c in df.columns]
    return df


def _hash(conteudos: dict[str, bytes]) -> str:
    h = hashlib.sha1()
    for k in sorted(conteudos):
        h.update(k.encode())
        h.update(conteudos[k])
    return h.hexdigest()


def ler_arquivos(enviados: dict[str, bytes] | None = None) -> dict[str, pd.DataFrame]:
    """Lê os CSVs. Arquivos enviados substituem os originais; os não enviados vêm de dados/."""
    enviados = enviados or {}
    dfs = {}
    for nome in ARQUIVOS:
        if nome in enviados and enviados[nome]:
            df = _ler_csv(enviados[nome])
        else:
            df = _ler_csv(PASTA_DADOS / f"{nome}.csv")
        faltando = [c for c in COLUNAS[nome] if c not in df.columns]
        if faltando:
            raise ValueError(f"{nome}.csv sem as colunas obrigatórias: {', '.join(faltando)}")
        dfs[nome] = df
    return dfs


# ---------------------------------------------------------------------------
# Etapa 1: limpeza
# ---------------------------------------------------------------------------

def limpar(dfs: dict[str, pd.DataFrame]) -> tuple[dict[str, pd.DataFrame], list[dict]]:
    """Limpa os dados e devolve (dados limpos, lista de problemas encontrados)."""
    q: list[dict] = []

    def reg(arquivo, problema, linhas, tratamento, motivo):
        q.append({"arquivo": arquivo, "problema": problema, "linhas": int(linhas),
                  "tratamento": tratamento, "motivo": motivo})

    # --- skus
    s = dfs["skus"].copy()
    for c in ["sku", "marca", "descricao", "categoria", "status", "fornecedor", "unidade_compra"]:
        s[c] = s[c].astype(str).str.strip()
    s["status"] = s["status"].str.lower()
    dup = s.duplicated("sku", keep="last").sum()
    if dup:
        reg("skus.csv", "SKU repetido no cadastro", dup, "Mantida a última linha", "Cadastro deve ter um registro por SKU")
        s = s.drop_duplicates("sku", keep="last")
    for c in ["custo_unitario", "preco_venda", "moq"]:
        s[c] = pd.to_numeric(s[c], errors="coerce")
    ruins = s["custo_unitario"].isna() | (s["custo_unitario"] <= 0) | s["preco_venda"].isna() | (s["preco_venda"] <= 0)
    if ruins.sum():
        reg("skus.csv", "Custo ou preço ausente/zero", ruins.sum(), "SKU fora do plano de compras", "Sem custo não há como orçar")
    s.loc[s["moq"].isna() | (s["moq"] < 1), "moq"] = 1
    s["moq"] = s["moq"].astype(int)
    s["fator_un"] = s["unidade_compra"].map(fator_unidade)
    s["moq_un"] = s["moq"] * s["fator_un"]
    s["cadastro_ok"] = ~ruins
    n_cx = int((s["fator_un"] > 1).sum())
    if n_cx:
        reg("skus.csv", "SKUs comprados em caixa (cx 12 un)", n_cx,
            "Quantidades convertidas para unidades no cálculo e de volta para caixas no pedido",
            "Estoque e vendas estão em unidades; o pedido precisa sair na unidade de compra")
    skus_validos = set(s["sku"])

    # --- vendas
    v = dfs["vendas_diarias"].copy()
    v["sku"] = v["sku"].astype(str).str.strip()
    v["data"] = pd.to_datetime(v["data"], errors="coerce")
    v["unidades_vendidas"] = pd.to_numeric(v["unidades_vendidas"], errors="coerce")
    v["receita"] = pd.to_numeric(v["receita"], errors="coerce")
    n0 = len(v)
    dup = v.duplicated().sum()
    if dup:
        reg("vendas_diarias.csv", "Linhas duplicadas (mesmo SKU, dia, unidades e receita)", dup,
            "Removidas", "O saldo de estoque só fecha sem a cópia: a venda foi contada duas vezes")
        v = v.drop_duplicates()
    dup2 = v.duplicated(["sku", "data"]).sum()
    if dup2:
        reg("vendas_diarias.csv", "Mesmo SKU e dia com valores diferentes", dup2, "Somados", "Pode ser venda parcial lançada em duas linhas")
        v = v.groupby(["sku", "data"], as_index=False)[["unidades_vendidas", "receita"]].sum()
    ruins = v["data"].isna() | v["unidades_vendidas"].isna() | (v["unidades_vendidas"] <= 0)
    if ruins.sum():
        reg("vendas_diarias.csv", "Data inválida ou unidades <= 0", ruins.sum(), "Removidas", "Não representam venda")
        v = v[~ruins]
    fora = ~v["sku"].isin(skus_validos)
    if fora.sum():
        reg("vendas_diarias.csv", "SKU fora do cadastro", fora.sum(), "Removidas", "Sem custo/preço não entram na análise")
        v = v[~fora]

    # --- estoque
    e = dfs["estoque_diario"].copy()
    e["sku"] = e["sku"].astype(str).str.strip()
    e["data"] = pd.to_datetime(e["data"], errors="coerce")
    e["estoque_disponivel"] = pd.to_numeric(e["estoque_disponivel"], errors="coerce")
    e = e[e["data"].notna() & e["sku"].isin(skus_validos)]
    dup = e.duplicated(["sku", "data"], keep="last").sum()
    if dup:
        reg("estoque_diario.csv", "Mais de uma posição no mesmo dia", dup, "Mantida a última", "Posição é de fim de dia")
        e = e.drop_duplicates(["sku", "data"], keep="last")
    neg = (e["estoque_disponivel"] < 0).sum()
    if neg:
        reg("estoque_diario.csv", "Saldo negativo (de -1 a -8, isolado no meio de saldos altos)", neg,
            "Recalculado pelo saldo: dia anterior - vendas + recebimentos",
            "O dia seguinte volta ao valor esperado, então é erro de registro, não ruptura")
        e.loc[e["estoque_disponivel"] < 0, "estoque_disponivel"] = np.nan

    # --- compras
    c = dfs["compras"].copy()
    for col in ["po_id", "fornecedor", "sku", "unidade"]:
        c[col] = c[col].astype(str).str.strip()
    c["data_pedido"] = pd.to_datetime(c["data_pedido"], errors="coerce")
    c["data_recebimento"] = pd.to_datetime(c["data_recebimento"].replace({"": np.nan}), errors="coerce")
    c["quantidade"] = pd.to_numeric(c["quantidade"], errors="coerce")
    c = c[c["sku"].isin(skus_validos) & c["data_pedido"].notna() & c["quantidade"].notna()]
    dup = c.duplicated("po_id").sum()
    if dup:
        reg("compras.csv", "Pedido de compra repetido", dup, "Mantida a primeira", "po_id é único")
        c = c.drop_duplicates("po_id")
    c["qtd_un"] = c["quantidade"] * c["unidade"].map(fator_unidade)
    c["lead_time"] = (c["data_recebimento"] - c["data_pedido"]).dt.days
    lt_neg = (c["lead_time"] < 0).sum()
    if lt_neg:
        reg("compras.csv", "Recebimento antes do pedido", lt_neg, "Lead time descartado", "Datas inconsistentes")
        c.loc[c["lead_time"] < 0, "lead_time"] = np.nan
    n_cx = (c["unidade"].map(fator_unidade) > 1).sum()
    if n_cx:
        reg("compras.csv", "Pedidos em caixas", n_cx, "Multiplicados por 12 para comparar com estoque em unidades",
            "Sem converter, o recebimento não bate com o saldo")
    abertos = c["data_recebimento"].isna().sum()
    if abertos:
        reg("compras.csv", "Pedidos ainda não recebidos", abertos,
            "Entram como estoque em trânsito, com chegada prevista pelo lead time do fornecedor já descontada a idade do pedido",
            "Ignorar faria comprar em dobro; contar como já recebido esconderia ruptura")
    mm = c.merge(s[["sku", "fornecedor", "moq_un"]], on="sku", suffixes=("", "_cad"))
    fora_moq = (mm["qtd_un"] % mm["moq_un"] != 0).sum()
    if fora_moq:
        reg("compras.csv", "Pedido histórico fora do múltiplo de MOQ", fora_moq, "Mantido (é histórico)", "Regra vale para pedidos novos")
    forn_dif = (mm["fornecedor"] != mm["fornecedor_cad"]).sum()
    if forn_dif:
        reg("compras.csv", "Fornecedor do pedido diferente do cadastro", forn_dif, "Lead time atribuído ao fornecedor do pedido", "Pedido é a fonte do lead time real")

    f = dfs["fornecedores"].copy()
    f["fornecedor"] = f["fornecedor"].astype(str).str.strip()
    return {"skus": s, "vendas": v, "estoque": e, "compras": c, "fornecedores": f}, q


# ---------------------------------------------------------------------------
# Etapa 1: painel diário (saldo reconstruído e ruptura)
# ---------------------------------------------------------------------------

def montar_painel(d: dict[str, pd.DataFrame], q: list[dict]) -> tuple[pd.DataFrame, pd.Timestamp]:
    s, v, e, c = d["skus"], d["vendas"], d["estoque"], d["compras"]
    data_ref = max(e["data"].max(), v["data"].max())
    inicio = e.groupby("sku")["data"].min()
    inicio = inicio[inicio <= data_ref]
    n_dias = ((data_ref - inicio).dt.days + 1).astype(int)
    sku_rep = np.repeat(inicio.index.values, n_dias.values)
    off = np.concatenate([np.arange(k) for k in n_dias.values])
    datas = np.repeat(inicio.values, n_dias.values) + off.astype("timedelta64[D]")
    g = pd.DataFrame({"sku": sku_rep, "data": datas})

    rec = (c.dropna(subset=["data_recebimento"]).groupby(["sku", "data_recebimento"])["qtd_un"].sum()
           .rename("recebido").reset_index().rename(columns={"data_recebimento": "data"}))
    g = (g.merge(e[["sku", "data", "estoque_disponivel"]], on=["sku", "data"], how="left")
          .merge(v[["sku", "data", "unidades_vendidas", "receita"]], on=["sku", "data"], how="left")
          .merge(rec, on=["sku", "data"], how="left"))
    g[["unidades_vendidas", "receita", "recebido"]] = g[["unidades_vendidas", "receita", "recebido"]].fillna(0.0)
    g = g.sort_values(["sku", "data"]).reset_index(drop=True)

    antes = v.merge(inicio.rename("ini"), left_on="sku", right_index=True)
    antes = (antes["data"] < antes["ini"]).sum()
    if antes:
        q.append({"arquivo": "vendas_diarias.csv", "problema": "Vendas antes do primeiro registro de estoque do SKU",
                  "linhas": int(antes), "tratamento": "Ficam fora da análise de ruptura (sem saldo para comparar)",
                  "motivo": "Não dá para saber se havia estoque"})

    # Reconstrução pelo saldo: estoque_t = estoque_{t-1} - vendas_t + recebido_t
    net = g["recebido"] - g["unidades_vendidas"]
    cum = net.groupby(g["sku"]).cumsum()
    valido = g["estoque_disponivel"].notna()
    k = (g["estoque_disponivel"] - cum).where(valido)
    k_ff = k.groupby(g["sku"]).ffill()
    k_ff = k_ff.fillna(k.groupby(g["sku"]).bfill())
    estimado = (k_ff + cum).clip(lower=0)
    # checagem: nos dias com registro, o saldo previsto pelo dia anterior bate?
    k_prev = k.groupby(g["sku"]).shift().groupby(g["sku"]).ffill()
    prev_ok = valido & k_prev.notna()
    diverg = ((g["estoque_disponivel"] - (k_prev + cum)).abs() > 0.5) & prev_ok
    faltando = int((~valido).sum())
    g["estoque"] = np.where(valido, g["estoque_disponivel"], estimado)
    g["reconstruido"] = ~valido
    if faltando:
        q.append({"arquivo": "estoque_diario.csv", "problema": "Dias sem posição de estoque (ou com saldo negativo) dentro do histórico do SKU",
                  "linhas": faltando, "tratamento": "Saldo reconstruído pelo balanço (anterior - vendas + recebimentos)",
                  "motivo": f"O balanço fecha em {100 * (1 - diverg.sum() / max(prev_ok.sum(), 1)):.2f}% dos dias com registro, então a reconstrução é confiável"})

    g["abertura"] = g.groupby("sku")["estoque"].shift()
    primeiro = g["abertura"].isna()
    g.loc[primeiro, "abertura"] = g.loc[primeiro, "estoque"] + g.loc[primeiro, "unidades_vendidas"] - g.loc[primeiro, "recebido"]
    # Ruptura:
    #  total   -> começou o dia sem estoque e nada chegou: a demanda não foi observada.
    #  parcial -> acabou durante o dia (fechou em zero) ou chegou mercadoria num dia que começou zerado.
    g["rup_total"] = (g["abertura"] <= 0) & (g["recebido"] <= 0) & (g["unidades_vendidas"] <= 0)
    g["rup_parcial"] = ~g["rup_total"] & ((g["estoque"] <= 0) | (g["abertura"] <= 0))
    g["disponivel"] = ~g["rup_total"] & ~g["rup_parcial"]
    return g, data_ref


def indice_diario(g: pd.DataFrame, s: pd.DataFrame) -> pd.Series:
    """Índice de sazonalidade diário do portfólio: venda média por SKU-dia disponível,
    dividida pela mediana móvel centrada de 91 dias. Captura campanhas e dia da semana."""
    ativos = set(s.loc[s["status"] == "ativo", "sku"])
    z = g[g["disponivel"] & g["sku"].isin(ativos)]
    taxa = z.groupby("data")["unidades_vendidas"].mean()
    taxa = taxa.asfreq("D")
    base = taxa.rolling(91, center=True, min_periods=30).median()
    idx = (taxa / base).clip(0.3, 6).fillna(1.0)
    return idx


def preparar_base(enviados: dict[str, bytes] | None = None) -> dict:
    """Etapa pesada: lê, limpa e pré-agrega por SKU. Retorno serializável em JSON."""
    dfs = ler_arquivos(enviados)
    d, q = limpar(dfs)
    g, data_ref = montar_painel(d, q)
    s, c = d["skus"], d["compras"]
    idx = indice_diario(g, s)
    g["idx"] = g["data"].map(idx).fillna(1.0).values
    g["dessaz"] = g["unidades_vendidas"] / g["idx"]

    # fator de preço do dia (campanhas com desconto, ex.: Black Friday 2025 a 75% do preço)
    pv = g.merge(s[["sku", "preco_venda"]], on="sku")
    tem = pv["unidades_vendidas"] > 0
    pf = (pv[tem].groupby("data")["receita"].sum() / (pv[tem]["unidades_vendidas"] * pv[tem]["preco_venda"]).groupby(pv[tem]["data"]).sum())
    g["fator_preco"] = g["data"].map(pf).fillna(1.0).clip(0.3, 1.0).values

    # taxa local (demanda dessazonalizada nos dias com estoque), para estimar a venda perdida
    obs = g["dessaz"].where(g["disponivel"])
    local = obs.groupby(g["sku"]).transform(lambda x: x.rolling(57, center=True, min_periods=5).mean())
    local = local.groupby(g["sku"]).ffill().groupby(g["sku"]).bfill().fillna(0)
    esperado = local * g["idx"]
    g["perda_un"] = np.where(g["rup_total"], esperado,
                     np.where(g["rup_parcial"], np.maximum(esperado - g["unidades_vendidas"], 0), 0.0))

    sk = s.set_index("sku")
    g = g.merge(s[["sku", "preco_venda", "custo_unitario", "status"]], on="sku")
    g["perda_receita"] = g["perda_un"] * g["preco_venda"] * g["fator_preco"]
    g["perda_margem"] = g["perda_un"] * (g["preco_venda"] * g["fator_preco"] - g["custo_unitario"])
    ult365 = g["data"] > data_ref - pd.Timedelta(days=365)
    ult90 = g["data"] > data_ref - pd.Timedelta(days=90)

    # demanda atual por SKU (janela recente; amplia se houver poucos dias com estoque)
    linhas = []
    gs = {k: v for k, v in g.groupby("sku")}
    for sku, row in sk.iterrows():
        z = gs.get(sku)
        if z is None:
            continue
        disp = z[z["disponivel"]]
        base_d, sig, n_obs, jan = 0.0, 0.0, 0, 0
        for janela in (56, 120, 10_000):
            w = disp[disp["data"] > data_ref - pd.Timedelta(days=janela)]
            if len(w) >= 14 or janela == 10_000:
                base_d = float(w["dessaz"].mean()) if len(w) else 0.0
                sig = float(w["dessaz"].std(ddof=1)) if len(w) >= 10 else math.sqrt(max(base_d, 0)) * 1.3
                n_obs, jan = len(w), janela
                break
        if not np.isfinite(sig):
            sig = math.sqrt(max(base_d, 0)) * 1.3
        # demanda pela média simples (como o time faz hoje), para comparar
        u90 = z[z["data"] > data_ref - pd.Timedelta(days=90)]
        media_simples = float(u90["unidades_vendidas"].sum() / 90) if len(u90) else 0.0
        com_venda = z[z["unidades_vendidas"] > 0]
        ult_venda = com_venda["data"].max() if len(com_venda) else pd.NaT
        z365 = z[z["data"] > data_ref - pd.Timedelta(days=365)]
        linhas.append({
            "sku": sku, "marca": row["marca"], "descricao": row["descricao"], "categoria": row["categoria"],
            "status": row["status"], "custo": float(row["custo_unitario"]), "preco": float(row["preco_venda"]),
            "fornecedor": row["fornecedor"], "unidade_compra": row["unidade_compra"], "fator_un": int(row["fator_un"]),
            "moq": int(row["moq"]), "moq_un": int(row["moq_un"]), "cadastro_ok": bool(row["cadastro_ok"]),
            "estoque": float(z["estoque"].iloc[-1]), "inicio": z["data"].iloc[0].strftime("%Y-%m-%d"),
            "dias_hist": int(len(z)), "base_d": round(base_d, 4), "sigma_d": round(sig, 4),
            "n_obs": int(n_obs), "janela_usada": int(min(jan, len(z))),
            "media_simples_90d": round(media_simples, 4),
            "vendas_90d": float(u90["unidades_vendidas"].sum()),
            "vendas_365d": float(z365["unidades_vendidas"].sum()),
            "receita_365d": float(z365["receita"].sum()),
            "dias_ruptura_365d": int((z365["rup_total"] | z365["rup_parcial"]).sum()),
            "dias_ruptura_90d": int((u90["rup_total"] | u90["rup_parcial"]).sum()),
            "perda_un_365d": float(z365["perda_un"].sum()),
            "perda_receita_365d": float(z365["perda_receita"].sum()),
            "perda_margem_365d": float(z365["perda_margem"].sum()),
            "perda_receita_total": float(z["perda_receita"].sum()),
            "dias_sem_venda": int((data_ref - ult_venda).days) if pd.notna(ult_venda) else int(len(z)),
        })
    skus_out = pd.DataFrame(linhas)

    # lead time por fornecedor (pedidos recebidos)
    rec = c.dropna(subset=["lead_time"])
    lt = {f: sorted(int(x) for x in grp["lead_time"]) for f, grp in rec.groupby("fornecedor")}
    abertas = c[c["data_recebimento"].isna()].copy()
    abertas["idade"] = (data_ref - abertas["data_pedido"]).dt.days
    abertas_out = [{"po_id": r.po_id, "sku": r.sku, "fornecedor": r.fornecedor,
                    "data_pedido": r.data_pedido.strftime("%Y-%m-%d"), "quantidade": float(r.quantidade),
                    "unidade": r.unidade, "qtd_un": float(r.qtd_un), "idade": int(r.idade)}
                   for r in abertas.itertuples()]

    # séries para gráficos
    mes = g["data"].dt.to_period("M").astype(str)
    serie_mensal = (g.assign(mes=mes).groupby(["mes", "status"])
                    .agg(receita=("receita", "sum"), perda_receita=("perda_receita", "sum"))
                    .reset_index().to_dict("records"))
    idx_out = [{"data": d_.strftime("%Y-%m-%d"), "idx": round(float(x), 4)} for d_, x in idx.items()]
    receita_365 = float(g.loc[ult365, "receita"].sum())

    nomes = dict(zip(d["fornecedores"]["fornecedor"], d["fornecedores"]["nome_fornecedor"]))
    return {
        "versao": 1,
        "data_ref": data_ref.strftime("%Y-%m-%d"),
        "inicio_hist": g["data"].min().strftime("%Y-%m-%d"),
        "skus": skus_out.to_dict("records"),
        "lead_times": lt,
        "fornecedores": nomes,
        "abertas": abertas_out,
        "indice": idx_out,
        "serie_mensal": serie_mensal,
        "receita_365d": receita_365,
        "qualidade": q,
        "contagens": {k: int(len(v)) for k, v in dfs.items()},
        "dias_reconstruidos": int(g["reconstruido"].sum()),
    }


def para_json(obj):
    """Converte tipos numpy/pandas e NaN/inf para tipos JSON."""
    if isinstance(obj, dict):
        return {str(k): para_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [para_json(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        x = float(obj)
        return None if not math.isfinite(x) else x
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, (pd.Timestamp, datetime, date)):
        return obj.strftime("%Y-%m-%d")
    if isinstance(obj, np.ndarray):
        return para_json(obj.tolist())
    return obj


CACHE_BASE = PASTA_DADOS / "base_padrao.json.gz"


def _hash_originais() -> str:
    return _hash({n: (PASTA_DADOS / f"{n}.csv").read_bytes() for n in ARQUIVOS})


@lru_cache(maxsize=1)
def _base_padrao_json() -> str:
    """Base dos dados originais. Usa o arquivo pré-calculado se ele corresponder aos CSVs
    atuais (hash); senão recalcula e tenta gravar (na Vercel o disco é só leitura)."""
    h = _hash_originais()
    if CACHE_BASE.exists():
        try:
            dados = json.loads(gzip.decompress(CACHE_BASE.read_bytes()))
            if dados.get("hash") == h:
                return json.dumps(dados)
        except Exception:
            pass
    base = para_json(preparar_base(None))
    base["hash"] = h
    txt = json.dumps(base)
    try:
        CACHE_BASE.write_bytes(gzip.compress(txt.encode("utf-8")))
    except OSError:
        pass
    return txt


def base_padrao() -> dict:
    return json.loads(_base_padrao_json())


# ---------------------------------------------------------------------------
# Etapa 2: cálculo com parâmetros
# ---------------------------------------------------------------------------

def mesclar_params(p: dict | None) -> dict:
    out = json.loads(json.dumps(PARAMS_PADRAO))
    for k, val in (p or {}).items():
        if val is None or k not in out:
            continue
        if k == "nivel_servico_classe" and isinstance(val, dict):
            out[k].update({kk: float(vv) for kk, vv in val.items() if vv is not None})
        elif k == "campanhas":
            out[k] = val
        elif isinstance(out[k], (int, float)) and not isinstance(out[k], bool):
            out[k] = float(val)
        else:
            out[k] = val
    out["dias_horizonte"] = int(out["dias_horizonte"])
    out["cobertura_pedido_dias"] = int(out["cobertura_pedido_dias"])
    for k in ("nivel_servico_uniforme",):
        out[k] = min(max(out[k], 0.5), 0.999)
    for cl in out["nivel_servico_classe"]:
        out["nivel_servico_classe"][cl] = min(max(out["nivel_servico_classe"][cl], 0.5), 0.999)
    return out


def estimar_multiplicador(indice: list[dict], inicio: str, fim: str) -> float | None:
    """Média do índice diário no mesmo período do ano anterior (ex.: BF 2025 para BF 2026)."""
    idx = pd.Series({pd.Timestamp(r["data"]): r["idx"] for r in indice})
    a = pd.Timestamp(inicio) - pd.DateOffset(years=1)
    b = pd.Timestamp(fim) - pd.DateOffset(years=1)
    w = idx[(idx.index >= a) & (idx.index <= b)]
    if len(w) < max(3, 0.6 * ((b - a).days + 1)):
        return None
    return round(float(w.mean()), 2)


def amostra_lead_time(lts: dict, fornecedor: str) -> np.ndarray:
    a = lts.get(fornecedor)
    if not a:
        a = [x for v in lts.values() for x in v] or [30]
    return np.asarray(a, dtype=float)


def chegada_pedido_aberto(amostra: np.ndarray, idade: int) -> tuple[int, bool]:
    """Dias a partir da data de referência até a chegada de um pedido em aberto.
    Usa o lead time condicional: E[LT | LT > idade]. Se já passou de todos, considera 7 dias e marca atraso."""
    maiores = amostra[amostra > idade]
    if len(maiores) == 0:
        return 7, True
    return max(1, int(round(maiores.mean() - idade))), False


def classificar_abc(receita: np.ndarray) -> np.ndarray:
    """A = até 80% da receita prevista acumulada, B = até 95%, C = resto."""
    ordem = np.argsort(-receita)
    acum = np.cumsum(receita[ordem]) / max(receita.sum(), 1e-9)
    cl = np.empty(len(receita), dtype=object)
    cl[ordem] = np.where(acum <= 0.80, "A", np.where(acum <= 0.95, "B", "C"))
    return cl


def demanda_no_lead_time(d: np.ndarray, sig: np.ndarray, amostras: list[np.ndarray], rng) -> np.ndarray:
    """Matriz (n_sku, Z_SIM) com sorteios da demanda durante o lead time.
    O lead time vem da distribuição empírica do fornecedor (não só da média)."""
    n = len(d)
    out = np.zeros((n, Z_SIM))
    z = rng.standard_normal((n, Z_SIM))
    for i in range(n):
        L = rng.choice(amostras[i], Z_SIM)
        out[i] = np.maximum(d[i] * L + sig[i] * np.sqrt(L) * z[i], 0)
    return out


def estoque_seguranca(ltd: np.ndarray, d: np.ndarray, lbar: np.ndarray, alfa: np.ndarray):
    """Ponto de pedido = quantil alfa da demanda no lead time; ES = quantil - média."""
    srt = np.sort(ltd, axis=1)
    pos = np.clip((alfa * Z_SIM).astype(int), 0, Z_SIM - 1)
    rop = srt[np.arange(len(d)), pos]
    media = ltd.mean(axis=1)
    es = np.maximum(rop - media, 0)
    falta = np.maximum(ltd - rop[:, None], 0).mean(axis=1)   # unidades faltantes esperadas por ciclo
    return es, falta


@dataclass
class Plano:
    pedidos: list            # (i, t, Q, IP_antes)
    perdido: np.ndarray      # unidades perdidas por SKU (previsão média) no horizonte
    estoque_fim: np.ndarray


def simular(f, estoque, chegadas, L, es, elegivel, moq_un, cobertura, janela, fixos=None, d=None, f_dec=None):
    """Simulação diária do estoque projetado.
    f: demanda prevista (n, T); chegadas: matriz (n, T+1) com pedidos em aberto.
    Política: se posição (estoque + trânsito) < demanda no lead time + ES, pede até cobrir
    lead time + cobertura + ES, arredondando para múltiplos do MOQ.
    Com ``fixos`` (lista de (i, t, Q)), não decide nada: só aplica os pedidos dados.
    ``f_dec`` é a demanda que o plano deve cobrir (zerada depois da data limite de cobertura)."""
    n, T = f.shape
    cumR = np.concatenate([np.zeros((n, 1)), np.cumsum(f, axis=1)], axis=1)
    cumF = cumR if f_dec is None else np.concatenate([np.zeros((n, 1)), np.cumsum(f_dec, axis=1)], axis=1)
    arr = chegadas.copy()
    onhand = estoque.astype(float).copy()
    pipeline = arr[:, 1:].sum(axis=1) + 0.0
    pedidos = []
    perdido = np.zeros(n)
    rows = np.arange(n)
    t0, t1, h_fim = janela
    dref = f.min(axis=1) if d is None else d
    if fixos is not None:
        for (i, t, Q) in fixos:
            arr[i, min(t + L[i], T)] += Q
    for t in range(T):
        onhand += arr[:, t]
        if t > 0:
            pipeline -= arr[:, t]
        if fixos is None and t0 <= t <= t1:
            ip = onhand + pipeline
            fimL = np.minimum(t + L, T)
            dem_L = cumF[rows, fimL] - cumF[:, t]
            dias_L = np.maximum(fimL - t, 1)
            m_rel = np.where(dref > 0, ((cumR[rows, fimL] - cumR[:, t]) / dias_L) / np.maximum(dref, 1e-9), 1.0)
            precisa = dem_L + es * np.clip(m_rel, 0.5, 4)
            gat = elegivel & (ip < precisa)
            if gat.any():
                fimC = np.minimum(fimL + cobertura, T)
                alvo = precisa + (cumF[rows, fimC] - cumF[rows, fimL])
                for i in np.nonzero(gat)[0]:
                    Q = arredondar_lote(alvo[i] - ip[i], moq_un[i])
                    if Q <= 0:
                        continue
                    pedidos.append((int(i), t, Q, float(ip[i])))
                    arr[i, fimL[i]] += Q
                    if fimL[i] > t:
                        pipeline[i] += Q
        vend = np.minimum(onhand, f[:, t])
        if t < h_fim:
            perdido += f[:, t] - vend
        onhand -= vend
    return Plano(pedidos, perdido, onhand)


def valorar_lotes(ped, f, L, cobertura, sig_ltd, sig_d, moq_un, margem, custo, h_aa, T):
    """Valor econômico de cada lote (múltiplo de MOQ) de cada pedido.
    Valor = margem x P(demanda até a próxima reposição alcança a unidade) - custo de carregar a unidade.
    Devolve lista de lotes (pedido, j, valor, custo)."""
    n, _ = f.shape
    cumF = np.concatenate([np.zeros((n, 1)), np.cumsum(f, axis=1)], axis=1)
    lotes = []
    K = 12
    for k, (i, t, Q, ip) in enumerate(ped):
        fimL = min(t + L[i], T)
        fimC = min(fimL + cobertura, T)
        mu = cumF[i, fimC] - cumF[i, t]
        mu_L = cumF[i, fimL] - cumF[i, t]
        dias = max(fimC - t, 1)
        fbar = max(mu / dias, 1e-6)
        m_rel = max(fbar / max(f[i, :].mean(), 1e-9), 0.5)
        sd = math.sqrt((sig_ltd[i] * m_rel) ** 2 + cobertura * (sig_d[i] * m_rel) ** 2) + 1e-6
        m = moq_un[i]
        nl = int(round(Q / m))
        for j in range(nl):
            u = ip + j * m + (np.arange(K) + 0.5) * m / K
            p = 1 - norm_cdf((u - mu) / sd)
            dias_parado = np.maximum(u - mu_L, 0) / fbar
            val = float(np.sum(margem[i] * p - custo[i] * h_aa * dias_parado / 365.0) * m / K)
            lotes.append((k, j, val, m * custo[i]))
    return lotes


def _fmt_data(base_dt: pd.Timestamp, t: int) -> str:
    return (base_dt + pd.Timedelta(days=int(t))).strftime("%Y-%m-%d")


def calcular(base: dict, params: dict | None = None) -> dict:
    p = mesclar_params(params)
    rng = np.random.default_rng(SEED)
    sk = pd.DataFrame(base["skus"])
    n = len(sk)
    data_ref = pd.Timestamp(base["data_ref"])
    dia0 = data_ref + pd.Timedelta(days=1)
    ini_h = pd.Timestamp(p["inicio_horizonte"])
    H = p["dias_horizonte"]
    t0 = max((ini_h - dia0).days, 0)
    t1 = t0 + H - 1
    h_fim = t1 + 1
    T = t1 + 1 + 150
    h_aa = p["custo_capital_aa"] + 12 * p["armazenagem_am"]
    datas = [dia0 + pd.Timedelta(days=k) for k in range(T)]

    # campanhas e multiplicadores (estimados do ano anterior se não informados)
    camp = []
    mult = np.ones(T)
    for cpn in p["campanhas"]:
        try:
            a, b = pd.Timestamp(cpn["inicio"]), pd.Timestamp(cpn["fim"])
        except Exception:
            continue
        est = estimar_multiplicador(base["indice"], cpn["inicio"], cpn["fim"])
        m = cpn.get("multiplicador")
        m = float(m) if m not in (None, "") else (est if est else 1.0)
        camp.append({"nome": cpn.get("nome", "Campanha"), "inicio": cpn["inicio"], "fim": cpn["fim"],
                     "multiplicador": m, "estimado_hist": est})
        for k, dt in enumerate(datas):
            if a <= dt <= b:
                mult[k] = max(mult[k], m) if mult[k] != 1 else m

    ativo = (sk["status"] == "ativo").values & sk["cadastro_ok"].values
    d = np.where(ativo, sk["base_d"].values, 0.0)
    d_desc = sk["base_d"].values  # para descontinuados, só para cobertura
    sig = sk["sigma_d"].values
    f = d[:, None] * mult[None, :]
    preco, custo = sk["preco"].values, sk["custo"].values
    margem = preco - custo
    moq_un = sk["moq_un"].values.astype(int)

    # classes ABC pela receita prevista no horizonte (só ativos)
    rec_prev = (f[:, t0:t1 + 1].sum(axis=1)) * preco
    classe = np.full(n, "-", dtype=object)
    classe[ativo] = classificar_abc(rec_prev[ativo])
    # nível de serviço econômico por SKU: ponto em que o custo de carregar mais uma unidade de
    # segurança iguala a margem que ela protege. 1 - P(ruptura no ciclo) = 1 - h*c*Q / (margem*D anual)
    cob_ = int(p["cobertura_pedido_dias"])
    h_aa_ = p["custo_capital_aa"] + 12 * p["armazenagem_am"]
    q_ciclo = np.maximum(moq_un, d * cob_)
    alfa_econ = np.where(ativo & (d > 0), 1 - (h_aa_ * custo * q_ciclo) / np.maximum(margem * d * 365, 1e-9), np.nan)
    alfa_econ = np.clip(np.nan_to_num(alfa_econ, nan=0.5), 0.5, 0.995)
    if p["modo_servico"] == "uniforme":
        alfa = np.where(ativo, p["nivel_servico_uniforme"], 0.5)
    elif p["modo_servico"] == "economico":
        alfa = np.where(ativo, alfa_econ, 0.5)
    else:
        alfa = np.array([p["nivel_servico_classe"].get(c, 0.9) if a else 0.5 for c, a in zip(classe, ativo)])

    # lead time
    amostras = [amostra_lead_time(base["lead_times"], fz) for fz in sk["fornecedor"]]
    lbar = np.array([int(round(a.mean())) for a in amostras])
    ltd = demanda_no_lead_time(np.maximum(d, 0), np.where(ativo, sig, 0), amostras, rng)
    es, falta_ciclo = estoque_seguranca(ltd, d, lbar, alfa)
    sig_ltd = ltd.std(axis=1)

    # pedidos em aberto
    chegadas = np.zeros((n, T + 1))
    pos = {s_: i for i, s_ in enumerate(sk["sku"])}
    abertas_out = []
    transito = np.zeros(n)
    for po in base["abertas"]:
        i = pos.get(po["sku"])
        if i is None:
            continue
        dias, atrasado = chegada_pedido_aberto(amostra_lead_time(base["lead_times"], po["fornecedor"]), po["idade"])
        chegadas[i, min(dias - 1, T)] += po["qtd_un"]
        transito[i] += po["qtd_un"]
        abertas_out.append({**po, "chegada_prevista": _fmt_data(data_ref, dias), "atrasado": atrasado,
                            "custo": po["qtd_un"] * custo[i], "status_sku": sk["status"].iloc[i]})

    elegivel = ativo & (d > 0)
    cob = p["cobertura_pedido_dias"]
    # até quando as compras desta janela precisam cobrir a demanda:
    #  "horizonte_lt" -> fim do horizonte + lead time (o 1º pedido da próxima janela só chega depois disso)
    #  "horizonte"    -> só até o fim do horizonte
    lim = np.full(n, h_fim) + (lbar if p["cobertura_ate"] == "horizonte_lt" else 0)
    f_dec = np.where(np.arange(T)[None, :] < lim[:, None], f, 0.0)
    plano = simular(f, sk["estoque"].values, chegadas, lbar, es, elegivel, moq_un, cob, (t0, t1, h_fim), d=d, f_dec=f_dec)
    sem_compra = simular(f, sk["estoque"].values, chegadas, lbar, es, elegivel, moq_un, cob, (t0, t1, h_fim), fixos=[])

    # valor de cada lote e escolha dentro do orçamento
    # necessidade líquida: demanda a cobrir - estoque - trânsito, sem estoque de segurança nem MOQ
    nec_liq_un = np.where(elegivel, np.maximum(f_dec.sum(axis=1) - sk["estoque"].values - chegadas.sum(axis=1), 0), 0)
    nec_liq_es_un = np.where(elegivel, np.maximum(f_dec.sum(axis=1) + es - sk["estoque"].values - chegadas.sum(axis=1), 0), 0)
    lotes = valorar_lotes(plano.pedidos, f_dec, lbar, cob, sig_ltd, sig, moq_un, margem, custo, h_aa, T)
    custo_politica = sum(l[3] for l in lotes)
    economicos = [l for l in lotes if l[2] > 0]
    custo_economico = sum(l[3] for l in economicos)
    ordem = sorted(economicos, key=lambda l: (-l[2] / l[3], l[0], l[1]))
    orc = p["orcamento"]
    gasto = 0.0
    n_lotes = {}
    corte_score = None
    primeiro_fora = None
    bloqueado = set()
    for (k, j, val, cst) in ordem:
        if k in bloqueado:           # lotes de um pedido entram em sequência (prefixo)
            continue
        if j != n_lotes.get(k, 0):
            bloqueado.add(k)
            continue
        if gasto + cst <= orc + 1e-6:
            gasto += cst
            n_lotes[k] = j + 1
            corte_score = val / cst
        else:
            if primeiro_fora is None:
                primeiro_fora = val / cst
            bloqueado.add(k)
    lote_val = {(k, j): (val, cst) for (k, j, val, cst) in lotes}
    # curva de retorno do orçamento: lotes na ordem de prioridade (respeitando a sequência por pedido)
    curva_orc, acum_c, acum_v, prox = [], 0.0, 0.0, {}
    pend = {}
    for (k, j, val, cst) in sorted(economicos, key=lambda l: (l[0], l[1])):
        pend.setdefault(k, []).append((val, cst))
    import heapq
    heap = [(-v[0][0] / v[0][1], k, 0) for k, v in pend.items()]
    heapq.heapify(heap)
    passo = max(custo_economico / 60, 1.0)
    prox_marca = passo
    while heap:
        sc, k, j = heapq.heappop(heap)
        val, cst = pend[k][j]
        acum_c += cst
        acum_v += val
        if j + 1 < len(pend[k]):
            heapq.heappush(heap, (-pend[k][j + 1][0] / pend[k][j + 1][1], k, j + 1))
        if acum_c >= prox_marca or not heap:
            curva_orc.append({"investimento": round(acum_c, 0), "margem_protegida": round(acum_v, 0), "margem_por_real": round(-sc, 2)})
            prox_marca += passo

    linhas_plano = []
    fixos = []
    for k, (i, t, Q, ip) in enumerate(plano.pedidos):
        nl_total = int(round(Q / moq_un[i]))
        nl_ok = n_lotes.get(k, 0)
        n_econ = sum(1 for j in range(nl_total) if lote_val[(k, j)][0] > 0)
        val_ok = sum(lote_val[(k, j)][0] for j in range(nl_ok))
        Q_ok = nl_ok * moq_un[i]
        if Q_ok > 0:
            fixos.append((i, t, Q_ok))
        if nl_ok == nl_total:
            st = "mantido"
        elif nl_ok == 0 and n_econ == 0:
            st = "não econômico"
        elif nl_ok == 0:
            st = "cortado (orçamento)"
        elif nl_ok < n_econ:
            st = "reduzido (orçamento)"
        else:
            st = "reduzido (lotes não econômicos)"
        cst_tot = Q * custo[i]
        linhas_plano.append({
            "sku": sk["sku"].iloc[i], "descricao": sk["descricao"].iloc[i], "marca": sk["marca"].iloc[i],
            "fornecedor": sk["fornecedor"].iloc[i], "classe": classe[i], "nivel_servico": round(float(alfa[i]), 3),
            "data_emissao": _fmt_data(dia0, t), "data_chegada": _fmt_data(dia0, t + lbar[i]),
            "lead_time_medio": int(lbar[i]),
            "unidade_compra": sk["unidade_compra"].iloc[i], "moq": int(sk["moq"].iloc[i]),
            "qtd_compra": int(Q_ok // sk["fator_un"].iloc[i]), "qtd_un": int(Q_ok), "custo": round(Q_ok * custo[i], 2),
            "qtd_sugerida_un": int(Q), "custo_sugerido": round(cst_tot, 2),
            "posicao_antes": round(ip, 0), "estoque_atual": float(sk["estoque"].iloc[i]),
            "demanda_dia": round(float(d[i]), 2),
            "margem_por_real": round(val_ok / max(Q_ok * custo[i], 1e-9), 2) if Q_ok else
                               round(max(lote_val[(k, 0)][0], 0) / lote_val[(k, 0)][1], 2),
            "urgente": bool(t <= t0 and ip < (f[i, t:t + lbar[i]].sum())),
            "status": st,
        })
    final = simular(f, sk["estoque"].values, chegadas, lbar, es, elegivel, moq_un, cob, (t0, t1, h_fim), fixos=fixos)
    economico_fixos = []
    for k, (i, t, Q, ip) in enumerate(plano.pedidos):
        ne = 0
        for j in range(int(round(Q / moq_un[i]))):
            if lote_val[(k, j)][0] > 0:
                ne = j + 1
            else:
                break
        if ne:
            economico_fixos.append((i, t, ne * moq_un[i]))
    sim_econ = simular(f, sk["estoque"].values, chegadas, lbar, es, elegivel, moq_un, cob, (t0, t1, h_fim), fixos=economico_fixos)

    dem_hh = f[:, t0:h_fim].sum(axis=1)
    def fill(pl, msk):
        dm = dem_hh[msk].sum()
        return round(float(1 - pl.perdido[msk].sum() / dm), 4) if dm > 0 else None
    servico_proj = []
    for cl in ["A", "B", "C"]:
        msk = classe == cl
        servico_proj.append({"classe": cl, "plano": fill(final, msk), "sem_compra": fill(sem_compra, msk),
                             "economico": fill(sim_econ, msk), "receita_perdida_plano": round(float((final.perdido[msk] * preco[msk]).sum()), 0)})
    servico_proj.append({"classe": "Total", "plano": fill(final, ativo), "sem_compra": fill(sem_compra, ativo),
                         "economico": fill(sim_econ, ativo), "receita_perdida_plano": round(float((final.perdido[ativo] * preco[ativo]).sum()), 0)})

    def rec_perdida(pl):
        return float((pl.perdido * preco).sum())

    # resumo por SKU do horizonte
    dem_h = f[:, t0:t1 + 1].sum(axis=1)
    rup_proj = final.perdido
    sku_plano = {}
    for l in linhas_plano:
        sku_plano.setdefault(l["sku"], 0.0)
        sku_plano[l["sku"]] += l["custo"]

    # ---------------- estoque parado e encalhe ----------------
    fbar90 = np.maximum(d_desc * mult[t0:t1 + 1].mean(), 0)
    estoque = sk["estoque"].values
    valor = estoque * custo
    cobertura = np.where(fbar90 > 0, estoque / np.maximum(fbar90, 1e-9), np.inf)
    # equilíbrio: liquidar compensa quando o tempo para vender a unidade a preço cheio passa
    # de (desconto x preço) / (custo x custo anual de carregar), ou do giro máximo aceitável.
    desc = p["desconto_max_liquidacao"]
    breakeven = np.where(custo > 0, desc * preco * 365 / np.maximum(custo * h_aa, 1e-9), np.inf)
    limite_liq = np.minimum(breakeven, p["giro_max_dias"])
    parado = []
    tot = {"descontinuado": 0.0, "sem_giro": 0.0, "excesso": 0.0}
    rec_tot = {"liquidar_un": 0.0, "liquidar_custo": 0.0, "valor_face": 0.0, "valor_estimado": 0.0,
               "vende_site_un": 0.0, "sem_demanda_custo": 0.0}
    taxa_hist = np.maximum(sk["base_d"].values, sk["vendas_365d"].values / 365.0)
    status = sk["status"].values
    for i in range(n):
        if estoque[i] <= 0:
            continue
        if status[i] != "ativo":
            tipo, exc = "descontinuado", estoque[i]
            liq = estoque[i]
            acao = f"Liquidar com até {desc:.0%} de desconto (outlet, kits, marketplace) e não recomprar"
        elif sk["vendas_90d"].iloc[i] == 0:
            tipo, exc = "sem_giro", estoque[i]
            liq = estoque[i]
            acao = "Sem venda há 90 dias: liquidar e avaliar descontinuar"
        elif cobertura[i] > p["dias_excesso"]:
            tipo = "excesso"
            exc = estoque[i] - fbar90[i] * p["dias_excesso"]
            liq = max(estoque[i] - fbar90[i] * limite_liq[i], 0)
            if liq > 0:
                acao = f"Liquidar o que passa de {limite_liq[i]:.0f} dias de cobertura; não comprar até normalizar"
            else:
                acao = "Suspender compras e usar em Black Friday/Natal e kits, sem desconto profundo"
        else:
            continue
        tot[tipo] += exc * custo[i]
        pr_liq = preco[i] * (1 - desc)
        face = liq * pr_liq
        rec_tot["liquidar_un"] += liq
        rec_tot["liquidar_custo"] += liq * custo[i]
        rec_tot["valor_face"] += face
        vende = min(liq, taxa_hist[i] * p["lift_liquidacao"] * p["janela_liquidacao_dias"])
        recup = vende * pr_liq
        rec_tot["valor_estimado"] += recup
        rec_tot["vende_site_un"] += vende
        rec_tot["sem_demanda_custo"] += (liq - vende) * custo[i]
        em_aberto = transito[i]
        parado.append({
            "sku": sk["sku"].iloc[i], "descricao": sk["descricao"].iloc[i], "marca": sk["marca"].iloc[i],
            "categoria": sk["categoria"].iloc[i], "status": status[i], "tipo": tipo,
            "estoque": float(estoque[i]), "valor_custo": round(valor[i], 2),
            "demanda_dia": round(float(fbar90[i]), 2),
            "cobertura_dias": None if not np.isfinite(cobertura[i]) else round(float(cobertura[i]), 0),
            "excesso_un": round(float(exc), 0), "excesso_custo": round(exc * custo[i], 2),
            "liquidar_un": round(float(liq), 0), "preco_liquidacao": round(pr_liq, 2),
            "valor_recuperavel": round(recup, 2), "vende_site_un": round(float(vende), 0),
            "sem_demanda_un": round(float(liq - vende), 0),
            "valor_face_liquidacao": round(face, 2),
            "custo_carregar_ano": round(exc * custo[i] * h_aa, 2),
            "em_transito_un": float(em_aberto), "dias_sem_venda": int(sk["dias_sem_venda"].iloc[i]),
            "acao": acao + (" | Cancelar/adiar o pedido em aberto" if em_aberto > 0 else ""),
        })
    parado.sort(key=lambda r: -r["excesso_custo"])
    capital_parado = sum(tot.values())
    po_excesso = [a for a in abertas_out if (a["sku"] in {r["sku"] for r in parado})]

    # ---------------- política de nível de serviço (troca caixa x serviço) ----------------
    # reposições esperadas no horizonte: demanda do período / tamanho típico do pedido
    q_tip = np.maximum(moq_un, d * cob)
    ciclos = np.where(ativo & (d > 0), np.maximum(H * d * mult[t0:t1 + 1].mean() / np.maximum(q_tip, 1), 1), 0)
    curva = []
    for a_ in [0.80, 0.85, 0.90, 0.95, 0.97, 0.98, 0.99, 0.995]:
        es_a, falta_a = estoque_seguranca(ltd, d, lbar, np.where(ativo, a_, 0.5))
        es_a, falta_a = np.where(ativo, es_a, 0), np.where(ativo, falta_a, 0)
        curva.append(_ponto_curva(f"{a_:.1%} para todos", a_, es_a, falta_a, ciclos, custo, preco, h_aa, H))
    es_cl, falta_cl = estoque_seguranca(ltd, d, lbar, np.array([p["nivel_servico_classe"].get(c, 0.9) if a else 0.5 for c, a in zip(classe, ativo)]))
    ponto_classe = _ponto_curva("Por classe ({})".format("/".join(f"{k} {v:.0%}" for k, v in p["nivel_servico_classe"].items())),
                                None, np.where(ativo, es_cl, 0), np.where(ativo, falta_cl, 0), ciclos, custo, preco, h_aa, H)
    es_98, falta_98 = estoque_seguranca(ltd, d, lbar, np.where(ativo, 0.98, 0.5))
    ponto_98 = _ponto_curva("Meta da diretoria (98% para todos)", 0.98, np.where(ativo, es_98, 0), np.where(ativo, falta_98, 0), ciclos, custo, preco, h_aa, H)
    # nível econômico por SKU: 1 - h*c*Q / (margem * D anual)
    es_ec, falta_ec = estoque_seguranca(ltd, d, lbar, np.where(ativo, alfa_econ, 0.5))
    ponto_econ = _ponto_curva("Econômico por SKU", None, np.where(ativo, es_ec, 0), np.where(ativo, falta_ec, 0), ciclos, custo, preco, h_aa, H)
    por_classe = []
    for cl in ["A", "B", "C"]:
        msk = classe == cl
        if not msk.any():
            continue
        por_classe.append({
            "classe": cl, "skus": int(msk.sum()),
            "receita_prevista": round(float(rec_prev[msk].sum()), 0),
            "nivel_usado": round(float(np.mean(alfa[msk])), 3),
            "nivel_economico_mediano": round(float(np.nanmedian(alfa_econ[msk])), 3),
            "estoque_seguranca_custo": round(float((es[msk] * custo[msk]).sum()), 0),
            "moq_maior_180d": int(((moq_un[msk] / np.maximum(d[msk], 1e-9)) > 180).sum()),
        })

    # ---------------- lead time ----------------
    lt_out = []
    for fz, am in base["lead_times"].items():
        a = np.asarray(am, dtype=float)
        lt_out.append({"fornecedor": fz, "nome": base["fornecedores"].get(fz, fz), "pedidos": int(len(a)),
                       "media": round(float(a.mean()), 1), "p50": float(np.percentile(a, 50)),
                       "p90": float(np.percentile(a, 90)), "p95": float(np.percentile(a, 95)),
                       "min": float(a.min()), "max": float(a.max()), "desvio": round(float(a.std()), 1),
                       "cv": round(float(a.std() / a.mean()), 2),
                       "hist": np.histogram(a, bins=range(0, 91, 5))[0].tolist(),
                       "skus": int((sk["fornecedor"] == fz).sum()),
                       "perda_receita_12m": round(float(sk.loc[sk["fornecedor"] == fz, "perda_receita_365d"].sum()), 0)})
    # custo da variabilidade do lead time: ES real - ES se o lead time fosse fixo na média
    ltd_fixo = demanda_no_lead_time(np.maximum(d, 0), np.where(ativo, sig, 0), [np.array([a.mean()]) for a in amostras], np.random.default_rng(SEED))
    es_fixo, _ = estoque_seguranca(ltd_fixo, d, lbar, alfa)
    for r in lt_out:
        msk = (sk["fornecedor"] == r["fornecedor"]).values & ativo
        r["custo_variabilidade"] = round(float(((es - es_fixo)[msk] * custo[msk]).sum()), 0)

    # ---------------- KPIs ----------------
    sku_r = sk.copy()
    perda12 = float(sk["perda_receita_365d"].sum())
    perda12_ativos = float(sk.loc[sk["status"] == "ativo", "perda_receita_365d"].sum())
    ativos_rup = sk[(sk["status"] == "ativo") & (sk["perda_receita_365d"] > 0)].copy()
    # média simples x demanda corrigida (SKUs ativos com ruptura nos últimos 90 dias)
    comp = sk[(sk["status"] == "ativo") & (sk["dias_ruptura_90d"] > 0) & (sk["base_d"] > 0)]
    subest = float(1 - comp["media_simples_90d"].sum() / max(comp["base_d"].sum(), 1e-9)) if len(comp) else 0.0

    gasto_plano = sum(l["custo"] for l in linhas_plano)
    receita_sem = rec_perdida(sem_compra)
    receita_final = rec_perdida(final)
    receita_econ = rec_perdida(sim_econ)
    kpis = {
        "data_ref": base["data_ref"], "inicio_horizonte": p["inicio_horizonte"],
        "fim_horizonte": _fmt_data(dia0, t1),
        "skus": n, "skus_ativos": int(ativo.sum()),
        "estoque_total_custo": round(float(valor.sum()), 0),
        "estoque_total_un": round(float(estoque.sum()), 0),
        "capital_parado": round(capital_parado, 0),
        "capital_parado_detalhe": {k: round(v, 0) for k, v in tot.items()},
        "custo_carregar_parado_ano": round(capital_parado * h_aa, 0),
        "custo_carregar_aa": round(h_aa, 4),
        "receita_perdida_12m": round(perda12, 0),
        "receita_perdida_12m_ativos": round(perda12_ativos, 0),
        "margem_perdida_12m": round(float(sk["perda_margem_365d"].sum()), 0),
        "receita_12m": round(base["receita_365d"], 0),
        "perda_pct_receita": round(perda12 / max(base["receita_365d"] + perda12, 1) , 4),
        "skus_com_ruptura_12m": int((sk["dias_ruptura_365d"] > 0).sum()),
        "subestimacao_media_simples": round(subest, 4),
        "skus_comparados_media": int(len(comp)),
        "orcamento": orc,
        "necessidade_politica": round(custo_politica, 0),
        "necessidade_liquida": round(float((nec_liq_un * custo).sum()), 0),
        "necessidade_liquida_es": round(float((nec_liq_es_un * custo).sum()), 0),
        "necessidade_economica": round(custo_economico, 0),
        "plano_custo": round(gasto_plano, 0),
        "plano_pedidos": int(sum(1 for l in linhas_plano if l["qtd_un"] > 0)),
        "plano_skus": int(len({l["sku"] for l in linhas_plano if l["qtd_un"] > 0})),
        "pedidos_cortados": int(sum(1 for l in linhas_plano if l["status"].startswith("cortado"))),
        "pedidos_reduzidos": int(sum(1 for l in linhas_plano if l["status"].startswith("reduzido (orç"))),
        "pedidos_nao_economicos": int(sum(1 for l in linhas_plano if l["status"] == "não econômico")),
        "urgentes": int(sum(1 for l in linhas_plano if l["urgente"] and l["qtd_un"] > 0)),
        "receita_prevista_horizonte": round(float((dem_h * preco).sum()), 0),
        "ruptura_proj_sem_compra": round(receita_sem, 0),
        "ruptura_proj_plano": round(receita_final, 0),
        "ruptura_proj_economico": round(receita_econ, 0),
        "receita_protegida": round(receita_sem - receita_final, 0),
        "corte_margem_por_real": None if corte_score is None else round(corte_score, 2),
        "primeiro_fora_margem_por_real": None if primeiro_fora is None else round(primeiro_fora, 2),
        "transito_custo": round(float((transito * custo).sum()), 0),
        "transito_pedidos": len(abertas_out),
        "transito_atrasados": int(sum(1 for a in abertas_out if a["atrasado"])),
        "po_em_excesso_custo": round(sum(a["custo"] for a in po_excesso), 0),
        "po_em_excesso_qtd": len(po_excesso),
        "liquidacao": {k: round(v, 0) for k, v in rec_tot.items()},
        "es_custo": round(float((es * custo).sum()), 0),
        "multiplicadores": camp,
        "desc_vendas_90d": float(sk.loc[sk["status"] != "ativo", "vendas_90d"].sum()),
        "desc_estoque_un": float(sk.loc[sk["status"] != "ativo", "estoque"].sum()),
        "mes_pico_ruptura": None,
    }

    resultado = {
        "parametros": p,
        "kpis": kpis,
        "plano": sorted(linhas_plano, key=lambda l: (l["data_emissao"], -l["margem_por_real"])),
        "criterio": ("Cada pedido é dividido em lotes do tamanho do MOQ. Cada lote vale a margem bruta que ele protege "
                     "(margem x probabilidade de a demanda até a próxima reposição chegar até aquela unidade) menos o custo "
                     "de carregar o estoque (custo de capital + armazenagem). Os lotes entram em ordem de margem protegida "
                     "por R$ investido até o orçamento acabar. Sai primeiro o lote que protege menos margem por real: em geral, "
                     "o colchão de segurança de itens de giro lento e lotes que só existem por causa de MOQ alto. "
                     "Lotes com valor negativo não entram nem com orçamento sobrando."),
        "curva_orcamento": curva_orc,
        "parado": parado,
        "po_em_excesso": po_excesso,
        "abertas": abertas_out,
        "servico": {"curva": curva, "ponto_classe": ponto_classe, "ponto_98": ponto_98, "ponto_economico": ponto_econ,
                    "por_classe": por_classe, "projetado": servico_proj,
                    "economico_abaixo_90": int((ativo & (alfa_econ < 0.90)).sum()),
                    "economico_abaixo_98": int((ativo & (alfa_econ < 0.98)).sum())},
        "lead_times": sorted(lt_out, key=lambda r: r["fornecedor"]),
        "ruptura_top": sorted([{"sku": r["sku"], "descricao": r["descricao"], "marca": r["marca"], "status": r["status"],
                                "dias_ruptura_365d": r["dias_ruptura_365d"], "perda_un": round(r["perda_un_365d"], 0),
                                "perda_receita": round(r["perda_receita_365d"], 0), "estoque": r["estoque"],
                                "demanda_dia": r["base_d"], "media_simples": r["media_simples_90d"]}
                               for r in base["skus"] if r["perda_receita_365d"] > 0],
                              key=lambda r: -r["perda_receita"])[:30],
        "serie_mensal": base["serie_mensal"],
        "indice": base["indice"],
        "qualidade": base["qualidade"],
        "contagens": base["contagens"],
        "previsao_diaria": [{"data": datas[k].strftime("%Y-%m-%d"), "unidades": round(float(f[:, k].sum()), 0),
                             "receita": round(float((f[:, k] * preco).sum()), 0)} for k in range(t0, t1 + 1)],
        "por_marca": _por_grupo(sk, valor, parado, "marca"),
        "por_categoria": _por_grupo(sk, valor, parado, "categoria"),
    }
    resultado["recomendacoes"] = recomendacoes(resultado)
    return resultado


def _ponto_curva(nome, alfa, es, falta, ciclos, custo, preco, h_aa, H):
    capital = float((es * custo).sum())
    perdido_un = float((falta * ciclos).sum())
    margem = preco - custo
    return {"nome": nome, "nivel": alfa,
            "capital_seguranca": round(capital, 0),
            "custo_carregar_horizonte": round(capital * h_aa * H / 365, 0),
            "receita_em_risco": round(float((falta * ciclos * preco).sum()), 0),
            "margem_em_risco": round(float((falta * ciclos * margem).sum()), 0),
            "unidades_em_risco": round(perdido_un, 0),
            "custo_total": round(capital * h_aa * H / 365 + float((falta * ciclos * margem).sum()), 0)}


def _por_grupo(sk, valor, parado, col):
    df = pd.DataFrame({"g": sk[col].values, "valor": valor})
    tot = df.groupby("g")["valor"].sum()
    par = pd.DataFrame(parado)
    pg = par.groupby(col)["excesso_custo"].sum() if len(par) else pd.Series(dtype=float)
    out = [{"grupo": g, "estoque_custo": round(float(v), 0), "parado_custo": round(float(pg.get(g, 0.0)), 0)} for g, v in tot.items()]
    return sorted(out, key=lambda r: -r["parado_custo"])


def fnum(x: float, casas: int = 0) -> str:
    s = f"{x:,.{casas}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def brl(x: float) -> str:
    s = f"{x:,.0f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def recomendacoes(r: dict) -> dict:
    k = r["kpis"]
    p = r["parametros"]
    serv = r["servico"]
    comp = []
    meses = {}
    for x in r["serie_mensal"]:
        meses[x["mes"]] = meses.get(x["mes"], 0) + x["perda_receita"]
    pico = max(meses, key=meses.get) if meses else None
    pico_txt = f"{pico[5:7]}/{pico[:4]}" if pico else "-"
    urg = [l for l in r["plano"] if l["urgente"] and l["qtd_un"] > 0]
    if urg:
        comp.append({"titulo": f"Emitir hoje os {len(urg)} pedidos urgentes",
                     "criterio": "A posição atual (estoque + em trânsito) não cobre a demanda até a chegada do pedido",
                     "dono": "Compras", "ganho": f"{brl(sum(l['custo'] for l in urg))} em pedidos; evita ruptura imediata em itens que vendem",
                     "prioridade": 1})
    comp.append({"titulo": f"Executar o plano de compras: {k['plano_pedidos']} pedidos, {brl(k['plano_custo'])} (orçamento {brl(k['orcamento'])})",
                 "criterio": "Ponto de pedido com demanda corrigida por ruptura, campanhas e lead time real; corte por margem protegida por R$",
                 "dono": "Compras + Planejamento de demanda",
                 "ganho": f"Receita protegida estimada de {brl(k['receita_protegida'])} no horizonte, contra não comprar",
                 "prioridade": 2})
    bf = next((c for c in k["multiplicadores"] if "black" in c["nome"].lower()), None)
    if bf:
        comp.append({"titulo": f"Antecipar a Black Friday: demanda {fnum(bf['multiplicador'], 1)}x de {bf['inicio'][8:10]}/{bf['inicio'][5:7]} a {bf['fim'][8:10]}/{bf['fim'][5:7]}",
                     "criterio": f"O mês com mais receita perdida por ruptura no histórico foi {pico_txt} ({brl(meses.get(pico, 0))}). O plano já antecipa os pedidos pelo lead time de cada fornecedor, incluindo a cauda longa do mais lento",
                     "dono": "Compras + Marketing", "ganho": "Evita repetir a ruptura de dezembro/2025", "prioridade": 3})
    if k["po_em_excesso_qtd"]:
        comp.append({"titulo": f"Cancelar ou adiar {k['po_em_excesso_qtd']} pedidos em aberto de SKUs que já estão em excesso",
                     "criterio": "SKU com cobertura acima do limite de excesso e pedido ainda não recebido",
                     "dono": "Compras", "ganho": f"Até {brl(k['po_em_excesso_custo'])} de caixa não imobilizado", "prioridade": 4})
    enc = []
    det = k["capital_parado_detalhe"]
    liq = k["liquidacao"]
    enc.append({"titulo": f"Liquidar {fnum(liq['liquidar_un'])} unidades (custo {brl(liq['liquidar_custo'])}) com até {p['desconto_max_liquidacao']:.0%} de desconto",
                "criterio": f"Descontinuados, ativos sem venda há 90 dias e o excedente acima de {p['giro_max_dias']:.0f} dias de cobertura (risco de validade). Para ativos, liquidar só compensa acima desse giro: abaixo disso vender a preço cheio, mesmo devagar, rende mais que o desconto",
                "dono": "Comercial/E-commerce + Marketing",
                "ganho": f"Recuperável no site em {p['janela_liquidacao_dias']:.0f} dias: {brl(liq['valor_estimado'])} (supondo venda {fnum(p['lift_liquidacao'], 1)}x maior com o desconto). Outros {brl(liq['sem_demanda_custo'])} a custo não têm demanda no site: outlet, B2B, kits ou devolução",
                "prioridade": 1})
    enc.append({"titulo": f"Descontinuados: {brl(det['descontinuado'])} parados",
                "criterio": f"Os descontinuados venderam {fnum(r['kpis'].get('desc_vendas_90d', 0))} unidades nos últimos 90 dias e têm {fnum(r['kpis'].get('desc_estoque_un', 0))} em estoque. O site não escoa isso sozinho",
                "dono": "Comercial", "ganho": f"Corta {brl(det['descontinuado'] * k['custo_carregar_aa'])}/ano de custo de carregar; abrir canal de saída (outlet, B2B, kits/brindes)",
                "prioridade": 2})
    enc.append({"titulo": f"Excesso em ativos: {brl(det['excesso'])} acima de {p['dias_excesso']:.0f} dias de cobertura",
                "criterio": "Não recomprar até a cobertura cair; usar nas campanhas do trimestre e em kits",
                "dono": "Planejamento + Marketing", "ganho": f"{brl(det['excesso'] * k['custo_carregar_aa'])}/ano de custo de carregar", "prioridade": 3})
    pol = []
    pc, p98, pe = serv["ponto_classe"], serv["ponto_98"], serv["ponto_economico"]
    d_es = pc["capital_seguranca"] - p98["capital_seguranca"]
    d_rr = pc["receita_em_risco"] - p98["receita_em_risco"]
    pol.append({"titulo": "Trocar a meta única de 98% por nível de serviço por classe ABC, com exceções por MOQ",
                "criterio": (f"{pc['nome']}. O nível econômico depende da margem, do giro e do MOQ de cada SKU: "
                             f"{serv['economico_abaixo_90']} SKUs ativos têm nível econômico abaixo de 90% (giro lento e MOQ grande), "
                             "e para eles 98% só imobiliza caixa. Descontinuados ficam sem meta e sem recompra"),
                "dono": "Diretoria + Planejamento",
                "ganho": (f"Custo total no horizonte (carregar + margem perdida) de {brl(pc['custo_total'])} contra {brl(p98['custo_total'])} com 98% para todos. "
                          f"Estoque de segurança {'+' if d_es >= 0 else '-'}{brl(abs(d_es))}; receita em risco {'+' if d_rr >= 0 else '-'}{brl(abs(d_rr))}. "
                          "Ganho pequeno: neste trimestre o orçamento pesa mais que a meta"),
                "prioridade": 1})
    lt = sorted(r["lead_times"], key=lambda x: -x["cv"])
    if lt:
        pior = lt[0]
        pol.append({"titulo": f"Atacar a variabilidade do lead time do {pior['fornecedor']} ({pior['nome']})",
                    "criterio": f"Mediana de {pior['p50']:.0f} dias, mas p90 de {pior['p90']:.0f} dias (CV {fnum(pior['cv'], 2)}, o maior entre os fornecedores). A média de {fnum(pior['media'], 1)} dias esconde a cauda longa. Os SKUs dele respondem por {pior['perda_receita_12m'] / max(sum(x['perda_receita_12m'] for x in r['lead_times']), 1):.0%} da receita perdida por ruptura em 12 meses",
                    "dono": "Compras (negociação com fornecedor)",
                    "ganho": f"{brl(pior['custo_variabilidade'])} de estoque de segurança a menos se o lead time fosse estável (todos os fornecedores: {brl(sum(x['custo_variabilidade'] for x in r['lead_times']))})",
                    "prioridade": 2})
    moq_trap = sum(c["moq_maior_180d"] for c in serv["por_classe"])
    pol.append({"titulo": f"Renegociar MOQ de {moq_trap} SKUs cujo lote mínimo cobre mais de 180 dias de venda",
                "criterio": "Esses SKUs viram estoque parado a cada compra; o plano já recusa lotes de valor negativo",
                "dono": "Compras", "ganho": f"Reduz o principal gerador de excesso ({brl(det['excesso'])} hoje)", "prioridade": 3})
    pol.append({"titulo": "Prever demanda corrigida por ruptura e campanhas, não pela média de vendas",
                "criterio": f"Nos {k['skus_comparados_media']} SKUs ativos com ruptura nos últimos 90 dias, a média simples subestima a demanda em {k['subestimacao_media_simples']:.0%}",
                "dono": "Planejamento de demanda", "ganho": f"Ataca a perda de {brl(k['receita_perdida_12m_ativos'])} de receita por ruptura em 12 meses",
                "prioridade": 4})
    pol.append({"titulo": "Corrigir a origem dos erros de dados (vendas duplicadas, saldos negativos, dias sem posição)",
                "criterio": "Venda duplicada infla a média e gera compra a mais; saldo negativo parece ruptura falsa",
                "dono": "BI/TI", "ganho": "Previsão e indicadores confiáveis sem limpeza manual", "prioridade": 5})
    return {"compra": comp, "encalhe": enc, "processo": pol}


def plano_csv(res: dict) -> str:
    cols = ["sku", "descricao", "marca", "fornecedor", "classe", "nivel_servico", "data_emissao", "data_chegada",
            "unidade_compra", "moq", "qtd_compra", "qtd_un", "custo", "qtd_sugerida_un", "custo_sugerido",
            "margem_por_real", "urgente", "status"]
    df = pd.DataFrame(res["plano"])
    if df.empty:
        return ";".join(cols) + "\n"
    return df[cols].to_csv(index=False, sep=";", decimal=",")

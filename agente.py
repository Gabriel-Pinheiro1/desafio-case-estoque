"""
Agente de compras (IA com ferramentas) do Problema 3 - Estoque.

O agente responde perguntas de negócio mexendo de verdade no plano: cada número que ele
cita sai de uma ferramenta, e as ferramentas chamam o mesmo motor da tela (``analise.calcular``).

Sem estado entre requisições (pronto para a Vercel): a cada pergunta o navegador manda os
parâmetros atuais do painel, a base (se houve upload) e o histórico da conversa.

Ferramentas:
- recalcular_plano(mudancas)            roda o plano com parâmetros alterados e compara com o atual
- comparar_cenarios(cenario_id)         o que saiu, entrou, foi reduzido ou aumentado (R$ e receita em risco)
- consultar_sku(sku)                    cadastro, demanda, estoque, pedidos no plano e o porquê do status
- consultar_fornecedor(fornecedor)      lead time (distribuição), pedidos em aberto e pedidos do plano
- estoque_parado(tipo, limite)          capital parado, o que liquidar e quanto volta
- rascunho_ordem_compra(fornecedor)     rascunho da OC por fornecedor (pendente de aprovação humana)

Configuração por variáveis de ambiente: AI_API_KEY, AI_BASE_URL, AI_MODEL. Sem AI_API_KEY o
agente fica desligado e o resto do app funciona igual.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

import analise

# ---------------------------------------------------------------------------
# Limites (o app é público)
# ---------------------------------------------------------------------------
MAX_PASSOS = 6                 # chamadas à IA por pergunta (loop de ferramentas)
MAX_FERRAMENTAS_POR_PASSO = 6
MAX_TOKENS = 1200              # por chamada à IA
TIMEOUT_CHAMADA_S = 25         # por chamada HTTP à IA
PRAZO_TOTAL_S = 50             # a rota tem maxDuration de 60 s na Vercel
MAX_PERGUNTA = 1000            # caracteres
MAX_HISTORICO = 10             # mensagens anteriores aproveitadas
MAX_CHARS_MSG = 3000           # por mensagem do histórico
MAX_CORPO_BYTES = 3_000_000    # a base pré-agregada tem ~400 KB; a Vercel aceita até 4,5 MB
CHAMADAS_POR_MINUTO = 8        # por IP, em memória da instância


class ErroFerramenta(ValueError):
    """Erro de argumento vindo da IA. Volta para ela como resultado da ferramenta."""


class ErroIA(RuntimeError):
    """Falha ao falar com o serviço de IA. Vira mensagem legível para o usuário."""


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

def carregar_env(caminho: Path) -> None:
    """Parser mínimo de .env (CHAVE=valor por linha). Não sobrescreve variáveis já definidas,
    então na Vercel valem as Environment Variables do projeto."""
    try:
        linhas = Path(caminho).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for linha in linhas:
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        k, v = linha.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


@dataclass
class Config:
    chave: str
    base_url: str
    modelo: str

    @property
    def ligado(self) -> bool:
        return bool(self.chave)


def config() -> Config:
    return Config(
        chave=os.environ.get("AI_API_KEY", "").strip(),
        base_url=os.environ.get("AI_BASE_URL", "https://ai-proxy.gogroupbr.com/v1").strip().rstrip("/"),
        modelo=os.environ.get("AI_MODEL", "latest").strip() or "latest",
    )


# ---------------------------------------------------------------------------
# Limite de chamadas por IP (em memória: vale por instância, não é global)
# ---------------------------------------------------------------------------

class LimitadorPorIP:
    def __init__(self, limite: int = CHAMADAS_POR_MINUTO, janela_s: float = 60.0):
        self.limite, self.janela = limite, janela_s
        self._reg: dict[str, deque] = defaultdict(deque)
        self._trava = threading.Lock()

    def permitir(self, ip: str, agora: float | None = None) -> bool:
        agora = time.monotonic() if agora is None else agora
        with self._trava:
            fila = self._reg[ip]
            while fila and agora - fila[0] > self.janela:
                fila.popleft()
            if len(fila) >= self.limite:
                return False
            fila.append(agora)
            if len(self._reg) > 5000:  # não deixa a memória crescer sem limite
                for k in [k for k, v in self._reg.items() if not v or agora - v[-1] > self.janela]:
                    del self._reg[k]
            return True


# ---------------------------------------------------------------------------
# Validação dos parâmetros que a IA quer mudar
# ---------------------------------------------------------------------------

REGRAS: dict[str, tuple] = {
    "orcamento": ("num", 0, 50_000_000, "orçamento de compras do horizonte, em R$ (ex.: 1500000)"),
    "custo_capital_aa": ("num", 0, 1, "custo de capital ao ano, fração (0.18 = 18%)"),
    "armazenagem_am": ("num", 0, 0.1, "armazenagem ao mês, fração do custo (0.012 = 1,2%)"),
    "desconto_max_liquidacao": ("num", 0, 0.9, "desconto máximo de liquidação, fração (0.4 = 40%)"),
    "inicio_horizonte": ("data", None, None, "início do horizonte, AAAA-MM-DD"),
    "dias_horizonte": ("int", 7, 180, "duração do horizonte em dias"),
    "modo_servico": ("enum", ("classe", "uniforme", "economico"), None, "política de nível de serviço"),
    "nivel_servico_uniforme": ("num", 0.5, 0.999, "nível de serviço igual para todos, fração (0.98)"),
    "nivel_servico_classe": ("classe", 0.5, 0.999, "nível por classe, ex.: {\"A\": 0.995}"),
    "cobertura_pedido_dias": ("int", 0, 120, "dias que cada pedido cobre além do lead time"),
    "cobertura_ate": ("enum", ("horizonte", "horizonte_lt"), None, "até quando as compras cobrem a demanda"),
    "criterio_corte": ("enum", ("margem", "abc"), None, "o que sai primeiro quando falta orçamento"),
    "dias_excesso": ("int", 30, 1000, "acima disso de cobertura é capital parado"),
    "giro_max_dias": ("int", 30, 3000, "acima disso de cobertura, liquidar"),
    "lift_liquidacao": ("num", 1, 10, "quanto a venda sobe na liquidação (x)"),
    "janela_liquidacao_dias": ("int", 7, 365, "prazo para escoar a liquidação no site"),
    "campanhas": ("campanhas", None, None, "lista de {nome, inicio, fim, multiplicador|null}"),
}


def _data_iso(v: Any, campo: str) -> str:
    if not isinstance(v, str):
        raise ErroFerramenta(f"'{campo}' precisa ser uma data AAAA-MM-DD")
    try:
        return date.fromisoformat(v[:10]).isoformat()
    except ValueError:
        raise ErroFerramenta(f"'{campo}' = {v!r} não é uma data AAAA-MM-DD válida")


def _numero(v: Any, campo: str, lo: float, hi: float, inteiro: bool = False) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ErroFerramenta(f"'{campo}' precisa ser um número (recebi {v!r})")
    if inteiro and float(v) != int(v):
        raise ErroFerramenta(f"'{campo}' precisa ser um número inteiro de dias (recebi {v!r})")
    if not (lo <= v <= hi):
        dica = " Percentuais vão em fração: 99,5% = 0.995." if hi <= 1 and v > 1 else ""
        raise ErroFerramenta(f"'{campo}' = {v} fora do intervalo permitido [{lo}, {hi}].{dica}")
    return int(v) if inteiro else float(v)


def validar_mudancas(m: Any) -> dict:
    """Valida o que a IA pediu para mudar. Levanta ErroFerramenta com mensagem que ela entende."""
    if not isinstance(m, dict) or not m:
        raise ErroFerramenta("'mudancas' precisa ser um objeto com ao menos um parâmetro, ex.: {\"orcamento\": 1500000}")
    out: dict[str, Any] = {}
    for campo, v in m.items():
        if campo not in REGRAS:
            raise ErroFerramenta(f"parâmetro desconhecido '{campo}'. Use só: {', '.join(REGRAS)}")
        tipo, a, b, _ = REGRAS[campo]
        if tipo == "num":
            out[campo] = _numero(v, campo, a, b)
        elif tipo == "int":
            out[campo] = _numero(v, campo, a, b, inteiro=True)
        elif tipo == "enum":
            if v not in a:
                raise ErroFerramenta(f"'{campo}' = {v!r} inválido. Opções: {', '.join(a)}")
            out[campo] = v
        elif tipo == "data":
            out[campo] = _data_iso(v, campo)
        elif tipo == "classe":
            if not isinstance(v, dict) or not v:
                raise ErroFerramenta("'nivel_servico_classe' precisa ser um objeto, ex.: {\"A\": 0.995}")
            cl = {}
            for c, x in v.items():
                if c not in ("A", "B", "C"):
                    raise ErroFerramenta(f"classe '{c}' não existe. Use A, B ou C")
                cl[c] = _numero(x, f"nivel_servico_classe.{c}", a, b)
            out[campo] = cl
        elif tipo == "campanhas":
            if not isinstance(v, list) or len(v) > 10:
                raise ErroFerramenta("'campanhas' precisa ser uma lista com até 10 campanhas")
            cs = []
            for c in v:
                if not isinstance(c, dict):
                    raise ErroFerramenta("cada campanha precisa ser {nome, inicio, fim, multiplicador}")
                ini, fim = _data_iso(c.get("inicio"), "campanhas.inicio"), _data_iso(c.get("fim"), "campanhas.fim")
                if fim < ini:
                    raise ErroFerramenta(f"campanha '{c.get('nome')}': fim antes do início")
                mult = c.get("multiplicador")
                if mult is not None:
                    mult = _numero(mult, "campanhas.multiplicador", 0.1, 10)
                cs.append({"nome": str(c.get("nome") or "Campanha")[:60], "inicio": ini, "fim": fim, "multiplicador": mult})
            out[campo] = cs
    return out


def aplicar_mudancas(params: dict, mudancas: dict) -> tuple[dict, list[str]]:
    """Parâmetros do painel + mudanças (o nível por classe é mesclado, não substituído)."""
    novo = json.loads(json.dumps(params or {}))
    obs = []
    for k, v in mudancas.items():
        if k == "nivel_servico_classe":
            base_cl = dict(analise.PARAMS_PADRAO["nivel_servico_classe"])
            base_cl.update(novo.get(k) or {})
            base_cl.update(v)
            novo[k] = base_cl
        else:
            novo[k] = v
    modo = novo.get("modo_servico", analise.PARAMS_PADRAO["modo_servico"])
    if "nivel_servico_classe" in mudancas and "modo_servico" not in mudancas and modo != "classe":
        novo["modo_servico"] = "classe"
        obs.append("O nível por classe só vale com a política 'classe'; a política foi trocada para 'classe'.")
    if "nivel_servico_uniforme" in mudancas and "modo_servico" not in mudancas and modo != "uniforme":
        novo["modo_servico"] = "uniforme"
        obs.append("O nível uniforme só vale com a política 'uniforme'; a política foi trocada para 'uniforme'.")
    return analise.mesclar_params(novo), obs


# ---------------------------------------------------------------------------
# Contexto de uma pergunta (planos calculados, cenários e rascunhos)
# ---------------------------------------------------------------------------

def _r(x, casas=0):
    return None if x is None else round(float(x), casas)


class Contexto:
    def __init__(self, base: dict | None, params: dict | None):
        self.base = base or analise.base_padrao()
        self.params = analise.mesclar_params(params or {})
        self._atual = None
        self.cenarios: list[dict] = []
        self._res_cenario: dict[str, dict] = {}
        self.ordens: list[dict] = []
        self._sk = None

    @property
    def atual(self) -> dict:
        if self._atual is None:
            self._atual = analise.calcular(self.base, self.params)
        return self._atual

    def skus(self) -> dict[str, dict]:
        if self._sk is None:
            self._sk = {s["sku"]: s for s in self.base["skus"]}
        return self._sk

    def resultado(self, cenario_id: str | None) -> tuple[dict, str]:
        if not cenario_id or cenario_id == "atual":
            return self.atual, "atual"
        if cenario_id not in self._res_cenario:
            ids = ", ".join(self._res_cenario) or "nenhum (chame recalcular_plano antes)"
            raise ErroFerramenta(f"cenário '{cenario_id}' não existe nesta pergunta. Cenários: {ids}")
        return self._res_cenario[cenario_id], cenario_id


def resumo(res: dict) -> dict:
    k = res["kpis"]
    tot = next((x for x in res["servico"]["projetado"] if x["classe"] == "Total"), {})
    return {
        "orcamento": _r(k["orcamento"]),
        "plano_custo": _r(k["plano_custo"]),
        "plano_pedidos": k["plano_pedidos"],
        "plano_skus": k["plano_skus"],
        "pedidos_cortados_orcamento": k["pedidos_cortados"],
        "pedidos_reduzidos_orcamento": k["pedidos_reduzidos"],
        "receita_em_risco_horizonte": _r(k["ruptura_proj_plano"]),
        "receita_em_risco_sem_comprar": _r(k["ruptura_proj_sem_compra"]),
        "receita_protegida": _r(k["receita_protegida"]),
        "fill_rate_projetado": tot.get("plano"),
        "margem_por_real_no_corte": k["corte_margem_por_real"],
        "compra_economica_completa": _r(k["necessidade_economica"]),
        "estoque_seguranca_custo": _r(k["es_custo"]),
        "capital_parado": _r(k["capital_parado"]),
        "horizonte": f"{k['inicio_horizonte']} a {k['fim_horizonte']}",
    }


def _diferenca(a: dict, b: dict) -> dict:
    out = {}
    for c in ("plano_custo", "plano_pedidos", "plano_skus", "receita_em_risco_horizonte", "receita_protegida",
              "fill_rate_projetado", "estoque_seguranca_custo"):
        if a.get(c) is not None and b.get(c) is not None:
            out[c] = round(b[c] - a[c], 4 if c == "fill_rate_projetado" else 0)
    return out


def _por_sku(res: dict) -> dict[str, dict]:
    d: dict[str, dict] = {}
    for l in res["plano"]:
        if l["qtd_un"] <= 0:
            continue
        x = d.setdefault(l["sku"], {"sku": l["sku"], "descricao": l["descricao"], "fornecedor": l["fornecedor"],
                                    "classe": l["classe"], "custo": 0.0, "qtd_un": 0, "pedidos": 0})
        x["custo"] += l["custo"]
        x["qtd_un"] += l["qtd_un"]
        x["pedidos"] += 1
    return d


def comparar(a: dict, b: dict, limite: int = 8) -> dict:
    """Plano a (atual) contra plano b (cenário), por SKU."""
    sa, sb = _por_sku(a), _por_sku(b)
    ra, rb = a.get("risco_por_sku", {}), b.get("risco_por_sku", {})

    def item(sku):
        x = sa.get(sku) or sb.get(sku)
        return {"sku": sku, "descricao": x["descricao"], "fornecedor": x["fornecedor"], "classe": x["classe"],
                "custo_antes": _r(sa.get(sku, {}).get("custo", 0)), "custo_depois": _r(sb.get(sku, {}).get("custo", 0)),
                "receita_em_risco_antes": ra.get(sku, 0), "receita_em_risco_depois": rb.get(sku, 0)}

    saiu = [item(s) for s in sa if s not in sb]
    entrou = [item(s) for s in sb if s not in sa]
    reduziu = [item(s) for s in sa if s in sb and sb[s]["custo"] < sa[s]["custo"] - 0.5]
    aumentou = [item(s) for s in sa if s in sb and sb[s]["custo"] > sa[s]["custo"] + 0.5]
    for lst in (saiu, reduziu):
        lst.sort(key=lambda r: -(r["receita_em_risco_depois"] - r["receita_em_risco_antes"]) or -r["custo_antes"])
    for lst in (entrou, aumentou):
        lst.sort(key=lambda r: -r["custo_depois"])

    def tot(lst, c):
        return _r(sum(r[c] for r in lst))
    return {
        "skus_que_sairam": len(saiu), "custo_que_saiu": tot(saiu, "custo_antes"),
        "receita_em_risco_dos_que_sairam": {"antes": tot(saiu, "receita_em_risco_antes"), "depois": tot(saiu, "receita_em_risco_depois")},
        "skus_que_entraram": len(entrou), "custo_que_entrou": tot(entrou, "custo_depois"),
        "skus_reduzidos": len(reduziu), "skus_aumentados": len(aumentou),
        "sairam": saiu[:limite], "entraram": entrou[:limite], "reduzidos": reduziu[:limite], "aumentados": aumentou[:limite],
    }


def _rs(x: float) -> str:
    return f"R$ {x:.2f}".replace(".", ",")


def _explicar_linha(l: dict, res: dict) -> str:
    st, corte = l["status"], res["kpis"]["corte_margem_por_real"]
    corte_txt = _rs(corte) if corte is not None else "sem lote dentro do orçamento"
    if st == "mantido":
        return (f"Pedido mantido inteiro: protege {_rs(l['margem_por_real'])} de margem por real investido; "
                f"o último lote que coube no orçamento protege {corte_txt}.")
    if st.startswith("cortado"):
        return (f"Cortado pelo orçamento: o primeiro lote deste pedido protegeria {_rs(l['margem_por_real'])} de margem por real, "
                f"e o orçamento acabou antes de chegar nele (o último lote que entrou protege {corte_txt}). "
                "Com orçamento limitado, entram antes os lotes que protegem mais margem por real investido.")
    if st.startswith("reduzido (orç"):
        return (f"Reduzido pelo orçamento: ficaram {l['qtd_un']} de {l['qtd_sugerida_un']} un sugeridas. "
                f"Os lotes de cima protegiam menos margem por real que o corte ({corte_txt}).")
    if st == "não econômico":
        dias = l["qtd_sugerida_un"] / l["demanda_dia"] if l["demanda_dia"] else None
        return ("Não econômico: o lote mínimo (MOQ) custa mais para carregar (capital + armazenagem) do que a margem que ele protege"
                + (f"; o pedido sugerido de {l['qtd_sugerida_un']} un cobre cerca de {dias:.0f} dias de venda" if dias else "")
                + ". Não entra nem com orçamento sobrando.")
    return f"{st}: os lotes acima de {l['qtd_un']} un não pagam o custo de carregar."


# ---------------------------------------------------------------------------
# Ferramentas
# ---------------------------------------------------------------------------

def f_recalcular_plano(ctx: Contexto, mudancas: Any = None, **_):
    m = validar_mudancas(mudancas)
    novos, obs = aplicar_mudancas(ctx.params, m)
    try:
        res = analise.calcular(ctx.base, novos)
    except Exception as e:  # o motor recusou a combinação
        raise ErroFerramenta(f"o motor não conseguiu calcular com esses parâmetros: {e}")
    cid = f"C{len(ctx.cenarios) + 1}"
    ctx._res_cenario[cid] = res
    antes, depois = resumo(ctx.atual), resumo(res)
    cmp_ = comparar(ctx.atual, res, limite=5)
    ctx.cenarios.append({"id": cid, "mudancas": m, "params": novos, "antes": antes, "depois": depois})
    return {"cenario_id": cid, "mudancas": m, "observacoes": obs, "plano_atual": antes, "cenario": depois,
            "diferenca_cenario_menos_atual": _diferenca(antes, depois), "comparacao_por_sku": cmp_,
            "nota": "Use comparar_cenarios para a lista completa; o usuário pode aplicar este cenário no painel."}


def f_comparar_cenarios(ctx: Contexto, cenario_id: str | None = None, limite: int = 15, **_):
    limite = int(_numero(limite, "limite", 1, 40, inteiro=True))
    if not cenario_id or cenario_id == "atual":
        # sem cenário: o que o orçamento atual deixou de fora
        fora = [l for l in ctx.atual["plano"] if l["status"].startswith(("cortado", "reduzido (orç"))]
        risco = ctx.atual.get("risco_por_sku", {})
        fora.sort(key=lambda l: -risco.get(l["sku"], 0))
        return {"plano": "atual", "pedidos_fora_ou_reduzidos_pelo_orcamento": len(fora),
                "custo_que_ficou_de_fora": _r(sum(l["custo_sugerido"] - l["custo"] for l in fora)),
                "itens": [{"sku": l["sku"], "descricao": l["descricao"], "fornecedor": l["fornecedor"], "classe": l["classe"],
                           "status": l["status"], "data_emissao": l["data_emissao"],
                           "custo_no_plano": _r(l["custo"]), "custo_sugerido": _r(l["custo_sugerido"]),
                           "margem_por_real": l["margem_por_real"], "receita_em_risco_sku": risco.get(l["sku"], 0)}
                          for l in fora[:limite]],
                "corte_margem_por_real": ctx.atual["kpis"]["corte_margem_por_real"]}
    res, cid = ctx.resultado(cenario_id)
    c = next(x for x in ctx.cenarios if x["id"] == cid)
    return {"cenario_id": cid, "mudancas": c["mudancas"], "plano_atual": c["antes"], "cenario": c["depois"],
            "diferenca_cenario_menos_atual": _diferenca(c["antes"], c["depois"]),
            "comparacao_por_sku": comparar(ctx.atual, res, limite=limite)}


def f_consultar_sku(ctx: Contexto, sku: str = "", cenario_id: str | None = None, **_):
    if not isinstance(sku, str) or not sku.strip():
        raise ErroFerramenta("informe 'sku' (código, ex.: KSH-1003, ou parte do nome)")
    q = sku.strip()
    todos = ctx.skus()
    s = todos.get(q.upper()) or todos.get(q)
    if s is None:
        achados = [x for x in todos.values() if q.lower() in (x["sku"] + " " + x["descricao"]).lower()]
        if len(achados) != 1:
            return {"encontrados": len(achados), "candidatos": [{"sku": x["sku"], "descricao": x["descricao"], "status": x["status"]}
                                                                 for x in achados[:8]],
                    "nota": "Mais de um ou nenhum SKU bate com a busca; chame de novo com o código exato."}
        s = achados[0]
    res, cid = ctx.resultado(cenario_id)
    linhas = [l for l in res["plano"] if l["sku"] == s["sku"]]
    transito = [a for a in res["abertas"] if a["sku"] == s["sku"]]
    parado = next((p for p in res["parado"] if p["sku"] == s["sku"]), None)
    d = s["base_d"]
    estoque = s["estoque"]
    em_transito = sum(a["qtd_un"] for a in transito)
    if s["status"] != "ativo":
        motivo = "SKU descontinuado: não é recomprado; o estoque que sobra entra em estoque parado."
    elif not s.get("cadastro_ok", True):
        motivo = "Cadastro incompleto (custo, preço ou MOQ): o SKU fica fora do plano até corrigir."
    elif not linhas:
        motivo = ("Sem pedido no horizonte: a posição (estoque + em trânsito) cobre a demanda prevista mais o estoque de segurança."
                  if d > 0 else "Sem demanda recente medida, então não há pedido.")
    else:
        motivo = " | ".join(_explicar_linha(l, res) for l in linhas)
    out = {
        "plano_consultado": cid,
        "sku": s["sku"], "descricao": s["descricao"], "marca": s["marca"], "categoria": s["categoria"],
        "status_cadastro": s["status"], "fornecedor": s["fornecedor"],
        "custo_unitario": s["custo"], "preco_venda": s["preco"],
        "unidade_compra": s["unidade_compra"], "moq": s["moq"], "moq_em_unidades": s["moq_un"],
        "demanda_corrigida_dia": _r(d, 2), "media_simples_90d_dia": _r(s["media_simples_90d"], 2),
        "estoque_un": estoque, "em_transito_un": em_transito,
        "cobertura_dias_estoque_mais_transito": _r((estoque + em_transito) / d) if d > 0 else None,
        "ruptura_12m": {"dias": s["dias_ruptura_365d"], "receita_perdida": _r(s["perda_receita_365d"])},
        "receita_em_risco_horizonte": res.get("risco_por_sku", {}).get(s["sku"], 0),
        "pedidos_no_plano": [{"data_emissao": l["data_emissao"], "data_chegada": l["data_chegada"], "status": l["status"],
                              "qtd_compra": l["qtd_compra"], "unidade_compra": l["unidade_compra"], "qtd_un": l["qtd_un"],
                              "custo": _r(l["custo"]), "qtd_sugerida_un": l["qtd_sugerida_un"],
                              "custo_sugerido": _r(l["custo_sugerido"]), "margem_por_real": l["margem_por_real"],
                              "classe": l["classe"], "nivel_servico": l["nivel_servico"], "urgente": l["urgente"]}
                             for l in linhas],
        "corte_do_plano_margem_por_real": res["kpis"]["corte_margem_por_real"],
        "por_que": motivo,
        "pedidos_em_aberto": [{"po_id": a["po_id"], "qtd_un": a["qtd_un"], "data_pedido": a["data_pedido"],
                               "chegada_prevista": a["chegada_prevista"], "atrasado": a["atrasado"]} for a in transito],
    }
    if parado:
        out["estoque_parado"] = {k: parado[k] for k in ("tipo", "cobertura_dias", "excesso_custo", "liquidar_un",
                                                         "preco_liquidacao", "valor_recuperavel", "acao")}
    return out


def _achar_fornecedor(ctx: Contexto, fornecedor: Any) -> str:
    if not isinstance(fornecedor, str) or not fornecedor.strip():
        raise ErroFerramenta("informe 'fornecedor' (código F01..F06 ou parte do nome)")
    nomes = ctx.base["fornecedores"]
    q = fornecedor.strip()
    if q.upper() in nomes:
        return q.upper()
    achados = [k for k, v in nomes.items() if q.lower() in v.lower()]
    if len(achados) == 1:
        return achados[0]
    raise ErroFerramenta(f"fornecedor '{q}' não encontrado. Existem: " + ", ".join(f"{k} ({v})" for k, v in nomes.items()))


def f_consultar_fornecedor(ctx: Contexto, fornecedor: str = "", **_):
    fz = _achar_fornecedor(ctx, fornecedor)
    res = ctx.atual
    lt = next((x for x in res["lead_times"] if x["fornecedor"] == fz), None)
    abertas = [a for a in res["abertas"] if a["fornecedor"] == fz]
    plano = [l for l in res["plano"] if l["fornecedor"] == fz and l["qtd_un"] > 0]
    return {
        "fornecedor": fz, "nome": ctx.base["fornecedores"].get(fz, fz),
        "lead_time_dias": None if lt is None else {k: lt[k] for k in ("pedidos", "media", "p50", "p90", "p95", "max", "cv")},
        "custo_da_variabilidade_do_lead_time": None if lt is None else lt["custo_variabilidade"],
        "skus": None if lt is None else lt["skus"],
        "receita_perdida_ruptura_12m": None if lt is None else lt["perda_receita_12m"],
        "pedidos_em_aberto": len(abertas), "pedidos_em_aberto_atrasados": sum(1 for a in abertas if a["atrasado"]),
        "custo_em_aberto": _r(sum(a["custo"] for a in abertas)),
        "lista_em_aberto": [{"po_id": a["po_id"], "sku": a["sku"], "quantidade": a["quantidade"], "unidade": a["unidade"],
                             "data_pedido": a["data_pedido"], "idade_dias": a["idade"], "chegada_prevista": a["chegada_prevista"],
                             "atrasado": a["atrasado"], "custo": _r(a["custo"])} for a in abertas[:15]],
        "plano_no_horizonte": {"pedidos": len(plano), "custo": _r(sum(l["custo"] for l in plano)),
                               "urgentes": sum(1 for l in plano if l["urgente"]),
                               "primeira_emissao": min((l["data_emissao"] for l in plano), default=None)},
    }


def f_estoque_parado(ctx: Contexto, tipo: str | None = None, limite: int = 10, **_):
    limite = int(_numero(limite, "limite", 1, 30, inteiro=True))
    if tipo not in (None, "", "descontinuado", "excesso", "sem_giro"):
        raise ErroFerramenta("'tipo' precisa ser descontinuado, excesso ou sem_giro (ou vazio para todos)")
    res = ctx.atual
    k, p = res["kpis"], res["parametros"]
    itens = [x for x in res["parado"] if not tipo or x["tipo"] == tipo]
    return {
        "capital_parado": k["capital_parado"], "detalhe": k["capital_parado_detalhe"],
        "custo_de_carregar_ao_ano": k["custo_carregar_parado_ano"],
        "liquidacao": k["liquidacao"], "desconto_maximo": p["desconto_max_liquidacao"],
        "regra": f"excesso acima de {p['dias_excesso']:.0f} dias de cobertura; liquidar acima de {p['giro_max_dias']:.0f} dias e todos os descontinuados",
        "pedidos_em_aberto_de_skus_em_excesso": {"qtd": k["po_em_excesso_qtd"], "custo": k["po_em_excesso_custo"]},
        "itens_no_filtro": len(itens),
        "maiores": [{c: x[c] for c in ("sku", "descricao", "tipo", "estoque", "cobertura_dias", "excesso_custo",
                                       "liquidar_un", "preco_liquidacao", "valor_recuperavel", "acao")} for x in itens[:limite]],
    }


def f_rascunho_ordem_compra(ctx: Contexto, fornecedor: str = "", de: str | None = None, ate: str | None = None,
                            cenario_id: str | None = None, **_):
    fz = _achar_fornecedor(ctx, fornecedor)
    res, cid = ctx.resultado(cenario_id)
    ini = _data_iso(de, "de") if de else res["parametros"]["inicio_horizonte"]
    fim = _data_iso(ate, "ate") if ate else (date.fromisoformat(ini) + timedelta(days=6)).isoformat()
    if fim < ini or (date.fromisoformat(fim) - date.fromisoformat(ini)).days > 120:
        raise ErroFerramenta("janela inválida: 'ate' precisa ser depois de 'de' e no máximo 120 dias depois")
    sk = ctx.skus()
    do_forn = [l for l in res["plano"] if l["fornecedor"] == fz and l["qtd_un"] > 0]
    na_janela = [l for l in do_forn if ini <= l["data_emissao"] <= fim]
    aviso = None
    if not na_janela and not de and not ate:
        # nada a emitir na 1ª semana: o rascunho vai para a semana da próxima emissão do fornecedor
        prox = min((l["data_emissao"] for l in do_forn if l["data_emissao"] > fim), default=None)
        if prox:
            aviso = (f"Nenhum pedido de {fz} a emitir de {ini} a {fim}. O rascunho cobre a semana da próxima emissão "
                     f"do fornecedor, que é {prox}.")
            ini, fim = prox, (date.fromisoformat(prox) + timedelta(days=6)).isoformat()
            na_janela = [l for l in do_forn if ini <= l["data_emissao"] <= fim]
    depois = [l for l in do_forn if l["data_emissao"] > fim]
    itens = []
    for l in sorted(na_janela, key=lambda l: (l["data_emissao"], l["sku"])):
        fator = sk.get(l["sku"], {}).get("fator_un", 1) or 1
        itens.append({"sku": l["sku"], "descricao": l["descricao"], "quantidade": l["qtd_compra"],
                      "unidade_compra": l["unidade_compra"], "moq": l["moq"],
                      "lotes_de_moq": round(l["qtd_compra"] / l["moq"], 2) if l["moq"] else None,
                      "unidades": l["qtd_un"], "custo_unitario_compra": round(sk.get(l["sku"], {}).get("custo", 0) * fator, 2),
                      "custo": _r(l["custo"], 2), "data_emissao": l["data_emissao"], "chegada_prevista": l["data_chegada"],
                      "urgente": l["urgente"], "status_no_plano": l["status"]})
    ordem = {
        "numero": f"RASCUNHO-{fz}-{ini.replace('-', '')}",
        "status": "pendente de aprovação humana",
        "enviada": False,
        "fornecedor": fz, "nome_fornecedor": ctx.base["fornecedores"].get(fz, fz),
        "plano": cid, "janela_emissao": {"de": ini, "ate": fim},
        "itens": itens, "total_itens": len(itens), "total_custo": _r(sum(i["custo"] for i in itens), 2),
        "pedidos_do_fornecedor_depois_da_janela": {"qtd": len(depois), "custo": _r(sum(l["custo"] for l in depois))},
        "aviso_janela": aviso,
        "observacao": ("Rascunho gerado a partir do plano. Nada foi enviado ao fornecedor nem ao ERP: "
                       "Compras precisa revisar e aprovar antes de emitir."),
    }
    ctx.ordens.append(ordem)
    return ordem


FUNCOES: dict[str, Callable] = {
    "recalcular_plano": f_recalcular_plano,
    "comparar_cenarios": f_comparar_cenarios,
    "consultar_sku": f_consultar_sku,
    "consultar_fornecedor": f_consultar_fornecedor,
    "estoque_parado": f_estoque_parado,
    "rascunho_ordem_compra": f_rascunho_ordem_compra,
}


def _ferramenta(nome, descricao, props, obrig=()):
    return {"type": "function", "function": {"name": nome, "description": descricao,
                                             "parameters": {"type": "object", "properties": props, "required": list(obrig)}}}


_PROPS_MUDANCAS = {}
for _k, (_t, _a, _b, _d) in REGRAS.items():
    if _t == "num":
        _PROPS_MUDANCAS[_k] = {"type": "number", "description": _d}
    elif _t == "int":
        _PROPS_MUDANCAS[_k] = {"type": "integer", "description": _d}
    elif _t == "enum":
        _PROPS_MUDANCAS[_k] = {"type": "string", "enum": list(_a), "description": _d}
    elif _t == "data":
        _PROPS_MUDANCAS[_k] = {"type": "string", "description": _d}
    elif _t == "classe":
        _PROPS_MUDANCAS[_k] = {"type": "object", "description": _d,
                               "properties": {c: {"type": "number"} for c in "ABC"}}
    else:
        _PROPS_MUDANCAS[_k] = {"type": "array", "description": _d, "items": {"type": "object"}}

FERRAMENTAS = [
    _ferramenta("recalcular_plano",
                "Recalcula o plano de compras de 90 dias com parâmetros alterados a partir dos atuais do painel e compara com o "
                "plano atual (custo, pedidos, receita em risco, fill rate, SKUs que saíram/entraram). Use para toda pergunta 'e se'. "
                "Passe só o que muda. Percentuais em fração (99,5% = 0.995); reais sem abreviação (R$ 1,5 mi = 1500000).",
                {"mudancas": {"type": "object", "properties": _PROPS_MUDANCAS, "description": "parâmetros que mudam"}},
                ["mudancas"]),
    _ferramenta("comparar_cenarios",
                "Lista completa do que saiu, entrou, foi reduzido ou aumentado num cenário calculado, por SKU, com R$ e receita em "
                "risco. Sem cenario_id: lista o que o orçamento ATUAL deixou de fora (pedidos cortados ou reduzidos).",
                {"cenario_id": {"type": "string", "description": "ex.: C1 (devolvido por recalcular_plano); vazio = plano atual"},
                 "limite": {"type": "integer", "description": "itens por lista (1 a 40, padrão 15)"}}),
    _ferramenta("consultar_sku",
                "Ficha de um SKU: cadastro, demanda corrigida, estoque, trânsito, pedidos no plano com status (mantido, cortado, "
                "reduzido, não econômico) e o motivo, receita em risco e estoque parado. Use para 'por que o SKU X saiu/entrou'.",
                {"sku": {"type": "string", "description": "código (ex.: KSH-1003) ou parte do nome"},
                 "cenario_id": {"type": "string", "description": "opcional: consultar o SKU num cenário (ex.: C1)"}},
                ["sku"]),
    _ferramenta("consultar_fornecedor",
                "Fornecedor: lead time (média, mediana, p90, p95, CV), custo da variabilidade, pedidos em aberto (chegada prevista, "
                "atrasados) e pedidos dele no plano atual.",
                {"fornecedor": {"type": "string", "description": "código F01..F06 ou parte do nome"}}, ["fornecedor"]),
    _ferramenta("estoque_parado",
                "Capital parado (descontinuados, excesso, sem giro), custo de carregar, o que liquidar e quanto volta.",
                {"tipo": {"type": "string", "enum": ["descontinuado", "excesso", "sem_giro"]},
                 "limite": {"type": "integer", "description": "quantos itens (1 a 30, padrão 10)"}}),
    _ferramenta("rascunho_ordem_compra",
                "Monta o RASCUNHO da ordem de compra de um fornecedor com os pedidos do plano a emitir na janela (padrão: os 7 "
                "primeiros dias do horizonte), na unidade de compra e em múltiplos do MOQ. Fica pendente de aprovação humana; "
                "não envia nada.",
                {"fornecedor": {"type": "string", "description": "código F01..F06 ou parte do nome"},
                 "de": {"type": "string", "description": "AAAA-MM-DD; padrão = início do horizonte"},
                 "ate": {"type": "string", "description": "AAAA-MM-DD; padrão = de + 6 dias"},
                 "cenario_id": {"type": "string", "description": "opcional: usar o plano de um cenário (ex.: C1)"}},
                ["fornecedor"]),
]


def executar(ctx: Contexto, nome: str, args: Any) -> dict:
    """Roda uma ferramenta. Qualquer erro vira {"erro": ...} para a IA corrigir ou explicar."""
    f = FUNCOES.get(nome)
    if f is None:
        return {"erro": f"ferramenta '{nome}' não existe. Use: {', '.join(FUNCOES)}"}
    if not isinstance(args, dict):
        return {"erro": "os argumentos precisam ser um objeto JSON"}
    try:
        return analise.para_json(f(ctx, **args))
    except ErroFerramenta as e:
        return {"erro": str(e)}
    except TypeError as e:
        return {"erro": f"argumentos inválidos para {nome}: {e}"}
    except Exception as e:  # nada derruba a rota
        return {"erro": f"falha interna em {nome}: {type(e).__name__}"}


# ---------------------------------------------------------------------------
# Chamada à IA (API compatível com OpenAI, via urllib)
# ---------------------------------------------------------------------------

def chamar_ia(mensagens: list[dict], ferramentas: list[dict], cfg: Config, timeout: float,
              escolha: str = "auto") -> dict:
    corpo = {"model": cfg.modelo, "messages": mensagens, "max_tokens": MAX_TOKENS, "temperature": 0.1,
             "tools": ferramentas, "tool_choice": escolha}
    req = urllib.request.Request(f"{cfg.base_url}/chat/completions", data=json.dumps(corpo).encode("utf-8"), method="POST",
                                 # User-Agent explícito: o Cloudflare do gateway recusa o padrão do urllib (erro 1010)
                                 headers={"Authorization": f"Bearer {cfg.chave}", "Content-Type": "application/json",
                                          "User-Agent": "estoque-gobeaute-agente/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            dados = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        motivo = {401: "a chave foi recusada", 403: "acesso negado", 429: "o serviço de IA está no limite de uso",
                  400: "o serviço de IA recusou a requisição"}.get(e.code, "o serviço de IA está com problema")
        raise ErroIA(f"Não consegui falar com a IA: {motivo} (HTTP {e.code}). Tente de novo em instantes.") from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError):
        raise ErroIA("A IA não respondeu a tempo. Tente de novo ou faça uma pergunta mais direta.") from None
    except (ValueError, UnicodeDecodeError):
        raise ErroIA("A IA devolveu uma resposta que não consegui ler. Tente de novo.") from None
    try:
        return dados["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise ErroIA("A IA devolveu uma resposta vazia. Tente de novo.") from None


def _texto(conteudo: Any) -> str:
    if isinstance(conteudo, str):
        return conteudo
    if isinstance(conteudo, list):  # alguns modelos devolvem partes
        return "".join(p.get("text", "") for p in conteudo if isinstance(p, dict))
    return ""


# ---------------------------------------------------------------------------
# Prompt e loop
# ---------------------------------------------------------------------------

PROMPT_SISTEMA = """Você é o Agente de Compras da GoBeauté. Ajuda Compras e a diretoria a decidir o plano de compras de 90 dias \
dentro do orçamento, usando o mesmo motor de cálculo da tela (demanda corrigida por ruptura, lead time real, MOQ, \
orçamento cortando primeiro o que protege menos margem por real).

REGRAS INEGOCIÁVEIS
1. Só cite números (R$, unidades, %, dias, contagens, datas) que vieram de uma ferramenta nesta pergunta. Se nenhuma \
ferramenta trouxe o número, chame a ferramenta certa ou diga que não tem esse dado. Não estime de cabeça. Prefira os \
campos já calculados (ex.: diferenca_cenario_menos_atual) a fazer contas.
2. Pergunta "e se" (orçamento, nível de serviço, campanha, critério de corte...): chame recalcular_plano só com o que \
muda. Percentual em fração (99,5% = 0.995). Reais sem abreviação (R$ 1,5 mi = 1500000). Depois diga que o usuário \
pode clicar em "Aplicar este cenário" para levar os parâmetros ao painel.
3. "Por que o SKU X saiu/entrou": use consultar_sku (com cenario_id se a pergunta é sobre um cenário) e explique pelo \
campo por_que, com a margem por real do pedido e o corte do plano.
4. Ordem de compra: use rascunho_ordem_compra. É SEMPRE um rascunho pendente de aprovação humana. Nunca diga que foi \
emitida, enviada ou aprovada; você não tem como enviar nada a fornecedor ou ERP. A tela mostra a tabela da OC; no texto, \
resuma (itens, total, janela, urgentes) sem repetir item por item.
5. Se uma ferramenta devolver "erro", corrija o argumento e tente de novo uma vez; se não der, explique o erro.
6. Se a pergunta não for sobre estoque, compras, fornecedores ou o plano, diga em uma frase o que você sabe fazer.

FORMATO
- Português do Brasil, direto. Primeira frase: a resposta com o número principal. Depois até 5 tópicos curtos com "- ". \
Feche com uma recomendação de uma linha. Use **negrito** só nos números-chave. Sem tabelas e sem títulos com #.
- Valores: R$ 1,37 mi, R$ 245 mil, R$ 12.300; percentuais com vírgula (87,3%).
- Memorando para a diretoria (quando pedirem): até 150 palavras, com Contexto, Números, Recomendação e Decisão pedida.
"""


def _contexto_msg(ctx: Contexto) -> str:
    p = ctx.params
    k = ctx.base.get("data_ref")
    parcial = {c: p[c] for c in ("orcamento", "inicio_horizonte", "dias_horizonte", "modo_servico", "nivel_servico_uniforme",
                                 "nivel_servico_classe", "criterio_corte", "cobertura_ate", "custo_capital_aa", "armazenagem_am",
                                 "desconto_max_liquidacao")}
    parcial["campanhas"] = p["campanhas"]
    fonte = "CSVs enviados pelo usuário" if ctx.base.get("arquivos_enviados") else "dados originais"
    return (f"Dados: {fonte}, referência {k}. Fornecedores: "
            + ", ".join(f"{a} = {b}" for a, b in ctx.base["fornecedores"].items())
            + f". Parâmetros atuais do painel (o 'plano atual'): {json.dumps(parcial, ensure_ascii=False)}")


def _historico_limpo(historico: Any) -> list[dict]:
    out = []
    if not isinstance(historico, list):
        return out
    for h in historico[-MAX_HISTORICO:]:
        if isinstance(h, dict) and h.get("role") in ("user", "assistant") and isinstance(h.get("content"), str):
            out.append({"role": h["role"], "content": h["content"][:MAX_CHARS_MSG]})
    return out


def _brl(x: float | None, casas: int = 0) -> str:
    if x is None:
        return "-"
    return "R$ " + f"{x:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _resumo_ferramenta(nome: str, r: dict) -> str:
    if "erro" in r:
        return "erro: " + r["erro"][:160]
    if nome == "recalcular_plano":
        c = r["cenario"]
        return (f"{r['cenario_id']}: {c['plano_pedidos']} pedidos, {_brl(c['plano_custo'])}, "
                f"receita em risco {_brl(c['receita_em_risco_horizonte'])}")
    if nome == "rascunho_ordem_compra":
        return f"{r['numero']}: {r['total_itens']} itens, {_brl(r['total_custo'], 2)} ({r['status']})"
    if nome == "consultar_sku" and "sku" in r:
        st = ", ".join(sorted({l["status"] for l in r["pedidos_no_plano"]})) or "sem pedido"
        return f"{r['sku']} ({r['descricao']}): {st}"
    if nome == "consultar_fornecedor":
        return f"{r['fornecedor']}: {r['pedidos_em_aberto']} pedidos em aberto, {r['plano_no_horizonte']['pedidos']} no plano"
    return "ok"


def responder(pergunta: Any, historico: Any = None, params: dict | None = None, base: dict | None = None,
              chamar: Callable | None = None) -> dict:
    """Uma pergunta, um loop de até MAX_PASSOS chamadas à IA com ferramentas. Nunca levanta exceção."""
    cfg = config()
    if not cfg.ligado:
        return {"ok": False, "desligado": True,
                "erro": "O agente de IA está desligado: falta a variável AI_API_KEY no servidor. O resto do app funciona normalmente."}
    if not isinstance(pergunta, str) or not pergunta.strip():
        return {"ok": False, "erro": "Escreva uma pergunta."}
    pergunta = pergunta.strip()
    if len(pergunta) > MAX_PERGUNTA:
        return {"ok": False, "erro": f"A pergunta passou de {MAX_PERGUNTA} caracteres. Encurte e tente de novo."}
    chamar = chamar or chamar_ia
    try:
        ctx = Contexto(base if isinstance(base, dict) else None, params if isinstance(params, dict) else {})
        contexto = _contexto_msg(ctx)
    except Exception as e:
        return {"ok": False, "erro": f"Não consegui ler os parâmetros ou a base enviados: {type(e).__name__}."}

    msgs: list[dict] = [{"role": "system", "content": PROMPT_SISTEMA + "\nCONTEXTO\n" + contexto}]
    msgs += _historico_limpo(historico)
    msgs.append({"role": "user", "content": pergunta})
    usadas: list[dict] = []
    resposta, passos, limite = None, 0, False
    inicio = time.monotonic()
    try:
        for passo in range(MAX_PASSOS):
            restante = PRAZO_TOTAL_S - (time.monotonic() - inicio)
            if restante < 5:
                limite = True
                break
            ultimo = passo == MAX_PASSOS - 1
            msg = chamar(msgs, FERRAMENTAS, cfg, min(TIMEOUT_CHAMADA_S, restante), "none" if ultimo else "auto")
            passos += 1
            chamadas = (msg.get("tool_calls") or []) if not ultimo else []
            if not chamadas:
                resposta = _texto(msg.get("content")).strip()
                break
            chamadas = chamadas[:MAX_FERRAMENTAS_POR_PASSO]
            msgs.append({"role": "assistant", "content": _texto(msg.get("content")), "tool_calls": chamadas})
            for tc in chamadas:
                fn = tc.get("function") or {}
                nome = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (ValueError, TypeError):
                    args = None
                r = executar(ctx, nome, args) if args is not None else {"erro": "argumentos não são JSON válido"}
                usadas.append({"nome": nome, "args": args, "ok": "erro" not in r, "resumo": _resumo_ferramenta(nome, r)})
                msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": json.dumps(r, ensure_ascii=False)})
        else:
            limite = True
    except ErroIA as e:
        return analise.para_json({"ok": False, "erro": str(e), "ferramentas": usadas, "cenarios": ctx.cenarios,
                                  "ordens": ctx.ordens})
    except Exception as e:  # resposta da IA fora do formato esperado
        return {"ok": False, "erro": f"Falha inesperada no agente ({type(e).__name__}). Tente de novo.", "ferramentas": usadas}

    if not resposta:
        resposta = ("Cheguei ao limite de passos ou de tempo antes de fechar a resposta. "
                    "Os resultados das ferramentas abaixo já valem; refaça a pergunta de forma mais direta para eu concluir.")
    return analise.para_json({"ok": True, "resposta": resposta, "ferramentas": usadas, "cenarios": ctx.cenarios,
                              "ordens": ctx.ordens, "passos": passos, "limite_atingido": limite, "modelo": cfg.modelo})

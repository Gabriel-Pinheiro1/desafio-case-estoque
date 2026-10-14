"""Agente de compras com a IA simulada: nenhum teste chama a rede."""
import json

import pytest
from fastapi.testclient import TestClient

import agente
import analise
from app import app

cli = TestClient(app)


class IAFalsa:
    """Devolve, em ordem, as mensagens roteirizadas e guarda o que recebeu."""

    def __init__(self, roteiro):
        self.roteiro = list(roteiro)
        self.recebido = []

    def __call__(self, mensagens, ferramentas, cfg, timeout, escolha="auto"):
        self.recebido.append({"mensagens": json.loads(json.dumps(mensagens)), "escolha": escolha,
                              "ferramentas": [f["function"]["name"] for f in ferramentas]})
        return self.roteiro.pop(0) if self.roteiro else {"content": "fim"}


def chamada(nome, args, id_="t1"):
    return {"content": None, "tool_calls": [{"id": id_, "type": "function",
                                             "function": {"name": nome, "arguments": json.dumps(args)}}]}


def resultados_ferramenta(ia, passo):
    return [json.loads(m["content"]) for m in ia.recebido[passo]["mensagens"] if m["role"] == "tool"]


@pytest.fixture
def ligado(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "chave-de-teste")


def test_loop_de_ferramentas_recalcula_com_o_parametro_pedido(ligado):
    ia = IAFalsa([chamada("recalcular_plano", {"mudancas": {"orcamento": 1_370_000}}),
                  {"content": "Com R$ 1,37 mi o plano cai para 139 pedidos."}])
    out = agente.responder("e se o orçamento cair para R$ 1,37 mi?", [], {}, None, chamar=ia)
    assert out["ok"] and out["passos"] == 2
    assert out["resposta"].startswith("Com R$ 1,37 mi")
    assert [f["nome"] for f in out["ferramentas"]] == ["recalcular_plano"] and out["ferramentas"][0]["ok"]
    # o resultado da ferramenta que voltou para a IA é o mesmo do motor
    r = resultados_ferramenta(ia, 1)[0]
    esperado = analise.calcular(analise.base_padrao(), {"orcamento": 1_370_000})["kpis"]
    assert r["cenario"]["orcamento"] == 1_370_000
    assert r["cenario"]["plano_custo"] == round(esperado["plano_custo"])
    assert r["cenario"]["plano_pedidos"] == esperado["plano_pedidos"]
    assert r["cenario"]["receita_em_risco_horizonte"] == round(esperado["ruptura_proj_plano"])
    # o cenário volta para a tela com os parâmetros completos ("Aplicar este cenário")
    c = out["cenarios"][0]
    assert c["id"] == "C1" and c["mudancas"] == {"orcamento": 1_370_000}
    assert c["params"]["orcamento"] == 1_370_000 and "nivel_servico_classe" in c["params"]


def test_recalculo_parte_dos_parametros_atuais_do_painel(ligado):
    atuais = {"orcamento": 2_500_000, "modo_servico": "uniforme", "nivel_servico_uniforme": 0.95}
    ia = IAFalsa([chamada("recalcular_plano", {"mudancas": {"nivel_servico_classe": {"A": 0.995}}}), {"content": "ok"}])
    out = agente.responder("quanto custa subir a classe A para 99,5%?", [], atuais, None, chamar=ia)
    p = out["cenarios"][0]["params"]
    assert p["orcamento"] == 2_500_000                       # manteve o painel
    assert p["nivel_servico_classe"]["A"] == 0.995 and p["nivel_servico_classe"]["B"] == 0.98
    assert p["modo_servico"] == "classe"                      # o nível por classe só vale nesse modo
    assert resultados_ferramenta(ia, 1)[0]["observacoes"]


def test_parametro_invalido_da_ia_vira_erro_tratado_e_ela_corrige(ligado):
    ia = IAFalsa([chamada("recalcular_plano", {"mudancas": {"nivel_servico_classe": {"A": 99.5}}}, "a"),
                  chamada("recalcular_plano", {"mudancas": {"orçamento": 1}}, "b"),
                  chamada("recalcular_plano", {"mudancas": {"orcamento": "muito"}}, "c"),
                  chamada("ferramenta_inventada", {}, "d"),
                  {"content": None, "tool_calls": [{"id": "e", "function": {"name": "consultar_sku", "arguments": "{nao e json"}}]},
                  {"content": "Não consegui calcular."}])
    out = agente.responder("sobe a A para 99,5", [], {}, None, chamar=ia)
    assert out["ok"]
    assert [f["ok"] for f in out["ferramentas"]] == [False] * 5
    erros = [resultados_ferramenta(ia, i)[-1]["erro"] for i in range(1, 6)]
    assert "0.995" in erros[0]                  # a dica de fração volta para a IA
    assert "desconhecido" in erros[1]
    assert "número" in erros[2]
    assert "não existe" in erros[3]
    assert "JSON" in erros[4]
    assert out["cenarios"] == []


def test_limite_de_passos(ligado):
    ia = IAFalsa([chamada("consultar_fornecedor", {"fornecedor": "F05"}, f"x{i}") for i in range(10)])
    out = agente.responder("pergunta que nunca termina", [], {}, None, chamar=ia)
    assert len(ia.recebido) == agente.MAX_PASSOS
    assert ia.recebido[-1]["escolha"] == "none"          # último passo força a resposta em texto
    assert len(out["ferramentas"]) == agente.MAX_PASSOS - 1


def test_ordem_de_compra_fica_pendente_de_aprovacao(ligado):
    ia = IAFalsa([chamada("rascunho_ordem_compra", {"fornecedor": "F04"}), {"content": "Rascunho pronto."}])
    out = agente.responder("monte a ordem de compra do F04", [], {}, None, chamar=ia)
    oc = out["ordens"][0]
    assert oc["status"] == "pendente de aprovação humana" and oc["enviada"] is False
    assert oc["fornecedor"] == "F04" and oc["itens"]
    skus = {s["sku"]: s for s in analise.base_padrao()["skus"]}
    for it in oc["itens"]:
        s = skus[it["sku"]]
        assert s["fornecedor"] == "F04"
        assert it["quantidade"] % s["moq"] == 0                 # múltiplo do MOQ na unidade de compra
        assert it["unidades"] == it["quantidade"] * s["fator_un"]
        assert it["unidade_compra"] == s["unidade_compra"]
    assert oc["total_custo"] == pytest.approx(sum(i["custo"] for i in oc["itens"]), abs=0.01)


def test_por_que_o_sku_saiu_no_cenario():
    ctx = agente.Contexto(None, {})
    r = agente.executar(ctx, "recalcular_plano", {"mudancas": {"orcamento": 1_370_000}})
    saiu = r["comparacao_por_sku"]["sairam"][0]["sku"]
    ficha = agente.executar(ctx, "consultar_sku", {"sku": saiu, "cenario_id": "C1"})
    assert all(l["qtd_un"] == 0 for l in ficha["pedidos_no_plano"])
    assert "orçamento" in ficha["por_que"]
    assert "erro" in agente.executar(ctx, "consultar_sku", {"sku": saiu, "cenario_id": "C9"})


def test_erro_da_ia_vira_mensagem_legivel(ligado):
    def falha(*a, **k):
        raise agente.ErroIA("A IA não respondeu a tempo. Tente de novo.")
    out = agente.responder("e se o orçamento cair?", [], {}, None, chamar=falha)
    assert out["ok"] is False and "não respondeu" in out["erro"]


def test_chamar_ia_trata_http_e_nao_vaza_a_chave(monkeypatch):
    import io
    import urllib.error

    def http_401(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"bad key"))
    monkeypatch.setattr(agente.urllib.request, "urlopen", http_401)
    cfg = agente.Config(chave="segredo-123", base_url="https://exemplo.invalid/v1", modelo="m")
    with pytest.raises(agente.ErroIA) as e:
        agente.chamar_ia([], agente.FERRAMENTAS, cfg, 1)
    assert "401" in str(e.value) and "segredo-123" not in str(e.value)


def test_app_funciona_sem_chave():
    assert cli.get("/api/agente/status").json()["ligado"] is False
    r = cli.post("/api/agente", json={"pergunta": "e se o orçamento cair?"})
    assert r.status_code == 200
    assert r.json()["desligado"] is True and "AI_API_KEY" in r.json()["erro"]
    assert cli.post("/api/calcular", json={"params": {"orcamento": 1_000_000}}).status_code == 200


def test_rota_do_agente_com_ia_simulada_e_limites(ligado, monkeypatch):
    ia = IAFalsa([chamada("estoque_parado", {"limite": 3}), {"content": "R$ 2,14 mi parados."}])
    monkeypatch.setattr(agente, "chamar_ia", ia)
    monkeypatch.setattr("app.limitador", agente.LimitadorPorIP(limite=2))
    r = cli.post("/api/agente", json={"pergunta": "quanto está parado?", "historico": [{"role": "user", "content": "oi"}],
                                      "params": {}})
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert r.json()["ferramentas"][0]["nome"] == "estoque_parado"
    # pergunta grande demais
    assert cli.post("/api/agente", json={"pergunta": "x" * 5000}).json()["ok"] is False
    # terceira chamada no mesmo minuto: bloqueada
    assert cli.post("/api/agente", json={"pergunta": "de novo"}).status_code == 429
    # corpo grande demais
    assert cli.post("/api/agente", content=b"{" + b" " * (agente.MAX_CORPO_BYTES + 10) + b"}",
                    headers={"content-type": "application/json"}).status_code == 413


def test_limitador_libera_depois_da_janela():
    lim = agente.LimitadorPorIP(limite=1, janela_s=60)
    assert lim.permitir("1.1.1.1", agora=0) and not lim.permitir("1.1.1.1", agora=30)
    assert lim.permitir("2.2.2.2", agora=30)
    assert lim.permitir("1.1.1.1", agora=61)


def test_carregar_env_nao_sobrescreve(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comentario\nAI_MODEL=modelo-x\nAI_BASE_URL='https://a.invalid/v1'\n", encoding="utf-8")
    monkeypatch.delenv("AI_MODEL", raising=False)
    monkeypatch.setenv("AI_BASE_URL", "https://ja-definida.invalid/v1")
    agente.carregar_env(env)
    assert agente.config().modelo == "modelo-x"
    assert agente.config().base_url == "https://ja-definida.invalid/v1"

"""Os testes nunca chamam a rede nem usam a chave real da IA."""
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def sem_rede_e_sem_chave(monkeypatch):
    # app.py carrega o .env local no import; aqui a chave real sai do ambiente
    monkeypatch.delenv("AI_API_KEY", raising=False)

    def proibido(*a, **k):
        raise AssertionError("teste tentou acessar a rede")

    monkeypatch.setattr(urllib.request, "urlopen", proibido)
    yield

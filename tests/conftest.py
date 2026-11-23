"""Configuracao global de testes offline da Athena (executada antes da coleta do pytest).

- Define chaves dummy para `GOOGLE_API_KEY` e `GEMINI_API_KEY` antes de qualquer `import agentes`,
  permitindo rodar `.venv\\Scripts\\pytest.exe` sem `.env` carregado.
- Bloqueia requisicoes reais a modelos LLM (`models.ALLOW_MODEL_REQUESTS = False`) para garantir
  que nenhum teste offline faca chamada acidental de rede.
"""

from __future__ import annotations

import os

os.environ.setdefault("GOOGLE_API_KEY", "test-dummy-key")
os.environ.setdefault("GEMINI_API_KEY", "test-dummy-key")
os.environ["LOGFIRE_SEND_TO_LOGFIRE"] = "false"

import pydantic_ai.models as _pai_models

_pai_models.ALLOW_MODEL_REQUESTS = False

"""Conecta sua conta Google via OAuth local uma unica vez e gera token.json."""

import sys

from ferramentas_google import CREDENTIALS_PATH, SCOPES, TOKEN_PATH, conectar_conta_google


def main() -> int:
    if not CREDENTIALS_PATH.exists():
        print(f"[ERRO] Arquivo '{CREDENTIALS_PATH.name}' nao encontrado em: {CREDENTIALS_PATH.parent}")
        print("\nComo gerar o credentials.json:")
        print("1. Acesse https://console.cloud.google.com/ e crie/selecione um projeto.")
        print("2. Ative as APIs: Gmail API, Google Calendar API e Google Drive API.")
        print("3. Configure a Tela de Permissao OAuth (External -> adicione seu e-mail em Test users).")
        print("4. Em Credenciais -> Criar credenciais -> ID do cliente OAuth -> Aplicativo para computador (Desktop app).")
        print(f"5. Baixe o JSON e salve como: {CREDENTIALS_PATH}")
        print("6. Rode novamente: uv run python conectar_google.py")
        return 1

    print("Abrindo o navegador para autorizar o acesso ao Google Workspace...")
    print("Escopos solicitados:")
    for scope in SCOPES:
        print(f"  - {scope}")

    try:
        conectar_conta_google()
    except Exception as e:
        print(f"\n[ERRO] Falha ao concluir autenticacao Google: {e}")
        return 1

    print(f"\n[OK] Conta Google conectada! Token salvo em: {TOKEN_PATH}")
    print("O Agente Google ja pode consultar Gmail (e criar rascunhos), Agenda e Drive.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

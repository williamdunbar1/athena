@echo off
cd /d "%~dp0"
echo Ligando container sandbox-agentes...
podman machine start >nul 2>&1
podman start sandbox-agentes >nul 2>&1
echo Abrindo painel em http://127.0.0.1:8000 ...
start "" cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:8000"
uv run --env-file .env uvicorn servidor:app --host 0.0.0.0 --port 8000 --reload
pause
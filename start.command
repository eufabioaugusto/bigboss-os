#!/bin/bash
cd "$(dirname "$0")"

echo "========================================"
echo "  🚀 BigBoss OS"
echo "========================================"
echo ""

# Inicia com o ambiente virtual Python local
if [ -f ".venv/bin/python" ]; then
    PYTHON_BIN=".venv/bin/python"
else
    PYTHON_BIN="python3"
fi

echo "Iniciando servidor em http://localhost:7860 ..."

# Abre a janela no Chrome em modo app assim que o servidor responder
(
  for i in {1..20}; do
    sleep 1.5
    if curl -s http://127.0.0.1:7860/crm/stats > /dev/null 2>&1; then
      if [ -d "/Applications/Google Chrome.app" ]; then
        open -a "Google Chrome" --args --app=http://localhost:7860 --window-size=1380,880 --window-position=80,60
      else
        open "http://localhost:7860"
      fi
      break
    fi
  done
) &

$PYTHON_BIN server.py

#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "========================================"
echo "  BigBoss OS — Instalação"
echo "========================================"
echo ""

# ── Python ───────────────────────────────────────────────────────────────────
PYTHON=$(command -v python3 || command -v python)
if [ -z "$PYTHON" ]; then
  echo "ERRO: Python 3 não encontrado."
  echo "Instale em: https://www.python.org/downloads/"
  read -p "Pressione Enter para fechar..."
  exit 1
fi
echo "Python: $($PYTHON --version)"

# ── Virtualenv ────────────────────────────────────────────────────────────────
if [ ! -f "$DIR/.venv/bin/python" ]; then
  echo ""
  echo "Criando ambiente virtual..."
  $PYTHON -m venv "$DIR/.venv"
fi

VENV_PYTHON="$DIR/.venv/bin/python"
VENV_PIP="$DIR/.venv/bin/pip"

# ── Dependências Python ───────────────────────────────────────────────────────
echo ""
echo "Instalando dependências (pode levar alguns minutos)..."
$VENV_PIP install -q --upgrade pip
$VENV_PIP install -q -r "$DIR/requirements.txt"

# ── Playwright Chromium ───────────────────────────────────────────────────────
echo ""
echo "Instalando navegador para enriquecimento de leads..."
$VENV_PYTHON -m playwright install chromium 2>/dev/null || "$DIR/.venv/bin/playwright" install chromium

# ── Node.js / Claude CLI ──────────────────────────────────────────────────────
if ! command -v claude &>/dev/null; then
  echo ""
  if ! command -v npm &>/dev/null; then
    echo "AVISO: Node.js não encontrado. O Claude CLI será instalado durante o primeiro uso."
    echo "Instale Node.js em: https://nodejs.org"
  else
    echo "Instalando Claude CLI..."
    npm install -g @anthropic-ai/claude-code 2>/dev/null
  fi
fi

# ── LaunchAgent (servidor em background) ─────────────────────────────────────
echo ""
echo "Configurando servidor em background..."

PLIST="$HOME/Library/LaunchAgents/com.bigbossos.server.plist"
mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$DIR/data"

launchctl unload "$PLIST" 2>/dev/null || true

cat > "$PLIST" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.bigbossos.server</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV_PYTHON</string>
    <string>$DIR/server.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$DIR/data/server.log</string>
  <key>StandardErrorPath</key>
  <string>$DIR/data/server.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
  </dict>
</dict>
</plist>
PLIST_EOF

launchctl load "$PLIST"

echo ""
echo -n "Aguardando servidor iniciar"
for i in {1..30}; do
  sleep 1
  if curl -s http://localhost:7860 > /dev/null 2>&1; then
    echo ""
    echo ""
    echo "========================================"
    echo "  Instalação concluída!"
    echo ""
    echo "  Use o BigBoss OS.app para abrir."
    echo "========================================"
    echo ""
    read -p "Pressione Enter para fechar..."
    exit 0
  fi
  echo -n "."
done

echo ""
echo "AVISO: servidor demorou para responder."
echo "Verifique data/server.log se houver problemas."
read -p "Pressione Enter para fechar..."

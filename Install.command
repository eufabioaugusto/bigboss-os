#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"

clear
echo "================================================"
echo "  BigBoss OS — Instalação"
echo "  (isso só acontece uma vez)"
echo "================================================"
echo ""

# Remove quarantine de todos os arquivos
xattr -dr com.apple.quarantine "$DIR" 2>/dev/null
chmod +x "$DIR/BigBoss OS.app/Contents/MacOS/bigboss-os"

# Verifica Python
PYTHON=$(command -v python3 2>/dev/null || command -v python 2>/dev/null)
if [ -z "$PYTHON" ]; then
  echo "ERRO: Python 3 não encontrado."
  echo "Instale em: https://www.python.org/downloads/"
  read -p "Pressione Enter para fechar..."
  exit 1
fi
echo "Python: $($PYTHON --version)"

# Cria venv
if [ ! -f "$DIR/.venv/bin/python" ]; then
  echo ""
  echo "[ 1/4 ] Criando ambiente Python..."
  $PYTHON -m venv "$DIR/.venv"
fi

# Instala dependências
echo ""
echo "[ 2/4 ] Instalando dependências..."
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

# Playwright
echo ""
echo "[ 3/4 ] Instalando navegador..."
"$DIR/.venv/bin/python" -m playwright install chromium 2>/dev/null || \
"$DIR/.venv/bin/playwright" install chromium

# Claude CLI
echo ""
echo "[ 4/5 ] Instalando motor de IA (Claude CLI)..."
NPM=$(command -v npm 2>/dev/null || command -v /opt/homebrew/bin/npm 2>/dev/null || command -v /usr/local/bin/npm 2>/dev/null)
CLAUDE=$(command -v claude 2>/dev/null)
if [ -z "$CLAUDE" ]; then
  if [ -z "$NPM" ]; then
    echo "AVISO: npm não encontrado — Node.js precisa ser instalado."
    echo "Baixe em: https://nodejs.org e rode o Install novamente."
  else
    echo "Instalando Claude CLI via npm..."
    "$NPM" install -g @anthropic-ai/claude-code 2>&1 | tail -3
    # Verifica se instalou
    CLAUDE=$(command -v claude 2>/dev/null || ls /opt/homebrew/bin/claude /usr/local/bin/claude 2>/dev/null | head -1)
    if [ -n "$CLAUDE" ]; then
      echo "Claude CLI instalado em: $CLAUDE"
    else
      echo "AVISO: Claude CLI não confirmado. Verifique manualmente."
    fi
  fi
else
  echo "Claude CLI já instalado: $CLAUDE"
fi

# LaunchAgent
echo ""
echo "[ 5/5 ] Configurando servidor..."
PLIST="$HOME/Library/LaunchAgents/com.outboundos.server.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$DIR/data"
launchctl unload "$PLIST" 2>/dev/null || true

cat > "$PLIST" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.outboundos.server</string>
  <key>ProgramArguments</key>
  <array>
    <string>$DIR/.venv/bin/python</string>
    <string>$DIR/server.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/data/server.log</string>
  <key>StandardErrorPath</key><string>$DIR/data/server.log</string>
</dict>
</plist>
PLIST_EOF

launchctl load "$PLIST"

# Aguarda servidor
echo ""
echo -n "Iniciando servidor"
for i in {1..30}; do
  sleep 1
  /usr/bin/curl -s http://localhost:7860 > /dev/null 2>&1 && break
  echo -n "."
done

echo ""
echo ""
echo "================================================"
echo "  Instalação concluída!"
echo "  Abrindo BigBoss OS..."
echo "================================================"

sleep 1
open "$DIR/BigBoss OS.app"
sleep 2
# Fecha esta janela do terminal
osascript -e 'tell application "Terminal" to close first window' 2>/dev/null || true

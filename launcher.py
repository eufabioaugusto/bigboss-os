"""
Abre o Outbound OS em janela nativa via WebKit (pywebview).
Inicia o servidor se necessário, depois abre a janela.
"""
import glob
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

URL = "http://127.0.0.1:7860"
DIR = Path(__file__).parent
APP_HOME = DIR / ".app-home"


def server_ready() -> bool:
    try:
        urllib.request.urlopen(URL, timeout=1)
        return True
    except Exception:
        return False


def start_server():
    python = DIR / ".venv" / "bin" / "python"
    log = DIR / "data" / "server.log"
    (DIR / "data").mkdir(exist_ok=True)
    with open(log, "a") as f:
        subprocess.Popen([str(python), str(DIR / "server.py")], cwd=str(DIR), stdout=f, stderr=f)


def wait_for_server(timeout: float = 30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if server_ready():
            return True
        time.sleep(0.3)
    return False


def _find_claude() -> str | None:
    # 1. PATH (pode não estar no .app context)
    found = shutil.which("claude")
    if found:
        return found
    # 2. Caminhos fixos
    candidates = [
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
        Path("/usr/bin/claude"),
        Path.home() / ".local/bin/claude",
        Path.home() / ".npm-global/bin/claude",
    ]
    # 3. Qualquer versão do nvm
    nvm_bins = sorted(glob.glob(str(Path.home() / ".nvm/versions/node/*/bin/claude")), reverse=True)
    candidates += [Path(p) for p in nvm_bins]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


class Api:
    """Funções Python expostas ao JavaScript via pywebview."""

    def open_auth(self):
        """Tenta PTY oculto. Se não detectar URL de login em 8s, abre Terminal limpo."""
        log_path = DIR / "auth-debug.log"

        def log(msg):
            with open(log_path, "a") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")

        log("=== open_auth chamado ===")
        log(f"APP_HOME={APP_HOME}")
        log(f"PATH={os.environ.get('PATH','')}")

        claude = _find_claude()
        log(f"claude encontrado: {claude}")

        if not claude:
            log("ERRO: claude não encontrado em nenhum path")
            return {"ok": False, "error": "Claude CLI não encontrado. Instale com: npm install -g @anthropic-ai/claude-code"}

        APP_HOME.mkdir(exist_ok=True)
        claude_cfg = APP_HOME / ".claude"
        claude_cfg.mkdir(exist_ok=True)
        settings = claude_cfg / "settings.json"
        if not settings.exists():
            settings.write_text('{"theme":"dark"}')

        def _open_terminal_fallback():
            log("Abrindo Terminal como fallback")
            node_path = _find_node()
            script_path = "/tmp/outbound-os-auth.command"
            with open(script_path, "w") as f:
                f.write(f"""#!/bin/bash
export HOME='{APP_HOME}'
export PATH="{Path(claude).parent}:{node_path}:/opt/homebrew/bin:/usr/local/bin:$PATH"
clear
echo "================================================"
echo "  Conectar ao Claude"
echo "================================================"
echo ""
echo "  O browser vai abrir para login."
echo "  Se nao abrir automaticamente, clique"
echo "  na URL que aparecer aqui."
echo ""
echo "  Feche esta janela quando o login for concluido."
echo "------------------------------------------------"
echo ""
'{claude}'
""")
            os.chmod(script_path, 0o755)
            subprocess.Popen(["open", script_path])

        def _find_node() -> str:
            """Encontra o binário node — necessário pois claude é um script Node."""
            node = shutil.which("node")
            if node:
                return str(Path(node).parent)
            candidates = [
                Path("/opt/homebrew/bin"),
                Path("/usr/local/bin"),
                Path("/usr/bin"),
            ]
            nvm_nodes = sorted(glob.glob(str(Path.home() / ".nvm/versions/node/*/bin")), reverse=True)
            candidates += [Path(p) for p in nvm_nodes]
            for c in candidates:
                if (c / "node").exists():
                    return str(c)
            return "/opt/homebrew/bin:/usr/local/bin"

        def _run():
            node_path = _find_node()
            log(f"node path: {node_path}")
            env = os.environ.copy()
            env["HOME"] = str(APP_HOME)
            env["TERM"] = "xterm-256color"
            env["PATH"] = f"{Path(claude).parent}:{node_path}:/opt/homebrew/bin:/usr/local/bin:{env.get('PATH', '')}"

            log(f"Iniciando PTY com claude={claude}")
            master_fd, slave_fd = pty.openpty()
            proc = subprocess.Popen(
                [claude], stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                env=env, close_fds=True,
            )
            os.close(slave_fd)
            log(f"Processo claude PID={proc.pid}")

            output = b""
            url_opened = False
            start = time.time()

            while time.time() - start < 180 and proc.poll() is None:
                try:
                    r, _, _ = select.select([master_fd], [], [], 0.3)
                    if r:
                        chunk = os.read(master_fd, 4096)
                        output += chunk
                        clean = re.sub(rb"\x1b\[[0-9;]*[mGKHFJA-Za-z]", b"", output)
                        text = clean.decode("utf-8", errors="ignore")

                        # Loga output cru (primeiros 2000 chars) para debug
                        if len(output) < 2000:
                            log(f"PTY output: {repr(text[-200:])}")

                        if not url_opened:
                            m = re.search(r"https://\S+", text)
                            if m:
                                url = m.group(0).rstrip(".,)")
                                log(f"URL encontrada: {url}")
                                subprocess.run(["open", url])
                                url_opened = True
                                log("Browser aberto")

                        lines = text.splitlines()
                        if lines and re.match(r"^\s*\d+\.", lines[-1]):
                            log(f"Auto-respondendo seletor: {lines[-1]}")
                            os.write(master_fd, b"1\n")

                    # Após 8s sem URL → Terminal como fallback
                    if not url_opened and time.time() - start > 8:
                        log(f"8s sem URL. Saída atual: {repr(output[-500:].decode('utf-8','ignore'))}")
                        proc.terminate()
                        _open_terminal_fallback()
                        break

                except OSError as e:
                    log(f"OSError: {e}")
                    break

            log(f"_run encerrado. poll={proc.poll()} url_opened={url_opened}")
            try:
                os.close(master_fd)
            except OSError:
                pass

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True}


def main():
    import webview

    if not server_ready():
        start_server()
        if not wait_for_server():
            webview.create_window(
                "Erro",
                html="<h2 style='font-family:sans-serif;padding:40px'>Servidor não iniciou. Veja data/server.log.</h2>",
            )
            webview.start()
            sys.exit(1)

    api = Api()
    window = webview.create_window(
        "BigBoss OS",
        URL,
        width=1360,
        height=860,
        min_size=(900, 600),
        text_select=False,
        js_api=api,
    )
    webview.start(gui="cocoa")


if __name__ == "__main__":
    main()

"""
Wrapper para Codex CLI como backend de IA.
Usa `codex exec` com flags não interativas e salva a saída em um arquivo temporário
para leitura direta do Python, evitando poluição de stdout.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

WORKDIR = str(Path(__file__).parent.parent)


@lru_cache(maxsize=1)
def _resolve_codex_bin() -> str:
    env_bin = (os.getenv("CODEX_BIN") or "").strip()
    if env_bin:
        resolved = Path(env_bin).expanduser()
        if resolved.exists():
            return str(resolved)

    detected = shutil.which("codex")
    if detected:
        return detected

    home = Path.home()
    candidates = [
        home / ".local" / "bin" / "codex",
        home / ".nvm" / "versions" / "node" / "v24.13.1" / "bin" / "codex",
        Path("/opt/homebrew/bin/codex"),
        Path("/usr/local/bin/codex"),
        Path("/usr/bin/codex"),
    ]
    
    # Busca recursiva simples por qualquer versão do nvm
    try:
        nvm_bins = sorted((home / ".nvm" / "versions" / "node").glob("*/bin/codex"), reverse=True)
        candidates.extend(nvm_bins)
    except Exception:
        pass

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    raise FileNotFoundError(
        "Codex CLI não encontrado.\n"
        "Instale ou defina CODEX_BIN no .env com o caminho completo do binário."
    )


def _find_node_path() -> str:
    """Localiza o diretório do node — necessário quando o LaunchAgent tem PATH limitado."""
    import glob
    node = shutil.which("node")
    if node:
        return str(Path(node).parent)
    for candidate in [
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path("/usr/bin"),
    ]:
        if (candidate / "node").exists():
            return str(candidate)
    try:
        nvm_bins = sorted(glob.glob(str(Path.home() / ".nvm/versions/node/*/bin")), reverse=True)
        for p in nvm_bins:
            if (Path(p) / "node").exists():
                return p
    except Exception:
        pass
    return ""


def _codex_env(codex_bin: str) -> dict:
    env = os.environ.copy()
    current_path = env.get("PATH", "")
    codex_path = Path(codex_bin).expanduser()
    node_path = _find_node_path()
    path_parts = [
        str(codex_path.parent),
        str(codex_path.resolve().parent),
        node_path,
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]
    if current_path:
        path_parts.append(current_path)
    env["PATH"] = ":".join(p for p in path_parts if p)
    return env


def ask(prompt: str, system: str = "") -> str:
    """Chama o Codex CLI e retorna a resposta como texto limpo."""
    full_prompt = f"{system}\n\n{prompt}" if system else prompt
    codex_bin = _resolve_codex_bin()

    # Cria arquivo temporário para salvar a saída do Codex de forma limpa
    fd, temp_out_path = tempfile.mkstemp(suffix=".txt")
    os.close(fd)

    try:
        cmd = [
            codex_bin, "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--ephemeral",
            "--ignore-rules",
            "-o", temp_out_path,
            full_prompt
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=WORKDIR,
            env=_codex_env(codex_bin),
            stdin=subprocess.DEVNULL,
            timeout=180,  # 3 minutos máximo
        )

        output = ""
        if os.path.exists(temp_out_path):
            with open(temp_out_path, "r", encoding="utf-8") as f:
                output = f.read().strip()

        stderr = (result.stderr or "").strip()

        if not output and result.returncode != 0:
            detail = stderr or f"Codex CLI falhou com código {result.returncode}."
            raise RuntimeError(detail)

        return output
    finally:
        if os.path.exists(temp_out_path):
            try:
                os.remove(temp_out_path)
            except Exception:
                pass


def ask_json(prompt: str, system: str = ""):
    """Chama o Codex e faz parse do JSON retornado."""
    system_json = (system + "\nRetorne APENAS JSON válido, sem markdown, sem explicação.").strip()
    raw = ask(prompt, system_json)

    raw = re.sub(r"^```json\s*|^```\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()

    if not raw:
        raise ValueError("Codex retornou resposta vazia")

    match = re.search(r"(\[.*\]|\{.*\})", raw, re.DOTALL)
    if match:
        raw = match.group(1)

    return json.loads(raw)

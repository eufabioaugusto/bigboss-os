#!/usr/bin/env python3
"""
Motor de Prospecção por Email — CLI + painel local
Uso:
  python prospection.py --prompt "Prospectar clínicas odontológicas em SP"
  python prospection.py --ui     (abre painel web local)
"""
import argparse
import csv
import json
import os
import sys
import yaml
from datetime import datetime
from pathlib import Path

from settings_store import get_runtime_config

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LEADS_DIR = DATA_DIR / "leads"
EMAILS_DIR = DATA_DIR / "emails"
LOGS_DIR = DATA_DIR / "logs"
TEMPLATES_DIR = BASE_DIR / "templates"


def ensure_data_dirs():
    for path in (LEADS_DIR, EMAILS_DIR, LOGS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    return get_runtime_config()


def load_env():
    # 1. .env local (SMTP_APP_PASSWORD etc)
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    # IA via Codex CLI (OAuth) — sem API key necessária


def save_leads_csv(leads: list[dict], run_id: str) -> Path:
    path = LEADS_DIR / f"{run_id}.csv"
    if not leads:
        return path
    fields = list(leads[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(leads)
    return path


def save_emails_json(leads: list[dict], run_id: str) -> Path:
    path = EMAILS_DIR / f"{run_id}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(leads, f, ensure_ascii=False, indent=2)
    return path


def print_summary(leads: list[dict]):
    total = len(leads)
    com_email = sum(1 for l in leads if l.get("email"))
    alta = sum(1 for l in leads if l.get("prioridade") == "alta")
    media = sum(1 for l in leads if l.get("prioridade") == "média")
    ready = sum(1 for l in leads if l.get("email_status") == "ready")
    sent = sum(1 for l in leads if l.get("email_status") == "sent")

    print(f"\n{'='*50}")
    print(f"  RESUMO DA RODADA")
    print(f"{'='*50}")
    print(f"  Total de leads:     {total}")
    print(f"  Com email:          {com_email}")
    print(f"  Prioridade alta:    {alta}")
    print(f"  Prioridade média:   {media}")
    print(f"  Emails prontos:     {ready}")
    print(f"  Emails enviados:    {sent}")
    print(f"{'='*50}\n")

    print(f"{'Empresa':<30} {'Score':>5} {'Prior.':<8} {'Status':<15}")
    print("-" * 65)
    for l in leads:
        print(
            f"{str(l.get('nome_empresa','?'))[:29]:<30} "
            f"{l.get('score', '-'):>5} "
            f"{l.get('prioridade','?'):<8} "
            f"{l.get('email_status','?'):<15}"
        )


def run_pipeline(prompt: str, config: dict, send: bool = False):
    from agents.researcher import run_research
    from agents.enricher import enrich_leads
    from agents.analyst import diagnose_leads
    from agents.copywriter import generate_emails_for_leads
    from agents.sender import run_send_batch
    import crm

    ensure_data_dirs()
    crm.init_db()
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"\n[{run_id}] Iniciando prospecção...")
    print(f"Prompt: {prompt}\n")

    print("1/4 Pesquisando leads...")
    leads = run_research(prompt, config)
    print(f"   {len(leads)} leads encontrados")

    if not leads:
        print("Nenhum lead encontrado. Verifique o prompt e as API keys.")
        return

    leads, duplicates = crm.dedupe_batch(leads)
    if duplicates:
        print(f"   {duplicates} leads duplicados na rodada foram removidos")

    leads = enrich_leads(leads)
    leads, contacted = crm.filter_new_leads(leads)
    if contacted:
        print(f"   {contacted} leads já contatados anteriormente foram pulados")

    if not leads:
        print("Todos os leads desta rodada já foram trabalhados anteriormente.")
        return

    print("2/4 Enriquecendo, diagnosticando e pontuando...")
    leads = diagnose_leads(leads, config)
    crm.save_leads(leads, run_id)

    print("3/4 Gerando emails personalizados...")
    leads = generate_emails_for_leads(leads, config, str(TEMPLATES_DIR))

    csv_path = save_leads_csv(leads, run_id)
    emails_path = save_emails_json(leads, run_id)
    print(f"   Leads salvos: {csv_path}")
    print(f"   Emails salvos: {emails_path}")

    print_summary(leads)

    if send:
        print("4/4 Iniciando envio...")
        leads = run_send_batch(leads, config, str(LOGS_DIR))
    else:
        print("4/4 Envio pulado (use --send para enviar)")

    return leads



def main():
    parser = argparse.ArgumentParser(description="Motor de Prospecção por Email")
    parser.add_argument("--prompt", "-p", help="Prompt central de prospecção")
    parser.add_argument("--send", action="store_true", help="Envia emails após gerar")
    args = parser.parse_args()

    load_env()
    ensure_data_dirs()
    config = load_config()

    if args.prompt:
        run_pipeline(args.prompt, config, send=args.send)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

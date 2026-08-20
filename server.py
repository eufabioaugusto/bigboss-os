#!/usr/bin/env python3
"""
Servidor FastAPI — Motor de Prospecção por Email
"""
import json, csv, os, yaml, sys, threading, time
from pathlib import Path
from datetime import datetime
from typing import Generator, Optional
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import crm

from settings_store import get_company, get_runtime_config, get_settings, init_db as init_settings_db, save_settings
from tasks_store import (
    create_task,
    create_task_run,
    delete_task,
    finish_task_run,
    get_recent_runs,
    get_task,
    init_db as init_tasks_db,
    list_due_tasks,
    list_task_runs,
    list_tasks,
    schedule_next_run,
    touch_task_schedule,
    update_task,
)
from templates_store import create_template, delete_template, duplicate_template, get_template, get_template_versions, get_templates, init_db as init_templates_db, render_template_preview, restore_template_version, update_template

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

LEADS_DIR  = BASE_DIR / "data/leads"
EMAILS_DIR = BASE_DIR / "data/emails"
LOGS_DIR   = BASE_DIR / "data/logs"

app = FastAPI()

# estado em memória da rodada atual
state: dict = {"leads": [], "run_id": None, "log": [], "status": "idle"}
scheduler_state: dict = {"thread": None, "stop": False, "running_tasks": set(), "lock": threading.Lock()}


def load_env():
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def load_config():
    return get_runtime_config()


@app.on_event("startup")
async def startup_event():
    load_env()
    ensure_data_dirs()
    init_settings_db()
    init_templates_db()
    init_tasks_db()
    start_scheduler()


@app.on_event("shutdown")
async def shutdown_event():
    scheduler_state["stop"] = True


def push_log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    state["log"].append(f"[{ts}] {msg}")


def _make_logger(target_logs: Optional[list[str]] = None, mirror_state: bool = False):
    def _logger(msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        if target_logs is not None:
            target_logs.append(line)
        if mirror_state:
            state["log"].append(line)
    return _logger


def ensure_data_dirs():
    for path in (LEADS_DIR, EMAILS_DIR, LOGS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _task_config(base_config: dict, task: dict) -> dict:
    config = json.loads(json.dumps(base_config))
    config["source"] = task.get("source") or "google"
    config["mode"] = "auto" if task.get("auto_send") else "manual"
    config["template_id"] = task.get("template_id")
    config["campaign_id"] = task.get("campaign_id")
    config.setdefault("limits", {})
    config.setdefault("search", {})
    config.setdefault("scoring", {})
    config["limits"]["max_leads_per_run"] = int(task.get("max_leads_per_run") or config["limits"].get("max_leads_per_run") or 10)
    config["limits"]["max_emails_per_day"] = int(task.get("max_emails_per_day") or config["limits"].get("max_emails_per_day") or 50)
    config["limits"]["delay_between_sends_seconds"] = int(task.get("delay_between_sends_seconds") or config["limits"].get("delay_between_sends_seconds") or 30)
    config["search"]["results_per_query"] = int(task.get("results_per_query") or config["search"].get("results_per_query") or 10)
    config["scoring"]["min_score_to_send"] = int(task.get("min_score_to_send") or config["scoring"].get("min_score_to_send") or 5)
    return config


def execute_pipeline(prompt: str, config: dict, *, progress_cb=None, publish_state: bool = False) -> dict:
    from agents.researcher import run_research
    from agents.enricher import enrich_leads
    from agents.analyst import diagnose_leads
    from agents.copywriter import generate_emails_for_leads
    from agents.sender import send_email
    import crm

    ensure_data_dirs()
    crm.init_db()
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    local_logs: list[str] = []
    logger = progress_cb or _make_logger(local_logs, mirror_state=False)
    leads: list[dict] = []
    crm_ids: dict[int, int] = {}
    status = "running"

    if publish_state:
        state["status"] = "running"
        state["leads"] = []
        state["log"] = []
        state["run_id"] = run_id
        state["crm_ids"] = {}
        logger = _make_logger(local_logs, mirror_state=True)

    try:
        logger("Pesquisando leads...")
        leads = run_research(prompt, config, progress_cb=logger)
        for lead in leads:
            lead["campaign_id"] = config.get("campaign_id")
        logger(f"{len(leads)} leads encontrados.")

        if not leads:
            status = "done"
            logger("Nenhum lead encontrado. Tente um prompt mais específico.")
            return {"status": status, "run_id": run_id, "leads": [], "logs": local_logs, "summary": {"leads_found": 0, "leads_qualified": 0, "emails_ready": 0, "emails_sent": 0}}

        logger("Verificando sites e dados reais...")
        leads, duplicates = crm.dedupe_batch(leads)
        if duplicates:
            logger(f"{duplicates} leads duplicados na rodada foram removidos.")

        leads = enrich_leads(leads, progress_cb=logger)
        leads, contacted = crm.filter_new_leads(leads)
        if contacted:
            logger(f"{contacted} leads já contatados anteriormente foram pulados.")

        before = len(leads)
        leads = [lead for lead in leads if _is_valid_lead(lead)]
        descartados = before - len(leads)
        if descartados:
            logger(f"{descartados} leads descartados (sem nome ou contato identificado).")

        if not leads:
            status = "done"
            logger("Nenhum lead qualificado. Tente um prompt diferente.")
            return {"status": status, "run_id": run_id, "leads": [], "logs": local_logs, "summary": {"leads_found": before, "leads_qualified": 0, "emails_ready": 0, "emails_sent": 0}}

        logger(f"Diagnosticando {len(leads)} leads qualificados...")
        leads = diagnose_leads(leads, config, progress_cb=logger)
        crm_ids = crm.save_leads(leads, run_id)

        logger("Gerando emails personalizados...")
        leads = generate_emails_for_leads(leads, config, str(BASE_DIR / "templates"), progress_cb=logger)

        # Persiste rascunhos, scoring e DM no CRM
        for lead in leads:
            cid = lead.get("crm_contact_id")
            if cid and lead.get("email_assunto"):
                crm.update_contact_draft(
                    cid,
                    lead["email_assunto"],
                    lead.get("email_corpo", ""),
                    lead.get("email_template_id"),
                    lead.get("score"),
                    lead.get("angulo"),
                    json.dumps(lead.get("sinais") or [], ensure_ascii=False),
                    lead.get("prioridade"),
                    lead.get("dm_instagram"),
                )

        ready = [lead for lead in leads if lead.get("email_status") == "ready"]
        alta = sum(1 for lead in leads if lead.get("prioridade") == "alta")
        logger(f"{len(leads)} qualificados | {alta} alta prioridade | {len(ready)} prontos para envio.")

        sent_count = 0
        if config.get("mode") == "auto" and ready:
            logger("Modo AUTO — enviando emails...")
            for index, lead in enumerate(leads):
                if lead.get("email_status") != "ready":
                    continue
                allowed, reason, contact = crm.can_send_lead(lead)
                if not allowed:
                    lead["email_status"] = "crm_locked"
                    if contact:
                        lead["crm_contact_id"] = contact["id"]
                        lead["crm_status"] = contact["status"]
                        lead["crm_send_count"] = contact["send_count"]
                    logger(f"↷ {lead.get('nome_empresa')} bloqueado pelo CRM: {reason}")
                    continue
                result = send_email(lead, config)
                lead["send_status"] = result["status"]
                lead["send_timestamp"] = result.get("timestamp")
                lead["email_status"] = "sent" if result["status"] == "sent" else "send_error"
                crm.register_send(
                    lead,
                    run_id,
                    crm_ids.get(index),
                    result["status"],
                    result.get("error"),
                    provider=result.get("provider"),
                    provider_message_id=result.get("provider_message_id"),
                )
                if result["status"] == "sent":
                    sent_count += 1
                logger(f"{'✓' if result['status']=='sent' else '✗'} {lead.get('nome_empresa')} → {lead.get('email')}")
                if result["status"] == "sent" and config["limits"]["delay_between_sends_seconds"] > 0:
                    time.sleep(config["limits"]["delay_between_sends_seconds"])
            logger("Envio concluído.")
        else:
            logger("Pronto. Revise os emails abaixo e clique em Enviar.")

        with open(EMAILS_DIR / f"{run_id}.json", "w") as f:
            json.dump(leads, f, ensure_ascii=False, indent=2)

        if publish_state:
            state["leads"] = leads
            state["crm_ids"] = crm_ids
            final_status = "awaiting_send" if ready and config.get("mode") != "auto" else "done"
            state["status"] = final_status
            state["last_run_id"] = run_id
            state["last_run_leads"] = len(leads)
            status = final_status
        else:
            status = "done"

        return {
            "status": status,
            "run_id": run_id,
            "leads": leads,
            "logs": local_logs,
            "crm_ids": crm_ids,
            "summary": {
                "leads_found": len(leads),
                "leads_qualified": len(leads),
                "emails_ready": len(ready),
                "emails_sent": sent_count,
                "high_priority": alta,
            },
        }
    except Exception as exc:
        import traceback
        traceback.print_exc()
        logger(f"Erro: {exc}")
        if publish_state:
            state["status"] = "error"
        return {
            "status": "error",
            "run_id": run_id,
            "leads": leads,
            "logs": local_logs,
            "crm_ids": crm_ids,
            "error": str(exc),
            "summary": {
                "leads_found": len(leads),
                "leads_qualified": len(leads),
                "emails_ready": sum(1 for lead in leads if lead.get("email_status") == "ready"),
                "emails_sent": sum(1 for lead in leads if lead.get("email_status") == "sent"),
            },
        }


def _run_task_thread(task_id: str, trigger_source: str = "scheduler"):
    task = get_task(task_id)
    if not task:
        with scheduler_state["lock"]:
            scheduler_state["running_tasks"].discard(task_id)
        return

    run_id = create_task_run(task_id, trigger_source=trigger_source)
    touch_task_schedule(task_id, last_run_at=datetime.now().isoformat(), last_result="Executando...", next_run_at=task.get("next_run_at"))
    config = _task_config(load_config(), task)
    result = execute_pipeline(task["prompt"], config, publish_state=False)
    summary = result.get("summary", {})
    task_status = "success" if result.get("status") != "error" else "error"
    finish_task_run(run_id, status=task_status, logs=result.get("logs", []), summary=summary, error=result.get("error"))
    fresh_task = get_task(task_id) or task
    last_result = (
        f"{summary.get('emails_sent', 0)} enviados · {summary.get('emails_ready', 0)} prontos · "
        f"{summary.get('leads_qualified', 0)} leads"
        if task_status == "success"
        else f"Erro: {result.get('error', 'falha interna')}"
    )
    if fresh_task.get("status") == "active":
        schedule_next_run(fresh_task)
    touch_task_schedule(task_id, last_result=last_result, last_run_at=datetime.now().isoformat(), next_run_at=(get_task(task_id) or {}).get("next_run_at"))
    with scheduler_state["lock"]:
        scheduler_state["running_tasks"].discard(task_id)


def generate_pending_followups():
    import crm
    from agents.ai import ask_json
    from datetime import datetime, timedelta
    
    try:
        needing = crm.get_contacts_needing_followup_generation()
        for contact in needing:
            contact_id = contact["id"]
            attempt_no = int(contact["send_count"] or 1) + 1
            
            # Ajuste de intervalo: para testes, se DEBUG_FOLLOWUP=true, agenda para 10 segundos
            if os.getenv("DEBUG_FOLLOWUP") == "true":
                scheduled_time = (datetime.now() + timedelta(seconds=10)).isoformat()
            else:
                days_delay = 3 if attempt_no == 2 else 4
                scheduled_time = (datetime.now() + timedelta(days=days_delay)).isoformat()
                
            contact_full = crm.get_contact_full(contact_id)
            last_envio = next((e for e in contact_full.get("envios", [])), None)
            
            # Contexto de campanha no follow-up
            camp_details = ""
            campaign_id = contact.get("campaign_id")
            if campaign_id:
                try:
                    with crm.get_conn() as conn:
                        row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
                        if row:
                            camp = dict(row)
                            camp_details = f"\nOferta da Campanha Ativa ({camp.get('name')}):\n"
                            camp_details += f"- Produto: {camp.get('product_name')}\n"
                            camp_details += f"- Descrição: {camp.get('product_description')}\n"
                            if camp.get("price_promo"):
                                camp_details += f"- Preço Promocional: R$ {camp.get('price_promo')}\n"
                            if camp.get("price_original"):
                                camp_details += f"- Preço Original: R$ {camp.get('price_original')}\n"
                            if camp.get("scarcity_limit"):
                                camp_details += f"- Limite/Vagas: {camp.get('scarcity_limit')}\n"
                            if camp.get("tone_of_voice"):
                                camp_details += f"- Tom de voz: {camp.get('tone_of_voice')}\n"
                except Exception:
                    pass

            context = (
                f"Empresa: {contact['nome_empresa']}\n"
                f"Segmento: {contact['segmento']}\n"
                f"Último email enviado em: {last_envio['enviado_em'] if last_envio else 'desconhecido'}\n"
                f"Assunto anterior: {contact.get('email_draft_assunto') or (last_envio or {}).get('assunto', '')}\n"
                f"Corpo anterior: {(last_envio or {}).get('corpo', '')[:400]}\n"
                f"Notas: {'; '.join(n['note'] for n in contact_full.get('notes', []))}\n"
                f"{camp_details}"
            )
            
            try:
                result = ask_json(
                    f"Gere um follow-up (tentativa {attempt_no}) para este lead que não respondeu ao email anterior.\n\n"
                    f"Contexto:\n{context}\n\n"
                    "Retorne JSON com: assunto (string) e corpo (string). Tom comercial direto, muito curto (máx 100 palavras), profissional, sem enrolação. Deve continuar o assunto anterior (geralmente mantendo a linha ou usando 'Re: assunto' ou assunto direto).",
                    "Você é copywriter especialista em cold outreach por e-mail no Brasil. Retorne APENAS JSON válido."
                )
                assunto = result.get("assunto", "")
                corpo = result.get("corpo", "")
                if assunto and corpo:
                    crm.add_followup(contact_id, attempt_no, assunto, corpo, scheduled_time)
                    push_log(f"Follow-up {attempt_no-1} gerado automaticamente para {contact['nome_empresa']}.")
            except Exception as e:
                print(f"[scheduler] erro ao gerar followup para {contact['nome_empresa']}: {e}")
    except Exception as exc:
        print(f"[scheduler] erro no generate_pending_followups: {exc}")


def send_due_followups():
    import crm
    from agents.sender import remaining_daily_quota, send_email
    from datetime import datetime
    
    try:
        due = crm.get_pending_followups()
        if not due:
            return
            
        config = load_config()
        if config.get("mode") != "auto":
            return
            
        quota = remaining_daily_quota(config)
        if quota <= 0:
            return
            
        for fu in due:
            # Verifica cota de envio diária a cada iteração
            if remaining_daily_quota(config) <= 0:
                break
                
            lead = {
                "nome_empresa": fu["nome_empresa"],
                "email": fu["primary_email"],
                "email_assunto": fu["email_assunto"],
                "email_corpo": fu["email_corpo"],
                "crm_contact_id": fu["contact_id"],
            }
            
            try:
                result = send_email(lead, config)
                with crm.get_conn() as conn:
                    if result["status"] == "sent":
                        conn.execute(
                            "UPDATE crm_followups SET status = 'sent', sent_at = ? WHERE id = ?",
                            (datetime.now().isoformat(), fu["id"])
                        )
                    else:
                        conn.execute(
                            "UPDATE crm_followups SET status = 'failed' WHERE id = ?",
                            (fu["id"],)
                        )
                    conn.commit()
                    
                crm.register_send(
                    lead,
                    "auto_followup",
                    None,
                    result["status"],
                    result.get("error"),
                    provider=result.get("provider"),
                    provider_message_id=result.get("provider_message_id"),
                )
                
                if result["status"] == "sent":
                    push_log(f"✓ Follow-up enviado automaticamente para {fu['nome_empresa']} ({fu['primary_email']}).")
                else:
                    push_log(f"✗ Falha ao enviar follow-up para {fu['nome_empresa']}: {result.get('error')}")
            except Exception as e:
                print(f"[scheduler] erro ao enviar followup {fu['id']}: {e}")
    except Exception as exc:
        print(f"[scheduler] erro no send_due_followups: {exc}")


def check_incoming_replies():
    import crm
    import re
    from agents.ai import ask_json
    from datetime import datetime, timedelta
    
    config = load_config()
    imap_cfg = config.get("imap", {})
    imap_enabled = (os.environ.get("IMAP_ENABLED") or "").strip().lower() == "true" or bool(imap_cfg.get("enabled"))
    if not imap_enabled:
        return
        
    import imaplib
    import email
    from email.header import decode_header
    
    imap_host = imap_cfg.get("host") or os.environ.get("IMAP_HOST")
    imap_port = int(imap_cfg.get("port") or os.environ.get("IMAP_PORT") or 993)
    
    sender_cfg = config.get("sender", {})
    imap_user = imap_cfg.get("username") or os.environ.get("IMAP_USERNAME") or sender_cfg.get("email") or ""
    imap_pass = imap_cfg.get("password") or os.environ.get("IMAP_PASSWORD") or os.environ.get("SMTP_APP_PASSWORD") or sender_cfg.get("app_password") or ""
    
    if not imap_host or not imap_user or not imap_pass:
        return

        
    try:
        mail = imaplib.IMAP4_SSL(imap_host, imap_port)
        mail.login(imap_user, imap_pass)
        mail.select("INBOX")
        
        # Busca emails dos últimos 2 dias para capturar respostas mesmo se já lidas no Gmail
        since_date = (datetime.now() - timedelta(days=2)).strftime("%d-%b-%Y")
        status, messages = mail.search(None, 'SINCE', since_date)
        if status != "OK" or not messages[0]:
            mail.logout()
            return
            
        msg_ids = messages[0].split()
        for m_id in msg_ids:
            res, data = mail.fetch(m_id, '(RFC822)')
            if res != "OK":
                continue
                
            raw_email = data[0][1]
            msg = email.message_from_bytes(raw_email)
            
            from_header = msg.get("From")
            if not from_header:
                continue
            
            emails_found = re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', from_header)
            if not emails_found:
                continue
            from_email = emails_found[0].lower().strip()
            
            contact = crm.get_contact_by_email(from_email)
            if not contact:
                continue
                
            provider_msg_id = msg.get("Message-ID") or f"imap_{from_email}_{msg.get('Date')}"
            
            with crm.get_conn() as conn:
                exists = conn.execute(
                    "SELECT COUNT(*) FROM crm_email_replies WHERE provider_msg_id = ?",
                    (provider_msg_id,)
                ).fetchone()[0]
                if exists:
                    continue
            
            subject = ""
            subject_header = msg.get("Subject")
            if subject_header:
                decoded = decode_header(subject_header)
                for part, encoding in decoded:
                    if isinstance(part, bytes):
                        subject += part.decode(encoding or "utf-8", errors="ignore")
                    else:
                        subject += part
            
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    content_disp = str(part.get("Content-Disposition"))
                    if content_type == "text/plain" and "attachment" not in content_disp:
                        payload = part.get_payload(decode=True)
                        if payload:
                            body = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
                            break
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    body = payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")
            
            body_preview = body.strip()[:400]
            
            prompt_classificar = f"""O lead '{contact['nome_empresa']}' respondeu ao nosso e-mail de prospecção com o seguinte texto:
"{body_preview}"

Classifique esta resposta em uma das seguintes categorias:
- "interesse" (deseja agendar, quer saber mais, demonstrou interesse real)
- "desinteresse" (recusou, não quer no momento, obrigado mas não)
- "remover" (pediu para retirar da lista, opt-out, não envie mais)
- "out_of_office" (resposta automática de férias ou ausência)
- "outros" (respostas neutras, perguntas gerais ou indefinidas)

Retorne JSON com: categoria (string) e justificativa (string)."""
            
            categoria = "outros"
            justificativa = ""
            try:
                res_class = ask_json(prompt_classificar, "Você é um assistente de vendas inteligente. Retorne apenas JSON.")
                categoria = res_class.get("categoria", "outros")
                justificativa = res_class.get("justificativa", "")
            except Exception as e:
                print(f"[IMAP] Erro ao classificar resposta com o Codex: {e}")
                
            with crm.get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO crm_email_replies (contact_id, from_email, from_name, subject, body_preview, received_at, status, provider, provider_msg_id)
                    VALUES (?, ?, ?, ?, ?, ?, 'unread', 'imap', ?)
                    """,
                    (contact["id"], from_email, from_header, subject, f"[{categoria.upper()}] {body_preview}", datetime.now().isoformat(), provider_msg_id)
                )
                conn.commit()
                
            crm.add_note(contact["id"], f"Resposta recebida. Classificação: {categoria.upper()}. Justificativa: {justificativa}", author="system")
            crm.update_contact_status(contact["id"], "replied", note=f"Classificado como {categoria.upper()}")
            push_log(f"✓ Resposta recebida de {contact['nome_empresa']} ({from_email}): '{body_preview[:60]}...' Classificação: {categoria.upper()}")
            
        mail.close()
        mail.logout()
    except Exception as exc:
        print(f"[IMAP] Erro na leitura da caixa de entrada: {exc}")


def _scheduler_loop():
    while not scheduler_state["stop"]:
        try:
            # 1. Processa tarefas agendadas normais
            due_tasks = list_due_tasks()
            for task in due_tasks:
                with scheduler_state["lock"]:
                    if task["id"] in scheduler_state["running_tasks"]:
                        continue
                    scheduler_state["running_tasks"].add(task["id"])
                threading.Thread(target=_run_task_thread, args=(task["id"], "scheduler"), daemon=True).start()
            
            # 2. Gera follow-ups pendentes para contatos elegíveis
            generate_pending_followups()
            
            # 3. Envia follow-ups que estão vencidos
            send_due_followups()
            
            # 4. Checa por novas respostas se IMAP estiver ativo
            check_incoming_replies()
            
            # 5. Auto-Pilot Autônomo: descobre nichos de alto ticket e abastece rotinas proativamente
            active_tasks = [t for t in list_tasks() if t.get("status") == "active"]
            if len(active_tasks) < 3:
                from agents.strategist import auto_discover_and_create_routines
                auto_discover_and_create_routines(max_create=2)
            
        except Exception as exc:
            print(f"[scheduler] erro no loop principal: {exc}")
        time.sleep(30)



def start_scheduler():
    if scheduler_state["thread"] and scheduler_state["thread"].is_alive():
        return
    scheduler_state["stop"] = False
    scheduler_state["thread"] = threading.Thread(target=_scheduler_loop, daemon=True, name="task-scheduler")
    scheduler_state["thread"].start()


# ── SSE stream de progresso ──────────────────────────────────────────────────

@app.get("/stream")
async def stream(request: Request):
    async def event_gen():
        last = 0
        while True:
            if await request.is_disconnected():
                break
            logs = state["log"]
            if len(logs) > last:
                for line in logs[last:]:
                    payload = {
                        "log": line,
                        "status": state["status"],
                        "run_id": state.get("last_run_id"),
                        "leads_count": state.get("last_run_leads"),
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                last = len(logs)
            import asyncio; await asyncio.sleep(0.5)
    return StreamingResponse(event_gen(), media_type="text/event-stream")


# ── Pipeline ─────────────────────────────────────────────────────────────────

def _is_valid_lead(lead: dict) -> bool:
    """Mantém leads com nome + pelo menos um ponto de presença digital ou telefone."""
    name = (lead.get("nome_empresa") or "").strip()
    return bool(name) and name not in ("None", "?", "") and bool(
        lead.get("email") or lead.get("website_verificado") or lead.get("website") or lead.get("instagram") or lead.get("telefone")
    )


def run_pipeline_bg(prompt: str, source: str = "google", template_id: str = None, auto_send: bool = False, max_leads_per_run: int = None, campaign_id: str = None):
    config = load_config()
    config["source"] = source
    if template_id:
        config["template_id"] = template_id
    if auto_send:
        config["mode"] = "auto"
    if campaign_id:
        config["campaign_id"] = campaign_id
    if max_leads_per_run:
        config.setdefault("limits", {})
        config["limits"]["max_leads_per_run"] = max_leads_per_run
    execute_pipeline(prompt, config, publish_state=True)


@app.post("/cancel")
async def cancel_pipeline():
    """Cancela a rodada em execução e libera o estado para nova prospecção."""
    state["status"] = "idle"
    state["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] Rodada cancelada pelo usuário.")
    return {"ok": True}


@app.post("/prospect")
async def prospect(req: Request, bg: BackgroundTasks):
    body = await req.json()
    prompt = body.get("prompt", "").strip()
    source = body.get("source", "google").strip()
    template_id = body.get("template_id")
    auto_send = bool(body.get("auto_send"))
    max_leads_per_run = body.get("max_leads_per_run")
    campaign_id = body.get("campaign_id")
    if max_leads_per_run is not None:
        try:
            max_leads_per_run = int(max_leads_per_run)
        except (ValueError, TypeError):
            max_leads_per_run = None
            
    if not prompt:
        return JSONResponse({"error": "Prompt vazio"}, status_code=400)
    if state["status"] == "running":
        return JSONResponse({"error": "Já rodando"}, status_code=409)
    bg.add_task(run_pipeline_bg, prompt, source, template_id, auto_send, max_leads_per_run, campaign_id)
    return {"ok": True}


@app.get("/leads")
def get_leads():
    return state["leads"]


@app.put("/leads/{idx}")
async def update_lead(idx: int, req: Request):
    leads = state["leads"]
    if idx >= len(leads):
        return JSONResponse({"error": "Índice inválido"}, status_code=404)
    body = await req.json()
    lead = leads[idx]
    for field in ("email", "email_assunto", "email_corpo", "email_status"):
        if field in body and body[field] is not None:
            lead[field] = body[field]
    return lead


@app.post("/send/{idx}")
async def send_one(idx: int):
    from agents.sender import send_email
    import crm
    config = load_config()
    crm.init_db()
    leads = state["leads"]
    if idx >= len(leads):
        return JSONResponse({"error": "Índice inválido"}, status_code=404)
    lead = leads[idx]
    allowed, reason, contact = crm.can_send_lead(lead)
    if not allowed:
        lead["email_status"] = "crm_locked"
        if contact:
            lead["crm_contact_id"] = contact["id"]
            lead["crm_status"] = contact["status"]
            lead["crm_send_count"] = contact["send_count"]
        return JSONResponse({"status": "blocked", "error": reason, "crm_status": lead.get("crm_status")}, status_code=409)
    result = send_email(lead, config)
    lead["send_status"] = result["status"]
    lead["send_timestamp"] = result["timestamp"]
    lead["email_status"] = "sent" if result["status"] == "sent" else "send_error"
    crm_id = state.get("crm_ids", {}).get(idx)
    contact = crm.register_send(
        lead,
        state.get("run_id"),
        crm_id,
        result["status"],
        result.get("error"),
        provider=result.get("provider"),
        provider_message_id=result.get("provider_message_id"),
    )
    lead["crm_contact_id"] = contact["id"]
    lead["crm_status"] = contact["status"]
    lead["crm_send_count"] = contact["send_count"]
    return result


@app.post("/set-email/{idx}")
async def set_email(idx: int, req: Request):
    body = await req.json()
    email = body.get("email", "").strip()
    if idx < len(state["leads"]) and email:
        state["leads"][idx]["email"] = email
        state["leads"][idx]["email_status"] = "ready"
    return {"ok": True}


@app.post("/send-all")
async def send_all():
    from agents.sender import send_email
    import crm, time
    config = load_config()
    crm.init_db()
    delay = config["limits"]["delay_between_sends_seconds"]
    sent, errors, blocked = 0, 0, 0
    for i, lead in enumerate(state["leads"]):
        if lead.get("email_status") != "ready":
            continue
        allowed, reason, contact = crm.can_send_lead(lead)
        if not allowed:
            lead["email_status"] = "crm_locked"
            if contact:
                lead["crm_contact_id"] = contact["id"]
                lead["crm_status"] = contact["status"]
                lead["crm_send_count"] = contact["send_count"]
            blocked += 1
            continue
        result = send_email(lead, config)
        lead["send_status"] = result["status"]
        lead["email_status"] = "sent" if result["status"] == "sent" else "send_error"
        crm_id = state.get("crm_ids", {}).get(i)
        contact = crm.register_send(
            lead,
            state.get("run_id"),
            crm_id,
            result["status"],
            result.get("error"),
            provider=result.get("provider"),
            provider_message_id=result.get("provider_message_id"),
        )
        lead["crm_contact_id"] = contact["id"]
        lead["crm_status"] = contact["status"]
        lead["crm_send_count"] = contact["send_count"]
        if result["status"] == "sent":
            sent += 1
            time.sleep(delay)
        else:
            errors += 1
    state["status"] = "done"
    return {"sent": sent, "errors": errors, "blocked": blocked}


@app.get("/settings")
def get_settings_payload():
    return get_settings()


@app.post("/settings")
async def save_settings_payload(req: Request):
    body = await req.json()
    return save_settings(body)


@app.get("/company")
def get_company_payload():
    return get_company()


@app.post("/company")
async def save_company(req: Request):
    body = await req.json()
    return {"company": save_settings({"company": body}).get("company", {})}


@app.get("/campaigns")
def list_campaigns():
    crm.init_db()
    with crm.get_conn() as conn:
        rows = conn.execute("SELECT * FROM campaigns ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


@app.post("/campaigns")
async def save_campaign_payload(req: Request):
    crm.init_db()
    body = await req.json()
    campaign_id = body.get("id")
    if not campaign_id:
        from templates_store import _slugify
        campaign_id = _slugify(body.get("name", "campanha"))
    
    name = body.get("name", "")
    product_name = body.get("product_name", "")
    product_description = body.get("product_description", "")
    
    price_original = body.get("price_original")
    if price_original == "" or price_original is None:
        price_original = None
    else:
        try:
            price_original = float(price_original)
        except ValueError:
            price_original = None
            
    price_promo = body.get("price_promo")
    if price_promo == "" or price_promo is None:
        price_promo = None
    else:
        try:
            price_promo = float(price_promo)
        except ValueError:
            price_promo = None
            
    scarcity_limit = body.get("scarcity_limit")
    if scarcity_limit == "" or scarcity_limit is None:
        scarcity_limit = None
    else:
        try:
            scarcity_limit = int(scarcity_limit)
        except ValueError:
            scarcity_limit = None
            
    tone_of_voice = body.get("tone_of_voice", "")
    is_active = int(body.get("is_active", 1))
    
    stamp = datetime.now().isoformat()
    
    with crm.get_conn() as conn:
        exists = conn.execute("SELECT id FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if exists:
            conn.execute(
                """
                UPDATE campaigns
                SET name = ?, product_name = ?, product_description = ?, price_original = ?, price_promo = ?, scarcity_limit = ?, tone_of_voice = ?, is_active = ?, updated_at = ?
                WHERE id = ?
                """,
                (name, product_name, product_description, price_original, price_promo, scarcity_limit, tone_of_voice, is_active, stamp, campaign_id)
            )
        else:
            conn.execute(
                """
                INSERT INTO campaigns (id, name, product_name, product_description, price_original, price_promo, scarcity_limit, tone_of_voice, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (campaign_id, name, product_name, product_description, price_original, price_promo, scarcity_limit, tone_of_voice, is_active, stamp, stamp)
            )
        conn.commit()
        
    return {"status": "ok", "id": campaign_id}


@app.delete("/campaigns/{campaign_id}")
def delete_campaign_endpoint(campaign_id: str):
    crm.init_db()
    with crm.get_conn() as conn:
        conn.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
        conn.execute("UPDATE scheduled_tasks SET campaign_id = NULL WHERE campaign_id = ?", (campaign_id,))
        conn.commit()
    return {"status": "ok"}


def _extract_emails_from_dict(val, found_emails=None) -> set[str]:
    import re
    if found_emails is None:
        found_emails = set()
        
    email_regex = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
    
    if isinstance(val, dict):
        for k, v in val.items():
            _extract_emails_from_dict(v, found_emails)
    elif isinstance(val, list):
        for item in val:
            _extract_emails_from_dict(item, found_emails)
    elif isinstance(val, str):
        val_clean = val.strip().lower()
        if "@" in val_clean and email_regex.match(val_clean):
            found_emails.add(val_clean)
            
    return found_emails


def _extract_meeting_details(val, result=None) -> dict:
    import re
    if result is None:
        result = {"link": None, "time": None}
        
    meet_regex = re.compile(r"https?://(?:meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}|zoom\.us/j/\d+|cal\.com/meet/[^\"\s]+|calendly\.com/events/[^\"\s]+)")
    date_regex = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
    
    if isinstance(val, dict):
        for k in ["location", "join_url", "meet_link", "video_url"]:
            if k in val and isinstance(val[k], str) and val[k].startswith("http"):
                result["link"] = val[k]
        for k in ["start_time", "startTime", "date"]:
            if k in val and isinstance(val[k], str) and date_regex.search(val[k]):
                result["time"] = val[k]
                
        for k, v in val.items():
            _extract_meeting_details(v, result)
            
    elif isinstance(val, list):
        for item in val:
            _extract_meeting_details(item, result)
            
    elif isinstance(val, str):
        if not result["link"] and meet_regex.search(val):
            m = meet_regex.search(val)
            result["link"] = m.group(0)
        if not result["time"] and date_regex.match(val):
            result["time"] = val
            
    return result


@app.post("/webhooks/booking")
async def webhook_booking(req: Request):
    import crm
    crm.init_db()
    
    try:
        payload = await req.json()
    except Exception:
        return JSONResponse({"error": "Payload JSON inválido"}, status_code=400)
    
    emails = _extract_emails_from_dict(payload)
    if not emails:
        return JSONResponse({"ok": False, "message": "Nenhum e-mail identificado no agendamento."}, status_code=200)
        
    matched_contact = None
    with crm.get_conn() as conn:
        for email in emails:
            contact = crm.get_contact_by_email(email, conn=conn)
            if contact:
                matched_contact = contact
                break
                
    if not matched_contact:
        return JSONResponse({
            "ok": False, 
            "message": f"Nenhum lead correspondente encontrado para os e-mails: {list(emails)}"
        }, status_code=200)
        
    contact_id = matched_contact["id"]
    
    # Extrai detalhes da reunião
    details = _extract_meeting_details(payload)
    meet_link = details.get("link")
    meet_time = details.get("time")
    
    note_lines = ["📅 Novo agendamento detectado via Calendly/Cal.com."]
    if meet_time:
        try:
            dt_str = meet_time.replace("Z", "+00:00")
            dt = datetime.fromisoformat(dt_str)
            formatted_time = dt.strftime("%d/%m/%Y às %H:%M")
            note_lines.append(f"⏰ Horário: {formatted_time}")
        except Exception:
            note_lines.append(f"⏰ Horário: {meet_time}")
    if meet_link:
        note_lines.append(f"🎥 Sala Virtual: {meet_link}")
        
    full_note = "\n".join(note_lines)
    
    # Atualiza o status do lead para 'meeting' e registra nota formatada
    crm.update_contact_status(contact_id, "meeting", note=full_note)
    crm.add_note(contact_id, full_note, author="system")
    
    return {
        "ok": True,
        "message": f"Lead '{matched_contact['nome_empresa']}' movido para Reunião.",
        "contact_id": contact_id
    }


def run_instagram_automation_bg(contact_id: int, insta_url: str, message: str):
    import time
    import random
    from playwright.sync_api import sync_playwright
    import crm
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp("http://localhost:9222")
            context = browser.contexts[0]
            page = context.new_page()
            
            page.goto(insta_url)
            page.wait_for_load_state("networkidle")
            time.sleep(3)
            
            # Detecta se caiu na tela de login
            if "accounts/login" in page.url or page.locator("input[name='username']").count() > 0:
                print("[automação] Usuário não logado no Instagram. Aguardando login manual...")
                crm.add_note(contact_id, "⚠️ O robô abriu o Instagram, mas detectou que você não está logado neste perfil de navegador. Por favor, faça login nesta janela nova para a IA prosseguir.", author="system")
                try:
                    # Espera a URL conter o perfil do usuário ou não ser mais a tela de login por até 120s
                    for _ in range(120):
                        time.sleep(1)
                        if "accounts/login" not in page.url and page.locator("input[name='username']").count() == 0:
                            break
                    page.goto(insta_url)
                    page.wait_for_load_state("networkidle")
                    time.sleep(3)
                except Exception:
                    print("[automação] Tempo esgotado esperando o login.")
                    return
            
            # Descarta popups de 'Salvar login' ou 'Notificações' se aparecerem na tela
            for btn_text in ["Agora não", "Not now", "cancelar", "cancel"]:
                try:
                    locator = page.get_by_role("button", name=btn_text).first
                    if locator.is_visible(timeout=1000):
                        locator.click()
                        time.sleep(1)
                except Exception:
                    pass

            msg_btn = None
            selectors_to_try = [
                "//div[text()='Enviar mensagem']",
                "//div[text()='Message']",
                "//button[contains(., 'Enviar mensagem')]",
                "//button[contains(., 'Message')]",
                "a[href*='/direct/t/']",
                "role=button[name='Enviar mensagem']",
                "role=button[name='Message']"
            ]
            for selector in selectors_to_try:
                try:
                    locator = page.locator(selector).first
                    if locator.is_visible(timeout=2000):
                        msg_btn = locator
                        break
                except Exception:
                    continue
                    
            clicked = False
            if msg_btn:
                try:
                    msg_btn.click()
                    page.wait_for_url("**/direct/t/**", timeout=10000)
                    clicked = True
                except Exception:
                    print("[automação] Falha ao clicar no botão de Mensagem. Aguardando clique manual...")
                    
            if not clicked:
                # Se falhar o clique automático (ex: por popup na frente), espera o usuário clicar
                page.wait_for_url("**/direct/t/**", timeout=30000)
                
            time.sleep(4)
            
            textbox = None
            textbox_selectors = [
                "div[role='textbox']",
                "div[contenteditable='true']",
                "textarea[placeholder*='Mensagem']",
                "textarea[placeholder*='Message']"
            ]
            for selector in textbox_selectors:
                try:
                    locator = page.locator(selector).first
                    if locator.is_visible(timeout=3000):
                        textbox = locator
                        break
                except Exception:
                    continue
                    
            if textbox:
                textbox.click()
                time.sleep(1)
                for char in message:
                    textbox.type(char)
                    time.sleep(random.uniform(0.04, 0.12))
                    
                crm.add_note(contact_id, "🤖 Mensagem de direct digitada automaticamente via robô BigBoss OS.", author="system")
                # Mantém a aba e a conexão abertas por 5 minutos para o usuário revisar e apertar Enter
                time.sleep(300)
                
    except Exception as exc:
        print(f"[automação] erro no direct do instagram: {exc}")
        crm.add_note(contact_id, f"❌ Erro na digitação automática do Instagram: {str(exc)}", author="system")
        # Mantém a aba aberta por 3 minutos mesmo em caso de erro para o usuário não perder o contexto
        time.sleep(180)


@app.post("/crm/contacts/{contact_id}/outreach-instagram-auto")
async def crm_outreach_instagram_auto(contact_id: int, bg: BackgroundTasks):
    import crm
    import subprocess
    import socket
    import time
    
    crm.init_db()
    contact = crm.get_contact_full(contact_id)
    if not contact:
        return JSONResponse({"error": "Lead não encontrado"}, status_code=404)
        
    insta_url = contact.get("instagram")
    if not insta_url:
        return JSONResponse({"error": "Lead não possui Instagram cadastrado"}, status_code=400)
        
    message = contact.get("dm_instagram")
    if not message:
        return JSONResponse({"error": "Roteiro de DM do Instagram não gerado para este lead"}, status_code=400)
        
    # Verifica se a porta de depuração 9222 do Chrome está ativa
    chrome_open = False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", 9222))
        chrome_open = True
    except Exception:
        pass
    finally:
        s.close()
        
    if not chrome_open:
        try:
            profile_dir = "/Users/fabio/.gemini/antigravity/brain/0509120b-a4d2-4a8a-a3cc-110ff72d6262/scratch/chrome_profile"
            subprocess.Popen([
                "open", "-n", "-a", "Google Chrome",
                "--args",
                "--remote-debugging-port=9222",
                f"--user-data-dir={profile_dir}"
            ])
            # Espera até 6 segundos para a porta responder
            for _ in range(12):
                time.sleep(0.5)
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.5)
                try:
                    s.connect(("127.0.0.1", 9222))
                    s.close()
                    chrome_open = True
                    break
                except Exception:
                    pass
            if not chrome_open:
                return JSONResponse({"error": "Chrome demorou muito para responder na porta 9222."}, status_code=500)
        except Exception as exc:
            return JSONResponse({"error": f"Não foi possível iniciar o Chrome: {str(exc)}"}, status_code=500)
            
    bg.add_task(run_instagram_automation_bg, contact_id, insta_url, message)
    return {"ok": True, "message": "Automação de digitação iniciada no Chrome."}


@app.get("/templates")
def list_templates():
    return get_templates()


@app.post("/templates")
async def create_template_payload(req: Request):
    body = await req.json()
    try:
        return create_template(body)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.put("/templates/{tpl_id}")
async def update_template_payload(tpl_id: str, req: Request):
    body = await req.json()
    result = update_template(tpl_id, body)
    if not result:
        return JSONResponse({"error": "template not found"}, status_code=404)
    return result


@app.delete("/templates/{tpl_id}")
def delete_template_payload(tpl_id: str):
    if not delete_template(tpl_id):
        return JSONResponse({"error": "template not found"}, status_code=404)
    return {"ok": True}


@app.post("/templates/{tpl_id}/duplicate")
def duplicate_template_payload(tpl_id: str):
    result = duplicate_template(tpl_id)
    if not result:
        return JSONResponse({"error": "template not found"}, status_code=404)
    return result


@app.get("/templates/{tpl_id}/versions")
def list_template_versions(tpl_id: str):
    if not get_template(tpl_id):
        return JSONResponse({"error": "template not found"}, status_code=404)
    return get_template_versions(tpl_id)


@app.post("/templates/{tpl_id}/restore/{version_no}")
def restore_template_payload(tpl_id: str, version_no: int):
    result = restore_template_version(tpl_id, version_no)
    if not result:
        return JSONResponse({"error": "template version not found"}, status_code=404)
    return result


@app.post("/templates/preview")
async def preview_template_payload(req: Request):
    body = await req.json()
    template = body.get("template") or {}
    if body.get("template_id"):
        template = get_template(body["template_id"]) or template
    if not template:
        return JSONResponse({"error": "template not found"}, status_code=404)
    return render_template_preview(template, lead=body.get("lead"), company=body.get("company"))


@app.post("/templates/{tpl_id}/send-test")
async def send_test_template_payload(tpl_id: str, req: Request):
    from agents.sender import send_email, validate_sender_config

    try:
        config = load_config()
        template = get_template(tpl_id)
        if not template:
            return JSONResponse({"error": "template not found"}, status_code=404)

        validate_sender_config(config)
        body = await req.json() if req else {}
        requested_recipient = (body.get("recipient") or "").strip()

        sender_cfg = config.get("sender", {})
        test_recipient = (
            requested_recipient
            or sender_cfg.get("reply_to_email")
            or sender_cfg.get("email")
            or get_company().get("email")
            or ""
        ).strip()

        if not test_recipient:
            return JSONResponse({"error": "Configure um email de resposta para receber o teste."}, status_code=400)

        test_lead = {
            "nome_empresa": "Lead de teste",
            "segmento": "empresa de teste",
            "cidade": "sua cidade",
            "sinais": ["oportunidade mapeada"],
            "angulo": "teste",
        }
        preview = render_template_preview(template, lead=test_lead)
        subject = f"[TESTE] {preview['assunto']}".strip()
        lead = {
            "nome_empresa": "Envio de teste",
            "email": test_recipient,
            "email_assunto": subject,
            "email_corpo": preview["corpo"],
        }
        result = send_email(lead, config)
        return {
            "recipient": test_recipient,
            "subject": subject,
            "status": result.get("status"),
            "error": result.get("error"),
            "provider": result.get("provider"),
            "timestamp": result.get("timestamp"),
        }
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": f"Falha interna no envio de teste: {exc}"}, status_code=500)


@app.post("/leads/{idx}/apply-template/{tpl_id}")
async def apply_template(idx: int, tpl_id: str):
    leads = state["leads"]
    if idx >= len(leads):
        return JSONResponse({"error": "not found"}, status_code=404)
    company = get_company()
    tpl = get_template(tpl_id)
    if not tpl:
        return JSONResponse({"error": "template not found"}, status_code=404)

    lead = leads[idx]
    vars_ = {
        "empresa": lead.get("nome_empresa", "sua empresa"),
        "segmento": lead.get("segmento", "seu segmento"),
        "cidade": lead.get("cidade", "sua cidade"),
        "contact_name": company.get("contact_name", company.get("name", "")),
        "agency_email": company.get("email", ""),
        "agency_name": company.get("name", ""),
        "sinal_principal": (lead.get("sinais") or ["presença digital"])[0],
    }
    assunto = tpl["assunto"]
    corpo = tpl["corpo"]
    for k, v in vars_.items():
        assunto = assunto.replace(f"{{{k}}}", str(v))
        corpo = corpo.replace(f"{{{k}}}", str(v))

    lead["email_assunto"] = assunto.strip()
    lead["email_corpo"] = corpo.strip()
    lead["email_template_id"] = tpl_id
    lead["email_template_label"] = tpl["label"]
    if lead.get("email"):
        lead["email_status"] = "ready"

    return {"assunto": assunto.strip(), "corpo": corpo.strip(), "label": tpl["label"]}


@app.get("/tasks")
def get_tasks_payload():
    return list_tasks()


@app.post("/tasks")
async def create_task_payload(req: Request):
    try:
        body = await req.json()
        return create_task(body)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.put("/tasks/{task_id}")
async def update_task_payload(task_id: str, req: Request):
    try:
        body = await req.json()
        result = update_task(task_id, body)
        if not result:
            return JSONResponse({"error": "task not found"}, status_code=404)
        return result
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.delete("/tasks/{task_id}")
def delete_task_payload(task_id: str):
    if not delete_task(task_id):
        return JSONResponse({"error": "task not found"}, status_code=404)
    return {"ok": True}


@app.get("/tasks/{task_id}/runs")
def get_task_runs_payload(task_id: str):
    if not get_task(task_id):
        return JSONResponse({"error": "task not found"}, status_code=404)
    return list_task_runs(task_id, 20)


@app.get("/task-runs")
def get_recent_task_runs():
    return get_recent_runs(30)


@app.post("/tasks/{task_id}/run-now")
def run_task_now_payload(task_id: str):
    task = get_task(task_id)
    if not task:
        return JSONResponse({"error": "task not found"}, status_code=404)
    with scheduler_state["lock"]:
        if task_id in scheduler_state["running_tasks"]:
            return JSONResponse({"error": "task already running"}, status_code=409)
        scheduler_state["running_tasks"].add(task_id)
    threading.Thread(target=_run_task_thread, args=(task_id, "manual"), daemon=True).start()
    return {"ok": True}


@app.get("/crm/history")
def crm_history():
    import crm; crm.init_db()
    return crm.get_history(100)


@app.get("/crm/replies")
def crm_replies(status: Optional[str] = "unread"):
    import crm; crm.init_db()
    return crm.get_email_replies(status_filter=status, limit=100)


@app.post("/crm/replies/{reply_id}/archive")
def crm_archive_reply(reply_id: int):
    import crm
    crm.init_db()
    crm.update_reply_status(reply_id, "archived")
    return {"ok": True}


@app.post("/crm/envios/{envio_id}/archive")
def crm_archive_envio(envio_id: int):
    import crm
    crm.init_db()
    crm.update_envio_status(envio_id, "archived")
    return {"ok": True}


@app.post("/crm/contacts/{contact_id}/archive-replies")
def crm_archive_contact_replies(contact_id: int):
    import crm
    crm.init_db()
    crm.archive_contact_replies(contact_id)
    return {"ok": True}


@app.post("/crm/contacts/{contact_id}/archive-envios")
def crm_archive_contact_envios(contact_id: int):
    import crm
    crm.init_db()
    crm.archive_contact_envios(contact_id)
    return {"ok": True}


@app.get("/crm/contacts")
def crm_contacts(status: Optional[str] = None, run_id: Optional[str] = None):
    import crm; crm.init_db()
    return crm.get_contacts(200, status_filter=status, run_id=run_id)


@app.post("/crm/contacts/clear")
def crm_clear_all_contacts():
    import crm
    crm.init_db()
    with crm.get_conn() as conn:
        conn.execute("DELETE FROM crm_events")
        conn.execute("DELETE FROM crm_notes")
        conn.execute("DELETE FROM crm_followups")
        conn.execute("DELETE FROM crm_email_replies")
        conn.execute("DELETE FROM envios")
        conn.execute("DELETE FROM leads")
        conn.execute("DELETE FROM crm_contacts")
        conn.commit()
    return {"ok": True, "message": "Todos os contatos foram limpos com sucesso."}



@app.get("/crm/contacts/{contact_id}")
def crm_contact_detail(contact_id: int):
    import crm; crm.init_db()
    contact = crm.get_contact_full(contact_id)
    if not contact:
        return JSONResponse({"error": "contact not found"}, status_code=404)
    return contact


@app.put("/crm/contacts/{contact_id}")
async def crm_update_contact(contact_id: int, req: Request):
    import crm
    crm.init_db()
    body = await req.json()
    try:
        contact = crm.update_contact_status(contact_id, body.get("status"), note=body.get("note"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not contact:
        return JSONResponse({"error": "contact not found"}, status_code=404)
    return contact


@app.post("/crm/contacts/{contact_id}/send")
async def crm_send_contact(contact_id: int):
    import crm
    from agents.sender import send_email
    crm.init_db()
    contact = crm.get_contact_full(contact_id)
    if not contact:
        return JSONResponse({"error": "contact not found"}, status_code=404)
    if not contact.get("email_draft_assunto"):
        return JSONResponse({"error": "Sem rascunho de email para este contato."}, status_code=400)
    if not contact.get("primary_email"):
        return JSONResponse({"error": "Email do destinatário não disponível."}, status_code=400)

    lead = {
        "nome_empresa": contact["nome_empresa"],
        "email": contact["primary_email"],
        "email_assunto": contact["email_draft_assunto"],
        "email_corpo": contact["email_draft_corpo"],
        "crm_contact_id": contact_id,
    }
    allowed, reason, _ = crm.can_send_lead(lead)
    if not allowed:
        return JSONResponse({"error": reason}, status_code=409)

    config = load_config()
    result = send_email(lead, config)
    crm.register_send(
        lead,
        state.get("last_run_id") or "crm_manual",
        None,
        result["status"],
        result.get("error"),
        provider=result.get("provider"),
        provider_message_id=result.get("provider_message_id"),
    )
    return result


@app.post("/crm/send-queue")
async def crm_send_queue(req: Request):
    import crm
    from agents.sender import remaining_daily_quota, send_email, validate_sender_config

    crm.init_db()
    body = await req.json() if req else {}
    ids = body.get("ids") or []
    status_filter = body.get("status") or "action:send_email"
    config = load_config()
    delay = int(config.get("limits", {}).get("delay_between_sends_seconds") or 0)

    try:
        validate_sender_config(config)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    quota = remaining_daily_quota(config)
    if quota <= 0:
        return {
            "sent": 0,
            "errors": 0,
            "blocked": 0,
            "skipped": 0,
            "daily_limit": True,
            "message": "Limite diário de envio atingido.",
        }

    if ids:
        contacts = [crm.get_contact_full(int(contact_id)) for contact_id in ids]
        contacts = [contact for contact in contacts if contact]
    else:
        contacts = crm.get_contacts(500, status_filter=status_filter)

    sent = errors = blocked = skipped = 0
    details = []

    for contact in contacts:
        if sent >= quota:
            skipped += 1
            details.append({"id": contact["id"], "status": "queued_daily_limit"})
            continue

        if not contact.get("email_draft_assunto") or not contact.get("email_draft_corpo"):
            skipped += 1
            details.append({"id": contact["id"], "status": "missing_draft"})
            continue

        if not contact.get("primary_email"):
            skipped += 1
            details.append({"id": contact["id"], "status": "missing_email"})
            continue

        lead = {
            "nome_empresa": contact.get("nome_empresa"),
            "email": contact.get("primary_email"),
            "email_assunto": contact.get("email_draft_assunto"),
            "email_corpo": contact.get("email_draft_corpo"),
            "website": contact.get("website"),
            "instagram": contact.get("instagram"),
            "crm_contact_id": contact["id"],
        }

        allowed, reason, _ = crm.can_send_lead(lead)
        if not allowed:
            blocked += 1
            details.append({"id": contact["id"], "status": "blocked", "error": reason})
            continue

        result = send_email(lead, config)
        crm.register_send(
            lead,
            state.get("last_run_id") or "crm_queue",
            None,
            result["status"],
            result.get("error"),
            provider=result.get("provider"),
            provider_message_id=result.get("provider_message_id"),
        )
        if result["status"] == "sent":
            sent += 1
            details.append({"id": contact["id"], "status": "sent", "provider": result.get("provider")})
            if delay > 0 and sent < min(quota, len(contacts)):
                time.sleep(delay)
        else:
            errors += 1
            details.append({"id": contact["id"], "status": "error", "error": result.get("error")})

    return {
        "sent": sent,
        "errors": errors,
        "blocked": blocked,
        "skipped": skipped,
        "daily_limit": sent >= quota and sent < len(contacts),
        "remaining_quota": max(quota - sent, 0),
        "details": details,
    }


@app.post("/crm/contacts/{contact_id}/note")
async def crm_add_note(contact_id: int, req: Request):
    import crm; crm.init_db()
    body = await req.json()
    note_text = (body.get("note") or "").strip()
    if not note_text:
        return JSONResponse({"error": "Nota vazia."}, status_code=400)
    if not crm.get_contact(contact_id):
        return JSONResponse({"error": "contact not found"}, status_code=404)
    note = crm.add_note(contact_id, note_text)
    return note


@app.put("/crm/contacts/{contact_id}/recall-date")
async def crm_set_recall_date(contact_id: int, req: Request):
    import crm; crm.init_db()
    body = await req.json()
    date_str = (body.get("date") or "").strip()
    if not crm.get_contact(contact_id):
        return JSONResponse({"error": "contact not found"}, status_code=404)
    with crm.get_conn() as conn:
        now = datetime.now().isoformat()
        conn.execute(
            "UPDATE crm_contacts SET recall_at = ?, status = 'recall', updated_at = ? WHERE id = ?",
            (date_str or None, now, contact_id),
        )
    return crm.get_contact(contact_id)


@app.post("/crm/contacts/{contact_id}/followup/gen")
async def crm_gen_followup(contact_id: int, req: Request):
    import crm
    from agents.ai import ask_json
    crm.init_db()
    contact = crm.get_contact_full(contact_id)
    if not contact:
        return JSONResponse({"error": "contact not found"}, status_code=404)

    body = await req.json()
    attempt_no = int(body.get("attempt_no") or 2)
    scheduled_for = (body.get("scheduled_for") or "").strip() or None

    last_envio = next((e for e in contact.get("envios", [])), None)
    context = (
        f"Empresa: {contact.get('nome_empresa')}\n"
        f"Segmento: {contact.get('segmento')}\n"
        f"Último email enviado em: {last_envio['enviado_em'] if last_envio else 'desconhecido'}\n"
        f"Assunto anterior: {contact.get('email_draft_assunto') or (last_envio or {}).get('assunto', '')}\n"
        f"Notas: {'; '.join(n['note'] for n in contact.get('notes', []))}"
    )

    try:
        result = ask_json(
            f"Gere um follow-up (tentativa {attempt_no}) para este lead que não respondeu ao primeiro email.\n\n"
            f"Contexto:\n{context}\n\n"
            "Retorne JSON com: assunto (string) e corpo (string). Tom consultivo, curto, diferente do anterior.",
            "Você é copywriter especializado em email outbound B2B brasileiro. Retorne APENAS JSON válido.",
        )
        assunto = result.get("assunto", "")
        corpo = result.get("corpo", "")
    except Exception as exc:
        return JSONResponse({"error": f"Erro ao gerar follow-up: {exc}"}, status_code=500)

    followup = crm.add_followup(contact_id, attempt_no, assunto, corpo, scheduled_for)
    return followup


@app.post("/crm/contacts/{contact_id}/reply")
async def crm_send_reply(contact_id: int, req: Request):
    import crm
    from agents.sender import send_email
    crm.init_db()
    contact = crm.get_contact_full(contact_id)
    if not contact:
        return JSONResponse({"error": "contact not found"}, status_code=404)
    if not contact.get("primary_email"):
        return JSONResponse({"error": "Email do destinatário não disponível."}, status_code=400)

    body = await req.json()
    subject = (body.get("subject") or "").strip()
    body_text = (body.get("body_text") or "").strip()
    if not subject or not body_text:
        return JSONResponse({"error": "Assunto e mensagem são obrigatórios."}, status_code=400)

    lead = {
        "nome_empresa": contact["nome_empresa"],
        "email": contact["primary_email"],
        "email_assunto": subject,
        "email_corpo": body_text,
        "crm_contact_id": contact_id,
    }

    config = load_config()
    result = send_email(lead, config)
    crm.register_send(
        lead,
        state.get("last_run_id") or "crm_reply",
        None,
        result["status"],
        result.get("error"),
        provider=result.get("provider"),
        provider_message_id=result.get("provider_message_id"),
    )
    crm.add_note(contact_id, f"Resposta enviada por e-mail: '{subject}'", author="user")
    return result


@app.post("/inbox/send-direct")
async def inbox_send_direct(req: Request):
    import crm
    from agents.sender import send_email, validate_sender_config
    
    body = await req.json()
    to_email = (body.get("to_email") or "").strip()
    company_name = (body.get("company_name") or "").strip() or to_email.split("@")[0].capitalize()
    subject = (body.get("subject") or "").strip()
    body_text = (body.get("body_text") or "").strip()
    
    if not to_email or "@" not in to_email:
        return JSONResponse({"error": "E-mail de destino inválido."}, status_code=400)
    if not subject:
        return JSONResponse({"error": "Assunto é obrigatório."}, status_code=400)
    if not body_text:
        return JSONResponse({"error": "Corpo do e-mail é obrigatório."}, status_code=400)
        
    config = load_config()
    try:
        validate_sender_config(config)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
        
    crm.init_db()
    # Find or create contact
    contact = crm.get_contact_by_email(to_email)
    if not contact:
        new_c = crm.upsert_contact({
            "nome_empresa": company_name,
            "email": to_email,
            "canal_origem": "direct_inbox",
            "status": "sent_1x",
            "score": 10,
            "email_draft_assunto": subject,
            "email_draft_corpo": body_text,
        })
        contact_id = new_c["id"]
    else:
        contact_id = contact["id"]
        crm.update_contact_status(contact_id, "sent_1x")
        
    lead = {
        "nome_empresa": company_name,
        "email": to_email,
        "email_assunto": subject,
        "email_corpo": body_text,
        "crm_contact_id": contact_id,
    }
    
    result = send_email(lead, config)
    if result.get("status") == "sent":
        crm.register_send(
            lead,
            "inbox_compose",
            None,
            "sent",
            None,
            provider=result.get("provider"),
            provider_message_id=result.get("provider_message_id"),
        )
        crm.add_note(contact_id, f"Novo e-mail enviado diretamente: '{subject}'", author="user")
        return {"status": "sent", "contact_id": contact_id, "result": result}
    else:
        crm.register_send(
            lead,
            "inbox_compose",
            None,
            "error",
            result.get("error"),
            provider=result.get("provider"),
        )
        return JSONResponse({"error": result.get("error") or "Erro ao enviar e-mail."}, status_code=500)


@app.post("/crm/contacts/{contact_id}/reply/suggest")
async def crm_suggest_reply(contact_id: int):
    import crm
    from agents.ai import ask_json
    crm.init_db()
    contact = crm.get_contact_full(contact_id)
    if not contact:
        return JSONResponse({"error": "contact not found"}, status_code=404)

    replies = contact.get("replies", [])
    last_reply = replies[0] if replies else {}
    reply_text = last_reply.get("body_preview") or last_reply.get("subject") or "Gostaria de agendar uma reunião."
    
    config = load_config()
    co = config.get("company", {})
    sender = config.get("sender", {})
    co_name = co.get("name") or "Nossa Empresa"
    rep_name = sender.get("name") or "Fabio"
    
    prompt = f"""Você é o especialista comercial da {co_name}.
O lead '{contact.get('nome_empresa')}' ({contact.get('segmento')}, {contact.get('cidade')}) respondeu à nossa mensagem com o seguinte texto:

---
"{reply_text}"
---

Elabore uma resposta amigável, direta, profissional e persuasiva de follow-up para dar sequência à conversa ou confirmar a reunião/próximo passo.
Nome do remetente: {rep_name}
Empresa: {co_name}

Retorne apenas JSON no formato:
{{
  "subject": "Re: {last_reply.get('subject') or ('Contato ' + contact.get('nome_empresa'))}",
  "body_text": "Texto completo da resposta pronto para envio."
}}"""

    try:
        res = ask_json(prompt, "Você é um assistente de vendas de alto nível. Retorne estritamente JSON.")
        return res
    except Exception as exc:
        return {
            "subject": f"Re: {last_reply.get('subject') or ('Contato ' + contact.get('nome_empresa'))}",
            "body_text": f"Olá! Agradeço pelo retorno.\n\nFico à disposição para darmos sequência e alinharmos os detalhes.\n\nQual o melhor dia e horário para conversarmos?\n\nAbraços,\n{rep_name} - {co_name}"
        }


@app.get("/crm/stats")
def crm_stats():
    import crm; crm.init_db()
    return crm.get_stats()


@app.get("/crm/followups/eligible")
def crm_followups_eligible():
    from agents.followup_engine import find_eligible_followups
    return find_eligible_followups(min_days=3, max_attempts=3)


@app.post("/crm/followups/run-cycle")
async def crm_followups_run_cycle(req: Request = None):
    from agents.followup_engine import run_followup_cycle
    body = await req.json() if req else {}
    min_days = int(body.get("min_days") or 3)
    max_attempts = int(body.get("max_attempts") or 3)
    limit = int(body.get("limit") or 20)
    config = load_config()
    return run_followup_cycle(config, min_days=min_days, max_attempts=max_attempts, limit=limit)


def _resolve_campaign_override(contact: dict, co_name: str, config: dict) -> tuple[str, str]:
    """Retorna o nome da empresa remetente e detalhes da oferta da campanha, se houver."""
    campaign_id = contact.get("campaign_id")
    offer_details = ""
    if campaign_id:
        import crm
        try:
            with crm.get_conn() as conn:
                row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
                if row:
                    camp = dict(row)
                    co_name = camp.get("product_name") or co_name
                    offer_details = f"\nDetalhes da Oferta Promocional Ativa ({camp.get('name')}):\n"
                    offer_details += f"- Produto/Serviço: {camp.get('product_name')}\n"
                    offer_details += f"- Oferta: {camp.get('product_description')}\n"
                    if camp.get("price_promo"):
                        offer_details += f"- Preço Promocional: R$ {camp.get('price_promo')}\n"
                    if camp.get("price_original"):
                        offer_details += f"- Preço Original: R$ {camp.get('price_original')}\n"
                    if camp.get("scarcity_limit"):
                        offer_details += f"- Escassez/Limite: {camp.get('scarcity_limit')} projetos/vagas restantes\n"
                    if camp.get("tone_of_voice"):
                        offer_details += f"- Tom de voz e regras adicionais de copy: {camp.get('tone_of_voice')}\n"
        except Exception:
            pass
    return co_name, offer_details


@app.post("/crm/contacts/{contact_id}/whatsapp-script")
async def crm_whatsapp_script(contact_id: int):
    import crm
    from agents.ai import ask_json
    crm.init_db()
    contact = crm.get_contact_full(contact_id)
    if not contact:
        return JSONResponse({"error": "contact not found"}, status_code=404)
        
    config = load_config()
    co_name = config.get("company", {}).get("name") or "BigBoss OS"
    sender_name = config.get("sender", {}).get("name") or "Fabio"
    co_name, offer_context = _resolve_campaign_override(contact, co_name, config)
    
    prompt = f"""Você é o especialista comercial da {co_name}.
{offer_context}
Crie uma mensagem curta, consultiva e de alto impacto para enviar no WhatsApp do seguinte lead:
- Empresa: {contact.get('nome_empresa')}
- Segmento: {contact.get('segmento')}
- Cidade: {contact.get('cidade')}
- Telefone: {contact.get('telefone')}
- Site: {contact.get('website')}
- Instagram: {contact.get('instagram')}
- Gargalos/Sinais: {contact.get('sinais')}

Diretrizes:
- Mensagem para WhatsApp: extremamente natural, sem parecer robô ou spam.
- Quebre em 2 ou 3 parágrafos curtos.
- Apresente-se ({sender_name} da {co_name}), elogie um ponto da empresa e faça uma pergunta aberta/convite para mostrar um diagnóstico rápido ou apresentar a oferta/campanha detalhada.
- Máximo 60 palavras.

Retorne APENAS JSON no formato:
{{
  "message": "Texto completo da mensagem pronto para o WhatsApp."
}}"""

    try:
        res = ask_json(prompt, "Você é copywriter especialista em abordagem comercial via WhatsApp. Retorne estritamente JSON.")
        return {"script": res.get("message") or ""}
    except Exception as exc:
        return {
            "script": f"Olá! Tudo bem com vocês da {contact.get('nome_empresa')}?\n\nAqui é o {sender_name} da {co_name}. Estava dando uma olhada no perfil de vocês e achei o trabalho excelente!\n\nPreparamos uma análise rápida com algumas oportunidades para acelerar as vendas e visibilidade de vocês. Faz sentido eu te mandar por aqui?"
        }


@app.post("/crm/contacts/{contact_id}/instagram-script")
async def crm_instagram_script(contact_id: int):
    import crm
    from agents.ai import ask_json
    crm.init_db()
    contact = crm.get_contact_full(contact_id)
    if not contact:
        return JSONResponse({"error": "contact not found"}, status_code=404)
        
    config = load_config()
    co_name = config.get("company", {}).get("name") or "BigBoss OS"
    sender_name = config.get("sender", {}).get("name") or "Fabio"
    co_name, offer_context = _resolve_campaign_override(contact, co_name, config)
    
    prompt = f"""Você é o especialista comercial da {co_name}.
{offer_context}
Crie uma mensagem super curta, informal e de altíssimo impacto para enviar no direct (DM) do Instagram do seguinte lead:
- Empresa: {contact.get('nome_empresa')}
- Segmento: {contact.get('segmento')}
- Cidade: {contact.get('cidade')}
- Biografia/Sinais no Instagram: {contact.get('sinais')}
- Site: {contact.get('website')}

Diretrizes:
- Mensagem para Instagram Direct: extremamente casual, curta, focada em rede social.
- Evite jargões corporativos ou formalidades excessivas. Pode usar quebras de linha leves.
- Mencione que viu o perfil deles no Instagram e identifique um gancho ou oportunidade de melhoria rápida, ou faça a chamada para a oferta promocional.
- Faça uma pergunta de gancho simples para iniciar a conversa: "Faz sentido eu te mandar o link desse diagnóstico por aqui?" ou similar.
- Máximo 45 palavras.

Retorne APENAS JSON no formato:
{{
  "message": "Texto completo da abordagem pronto para a DM do Instagram."
}}"""

    try:
        res = ask_json(prompt, "Você é copywriter especialista em abordagem comercial via Instagram DM. Retorne estritamente JSON.")
        return {"script": res.get("message") or ""}
    except Exception as exc:
        return {
            "script": f"Olá, pessoal da {contact.get('nome_empresa')}! Tudo bem?\n\nAchei o perfil de vocês muito bacana, mas notei um detalhe simples no feed/link que pode estar fazendo vocês perderem clientes para a concorrência local.\n\nFizemos um diagnóstico rápido de 2 min com o que ajustar para resolver isso. Faz sentido eu te mandar por aqui?"
        }


@app.post("/crm/contacts/{contact_id}/tiktok-script")
async def crm_tiktok_script(contact_id: int):
    import crm
    from agents.ai import ask_json
    crm.init_db()
    contact = crm.get_contact_full(contact_id)
    if not contact:
        return JSONResponse({"error": "contact not found"}, status_code=404)
        
    config = load_config()
    co_name = config.get("company", {}).get("name") or "BigBoss OS"
    sender_name = config.get("sender", {}).get("name") or "Fabio"
    co_name, offer_context = _resolve_campaign_override(contact, co_name, config)
    
    prompt = f"""Você é o especialista comercial da {co_name}.
{offer_context}
Crie uma mensagem super curta, dinâmica e inovadora para enviar na DM do TikTok do seguinte lead:
- Empresa: {contact.get('nome_empresa')}
- Segmento: {contact.get('segmento')}
- Cidade: {contact.get('cidade')}
- Site: {contact.get('website')}
- Gargalos/Sinais técnicos: {contact.get('sinais')}

Diretrizes:
- Mensagem para TikTok: tom moderno, dinâmico e focado em conteúdo visual (vídeos curtos, criativos para anúncios, posicionamento para audiência jovem, ou integração com TikTok Shop).
- Mencione que analisou a presença deles em vídeo ou TikTok Shop (ou a oferta da campanha).
- Faça um convite direto e informal: "Faz sentido eu te mandar esse diagnóstico rápido?"
- Máximo 45 palavras.

Retorne APENAS JSON no formato:
{{
  "message": "Texto completo da abordagem pronto para a DM do TikTok."
}}"""

    try:
        res = ask_json(prompt, "Você é copywriter especialista em abordagem comercial via TikTok DM. Retorne estritamente JSON.")
        return {"script": res.get("message") or ""}
    except Exception as exc:
        return {
            "script": f"Olá! Tudo bem?\n\nEstava analisando o posicionamento de vocês em vídeo e vi um potencial enorme para atrair mais clientes usando TikTok Ads e edições dinâmicas.\n\nPreparamos uma análise rápida de 2 min de como fazer isso sem mistério. Posso te enviar o link por aqui?"
        }


@app.get("/crm/analytics")
def crm_analytics():
    import crm
    crm.init_db()
    with crm.get_conn() as conn:
        total_leads = conn.execute("SELECT COUNT(*) FROM crm_contacts").fetchone()[0]
        total_sent = conn.execute("SELECT COUNT(*) FROM envios WHERE status = 'sent'").fetchone()[0]
        total_replies = conn.execute("SELECT COUNT(*) FROM crm_email_replies").fetchone()[0]
        total_meetings = conn.execute("SELECT COUNT(*) FROM crm_contacts WHERE status = 'meeting'").fetchone()[0]
        total_blocked = conn.execute("SELECT COUNT(*) FROM crm_contacts WHERE status = 'blocked'").fetchone()[0]
        active_followups = conn.execute("SELECT COUNT(*) FROM crm_contacts WHERE status = 'sent_1x' AND send_count >= 1").fetchone()[0]
        
        # Template performance
        tpl_rows = conn.execute(
            """
            SELECT e.assunto, 
                   COUNT(e.id) as total_sent,
                   (SELECT COUNT(*) FROM crm_email_replies r 
                    WHERE r.contact_id = e.contact_id) as total_replies
            FROM envios e
            WHERE e.status = 'sent'
            GROUP BY e.assunto
            ORDER BY total_sent DESC
            LIMIT 6
            """
        ).fetchall()
        
    reply_rate = round((total_replies / total_sent * 100), 1) if total_sent > 0 else 0.0
    
    return {
        "total_leads": total_leads,
        "total_sent": total_sent,
        "total_replies": total_replies,
        "total_meetings": total_meetings,
        "total_blocked": total_blocked,
        "active_followups": active_followups,
        "reply_rate": reply_rate,
        "template_performance": [dict(r) for r in tpl_rows]
    }


# ── Pilares Estratégicos: Cavalo de Tróia, Auto-Pilot & Fila WhatsApp ──────────

@app.post("/templates/generate-ai-presets")
def generate_ai_template_endpoint():
    from agents.copy_innovator import generate_innovative_template
    try:
        tpl = generate_innovative_template()
        return {"status": "ok", "template": tpl}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/tasks/auto-pilot-generate")
def auto_pilot_tasks_endpoint():
    from agents.strategist import auto_discover_and_create_routines
    try:
        created = auto_discover_and_create_routines(max_create=2)
        return {"status": "ok", "created_count": len(created), "tasks": created}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/crm/whatsapp-queue")
def get_whatsapp_queue_endpoint():
    from agents.whatsapp_queue import get_daily_whatsapp_queue
    try:
        queue = get_daily_whatsapp_queue()
        return {"queue": queue, "total": len(queue), "pending": len([q for q in queue if not q.get("whats_sent")])}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/crm/contacts/{contact_id}/mark-whatsapp-sent")
def mark_contact_whatsapp_sent_endpoint(contact_id: int):
    from agents.whatsapp_queue import mark_whatsapp_sent
    try:
        res = mark_whatsapp_sent(contact_id)
        return res
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/crm/contacts/{contact_id}/log-outreach")
async def log_contact_outreach(contact_id: int, req: Request):
    import crm
    try:
        body = await req.json()
        platform = body.get("platform", "whatsapp")
        crm.init_db()
        
        event_type = f"{platform}_sent"
        note_text = f"Abordagem manual realizada via {platform.capitalize()}."
        
        if platform == "whatsapp":
            icon = "📲"
            label = "WhatsApp"
        elif platform == "instagram":
            icon = "📸"
            label = "Instagram DM"
        elif platform == "tiktok":
            icon = "🎵"
            label = "TikTok DM"
        else:
            icon = "👤"
            label = platform.capitalize()

        with crm.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO crm_events (contact_id, event_type, from_status, to_status, note, created_at)
                VALUES (?, ?, NULL, NULL, ?, datetime('now'))
                """,
                (contact_id, event_type, note_text)
            )
            # Atualiza status se for novo ou pronto para envio
            conn.execute(
                """
                UPDATE crm_contacts
                SET status = 'sent_1x', send_count = send_count + 1, last_sent_at = datetime('now'), updated_at = datetime('now')
                WHERE id = ? AND status IN ('new', 'ready')
                """,
                (contact_id,)
            )
            
        crm.add_note(contact_id, f"{icon} Abordagem manual via {label} realizada.", author="user")
        return {"status": "ok", "contact_id": contact_id}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/crm/daily-digest/send")
def send_daily_digest_endpoint(req: Request):
    from agents.whatsapp_queue import send_daily_digest_alert
    try:
        res = send_daily_digest_alert("fabio@ultraweb.com.br")
        return res
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Setup / Onboarding ───────────────────────────────────────────────────────

@app.get("/setup/status")
def get_setup_status():
    import shutil
    from pathlib import Path

    # Resolva binário de IA: pode ser codex ou claude
    ai_bin = shutil.which("codex") or shutil.which("claude")
    if not ai_bin:
        for candidate in [
            Path.home() / ".local" / "bin" / "codex",
            Path.home() / ".local" / "bin" / "claude",
            Path("/opt/homebrew/bin/codex"),
            Path("/opt/homebrew/bin/claude"),
            Path("/usr/local/bin/codex"),
            Path("/usr/local/bin/claude"),
            Path("/usr/bin/codex"),
            Path("/usr/bin/claude"),
        ]:
            if candidate.exists():
                ai_bin = str(candidate)
                break
    ai_installed = bool(ai_bin)

    # Usa diretório isolado do app — não depende do ~/.claude do sistema
    app_home = BASE_DIR / ".app-home"
    # Claude ou Codex salvam o token em .claude.json ou .codex.json
    ai_authenticated = False
    for filename in [".codex.json", ".claude.json"]:
        token_file = app_home / filename
        if token_file.exists() and token_file.stat().st_size > 100:
            try:
                import json as _json
                data = _json.loads(token_file.read_text())
                oauth = data.get("oauthAccount")
                # Só autentica se oauthAccount for um dict com dados reais de conta
                if isinstance(oauth, dict) and bool(oauth.get("emailAddress") or oauth.get("accountUuid") or oauth.get("id") or oauth.get("email")):
                    ai_authenticated = True
                    break
            except Exception:
                pass

    settings = get_settings()
    company = settings.get("company") or {}
    sender = settings.get("sender") or {}
    company_configured = bool((company.get("name") or "").strip())
    sender_configured = bool(
        (sender.get("name") or "").strip() and
        (sender.get("reply_to_email") or "").strip()
    )

    return {
        "claude_installed": ai_installed,
        "claude_authenticated": ai_authenticated,
        "company_configured": company_configured,
        "sender_configured": sender_configured,
        "ready": all([ai_installed, ai_authenticated, company_configured, sender_configured]),
    }


@app.post("/setup/install-claude")
def install_claude():
    """Instala o Claude/Codex CLI em background e abre o browser para OAuth."""
    import shutil
    import subprocess
    import threading
    from pathlib import Path

    def _run():
        # 1. Instala se necessário
        ai_bin = shutil.which("codex") or shutil.which("claude")
        if not ai_bin:
            npm = shutil.which("npm")
            if not npm:
                for candidate in [
                    Path.home() / ".nvm/versions/node/v24.13.1/bin/npm",
                    Path("/opt/homebrew/bin/npm"),
                    Path("/usr/local/bin/npm"),
                ]:
                    if candidate.exists():
                        npm = str(candidate)
                        break
            if npm:
                subprocess.run([npm, "install", "-g", "@anthropic-ai/claude-code"],
                               capture_output=True)

        # 2. Abre o browser de autenticação
        ai_bin = shutil.which("codex") or shutil.which("claude")
        if not ai_bin:
            for candidate in [
                Path.home() / ".local/bin/codex",
                Path.home() / ".local/bin/claude",
                Path("/opt/homebrew/bin/codex"),
                Path("/opt/homebrew/bin/claude"),
                Path("/usr/local/bin/codex"),
                Path("/usr/local/bin/claude"),
            ]:
                if candidate.exists():
                    ai_bin = str(candidate)
                    break

        if ai_bin:
            app_home = str(BASE_DIR / ".app-home")
            Path(app_home).mkdir(exist_ok=True)
            env = os.environ.copy()
            env["PATH"] = f"{Path(ai_bin).parent}:/opt/homebrew/bin:/usr/local/bin:{env.get('PATH','')}"
            env["HOME"] = app_home
            env["TERM"] = "xterm-256color"

            # Usa pseudo-terminal para que o Claude detecte TTY e abra o browser OAuth
            import pty, select, re as _re, time as _time

            master_fd, slave_fd = pty.openpty()
            proc = subprocess.Popen(
                [ai_bin],
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                env=env, close_fds=True,
            )
            os.close(slave_fd)

            # Lê output por até 15s buscando URL de auth
            output = b""
            deadline = _time.time() + 15
            while _time.time() < deadline and proc.poll() is None:
                try:
                    r, _, _ = select.select([master_fd], [], [], 0.3)
                    if r:
                        output += os.read(master_fd, 4096)
                        text = _re.sub(rb'\x1b\[[0-9;]*[mGKHF]', b'', output).decode('utf-8', errors='ignore')
                        m = _re.search(r'https://\S+', text)
                        if m:
                            subprocess.run(["open", m.group(0)])
                            break
                except OSError:
                    break
            try:
                os.close(master_fd)
            except OSError:
                pass

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True}


# ── UI ───────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    html = (BASE_DIR / "ui.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    import signal
    PORT = 7860

    # Mata qualquer processo que esteja ocupando a porta antes de subir
    import subprocess as _sp
    for lsof_path in ["/usr/sbin/lsof", "/usr/bin/lsof", "lsof"]:
        try:
            _result = _sp.run([lsof_path, "-ti", f":{PORT}"], capture_output=True, text=True)
            for _pid in _result.stdout.strip().splitlines():
                try:
                    os.kill(int(_pid), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass
            break
        except FileNotFoundError:
            continue

    for d in [LEADS_DIR, EMAILS_DIR, LOGS_DIR]:
        d.mkdir(parents=True, exist_ok=True)
    load_env()
    init_settings_db()
    init_templates_db()
    init_tasks_db()

    print("\nMotor de Prospecção rodando em http://127.0.0.1:7860\n")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")

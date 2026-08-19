"""
Envia emails e registra logs.
Prioriza provider gerenciado pela plataforma; SMTP fica como fallback técnico.
"""
import json
import os
import re
import smtplib
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv
load_dotenv()

import crm


def _slug_tag(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_-]+", "-", (value or "").strip())
    return clean.strip("-")[:120] or "unknown"


def _sender_cfg(config: dict) -> dict:
    return config.get("sender", {})


def _provider(config: dict) -> str:
    return (_sender_cfg(config).get("provider") or "platform_managed").strip()


def _platform_from_email(config: dict) -> str:
    sender_cfg = _sender_cfg(config)
    return (
        sender_cfg.get("platform_from_email")
        or sender_cfg.get("email")
        or os.environ.get("PLATFORM_FROM_EMAIL")
        or ""
    ).strip()


def _platform_from_name(config: dict) -> str:
    sender_cfg = _sender_cfg(config)
    return (
        sender_cfg.get("platform_from_name")
        or sender_cfg.get("name")
        or os.environ.get("PLATFORM_FROM_NAME")
        or "BigBoss OS"
    ).strip()


def _reply_to_email(config: dict) -> str:
    sender_cfg = _sender_cfg(config)
    return (
        sender_cfg.get("reply_to_email")
        or sender_cfg.get("email")
        or ""
    ).strip()


def _reply_to_name(config: dict) -> str:
    sender_cfg = _sender_cfg(config)
    return (
        sender_cfg.get("reply_to_name")
        or sender_cfg.get("name")
        or ""
    ).strip()


def _resend_api_key(config: dict) -> str:
    sender_cfg = _sender_cfg(config)
    return (
        sender_cfg.get("resend_api_key")
        or os.environ.get("RESEND_API_KEY")
        or ""
    ).strip()


def _resolve_smtp_password(config: dict) -> str:
    sender_cfg = _sender_cfg(config)
    return (
        sender_cfg.get("app_password")
        or os.environ.get("SMTP_APP_PASSWORD")
        or ""
    ).strip()

def validate_sender_config(config: dict):
    sender_cfg = _sender_cfg(config)
    provider = _provider(config)
    missing = []

    if provider == "platform_managed":
        if not _reply_to_name(config):
            missing.append("sender.name")
        if not _reply_to_email(config):
            missing.append("sender.reply_to_email")
        if not _platform_from_email(config):
            missing.append("PLATFORM_FROM_EMAIL")
        if not _resend_api_key(config):
            missing.append("RESEND_API_KEY")
    elif provider == "smtp":
        if not sender_cfg.get("email"):
            missing.append("sender.email")
        if not sender_cfg.get("name"):
            missing.append("sender.name")
        if not _resolve_smtp_password(config):
            missing.append("SMTP_APP_PASSWORD ou sender.app_password")
    else:
        missing.append(f"provider inválido: {provider}")

    if missing:
        raise ValueError(f"Configuração de envio incompleta: {', '.join(missing)}")


def remaining_daily_quota(config: dict) -> int:
    crm.init_db()
    sent_today = crm.count_sent_today()
    max_daily = int(config["limits"].get("max_emails_per_day", 0) or 0)
    if max_daily <= 0:
        return 0
    return max(max_daily - sent_today, 0)


import html
from typing import Optional


def _safe_test_email() -> Optional[str]:
    return (os.environ.get("SAFE_TEST_EMAIL") or "").strip() or None


def _get_signature_html(config: dict) -> str:
    sig = config.get("signature", {})
    if isinstance(sig, dict) and sig.get("is_enabled", True):
        return (sig.get("html") or "").strip()
    return (config.get("company", {}).get("signature_html") or "").strip()


def _format_email_html_and_text(lead: dict, config: dict, safe_to: Optional[str] = None) -> tuple[str, str]:
    body_text = lead.get("email_corpo", "")
    sig_html = _get_signature_html(config)

    # 1. Plain text version
    full_text = body_text
    if sig_html:
        co = config.get("company", {})
        contact_name = co.get("contact_name") or "Operador"
        co_name = co.get("name") or "BigBoss OS"
        co_web = co.get("website") or ""
        co_phone = co.get("phone") or ""
        co_email = co.get("email") or ""
        
        parts = [contact_name]
        if co_name:
            parts.append(co_name)
        if co_web:
            parts.append(co_web)
        if co_phone:
            parts.append(f"WhatsApp: {co_phone}")
        if co_email:
            parts.append(co_email)
            
        plain_sig = "\n".join(parts)
        full_text = f"{full_text}\n\n---\n{plain_sig}"
    if safe_to:
        full_text = f"{full_text}\n\n---\n🛡️ [Modo de Teste Seguro Ativo]\nDestinatário original do lead: {lead.get('email')}\nEmpresa: {lead.get('nome_empresa')}"

    # Se já foi fornecido HTML customizado completo, usa direto
    if lead.get("email_html"):
        return full_text, lead["email_html"]

    # 2. HTML version
    escaped_body = html.escape(body_text).replace("\n", "<br>")
    safe_banner_html = ""
    if safe_to:
        safe_banner_html = f"""
        <div style="margin-top:20px;padding:12px 14px;background:#fefce8;border:1px solid #fef08a;border-radius:8px;font-size:12px;color:#854d0e;">
          🛡️ <strong>[Modo de Teste Seguro Ativo]</strong><br>
          Destinatário original do lead: {html.escape(str(lead.get('email')))}<br>
          Empresa: {html.escape(str(lead.get('nome_empresa')))}
        </div>
        """

    sig_block = ""
    if sig_html:
        sig_block = f"""
        <div style="margin-top:24px;padding-top:16px;border-top:1px solid #e2e8f0;">
          {sig_html}
        </div>
        """

    full_html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; font-size: 15px; line-height: 1.6; color: #1e293b; margin: 0; padding: 12px 0;">
      <div style="max-width: 620px;">
        <div>{escaped_body}</div>
        {sig_block}
        {safe_banner_html}
      </div>
    </body>
    </html>
    """.strip()

    return full_text, full_html


def _send_via_resend(lead: dict, config: dict) -> dict:
    sender_cfg = _sender_cfg(config)
    from_email = _platform_from_email(config)
    from_name = _platform_from_name(config)
    reply_to_email = _reply_to_email(config)
    reply_to_name = _reply_to_name(config)

    safe_to = _safe_test_email()
    target_email = safe_to if safe_to else lead["email"]
    subject = lead["email_assunto"]
    if safe_to:
        subject = f"[TESTE -> {lead.get('nome_empresa', 'Lead')}] {subject}"

    full_text, full_html = _format_email_html_and_text(lead, config, safe_to)

    payload = {
        "from": f"{from_name} <{from_email}>",
        "to": [target_email],
        "subject": subject,
        "text": full_text,
        "html": full_html,
        "reply_to": [f"{reply_to_name} <{reply_to_email}>"] if reply_to_email else [],
        "tags": [
            {"name": "company", "value": _slug_tag(lead.get("nome_empresa") or "unknown")},
            {"name": "provider", "value": _slug_tag(sender_cfg.get("provider", "platform_managed"))},
        ],
    }

    request = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_resend_api_key(config)}",
            "Content-Type": "application/json",
            "User-Agent": "outbound-os/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
        return {
            "status": "sent",
            "timestamp": datetime.now().isoformat(),
            "error": None,
            "provider": "resend",
            "provider_message_id": body.get("id"),
        }
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", "ignore")
        return {
            "status": "error",
            "timestamp": datetime.now().isoformat(),
            "error": f"Resend HTTP {exc.code}: {details}",
            "provider": "resend",
        }
    except Exception as exc:
        return {
            "status": "error",
            "timestamp": datetime.now().isoformat(),
            "error": str(exc),
            "provider": "resend",
        }


def _send_via_smtp(lead: dict, config: dict) -> dict:
    sender_cfg = _sender_cfg(config)
    co = config.get("company", {})
    
    smtp_host = sender_cfg.get("smtp_host") or os.environ.get("SMTP_HOST") or "smtp.domain.com"
    smtp_port = int(sender_cfg.get("smtp_port") or os.environ.get("SMTP_PORT") or 465)
    smtp_user = sender_cfg.get("email") or sender_cfg.get("smtp_user") or os.environ.get("SMTP_USER") or "user@domain.com"
    smtp_password = _resolve_smtp_password(config) or os.environ.get("SMTP_APP_PASSWORD") or os.environ.get("IMAP_PASSWORD") or ""

    from_name = sender_cfg.get("name") or co.get("contact_name") or "Operador"
    from_email = sender_cfg.get("from_email") or sender_cfg.get("email") or co.get("email") or "info@domain.com"
    reply_to = sender_cfg.get("reply_to_email") or co.get("email") or "info@domain.com"

    safe_to = _safe_test_email()
    target_email = safe_to if safe_to else lead["email"]
    subject = lead["email_assunto"]
    if safe_to:
        subject = f"[TESTE -> {lead.get('nome_empresa', 'Lead')}] {subject}"

    full_text, full_html = _format_email_html_and_text(lead, config, safe_to)

    from email.utils import formataddr
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, from_email))
    msg["To"] = target_email
    if reply_to:
        msg["Reply-To"] = formataddr((from_name, reply_to))

    msg.attach(MIMEText(full_text, "plain", "utf-8"))
    msg.attach(MIMEText(full_html, "html", "utf-8"))

    try:
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=20)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=20)
            server.starttls()

        if smtp_password:
            server.login(smtp_user, smtp_password)

        server.sendmail(smtp_user, target_email, msg.as_string())
        server.quit()

        return {"status": "sent", "timestamp": datetime.now().isoformat(), "error": None, "provider": "smtp"}
    except Exception as exc:
        return {"status": "error", "timestamp": datetime.now().isoformat(), "error": str(exc), "provider": "smtp"}


def send_email(lead: dict, config: dict) -> dict:
    from agents.validator import validate_email
    
    target_email = _safe_test_email() or lead.get("email") or ""
    val = validate_email(target_email)
    if not val["is_valid"]:
        return {
            "status": "blocked",
            "timestamp": datetime.now().isoformat(),
            "error": f"Anti-bounce: {val['reason']}",
            "provider": "validator",
        }

    provider = _provider(config)
    if provider == "platform_managed":
        return _send_via_resend(lead, config)
    return _send_via_smtp(lead, config)


def run_send_batch(leads: list[dict], config: dict, logs_dir: str, mode: str = None) -> list[dict]:
    mode = mode or config.get("mode", "manual")
    delay = config["limits"]["delay_between_sends_seconds"]
    results = []

    validate_sender_config(config)

    sendable = [lead for lead in leads if lead.get("email_status") == "ready" and lead.get("email")]

    if not sendable:
        print("Nenhum lead pronto para envio.")
        return leads

    quota = remaining_daily_quota(config)
    if quota <= 0:
        print("Limite diário de envio atingido. Nenhum email será enviado hoje.")
        return leads
    if quota < len(sendable):
        print(f"Limite diário permite enviar {quota} de {len(sendable)} emails prontos.")

    os.makedirs(logs_dir, exist_ok=True)

    print(f"\n{len(sendable)} emails prontos para envio. Modo: {mode} | Provider: {_provider(config)}")

    if mode == "assisted":
        confirm = input(f"Enviar todos os {len(sendable)} emails? [s/N] ").strip().lower()
        if confirm != "s":
            print("Envio cancelado.")
            return leads

    sent_count = 0
    for lead in leads:
        if lead.get("email_status") != "ready":
            results.append(lead)
            continue

        if sent_count >= quota:
            lead["email_status"] = "queued_daily_limit"
            results.append(lead)
            continue

        if mode == "manual":
            print(f"\n--- {lead.get('nome_empresa')} <{lead.get('email')}> ---")
            print(f"Assunto: {lead['email_assunto']}")
            print(f"---\n{lead['email_corpo']}\n---")
            confirm = input("Enviar? [s/N/q(quit)] ").strip().lower()
            if confirm == "q":
                results.append(lead)
                results.extend([item for item in leads if item not in results])
                break
            if confirm != "s":
                lead["email_status"] = "skipped_manual"
                results.append(lead)
                continue

        result = send_email(lead, config)
        lead["send_provider"] = result.get("provider")
        lead["send_provider_message_id"] = result.get("provider_message_id")
        lead["send_status"] = result["status"]
        lead["send_timestamp"] = result["timestamp"]
        lead["send_error"] = result["error"]
        lead["email_status"] = "sent" if result["status"] == "sent" else "send_error"

        if result["status"] == "sent":
            sent_count += 1
            print(f"  [ok] {lead.get('nome_empresa')} -> {lead.get('email')}")
        else:
            print(f"  [erro] {lead.get('nome_empresa')}: {result['error']}")

        results.append(lead)

        if sent_count < len(sendable):
            time.sleep(delay)

    log_path = f"{logs_dir}/send_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(log_path, "w", encoding="utf-8") as file:
        json.dump(results, file, ensure_ascii=False, indent=2)

    return results

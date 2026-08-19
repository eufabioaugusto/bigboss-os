"""
Gerenciador da Fila Diária de WhatsApp (Combo Multi-Canal) & Alerta Executivo por E-mail.
Garante que todo lead que recebeu e-mail hoje seja abordado no WhatsApp no mesmo dia com 1 clique.
"""
import re
import html
import urllib.parse
from datetime import datetime
from typing import List, Dict, Any, Optional

import crm
from agents.ai import ask_json
from agents.sender import send_email
from settings_store import get_runtime_config, get_company


def _clean_phone(phone_str: str) -> str:
    digits = re.sub(r"\D", "", phone_str or "")
    if not digits:
        return ""
    if len(digits) in (10, 11) and not digits.startswith("55"):
        return "55" + digits
    return digits


def get_daily_whatsapp_queue() -> List[Dict[str, Any]]:
    """Busca contatos contactados recentemente por e-mail com telefone disponível para abordagem WhatsApp."""
    crm.init_db()
    with crm.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.*,
                   (SELECT COUNT(*) FROM crm_events e 
                    WHERE e.contact_id = c.id AND e.event_type = 'whatsapp_sent') as whats_count
            FROM crm_contacts c
            WHERE (c.telefone IS NOT NULL AND c.telefone != '')
              AND (c.status IN ('sent_1x', 'replied', 'ready', 'followup_1', 'followup_2', 'meeting'))
            ORDER BY c.updated_at DESC
            LIMIT 50
            """
        ).fetchall()

    queue = []
    for r in rows:
        c = dict(r)
        phone_clean = _clean_phone(c.get("telefone") or "")
        if not phone_clean:
            continue

        # Gera script personalizado se ainda não tiver
        company_name = c.get("nome_empresa") or "Empresa"
        segmento = c.get("segmento") or "Geral"
        cidade = c.get("cidade") or ""

        script = (
            f"Olá! Aqui é o Fabio, da UltraWeb. Enviei um e-mail hoje para a {company_name} "
            f"com uma análise rápida de 2 min sobre a presença digital e oportunidades no Google. "
            f"Conseguiu dar uma olhada por lá?"
        )

        encoded_text = urllib.parse.quote(script)
        wa_link = f"https://wa.me/{phone_clean}?text={encoded_text}"

        queue.append({
            "id": c["id"],
            "company_name": company_name,
            "email": c.get("primary_email"),
            "phone": c.get("telefone"),
            "phone_clean": phone_clean,
            "city": cidade,
            "status": c.get("status"),
            "last_sent_at": c.get("last_sent_at"),
            "whats_sent": (c.get("whats_count") or 0) > 0,
            "script": script,
            "wa_link": wa_link,
        })

    return queue


def mark_whatsapp_sent(contact_id: int) -> Dict[str, Any]:
    """Registra envio de WhatsApp no histórico do lead."""
    crm.init_db()
    with crm.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO crm_events (contact_id, event_type, from_status, to_status, note, created_at)
            VALUES (?, 'whatsapp_sent', NULL, NULL, 'Abordagem via WhatsApp realizada com script de IA', datetime('now'))
            """,
            (contact_id,)
        )
    crm.add_note(contact_id, "📲 Abordagem Multi-canal via WhatsApp enviada.", author="user")
    return {"status": "ok", "contact_id": contact_id}


def send_daily_digest_alert(to_email: str = "fabio@ultraweb.com.br") -> Dict[str, Any]:
    """Envia o Relatório Executivo Diário de Prospecção com riqueza total de dados para o e-mail do usuário."""
    config = get_runtime_config()
    stats = crm.get_stats()
    queue = get_daily_whatsapp_queue()
    pending_whats = [q for q in queue if not q.get("whats_sent")]

    # Busca todos os contatos do CRM com detalhes ricos
    crm.init_db()
    with crm.get_conn() as conn:
        contact_rows = conn.execute(
            """
            SELECT * FROM crm_contacts
            ORDER BY updated_at DESC
            LIMIT 30
            """
        ).fetchall()

    today_str = datetime.now().strftime("%d/%m/%Y")
    subject = f"📊 Relatório Executivo Outbound ({today_str}) · {len(contact_rows)} Leads Ativos · {len(pending_whats)} WhatsApps"

    # Monta Tabela de Leads Ricos
    leads_rows_html = ""
    for r in contact_rows:
        c = dict(r)
        status_label = {
            "sent_1x": "📤 Enviado (1x)",
            "replied": "🔥 Respondeu",
            "meeting": "🤝 Reunião Agendada",
            "ready": "⏳ Pronto p/ Envio",
            "followup_1": "🔁 Follow-up 1",
            "followup_2": "🔁 Follow-up 2",
            "blocked": "🚫 Bloqueado",
        }.get(c.get("status"), c.get("status") or "Novo")

        score = c.get("score") if c.get("score") is not None else 7
        score_badge = f"<span style='padding:2px 8px;border-radius:12px;font-size:11px;font-weight:700;background:{'#fef2f2;color:#b91c1c;' if score>=8 else '#fefce8;color:#a16207;'}'>⭐ {score}/10</span>"
        
        sinais = html.escape(str(c.get("sinais") or "Presença digital analisada"))
        cidade = html.escape(str(c.get("cidade") or "Brasil"))
        segmento = html.escape(str(c.get("segmento") or "Geral"))
        empresa = html.escape(str(c.get("nome_empresa") or "Empresa"))
        email = html.escape(str(c.get("primary_email") or "-"))
        tel = html.escape(str(c.get("telefone") or "-"))

        leads_rows_html += f"""
        <tr style="border-bottom: 1px solid #e2e8f0; font-size: 13px;">
          <td style="padding: 10px 8px;">
            <strong style="color: #0f172a;">{empresa}</strong><br>
            <span style="font-size: 11.5px; color: #64748b;">📍 {cidade} · {segmento}</span>
          </td>
          <td style="padding: 10px 8px; color: #334155;">
            {email}<br>
            <span style="font-size: 11.5px; color: #16a34a; font-weight: 500;">📞 {tel}</span>
          </td>
          <td style="padding: 10px 8px; text-align: center;">
            {score_badge}
          </td>
          <td style="padding: 10px 8px;">
            <span style="font-size: 11px; background: #f1f5f9; padding: 2px 6px; border-radius: 4px; color: #475569; display: inline-block;">{sinais}</span>
          </td>
          <td style="padding: 10px 8px; text-align: right;">
            <span style="font-size: 11.5px; font-weight: 600; color: #1e293b;">{status_label}</span>
          </td>
        </tr>
        """

    # Monta Fila de WhatsApp com botões clicáveis
    whats_cards_html = ""
    for item in pending_whats[:25]:
        whats_cards_html += f"""
        <div style="margin-bottom: 14px; padding: 14px 16px; background: #ffffff; border: 1.5px solid #bbf7d0; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.04);">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
            <div>
              <strong style="color: #0f172a; font-size: 14.5px;">🏢 {html.escape(item['company_name'])}</strong>
              <span style="color: #64748b; font-size: 12px; margin-left: 6px;">📍 {html.escape(item['city'] or 'Brasil')}</span>
            </div>
            <span style="font-size: 11.5px; color: #16a34a; font-weight: 600;">📞 {html.escape(item['phone'])}</span>
          </div>
          <div style="background: #f8fafc; border-left: 3px solid #16a34a; padding: 10px 12px; border-radius: 6px; font-size: 12.5px; line-height: 1.5; color: #334155; margin-bottom: 12px; font-style: italic;">
            "{html.escape(item['script'])}"
          </div>
          <a href="{item['wa_link']}" target="_blank" style="display: inline-block; padding: 8px 18px; background: #16a34a; color: #ffffff; text-decoration: none; border-radius: 6px; font-weight: 700; font-size: 13px;">
            🟢 Disparar no WhatsApp Web
          </a>
        </div>
        """

    if not pending_whats:
        whats_cards_html = "<div style='padding: 16px; background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 8px; color: #166534; font-size: 13px;'>✅ Nenhum WhatsApp pendente na fila de hoje. Todos os leads contactados já foram abordados!</div>"

    body_html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #1e293b; background: #f8fafc; margin: 0; padding: 24px 12px; line-height: 1.5;">
      <div style="max-width: 680px; margin: 0 auto; background: #ffffff; border-radius: 12px; border: 1px solid #e2e8f0; padding: 28px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05);">
        
        <!-- Header -->
        <div style="padding-bottom: 18px; border-bottom: 2px solid #2563eb; margin-bottom: 24px; display: flex; justify-content: space-between; align-items: center;">
          <div>
            <h2 style="margin: 0; color: #0f172a; font-size: 20px;">📊 Relatório Executivo de Outbound</h2>
            <p style="margin: 4px 0 0 0; color: #64748b; font-size: 13px;">UltraWeb · Resumo Estratégico & Fila de Conversão ({today_str})</p>
          </div>
          <div style="text-align: right;">
            <span style="font-size: 12px; font-weight: 700; color: #16a34a; background: #dcfce7; padding: 4px 10px; border-radius: 20px;">● Operação Ativa</span>
          </div>
        </div>

        <!-- KPI Grid -->
        <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 28px;">
          <div style="background: #f8fafc; border: 1px solid #e2e8f0; padding: 12px 10px; border-radius: 8px; text-align: center;">
            <div style="font-size: 20px; font-weight: 800; color: #0f172a;">{stats.get('total_contacts', len(contact_rows))}</div>
            <div style="font-size: 11px; color: #64748b; margin-top: 2px;">Total de Leads</div>
          </div>
          <div style="background: #eff6ff; border: 1px solid #bfdbfe; padding: 12px 10px; border-radius: 8px; text-align: center;">
            <div style="font-size: 20px; font-weight: 800; color: #1d4ed8;">{stats.get('total_enviados', 0)}</div>
            <div style="font-size: 11px; color: #2563eb; margin-top: 2px;">E-mails Enviados</div>
          </div>
          <div style="background: #f0fdf4; border: 1px solid #bbf7d0; padding: 12px 10px; border-radius: 8px; text-align: center;">
            <div style="font-size: 20px; font-weight: 800; color: #15803d;">{stats.get('replies_unread', 0)}</div>
            <div style="font-size: 11px; color: #16a34a; margin-top: 2px;">Respostas / Reuniões</div>
          </div>
          <div style="background: #fefce8; border: 1px solid #fef08a; padding: 12px 10px; border-radius: 8px; text-align: center;">
            <div style="font-size: 20px; font-weight: 800; color: #a16207;">{len(pending_whats)}</div>
            <div style="font-size: 11px; color: #ca8a04; margin-top: 2px;">WhatsApps Pendentes</div>
          </div>
        </div>

        <!-- Section 1: WhatsApp Action Queue -->
        <div style="margin-bottom: 30px;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
            <h3 style="margin: 0; color: #0f172a; font-size: 16px; display: flex; align-items: center; gap: 6px;">
              📲 Fila de Ação WhatsApp (Combo Multi-Canal):
            </h3>
            <span style="font-size: 12px; color: #16a34a; font-weight: 600;">{len(pending_whats)} pendentes</span>
          </div>
          <p style="color: #64748b; font-size: 13px; margin: 0 0 16px 0;">
            Estes contatos receberam e-mail hoje. Clique em cada botão abaixo no celular ou computador para abrir a conversa com o script já preenchido:
          </p>
          {whats_cards_html}
        </div>

        <!-- Section 2: Full Detailed Leads Table -->
        <div style="margin-bottom: 24px;">
          <h3 style="margin: 0 0 12px 0; color: #0f172a; font-size: 16px;">
            🏢 Detalhamento dos Leads no Pipeline:
          </h3>
          <table style="width: 100%; border-collapse: collapse; text-align: left; background: #fff;">
            <thead>
              <tr style="background: #f8fafc; border-bottom: 2px solid #e2e8f0; font-size: 12px; color: #64748b; text-transform: uppercase;">
                <th style="padding: 8px;">Empresa / Local</th>
                <th style="padding: 8px;">Contatos</th>
                <th style="padding: 8px; text-align: center;">Score</th>
                <th style="padding: 8px;">Sinais & Gargalos</th>
                <th style="padding: 8px; text-align: right;">Status</th>
              </tr>
            </thead>
            <tbody>
              {leads_rows_html}
            </tbody>
          </table>
        </div>

        <!-- Footer -->
        <div style="margin-top: 32px; padding-top: 18px; border-top: 1px solid #e2e8f0; text-align: center; color: #94a3b8; font-size: 12px;">
          Vendor OS / UltraWeb Outbound Engine · <a href="http://127.0.0.1:7860" style="color: #2563eb; text-decoration: none; font-weight: 600;">Abrir Painel de Controle</a>
        </div>
      </div>
    </body>
    </html>
    """

    payload = {
        "nome_empresa": "Fabio da Ultraweb",
        "email": to_email,
        "email_assunto": subject,
        "email_corpo": f"Relatório executivo diário de outbound com {len(contact_rows)} leads e {len(pending_whats)} WhatsApps pendentes.",
        "email_html": body_html,
    }

    result = send_email(payload, config)
    return {
        "status": result.get("status"),
        "to": to_email,
        "total_leads": len(contact_rows),
        "pending_whats": len(pending_whats),
        "error": result.get("error"),
    }

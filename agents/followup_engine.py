"""
Motor de Cadência de Follow-ups Automáticos.
Identifica leads sem resposta após N dias e dispara reengajamento inteligente com IA.
"""
from datetime import datetime, timedelta
from typing import Dict, List, Any
import crm
from agents.ai import ask_json
from agents.sender import send_email
from settings_store import get_runtime_config

def load_config() -> dict:
    return get_runtime_config()


def find_eligible_followups(min_days: int = 3, max_attempts: int = 3) -> List[Dict[str, Any]]:
    """Busca contatos elegíveis para follow-up (sem resposta há mais de min_days dias)."""
    crm.init_db()
    cutoff = (datetime.now() - timedelta(days=min_days)).isoformat()
    
    with crm.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.*, 
                   (SELECT assunto FROM envios WHERE contact_id = c.id ORDER BY id DESC LIMIT 1) as last_subject,
                   (SELECT corpo FROM envios WHERE contact_id = c.id ORDER BY id DESC LIMIT 1) as last_body
            FROM crm_contacts c
            WHERE c.status IN ('sent_1x', 'ready')
              AND c.send_count >= 1
              AND c.send_count < ?
              AND c.do_not_contact = 0
              AND c.last_sent_at IS NOT NULL
              AND c.last_sent_at <= ?
              AND c.primary_email IS NOT NULL
              AND c.primary_email != ''
            ORDER BY c.last_sent_at ASC
            LIMIT 50
            """,
            (max_attempts, cutoff)
        ).fetchall()
        
    return [dict(r) for r in rows]


def generate_followup_copy(contact: Dict[str, Any], attempt_no: int) -> Dict[str, str]:
    """Gera mensagem de follow-up adaptada ao contexto e número da tentativa."""
    last_subject = contact.get("last_subject") or contact.get("email_draft_assunto") or f"Parceria {contact.get('nome_empresa')}"
    if not last_subject.lower().startswith("re:"):
        subject = f"Re: {last_subject}"
    else:
        subject = last_subject

    prompt = f"""Gere um follow-up número {attempt_no} para este lead que não respondeu ao e-mail anterior.

Lead:
- Empresa: {contact.get('nome_empresa')}
- Segmento: {contact.get('segmento')}
- Cidade: {contact.get('cidade')}
- E-mail anterior enviado com assunto: '{last_subject}'

Diretrizes para a tentativa {attempt_no}:
- Tentativa 2: Muito breve (3 a 5 linhas). Pergunte de forma leve e consultiva se conseguiram ver o ponto levantado antes.
- Tentativa 3: Última tentativa cordial (break-up email suave). Pergunte se faz sentido retomar no futuro ou se prefere que não façamos novo contato.
- Não soe insistente nem desesperado.
- Máximo 70 palavras.

Retorne APENAS um JSON válido com as chaves: "assunto" e "corpo"."""

    system = "Você é especialista em copywriting B2B e cadências de e-mail de alta conversão. Retorne apenas JSON."

    try:
        res = ask_json(prompt, system)
        return {
            "assunto": res.get("assunto") or subject,
            "corpo": res.get("corpo") or "Olá,\n\nConseguiram avaliar o e-mail que enviei anteriormente sobre a presença digital de vocês?\n\nSeguimos à disposição!",
        }
    except Exception:
        return {
            "assunto": subject,
            "corpo": f"Olá equipe {contact.get('nome_empresa')},\n\nPassando apenas para checar se conseguiram ver o e-mail anterior que enviei.\n\nFaria sentido conversarmos nesta semana?",
        }


def process_single_followup(contact: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Gera e despacha o follow-up para um contato."""
    contact_id = contact["id"]
    attempt_no = int(contact.get("send_count") or 1) + 1
    
    copy = generate_followup_copy(contact, attempt_no)
    
    lead_payload = {
        "nome_empresa": contact["nome_empresa"],
        "email": contact["primary_email"],
        "email_assunto": copy["assunto"],
        "email_corpo": copy["corpo"],
        "crm_contact_id": contact_id,
    }
    
    result = send_email(lead_payload, config)
    status = result.get("status", "error")
    
    crm.register_send(
        lead_payload,
        "automated_followup",
        None,
        status,
        result.get("error"),
        provider=result.get("provider"),
        provider_message_id=result.get("provider_message_id"),
    )
    
    if status == "sent":
        crm.add_note(
            contact_id, 
            f"Follow-up #{attempt_no} disparado automaticamente: '{copy['assunto']}'",
            author="robot"
        )
    
    return {
        "contact_id": contact_id,
        "company": contact["nome_empresa"],
        "email": contact["primary_email"],
        "attempt_no": attempt_no,
        "status": status,
        "error": result.get("error"),
    }


def run_followup_cycle(config: Dict[str, Any], min_days: int = 3, max_attempts: int = 3, limit: int = 20) -> Dict[str, Any]:
    """Executa o ciclo de follow-up automático."""
    eligible = find_eligible_followups(min_days=min_days, max_attempts=max_attempts)
    eligible = eligible[:limit]
    
    summary = {"total_eligible": len(eligible), "sent": 0, "errors": 0, "blocked": 0, "items": []}
    
    for c in eligible:
        res = process_single_followup(c, config)
        summary["items"].append(res)
        if res["status"] == "sent":
            summary["sent"] += 1
        elif res["status"] == "blocked":
            summary["blocked"] += 1
        else:
            summary["errors"] += 1
            
    return summary

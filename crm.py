"""
CRM local — SQLite.
Mantém identidade única por contato/empresa, bloqueia reenvio acidental e guarda status atual do relacionamento.
"""
import json
import re
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

DB_PATH = Path(__file__).parent / "data" / "crm.db"

LOCKED_STATUSES = {"recall", "replied", "meeting", "blocked", "bounced"}
EDITABLE_STATUSES = {"new", "ready", "sent_1x", "recall", "replied", "meeting", "blocked", "bounced", "send_error", "qualified"}


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now().isoformat()


def _table_columns(conn, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _ensure_column(conn, table: str, column: str, ddl: str):
    if column not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db():
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                contact_id INTEGER,
                identity_key TEXT,
                crm_status TEXT,
                nome_empresa TEXT,
                segmento TEXT,
                cidade TEXT,
                email TEXT,
                website TEXT,
                instagram TEXT,
                score INTEGER,
                prioridade TEXT,
                angulo TEXT,
                sinais TEXT,
                criado_em TEXT
            );

            CREATE TABLE IF NOT EXISTS envios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER,
                contact_id INTEGER,
                run_id TEXT,
                nome_empresa TEXT,
                email_destinatario TEXT,
                assunto TEXT,
                corpo TEXT,
                status TEXT,
                erro TEXT,
                attempt_no INTEGER DEFAULT 1,
                status_after TEXT,
                provider TEXT,
                provider_message_id TEXT,
                enviado_em TEXT,
                FOREIGN KEY (lead_id) REFERENCES leads(id)
            );

            CREATE TABLE IF NOT EXISTS crm_contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identity_key TEXT NOT NULL UNIQUE,
                identity_type TEXT NOT NULL,
                normalized_email TEXT,
                website_host TEXT,
                normalized_name TEXT,
                normalized_city TEXT,
                nome_empresa TEXT,
                segmento TEXT,
                cidade TEXT,
                primary_email TEXT,
                telefone TEXT,
                website TEXT,
                instagram TEXT,
                linkedin TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                send_count INTEGER NOT NULL DEFAULT 0,
                first_sent_at TEXT,
                last_sent_at TEXT,
                recall_at TEXT,
                last_reply_at TEXT,
                last_error_at TEXT,
                blocked_reason TEXT,
                do_not_contact INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS crm_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_id INTEGER NOT NULL,
                run_id TEXT,
                event_type TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT,
                note TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (contact_id) REFERENCES crm_contacts(id)
            );

            CREATE TABLE IF NOT EXISTS campaigns (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                product_name TEXT NOT NULL,
                product_description TEXT NOT NULL,
                price_original REAL,
                price_promo REAL,
                scarcity_limit INTEGER,
                tone_of_voice TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

        _ensure_column(conn, "leads", "contact_id", "INTEGER")
        _ensure_column(conn, "leads", "identity_key", "TEXT")
        _ensure_column(conn, "leads", "crm_status", "TEXT")
        _ensure_column(conn, "leads", "source", "TEXT")
        _ensure_column(conn, "leads", "campaign_id", "TEXT")
        _ensure_column(conn, "crm_contacts", "campaign_id", "TEXT")
        _ensure_column(conn, "envios", "contact_id", "INTEGER")
        _ensure_column(conn, "envios", "attempt_no", "INTEGER DEFAULT 1")
        _ensure_column(conn, "envios", "status_after", "TEXT")
        _ensure_column(conn, "envios", "provider", "TEXT")
        _ensure_column(conn, "envios", "provider_message_id", "TEXT")

        # Scoring + rascunho de email + DM por contato
        _ensure_column(conn, "crm_contacts", "source", "TEXT")
        _ensure_column(conn, "crm_contacts", "score", "INTEGER")
        _ensure_column(conn, "crm_contacts", "angulo", "TEXT")
        _ensure_column(conn, "crm_contacts", "sinais", "TEXT")
        _ensure_column(conn, "crm_contacts", "prioridade", "TEXT")
        _ensure_column(conn, "crm_contacts", "email_draft_assunto", "TEXT")
        _ensure_column(conn, "crm_contacts", "email_draft_corpo", "TEXT")
        _ensure_column(conn, "crm_contacts", "email_draft_template_id", "TEXT")
        _ensure_column(conn, "crm_contacts", "dm_instagram", "TEXT")

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS crm_notes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_id INTEGER NOT NULL REFERENCES crm_contacts(id),
                author     TEXT    NOT NULL DEFAULT 'user',
                note       TEXT    NOT NULL,
                created_at TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS crm_followups (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_id     INTEGER NOT NULL REFERENCES crm_contacts(id),
                attempt_no     INTEGER NOT NULL DEFAULT 1,
                email_assunto  TEXT,
                email_corpo    TEXT,
                scheduled_for  TEXT,
                sent_at        TEXT,
                status         TEXT NOT NULL DEFAULT 'pending'
            );

            CREATE INDEX IF NOT EXISTS idx_notes_contact ON crm_notes(contact_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_followups_due ON crm_followups(status, scheduled_for);

            CREATE TABLE IF NOT EXISTS crm_email_replies (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_id     INTEGER REFERENCES crm_contacts(id),
                from_email     TEXT,
                from_name      TEXT,
                subject        TEXT,
                body_preview   TEXT,
                received_at    TEXT NOT NULL,
                status         TEXT NOT NULL DEFAULT 'unread',
                provider       TEXT,
                provider_msg_id TEXT,
                created_at     TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_replies_status_received ON crm_email_replies(status, received_at DESC);
            CREATE INDEX IF NOT EXISTS idx_replies_contact ON crm_email_replies(contact_id, received_at DESC);
            """
        )

        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);
            CREATE INDEX IF NOT EXISTS idx_leads_website ON leads(website);
            CREATE INDEX IF NOT EXISTS idx_leads_empresa_cidade ON leads(nome_empresa, cidade);
            CREATE INDEX IF NOT EXISTS idx_envios_status_data ON envios(status, enviado_em);
            CREATE INDEX IF NOT EXISTS idx_envios_contact_id ON envios(contact_id);
            CREATE INDEX IF NOT EXISTS idx_contacts_email ON crm_contacts(normalized_email);
            CREATE INDEX IF NOT EXISTS idx_contacts_website ON crm_contacts(website_host);
            CREATE INDEX IF NOT EXISTS idx_contacts_name_city ON crm_contacts(normalized_name, normalized_city);
            CREATE INDEX IF NOT EXISTS idx_contacts_status ON crm_contacts(status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_events_contact ON crm_events(contact_id, created_at DESC);
            """
        )


def _normalize_text(value: Optional[str]) -> str:
    raw = unicodedata.normalize("NFKD", (value or "").strip().lower())
    return raw.encode("ascii", "ignore").decode("ascii")


def _normalize_email(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def _normalize_website(value: Optional[str]) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    host = parsed.netloc or parsed.path
    host = host.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def build_identity(lead: dict) -> tuple[str, ...]:
    email = _normalize_email(lead.get("email") or lead.get("email_verificado"))
    if email:
        return ("email", email)

    website = _normalize_website(lead.get("website_verificado") or lead.get("website"))
    if website:
        return ("website", website)

    nome = _normalize_text(lead.get("nome_empresa"))
    cidade = _normalize_text(lead.get("cidade"))
    return ("company", nome, cidade)


def _identity_key(identity: tuple[str, ...]) -> str:
    head, *tail = identity
    return f"{head}:{'|'.join(tail)}"


def _contact_payload(lead: dict) -> dict:
    return {
        "source": (lead.get("source") or "google").strip(),
        "normalized_email": _normalize_email(lead.get("email") or lead.get("email_verificado")),
        "website_host": _normalize_website(lead.get("website_verificado") or lead.get("website")),
        "normalized_name": _normalize_text(lead.get("nome_empresa")),
        "normalized_city": _normalize_text(lead.get("cidade")),
        "nome_empresa": (lead.get("nome_empresa") or "").strip(),
        "segmento": (lead.get("segmento") or "").strip(),
        "cidade": (lead.get("cidade") or "").strip(),
        "primary_email": (lead.get("email") or lead.get("email_verificado") or "").strip(),
        "telefone": (lead.get("telefone") or "").strip(),
        "website": (lead.get("website_verificado") or lead.get("website") or "").strip(),
        "instagram": (lead.get("instagram_verificado") or lead.get("instagram") or lead.get("tiktok") or lead.get("tt_url") or "").strip(),
        "linkedin": (lead.get("linkedin") or "").strip(),
    }


def _row_to_contact(row) -> Optional[dict]:
    if not row:
        return None
    data = dict(row)
    data["do_not_contact"] = bool(data.get("do_not_contact"))
    data["available_channels"] = _available_channels(data)
    data["next_action"] = _next_action(data)
    return data


def _available_channels(contact: dict) -> list[str]:
    channels = []
    if contact.get("primary_email") and contact.get("email_draft_assunto"):
        channels.append("email")
    if contact.get("instagram"):
        channels.append("instagram_dm")
    if contact.get("telefone"):
        channels.append("phone")
    if contact.get("website"):
        channels.append("website")
    return channels


def _next_action(contact: dict) -> str:
    status = contact.get("status")
    if contact.get("do_not_contact") or status in {"replied", "blocked", "bounced"}:
        return "blocked"
    if status == "sent_1x":
        return "followup"
    if contact.get("primary_email") and contact.get("email_draft_assunto"):
        return "send_email"
    if contact.get("instagram"):
        return "send_instagram_dm"
    if contact.get("telefone"):
        return "call_or_whatsapp"
    return "find_contact"


def _next_action_sql(action: str) -> tuple[str, list]:
    if action == "send_email":
        return (
            """
            c.do_not_contact = 0
            AND c.status NOT IN ('replied','blocked','bounced','sent_1x')
            AND c.primary_email IS NOT NULL AND c.primary_email != ''
            AND c.email_draft_assunto IS NOT NULL AND c.email_draft_assunto != ''
            """,
            [],
        )
    if action == "send_instagram_dm":
        return (
            """
            c.do_not_contact = 0
            AND c.status NOT IN ('replied','blocked','bounced','sent_1x')
            AND (c.primary_email IS NULL OR c.primary_email = '' OR c.email_draft_assunto IS NULL OR c.email_draft_assunto = '')
            AND c.instagram IS NOT NULL AND c.instagram != ''
            """,
            [],
        )
    if action == "call_or_whatsapp":
        return (
            """
            c.do_not_contact = 0
            AND c.status NOT IN ('replied','blocked','bounced','sent_1x')
            AND (c.primary_email IS NULL OR c.primary_email = '' OR c.email_draft_assunto IS NULL OR c.email_draft_assunto = '')
            AND (c.instagram IS NULL OR c.instagram = '')
            AND c.telefone IS NOT NULL AND c.telefone != ''
            """,
            [],
        )
    if action == "find_contact":
        return (
            """
            c.do_not_contact = 0
            AND c.status NOT IN ('replied','blocked','bounced','sent_1x')
            AND (c.primary_email IS NULL OR c.primary_email = '')
            AND (c.instagram IS NULL OR c.instagram = '')
            AND (c.telefone IS NULL OR c.telefone = '')
            """,
            [],
        )
    if action == "followup":
        return ("c.status = 'sent_1x'", [])
    if action == "blocked":
        return ("c.do_not_contact = 1 OR c.status IN ('replied','blocked','bounced')", [])
    return ("1 = 1", [])


def _find_contact_row(conn, lead: dict):
    payload = _contact_payload(lead)

    if payload["normalized_email"]:
        row = conn.execute(
            "SELECT * FROM crm_contacts WHERE normalized_email = ? LIMIT 1",
            (payload["normalized_email"],),
        ).fetchone()
        if row:
            return row

    if payload["website_host"]:
        row = conn.execute(
            "SELECT * FROM crm_contacts WHERE website_host = ? LIMIT 1",
            (payload["website_host"],),
        ).fetchone()
        if row:
            return row

    if payload["normalized_name"] and payload["normalized_city"]:
        row = conn.execute(
            """
            SELECT * FROM crm_contacts
            WHERE normalized_name = ? AND normalized_city = ?
            LIMIT 1
            """,
            (payload["normalized_name"], payload["normalized_city"]),
        ).fetchone()
        if row:
            return row

    if payload["normalized_name"]:
        row = conn.execute(
            "SELECT * FROM crm_contacts WHERE normalized_name = ? LIMIT 1",
            (payload["normalized_name"],),
        ).fetchone()
        if row:
            return row

    return None


def _contact_block_reason(contact: Optional[dict]) -> Optional[str]:
    if not contact:
        return None
    if contact.get("do_not_contact"):
        return contact.get("blocked_reason") or "Contato bloqueado no CRM."
    if contact.get("status") == "replied":
        return "Contato já respondeu. Novo envio bloqueado."
    if contact.get("status") == "bounced":
        return "Contato com bounce registrado. Novo envio bloqueado."
    if contact.get("status") == "blocked":
        return contact.get("blocked_reason") or "Contato bloqueado no CRM."
    if contact.get("status") == "recall":
        return "Contato marcado para recall. Novo envio automático bloqueado."
    if int(contact.get("send_count") or 0) >= 1:
        count = int(contact.get("send_count") or 0)
        return f"Empresa já recebeu {count} envio(s)."
    return None


def _next_contact_status(current: Optional[dict], lead: dict) -> str:
    if current:
        existing_status = current.get("status") or "new"
        if int(current.get("send_count") or 0) >= 1 or existing_status in LOCKED_STATUSES:
            return existing_status
        if lead.get("email_status") == "ready":
            return "ready"
        if existing_status in {"new", "ready", "send_error"}:
            return existing_status
        return existing_status
    return "ready" if lead.get("email_status") == "ready" else "new"


def _log_event(conn, contact_id: int, event_type: str, from_status: Optional[str], to_status: Optional[str], *, run_id: Optional[str] = None, note: Optional[str] = None, payload: Optional[dict] = None):
    conn.execute(
        """
        INSERT INTO crm_events (contact_id, run_id, event_type, from_status, to_status, note, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            contact_id,
            run_id,
            event_type,
            from_status,
            to_status,
            note,
            json.dumps(payload or {}, ensure_ascii=False),
            _now(),
        ),
    )


def get_contact(contact_id: int, conn=None) -> Optional[dict]:
    own = conn is None
    conn = conn or get_conn()
    try:
        row = conn.execute("SELECT * FROM crm_contacts WHERE id = ?", (contact_id,)).fetchone()
        return _row_to_contact(row)
    finally:
        if own:
            conn.close()


def upsert_contact(lead: dict, conn=None) -> dict:
    init_db()
    own = conn is None
    conn = conn or get_conn()
    payload = _contact_payload(lead)
    now = _now()

    try:
        row = _find_contact_row(conn, lead)
        if row:
            current = _row_to_contact(row)
            identity = build_identity(lead)
            identity_type = current["identity_type"]
            identity_key = current["identity_key"]

            if payload["normalized_email"] and not current.get("normalized_email"):
                identity_type = "email"
                identity_key = _identity_key(("email", payload["normalized_email"]))
            elif payload["website_host"] and identity_type == "company":
                identity_type = "website"
                identity_key = _identity_key(("website", payload["website_host"]))
            elif identity_type not in {"email", "website", "company"}:
                identity_type = identity[0]
                identity_key = _identity_key(identity)

            next_status = _next_contact_status(current, lead)
            conn.execute(
                """
                UPDATE crm_contacts
                SET identity_key = ?, identity_type = ?, normalized_email = ?, website_host = ?,
                    normalized_name = ?, normalized_city = ?, nome_empresa = ?, segmento = ?, cidade = ?,
                    primary_email = ?, telefone = ?, website = ?, instagram = ?, linkedin = ?,
                    source = ?, status = ?, campaign_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    identity_key,
                    identity_type,
                    payload["normalized_email"] or current.get("normalized_email"),
                    payload["website_host"] or current.get("website_host"),
                    payload["normalized_name"] or current.get("normalized_name"),
                    payload["normalized_city"] or current.get("normalized_city"),
                    payload["nome_empresa"] or current.get("nome_empresa"),
                    payload["segmento"] or current.get("segmento"),
                    payload["cidade"] or current.get("cidade"),
                    payload["primary_email"] or current.get("primary_email"),
                    payload["telefone"] or current.get("telefone"),
                    payload["website"] or current.get("website"),
                    payload["instagram"] or current.get("instagram"),
                    payload["linkedin"] or current.get("linkedin"),
                    payload["source"] or current.get("source"),
                    next_status,
                    lead.get("campaign_id") or current.get("campaign_id"),
                    now,
                    current["id"],
                ),
            )
            return get_contact(current["id"], conn=conn)

        identity = build_identity(lead)
        identity_key = _identity_key(identity)
        status = _next_contact_status(None, lead)
        cur = conn.execute(
            """
            INSERT INTO crm_contacts (
                identity_key, identity_type, normalized_email, website_host, normalized_name, normalized_city,
                nome_empresa, segmento, cidade, primary_email, telefone, website, instagram, linkedin,
                source, status, campaign_id, send_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                identity_key,
                identity[0],
                payload["normalized_email"] or None,
                payload["website_host"] or None,
                payload["normalized_name"] or None,
                payload["normalized_city"] or None,
                payload["nome_empresa"] or None,
                payload["segmento"] or None,
                payload["cidade"] or None,
                payload["primary_email"] or None,
                payload["telefone"] or None,
                payload["website"] or None,
                payload["instagram"] or None,
                payload["linkedin"] or None,
                payload["source"] or "google",
                status,
                lead.get("campaign_id"),
                now,
                now,
            ),
        )
        contact_id = cur.lastrowid
        _log_event(conn, contact_id, "discovered", None, status, payload={"identity_key": identity_key})
        return get_contact(contact_id, conn=conn)
    finally:
        if own:
            conn.commit()
            conn.close()


def dedupe_batch(leads: list[dict]) -> tuple[list[dict], int]:
    unique = []
    seen = set()
    skipped = 0

    for lead in leads:
        identity = build_identity(lead)
        if identity in seen:
            skipped += 1
            continue
        seen.add(identity)
        unique.append(lead)

    return unique, skipped


def get_contact_by_lead(lead: dict) -> Optional[dict]:
    init_db()
    with get_conn() as conn:
        return _row_to_contact(_find_contact_row(conn, lead))


def can_send_lead(lead: dict) -> tuple[bool, Optional[str], Optional[dict]]:
    init_db()
    with get_conn() as conn:
        contact = _row_to_contact(_find_contact_row(conn, lead))
    reason = _contact_block_reason(contact)
    return reason is None, reason, contact


def was_contacted(lead: dict) -> bool:
    allowed, _, contact = can_send_lead(lead)
    return bool(contact) and not allowed


def filter_new_leads(leads: list[dict]) -> tuple[list[dict], int]:
    fresh = []
    skipped = 0
    for lead in leads:
        if was_contacted(lead):
            skipped += 1
            continue
        fresh.append(lead)
    return fresh, skipped


def count_sent_today() -> int:
    today = datetime.now().date().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM envios WHERE status='sent' AND substr(enviado_em, 1, 10) = ?",
            (today,),
        ).fetchone()
    return row[0] if row else 0


def save_leads(leads: list[dict], run_id: str) -> dict[int, int]:
    """Salva snapshots dos leads no CRM. Retorna mapa {índice_da_tela: lead_snapshot_id}."""
    init_db()
    idx_to_id = {}
    with get_conn() as conn:
        for index, lead in enumerate(leads):
            contact = upsert_contact(lead, conn=conn)
            lead["crm_contact_id"] = contact["id"]
            lead["crm_status"] = contact["status"]
            lead["crm_send_count"] = contact["send_count"]
            lead["crm_identity_key"] = contact["identity_key"]

            cur = conn.execute(
                """
                INSERT INTO leads (
                    run_id, contact_id, identity_key, crm_status, nome_empresa, segmento, cidade, email, website,
                    instagram, source, score, prioridade, angulo, sinais, campaign_id, criado_em
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    contact["id"],
                    contact["identity_key"],
                    contact["status"],
                    lead.get("nome_empresa"),
                    lead.get("segmento"),
                    lead.get("cidade"),
                    lead.get("email"),
                    lead.get("website"),
                    lead.get("instagram"),
                    lead.get("source"),
                    lead.get("score", 0),
                    lead.get("prioridade"),
                    lead.get("angulo"),
                    json.dumps(lead.get("sinais", []), ensure_ascii=False),
                    lead.get("campaign_id"),
                    _now(),
                ),
            )
            idx_to_id[index] = cur.lastrowid
    return idx_to_id


def register_send(
    lead: dict,
    run_id: str,
    lead_id: Optional[int],
    status: str,
    erro: str = None,
    *,
    provider: Optional[str] = None,
    provider_message_id: Optional[str] = None,
) -> dict:
    init_db()
    with get_conn() as conn:
        contact = upsert_contact(lead, conn=conn)
        previous_status = contact["status"]
        attempt_no = conn.execute(
            "SELECT COUNT(*) FROM envios WHERE contact_id = ?",
            (contact["id"],),
        ).fetchone()[0] + 1

        next_status = previous_status
        updates = {
            "last_error_at": contact.get("last_error_at"),
            "last_sent_at": contact.get("last_sent_at"),
            "first_sent_at": contact.get("first_sent_at"),
            "send_count": int(contact.get("send_count") or 0),
            "blocked_reason": contact.get("blocked_reason"),
        }

        if status == "sent":
            updates["send_count"] += 1
            updates["first_sent_at"] = updates["first_sent_at"] or _now()
            updates["last_sent_at"] = _now()
            next_status = "sent_1x"
        elif status == "error":
            updates["last_error_at"] = _now()
            if previous_status not in LOCKED_STATUSES and updates["send_count"] == 0:
                next_status = "send_error"

        conn.execute(
            """
            UPDATE crm_contacts
            SET status = ?, send_count = ?, first_sent_at = ?, last_sent_at = ?, last_error_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                next_status,
                updates["send_count"],
                updates["first_sent_at"],
                updates["last_sent_at"],
                updates["last_error_at"],
                _now(),
                contact["id"],
            ),
        )

        conn.execute(
            """
            INSERT INTO envios (
                lead_id, contact_id, run_id, nome_empresa, email_destinatario, assunto, corpo, status, erro,
                attempt_no, status_after, provider, provider_message_id, enviado_em
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lead_id,
                contact["id"],
                run_id,
                lead.get("nome_empresa"),
                lead.get("email"),
                lead.get("email_assunto"),
                lead.get("email_corpo"),
                status,
                erro,
                attempt_no,
                next_status,
                provider,
                provider_message_id,
                _now(),
            ),
        )

        _log_event(
            conn,
            contact["id"],
            "send_attempt",
            previous_status,
            next_status,
            run_id=run_id,
            note=erro,
            payload={"status": status, "email": lead.get("email"), "attempt_no": attempt_no, "provider": provider},
        )

        updated = get_contact(contact["id"], conn=conn)
        lead["crm_contact_id"] = updated["id"]
        lead["crm_status"] = updated["status"]
        lead["crm_send_count"] = updated["send_count"]
        return updated


def get_contacts(limit: int = 200, status_filter: Optional[str] = None, run_id: Optional[str] = None) -> list[dict]:
    init_db()
    with get_conn() as conn:
        where_clauses = []
        params: list = []

        if status_filter == "queue":
            where_clauses.append("c.status IN ('ready','new') AND c.email_draft_assunto IS NOT NULL")
        elif status_filter and status_filter.startswith("action:"):
            action_sql, action_params = _next_action_sql(status_filter.split(":", 1)[1])
            where_clauses.append(f"({action_sql})")
            params.extend(action_params)
        elif status_filter == "sent":
            where_clauses.append("c.status = 'sent_1x'")
        elif status_filter == "followup":
            today = datetime.now().date().isoformat()
            where_clauses.append("c.recall_at IS NOT NULL AND c.recall_at <= ?")
            params.append(today)
        elif status_filter == "replied":
            where_clauses.append("c.status = 'replied'")
        elif status_filter and status_filter != "all":
            where_clauses.append("c.status = ?")
            params.append(status_filter)

        if run_id:
            where_clauses.append(
                "c.id IN (SELECT contact_id FROM leads WHERE run_id = ?)"
            )
            params.append(run_id)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT
                c.*,
                COALESCE(MAX(e.enviado_em), c.last_sent_at) AS last_touch_at,
                COUNT(e.id) AS attempts_count,
                (SELECT l.run_id FROM leads l WHERE l.contact_id = c.id ORDER BY l.id DESC LIMIT 1) AS run_id,
                COALESCE(
                    (SELECT t.name FROM scheduled_tasks t JOIN task_runs tr ON tr.task_id = t.id 
                     WHERE substr(replace(replace(replace(tr.started_at, '-', ''), 'T', '_'), ':', ''), 1, 15) = (SELECT l.run_id FROM leads l WHERE l.contact_id = c.id ORDER BY l.id DESC LIMIT 1)
                     LIMIT 1),
                    (SELECT t.name FROM scheduled_tasks t WHERE t.prompt = c.segmento OR c.segmento LIKE '%' || t.name || '%' LIMIT 1)
                ) AS routine_name
            FROM crm_contacts c
            LEFT JOIN envios e ON e.contact_id = c.id
            {where_sql}
            GROUP BY c.id
            ORDER BY COALESCE(c.last_sent_at, c.updated_at) DESC, c.updated_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_row_to_contact(row) for row in rows]


def update_contact_draft(
    contact_id: int,
    assunto: str,
    corpo: str,
    template_id: Optional[str] = None,
    score: Optional[int] = None,
    angulo: Optional[str] = None,
    sinais: Optional[str] = None,
    prioridade: Optional[str] = None,
    dm_instagram: Optional[str] = None,
) -> Optional[dict]:
    init_db()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE crm_contacts
            SET email_draft_assunto = ?,
                email_draft_corpo = ?,
                email_draft_template_id = ?,
                score = COALESCE(?, score),
                angulo = COALESCE(?, angulo),
                sinais = COALESCE(?, sinais),
                prioridade = COALESCE(?, prioridade),
                dm_instagram = COALESCE(?, dm_instagram),
                updated_at = ?
            WHERE id = ?
            """,
            (assunto, corpo, template_id, score, angulo, sinais, prioridade, dm_instagram, _now(), contact_id),
        )
        return get_contact(contact_id, conn=conn)


def add_note(contact_id: int, note: str, author: str = "user") -> dict:
    init_db()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO crm_notes (contact_id, author, note, created_at) VALUES (?, ?, ?, ?)",
            (contact_id, author, note.strip(), _now()),
        )
        row = conn.execute(
            "SELECT * FROM crm_notes WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)


def get_notes(contact_id: int) -> list[dict]:
    init_db()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM crm_notes WHERE contact_id = ? ORDER BY created_at ASC",
            (contact_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_contact_full(contact_id: int) -> Optional[dict]:
    init_db()
    with get_conn() as conn:
        contact = get_contact(contact_id, conn=conn)
        if not contact:
            return None

        envios_rows = conn.execute(
            """
            SELECT id, assunto, corpo, status, erro, attempt_no, provider, enviado_em
            FROM envios WHERE contact_id = ? ORDER BY enviado_em DESC LIMIT 20
            """,
            (contact_id,),
        ).fetchall()

        events_rows = conn.execute(
            """
            SELECT event_type, from_status, to_status, note, created_at
            FROM crm_events WHERE contact_id = ? ORDER BY created_at DESC LIMIT 30
            """,
            (contact_id,),
        ).fetchall()

        notes_rows = conn.execute(
            "SELECT id, author, note, created_at FROM crm_notes WHERE contact_id = ? ORDER BY created_at ASC",
            (contact_id,),
        ).fetchall()

        followups_rows = conn.execute(
            """
            SELECT id, attempt_no, email_assunto, scheduled_for, sent_at, status
            FROM crm_followups WHERE contact_id = ? ORDER BY attempt_no ASC
            """,
            (contact_id,),
        ).fetchall()

        replies_rows = conn.execute(
            """
            SELECT * FROM crm_email_replies WHERE contact_id = ? ORDER BY id DESC LIMIT 10
            """,
            (contact_id,),
        ).fetchall()

    contact["envios"] = [dict(r) for r in envios_rows]
    contact["events"] = [dict(r) for r in events_rows]
    contact["notes"] = [dict(r) for r in notes_rows]
    contact["followups"] = [dict(r) for r in followups_rows]
    contact["replies"] = [dict(r) for r in replies_rows]
    return contact


def add_followup(
    contact_id: int,
    attempt_no: int,
    assunto: str,
    corpo: str,
    scheduled_for: Optional[str] = None,
) -> dict:
    init_db()
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO crm_followups (contact_id, attempt_no, email_assunto, email_corpo, scheduled_for, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
            """,
            (contact_id, attempt_no, assunto, corpo, scheduled_for),
        )
        row = conn.execute(
            "SELECT * FROM crm_followups WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)


def get_pending_followups(due_before: Optional[str] = None) -> list[dict]:
    init_db()
    if due_before is None:
        due_before = _now()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT f.*, c.nome_empresa, c.primary_email, c.status AS contact_status
            FROM crm_followups f
            JOIN crm_contacts c ON c.id = f.contact_id
            WHERE f.status = 'pending'
              AND (f.scheduled_for IS NULL OR f.scheduled_for <= ?)
              AND c.do_not_contact = 0
            ORDER BY f.scheduled_for ASC
            LIMIT 50
            """,
            (due_before,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_email_replies(status_filter: Optional[str] = "unread", limit: int = 100) -> list[dict]:
    init_db()
    with get_conn() as conn:
        params: list = []
        where = ""
        if status_filter == "all":
            where = ""
        elif status_filter == "archived":
            where = "WHERE r.status = 'archived'"
        elif status_filter:
            where = "WHERE r.status = ?"
            params.append(status_filter)
        else:
            where = "WHERE r.status != 'archived'"
            
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT
                r.*,
                c.nome_empresa,
                c.segmento,
                c.cidade,
                c.status AS crm_status
            FROM crm_email_replies r
            LEFT JOIN crm_contacts c ON c.id = r.contact_id
            {where}
            ORDER BY r.received_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def update_reply_status(reply_id: int, status: str):
    init_db()
    with get_conn() as conn:
        conn.execute("UPDATE crm_email_replies SET status = ? WHERE id = ?", (status, reply_id))


def update_envio_status(envio_id: int, status: str):
    init_db()
    with get_conn() as conn:
        conn.execute("UPDATE envios SET status = ? WHERE id = ?", (status, envio_id))


def archive_contact_replies(contact_id: int):
    init_db()
    with get_conn() as conn:
        conn.execute("UPDATE crm_email_replies SET status = 'archived' WHERE contact_id = ?", (contact_id,))


def archive_contact_envios(contact_id: int):
    init_db()
    with get_conn() as conn:
        conn.execute("UPDATE envios SET status = 'archived' WHERE contact_id = ?", (contact_id,))


def update_contact_status(contact_id: int, status: str, note: Optional[str] = None) -> Optional[dict]:
    init_db()
    status = (status or "").strip().lower()
    if status not in EDITABLE_STATUSES:
        raise ValueError("Status de CRM inválido.")

    with get_conn() as conn:
        current = get_contact(contact_id, conn=conn)
        if not current:
            return None

        do_not_contact = 1 if status in {"blocked", "replied", "bounced"} else 0
        recall_at = _now() if status == "recall" else current.get("recall_at")
        last_reply_at = _now() if status == "replied" else current.get("last_reply_at")
        blocked_reason = note if status == "blocked" and note else current.get("blocked_reason")

        conn.execute(
            """
            UPDATE crm_contacts
            SET status = ?, do_not_contact = ?, recall_at = ?, last_reply_at = ?, blocked_reason = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                do_not_contact,
                recall_at,
                last_reply_at,
                blocked_reason,
                _now(),
                contact_id,
            ),
        )
        _log_event(conn, contact_id, "status_change", current.get("status"), status, note=note)
        return get_contact(contact_id, conn=conn)


def get_history(limit: int = 100) -> list[dict]:
    init_db()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                e.*,
                l.segmento,
                l.cidade,
                l.score,
                l.prioridade,
                c.status AS crm_status,
                c.send_count AS crm_send_count
            FROM envios e
            LEFT JOIN leads l ON e.lead_id = l.id
            LEFT JOIN crm_contacts c ON e.contact_id = c.id
            WHERE e.status != 'archived'
            ORDER BY e.enviado_em DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_stats() -> dict:
    init_db()
    today = datetime.now().date().isoformat()
    with get_conn() as conn:
        total_leads = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        total_contacts = conn.execute("SELECT COUNT(*) FROM crm_contacts").fetchone()[0]
        total_enviados = conn.execute("SELECT COUNT(*) FROM envios WHERE status='sent'").fetchone()[0]
        total_erros = conn.execute("SELECT COUNT(*) FROM envios WHERE status='error'").fetchone()[0]
        sent_once = conn.execute("SELECT COUNT(*) FROM crm_contacts WHERE send_count >= 1").fetchone()[0]
        recall = conn.execute("SELECT COUNT(*) FROM crm_contacts WHERE status = 'recall'").fetchone()[0]
        replied = conn.execute("SELECT COUNT(*) FROM crm_contacts WHERE status = 'replied'").fetchone()[0]
        blocked = conn.execute("SELECT COUNT(*) FROM crm_contacts WHERE status = 'blocked' OR do_not_contact = 1").fetchone()[0]
        replies_unread = conn.execute("SELECT COUNT(*) FROM crm_email_replies WHERE status = 'unread'").fetchone()[0]
        queue = conn.execute(
            "SELECT COUNT(*) FROM crm_contacts WHERE status IN ('ready','new') AND email_draft_assunto IS NOT NULL"
        ).fetchone()[0]
        action_counts = {}
        for action in ("send_email", "send_instagram_dm", "call_or_whatsapp", "find_contact", "followup"):
            action_sql, action_params = _next_action_sql(action)
            action_counts[action] = conn.execute(f"SELECT COUNT(*) FROM crm_contacts c WHERE {action_sql}", action_params).fetchone()[0]
        followup_due = conn.execute(
            "SELECT COUNT(*) FROM crm_contacts WHERE recall_at IS NOT NULL AND recall_at <= ?",
            (today,),
        ).fetchone()[0]
        ultimos_runs = conn.execute(
            """
            SELECT DISTINCT l.run_id,
                   COALESCE(
                       (SELECT t.name FROM scheduled_tasks t JOIN task_runs tr ON tr.task_id = t.id 
                        WHERE substr(replace(replace(replace(tr.started_at, '-', ''), 'T', '_'), ':', ''), 1, 15) = l.run_id LIMIT 1),
                       'Campanha ' || l.run_id
                   ) AS routine_name
            FROM leads l WHERE l.run_id IS NOT NULL ORDER BY l.criado_em DESC LIMIT 10
            """
        ).fetchall()
        rotinas = conn.execute("SELECT DISTINCT name FROM scheduled_tasks WHERE name IS NOT NULL ORDER BY name ASC").fetchall()

    def _format_run_label(r_id, r_name):
        if not r_id:
            return "Sem lote"
        m = re.match(r"^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})", str(r_id))
        date_part = f"{m.group(3)}/{m.group(2)} às {m.group(4)}:{m.group(5)}" if m else str(r_id)
        return f"Lote {date_part} ({r_name})" if r_name and not r_name.startswith("Campanha") else f"Lote {date_part}"

    return {
        "total_leads": total_leads,
        "total_contacts": total_contacts,
        "total_enviados": total_enviados,
        "total_erros": total_erros,
        "sent_once": sent_once,
        "recall": recall,
        "replied": replied,
        "blocked": blocked,
        "replies_unread": replies_unread,
        "queue": queue,
        "actions": action_counts,
        "followup_due": followup_due,
        "runs": [row[0] for row in ultimos_runs],
        "runs_meta": [{"id": row[0], "label": _format_run_label(row[0], row[1]), "routine": row[1]} for row in ultimos_runs],
        "routines": [row[0] for row in rotinas],
    }


def get_contact_by_email(email: str, conn=None) -> Optional[dict]:
    init_db()
    own = conn is None
    conn = conn or get_conn()
    try:
        normalized = _normalize_email(email)
        row = conn.execute(
            "SELECT * FROM crm_contacts WHERE normalized_email = ? LIMIT 1",
            (normalized,),
        ).fetchone()
        return _row_to_contact(row) if row else None
    finally:
        if own:
            conn.close()


def get_contacts_needing_followup_generation(conn=None) -> list[dict]:
    init_db()
    own = conn is None
    conn = conn or get_conn()
    try:
        rows = conn.execute(
            """
            SELECT c.*
            FROM crm_contacts c
            WHERE c.status = 'sent_1x'
              AND c.do_not_contact = 0
              AND c.send_count >= 1
              AND c.send_count < 3
              AND c.id NOT IN (
                  SELECT contact_id FROM crm_followups 
                  WHERE status = 'pending' 
                     OR (status = 'sent' AND attempt_no = c.send_count + 1)
              )
            """
        ).fetchall()
        return [_row_to_contact(row) for row in rows]
    finally:
        if own:
            conn.close()


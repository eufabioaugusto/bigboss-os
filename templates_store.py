import json
import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

import crm
from settings_store import get_company

BASE_DIR = Path(__file__).parent
TEMPLATES_FILE = BASE_DIR / 'templates' / 'email_templates.yaml'
DEFAULT_ANGLE_MAP = {
    'presenca_digital': ['sem_site', 'site_fraco', 'instagram_parado', 'identidade_fraca', 'comunicacao_generica', 'default'],
    'autoridade_local': ['baixa_autoridade'],
    'ecommerce_vendas': ['sem_funil'],
}
DEFAULT_TAGS_MAP = {
    'presenca_digital': ['presença digital', 'site', 'instagram'],
    'autoridade_local': ['negócio local', 'google maps'],
    'ecommerce_vendas': ['e-commerce', 'vendas'],
}
SAMPLE_LEAD = {
    'nome_empresa': 'Clínica Exemplo',
    'segmento': 'clínica odontológica',
    'cidade': 'Belo Horizonte',
    'sinais': ['site desatualizado', 'instagram parado'],
    'angulo': 'site_fraco',
}


def _now() -> str:
    return datetime.now().isoformat()


def init_db():
    crm.init_db()
    with crm.get_conn() as conn:
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS email_templates (
                id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                description TEXT,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                angle_targets TEXT NOT NULL DEFAULT '[]',
                tags TEXT NOT NULL DEFAULT '[]',
                priority INTEGER NOT NULL DEFAULT 100,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            '''
        )
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS template_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id TEXT NOT NULL,
                version_no INTEGER NOT NULL,
                snapshot TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            '''
        )
        _ensure_column(conn, 'email_templates', 'tags', "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, 'email_templates', 'priority', 'INTEGER NOT NULL DEFAULT 100')


def _ensure_column(conn, table: str, column: str, definition: str):
    cols = [row[1] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()]
    if column not in cols:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')


def _slugify(value: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', (value or '').strip().lower()).strip('-')
    return slug or f'template-{datetime.now().strftime("%H%M%S")}'


def _safe_json_load(value: str, default):
    try:
        return json.loads(value or json.dumps(default))
    except json.JSONDecodeError:
        return default


def _row_to_dict(row) -> dict:
    data = dict(row)
    data['is_active'] = bool(data.get('is_active', 1))
    data['angle_targets'] = _safe_json_load(data.get('angle_targets'), [])
    data['tags'] = _safe_json_load(data.get('tags'), [])
    data['assunto'] = data.pop('subject')
    data['corpo'] = data.pop('body')
    return data


def _load_seed_templates() -> list[dict]:
    if not TEMPLATES_FILE.exists():
        return []
    with open(TEMPLATES_FILE, encoding='utf-8') as file:
        raw = yaml.safe_load(file) or {}
    items = []
    for template_id, payload in (raw.get('templates') or {}).items():
        items.append({
            'id': payload.get('id') or template_id,
            'label': payload.get('label') or template_id,
            'description': payload.get('description', ''),
            'assunto': payload.get('assunto', ''),
            'corpo': payload.get('corpo', ''),
            'is_active': True,
            'angle_targets': deepcopy(DEFAULT_ANGLE_MAP.get(template_id, ['default'])),
            'tags': deepcopy(DEFAULT_TAGS_MAP.get(template_id, [])),
            'priority': 100,
        })
    return items


def _snapshot_payload(template: dict) -> str:
    payload = deepcopy(template)
    return json.dumps(payload, ensure_ascii=False)


def _insert_version(conn, template: dict):
    latest = conn.execute(
        'SELECT COALESCE(MAX(version_no), 0) FROM template_versions WHERE template_id = ?',
        (template['id'],),
    ).fetchone()[0]
    conn.execute(
        '''
        INSERT INTO template_versions (template_id, version_no, snapshot, created_at)
        VALUES (?, ?, ?, ?)
        ''',
        (template['id'], latest + 1, _snapshot_payload(template), _now()),
    )




def _backfill_versions(conn):
    template_rows = conn.execute('SELECT * FROM email_templates').fetchall()
    for row in template_rows:
        template = _row_to_dict(row)
        count = conn.execute('SELECT COUNT(*) FROM template_versions WHERE template_id = ?', (template['id'],)).fetchone()[0]
        if count == 0:
            _insert_version(conn, template)

def reload_seed_templates(force: bool = False):
    init_db()
    with crm.get_conn() as conn:
        count = conn.execute('SELECT COUNT(*) FROM email_templates').fetchone()[0]
        if count and not force:
            _backfill_versions(conn)
            return
        if force:
            conn.execute('DELETE FROM email_templates')
        stamp = _now()
        for template in _load_seed_templates():
            conn.execute(
                '''
                INSERT INTO email_templates (id, label, description, subject, body, is_active, angle_targets, tags, priority, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    template['id'],
                    template['label'],
                    template.get('description', ''),
                    template.get('assunto', ''),
                    template.get('corpo', ''),
                    1 if template.get('is_active', True) else 0,
                    json.dumps(template.get('angle_targets', []), ensure_ascii=False),
                    json.dumps(template.get('tags', []), ensure_ascii=False),
                    template.get('priority', 100),
                    stamp,
                    stamp,
                ),
            )
            _insert_version(conn, {**template, 'created_at': stamp, 'updated_at': stamp})

def seed_defaults():
    reload_seed_templates(force=False)


def get_templates(active_only: bool = False) -> list[dict]:
    init_db()
    seed_defaults()
    query = 'SELECT * FROM email_templates'
    if active_only:
        query += ' WHERE is_active = 1'
    query += ' ORDER BY priority ASC, updated_at DESC, label ASC'
    with crm.get_conn() as conn:
        rows = conn.execute(query).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_template(template_id: str) -> Optional[dict]:
    init_db()
    seed_defaults()
    with crm.get_conn() as conn:
        row = conn.execute('SELECT * FROM email_templates WHERE id = ?', (template_id,)).fetchone()
    return _row_to_dict(row) if row else None


def get_template_versions(template_id: str) -> list[dict]:
    init_db()
    with crm.get_conn() as conn:
        rows = conn.execute(
            'SELECT version_no, snapshot, created_at FROM template_versions WHERE template_id = ? ORDER BY version_no DESC',
            (template_id,),
        ).fetchall()
    result = []
    for row in rows:
        result.append({
            'version_no': row['version_no'],
            'created_at': row['created_at'],
            'snapshot': _safe_json_load(row['snapshot'], {}),
        })
    return result


def _normalize_payload(payload: dict, template_id: Optional[str] = None) -> dict:
    label = (payload.get('label') or payload.get('name') or 'Novo template').strip()
    tags = payload.get('tags') or []
    if isinstance(tags, str):
        tags = [item.strip() for item in tags.split(',') if item.strip()]
    angles = payload.get('angle_targets') or ['default']
    if isinstance(angles, str):
        angles = [item.strip() for item in angles.split(',') if item.strip()]
    return {
        'id': _slugify(payload.get('id') or template_id or label),
        'label': label,
        'description': (payload.get('description') or '').strip(),
        'assunto': (payload.get('assunto') or payload.get('subject') or '').strip(),
        'corpo': (payload.get('corpo') or payload.get('body') or '').strip(),
        'is_active': bool(payload.get('is_active', True)),
        'angle_targets': angles or ['default'],
        'tags': tags,
        'priority': int(payload.get('priority', 100) or 100),
        'updated_at': _now(),
    }


def create_template(payload: dict) -> dict:
    init_db()
    seed_defaults()
    data = _normalize_payload(payload)
    data['created_at'] = data['updated_at']
    with crm.get_conn() as conn:
        conn.execute(
            '''
            INSERT INTO email_templates (id, label, description, subject, body, is_active, angle_targets, tags, priority, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                data['id'], data['label'], data['description'], data['assunto'], data['corpo'],
                1 if data['is_active'] else 0, json.dumps(data['angle_targets'], ensure_ascii=False),
                json.dumps(data['tags'], ensure_ascii=False), data['priority'], data['created_at'], data['updated_at'],
            ),
        )
        _insert_version(conn, data)
    return get_template(data['id'])


def update_template(template_id: str, payload: dict) -> Optional[dict]:
    current = get_template(template_id)
    if not current:
        return None
    merged = deepcopy(current)
    merged.update(payload)
    data = _normalize_payload(merged, template_id=template_id)
    with crm.get_conn() as conn:
        conn.execute(
            '''
            UPDATE email_templates
            SET label = ?, description = ?, subject = ?, body = ?, is_active = ?, angle_targets = ?, tags = ?, priority = ?, updated_at = ?
            WHERE id = ?
            ''',
            (
                data['label'], data['description'], data['assunto'], data['corpo'],
                1 if data['is_active'] else 0, json.dumps(data['angle_targets'], ensure_ascii=False),
                json.dumps(data['tags'], ensure_ascii=False), data['priority'], data['updated_at'], template_id,
            ),
        )
        snapshot = {**current, **data, 'id': template_id, 'created_at': current.get('created_at'), 'updated_at': data['updated_at']}
        _insert_version(conn, snapshot)
    return get_template(template_id)


def restore_template_version(template_id: str, version_no: int) -> Optional[dict]:
    init_db()
    with crm.get_conn() as conn:
        row = conn.execute(
            'SELECT snapshot FROM template_versions WHERE template_id = ? AND version_no = ?',
            (template_id, version_no),
        ).fetchone()
    if not row:
        return None
    snapshot = _safe_json_load(row['snapshot'], {})
    snapshot.pop('created_at', None)
    snapshot.pop('updated_at', None)
    return update_template(template_id, snapshot)


def delete_template(template_id: str) -> bool:
    init_db()
    with crm.get_conn() as conn:
        deleted = conn.execute('DELETE FROM email_templates WHERE id = ?', (template_id,)).rowcount
    return bool(deleted)


def duplicate_template(template_id: str) -> Optional[dict]:
    current = get_template(template_id)
    if not current:
        return None
    payload = deepcopy(current)
    payload['id'] = f"{current['id']}-copy-{datetime.now().strftime('%H%M%S')}"
    payload['label'] = f"{current['label']} (Cópia)"
    return create_template(payload)


def render_template_preview(template: dict, lead: Optional[dict] = None, company: Optional[dict] = None) -> dict:
    lead = {**SAMPLE_LEAD, **(lead or {})}
    company = {**get_company(), **(company or {})}
    vars_ = {
        'empresa': lead.get('nome_empresa', 'sua empresa'),
        'segmento': lead.get('segmento', 'seu segmento'),
        'cidade': lead.get('cidade', 'sua cidade'),
        'sinal_principal': (lead.get('sinais') or ['presença digital'])[0],
        'angulo_label': lead.get('angulo', 'default'),
        'contact_name': company.get('contact_name', company.get('name', 'Equipe')),
        'agency_email': company.get('email', 'contato@suaempresa.com'),
        'agency_name': company.get('name', 'Sua Empresa'),
    }
    assunto = template.get('assunto', '')
    corpo = template.get('corpo', '')
    for key, value in vars_.items():
        assunto = assunto.replace(f'{{{key}}}', str(value))
        corpo = corpo.replace(f'{{{key}}}', str(value))
    return {'assunto': assunto.strip(), 'corpo': corpo.strip(), 'sample_lead': lead, 'company': company}

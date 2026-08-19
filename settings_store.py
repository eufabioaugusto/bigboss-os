import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import yaml

import crm

BASE_DIR = Path(__file__).parent
COMPANY_FILE = BASE_DIR / 'company.yaml'
CONFIG_FILE = BASE_DIR / 'config.yaml'

SECTION_KEYS = ('company', 'sender', 'limits', 'mode', 'search', 'scoring', 'imap', 'signature')

DEFAULT_SIGNATURE_HTML = """<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; font-size: 14px; color: #334155; line-height: 1.5;">
  <p style="margin: 0 0 4px 0; font-weight: 700; color: #0f172a; font-size: 15px;">Fabio da Ultraweb</p>
  <p style="margin: 0 0 8px 0; color: #64748b; font-size: 13px;">Estratégia & Crescimento Digital · <a href="https://ultraweb.com.br" target="_blank" style="color: #2563eb; text-decoration: none; font-weight: 500;">ultraweb.com.br</a></p>
  <p style="margin: 0; color: #64748b; font-size: 12.5px;">📞 WhatsApp: <a href="https://wa.me/5511948129812" target="_blank" style="color: #16a34a; text-decoration: none; font-weight: 500;">(11) 94812-9812</a> · ✉️ <a href="mailto:fabio@ultraweb.com.br" style="color: #64748b; text-decoration: none;">fabio@ultraweb.com.br</a></p>
</div>"""


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding='utf-8') as file:
        return yaml.safe_load(file) or {}


def init_db():
    crm.init_db()
    with crm.get_conn() as conn:
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            '''
        )


def _deep_merge(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_defaults() -> dict:
    config = _load_yaml(CONFIG_FILE)
    company = _load_yaml(COMPANY_FILE)
    return {
        'company': company,
        'sender': config.get('sender', {}),
        'limits': config.get('limits', {}),
        'mode': config.get('mode', 'manual'),
        'search': config.get('search', {}),
        'scoring': config.get('scoring', {}),
        'imap': config.get('imap', {}),
        'signature': {
            'is_enabled': True,
            'html': DEFAULT_SIGNATURE_HTML,
        },
    }



def get_settings() -> dict:
    init_db()
    settings = load_defaults()
    with crm.get_conn() as conn:
        rows = conn.execute('SELECT key, value FROM app_settings').fetchall()
    for row in rows:
        if row['key'] not in SECTION_KEYS:
            continue
        try:
            value = json.loads(row['value'])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(settings.get(row['key']), dict):
            settings[row['key']] = _deep_merge(settings[row['key']], value)
        else:
            settings[row['key']] = value
    return settings


def get_runtime_config() -> dict:
    settings = get_settings()
    return {
        'sender': settings.get('sender', {}),
        'limits': settings.get('limits', {}),
        'mode': settings.get('mode', 'manual'),
        'search': settings.get('search', {}),
        'scoring': settings.get('scoring', {}),
        'company': settings.get('company', {}),
    }


def get_company() -> dict:
    return get_settings().get('company', {})


def save_settings(payload: dict) -> dict:
    init_db()
    current = get_settings()
    updated = deepcopy(current)
    stamp = datetime.now().isoformat()

    with crm.get_conn() as conn:
        for section in SECTION_KEYS:
            if section not in payload:
                continue
            value = payload[section]
            if isinstance(value, dict) and isinstance(updated.get(section), dict):
                updated[section] = _deep_merge(updated[section], value)
            else:
                updated[section] = value
            conn.execute(
                '''
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                ''',
                (section, json.dumps(updated[section], ensure_ascii=False), stamp),
            )

    return updated

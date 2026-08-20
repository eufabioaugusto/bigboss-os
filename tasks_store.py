import json
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Optional

import crm

DEFAULT_TASK = {
    "name": "Nova tarefa",
    "task_type": "full_cycle",
    "source": "google",
    "status": "active",
    "interval_minutes": 1440,
    "schedule_time": "09:30",
    "schedule_days": "weekdays",
    "start_date": None,
    "end_date": None,
    "prompt": "",
    "auto_send": False,
    "max_leads_per_run": 10,
    "min_score_to_send": 7,
    "results_per_query": 10,
    "max_emails_per_day": 50,
    "delay_between_sends_seconds": 30,
    "template_id": None,
}


def _now() -> str:
    return datetime.now().isoformat()


def compute_next_run(task: dict, from_dt: Optional[datetime] = None) -> Optional[str]:
    """Calcula a próxima execução de forma inteligente, respeitando horário comercial, dias da semana e período."""
    from_dt = from_dt or datetime.now()
    status = task.get("status", "active")
    if str(status).lower() != "active":
        return None

    # Verifica data de término
    end_date_str = task.get("end_date")
    if end_date_str:
        try:
            end_dt = datetime.fromisoformat(end_date_str) if "T" in end_date_str else datetime.strptime(end_date_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            if from_dt > end_dt:
                return None  # Campanha finalizada
        except Exception:
            pass

    # Verifica data de início
    start_date_str = task.get("start_date")
    start_dt = None
    if start_date_str:
        try:
            start_dt = datetime.fromisoformat(start_date_str) if "T" in start_date_str else datetime.strptime(start_date_str, "%Y-%m-%d")
        except Exception:
            pass

    schedule_time = task.get("schedule_time") or "09:30"
    try:
        sh, sm = map(int, schedule_time.split(":"))
    except Exception:
        sh, sm = 9, 30

    schedule_days = task.get("schedule_days") or "weekdays"

    search_base = from_dt
    if start_dt and start_dt > from_dt:
        search_base = start_dt

    candidate_date = search_base.date()
    for day_offset in range(14):
        target_date = candidate_date + timedelta(days=day_offset)
        candidate_dt = datetime(target_date.year, target_date.month, target_date.day, sh, sm, 0)

        # Deve ser estritamente no futuro
        if candidate_dt <= from_dt:
            continue

        # Verifica dias da semana (0=Seg, 4=Sex, 5=Sáb, 6=Dom)
        weekday = candidate_dt.weekday()
        if schedule_days == "weekdays" and weekday >= 5:
            continue

        # Verifica término
        if end_date_str:
            try:
                end_dt = datetime.fromisoformat(end_date_str) if "T" in end_date_str else datetime.strptime(end_date_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
                if candidate_dt > end_dt:
                    return None
            except Exception:
                pass

        return candidate_dt.isoformat()

    return (from_dt + timedelta(days=1)).replace(hour=sh, minute=sm).isoformat()


def init_db():
    crm.init_db()
    with crm.get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                task_type TEXT NOT NULL,
                status TEXT NOT NULL,
                interval_minutes INTEGER NOT NULL,
                prompt TEXT NOT NULL,
                auto_send INTEGER NOT NULL DEFAULT 0,
                max_leads_per_run INTEGER NOT NULL DEFAULT 10,
                min_score_to_send INTEGER NOT NULL DEFAULT 7,
                results_per_query INTEGER NOT NULL DEFAULT 10,
                max_emails_per_day INTEGER NOT NULL DEFAULT 50,
                delay_between_sends_seconds INTEGER NOT NULL DEFAULT 30,
                last_run_at TEXT,
                next_run_at TEXT,
                last_result TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS task_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                trigger_source TEXT NOT NULL DEFAULT 'scheduler',
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                leads_found INTEGER NOT NULL DEFAULT 0,
                leads_qualified INTEGER NOT NULL DEFAULT 0,
                emails_ready INTEGER NOT NULL DEFAULT 0,
                emails_sent INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                logs_json TEXT NOT NULL DEFAULT '[]',
                summary_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(task_id) REFERENCES scheduled_tasks(id)
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_status_next_run ON scheduled_tasks(status, next_run_at);
            CREATE INDEX IF NOT EXISTS idx_task_runs_task_started ON task_runs(task_id, started_at DESC);
            """
        )
        existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(scheduled_tasks)").fetchall()}
        if "source" not in existing_columns:
            conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN source TEXT NOT NULL DEFAULT 'google'")
        if "template_id" not in existing_columns:
            conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN template_id TEXT")
        if "schedule_time" not in existing_columns:
            conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN schedule_time TEXT DEFAULT '09:30'")
        if "schedule_days" not in existing_columns:
            conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN schedule_days TEXT DEFAULT 'weekdays'")
        if "start_date" not in existing_columns:
            conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN start_date TEXT")
        if "end_date" not in existing_columns:
            conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN end_date TEXT")
        if "campaign_id" not in existing_columns:
            conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN campaign_id TEXT")


def _row_to_task(row) -> dict:
    data = dict(row)
    data["auto_send"] = bool(data.get("auto_send"))
    if not data.get("schedule_time"):
        data["schedule_time"] = "09:30"
    if not data.get("schedule_days"):
        data["schedule_days"] = "weekdays"
    return data


def _row_to_run(row) -> dict:
    data = dict(row)
    try:
        data["logs"] = json.loads(data.pop("logs_json") or "[]")
    except json.JSONDecodeError:
        data["logs"] = []
    try:
        data["summary"] = json.loads(data.pop("summary_json") or "{}")
    except json.JSONDecodeError:
        data["summary"] = {}
    return data


def _normalize_task_payload(payload: dict, current: Optional[dict] = None) -> dict:
    base = deepcopy(DEFAULT_TASK)
    if current:
        base.update(current)
    base.update(payload or {})
    interval_minutes = max(int(base.get("interval_minutes") or 1440), 1)
    return {
        "id": (base.get("id") or f"task_{uuid.uuid4().hex[:10]}").strip(),
        "name": (base.get("name") or "Nova tarefa").strip(),
        "task_type": (base.get("task_type") or "full_cycle").strip(),
        "source": (base.get("source") or "google").strip(),
        "status": "paused" if str(base.get("status")).lower() == "paused" else "active",
        "interval_minutes": interval_minutes,
        "schedule_time": (base.get("schedule_time") or "09:30").strip(),
        "schedule_days": (base.get("schedule_days") or "weekdays").strip(),
        "start_date": (base.get("start_date") or "").strip() or None,
        "end_date": (base.get("end_date") or "").strip() or None,
        "prompt": (base.get("prompt") or "").strip(),
        "auto_send": bool(base.get("auto_send")),
        "max_leads_per_run": max(int(base.get("max_leads_per_run") or 10), 1),
        "min_score_to_send": max(int(base.get("min_score_to_send") or 7), 0),
        "results_per_query": max(int(base.get("results_per_query") or 10), 1),
        "max_emails_per_day": max(int(base.get("max_emails_per_day") or 50), 1),
        "delay_between_sends_seconds": max(int(base.get("delay_between_sends_seconds") or 30), 0),
        "template_id": (base.get("template_id") or "").strip() or None,
        "campaign_id": (base.get("campaign_id") or "").strip() or None,
    }


def list_tasks() -> list[dict]:
    init_db()
    with crm.get_conn() as conn:
        rows = conn.execute("SELECT * FROM scheduled_tasks ORDER BY created_at DESC, name ASC").fetchall()
    return [_row_to_task(row) for row in rows]


def get_task(task_id: str) -> Optional[dict]:
    init_db()
    with crm.get_conn() as conn:
        row = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
    return _row_to_task(row) if row else None


def create_task(payload: dict) -> dict:
    init_db()
    data = _normalize_task_payload(payload)
    if not data["prompt"]:
        raise ValueError("Prompt da tarefa é obrigatório.")
    stamp = _now()
    next_run_at = compute_next_run(data) if data["status"] == "active" else None
    with crm.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO scheduled_tasks (
                id, name, task_type, source, status, interval_minutes, schedule_time, schedule_days,
                start_date, end_date, prompt, auto_send, max_leads_per_run, min_score_to_send, results_per_query,
                max_emails_per_day, delay_between_sends_seconds, template_id, campaign_id, last_run_at, next_run_at, last_result, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["id"], data["name"], data["task_type"], data["source"], data["status"], data["interval_minutes"],
                data["schedule_time"], data["schedule_days"], data["start_date"], data["end_date"], data["prompt"],
                1 if data["auto_send"] else 0, data["max_leads_per_run"], data["min_score_to_send"], data["results_per_query"],
                data["max_emails_per_day"], data["delay_between_sends_seconds"], data["template_id"], data["campaign_id"], None, next_run_at, None, stamp, stamp,
            ),
        )
    return get_task(data["id"])


def update_task(task_id: str, payload: dict) -> Optional[dict]:
    init_db()
    current = get_task(task_id)
    if not current:
        return None
    data = _normalize_task_payload({**current, **payload}, current=current)
    if not data["prompt"]:
        raise ValueError("Prompt da tarefa é obrigatório.")
    stamp = _now()
    next_run_at = current.get("next_run_at")
    if data["status"] == "paused":
        next_run_at = None
    elif payload.get("status") == "active" or "schedule_time" in payload or "schedule_days" in payload or "start_date" in payload or "end_date" in payload:
        next_run_at = compute_next_run(data)
    with crm.get_conn() as conn:
        conn.execute(
            """
            UPDATE scheduled_tasks
            SET name=?, task_type=?, source=?, status=?, interval_minutes=?, schedule_time=?, schedule_days=?,
                start_date=?, end_date=?, prompt=?, auto_send=?, max_leads_per_run=?, min_score_to_send=?,
                results_per_query=?, max_emails_per_day=?, delay_between_sends_seconds=?, template_id=?, campaign_id=?, next_run_at=?, updated_at=?
            WHERE id=?
            """,
            (
                data["name"], data["task_type"], data["source"], data["status"], data["interval_minutes"],
                data["schedule_time"], data["schedule_days"], data["start_date"], data["end_date"], data["prompt"],
                1 if data["auto_send"] else 0, data["max_leads_per_run"], data["min_score_to_send"], data["results_per_query"],
                data["max_emails_per_day"], data["delay_between_sends_seconds"], data["template_id"], data["campaign_id"], next_run_at, stamp, task_id,
            ),
        )
    return get_task(task_id)


def delete_task(task_id: str) -> bool:
    init_db()
    with crm.get_conn() as conn:
        deleted = conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,)).rowcount
    return bool(deleted)


def touch_task_schedule(task_id: str, *, last_result: Optional[str] = None, next_run_at: Optional[str] = None, last_run_at: Optional[str] = None):
    init_db()
    stamp = _now()
    with crm.get_conn() as conn:
        conn.execute(
            """
            UPDATE scheduled_tasks
            SET last_run_at = COALESCE(?, last_run_at),
                next_run_at = ?,
                last_result = COALESCE(?, last_result),
                updated_at = ?
            WHERE id = ?
            """,
            (last_run_at, next_run_at, last_result, stamp, task_id),
        )


def schedule_next_run(task: dict, *, now: Optional[datetime] = None, immediate: bool = False):
    base = now or datetime.now()
    next_run_at = base.isoformat() if immediate else compute_next_run(task, from_dt=base)
    touch_task_schedule(task["id"], next_run_at=next_run_at)


def list_due_tasks(now_iso: Optional[str] = None) -> list[dict]:
    init_db()
    now_iso = now_iso or _now()
    with crm.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM scheduled_tasks
            WHERE status = 'active' AND next_run_at IS NOT NULL AND next_run_at <= ?
            ORDER BY next_run_at ASC
            """,
            (now_iso,),
        ).fetchall()
    return [_row_to_task(row) for row in rows]


def create_task_run(task_id: str, trigger_source: str = "scheduler") -> int:
    init_db()
    with crm.get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO task_runs (
                task_id, trigger_source, status, started_at, finished_at,
                leads_found, leads_qualified, emails_ready, emails_sent, error, logs_json, summary_json
            ) VALUES (?, ?, 'running', ?, NULL, 0, 0, 0, 0, NULL, '[]', '{}')
            """,
            (task_id, trigger_source, _now()),
        )
        return int(cur.lastrowid)


def finish_task_run(run_id: int, *, status: str, leads_found: int = 0, leads_qualified: int = 0, emails_ready: int = 0, emails_sent: int = 0, error: Optional[str] = None, logs: Optional[list] = None, summary: Optional[dict] = None):
    init_db()
    with crm.get_conn() as conn:
        conn.execute(
            """
            UPDATE task_runs
            SET status = ?,
                finished_at = ?,
                leads_found = ?,
                leads_qualified = ?,
                emails_ready = ?,
                emails_sent = ?,
                error = ?,
                logs_json = ?,
                summary_json = ?
            WHERE id = ?
            """,
            (
                status,
                _now(),
                int(leads_found or 0),
                int(leads_qualified or 0),
                int(emails_ready or 0),
                int(emails_sent or 0),
                error,
                json.dumps(logs or [], ensure_ascii=False),
                json.dumps(summary or {}, ensure_ascii=False),
                run_id,
            ),
        )


def list_task_runs(task_id: Optional[str] = None, limit: int = 50) -> list[dict]:
    init_db()
    query = "SELECT * FROM task_runs"
    params = []
    if task_id:
        query += " WHERE task_id = ?"
        params.append(task_id)
    query += " ORDER BY started_at DESC LIMIT ?"
    params.append(max(int(limit or 50), 1))
    with crm.get_conn() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [_row_to_run(row) for row in rows]


get_recent_runs = list_task_runs

"""SQLite job store for post-close Mode B data updates.

This is the server-side source of truth for the long-running post-close
scheduler. It records every attempt, keeps PASS/BLOCKED history by target date,
and lets the worker avoid duplicate successful runs.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.utils.config_loader import load as config_load
from src.utils.safe_cast import safe_bool

_KST = ZoneInfo("Asia/Seoul")
_ROOT = Path(__file__).resolve().parents[3]


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _resolve_db_path(db_path: Path | str | None = None) -> Path:
    if db_path is not None:
        candidate = Path(db_path)
    else:
        cfg = config_load("risk_config.yaml", "post_close_data_update.scheduler") or {}
        db_cfg = cfg.get("db", {}) or {}
        candidate = Path(str(db_cfg.get("path") or "artifacts/db/post_close_jobs.sqlite3"))
    return candidate if candidate.is_absolute() else _ROOT / candidate


class PostCloseJobStore:
    """SQLite-backed job history for post-close update workers."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        cfg = config_load("risk_config.yaml", "post_close_data_update.scheduler") or {}
        db_cfg = cfg.get("db", {}) or {}
        self.enabled = safe_bool(db_cfg.get("enabled"), default=True)
        self.db_path = _resolve_db_path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS post_close_job_runs (
                    run_id TEXT PRIMARY KEY,
                    target_end_date TEXT NOT NULL,
                    bundle_id TEXT,
                    status TEXT NOT NULL,
                    dry_run INTEGER NOT NULL DEFAULT 0,
                    run_prelive INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    report_path TEXT,
                    blockers_json TEXT,
                    decision_json TEXT,
                    update_report_json TEXT,
                    registry_mutated INTEGER NOT NULL DEFAULT 0,
                    live_trading_allowed INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_post_close_target_started
                ON post_close_job_runs (target_end_date, started_at DESC)
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_post_close_status
                ON post_close_job_runs (status)
                """
            )

    def create_run(
        self,
        *,
        target_end_date: str,
        bundle_id: str | None,
        dry_run: bool,
        run_prelive: bool,
        decision: dict[str, Any],
        started_at: datetime | None = None,
    ) -> str:
        run_id = f"PCR-{uuid.uuid4().hex[:12].upper()}"
        started = (started_at or datetime.now(_KST)).astimezone(_KST).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO post_close_job_runs (
                    run_id, target_end_date, bundle_id, status, dry_run,
                    run_prelive, started_at, decision_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    str(target_end_date),
                    bundle_id,
                    "RUNNING",
                    int(bool(dry_run)),
                    int(bool(run_prelive)),
                    started,
                    _json_dump(decision),
                ),
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        report_path: str | None,
        blockers: list[str] | None,
        update_report: dict[str, Any],
        registry_mutated: bool,
        live_trading_allowed: bool,
        finished_at: datetime | None = None,
    ) -> None:
        finished = (finished_at or datetime.now(_KST)).astimezone(_KST).isoformat()
        with self._lock:
            self._conn.execute(
                """
                UPDATE post_close_job_runs
                SET status = ?,
                    finished_at = ?,
                    report_path = ?,
                    blockers_json = ?,
                    update_report_json = ?,
                    registry_mutated = ?,
                    live_trading_allowed = ?
                WHERE run_id = ?
                """,
                (
                    str(status),
                    finished,
                    report_path,
                    _json_dump(blockers or []),
                    _json_dump(update_report),
                    int(bool(registry_mutated)),
                    int(bool(live_trading_allowed)),
                    run_id,
                ),
            )

    def latest_run(self, target_end_date: str | None = None) -> dict[str, Any] | None:
        query = "SELECT * FROM post_close_job_runs"
        params: tuple[Any, ...] = ()
        if target_end_date is not None:
            query += " WHERE target_end_date = ?"
            params = (str(target_end_date),)
        query += " ORDER BY started_at DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def has_passed_target(self, target_end_date: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM post_close_job_runs
                WHERE target_end_date = ? AND status = 'PASS'
                LIMIT 1
                """,
                (str(target_end_date),),
            ).fetchone()
        return row is not None

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "target_end_date": row["target_end_date"],
            "bundle_id": row["bundle_id"],
            "status": row["status"],
            "dry_run": bool(row["dry_run"]),
            "run_prelive": bool(row["run_prelive"]),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "report_path": row["report_path"],
            "blockers": _json_load(row["blockers_json"], []),
            "decision": _json_load(row["decision_json"], {}),
            "update_report": _json_load(row["update_report_json"], {}),
            "registry_mutated": bool(row["registry_mutated"]),
            "live_trading_allowed": bool(row["live_trading_allowed"]),
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()

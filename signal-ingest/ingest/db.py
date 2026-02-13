from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Union

from ingest.config import Settings

log = logging.getLogger(__name__)


# ============================================================================
# MySQL Backend
# ============================================================================

class MySQL:
    def __init__(self, pool):
        self.pool = pool

    @contextmanager
    def connection(self):
        conn = self.pool.get_connection()
        try:
            yield conn
        finally:
            conn.close()


def create_mysql(settings: Settings):
    import mysql.connector
    from mysql.connector import pooling
    
    pool_config = {
        "pool_name": "ingest_pool",
        "pool_size": 2,
        "pool_reset_session": True,
        "host": settings.mysql_host,
        "port": settings.mysql_port,
        "user": settings.mysql_user,
        "password": settings.mysql_password,
        "database": settings.mysql_database,
        "charset": "utf8mb4",
        "collation": "utf8mb4_unicode_ci",
        "autocommit": False,
    }

    log.info("Creating MySQL connection pool to %s:%d/%s", 
             settings.mysql_host, settings.mysql_port, settings.mysql_database)
    
    pool = pooling.MySQLConnectionPool(**pool_config)
    return MySQL(pool=pool)


# ============================================================================
# Oracle Backend (legacy)
# ============================================================================

class Oracle:
    def __init__(self, pool):
        self.pool = pool

    @contextmanager
    def connection(self):
        conn = self.pool.acquire()
        try:
            yield conn
        finally:
            self.pool.release(conn)


def create_oracle(settings: Settings) -> Oracle:
    import oracledb
    
    oracledb.defaults.fetch_lobs = True
    wallet_dir = settings.oracle_wallet_dir.strip()
    if wallet_dir:
        os.environ.setdefault("TNS_ADMIN", wallet_dir)

    pool_kwargs: dict = {
        "user": settings.oracle_user,
        "password": settings.oracle_password,
        "dsn": settings.oracle_dsn,
        "min": 1,
        "max": 2,
        "increment": 1,
    }
    if wallet_dir:
        pool_kwargs["config_dir"] = wallet_dir
        pool_kwargs["wallet_location"] = wallet_dir

    pool = oracledb.create_pool(**pool_kwargs)
    return Oracle(pool=pool)


# ============================================================================
# Factory function
# ============================================================================

Database = Union[MySQL, Oracle]


def create_db(settings: Settings) -> Database:
    """Create database connection based on settings."""
    if settings.db_backend == "mysql":
        return create_mysql(settings)
    else:
        return create_oracle(settings)


@dataclass(frozen=True)
class Job:
    job_id: int
    type: str
    payload: Dict[str, Any]
    attempts: int


def claim_next_job(db: Database, *, allowed_types: List[str]) -> Optional[Job]:
    if not allowed_types:
        raise ValueError("allowed_types must be non-empty")

    with db.connection() as conn:
        cur = conn.cursor()
        
        if isinstance(db, MySQL):
            # MySQL syntax
            placeholders = ", ".join(["%s"] * len(allowed_types))
            cur.execute(
                f"""
                SELECT job_id, type, payload_json, attempts
                FROM jobs
                WHERE status = 'pending'
                  AND type IN ({placeholders})
                ORDER BY updated_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                tuple(allowed_types),
            )
        else:
            # Oracle syntax
            type_binds = ", ".join(f":t{i}" for i in range(len(allowed_types)))
            params: Dict[str, Any] = {f"t{i}": t for i, t in enumerate(allowed_types)}
            cur.execute(
                f"""
                SELECT job_id, type, payload_json, attempts
                FROM jobs
                WHERE status = 'pending'
                  AND type IN ({type_binds})
                ORDER BY updated_at
                FETCH FIRST 1 ROWS ONLY
                FOR UPDATE SKIP LOCKED
                """,
                params,
            )
        
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return None

        job_id = int(row[0])
        
        if isinstance(db, MySQL):
            cur.execute(
                "UPDATE jobs SET status = 'in_progress' WHERE job_id = %s",
                (job_id,),
            )
        else:
            cur.execute(
                "UPDATE jobs SET status = 'in_progress', updated_at = SYSTIMESTAMP WHERE job_id = :job_id",
                {"job_id": job_id},
            )
        conn.commit()

        payload_raw = row[2] or "{}"
        payload = json.loads(payload_raw)
        return Job(job_id=job_id, type=row[1], payload=payload, attempts=int(row[3]))


def complete_job(db: Database, *, job_id: int) -> None:
    with db.connection() as conn:
        cur = conn.cursor()
        if isinstance(db, MySQL):
            cur.execute("UPDATE jobs SET status='done' WHERE job_id=%s", (job_id,))
        else:
            cur.execute(
                "UPDATE jobs SET status='done', updated_at=SYSTIMESTAMP WHERE job_id=:job_id",
                {"job_id": job_id},
            )
        conn.commit()


def fail_job(db: Database, *, job_id: int, attempts: int, max_attempts: int = 3) -> None:
    status = "pending" if attempts + 1 < max_attempts else "failed"
    with db.connection() as conn:
        cur = conn.cursor()
        if isinstance(db, MySQL):
            cur.execute(
                "UPDATE jobs SET status=%s, attempts=%s WHERE job_id=%s",
                (status, attempts + 1, job_id),
            )
        else:
            cur.execute(
                """
                UPDATE jobs
                SET status=:status, attempts=:attempts, updated_at=SYSTIMESTAMP
                WHERE job_id=:job_id
                """,
                {"status": status, "attempts": attempts + 1, "job_id": job_id},
            )
        conn.commit()


def is_job_cancelled(db: Database, *, job_id: int) -> bool:
    """Check if a job has been cancelled (e.g., user started a new linking attempt)."""
    with db.connection() as conn:
        cur = conn.cursor()
        if isinstance(db, MySQL):
            cur.execute("SELECT status FROM jobs WHERE job_id = %s", (job_id,))
        else:
            cur.execute("SELECT status FROM jobs WHERE job_id = :job_id", {"job_id": job_id})
        row = cur.fetchone()
        if row is None:
            return True  # Job doesn't exist, treat as cancelled
        return row[0] == "cancelled"


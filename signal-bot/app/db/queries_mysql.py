from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.db.mysql import MySQL, is_mysql_error, MYSQL_ERR_DUP_ENTRY


@dataclass(frozen=True)
class RawMessage:
    message_id: str
    group_id: str
    ts: int
    sender_hash: str
    content_text: str
    reply_to_id: str | None


def insert_raw_message(db: MySQL, msg: RawMessage) -> None:
    with db.connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO raw_messages(message_id, group_id, ts, sender_hash, content_text, reply_to_id)
                VALUES(%s, %s, %s, %s, %s, %s)
                """,
                (
                    msg.message_id,
                    msg.group_id,
                    msg.ts,
                    msg.sender_hash,
                    msg.content_text,
                    msg.reply_to_id,
                ),
            )
            conn.commit()
        except Exception as exc:
            # Error 1062: Duplicate entry (duplicate message_id)
            if is_mysql_error(exc, MYSQL_ERR_DUP_ENTRY):
                conn.rollback()
                return
            conn.rollback()
            raise


def enqueue_job(db: MySQL, job_type: str, payload: Dict[str, Any]) -> None:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO jobs(type, payload_json, status, attempts)
            VALUES(%s, %s, 'pending', 0)
            """,
            (job_type, json.dumps(payload, ensure_ascii=False)),
        )
        conn.commit()


def get_raw_message(db: MySQL, message_id: str) -> Optional[RawMessage]:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT message_id, group_id, ts, sender_hash, content_text, reply_to_id
            FROM raw_messages
            WHERE message_id = %s
            """,
            (message_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return RawMessage(
            message_id=row[0],
            group_id=row[1],
            ts=int(row[2]),
            sender_hash=row[3],
            content_text=row[4] or "",
            reply_to_id=row[5],
        )


def get_last_messages_text(db: MySQL, group_id: str, n: int) -> List[str]:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT content_text
            FROM raw_messages
            WHERE group_id = %s
            ORDER BY ts DESC
            LIMIT %s
            """,
            (group_id, n),
        )
        rows = cur.fetchall()
        # rows are newest-first; reverse for natural reading order
        return [r[0] or "" for r in reversed(rows)]


def get_buffer(db: MySQL, group_id: str) -> str:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT buffer_text FROM buffers WHERE group_id = %s",
            (group_id,),
        )
        row = cur.fetchone()
        return (row[0] or "") if row else ""


def set_buffer(db: MySQL, group_id: str, buffer_text: str) -> None:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO buffers (group_id, buffer_text)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE buffer_text = VALUES(buffer_text)
            """,
            (group_id, buffer_text),
        )
        conn.commit()


def new_case_id(db: MySQL) -> str:
    # Generate a UUID and return as lowercase hex (32 chars)
    return uuid.uuid4().hex


def insert_case(
    db: MySQL,
    *,
    case_id: str,
    group_id: str,
    status: str,
    problem_title: str,
    problem_summary: str,
    solution_summary: str,
    tags: List[str],
    evidence_ids: List[str],
) -> None:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO cases(
              case_id, group_id, status, problem_title, problem_summary, solution_summary, tags_json
            )
            VALUES(%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                case_id,
                group_id,
                status,
                problem_title,
                problem_summary,
                solution_summary,
                json.dumps(tags, ensure_ascii=False),
            ),
        )
        for mid in evidence_ids:
            cur.execute(
                "INSERT INTO case_evidence(case_id, message_id) VALUES(%s, %s)",
                (case_id, mid),
            )
        conn.commit()


def create_history_token(db: MySQL, *, token: str, admin_id: str, group_id: str, ttl_minutes: int) -> None:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO history_tokens(token, admin_id, group_id, expires_at, used_at)
            VALUES(%s, %s, %s, DATE_ADD(NOW(), INTERVAL %s MINUTE), NULL)
            """,
            (token, admin_id, group_id, ttl_minutes),
        )
        conn.commit()


def validate_history_token(db: MySQL, *, token: str, group_id: str) -> bool:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT 1
            FROM history_tokens
            WHERE token = %s
              AND group_id = %s
              AND used_at IS NULL
              AND expires_at > NOW()
            """,
            (token, group_id),
        )
        return cur.fetchone() is not None


def mark_history_token_used(db: MySQL, *, token: str) -> None:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE history_tokens
            SET used_at = NOW()
            WHERE token = %s
            """,
            (token,),
        )
        conn.commit()


@dataclass(frozen=True)
class Job:
    job_id: int
    type: str
    payload: Dict[str, Any]
    attempts: int


def claim_next_job(db: MySQL, *, allowed_types: List[str]) -> Optional[Job]:
    if not allowed_types:
        raise ValueError("allowed_types must be non-empty")

    placeholders = ", ".join(["%s"] * len(allowed_types))

    with db.connection() as conn:
        cur = conn.cursor()
        # MySQL 8.0+ supports SELECT ... FOR UPDATE SKIP LOCKED
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
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return None

        job_id = int(row[0])
        cur.execute(
            """
            UPDATE jobs
            SET status = 'in_progress'
            WHERE job_id = %s
            """,
            (job_id,),
        )
        conn.commit()

        payload_raw = row[2] or "{}"
        payload = json.loads(payload_raw)
        return Job(job_id=job_id, type=row[1], payload=payload, attempts=int(row[3]))


def complete_job(db: MySQL, *, job_id: int) -> None:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE jobs SET status='done' WHERE job_id=%s",
            (job_id,),
        )
        conn.commit()


def fail_job(db: MySQL, *, job_id: int, attempts: int, max_attempts: int = 3) -> None:
    status = "pending" if attempts + 1 < max_attempts else "failed"
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE jobs
            SET status=%s, attempts=%s
            WHERE job_id=%s
            """,
            (status, attempts + 1, job_id),
        )
        conn.commit()

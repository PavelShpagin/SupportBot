from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.db.oracle import Oracle, is_ora_error


@dataclass(frozen=True)
class RawMessage:
    message_id: str
    group_id: str
    ts: int
    sender_hash: str
    content_text: str
    reply_to_id: str | None


def insert_raw_message(db: Oracle, msg: RawMessage) -> None:
    with db.connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO raw_messages(message_id, group_id, ts, sender_hash, content_text, reply_to_id)
                VALUES(:message_id, :group_id, :ts, :sender_hash, :content_text, :reply_to_id)
                """,
                {
                    "message_id": msg.message_id,
                    "group_id": msg.group_id,
                    "ts": msg.ts,
                    "sender_hash": msg.sender_hash,
                    "content_text": msg.content_text,
                    "reply_to_id": msg.reply_to_id,
                },
            )
            conn.commit()
        except Exception as exc:
            # ORA-00001: unique constraint violated (duplicate message_id)
            if is_ora_error(exc, 1):
                conn.rollback()
                return
            conn.rollback()
            raise


def enqueue_job(db: Oracle, job_type: str, payload: Dict[str, Any]) -> None:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO jobs(type, payload_json, status, attempts, updated_at)
            VALUES(:type, :payload_json, 'pending', 0, SYSTIMESTAMP)
            """,
            {"type": job_type, "payload_json": json.dumps(payload, ensure_ascii=False)},
        )
        conn.commit()


def get_raw_message(db: Oracle, message_id: str) -> Optional[RawMessage]:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT message_id, group_id, ts, sender_hash, content_text, reply_to_id
            FROM raw_messages
            WHERE message_id = :message_id
            """,
            {"message_id": message_id},
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


def get_last_messages_text(db: Oracle, group_id: str, n: int) -> List[str]:
    # Use ROWNUM so the limit can be bound.
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT content_text
            FROM (
              SELECT content_text
              FROM raw_messages
              WHERE group_id = :group_id
              ORDER BY ts DESC
            )
            WHERE ROWNUM <= :n
            """,
            {"group_id": group_id, "n": n},
        )
        rows = cur.fetchall()
        # rows are newest-first; reverse for natural reading order
        return [r[0] or "" for r in reversed(rows)]


def get_buffer(db: Oracle, group_id: str) -> str:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT buffer_text FROM buffers WHERE group_id = :group_id",
            {"group_id": group_id},
        )
        row = cur.fetchone()
        return (row[0] or "") if row else ""


def set_buffer(db: Oracle, group_id: str, buffer_text: str) -> None:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            MERGE INTO buffers b
            USING (SELECT :group_id AS group_id, :buffer_text AS buffer_text FROM dual) s
            ON (b.group_id = s.group_id)
            WHEN MATCHED THEN UPDATE SET b.buffer_text = s.buffer_text, b.updated_at = SYSTIMESTAMP
            WHEN NOT MATCHED THEN INSERT (group_id, buffer_text, updated_at) VALUES (s.group_id, s.buffer_text, SYSTIMESTAMP)
            """,
            {"group_id": group_id, "buffer_text": buffer_text},
        )
        conn.commit()


def new_case_id(db: Oracle) -> str:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT LOWER(RAWTOHEX(SYS_GUID())) FROM dual")
        return cur.fetchone()[0]


def insert_case(
    db: Oracle,
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
              case_id, group_id, status, problem_title, problem_summary, solution_summary, tags_json, created_at, updated_at
            )
            VALUES(
              :case_id, :group_id, :status, :problem_title, :problem_summary, :solution_summary, :tags_json, SYSTIMESTAMP, SYSTIMESTAMP
            )
            """,
            {
                "case_id": case_id,
                "group_id": group_id,
                "status": status,
                "problem_title": problem_title,
                "problem_summary": problem_summary,
                "solution_summary": solution_summary,
                "tags_json": json.dumps(tags, ensure_ascii=False),
            },
        )
        for mid in evidence_ids:
            cur.execute(
                "INSERT INTO case_evidence(case_id, message_id) VALUES(:case_id, :message_id)",
                {"case_id": case_id, "message_id": mid},
            )
        conn.commit()


def create_history_token(db: Oracle, *, token: str, admin_id: str, group_id: str, ttl_minutes: int) -> None:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO history_tokens(token, admin_id, group_id, expires_at, used_at)
            VALUES(:token, :admin_id, :group_id, SYSTIMESTAMP + NUMTODSINTERVAL(:ttl_minutes, 'MINUTE'), NULL)
            """,
            {
                "token": token,
                "admin_id": admin_id,
                "group_id": group_id,
                "ttl_minutes": ttl_minutes,
            },
        )
        conn.commit()


def validate_history_token(db: Oracle, *, token: str, group_id: str) -> bool:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT 1
            FROM history_tokens
            WHERE token = :token
              AND group_id = :group_id
              AND used_at IS NULL
              AND expires_at > SYSTIMESTAMP
            """,
            {"token": token, "group_id": group_id},
        )
        return cur.fetchone() is not None


def mark_history_token_used(db: Oracle, *, token: str) -> None:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE history_tokens
            SET used_at = SYSTIMESTAMP
            WHERE token = :token
            """,
            {"token": token},
        )
        conn.commit()


@dataclass(frozen=True)
class Job:
    job_id: int
    type: str
    payload: Dict[str, Any]
    attempts: int


def claim_next_job(db: Oracle, *, allowed_types: List[str]) -> Optional[Job]:
    if not allowed_types:
        raise ValueError("allowed_types must be non-empty")

    type_binds = ", ".join(f":t{i}" for i in range(len(allowed_types)))
    params: Dict[str, Any] = {f"t{i}": t for i, t in enumerate(allowed_types)}

    with db.connection() as conn:
        cur = conn.cursor()
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
        cur.execute(
            """
            UPDATE jobs
            SET status = 'in_progress', updated_at = SYSTIMESTAMP
            WHERE job_id = :job_id
            """,
            {"job_id": job_id},
        )
        conn.commit()

        payload_raw = row[2] or "{}"
        payload = json.loads(payload_raw)
        return Job(job_id=job_id, type=row[1], payload=payload, attempts=int(row[3]))


def complete_job(db: Oracle, *, job_id: int) -> None:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE jobs SET status='done', updated_at=SYSTIMESTAMP WHERE job_id=:job_id",
            {"job_id": job_id},
        )
        conn.commit()


def fail_job(db: Oracle, *, job_id: int, attempts: int, max_attempts: int = 3) -> None:
    status = "pending" if attempts + 1 < max_attempts else "failed"
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE jobs
            SET status=:status, attempts=:attempts, updated_at=SYSTIMESTAMP
            WHERE job_id=:job_id
            """,
            {"status": status, "attempts": attempts + 1, "job_id": job_id},
        )
        conn.commit()


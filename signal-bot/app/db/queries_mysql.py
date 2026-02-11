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
    image_paths: List[str]
    reply_to_id: str | None


def _parse_json_list(raw: str | None) -> List[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data if str(x)]


def insert_raw_message(db: MySQL, msg: RawMessage) -> None:
    with db.connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO raw_messages(message_id, group_id, ts, sender_hash, content_text, image_paths_json, reply_to_id)
                VALUES(%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    msg.message_id,
                    msg.group_id,
                    msg.ts,
                    msg.sender_hash,
                    msg.content_text,
                    json.dumps(msg.image_paths, ensure_ascii=False),
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
            SELECT message_id, group_id, ts, sender_hash, content_text, image_paths_json, reply_to_id
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
            image_paths=_parse_json_list(row[5]),
            reply_to_id=row[6],
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
    evidence_image_paths: List[str],
) -> None:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO cases(
              case_id, group_id, status, problem_title, problem_summary, solution_summary, tags_json, evidence_image_paths_json
            )
            VALUES(%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                case_id,
                group_id,
                status,
                problem_title,
                problem_summary,
                solution_summary,
                json.dumps(tags, ensure_ascii=False),
                json.dumps(evidence_image_paths, ensure_ascii=False),
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


# ─────────────────────────────────────────────────────────────────────────────
# Admin session management
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AdminSession:
    admin_id: str
    state: str  # 'awaiting_group_name', 'awaiting_qr_scan', 'idle'
    pending_group_id: str | None
    pending_group_name: str | None
    pending_token: str | None
    lang: str = "uk"  # 'uk' or 'en'


def get_admin_session(db: MySQL, admin_id: str) -> Optional[AdminSession]:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT admin_id, state, pending_group_id, pending_group_name, pending_token, lang
            FROM admin_sessions
            WHERE admin_id = %s
            """,
            (admin_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return AdminSession(
            admin_id=row[0],
            state=row[1],
            pending_group_id=row[2],
            pending_group_name=row[3],
            pending_token=row[4],
            lang=row[5] if row[5] else "uk",
        )


def upsert_admin_session(
    db: MySQL,
    *,
    admin_id: str,
    state: str,
    pending_group_id: str | None = None,
    pending_group_name: str | None = None,
    pending_token: str | None = None,
) -> None:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO admin_sessions (admin_id, state, pending_group_id, pending_group_name, pending_token)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                state = VALUES(state),
                pending_group_id = VALUES(pending_group_id),
                pending_group_name = VALUES(pending_group_name),
                pending_token = VALUES(pending_token)
            """,
            (admin_id, state, pending_group_id, pending_group_name, pending_token),
        )
        conn.commit()


def set_admin_awaiting_group_name(db: MySQL, admin_id: str) -> None:
    """Reset admin to awaiting_group_name state."""
    upsert_admin_session(
        db,
        admin_id=admin_id,
        state="awaiting_group_name",
        pending_group_id=None,
        pending_group_name=None,
        pending_token=None,
    )


def set_admin_awaiting_qr_scan(
    db: MySQL,
    *,
    admin_id: str,
    group_id: str,
    group_name: str,
    token: str,
) -> None:
    """Set admin to awaiting_qr_scan state with pending group info."""
    upsert_admin_session(
        db,
        admin_id=admin_id,
        state="awaiting_qr_scan",
        pending_group_id=group_id,
        pending_group_name=group_name,
        pending_token=token,
    )


def get_admin_by_token(db: MySQL, token: str) -> Optional[AdminSession]:
    """Find admin session by pending token."""
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT admin_id, state, pending_group_id, pending_group_name, pending_token, lang
            FROM admin_sessions
            WHERE pending_token = %s AND state = 'awaiting_qr_scan'
            """,
            (token,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return AdminSession(
            admin_id=row[0],
            state=row[1],
            pending_group_id=row[2],
            pending_group_name=row[3],
            pending_token=row[4],
            lang=row[5] if row[5] else "uk",
        )


def set_admin_lang(db: MySQL, admin_id: str, lang: str) -> None:
    """Set admin's language preference."""
    if lang not in ("uk", "en"):
        lang = "uk"
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE admin_sessions SET lang = %s WHERE admin_id = %s
            """,
            (lang, admin_id),
        )
        conn.commit()


def cancel_pending_history_jobs(db: MySQL, token: str) -> int:
    """Cancel any pending HISTORY_LINK jobs with the given token.
    
    Returns the number of jobs cancelled.
    """
    with db.connection() as conn:
        cur = conn.cursor()
        # Mark as cancelled any pending jobs that have this token in payload
        cur.execute(
            """
            UPDATE jobs 
            SET status = 'cancelled'
            WHERE type = 'HISTORY_LINK' 
              AND status = 'pending'
              AND payload_json LIKE %s
            """,
            (f'%"token":"{token}"%',),
        )
        cancelled = cur.rowcount
        conn.commit()
        return cancelled


def link_admin_to_group(db: MySQL, *, admin_id: str, group_id: str) -> None:
    """Record that an admin has connected a group."""
    with db.connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO admins_groups (admin_id, group_id)
                VALUES (%s, %s)
                """,
                (admin_id, group_id),
            )
            conn.commit()
        except Exception as exc:
            if is_mysql_error(exc, MYSQL_ERR_DUP_ENTRY):
                conn.rollback()
                return
            conn.rollback()
            raise


# ─────────────────────────────────────────────────────────────────────────────
# Reactions
# ─────────────────────────────────────────────────────────────────────────────

# Positive emoji that indicate problem resolution / approval
POSITIVE_EMOJI = frozenset([
    "\U0001F44D",  # thumbs up
    "\U0001F44F",  # clapping hands
    "\u2705",      # check mark
    "\U0001F389",  # party popper
    "\U0001F64F",  # folded hands (thank you)
    "\u2764\ufe0f", # red heart
    "\U0001F499",  # blue heart
    "\U0001F49A",  # green heart
    "\U0001F31F",  # star
    "\U0001F4AF",  # 100
])


def upsert_reaction(
    db: MySQL,
    *,
    group_id: str,
    target_ts: int,
    target_author: str,
    sender_hash: str,
    emoji: str,
) -> None:
    """Insert a reaction (ignore if duplicate)."""
    with db.connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO reactions (group_id, target_ts, target_author, sender_hash, emoji)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (group_id, target_ts, target_author, sender_hash, emoji),
            )
            conn.commit()
        except Exception as exc:
            if is_mysql_error(exc, MYSQL_ERR_DUP_ENTRY):
                conn.rollback()
                return
            conn.rollback()
            raise


def delete_reaction(
    db: MySQL,
    *,
    group_id: str,
    target_ts: int,
    sender_hash: str,
    emoji: str,
) -> None:
    """Remove a reaction."""
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM reactions
            WHERE group_id = %s AND target_ts = %s AND sender_hash = %s AND emoji = %s
            """,
            (group_id, target_ts, sender_hash, emoji),
        )
        conn.commit()


def get_positive_reactions_for_message(db: MySQL, *, group_id: str, target_ts: int) -> int:
    """Count positive emoji reactions on a message."""
    placeholders = ", ".join(["%s"] * len(POSITIVE_EMOJI))
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM reactions
            WHERE group_id = %s AND target_ts = %s AND emoji IN ({placeholders})
            """,
            (group_id, target_ts, *POSITIVE_EMOJI),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0


def get_message_by_ts(db: MySQL, *, group_id: str, ts: int) -> Optional[RawMessage]:
    """Find a raw message by group_id and timestamp."""
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT message_id, group_id, ts, sender_hash, content_text, image_paths_json, reply_to_id
            FROM raw_messages
            WHERE group_id = %s AND ts = %s
            LIMIT 1
            """,
            (group_id, ts),
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
            image_paths=_parse_json_list(row[5]),
            reply_to_id=row[6],
        )

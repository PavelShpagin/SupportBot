from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
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
    sender_name: str | None = None


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


def insert_raw_message(db: MySQL, msg: RawMessage) -> bool:
    with db.connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO raw_messages(message_id, group_id, ts, sender_hash, sender_name, content_text, image_paths_json, reply_to_id)
                VALUES(%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    msg.message_id,
                    msg.group_id,
                    msg.ts,
                    msg.sender_hash,
                    msg.sender_name,
                    msg.content_text,
                    json.dumps(msg.image_paths, ensure_ascii=False),
                    msg.reply_to_id,
                ),
            )
            conn.commit()
            return True
        except Exception as exc:
            # Error 1062: Duplicate entry (duplicate message_id)
            if is_mysql_error(exc, MYSQL_ERR_DUP_ENTRY):
                conn.rollback()
                return False
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
            SELECT message_id, group_id, ts, sender_hash, sender_name, content_text, image_paths_json, reply_to_id
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
            sender_name=row[4],
            content_text=row[5] or "",
            image_paths=_parse_json_list(row[6]),
            reply_to_id=row[7],
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
    updated_at: datetime | None = None


def get_admin_session(db: MySQL, admin_id: str) -> Optional[AdminSession]:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT admin_id, state, pending_group_id, pending_group_name, pending_token, lang, updated_at
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
            updated_at=row[6] if len(row) > 6 else None,
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


def delete_admin_session(db: MySQL, admin_id: str) -> bool:
    """Delete admin session when user removes/blocks the bot.
    
    This allows them to get a fresh start if they re-add the bot.
    Returns True if a session was deleted.
    """
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM admin_sessions WHERE admin_id = %s
            """,
            (admin_id,),
        )
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted


def delete_admin_history_tokens(db: MySQL, admin_id: str) -> int:
    """Delete all history tokens for an admin (for compliance when they remove bot)."""
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM history_tokens WHERE admin_id = %s
            """,
            (admin_id,),
        )
        deleted = cur.rowcount
        conn.commit()
        return deleted


def cancel_pending_history_jobs(db: MySQL, token: str) -> int:
    """Cancel any pending or claimed HISTORY_LINK jobs with the given token.
    
    Returns the number of jobs cancelled.
    """
    with db.connection() as conn:
        cur = conn.cursor()
        # Mark as cancelled any pending OR claimed jobs that have this token in payload
        # We cancel claimed jobs too because the user wants to abort the current operation
        cur.execute(
            """
            UPDATE jobs 
            SET status = 'cancelled'
            WHERE type = 'HISTORY_LINK' 
              AND status IN ('pending', 'in_progress')
              AND payload_json LIKE %s
            """,
            (f'%"token":"{token}"%',),
        )
        cancelled = cur.rowcount
        conn.commit()
        return cancelled


def cancel_all_history_jobs_for_admin(db: MySQL, admin_id: str) -> int:
    """Cancel ALL pending, claimed, or in_progress HISTORY_LINK jobs for this admin.
    
    This is called when starting a new history link to ensure only one job runs at a time.
    Returns the number of jobs cancelled.
    """
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE jobs 
            SET status = 'cancelled'
            WHERE type = 'HISTORY_LINK' 
              AND status IN ('pending', 'in_progress')
              AND payload_json LIKE %s
            """,
            (f'%"admin_id":"{admin_id}"%',),
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


def get_group_admins(db: MySQL, group_id: str) -> List[str]:
    """Get list of admin IDs (phone numbers) for a group."""
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT admin_id FROM admins_groups WHERE group_id = %s",
            (group_id,),
        )
        rows = cur.fetchall()
        return [r[0] for r in rows]


def unlink_admin_from_all_groups(db: MySQL, admin_id: str) -> int:
    """Remove all group links for an admin. Returns number of removed links."""
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM admins_groups
            WHERE admin_id = %s
            """,
            (admin_id,),
        )
        removed = cur.rowcount
        conn.commit()
        return removed


def list_groups_with_linked_admins(db: MySQL) -> List[str]:
    """Return group_ids that have at least one linked admin."""
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT group_id FROM admins_groups")
        rows = cur.fetchall()
        return [str(r[0]) for r in rows if r and r[0]]


def unlink_all_admins_from_group(db: MySQL, group_id: str) -> int:
    """Remove all admin links for a group (e.g. when bot was removed from group). Returns count removed."""
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM admins_groups WHERE group_id = %s",
            (group_id,),
        )
        removed = cur.rowcount
        conn.commit()
        return removed


def archive_cases_for_group(db: MySQL, group_id: str) -> int:
    """
    Mark all active cases for a group as 'archived' instead of deleting them.
    Called during re-ingest so that old case links (already sent in Signal) remain
    accessible. Archived cases are excluded from RAG queries but still served by
    the web frontend with a soft 'archived' banner.
    Returns the number of cases archived.
    """
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE cases SET status = 'archived', in_rag = 0 "
            "WHERE group_id = %s AND status != 'archived'",
            (group_id,),
        )
        count = cur.rowcount
        conn.commit()
    return count


def clear_group_runtime_data(db: MySQL, group_id: str) -> None:
    """Delete transient per-group data before re-ingest.

    Removes raw messages, the conversation buffer, and reactions so re-ingest
    starts from a clean slate. Does NOT touch cases (use archive_cases_for_group
    for that) or admin/group config.
    """
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM raw_messages WHERE group_id = %s", (group_id,))
        cur.execute("DELETE FROM buffers WHERE group_id = %s", (group_id,))
        cur.execute("DELETE FROM reactions WHERE group_id = %s", (group_id,))
        conn.commit()


def delete_all_group_data(db: MySQL, group_id: str) -> dict:
    """
    Delete ALL data associated with a group for compliance.
    Called when the bot is removed from a group (GDPR hard-delete).
    For re-ingest use archive_cases_for_group() instead so old links stay valid.

    Returns dict with counts of deleted items.
    """
    stats = {
        "cases": 0,
        "case_evidence": 0,
        "raw_messages": 0,
        "buffer": 0,
        "group_docs": 0,
        "reactions": 0,
        "jobs": 0,
    }
    
    with db.connection() as conn:
        cur = conn.cursor()
        
        # 1. Get all case_ids for this group (needed for case_evidence cleanup)
        cur.execute("SELECT case_id FROM cases WHERE group_id = %s", (group_id,))
        case_ids = [r[0] for r in cur.fetchall()]
        
        # 2. Delete case_evidence for all cases in this group
        if case_ids:
            placeholders = ",".join(["%s"] * len(case_ids))
            cur.execute(f"DELETE FROM case_evidence WHERE case_id IN ({placeholders})", case_ids)
            stats["case_evidence"] = cur.rowcount
        
        # 3. Delete cases
        cur.execute("DELETE FROM cases WHERE group_id = %s", (group_id,))
        stats["cases"] = cur.rowcount
        
        # 4. Delete raw_messages
        cur.execute("DELETE FROM raw_messages WHERE group_id = %s", (group_id,))
        stats["raw_messages"] = cur.rowcount
        
        # 5. Delete buffer
        cur.execute("DELETE FROM buffers WHERE group_id = %s", (group_id,))
        stats["buffer"] = cur.rowcount
        
        # 6. Delete group config (docs URLs)
        cur.execute("DELETE FROM chat_groups WHERE group_id = %s", (group_id,))
        stats["group_docs"] = cur.rowcount
        
        # 7. Delete reactions for this group
        cur.execute("DELETE FROM reactions WHERE group_id = %s", (group_id,))
        stats["reactions"] = cur.rowcount
        
        # 8. Delete pending jobs for this group
        cur.execute(
            "DELETE FROM jobs WHERE status = 'pending' AND payload_json LIKE %s",
            (f'%"{group_id}"%',)
        )
        stats["jobs"] = cur.rowcount
        
        conn.commit()
    
    return stats


def get_case_ids_for_group(db: MySQL, group_id: str) -> List[str]:
    """Get all case IDs for a group (for RAG cleanup)."""
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT case_id FROM cases WHERE group_id = %s", (group_id,))
        return [r[0] for r in cur.fetchall()]


def list_known_admin_ids(db: MySQL) -> List[str]:
    """Return all admin IDs seen in sessions or admin-group links."""
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT admin_id FROM admin_sessions
            UNION
            SELECT admin_id FROM admins_groups
            """
        )
        rows = cur.fetchall()
        return [str(r[0]) for r in rows if r and r[0]]


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
            SELECT message_id, group_id, ts, sender_hash, sender_name, content_text, image_paths_json, reply_to_id
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
            sender_name=row[4],
            content_text=row[5] or "",
            image_paths=_parse_json_list(row[6]),
            reply_to_id=row[7],
        )


def get_case(db: MySQL, case_id: str) -> Optional[Dict[str, Any]]:
    """Get case details by ID."""
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT case_id, group_id, status, problem_title, problem_summary, solution_summary, tags_json, evidence_image_paths_json, created_at, closed_emoji
            FROM cases
            WHERE case_id = %s
            """,
            (case_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "case_id": row[0],
            "group_id": row[1],
            "status": row[2],
            "problem_title": row[3],
            "problem_summary": row[4],
            "solution_summary": row[5],
            "tags": _parse_json_list(row[6]),
            "evidence_image_paths": _parse_json_list(row[7]),
            "created_at": row[8].isoformat() if row[8] else None,
            "closed_emoji": row[9],
        }


def close_case_by_message_ts(
    db: MySQL,
    *,
    group_id: str,
    target_ts: int,
    emoji: str,
) -> Optional[str]:
    """Close an open case associated with a message (by timestamp).

    Looks up the raw_message at (group_id, ts=target_ts), finds any case linked
    via case_evidence, marks it solved with the given emoji.  Returns case_id or None.
    """
    with db.connection() as conn:
        cur = conn.cursor()
        # Find the message_id for this timestamp in this group
        cur.execute(
            "SELECT message_id FROM raw_messages WHERE group_id = %s AND ts = %s LIMIT 1",
            (group_id, target_ts),
        )
        row = cur.fetchone()
        if not row:
            return None
        message_id = row[0]

        # Find a case linked to this message
        cur.execute(
            """
            SELECT c.case_id FROM cases c
            JOIN case_evidence ce ON c.case_id = ce.case_id
            WHERE ce.message_id = %s AND c.status = 'open'
            LIMIT 1
            """,
            (message_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        case_id = row[0]

        cur.execute(
            "UPDATE cases SET status = 'solved', closed_emoji = %s, updated_at = NOW() WHERE case_id = %s",
            (emoji, case_id),
        )
        conn.commit()
        return case_id


def get_case_evidence(db: MySQL, case_id: str) -> List[RawMessage]:
    """Get all messages associated with a case."""
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT rm.message_id, rm.group_id, rm.ts, rm.sender_hash, rm.sender_name, rm.content_text, rm.image_paths_json, rm.reply_to_id
            FROM raw_messages rm
            JOIN case_evidence ce ON rm.message_id = ce.message_id
            WHERE ce.case_id = %s
            ORDER BY rm.ts ASC
            """,
            (case_id,),
        )
        rows = cur.fetchall()
        return [
            RawMessage(
                message_id=r[0],
                group_id=r[1],
                ts=int(r[2]),
                sender_hash=r[3],
                sender_name=r[4],
                content_text=r[5] or "",
                image_paths=_parse_json_list(r[6]),
                reply_to_id=r[7],
            )
            for r in rows
        ]


# ─────────────────────────────────────────────────────────────────────────────
# B1 / B3 / SCRAG Pipeline Queries
# ─────────────────────────────────────────────────────────────────────────────

def get_open_cases_for_group(db: MySQL, group_id: str) -> List[Dict[str, Any]]:
    """Return all open (B1) cases for a group, ordered newest first."""
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT case_id, problem_title, problem_summary, solution_summary, tags_json, created_at
            FROM cases
            WHERE group_id = %s AND status = 'open'
            ORDER BY created_at DESC
            """,
            (group_id,),
        )
        rows = cur.fetchall()
        return [
            {
                "case_id": row[0],
                "problem_title": row[1],
                "problem_summary": row[2],
                "solution_summary": row[3] or "",
                "tags": _parse_json_list(row[4]),
                "created_at": row[5].isoformat() if row[5] else None,
            }
            for row in rows
        ]


def get_recent_solved_cases(db: MySQL, group_id: str, since_ts_ms: int) -> List[Dict[str, Any]]:
    """Return solved (B3) cases whose evidence falls within the B2 window.

    since_ts_ms is the oldest message timestamp (ms) still in the rolling buffer.
    Cases are included when at least one of their evidence messages is >= since_ts_ms.
    """
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT c.case_id, c.problem_title, c.problem_summary, c.solution_summary, c.tags_json, c.created_at
            FROM cases c
            JOIN case_evidence ce ON c.case_id = ce.case_id
            JOIN raw_messages rm ON ce.message_id = rm.message_id
            WHERE c.group_id = %s
              AND c.status = 'solved'
              AND rm.ts >= %s
            ORDER BY c.created_at DESC
            LIMIT 10
            """,
            (group_id, since_ts_ms),
        )
        rows = cur.fetchall()
        return [
            {
                "case_id": row[0],
                "problem_title": row[1],
                "problem_summary": row[2],
                "solution_summary": row[3] or "",
                "tags": _parse_json_list(row[4]),
                "created_at": row[5].isoformat() if row[5] else None,
            }
            for row in rows
        ]


def update_case_to_solved(db: MySQL, case_id: str, solution_summary: str) -> None:
    """Promote a B1 open case to solved. Caller is responsible for RAG indexing."""
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE cases SET status = 'solved', solution_summary = %s, updated_at = NOW()
            WHERE case_id = %s
            """,
            (solution_summary, case_id),
        )
        conn.commit()


def mark_case_in_rag(db: MySQL, case_id: str) -> None:
    """Mark that a case has been indexed in ChromaDB (SCRAG)."""
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE cases SET in_rag = 1, updated_at = NOW() WHERE case_id = %s",
            (case_id,),
        )
        conn.commit()


def expire_old_open_cases(db: MySQL, max_age_days: int = 7) -> List[str]:
    """Delete open B1 cases older than max_age_days. Returns list of deleted case_ids."""
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT case_id FROM cases
            WHERE status = 'open'
              AND in_rag = 0
              AND created_at < NOW() - INTERVAL %s DAY
            """,
            (max_age_days,),
        )
        rows = cur.fetchall()
        if not rows:
            return []
        expired_ids = [r[0] for r in rows]
        placeholders = ", ".join(["%s"] * len(expired_ids))
        cur.execute(f"DELETE FROM case_evidence WHERE case_id IN ({placeholders})", expired_ids)
        cur.execute(f"DELETE FROM cases WHERE case_id IN ({placeholders})", expired_ids)
        conn.commit()
        return expired_ids


# ─────────────────────────────────────────────────────────────────────────────
# Group Configuration (Docs)
# ─────────────────────────────────────────────────────────────────────────────

def upsert_group_docs(db: MySQL, group_id: str, docs_urls: List[str]) -> None:
    """Set documentation URLs for a group."""
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO chat_groups (group_id, docs_urls)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE
                docs_urls = VALUES(docs_urls)
            """,
            (group_id, json.dumps(docs_urls, ensure_ascii=False)),
        )
        conn.commit()


def get_group_docs(db: MySQL, group_id: str) -> List[str]:
    """Get documentation URLs for a group. Returns empty list if not set."""
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT docs_urls FROM chat_groups WHERE group_id = %s",
            (group_id,),
        )
        row = cur.fetchone()
        if not row or not row[0]:
            return []
        return _parse_json_list(row[0])


def get_all_active_case_ids(db: MySQL) -> List[str]:
    """Return all non-archived case_ids across all groups.

    Used by the SYNC_RAG job to reconcile ChromaDB against MySQL: any case_id
    present in Chroma but absent from this list should be deleted from Chroma.
    """
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT case_id FROM cases WHERE status != 'archived'")
        return [r[0] for r in cur.fetchall()]


def wipe_all_data(db: MySQL) -> dict:
    """
    Wipe ALL persistent bot data for a clean slate.
    Keeps signal-cli registration (phone number account).
    Returns counts of deleted rows per table.
    """
    tables = [
        "case_evidence",
        "cases",
        "raw_messages",
        "buffers",
        "reactions",
        "admins_groups",
        "history_tokens",
        "admin_sessions",
        "chat_groups",
        "jobs",
    ]
    stats: dict = {}
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for table in tables:
            try:
                cur.execute(f"DELETE FROM {table}")
                stats[table] = cur.rowcount
            except Exception as exc:
                stats[table] = f"error: {exc}"
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
        conn.commit()
    return stats

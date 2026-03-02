"""
Read messages from Signal Desktop's SQLCipher database.

Signal Desktop stores its encryption key in a JSON config file.
The DB is at: ~/.config/Signal/sql/db.sqlite
The key is at: ~/.config/Signal/config.json (field: "key")

Signal Desktop 7+ stores attachments in a separate `message_attachments`
table instead of embedding them in the `json` column of the `messages` table.
This module supports both schemas transparently.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SignalMessage:
    """A message from the Signal Desktop database."""
    id: str
    conversation_id: str
    timestamp: int  # ms since epoch
    sender: Optional[str]
    body: str
    type: str  # 'incoming', 'outgoing', etc.
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    reactions: int = 0  # count of emoji reactions on this message
    reaction_emoji: Optional[str] = None  # first/representative reaction emoji (if known)
    sender_name: Optional[str] = None  # display name from contacts
    # Attachment metadata parsed from the json column or message_attachments table.
    # Each entry is a dict with keys: path (relative to Signal data dir),
    # fileName, contentType, cdnKey, cdnNumber, key, digest, size.
    attachments: list = None  # list[dict]

    def __post_init__(self):
        if self.attachments is None:
            object.__setattr__(self, "attachments", [])


def _get_db_key(signal_data_dir: str) -> str:
    """Extract the SQLCipher key from Signal Desktop's config.json."""
    import os

    config_path = Path(signal_data_dir) / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
        key = config.get("key")
        if key:
            return key
        if config.get("encryptedKey"):
            log.info(
                "config.json uses encryptedKey (OS-keychain protected). "
                "Falling back to SIGNAL_DESKTOP_KEY_HEX env var."
            )

    env_key = os.environ.get("SIGNAL_DESKTOP_KEY_HEX", "").strip()
    if env_key:
        return env_key

    raise RuntimeError(
        "Cannot read Signal Desktop DB key. "
        "config.json has no plain 'key' field and SIGNAL_DESKTOP_KEY_HEX is not set. "
        "Set SIGNAL_DESKTOP_KEY_HEX to the raw hex key from the OS keychain."
    )


def _open_db(signal_data_dir: str):
    """Open the Signal Desktop SQLCipher database."""
    sqlcipher = None
    try:
        import sqlcipher3 as sqlcipher
        log.info("Using sqlcipher3 (sqlcipher3-binary)")
    except ImportError:
        try:
            from pysqlcipher3 import dbapi2 as sqlcipher
            log.info("Using pysqlcipher3")
        except ImportError:
            raise RuntimeError("No SQLCipher library found. Install sqlcipher3-binary or pysqlcipher3")
    
    db_path = Path(signal_data_dir) / "sql" / "db.sqlite"
    if not db_path.exists():
        raise RuntimeError(f"Signal Desktop DB not found: {db_path}")
    
    key = _get_db_key(signal_data_dir)
    
    conn = sqlcipher.connect(str(db_path))
    try:
        conn.execute("PRAGMA cipher_compatibility = 4;")
        conn.execute("PRAGMA cipher_page_size = 4096;")
        conn.execute(f"PRAGMA key = \"x'{key}'\";")
        
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1;").fetchall()
        log.info("DB opened successfully, found tables")
        return conn
    except Exception as e:
        conn.close()
        raise RuntimeError(f"Failed to open Signal DB: {e}")


def _get_table_columns(conn, table: str) -> set[str]:
    """Get column names for a table."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _get_all_tables(conn) -> set[str]:
    """Get all table names in the database."""
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def get_conversations(signal_data_dir: str) -> list[dict]:
    """Get all conversations from Signal Desktop DB."""
    conn = _open_db(signal_data_dir)
    try:
        cols = _get_table_columns(conn, "conversations")
        
        select_cols = ["id"]
        if "type" in cols:
            select_cols.append("type")
        if "name" in cols:
            select_cols.append("name")
        if "profileName" in cols:
            select_cols.append("profileName")
        if "groupId" in cols:
            select_cols.append("groupId")
        if "e164" in cols:
            select_cols.append("e164")
        if "uuid" in cols:
            select_cols.append("uuid")
        
        q = f"SELECT {', '.join(select_cols)} FROM conversations"
        rows = conn.execute(q).fetchall()
        
        result = []
        for row in rows:
            d = {}
            for i, col in enumerate(select_cols):
                d[col] = row[i]
            result.append(d)
        
        return result
    finally:
        conn.close()


def get_contacts_from_db(signal_data_dir: str) -> set[str]:
    """Get contact identifiers (e164, uuid, id) from private conversations. No DevTools needed."""
    result = set()
    try:
        convs = get_conversations(signal_data_dir)
        for c in convs:
            if c.get("type") != "private":
                continue
            for key in ("e164", "uuid", "id"):
                val = c.get(key)
                if val and isinstance(val, str) and len(val) > 2:
                    result.add(val)
        return result
    except Exception as e:
        log.warning("Failed to get contacts from DB: %s", e)
        return set()


def _load_attachments_from_table(
    conn,
    tables: set[str],
    conversation_id: Optional[str] = None,
    message_ids: Optional[list[str]] = None,
) -> dict[str, list[dict]]:
    """Load attachments from the separate message_attachments table (Signal Desktop 7+).

    Returns a dict mapping messageId -> list of attachment dicts in the
    normalised format used by the rest of the pipeline.
    """
    if "message_attachments" not in tables:
        return {}

    ma_cols = _get_table_columns(conn, "message_attachments")
    cdn_key_col = "transitCdnKey" if "transitCdnKey" in ma_cols else "cdnKey"
    cdn_num_col = "transitCdnNumber" if "transitCdnNumber" in ma_cols else "cdnNumber"

    select_cols = [
        "messageId",
        "contentType" if "contentType" in ma_cols else "'' as contentType",
        "path" if "path" in ma_cols else "'' as path",
        "fileName" if "fileName" in ma_cols else "'' as fileName",
        f"{cdn_key_col} as cdnKey",
        f"{cdn_num_col} as cdnNumber",
        "key" if "key" in ma_cols else "'' as key",
        "digest" if "digest" in ma_cols else "'' as digest",
        "size" if "size" in ma_cols else "0 as size",
        "downloadPath" if "downloadPath" in ma_cols else "'' as downloadPath",
    ]

    q = f"SELECT {', '.join(select_cols)} FROM message_attachments"
    params: list = []
    conditions: list[str] = []

    if "attachmentType" in ma_cols:
        conditions.append("(attachmentType = 'standard' OR attachmentType = 'long-message')")

    if conversation_id and "conversationId" in ma_cols:
        conditions.append("conversationId = ?")
        params.append(conversation_id)

    if message_ids:
        placeholders = ",".join("?" for _ in message_ids)
        conditions.append(f"messageId IN ({placeholders})")
        params.extend(message_ids)

    if conditions:
        q += " WHERE " + " AND ".join(conditions)

    if "orderInMessage" in ma_cols:
        q += " ORDER BY orderInMessage ASC"

    rows = conn.execute(q, params).fetchall()
    result: dict[str, list[dict]] = {}
    for r in rows:
        msg_id = str(r[0])
        content_type = str(r[1] or "")
        cdn_key = str(r[4] or "")
        path = str(r[2] or "")
        download_path = str(r[9] or "")
        if not content_type and not cdn_key:
            continue
        att = {
            "path": path or download_path,
            "fileName": str(r[3] or ""),
            "contentType": content_type,
            "cdnKey": cdn_key,
            "cdnNumber": r[5],
            "key": str(r[6] or ""),
            "digest": str(r[7] or ""),
            "size": r[8] or 0,
        }
        result.setdefault(msg_id, []).append(att)

    log.info(
        "Loaded %d attachments for %d messages from message_attachments table",
        sum(len(v) for v in result.values()),
        len(result),
    )
    return result


def get_messages(
    signal_data_dir: str,
    conversation_id: Optional[str] = None,
    since_timestamp: Optional[int] = None,
    limit: int = 100,
) -> list[SignalMessage]:
    """Get messages from Signal Desktop DB.
    
    Supports both legacy (json column) and modern (message_attachments table)
    schemas for attachment data.
    """
    conn = _open_db(signal_data_dir)
    try:
        msg_cols = _get_table_columns(conn, "messages")
        conv_cols = _get_table_columns(conn, "conversations")
        tables = _get_all_tables(conn)

        has_att_table = "message_attachments" in tables
        has_att_flag = "hasAttachments" in msg_cols
        has_json_col = "json" in msg_cols
        
        ts_col = "sent_at" if "sent_at" in msg_cols else "timestamp"
        conv_id_col = "conversationId" if "conversationId" in msg_cols else "conversation_id"
        body_col = "body" if "body" in msg_cols else "message"
        type_col = "type" if "type" in msg_cols else None
        
        sender_col = None
        for candidate in ["sourceServiceId", "sourceUuid", "source"]:
            if candidate in msg_cols:
                sender_col = candidate
                break
        
        select_parts = [
            "m.id",
            f"m.{conv_id_col} as conversation_id",
            f"m.{ts_col} as timestamp",
            f"m.{body_col} as body",
        ]
        
        if sender_col:
            select_parts.append(f"m.{sender_col} as sender")
        else:
            select_parts.append("NULL as sender")
        
        if type_col:
            select_parts.append(f"m.{type_col} as msg_type")
        else:
            select_parts.append("'unknown' as msg_type")
        
        if "groupId" in conv_cols:
            select_parts.append("c.groupId as group_id")
        else:
            select_parts.append("NULL as group_id")
        
        if "name" in conv_cols:
            select_parts.append("c.name as group_name")
        else:
            select_parts.append("NULL as group_name")
        
        if sender_col and "profileName" in conv_cols:
            select_parts.append("COALESCE(sc.name, sc.profileName, sc.profileFullName) as sender_name")
        else:
            select_parts.append("NULL as sender_name")

        if has_json_col:
            select_parts.append("m.json as raw_json")

        sender_join = ""
        if sender_col:
            sender_join = f"LEFT JOIN conversations sc ON m.{sender_col} = sc.id"

        # Include messages with text body OR with attachments
        body_conditions = [f"(m.{body_col} IS NOT NULL AND m.{body_col} != '')"]

        if has_att_flag:
            body_conditions.append("(m.hasAttachments = 1)")
        if has_json_col:
            body_conditions.append(
                "(m.json LIKE '%\"attachments\":[%' AND m.json NOT LIKE '%\"attachments\":[]%')"
            )

        body_cond = " OR ".join(body_conditions)

        q = f"""
            SELECT {', '.join(select_parts)}
            FROM messages m
            LEFT JOIN conversations c ON m.{conv_id_col} = c.id
            {sender_join}
            WHERE ({body_cond})
        """
        
        params: list = []
        
        if conversation_id:
            q += f" AND m.{conv_id_col} = ?"
            params.append(conversation_id)
        
        if since_timestamp:
            q += f" AND m.{ts_col} > ?"
            params.append(since_timestamp)
        
        q += f" ORDER BY m.{ts_col} ASC LIMIT ?"
        params.append(limit)
        
        rows = conn.execute(q, params).fetchall()

        # Pre-load attachments from the separate table (Signal Desktop 7+)
        att_from_table: dict[str, list[dict]] = {}
        if has_att_table and rows:
            msg_ids = [str(r[0]) for r in rows]
            att_from_table = _load_attachments_from_table(
                conn, tables, conversation_id=conversation_id, message_ids=msg_ids,
            )

        # Build reactions maps
        reactions_by_ts: dict[int, int] = {}
        emoji_by_ts: dict[int, str] = {}
        try:
            if "reactions" in tables:
                rxn_cols = _get_table_columns(conn, "reactions")
                log.info("Reactions table columns: %s", rxn_cols)
                if "targetTimestamp" in rxn_cols:
                    rxn_where = ""
                    rxn_params: list = []
                    if conversation_id and "conversationId" in rxn_cols:
                        rxn_where = "WHERE conversationId = ?"
                        rxn_params = [conversation_id]
                    has_emoji_col = "emoji" in rxn_cols
                    emoji_sel = ", MIN(emoji)" if has_emoji_col else ""
                    rxn_rows = conn.execute(
                        f"SELECT targetTimestamp, COUNT(*){emoji_sel} FROM reactions {rxn_where} GROUP BY targetTimestamp",
                        rxn_params,
                    ).fetchall()
                    for r in rxn_rows:
                        if r[0] is None:
                            continue
                        ts_val = int(r[0])
                        reactions_by_ts[ts_val] = int(r[1])
                        if has_emoji_col and r[2]:
                            emoji_by_ts[ts_val] = str(r[2])
                    log.info("Found %d messages with reactions (from reactions table)", len(reactions_by_ts))
            
            if not reactions_by_ts and has_json_col:
                log.info("Trying to extract reactions from messages.json column")
                json_q = f"SELECT {ts_col}, json FROM messages WHERE json LIKE '%reactions%'"
                if conversation_id:
                    json_q += f" AND {conv_id_col} = ?"
                    json_rows = conn.execute(json_q, [conversation_id]).fetchall()
                else:
                    json_rows = conn.execute(json_q).fetchall()
                
                for row in json_rows:
                    try:
                        ts_val = int(row[0]) if row[0] else 0
                        msg_json = json.loads(row[1]) if row[1] else {}
                        rxns = msg_json.get("reactions", [])
                        if rxns and isinstance(rxns, list):
                            reactions_by_ts[ts_val] = len(rxns)
                            first_emoji = rxns[0].get("emoji") if isinstance(rxns[0], dict) else None
                            if first_emoji:
                                emoji_by_ts[ts_val] = str(first_emoji)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue
                
                if reactions_by_ts:
                    log.info("Found %d messages with reactions (from json column)", len(reactions_by_ts))
            
            if not reactions_by_ts:
                log.info("No reactions found in Signal Desktop DB")
                
        except Exception as e:
            log.warning("Could not fetch reactions: %s", e)

        result = []
        for row in rows:
            try:
                msg_id = str(row[0])
                ts = int(row[2]) if row[2] else 0
                body = str(row[3] or "")
                sender_name = str(row[8]) if len(row) > 8 and row[8] else None
                raw_json_str = str(row[9]) if (has_json_col and len(row) > 9 and row[9]) else None

                # Prefer message_attachments table (v7+), fall back to JSON
                attachments: list = att_from_table.get(msg_id, [])

                if not attachments and raw_json_str:
                    try:
                        msg_json = json.loads(raw_json_str)

                        def _parse_att(att: dict) -> Optional[dict]:
                            if not isinstance(att, dict):
                                return None
                            content_type = att.get("contentType") or ""
                            # Signal Desktop 7+ may use transitCdnKey instead of cdnKey
                            cdn_key = att.get("cdnKey") or att.get("transitCdnKey") or ""
                            cdn_number = att.get("cdnNumber") or att.get("transitCdnNumber")
                            path = att.get("path") or att.get("downloadPath") or ""
                            if not content_type and not cdn_key and not path:
                                return None
                            return {
                                "path": path,
                                "fileName": att.get("fileName") or "",
                                "contentType": content_type,
                                "cdnKey": cdn_key,
                                "cdnNumber": cdn_number,
                                "key": att.get("key") or "",
                                "digest": att.get("digest") or "",
                                "size": att.get("size") or 0,
                            }

                        raw_atts = msg_json.get("attachments") or []
                        for att in raw_atts:
                            parsed = _parse_att(att)
                            if parsed:
                                attachments.append(parsed)

                        if raw_atts and not attachments:
                            log.warning(
                                "Message %s has %d raw attachment(s) in JSON but all filtered out. "
                                "Sample keys: %s",
                                msg_id, len(raw_atts),
                                [list(a.keys())[:8] if isinstance(a, dict) else type(a).__name__
                                 for a in raw_atts[:2]],
                            )

                        quote = msg_json.get("quote") or {}
                        for q_att in quote.get("attachments") or []:
                            if not isinstance(q_att, dict):
                                continue
                            thumb = q_att.get("thumbnail")
                            if isinstance(thumb, dict) and (thumb.get("path") or thumb.get("cdnKey")):
                                parsed = _parse_att({**thumb, "contentType": thumb.get("contentType") or q_att.get("contentType") or ""})
                                if parsed:
                                    parsed["_source"] = "quote_thumbnail"
                                    attachments.append(parsed)

                    except (json.JSONDecodeError, TypeError):
                        pass

                if not body and not attachments:
                    continue

                msg = SignalMessage(
                    id=msg_id,
                    conversation_id=str(row[1]) if row[1] else "",
                    timestamp=ts,
                    body=body,
                    sender=str(row[4]) if row[4] else None,
                    type=str(row[5]) if row[5] else "unknown",
                    group_id=str(row[6]) if row[6] else None,
                    group_name=str(row[7]) if row[7] else None,
                    reactions=reactions_by_ts.get(ts, 0),
                    reaction_emoji=emoji_by_ts.get(ts),
                    sender_name=sender_name,
                    attachments=attachments,
                )
                result.append(msg)
            except Exception as e:
                log.warning("Failed to parse message row: %s", e)
                continue
        
        return result
    finally:
        conn.close()


def get_group_messages(
    signal_data_dir: str,
    group_id: Optional[str] = None,
    group_name: Optional[str] = None,
    limit: int = 800,
) -> list[SignalMessage]:
    """Get messages for a specific group by ID or name."""
    conn = _open_db(signal_data_dir)
    try:
        conv_cols = _get_table_columns(conn, "conversations")
        
        conv_id = None
        
        if group_id and "groupId" in conv_cols:
            row = conn.execute(
                "SELECT id FROM conversations WHERE groupId = ? LIMIT 1",
                (group_id,)
            ).fetchone()
            if row:
                conv_id = row[0]
        
        if not conv_id and group_name and "name" in conv_cols:
            row = conn.execute(
                "SELECT id FROM conversations WHERE LOWER(name) = LOWER(?) LIMIT 1",
                (group_name,)
            ).fetchone()
            if row:
                conv_id = row[0]
            else:
                row = conn.execute(
                    "SELECT id FROM conversations WHERE LOWER(name) LIKE LOWER(?) LIMIT 1",
                    (f"%{group_name}%",)
                ).fetchone()
                if row:
                    conv_id = row[0]
        
        if not conv_id:
            log.warning("Group not found: group_id=%s, group_name=%s", group_id, group_name)
            return []
        
        return get_messages(
            signal_data_dir=signal_data_dir,
            conversation_id=str(conv_id),
            limit=limit,
        )
    finally:
        conn.close()


def get_attachment_stats(
    signal_data_dir: str,
    conversation_id: Optional[str] = None,
) -> dict:
    """Count attachment download progress for a conversation.

    Supports both the legacy json column and the modern message_attachments table.
    Returns a dict with keys: ``total_attachments``, ``ready``, ``pending``.
    """
    conn = _open_db(signal_data_dir)
    try:
        tables = _get_all_tables(conn)
        msg_cols = _get_table_columns(conn, "messages")

        total = 0
        ready = 0
        pending = 0

        # Method 1: message_attachments table (Signal Desktop 7+)
        if "message_attachments" in tables:
            ma_cols = _get_table_columns(conn, "message_attachments")
            cdn_key_col = "transitCdnKey" if "transitCdnKey" in ma_cols else "cdnKey"

            q = "SELECT path, contentType, " + cdn_key_col + " FROM message_attachments"
            params: list = []
            conditions: list[str] = []

            if "attachmentType" in ma_cols:
                conditions.append("(attachmentType = 'standard' OR attachmentType = 'long-message')")

            if conversation_id and "conversationId" in ma_cols:
                conditions.append("conversationId = ?")
                params.append(conversation_id)

            if conditions:
                q += " WHERE " + " AND ".join(conditions)

            rows = conn.execute(q, params).fetchall()
            for r in rows:
                path = r[0] or ""
                content_type = r[1] or ""
                cdn_key = r[2] or ""
                if not content_type and not cdn_key:
                    continue
                total += 1
                if path:
                    ready += 1
                else:
                    pending += 1

            if total > 0:
                return {"total_attachments": total, "ready": ready, "pending": pending}

        # Method 2: Legacy json column
        if "json" not in msg_cols:
            return {"total_attachments": 0, "ready": 0, "pending": 0}

        conv_id_col = "conversationId" if "conversationId" in msg_cols else "conversation_id"

        q = "SELECT json FROM messages WHERE json LIKE '%\"attachments\":[%' AND json NOT LIKE '%\"attachments\":[]%'"
        params = []
        if conversation_id:
            q += f" AND {conv_id_col} = ?"
            params.append(conversation_id)

        rows = conn.execute(q, params).fetchall()

        for (raw_json,) in rows:
            try:
                msg_json = json.loads(raw_json) if raw_json else {}
                for att in msg_json.get("attachments") or []:
                    if not isinstance(att, dict):
                        continue
                    has_cdn = bool(att.get("cdnKey") or att.get("cdnId") or att.get("cdnNumber") is not None)
                    has_type = bool(att.get("contentType"))
                    if not (has_cdn or has_type):
                        continue
                    total += 1
                    if att.get("path"):
                        ready += 1
                    else:
                        pending += 1
            except (json.JSONDecodeError, TypeError):
                continue

        return {"total_attachments": total, "ready": ready, "pending": pending}
    finally:
        conn.close()


def is_db_available(signal_data_dir: str) -> bool:
    """Check if Signal Desktop DB is available and can be opened."""
    try:
        conn = _open_db(signal_data_dir)
        conn.close()
        return True
    except Exception as e:
        log.debug("Signal Desktop DB not available: %s", e)
        return False

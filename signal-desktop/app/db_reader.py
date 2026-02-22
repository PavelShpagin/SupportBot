"""
Read messages from Signal Desktop's SQLCipher database.

Signal Desktop stores its encryption key in a JSON config file.
The DB is at: ~/.config/Signal/sql/db.sqlite
The key is at: ~/.config/Signal/config.json (field: "key")
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
    sender_name: Optional[str] = None  # display name from contacts
    # Attachment metadata parsed from the json column.
    # Each entry is a dict with keys: path (relative to Signal data dir),
    # fileName, contentType.  Only entries with a non-empty path are included.
    attachments: list = None  # list[dict]

    def __post_init__(self):
        # dataclass frozen=True doesn't allow setattr, use object.__setattr__
        if self.attachments is None:
            object.__setattr__(self, "attachments", [])


def _get_db_key(signal_data_dir: str) -> str:
    """Extract the SQLCipher key from Signal Desktop's config.json."""
    config_path = Path(signal_data_dir) / "config.json"
    if not config_path.exists():
        raise RuntimeError(f"Signal Desktop config not found: {config_path}")
    
    with open(config_path) as f:
        config = json.load(f)
    
    key = config.get("key")
    if not key:
        raise RuntimeError("No 'key' field in Signal Desktop config.json")
    
    return key


def _open_db(signal_data_dir: str):
    """Open the Signal Desktop SQLCipher database."""
    # Try sqlcipher3-binary first (better compatibility), then pysqlcipher3
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
        # Signal Desktop uses SQLCipher 4 with specific settings
        # Order matters: cipher_compatibility and cipher_page_size BEFORE key
        conn.execute("PRAGMA cipher_compatibility = 4;")
        conn.execute("PRAGMA cipher_page_size = 4096;")
        conn.execute(f"PRAGMA key = \"x'{key}'\";")
        
        # Verify we can read
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1;").fetchall()
        log.info("DB opened successfully, found tables")
        return conn
    except Exception as e:
        conn.close()
        raise RuntimeError(f"Failed to open Signal DB: {e}")
    
    return conn


def _get_table_columns(conn, table: str) -> set[str]:
    """Get column names for a table."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def get_conversations(signal_data_dir: str) -> list[dict]:
    """Get all conversations from Signal Desktop DB."""
    conn = _open_db(signal_data_dir)
    try:
        cols = _get_table_columns(conn, "conversations")
        
        # Build query based on available columns
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


def get_messages(
    signal_data_dir: str,
    conversation_id: Optional[str] = None,
    since_timestamp: Optional[int] = None,
    limit: int = 100,
) -> list[SignalMessage]:
    """
    Get messages from Signal Desktop DB.
    
    Args:
        signal_data_dir: Path to Signal Desktop data directory
        conversation_id: Filter to specific conversation (optional)
        since_timestamp: Only get messages after this timestamp (ms, optional)
        limit: Maximum number of messages to return
    
    Returns:
        List of SignalMessage objects, oldest first
    """
    conn = _open_db(signal_data_dir)
    try:
        msg_cols = _get_table_columns(conn, "messages")
        conv_cols = _get_table_columns(conn, "conversations")
        
        # Determine column names (Signal Desktop schema varies by version)
        ts_col = "sent_at" if "sent_at" in msg_cols else "timestamp"
        conv_id_col = "conversationId" if "conversationId" in msg_cols else "conversation_id"
        body_col = "body" if "body" in msg_cols else "message"
        type_col = "type" if "type" in msg_cols else None
        
        # Sender column
        sender_col = None
        for candidate in ["sourceServiceId", "sourceUuid", "source"]:
            if candidate in msg_cols:
                sender_col = candidate
                break
        
        # Build the query
        select_parts = [
            f"m.id",
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
        
        # Join with conversations to get group info
        if "groupId" in conv_cols:
            select_parts.append("c.groupId as group_id")
        else:
            select_parts.append("NULL as group_id")
        
        if "name" in conv_cols:
            select_parts.append("c.name as group_name")
        else:
            select_parts.append("NULL as group_name")
        
        # Sender display name from contacts (sc = sender conversation)
        if sender_col and "profileName" in conv_cols:
            select_parts.append("COALESCE(sc.name, sc.profileName, sc.profileFullName) as sender_name")
        else:
            select_parts.append("NULL as sender_name")

        # Include the raw JSON column so we can parse attachments (row[9]).
        # The column is present in Signal Desktop 6+ and contains attachments,
        # reactions, and other rich data.
        has_json_col = "json" in msg_cols
        if has_json_col:
            select_parts.append("m.json as raw_json")

        sender_join = ""
        if sender_col:
            sender_join = f"LEFT JOIN conversations sc ON m.{sender_col} = sc.id"

        # Include messages that have either a text body OR at least one
        # attachment.  The post-filter below drops rows with neither.
        if has_json_col:
            body_cond = (
                f"(m.{body_col} IS NOT NULL AND m.{body_col} != '')"
                f" OR (m.json LIKE '%\"attachments\":[{{%')"
            )
        else:
            body_cond = f"m.{body_col} IS NOT NULL AND m.{body_col} != ''"

        q = f"""
            SELECT {', '.join(select_parts)}
            FROM messages m
            LEFT JOIN conversations c ON m.{conv_id_col} = c.id
            {sender_join}
            WHERE {body_cond}
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

        # Build a reactions-per-timestamp map.
        # Signal Desktop stores reactions either in a separate 'reactions' table
        # OR embedded in the 'json' column of the messages table.
        reactions_by_ts: dict[int, int] = {}
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            
            # Method 1: Try the reactions table
            if "reactions" in tables:
                rxn_cols = _get_table_columns(conn, "reactions")
                log.info("Reactions table columns: %s", rxn_cols)
                if "targetTimestamp" in rxn_cols:
                    rxn_where = ""
                    rxn_params: list = []
                    if conversation_id and "conversationId" in rxn_cols:
                        rxn_where = "WHERE conversationId = ?"
                        rxn_params = [conversation_id]
                    rxn_rows = conn.execute(
                        f"SELECT targetTimestamp, COUNT(*) FROM reactions {rxn_where} GROUP BY targetTimestamp",
                        rxn_params,
                    ).fetchall()
                    reactions_by_ts = {int(r[0]): int(r[1]) for r in rxn_rows if r[0] is not None}
                    log.info("Found %d messages with reactions (from reactions table)", len(reactions_by_ts))
            
            # Method 2: Try the json column in messages table (newer Signal Desktop versions)
            if not reactions_by_ts and "json" in msg_cols:
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
                ts = int(row[2]) if row[2] else 0
                body = str(row[3] or "")
                sender_name = str(row[8]) if len(row) > 8 and row[8] else None
                # row[9] is raw_json only when has_json_col is True (added last)
                raw_json_str = str(row[9]) if (has_json_col and len(row) > 9 and row[9]) else None

                # Parse attachments from the raw json column
                attachments: list = []
                if raw_json_str:
                    try:
                        msg_json = json.loads(raw_json_str)
                        for att in msg_json.get("attachments") or []:
                            if not isinstance(att, dict):
                                continue
                            att_path = att.get("path") or ""
                            content_type = att.get("contentType") or ""
                            file_name = att.get("fileName") or ""
                            if att_path:
                                attachments.append({
                                    "path": att_path,
                                    "fileName": file_name,
                                    "contentType": content_type,
                                })
                    except (json.JSONDecodeError, TypeError):
                        pass

                # Skip messages with neither text nor attachments
                if not body and not attachments:
                    continue

                msg = SignalMessage(
                    id=str(row[0]),
                    conversation_id=str(row[1]) if row[1] else "",
                    timestamp=ts,
                    body=body,
                    sender=str(row[4]) if row[4] else None,
                    type=str(row[5]) if row[5] else "unknown",
                    group_id=str(row[6]) if row[6] else None,
                    group_name=str(row[7]) if row[7] else None,
                    reactions=reactions_by_ts.get(ts, 0),
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
    """
    Get messages for a specific group by ID or name.
    
    This is useful for history ingestion - getting all messages from a group.
    """
    conn = _open_db(signal_data_dir)
    try:
        msg_cols = _get_table_columns(conn, "messages")
        conv_cols = _get_table_columns(conn, "conversations")
        
        # First, find the conversation ID for this group
        conv_id = None
        
        if group_id and "groupId" in conv_cols:
            row = conn.execute(
                "SELECT id FROM conversations WHERE groupId = ? LIMIT 1",
                (group_id,)
            ).fetchone()
            if row:
                conv_id = row[0]
        
        if not conv_id and group_name and "name" in conv_cols:
            # Try exact match first
            row = conn.execute(
                "SELECT id FROM conversations WHERE LOWER(name) = LOWER(?) LIMIT 1",
                (group_name,)
            ).fetchone()
            if row:
                conv_id = row[0]
            else:
                # Partial match
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


def is_db_available(signal_data_dir: str) -> bool:
    """Check if Signal Desktop DB is available and can be opened."""
    try:
        conn = _open_db(signal_data_dir)
        conn.close()
        return True
    except Exception as e:
        log.debug("Signal Desktop DB not available: %s", e)
        return False

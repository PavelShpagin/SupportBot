"""Database access layer - supports MySQL (default) and Oracle."""

from __future__ import annotations

import os
from typing import Union

# Type alias for database connection
Database = Union["MySQL", "Oracle"]

# Determine which database backend to use
DB_BACKEND = os.getenv("DB_BACKEND", "mysql").lower()


def get_db_backend() -> str:
    """Return the configured database backend ('mysql' or 'oracle')."""
    return DB_BACKEND


def is_mysql() -> bool:
    """Check if MySQL backend is configured."""
    return DB_BACKEND == "mysql"


def is_oracle() -> bool:
    """Check if Oracle backend is configured."""
    return DB_BACKEND == "oracle"


# Lazy imports based on backend
if DB_BACKEND == "mysql":
    from app.db.mysql import MySQL, create_mysql, is_mysql_error, MYSQL_ERR_DUP_ENTRY
    from app.db.schema_mysql import ensure_schema
    from app.db.queries_mysql import (
        RawMessage,
        Job,
        AdminSession,
        POSITIVE_EMOJI,
        insert_raw_message,
        enqueue_job,
        get_raw_message,
        get_last_messages_text,
        get_buffer,
        set_buffer,
        new_case_id,
        insert_case,
        upsert_case,
        confirm_cases_by_evidence_ts,
        store_case_embedding,
        find_similar_case,
        merge_case,
        archive_case,
        create_history_token,
        validate_history_token,
        mark_history_token_used,
        claim_next_job,
        complete_job,
        fail_job,
        get_admin_session,
        upsert_admin_session,
        set_admin_awaiting_group_name,
        set_admin_awaiting_qr_scan,
        get_admin_by_token,
        link_admin_to_group,
        get_group_admins,
        upsert_reaction,
        delete_reaction,
        get_positive_reactions_for_message,
        get_message_by_ts,
        get_case,
        get_case_evidence,
        get_open_cases_for_group,
        get_recent_solved_cases,
        update_case_to_solved,
        mark_case_in_rag,
        expire_old_open_cases,
        upsert_group_docs,
        get_group_docs,
        wipe_all_data,
        delete_all_group_data,
        clear_group_runtime_data,
        get_all_active_case_ids,
    )
    
    def create_db(settings):
        """Create database connection based on settings."""
        return create_mysql(settings)

else:
    from app.db.oracle import Oracle, create_oracle, is_ora_error
    from app.db.schema import ensure_schema
    from app.db.queries import (
        RawMessage,
        Job,
        insert_raw_message,
        enqueue_job,
        get_raw_message,
        get_last_messages_text,
        get_buffer,
        set_buffer,
        new_case_id,
        insert_case,
        create_history_token,
        validate_history_token,
        mark_history_token_used,
        claim_next_job,
        complete_job,
        fail_job,
    )
    
    def create_db(settings):
        """Create database connection based on settings."""
        return create_oracle(settings)


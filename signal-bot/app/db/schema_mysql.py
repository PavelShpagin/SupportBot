from __future__ import annotations

import logging

from app.db.mysql import MySQL, is_mysql_error, MYSQL_ERR_TABLE_EXISTS

log = logging.getLogger(__name__)


DDL_STATEMENTS = [
    """
    CREATE TABLE raw_messages (
      message_id    VARCHAR(128) PRIMARY KEY,
      group_id      VARCHAR(128) NOT NULL,
      ts            BIGINT NOT NULL,
      sender_hash   VARCHAR(64) NOT NULL,
      content_text  LONGTEXT,
      image_paths_json LONGTEXT,
      reply_to_id   VARCHAR(128),
      created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
      INDEX idx_raw_messages_group_ts (group_id, ts DESC)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE buffers (
      group_id     VARCHAR(128) PRIMARY KEY,
      buffer_text  LONGTEXT,
      updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE cases (
      case_id          VARCHAR(32) PRIMARY KEY,
      group_id         VARCHAR(128) NOT NULL,
      status           VARCHAR(16) NOT NULL,
      problem_title    VARCHAR(256) NOT NULL,
      problem_summary  LONGTEXT NOT NULL,
      solution_summary LONGTEXT,
      tags_json        LONGTEXT,
      evidence_image_paths_json LONGTEXT,
      created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
      updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP NOT NULL,
      CONSTRAINT cases_status_chk CHECK (status IN ('solved', 'open')),
      INDEX idx_cases_group (group_id),
      INDEX idx_cases_status (status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE case_evidence (
      case_id    VARCHAR(32) NOT NULL,
      message_id VARCHAR(128) NOT NULL,
      PRIMARY KEY (case_id, message_id),
      INDEX idx_case_evidence_message (message_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE admins_groups (
      admin_id   VARCHAR(128) NOT NULL,
      group_id   VARCHAR(128) NOT NULL,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
      PRIMARY KEY (admin_id, group_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE history_tokens (
      token       VARCHAR(64) PRIMARY KEY,
      admin_id    VARCHAR(128) NOT NULL,
      group_id    VARCHAR(128) NOT NULL,
      expires_at  TIMESTAMP NOT NULL,
      used_at     TIMESTAMP NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE jobs (
      job_id       BIGINT AUTO_INCREMENT PRIMARY KEY,
      type         VARCHAR(64) NOT NULL,
      payload_json LONGTEXT,
      status       VARCHAR(16) NOT NULL,
      attempts     INT DEFAULT 0 NOT NULL,
      updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP NOT NULL,
      CONSTRAINT jobs_status_chk CHECK (status IN ('pending', 'in_progress', 'done', 'failed', 'cancelled')),
      INDEX idx_jobs_status_type (status, type),
      INDEX idx_jobs_updated (updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE admin_sessions (
      admin_id       VARCHAR(128) PRIMARY KEY,
      pending_group_id   VARCHAR(128),
      pending_group_name VARCHAR(256),
      pending_token      VARCHAR(64),
      state              VARCHAR(32) NOT NULL DEFAULT 'awaiting_group_name',
      lang               VARCHAR(2) NOT NULL DEFAULT 'uk',
      updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP NOT NULL,
      CONSTRAINT admin_sessions_state_chk CHECK (state IN ('awaiting_group_name', 'awaiting_qr_scan', 'idle')),
      CONSTRAINT admin_sessions_lang_chk CHECK (lang IN ('uk', 'en'))
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE reactions (
      id            BIGINT AUTO_INCREMENT PRIMARY KEY,
      group_id      VARCHAR(128) NOT NULL,
      target_ts     BIGINT NOT NULL,
      target_author VARCHAR(128) NOT NULL,
      sender_hash   VARCHAR(64) NOT NULL,
      emoji         VARCHAR(32) NOT NULL,
      created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
      INDEX idx_reactions_target (group_id, target_ts),
      UNIQUE KEY uk_reactions_unique (group_id, target_ts, sender_hash, emoji)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE chat_groups (
      group_id      VARCHAR(128) PRIMARY KEY,
      group_name    VARCHAR(256),
      docs_urls     LONGTEXT,
      created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
      updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
]


MIGRATIONS = [
    # Add columns that may be missing from older installs
    "ALTER TABLE cases ADD COLUMN IF NOT EXISTS evidence_image_paths_json LONGTEXT",
    "ALTER TABLE raw_messages ADD COLUMN IF NOT EXISTS image_paths_json LONGTEXT",
]


def ensure_schema(db: MySQL) -> None:
    with db.connection() as conn:
        cur = conn.cursor()
        for ddl in DDL_STATEMENTS:
            try:
                cur.execute(ddl)
                conn.commit()
            except Exception as exc:
                # Error 1050: Table already exists
                if is_mysql_error(exc, MYSQL_ERR_TABLE_EXISTS):
                    conn.rollback()
                    continue
                conn.rollback()
                log.exception("Schema DDL failed")
                raise
        # Apply migrations (idempotent ALTER TABLE ADD COLUMN IF NOT EXISTS)
        for migration in MIGRATIONS:
            try:
                cur.execute(migration)
                conn.commit()
            except Exception as exc:
                conn.rollback()
                log.warning("Migration skipped (may already be applied): %s — %s", migration[:60], exc)

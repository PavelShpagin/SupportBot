from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator, Optional

import mysql.connector
from mysql.connector import pooling

from app.config import Settings

log = logging.getLogger(__name__)


class MySQL:
    def __init__(self, pool: pooling.MySQLConnectionPool):
        self.pool = pool

    @contextmanager
    def connection(self) -> Iterator[mysql.connector.MySQLConnection]:
        conn = self.pool.get_connection()
        try:
            yield conn
        finally:
            conn.close()


def create_mysql(settings: Settings) -> MySQL:
    pool_config = {
        "pool_name": "supportbot_pool",
        "pool_size": 4,
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


def is_mysql_error(exc: Exception, code: int) -> bool:
    """Check if exception is a MySQL error with specific code."""
    try:
        return getattr(exc, "errno", None) == code
    except Exception:
        return False


# MySQL error codes
MYSQL_ERR_DUP_ENTRY = 1062  # Duplicate entry for key
MYSQL_ERR_TABLE_EXISTS = 1050  # Table already exists

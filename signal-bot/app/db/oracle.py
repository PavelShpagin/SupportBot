from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator, Optional

import oracledb

from app.config import Settings

log = logging.getLogger(__name__)


class Oracle:
    def __init__(self, pool: oracledb.ConnectionPool):
        self.pool = pool

    @contextmanager
    def connection(self) -> Iterator[oracledb.Connection]:
        conn = self.pool.acquire()
        try:
            yield conn
        finally:
            self.pool.release(conn)


def create_oracle(settings: Settings) -> Oracle:
    # Make CLOB columns come back as Python strings.
    oracledb.defaults.fetch_lobs = True

    wallet_dir = settings.oracle_wallet_dir.strip()
    if wallet_dir:
        os.environ.setdefault("TNS_ADMIN", wallet_dir)
        log.info("Oracle wallet configured via TNS_ADMIN=%s", wallet_dir)

    pool_kwargs: dict = {
        "user": settings.oracle_user,
        "password": settings.oracle_password,
        "dsn": settings.oracle_dsn,
        "min": 1,
        "max": 4,
        "increment": 1,
    }

    # For Autonomous DB, using the wallet directory is typical.
    if wallet_dir:
        pool_kwargs["config_dir"] = wallet_dir
        pool_kwargs["wallet_location"] = wallet_dir

    pool = oracledb.create_pool(**pool_kwargs)
    return Oracle(pool=pool)


def is_ora_error(exc: Exception, code: int) -> bool:
    # oracledb raises oracledb.Error with args containing error object(s)
    try:
        err = exc.args[0]
        return getattr(err, "code", None) == code
    except Exception:
        return False


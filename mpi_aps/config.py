"""Environment-driven configuration. No secrets are stored in the repository."""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    db_path: str
    users_file: str | None
    dev_auth: bool
    plan_date: date
    log_level: str
    four_eyes: bool
    max_body_bytes: int = 5_000_000


def load() -> Settings:
    env = os.environ.get
    return Settings(
        host=env("MPI_HOST", "127.0.0.1"), port=int(env("MPI_PORT", "8080")),
        db_path=env("MPI_DB_PATH", "mpi_aps.sqlite"), users_file=env("MPI_USERS_FILE"),
        dev_auth=env("MPI_DEV_AUTH", "0") == "1",
        plan_date=date.fromisoformat(env("MPI_PLAN_DATE")) if env("MPI_PLAN_DATE") else date.today(),
        log_level=env("MPI_LOG_LEVEL", "INFO"), four_eyes=env("MPI_FOUR_EYES", "1") == "1")


class JsonFormatter(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        d = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(r.created)) + "Z", "level": r.levelname,
             "logger": r.name, "msg": r.getMessage()}
        d.update(getattr(r, "ctx", {}))
        if r.exc_info:
            d["exc"] = self.formatException(r.exc_info)
        return json.dumps(d, default=str)


def setup_logging(level: str) -> logging.Logger:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    log = logging.getLogger("mpi_aps")
    log.handlers[:] = [h]
    log.setLevel(level.upper())
    return log

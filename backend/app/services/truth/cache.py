import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.models import ClaimResponse


class ClaimCache:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS claim_cache (
                    normalized_claim TEXT PRIMARY KEY,
                    response_json TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )

    def available(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1")
            return True
        except sqlite3.Error:
            return False

    def get(self, normalized_claim: str, now: datetime | None = None) -> ClaimResponse | None:
        current = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT response_json, expires_at FROM claim_cache WHERE normalized_claim = ?",
                (normalized_claim,),
            ).fetchone()
            if not row:
                return None
            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at <= current:
                connection.execute("DELETE FROM claim_cache WHERE normalized_claim = ?", (normalized_claim,))
                return None
            payload = json.loads(row["response_json"])
            payload["cached"] = True
            return ClaimResponse.model_validate(payload)

    def set(self, normalized_claim: str, response: ClaimResponse, ttl: timedelta) -> None:
        checked_at = response.checked_at
        expires_at = checked_at + ttl
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO claim_cache(normalized_claim, response_json, checked_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(normalized_claim) DO UPDATE SET
                    response_json = excluded.response_json,
                    checked_at = excluded.checked_at,
                    expires_at = excluded.expires_at
                """,
                (normalized_claim, response.model_dump_json(), checked_at.isoformat(), expires_at.isoformat()),
            )

    def clear(self) -> int:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM claim_cache")
            return cursor.rowcount

import os
import sqlite3
import time
import uuid
from contextlib import contextmanager


SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts (
    id TEXT PRIMARY KEY,
    contract_number TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL UNIQUE,
    data_ciphertext TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL,
    first_viewed_at TEXT,
    signed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_contracts_created_at ON contracts(created_at);
CREATE INDEX IF NOT EXISTS idx_contracts_status ON contracts(status);
CREATE TABLE IF NOT EXISTS contract_events (
    id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(contract_id) REFERENCES contracts(id)
);
CREATE INDEX IF NOT EXISTS idx_events_contract ON contract_events(contract_id, created_at);
CREATE TABLE IF NOT EXISTS rate_limits (
    bucket_key TEXT PRIMARY KEY,
    window_start INTEGER NOT NULL,
    request_count INTEGER NOT NULL
);
"""


class ContractStore:
    def __init__(self, database_url=None):
        self.database_url = database_url or os.environ.get("DATABASE_URL", "").strip()
        self.is_postgres = self.database_url.startswith(("postgres://", "postgresql://"))
        if not self.database_url:
            instance_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance")
            os.makedirs(instance_dir, exist_ok=True)
            self.database_url = os.path.join(instance_dir, "contracts.db")
        self.initialize()

    @contextmanager
    def connect(self):
        if self.is_postgres:
            import psycopg
            from psycopg.rows import dict_row

            connection = psycopg.connect(self.database_url, row_factory=dict_row)
        else:
            connection = sqlite3.connect(self.database_url, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _sql(self, query):
        return query.replace("?", "%s") if self.is_postgres else query

    def initialize(self):
        with self.connect() as connection:
            if self.is_postgres:
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        connection.execute(statement)
            else:
                connection.executescript(SCHEMA)

    @staticmethod
    def _dict(row):
        return dict(row) if row is not None else None

    def create_contract(self, contract):
        fields = ("id", "contract_number", "token_hash", "data_ciphertext", "created_by",
                  "created_at", "expires_at", "status", "first_viewed_at", "signed_at")
        values = tuple(contract.get(field) for field in fields)
        with self.connect() as connection:
            connection.execute(
                self._sql("""INSERT INTO contracts
                    (id, contract_number, token_hash, data_ciphertext, created_by,
                     created_at, expires_at, status, first_viewed_at, signed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""),
                values,
            )
            self._add_event(connection, contract["id"], "CRIADO", contract["created_by"], contract["created_at"])

    def _add_event(self, connection, contract_id, event_type, actor, created_at):
        connection.execute(
            self._sql("INSERT INTO contract_events (id, contract_id, event_type, actor, created_at) VALUES (?, ?, ?, ?, ?)"),
            (str(uuid.uuid4()), contract_id, event_type, actor, created_at),
        )

    def add_event(self, contract_id, event_type, actor, created_at):
        with self.connect() as connection:
            self._add_event(connection, contract_id, event_type, actor, created_at)

    def get_by_token_hash(self, token_hash):
        with self.connect() as connection:
            row = connection.execute(self._sql("SELECT * FROM contracts WHERE token_hash = ?"), (token_hash,)).fetchone()
        return self._dict(row)

    def get_by_id(self, contract_id):
        with self.connect() as connection:
            row = connection.execute(self._sql("SELECT * FROM contracts WHERE id = ?"), (contract_id,)).fetchone()
        return self._dict(row)

    def list_recent(self, limit=30):
        with self.connect() as connection:
            rows = connection.execute(self._sql("SELECT * FROM contracts ORDER BY created_at DESC LIMIT ?"), (limit,)).fetchall()
        return [dict(row) for row in rows]

    def mark_viewed(self, contract_id, timestamp):
        with self.connect() as connection:
            row = connection.execute(self._sql("SELECT first_viewed_at FROM contracts WHERE id = ?"), (contract_id,)).fetchone()
            if row is None or row["first_viewed_at"]:
                return False
            cursor = connection.execute(
                self._sql("UPDATE contracts SET first_viewed_at = ?, status = ? WHERE id = ? AND first_viewed_at IS NULL"),
                (timestamp, "VISUALIZADO", contract_id),
            )
            if cursor.rowcount != 1:
                return False
            self._add_event(connection, contract_id, "VISUALIZADO", "paciente", timestamp)
            return True

    def mark_signed(self, contract_id, timestamp):
        with self.connect() as connection:
            cursor = connection.execute(
                self._sql("UPDATE contracts SET signed_at = ?, status = ? WHERE id = ? AND signed_at IS NULL"),
                (timestamp, "ASSINADO", contract_id),
            )
            if cursor.rowcount != 1:
                return False
            self._add_event(connection, contract_id, "ASSINADO", "paciente", timestamp)
            return True

    def mark_expired(self, contract_id, timestamp):
        with self.connect() as connection:
            cursor = connection.execute(
                self._sql("UPDATE contracts SET status = ? WHERE id = ? AND status NOT IN (?, ?)"),
                ("EXPIRADO", contract_id, "EXPIRADO", "ASSINADO"),
            )
            if cursor.rowcount == 1:
                self._add_event(connection, contract_id, "EXPIRADO", "sistema", timestamp)
                return True
            return False

    def events_for(self, contract_id):
        with self.connect() as connection:
            rows = connection.execute(
                self._sql("SELECT event_type, actor, created_at FROM contract_events WHERE contract_id = ? ORDER BY created_at"),
                (contract_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def allow_request(self, bucket, identity_hash, limit, window_seconds):
        window = int(time.time() // window_seconds)
        bucket_key = f"{bucket}:{identity_hash}"
        query = """
            INSERT INTO rate_limits (bucket_key, window_start, request_count) VALUES (?, ?, 1)
            ON CONFLICT(bucket_key) DO UPDATE SET
                request_count = CASE WHEN rate_limits.window_start = excluded.window_start
                    THEN rate_limits.request_count + 1 ELSE 1 END,
                window_start = excluded.window_start
        """
        with self.connect() as connection:
            connection.execute(self._sql(query), (bucket_key, window))
            row = connection.execute(self._sql("SELECT request_count FROM rate_limits WHERE bucket_key = ?"), (bucket_key,)).fetchone()
        return int(row["request_count"]) <= limit

    def health(self):
        with self.connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return "postgresql" if self.is_postgres else "sqlite"

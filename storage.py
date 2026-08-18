import os
import sqlite3
import time
import uuid
from contextlib import contextmanager


class AppointmentConflictError(Exception):
    pass


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
    signed_at TEXT,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_contracts_created_at ON contracts(created_at);
CREATE INDEX IF NOT EXISTS idx_contracts_status ON contracts(status);
CREATE INDEX IF NOT EXISTS idx_contracts_number ON contracts(contract_number);
CREATE TABLE IF NOT EXISTS contract_events (
    id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(contract_id) REFERENCES contracts(id)
);
CREATE INDEX IF NOT EXISTS idx_events_contract ON contract_events(contract_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_type ON contract_events(event_type, created_at);
CREATE TABLE IF NOT EXISTS appointment_types (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS appointments (
    id TEXT PRIMARY KEY,
    data_ciphertext TEXT NOT NULL,
    appointment_date TEXT NOT NULL,
    appointment_time TEXT NOT NULL,
    appointment_type_id TEXT NOT NULL,
    professional TEXT NOT NULL,
    professional_key TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    contract_id TEXT,
    FOREIGN KEY(appointment_type_id) REFERENCES appointment_types(id),
    FOREIGN KEY(contract_id) REFERENCES contracts(id)
);
CREATE INDEX IF NOT EXISTS idx_appointments_date ON appointments(appointment_date, appointment_time);
CREATE INDEX IF NOT EXISTS idx_appointments_status ON appointments(status, appointment_date);
CREATE INDEX IF NOT EXISTS idx_appointments_professional ON appointments(professional_key, appointment_date);
CREATE UNIQUE INDEX IF NOT EXISTS uq_appointment_slot
    ON appointments(appointment_date, appointment_time, professional_key)
    WHERE status <> 'CANCELADO';
CREATE TABLE IF NOT EXISTS appointment_events (
    id TEXT PRIMARY KEY,
    appointment_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(appointment_id) REFERENCES appointments(id)
);
CREATE INDEX IF NOT EXISTS idx_appointment_events ON appointment_events(appointment_id, created_at);
CREATE TABLE IF NOT EXISTS appointment_search_terms (
    appointment_id TEXT NOT NULL,
    token TEXT NOT NULL,
    PRIMARY KEY(appointment_id, token),
    FOREIGN KEY(appointment_id) REFERENCES appointments(id)
);
CREATE INDEX IF NOT EXISTS idx_appointment_search_token ON appointment_search_terms(token);
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
            if self.is_postgres:
                connection.execute("ALTER TABLE contracts ADD COLUMN IF NOT EXISTS deleted_at TEXT")
                connection.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS deleted_at TEXT")
            else:
                contract_columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(contracts)").fetchall()
                }
                if "deleted_at" not in contract_columns:
                    connection.execute("ALTER TABLE contracts ADD COLUMN deleted_at TEXT")
                appointment_columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(appointments)").fetchall()
                }
                if "deleted_at" not in appointment_columns:
                    connection.execute("ALTER TABLE appointments ADD COLUMN deleted_at TEXT")
            # Compatibilidade segura: versões anteriores registravam ENVIADO no
            # histórico, mas mantinham o status como AGUARDANDO.
            connection.execute("""
                UPDATE contracts SET status = 'ENVIADO'
                WHERE status = 'AGUARDANDO' AND EXISTS (
                    SELECT 1 FROM contract_events
                    WHERE contract_events.contract_id = contracts.id
                      AND contract_events.event_type = 'ENVIADO'
                )
            """)
            created_at = "2026-08-18T00:00:00+00:00"
            for type_id, name in (("consulta", "Consulta"), ("retorno", "Retorno"),
                                  ("avaliacao", "Avaliação"), ("procedimento", "Procedimento"),
                                  ("outro", "Outro")):
                connection.execute(
                    self._sql("""INSERT INTO appointment_types (id, name, active, created_at)
                        VALUES (?, ?, 1, ?) ON CONFLICT(id) DO NOTHING"""),
                    (type_id, name, created_at),
                )

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

    def mark_sent(self, contract_id, actor, timestamp):
        with self.connect() as connection:
            cursor = connection.execute(
                self._sql("UPDATE contracts SET status = ? WHERE id = ? AND deleted_at IS NULL AND status = ?"),
                ("ENVIADO", contract_id, "AGUARDANDO"),
            )
            if cursor.rowcount != 1:
                return False
            self._add_event(connection, contract_id, "ENVIADO", actor, timestamp)
            return True

    def get_by_token_hash(self, token_hash):
        with self.connect() as connection:
            row = connection.execute(
                self._sql("SELECT * FROM contracts WHERE token_hash = ? AND deleted_at IS NULL"),
                (token_hash,),
            ).fetchone()
        return self._dict(row)

    def get_by_id(self, contract_id):
        with self.connect() as connection:
            row = connection.execute(
                self._sql("SELECT * FROM contracts WHERE id = ? AND deleted_at IS NULL"),
                (contract_id,),
            ).fetchone()
        return self._dict(row)

    def list_recent(self, limit=30):
        with self.connect() as connection:
            rows = connection.execute(
                self._sql("SELECT * FROM contracts WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT ?"),
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_filtered(self, status=None, created_from=None, created_until=None):
        query = """SELECT * FROM contracts
            WHERE deleted_at IS NULL
              AND (? = 0 OR status = ?)
              AND (? = 0 OR created_at >= ?)
              AND (? = 0 OR created_at < ?)
            ORDER BY created_at DESC"""
        values = (
            int(bool(status)), status or "",
            int(bool(created_from)), created_from or "",
            int(bool(created_until)), created_until or "",
        )
        with self.connect() as connection:
            rows = connection.execute(self._sql(query), values).fetchall()
        return [dict(row) for row in rows]

    def expire_due(self, timestamp):
        with self.connect() as connection:
            rows = connection.execute(
                self._sql("SELECT id FROM contracts WHERE deleted_at IS NULL AND expires_at <= ? AND status NOT IN (?, ?)"),
                (timestamp, "EXPIRADO", "ASSINADO"),
            ).fetchall()
            expired_ids = [row["id"] for row in rows]
            for contract_id in expired_ids:
                cursor = connection.execute(
                    self._sql("UPDATE contracts SET status = ? WHERE id = ? AND deleted_at IS NULL AND status NOT IN (?, ?)"),
                    ("EXPIRADO", contract_id, "EXPIRADO", "ASSINADO"),
                )
                if cursor.rowcount == 1:
                    self._add_event(connection, contract_id, "EXPIRADO", "sistema", timestamp)
        return expired_ids

    def mark_viewed(self, contract_id, timestamp):
        with self.connect() as connection:
            cursor = connection.execute(
                self._sql("""UPDATE contracts SET first_viewed_at = ?, status = ?
                    WHERE id = ? AND first_viewed_at IS NULL AND signed_at IS NULL
                      AND deleted_at IS NULL AND status <> ?"""),
                (timestamp, "VISUALIZADO", contract_id, "EXPIRADO"),
            )
            if cursor.rowcount != 1:
                return False
            self._add_event(connection, contract_id, "VISUALIZADO", "paciente", timestamp)
            return True

    def mark_signed(self, contract_id, timestamp):
        with self.connect() as connection:
            cursor = connection.execute(
                self._sql("""UPDATE contracts SET signed_at = ?, status = ?
                    WHERE id = ? AND signed_at IS NULL AND deleted_at IS NULL
                      AND status <> ? AND expires_at > ?"""),
                (timestamp, "ASSINADO", contract_id, "EXPIRADO", timestamp),
            )
            if cursor.rowcount != 1:
                return False
            self._add_event(connection, contract_id, "ASSINADO", "paciente", timestamp)
            return True

    def mark_expired(self, contract_id, timestamp):
        with self.connect() as connection:
            cursor = connection.execute(
                self._sql("UPDATE contracts SET status = ? WHERE id = ? AND deleted_at IS NULL AND status NOT IN (?, ?)"),
                ("EXPIRADO", contract_id, "EXPIRADO", "ASSINADO"),
            )
            if cursor.rowcount == 1:
                self._add_event(connection, contract_id, "EXPIRADO", "sistema", timestamp)
                return True
            return False

    def soft_delete_contract(self, contract_id, actor, timestamp):
        """Oculta o contrato e revoga seu token sem apagar dados ou histórico."""
        with self.connect() as connection:
            cursor = connection.execute(
                self._sql("UPDATE contracts SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL"),
                (timestamp, contract_id),
            )
            if cursor.rowcount != 1:
                return False
            self._add_event(connection, contract_id, "EXCLUIDO", actor, timestamp)
            return True

    def events_for(self, contract_id):
        with self.connect() as connection:
            rows = connection.execute(
                self._sql("SELECT event_type, actor, created_at FROM contract_events WHERE contract_id = ? ORDER BY created_at"),
                (contract_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _is_unique_conflict(error):
        return (isinstance(error, sqlite3.IntegrityError) and "unique" in str(error).lower()) \
            or getattr(error, "sqlstate", None) == "23505"

    def appointment_types(self, active_only=True):
        query = "SELECT id, name, active FROM appointment_types"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY name"
        with self.connect() as connection:
            rows = connection.execute(query).fetchall()
        return [dict(row) for row in rows]

    def _add_appointment_event(self, connection, appointment_id, event_type, actor, description, created_at):
        connection.execute(
            self._sql("""INSERT INTO appointment_events
                (id, appointment_id, event_type, actor, description, created_at)
                VALUES (?, ?, ?, ?, ?, ?)"""),
            (str(uuid.uuid4()), appointment_id, event_type, actor, description, created_at),
        )

    def _replace_appointment_search_terms(self, connection, appointment_id, tokens):
        connection.execute(self._sql("DELETE FROM appointment_search_terms WHERE appointment_id = ?"),
                           (appointment_id,))
        for token in sorted(set(tokens)):
            connection.execute(
                self._sql("INSERT INTO appointment_search_terms (appointment_id, token) VALUES (?, ?)"),
                (appointment_id, token),
            )

    def create_appointment(self, appointment, search_tokens):
        fields = ("id", "data_ciphertext", "appointment_date", "appointment_time",
                  "appointment_type_id", "professional", "professional_key", "status",
                  "created_by", "created_at", "updated_at", "contract_id")
        values = tuple(appointment.get(field) for field in fields)
        try:
            with self.connect() as connection:
                connection.execute(
                    self._sql("""INSERT INTO appointments
                        (id, data_ciphertext, appointment_date, appointment_time,
                         appointment_type_id, professional, professional_key, status,
                         created_by, created_at, updated_at, contract_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""), values)
                self._replace_appointment_search_terms(connection, appointment["id"], search_tokens)
                self._add_appointment_event(connection, appointment["id"], "CRIADO",
                                            appointment["created_by"], "Agendamento criado",
                                            appointment["created_at"])
        except Exception as error:
            if self._is_unique_conflict(error):
                raise AppointmentConflictError from error
            raise

    def get_appointment(self, appointment_id):
        with self.connect() as connection:
            row = connection.execute(
                self._sql("""SELECT a.*, t.name AS appointment_type_name
                    FROM appointments a JOIN appointment_types t ON t.id = a.appointment_type_id
                    WHERE a.id = ? AND a.deleted_at IS NULL"""), (appointment_id,)).fetchone()
        return self._dict(row)

    def _appointment_ids_for_tokens(self, tokens):
        result = None
        with self.connect() as connection:
            for token in tokens:
                rows = connection.execute(
                    self._sql("SELECT appointment_id FROM appointment_search_terms WHERE token = ?"),
                    (token,),
                ).fetchall()
                ids = {row["appointment_id"] for row in rows}
                result = ids if result is None else result & ids
                if not result:
                    return set()
        return result or set()

    def list_appointments(self, date_from=None, date_until=None, status=None, professional=None,
                          appointment_type_id=None, search_tokens=None):
        base_query = """SELECT a.*, t.name AS appointment_type_name
            FROM appointments a JOIN appointment_types t ON t.id = a.appointment_type_id
            WHERE (? = 0 OR a.appointment_date >= ?)
              AND (? = 0 OR a.appointment_date < ?)
              AND (? = 0 OR a.status = ?)
              AND (? = 0 OR a.professional_key = ?)
              AND (? = 0 OR a.appointment_type_id = ?)
              AND a.deleted_at IS NULL"""
        values = (
            int(bool(date_from)), date_from or "",
            int(bool(date_until)), date_until or "",
            int(bool(status)), status or "",
            int(bool(professional)), professional or "",
            int(bool(appointment_type_id)), appointment_type_id or "",
        )
        rows = []
        candidate_ids = self._appointment_ids_for_tokens(search_tokens) if search_tokens else None
        with self.connect() as connection:
            if search_tokens:
                for appointment_id in candidate_ids:
                    row = connection.execute(self._sql(base_query + " AND a.id = ?"),
                                             values + (appointment_id,)).fetchone()
                    if row:
                        rows.append(dict(row))
            else:
                rows = [dict(row) for row in connection.execute(
                    self._sql(base_query + " ORDER BY a.appointment_date, a.appointment_time"), values).fetchall()]
        rows.sort(key=lambda row: (row["appointment_date"], row["appointment_time"], row["id"]))
        return rows

    def update_appointment(self, appointment_id, appointment, search_tokens, events):
        try:
            with self.connect() as connection:
                cursor = connection.execute(
                    self._sql("""UPDATE appointments SET data_ciphertext = ?, appointment_date = ?,
                        appointment_time = ?, appointment_type_id = ?, professional = ?,
                        professional_key = ?, status = ?, updated_at = ? WHERE id = ?"""),
                    (appointment["data_ciphertext"], appointment["appointment_date"],
                     appointment["appointment_time"], appointment["appointment_type_id"],
                     appointment["professional"], appointment["professional_key"],
                     appointment["status"], appointment["updated_at"], appointment_id),
                )
                if cursor.rowcount != 1:
                    return False
                self._replace_appointment_search_terms(connection, appointment_id, search_tokens)
                for event in events:
                    self._add_appointment_event(connection, appointment_id, event["event_type"],
                                                event["actor"], event["description"], event["created_at"])
                return True
        except Exception as error:
            if self._is_unique_conflict(error):
                raise AppointmentConflictError from error
            raise

    def appointment_events(self, appointment_id):
        with self.connect() as connection:
            rows = connection.execute(
                self._sql("""SELECT event_type, actor, description, created_at
                    FROM appointment_events WHERE appointment_id = ? ORDER BY created_at, id"""),
                (appointment_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def soft_delete_appointment(self, appointment_id, actor, timestamp):
        with self.connect() as connection:
            current = connection.execute(
                self._sql("SELECT status FROM appointments WHERE id = ? AND deleted_at IS NULL"),
                (appointment_id,),
            ).fetchone()
            if not current:
                return False
            cursor = connection.execute(
                self._sql("""UPDATE appointments SET status = ?, deleted_at = ?, updated_at = ?
                    WHERE id = ? AND deleted_at IS NULL"""),
                ("CANCELADO", timestamp, timestamp, appointment_id),
            )
            if cursor.rowcount != 1:
                return False
            self._add_appointment_event(connection, appointment_id, "EXCLUIDO", actor,
                                        f"Agendamento removido da agenda; status anterior: {current['status']}", timestamp)
            return True

    def add_appointment_event(self, appointment_id, event_type, actor, description, created_at):
        with self.connect() as connection:
            exists = connection.execute(self._sql("SELECT id FROM appointments WHERE id = ?"),
                                        (appointment_id,)).fetchone()
            if not exists:
                return False
            self._add_appointment_event(connection, appointment_id, event_type, actor, description, created_at)
            return True

    def appointment_professionals(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT professional FROM appointments WHERE deleted_at IS NULL ORDER BY professional").fetchall()
        return [row["professional"] for row in rows]

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

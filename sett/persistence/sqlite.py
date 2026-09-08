"""SQLite reference backend for durable SETT evidence and state records."""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Self
from uuid import uuid4

from sett.exceptions import (
    SETTConfigurationError,
    SETTIdempotencyConflictError,
    SETTPersistenceContinuityError,
    SETTPersistenceError,
    SETTPersistencePositionError,
    SETTValidationError,
)
from sett.persistence.contracts import (
    DurabilityDeclaration,
    DurabilityLevel,
    DurabilityReceipt,
    PersistenceBackend,
    PersistenceHealth,
    PersistenceHealthStatus,
    StateRecord,
    StateRecordReceipt,
    StreamEntry,
    StreamPosition,
    StreamReceipt,
    validate_position_identity,
    validate_stream_position,
)

_GENESIS_TOKEN = "0000000000000000"
_SYNCHRONOUS_NAMES = {0: "OFF", 1: "NORMAL", 2: "FULL", 3: "EXTRA"}

# One stable public sentence for every native SQLite infrastructure failure.
# The operation is carried by the reason code and the backend by
# ``backend_id``, so nothing here has to be parsed out of the message (#13),
# and the driver's own text - which routinely contains the database path -
# stays where only a local traceback can reach it, on ``__cause__``.
_INFRASTRUCTURE_FAILURE_MESSAGE = "SQLite persistence operation failed."


@contextmanager
def _normalized_sqlite_failures(
    operation: str, *, backend_id: str | None
) -> Iterator[None]:
    """Raise SQLite infrastructure failures inside the persistence hierarchy.

    ``SQLitePersistenceBackend`` is the reference implementation of the
    persistence contract, so an application that catches
    ``SETTPersistenceError`` must not silently miss a failure that the
    official backend itself produced (M-01, defensive and infrastructure
    audit of v0.14.0: opening an unwritable path escaped as a bare
    ``sqlite3.OperationalError``). Catching only ``sqlite3.Error`` keeps
    this a translation, never a catch-all: ``SETTIdempotencyConflictError``,
    ``SETTPersistenceContinuityError``, ``SETTPersistencePositionError``,
    ``SETTConfigurationError`` and ``SETTValidationError`` are raised by
    this module itself, are not ``sqlite3`` errors, and keep both their
    types and their existing semantics.

    Args:
        operation: Backend operation whose failure is being normalized.
            It becomes the specific half of the stable reason code.
        backend_id: Backend the failure is attributable to. Available
            before initialization completes, so an ``open()`` failure is
            attributed like any other.
    """
    try:
        yield
    except sqlite3.Error as error:
        raise SETTPersistenceError(
            _INFRASTRUCTURE_FAILURE_MESSAGE,
            reason_code=f"persistence.sqlite.{operation}_failed",
            backend_id=backend_id,
        ) from error


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _validate_write(write_id: str, payload: bytes) -> None:
    if not isinstance(write_id, str) or not write_id:
        raise SETTValidationError("write_id must be a non-empty string.")
    if not isinstance(payload, bytes):
        raise SETTValidationError("Persistence payload must be bytes.")


class _SQLiteDatabase:
    def __init__(
        self,
        path: str | Path,
        *,
        backend_id: str,
        namespace_id: str,
        journal_mode: str,
        synchronous: str,
    ) -> None:
        if not isinstance(backend_id, str) or not backend_id:
            raise SETTValidationError("backend_id must be a non-empty string.")
        if not isinstance(namespace_id, str) or not namespace_id:
            raise SETTValidationError("namespace_id must be a non-empty string.")
        self.backend_id = backend_id
        self.namespace_id = namespace_id
        self.path = Path(path)
        self.lock = threading.RLock()
        with _normalized_sqlite_failures("open", backend_id=backend_id):
            self.connection = sqlite3.connect(
                str(self.path), isolation_level=None, check_same_thread=False
            )
        try:
            with _normalized_sqlite_failures("open", backend_id=backend_id):
                self.connection.execute("PRAGMA foreign_keys=ON")
            requested_journal = journal_mode.strip().upper()
            requested_synchronous = synchronous.strip().upper()
            if requested_journal not in {"DELETE", "TRUNCATE", "PERSIST", "WAL"}:
                raise SETTValidationError("Unsupported SQLite journal_mode.")
            if requested_synchronous not in set(_SYNCHRONOUS_NAMES.values()):
                raise SETTValidationError("Unsupported SQLite synchronous mode.")
            with _normalized_sqlite_failures("open", backend_id=backend_id):
                self.connection.execute(f"PRAGMA journal_mode={requested_journal}")
                self.connection.execute(f"PRAGMA synchronous={requested_synchronous}")
                effective_journal = str(
                    self.connection.execute("PRAGMA journal_mode").fetchone()[0]
                ).upper()
                synchronous_value = int(
                    self.connection.execute("PRAGMA synchronous").fetchone()[0]
                )
                self.journal_mode = effective_journal
                self.synchronous = _SYNCHRONOUS_NAMES.get(
                    synchronous_value, f"UNKNOWN_{synchronous_value}"
                )
                self._create_schema()
                self._bind_backend_identity()
        except Exception:
            self.connection.close()
            raise

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sett_backend (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                backend_id TEXT NOT NULL,
                namespace_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sett_streams (
                stream_id TEXT PRIMARY KEY,
                head_token TEXT NOT NULL,
                sequence INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sett_evidence (
                stream_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                token TEXT NOT NULL,
                write_id TEXT NOT NULL,
                payload BLOB NOT NULL,
                payload_length INTEGER NOT NULL,
                fingerprint BLOB NOT NULL,
                accepted_at TEXT NOT NULL,
                PRIMARY KEY (stream_id, sequence),
                UNIQUE (stream_id, token),
                UNIQUE (stream_id, write_id)
            );
            CREATE TABLE IF NOT EXISTS sett_state (
                write_id TEXT PRIMARY KEY,
                state_ref TEXT NOT NULL UNIQUE,
                payload BLOB NOT NULL,
                payload_length INTEGER NOT NULL,
                fingerprint BLOB NOT NULL,
                accepted_at TEXT NOT NULL
            );
            """
        )

    def _bind_backend_identity(self) -> None:
        row = self.connection.execute(
            "SELECT backend_id, namespace_id FROM sett_backend WHERE singleton = 1"
        ).fetchone()
        if row is None:
            self.connection.execute(
                "INSERT INTO sett_backend(singleton, backend_id, namespace_id) "
                "VALUES (1, ?, ?)",
                (self.backend_id, self.namespace_id),
            )
        elif row[0] != self.backend_id or row[1] != self.namespace_id:
            raise SETTConfigurationError(
                "SQLite persistence file is bound to another backend_id or "
                "namespace_id."
            )

    def durability_level(self) -> DurabilityLevel:
        if str(self.path) == ":memory:":
            return DurabilityLevel.IN_MEMORY
        if self.journal_mode == "WAL" and self.synchronous in {"FULL", "EXTRA"}:
            return DurabilityLevel.POWER_LOSS
        if (
            self.journal_mode in {"DELETE", "TRUNCATE", "PERSIST"}
            and self.synchronous == "EXTRA"
        ):
            return DurabilityLevel.POWER_LOSS
        return DurabilityLevel.PROCESS_CRASH

    def durability(self) -> DurabilityDeclaration:
        level = self.durability_level()
        return DurabilityDeclaration(
            maximum_durability=level,
            observed_at=_now(),
            reason_codes=(
                f"persistence.sqlite.journal_mode.{self.journal_mode.lower()}",
                f"persistence.sqlite.synchronous.{self.synchronous.lower()}",
            ),
        )

    def health(self) -> PersistenceHealth:
        try:
            with self.lock:
                self.connection.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            status = PersistenceHealthStatus.UNAVAILABLE
            reasons = ("persistence.sqlite.read_failed",)
        else:
            status = PersistenceHealthStatus.HEALTHY
            reasons = ()
        return PersistenceHealth(
            status=status,
            observed_at=_now(),
            reason_codes=reasons,
        )

    def close(self) -> None:
        with self.lock, _normalized_sqlite_failures(
            "close", backend_id=self.backend_id
        ):
            self.connection.close()


class SQLiteEvidenceStream:
    """Transactional evidence stream over one SQLite database."""

    def __init__(self, database: _SQLiteDatabase, *, stream_id: str) -> None:
        """Bind one logical recorder stream to the shared database."""
        if not isinstance(stream_id, str) or not stream_id:
            raise SETTValidationError("stream_id must be a non-empty string.")
        self._database = database
        self._backend_id = database.backend_id
        self._stream_id = stream_id
        validate_stream_position(
            StreamPosition(self._backend_id, self._stream_id, _GENESIS_TOKEN)
        )
        with database.lock, _normalized_sqlite_failures(
            "stream_registration", backend_id=self._backend_id
        ):
            database.connection.execute(
                "INSERT OR IGNORE INTO sett_streams(stream_id, head_token, sequence) "
                "VALUES (?, ?, 0)",
                (stream_id, _GENESIS_TOKEN),
            )

    def head(self) -> StreamPosition:
        """Read the current stable token from the stream metadata row."""
        with self._database.lock, _normalized_sqlite_failures(
            "head_read", backend_id=self._backend_id
        ):
            row = self._database.connection.execute(
                "SELECT head_token FROM sett_streams WHERE stream_id = ?",
                (self._stream_id,),
            ).fetchone()
        assert row is not None
        return StreamPosition(self._backend_id, self._stream_id, row[0])

    def append(
        self,
        *,
        write_id: str,
        payload: bytes,
        expected_head: StreamPosition,
    ) -> StreamReceipt:
        """Append atomically or replay identical bytes by write identity."""
        _validate_write(write_id, payload)
        validate_position_identity(
            expected_head,
            backend_id=self._backend_id,
            stream_id=self._stream_id,
        )
        fingerprint = sha256(payload).digest()
        connection = self._database.connection
        with self._database.lock, _normalized_sqlite_failures(
            "append", backend_id=self._backend_id
        ):
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT token, payload_length, fingerprint, accepted_at "
                    "FROM sett_evidence WHERE stream_id = ? AND write_id = ?",
                    (self._stream_id, write_id),
                ).fetchone()
                if existing is not None:
                    token, payload_length, prior_fingerprint, accepted_at = existing
                    if payload_length != len(payload) or prior_fingerprint != fingerprint:
                        raise SETTIdempotencyConflictError(
                            "write_id was already used for different evidence bytes.",
                            key=write_id,
                            expected_fingerprint=bytes(prior_fingerprint).hex(),
                            actual_fingerprint=fingerprint.hex(),
                        )
                    connection.execute("COMMIT")
                    return StreamReceipt(
                        write_id=write_id,
                        position=StreamPosition(
                            self._backend_id, self._stream_id, token
                        ),
                        backend_id=self._backend_id,
                        achieved_durability=self._database.durability_level(),
                        accepted_at=_parse_time(accepted_at),
                        acquired=False,
                    )
                current_head, sequence = connection.execute(
                    "SELECT head_token, sequence FROM sett_streams "
                    "WHERE stream_id = ?",
                    (self._stream_id,),
                ).fetchone()
                if expected_head.token != current_head:
                    raise SETTPersistenceContinuityError(
                        "Evidence append expected a stale stream head.",
                        reason_code="persistence.stream_continuity_conflict",
                        backend_id=self._backend_id,
                    )
                next_sequence = sequence + 1
                token = f"{next_sequence:016d}"
                accepted_at = _now()
                connection.execute(
                    "INSERT INTO sett_evidence(" 
                    "stream_id, sequence, token, write_id, payload, "
                    "payload_length, fingerprint, accepted_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._stream_id,
                        next_sequence,
                        token,
                        write_id,
                        payload,
                        len(payload),
                        fingerprint,
                        accepted_at.isoformat(),
                    ),
                )
                connection.execute(
                    "UPDATE sett_streams SET head_token = ?, sequence = ? "
                    "WHERE stream_id = ?",
                    (token, next_sequence, self._stream_id),
                )
                connection.execute("COMMIT")
                return StreamReceipt(
                    write_id=write_id,
                    position=StreamPosition(self._backend_id, self._stream_id, token),
                    backend_id=self._backend_id,
                    achieved_durability=self._database.durability_level(),
                    accepted_at=accepted_at,
                    acquired=True,
                )
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _sequence_for(self, position: StreamPosition) -> int:
        validate_position_identity(
            position,
            backend_id=self._backend_id,
            stream_id=self._stream_id,
        )
        if position.token == _GENESIS_TOKEN:
            return 0
        row = self._database.connection.execute(
            "SELECT sequence FROM sett_evidence WHERE stream_id = ? AND token = ?",
            (self._stream_id, position.token),
        ).fetchone()
        if row is None:
            raise SETTPersistencePositionError(
                "The stream position is not part of this lineage.",
                reason_code="persistence.position_unknown",
                backend_id=self._backend_id,
            )
        return int(row[0])

    def read(self, *, after: StreamPosition | None = None) -> tuple[StreamEntry, ...]:
        """Read one contiguous suffix in committed sequence order."""
        with self._database.lock, _normalized_sqlite_failures(
            "read", backend_id=self._backend_id
        ):
            sequence = 0 if after is None else self._sequence_for(after)
            rows = self._database.connection.execute(
                "SELECT write_id, token, payload FROM sett_evidence "
                "WHERE stream_id = ? AND sequence > ? ORDER BY sequence",
                (self._stream_id, sequence),
            ).fetchall()
        return tuple(
            StreamEntry(
                write_id=write_id,
                position=StreamPosition(self._backend_id, self._stream_id, token),
                payload=bytes(payload),
            )
            for write_id, token, payload in rows
        )

    def flush(self, *, through: StreamPosition) -> DurabilityReceipt:
        """Confirm the effective SQLite level through a committed position."""
        with self._database.lock, _normalized_sqlite_failures(
            "flush", backend_id=self._backend_id
        ):
            self._sequence_for(through)
        return DurabilityReceipt(
            backend_id=self._backend_id,
            through=through,
            achieved_durability=self._database.durability_level(),
            confirmed_at=_now(),
        )

    def durability(self) -> DurabilityDeclaration:
        """Report the level derived from effective SQLite pragmas."""
        return self._database.durability()

    def health(self) -> PersistenceHealth:
        """Return a passive read-only observation of the shared database."""
        return self._database.health()


class SQLiteStateRecordStore:
    """Transactional random-access record store over SQLite."""

    def __init__(self, database: _SQLiteDatabase) -> None:
        """Bind random-access records to the shared database."""
        self._database = database
        self._backend_id = database.backend_id

    def put(self, *, write_id: str, payload: bytes) -> StateRecordReceipt:
        """Write record and identity index in one transaction, or replay."""
        _validate_write(write_id, payload)
        fingerprint = sha256(payload).digest()
        connection = self._database.connection
        with self._database.lock, _normalized_sqlite_failures(
            "state_write", backend_id=self._backend_id
        ):
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT state_ref, payload_length, fingerprint, accepted_at "
                    "FROM sett_state WHERE write_id = ?",
                    (write_id,),
                ).fetchone()
                if existing is not None:
                    state_ref, payload_length, prior_fingerprint, accepted_at = existing
                    if payload_length != len(payload) or prior_fingerprint != fingerprint:
                        raise SETTIdempotencyConflictError(
                            "write_id was already used for different state bytes.",
                            key=write_id,
                            expected_fingerprint=bytes(prior_fingerprint).hex(),
                            actual_fingerprint=fingerprint.hex(),
                        )
                    connection.execute("COMMIT")
                    return StateRecordReceipt(
                        write_id=write_id,
                        state_ref=state_ref,
                        backend_id=self._backend_id,
                        achieved_durability=self._database.durability_level(),
                        accepted_at=_parse_time(accepted_at),
                        acquired=False,
                    )
                state_ref = f"state-{uuid4()}"
                accepted_at = _now()
                connection.execute(
                    "INSERT INTO sett_state(" 
                    "write_id, state_ref, payload, payload_length, fingerprint, accepted_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        write_id,
                        state_ref,
                        payload,
                        len(payload),
                        fingerprint,
                        accepted_at.isoformat(),
                    ),
                )
                connection.execute("COMMIT")
                return StateRecordReceipt(
                    write_id=write_id,
                    state_ref=state_ref,
                    backend_id=self._backend_id,
                    achieved_durability=self._database.durability_level(),
                    accepted_at=accepted_at,
                    acquired=True,
                )
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def lookup(self, *, write_id: str) -> StateRecordReceipt | None:
        """Read an existing write receipt without modifying the database."""
        if not isinstance(write_id, str) or not write_id:
            raise SETTValidationError("write_id must be a non-empty string.")
        with self._database.lock, _normalized_sqlite_failures(
            "state_lookup", backend_id=self._backend_id
        ):
            row = self._database.connection.execute(
                "SELECT state_ref, accepted_at FROM sett_state WHERE write_id = ?",
                (write_id,),
            ).fetchone()
        if row is None:
            return None
        return StateRecordReceipt(
            write_id=write_id,
            state_ref=row[0],
            backend_id=self._backend_id,
            achieved_durability=self._database.durability_level(),
            accepted_at=_parse_time(row[1]),
            acquired=False,
        )

    def get(self, *, state_ref: str) -> StateRecord:
        """Read exact opaque bytes addressed by a stable state reference."""
        if not isinstance(state_ref, str) or not state_ref:
            raise SETTValidationError("state_ref must be a non-empty string.")
        with self._database.lock, _normalized_sqlite_failures(
            "state_read", backend_id=self._backend_id
        ):
            row = self._database.connection.execute(
                "SELECT payload FROM sett_state WHERE state_ref = ?",
                (state_ref,),
            ).fetchone()
        if row is None:
            raise SETTPersistenceError(
                "State record was not found.",
                reason_code="state.record_missing",
                backend_id=self._backend_id,
            )
        return StateRecord(state_ref=state_ref, payload=bytes(row[0]))

    def durability(self) -> DurabilityDeclaration:
        """Report the level derived from effective SQLite pragmas."""
        return self._database.durability()

    def health(self) -> PersistenceHealth:
        """Return a passive read-only observation of the shared database."""
        return self._database.health()


@dataclass(frozen=True, slots=True)
class SQLitePersistenceBackend(PersistenceBackend):
    """Reference backend binding one namespace to one SQLite file.

    This backend does not set, enforce, or verify filesystem permissions on
    the files it writes, and it does not encrypt them. Persisted evidence,
    stage outputs, ``PrivateMemory`` state, and authorization evidence can
    carry application data; protecting them is the deploying application's
    responsibility (M-02, defensive and infrastructure audit of v0.14.0).

    Protect the containing directory, not the files. Opening the database
    can create ``-wal`` and ``-shm`` sidecars, a clean ``close()`` removes
    them, and the next ``open()`` recreates them from whatever the process
    environment is at that moment. Permissions applied to a sidecar do not
    survive that cycle; permissions applied to the directory are inherited
    by every file SQLite later creates inside it.

    ``docs/security_model.md`` carries the observed modes on POSIX and
    Windows and the recommended deployment procedure.
    """

    namespace_id: str
    evidence: SQLiteEvidenceStream
    state: SQLiteStateRecordStore
    _database: _SQLiteDatabase

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        namespace_id: str,
        backend_id: str,
        stream_id: str = "evidence.main",
        journal_mode: str = "WAL",
        synchronous: str = "FULL",
    ) -> SQLitePersistenceBackend:
        """Open and validate effective SQLite durability settings.

        The path is used as given. Create and restrict its parent directory
        before calling this; see the class docstring and
        ``docs/security_model.md``. Infrastructure failures - an unwritable
        path included - raise ``SETTPersistenceError`` with reason code
        ``persistence.sqlite.open_failed`` and the driver's exception as
        ``__cause__``.
        """
        if not isinstance(namespace_id, str) or not namespace_id:
            raise SETTValidationError("namespace_id must be a non-empty string.")
        database = _SQLiteDatabase(
            path,
            backend_id=backend_id,
            namespace_id=namespace_id,
            journal_mode=journal_mode,
            synchronous=synchronous,
        )
        backend = cls(
            namespace_id=namespace_id,
            evidence=SQLiteEvidenceStream(database, stream_id=stream_id),
            state=SQLiteStateRecordStore(database),
            _database=database,
        )
        PersistenceBackend(
            namespace_id=namespace_id,
            evidence=backend.evidence,
            state=backend.state,
        )
        return backend

    @property
    def effective_journal_mode(self) -> str:
        """The journal mode read back from SQLite, never the requested value."""
        return self._database.journal_mode

    @property
    def effective_synchronous(self) -> str:
        """The synchronous mode read back from SQLite."""
        return self._database.synchronous

    def close(self) -> None:
        """Close the shared SQLite connection."""
        self._database.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

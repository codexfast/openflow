import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from flow_mcp.models import Scope


class Jobs:
    def __init__(self, path: Path | None = None) -> None:
        self.db = sqlite3.connect(str(path) if path else ":memory:")
        self._key = self._load_key(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS jobs "
            "(id TEXT PRIMARY KEY, connection TEXT NOT NULL, scope TEXT NOT NULL, "
            "operation TEXT NOT NULL, input TEXT NOT NULL, state TEXT NOT NULL, "
            "result TEXT NOT NULL, updated TEXT NOT NULL)"
        )
        self.db.execute("UPDATE jobs SET state='submitted_unverified' WHERE state='submitting'")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}
        if "account_binding" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN account_binding TEXT")
        self.db.commit()

    @staticmethod
    def _load_key(path: Path | None) -> bytes:
        if path is None or str(path) == ":memory:":
            return secrets.token_bytes(32)
        key_path = path.with_name(path.name + ".identity-key")
        if key_path.is_symlink():
            raise ValueError("identity_key_must_not_be_symlink")
        try:
            # POSIX mode 0600; Windows uses the containing directory's ACL.
            fd = os.open(
                key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            if key_path.is_symlink():
                raise ValueError("identity_key_must_not_be_symlink") from None
            with key_path.open("rb") as stream:
                key = stream.read(33)
            if len(key) != 32:
                raise ValueError("invalid_identity_key") from None
            return key
        try:
            key = secrets.token_bytes(32)
            with os.fdopen(fd, "wb") as stream:
                stream.write(key)
            return key
        except BaseException:
            key_path.unlink(missing_ok=True)
            raise

    def account_binding(self, email: str) -> str:
        return hmac.new(self._key, email.lower().encode(), hashlib.sha256).hexdigest()

    def bound_to_account(self, identity: str, email: str) -> bool:
        row = self.db.execute(
            "SELECT account_binding FROM jobs WHERE id=?", (identity,)
        ).fetchone()
        if row is None:
            raise ValueError("job_not_found")
        return isinstance(row[0], str) and hmac.compare_digest(
            row[0], self.account_binding(email)
        )

    def has_account_binding(self, identity: str) -> bool:
        row = self.db.execute(
            "SELECT account_binding FROM jobs WHERE id=?", (identity,)
        ).fetchone()
        if row is None:
            raise ValueError("job_not_found")
        return isinstance(row[0], str)

    def create(self, connection: str, scope: Scope, operation: str, value: dict[str, Any],
               account_email: str | None = None) -> str:
        identity = str(uuid4())
        self.db.execute(
            "INSERT INTO jobs (id, connection, scope, operation, input, state, result, "
            "updated, account_binding) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                identity,
                connection,
                scope.model_dump_json(),
                operation,
                json.dumps(value),
                "preview",
                "{}",
                datetime.now(UTC).isoformat(),
                self.account_binding(account_email) if account_email else None,
            ),
        )
        self.db.commit()
        return identity

    def get(self, identity: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT id, connection, scope, operation, input, state, result, updated "
            "FROM jobs WHERE id=?", (identity,)
        ).fetchone()
        if row is None:
            raise ValueError("job_not_found")
        return dict(
            zip(
                (
                    "job_id",
                    "connection_id",
                    "scope",
                    "operation",
                    "input",
                    "state",
                    "result",
                    "updated",
                ),
                (
                    row[0],
                    row[1],
                    json.loads(row[2]),
                    row[3],
                    json.loads(row[4]),
                    row[5],
                    json.loads(row[6]),
                    row[7],
                ),
                strict=True,
            )
        )

    def update(self, identity: str, state: str, result: dict[str, Any]) -> None:
        self.db.execute(
            "UPDATE jobs SET state=?, result=?, updated=? WHERE id=?",
            (state, json.dumps(result), datetime.now(UTC).isoformat(), identity),
        )
        self.db.commit()

    def update_if_state(
        self, identity: str, expected: str, state: str, result: dict[str, Any]
    ) -> bool:
        changed = self.db.execute(
            "UPDATE jobs SET state=?, result=?, updated=? WHERE id=? AND state=?",
            (state, json.dumps(result), datetime.now(UTC).isoformat(), identity, expected),
        ).rowcount
        self.db.commit()
        return changed == 1

    def owns(self, connection: str, scope: Scope, media: str, kind: str = "image") -> bool:
        rows = self.db.execute(
            "SELECT result FROM jobs WHERE connection=? AND scope=? AND state='completed'",
            (connection, scope.model_dump_json()),
        )
        return any(
            asset.get("media_id") == media and asset.get("kind") == kind
            for (value,) in rows
            for asset in json.loads(value).get("assets", [])
        )

    def delete_in_flight(self, connection: str, project_id: str) -> bool:
        rows = self.db.execute(
            "SELECT input FROM jobs WHERE connection=? AND operation='delete_project' "
            "AND state IN ('submitting', 'submitted_unverified')", (connection,),
        )
        return any(json.loads(value).get("project_id") == project_id for (value,) in rows)

    def create_delete_preview(
        self, connection: str, scope: Scope, value: dict[str, Any]
    ) -> str:
        try:
            self.db.execute("BEGIN IMMEDIATE")
            if self.delete_in_flight(connection, value["project_id"]):
                raise ValueError("delete_project_outcome_unknown_do_not_retry")
            return self.create(connection, scope, "delete_project", value)
        except BaseException:
            self.db.rollback()
            raise

    def claim_delete(self, identity: str, connection: str, project_id: str) -> bool:
        """One-use claim, including target exclusion across SQLite connections."""
        try:
            self.db.execute("BEGIN IMMEDIATE")
            if self.delete_in_flight(connection, project_id):
                raise ValueError("delete_project_outcome_unknown_do_not_retry")
            changed = self.db.execute(
                "UPDATE jobs SET state='submitting', updated=? "
                "WHERE id=? AND connection=? AND operation='delete_project' AND state='preview'",
                (datetime.now(UTC).isoformat(), identity, connection),
            ).rowcount
            self.db.commit()
            return changed == 1
        except BaseException:
            self.db.rollback()
            raise

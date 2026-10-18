"""Expiring artifact persistence; renderer is injected by server."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from urllib.parse import urlsplit

from learning_coach.service.ports import VisualRenderer
from .storage_errors import storage_boundary

_TOKEN = re.compile(r"[A-Za-z0-9_-]{32}\Z")

class ArtifactStore:
    """Store prepared visuals after acceptance; tokens expire 24h after commit.

    A token is a bearer capability, not an authenticated user identity. Owners
    are hashed session identifiers supplied by application code, never the model.
    SQLite indexes survive restarts; only indexed files are readable. Prepared
    objects are accepted only by the instance that created them.
    """

    TTL_SECONDS = 24 * 60 * 60

    def __init__(self, root, public_base_url: str = "http://localhost:8088", *, renderer: VisualRenderer):
        self.renderer = renderer
        parsed = urlsplit(public_base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or any(c.isspace() for c in public_base_url)):
            raise ValueError("invalid_public_base_url")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.public_base_url = public_base_url.rstrip("/")
        self._lock = threading.RLock()
        self._prepared = {}
        with self._connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS artifacts "
                       "(token TEXT PRIMARY KEY, session_hash TEXT NOT NULL, expires_at REAL NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS artifacts_session ON artifacts(session_hash)")
        self._cleanup_expired()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.root / "index.sqlite3", timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _owner(session_id: str) -> str:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("invalid_session_id")
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()

    def _cleanup_expired(self):
        with self._lock:
            now = time.time()
            with self._connection() as db:
                expired = db.execute("SELECT token FROM artifacts WHERE expires_at <= ?", (now,)).fetchall()
                # Keep the index if removal fails so a later cleanup can retry.
                for (token,) in expired:
                    if _TOKEN.fullmatch(token):
                        (self.root / (token + ".html")).unlink(missing_ok=True)
                db.execute("DELETE FROM artifacts WHERE expires_at <= ?", (now,))
            self._prepared = {token: item for token, item in self._prepared.items() if item[4] > now}

    @storage_boundary
    def prepare(self, session_id: str, spec: dict, image: bytes | None = None, photos: list | None = None) -> dict:
        owner = self._owner(session_id)
        document = (self.renderer(spec, image=image) if photos is None
                    else self.renderer(spec, image=image, photos=photos))
        with self._lock:
            for _ in range(10):
                token = secrets.token_urlsafe(24)
                if not _TOKEN.fullmatch(token):
                    raise ValueError("invalid_generated_token")
                with self._connection() as db:
                    exists = db.execute("SELECT 1 FROM artifacts WHERE token = ?", (token,)).fetchone()
                if token not in self._prepared and exists is None:
                    break
            else:
                raise RuntimeError("artifact_token_collision")
            url = self.public_base_url + "/artifacts/" + token
            self._prepared[token] = (
                owner, hashlib.sha256(document.encode("utf-8")).hexdigest(),
                spec["title"], url, time.time() + self.TTL_SECONDS,
            )
        return {"token": token, "url": url, "title": spec["title"], "html": document}

    @storage_boundary
    def commit(self, session_id: str, artifact: dict) -> None:
        owner = self._owner(session_id)
        if (not isinstance(artifact, dict) or set(artifact) != {"token", "url", "title", "html"}
                or not all(isinstance(value, str) for value in artifact.values())
                or not _TOKEN.fullmatch(artifact["token"])):
            raise ValueError("invalid_prepared_artifact")
        token = artifact["token"]
        with self._lock:
            self._cleanup_expired()
            prepared = self._prepared.get(token)
            if (prepared is None or prepared[0] != owner
                    or prepared[1] != hashlib.sha256(artifact["html"].encode("utf-8")).hexdigest()
                    or prepared[2] != artifact["title"] or prepared[3] != artifact["url"]):
                raise ValueError("artifact_not_prepared_for_session")
            path = self.root / (token + ".html")
            temporary = None
            try:
                with self._connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    if db.execute("SELECT 1 FROM artifacts WHERE token = ?", (token,)).fetchone():
                        raise ValueError("artifact_already_committed")
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root,
                                                     prefix=".artifact-", suffix=".tmp", delete=False) as handle:
                        temporary = Path(handle.name)
                        handle.write(artifact["html"])
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, path)
                    temporary = None
                    db.execute("INSERT INTO artifacts(token, session_hash, expires_at) VALUES (?, ?, ?)",
                               (token, owner, time.time() + self.TTL_SECONDS))
                self._prepared.pop(token, None)
            except BaseException:
                # A failed commit must not leave a readable, partially accepted visual.
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                with self._connection() as db:
                    accepted = db.execute("SELECT 1 FROM artifacts WHERE token = ?", (token,)).fetchone()
                if accepted is None:
                    path.unlink(missing_ok=True)
                raise

    @storage_boundary
    def get(self, token: str) -> str | None:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            return None
        with self._lock:
            self._cleanup_expired()
            with self._connection() as db:
                found = db.execute("SELECT 1 FROM artifacts WHERE token = ? AND expires_at > ?",
                                   (token, time.time())).fetchone()
            if found is None:
                return None
            path = self.root / (token + ".html")
            if path.is_symlink():
                return None
            try:
                return path.read_text(encoding="utf-8")
            except (FileNotFoundError, UnicodeDecodeError):
                return None

    @storage_boundary
    def delete_session(self, session_id: str) -> None:
        owner = self._owner(session_id)
        with self._lock:
            # Pending artifacts are revoked even when disk cleanup needs a retry.
            self._prepared = {token: item for token, item in self._prepared.items() if item[0] != owner}
            with self._connection() as db:
                rows = db.execute("SELECT token FROM artifacts WHERE session_hash = ?", (owner,)).fetchall()
                for (token,) in rows:
                    if _TOKEN.fullmatch(token):
                        (self.root / (token + ".html")).unlink(missing_ok=True)
                db.execute("DELETE FROM artifacts WHERE session_hash = ?", (owner,))

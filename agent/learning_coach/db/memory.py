"""Opt-in, session-scoped learning notes. Scores are heuristics, not measurements.

Model output supplies grounded notes only. Assessment evidence is supplied by the
application, never extracted from the model's notes or the learner's claims.
Trusted independent quiz evidence starts mastery from a neutral floor; a correct
independent restatement sets a higher diagnostic baseline. Untrusted, companion,
or unconfirmed evidence never moves the score.
"""

from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any


from learning_coach.domain.mastery import update_mastery
from learning_coach.domain.note_contracts import (NOTES_SCHEMA, SCHEMA_VERSION, ERROR_CATEGORIES,
                                                 LEGACY_ERROR_CATEGORIES)
from learning_coach.db.storage_errors import storage_boundary

DATABASE_VERSION = "1"
MAX_IDENTIFIER_CHARS = 512
MAX_NOTE_TEXT_CHARS = 100_000   # used when a schema string has no maxLength
LOCK_WAIT_SECONDS = 10         # how long SQLite waits for another writer


def _validate(value: Any, schema: dict[str, Any], field: str = "notes") -> None:
    """Validate the small schema above without an optional runtime dependency."""
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                _validate(value, option, field)
                return
            except ValueError:
                pass
        raise ValueError(f"{field}: invalid union value")
    kind = schema.get("type")
    expected = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}
    if kind in expected and type(value) is not expected[kind]:
        raise ValueError(f"{field}: expected {kind}")
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{field}: unsupported version")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{field}: unsupported value")
    if kind == "string" and (not value.strip() or len(value) > schema.get("maxLength", MAX_NOTE_TEXT_CHARS)):
        raise ValueError(f"{field}: empty or overlong string")
    if kind == "object":
        if not set(schema["required"]) <= set(value) or not set(value) <= set(schema["properties"]):
            raise ValueError(f"{field}: missing or extra fields")
        for key, child in schema["properties"].items():
            if key in value:
                _validate(value[key], child, f"{field}.{key}")
    if kind == "array":
        if len(value) > schema["maxItems"]:
            raise ValueError(f"{field}: too many entries")
        for item in value:
            _validate(item, schema["items"], field)


class LearningStore:
    """A database path comes from application configuration, not model output.

    Recording is disabled for new sessions. Disabling retains prior data but also
    hides it from snapshot(); clear() deletes all session content and consent.
    A successful apply_notes() transaction is idempotent by session and turn ID.
    """

    def __init__(self, db_path: str | Path):
        self._lock = RLock()
        if str(db_path) != ":memory:":
            Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            db_path = str(Path(db_path).expanduser())
        self._db = sqlite3.connect(str(db_path), timeout=LOCK_WAIT_SECONDS, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout = 10000")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS session_settings (session_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS applied_turns (
                session_id TEXT NOT NULL, turn_id TEXT NOT NULL, PRIMARY KEY (session_id, turn_id));
            CREATE TABLE IF NOT EXISTS profiles (
                session_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                origin TEXT NOT NULL, evidence_quote TEXT NOT NULL, turn_id TEXT NOT NULL,
                PRIMARY KEY (session_id, key));
            CREATE TABLE IF NOT EXISTS concepts (
                session_id TEXT NOT NULL, concept_id TEXT NOT NULL, label TEXT NOT NULL,
                prerequisites TEXT, related TEXT, evidence_quote TEXT, mastery_level REAL,
                attempts INTEGER NOT NULL DEFAULT 0, correct_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (session_id, concept_id));
            CREATE TABLE IF NOT EXISTS error_records (
                id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                concept_id TEXT NOT NULL, category TEXT NOT NULL, evidence_quote TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS review_items (
                session_id TEXT NOT NULL, topic TEXT NOT NULL, deadline TEXT,
                evidence_quote TEXT NOT NULL, turn_id TEXT NOT NULL, PRIMARY KEY (session_id, topic));
            CREATE TABLE IF NOT EXISTS quiz_events (
                id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                concept_id TEXT NOT NULL, kind TEXT NOT NULL, correct INTEGER NOT NULL,
                difficulty TEXT NOT NULL, error_category TEXT NOT NULL,
                UNIQUE (session_id, turn_id));
        """)
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS learning_cards (
                session_id TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL,
                body TEXT NOT NULL, PRIMARY KEY(session_id,id));
            CREATE TABLE IF NOT EXISTS retry_events (
                session_id TEXT NOT NULL, event_id TEXT NOT NULL, card_id TEXT NOT NULL,
                choice TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(session_id,event_id));
            CREATE TABLE IF NOT EXISTS learning_reveals (
                session_id TEXT NOT NULL, card_id TEXT NOT NULL, PRIMARY KEY(session_id,card_id));
        """)
        self._db.execute("INSERT OR IGNORE INTO metadata VALUES ('database_version', ?)", (DATABASE_VERSION,))
        version = self._db.execute("SELECT value FROM metadata WHERE key = 'database_version'").fetchone()[0]
        if version != DATABASE_VERSION:
            self._db.close()
            raise ValueError("Unsupported learning database version")
        # Error types were renamed to the four of the product design; convert old rows.
        for legacy, current in LEGACY_ERROR_CATEGORIES.items():
            self._db.execute("UPDATE error_records SET category = ? WHERE category = ?", (current, legacy))
            self._db.execute("UPDATE quiz_events SET error_category = ? WHERE error_category = ?", (current, legacy))

    @storage_boundary
    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _identifier(value: str, name: str) -> None:
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_IDENTIFIER_CHARS:
            raise ValueError(f"{name}: expected a nonempty identifier")

    def _enabled(self, session_id: str) -> bool:
        row = self._db.execute("SELECT enabled FROM session_settings WHERE session_id = ?", (session_id,)).fetchone()
        return bool(row and row[0])

    @storage_boundary
    def is_enabled(self, session_id: str) -> bool:
        self._identifier(session_id, "session_id")
        with self._lock:
            return self._enabled(session_id)

    @storage_boundary
    def set_enabled(self, session_id: str, enabled: bool) -> None:
        self._identifier(session_id, "session_id")
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        with self._lock:
            self._db.execute("INSERT INTO session_settings VALUES (?, ?) ON CONFLICT(session_id) DO UPDATE SET enabled=excluded.enabled", (session_id, int(enabled)))

    @storage_boundary
    def clear(self, session_id: str) -> None:
        self._identifier(session_id, "session_id")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for table in ("profiles", "concepts", "error_records", "review_items", "quiz_events", "applied_turns", "session_settings", "learning_cards", "retry_events", "learning_reveals"):
                    self._db.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    @storage_boundary
    def snapshot(self, session_id: str) -> dict[str, Any]:
        self._identifier(session_id, "session_id")
        with self._lock:
            # A transaction also gives a coherent view when another process writes.
            self._db.execute("BEGIN")
            try:
                notes = self._read_notes(session_id) if self._enabled(session_id) else _empty_notes()
                self._db.execute("COMMIT")
                return notes
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def _read_notes(self, session_id: str) -> dict[str, Any]:
        notes = _empty_notes()
        for row in self._db.execute("SELECT key, value, origin, evidence_quote, turn_id FROM profiles WHERE session_id = ? ORDER BY key", (session_id,)):
            item = dict(row)
            notes["profile"][item.pop("key")] = item
        for row in self._db.execute("SELECT concept_id, label, prerequisites, related, evidence_quote, mastery_level, attempts, correct_count FROM concepts WHERE session_id = ? ORDER BY concept_id", (session_id,)):
            item = dict(row)
            for name in ("prerequisites", "related"):
                item[name] = _from_json(item[name])
            notes["concepts"].append(item)
        notes["error_records"] = [dict(row) for row in self._db.execute("SELECT turn_id, concept_id, category, evidence_quote FROM error_records WHERE session_id = ? ORDER BY id", (session_id,))]
        notes["review_plan"] = [dict(row) for row in self._db.execute("SELECT topic, deadline, evidence_quote, turn_id FROM review_items WHERE session_id = ? ORDER BY topic", (session_id,))]
        return notes

    @storage_boundary
    def apply_notes(self, session_id: str, turn_id: str, notes: dict[str, Any], learner_text: str, quiz_evidence: dict[str, Any] | None = None) -> bool:
        self._identifier(session_id, "session_id")
        self._identifier(turn_id, "turn_id")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if not self._enabled(session_id) or self._db.execute("SELECT 1 FROM applied_turns WHERE session_id = ? AND turn_id = ?", (session_id, turn_id)).fetchone():
                    self._db.execute("ROLLBACK")
                    return False
                _check_notes(notes, learner_text)
                self._write_notes(session_id, turn_id, notes)
                self._apply_evidence(session_id, turn_id, quiz_evidence, learner_text)
                self._db.execute("INSERT INTO applied_turns VALUES (?, ?)", (session_id, turn_id))
                self._db.execute("COMMIT")
                return True
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def _write_notes(self, session_id: str, turn_id: str, notes: dict[str, Any]) -> None:
        for note in notes.get("study_notes", []):
            card_id = hashlib.sha256((turn_id + "\0" + note["concept_id"]).encode()).hexdigest()[:24]
            body = {**note, "id": card_id, "created_at": datetime.now(timezone.utc).isoformat(), "source": "本次教学对话"}
            self._db.execute("INSERT OR IGNORE INTO learning_cards VALUES (?, ?, 'study', ?)", (session_id,card_id,_to_json(body)))
        for item in notes["profile"]:
            self._db.execute("INSERT INTO profiles VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(session_id, key) DO UPDATE SET value=excluded.value, origin=excluded.origin, evidence_quote=excluded.evidence_quote, turn_id=excluded.turn_id", (session_id, item["key"], item["value"], item["origin"], item["evidence_quote"], turn_id))
        for item in notes["concepts"]:
            self._db.execute("""INSERT INTO concepts (session_id, concept_id, label, prerequisites, related, evidence_quote)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(session_id, concept_id) DO UPDATE SET
                label=excluded.label, prerequisites=COALESCE(excluded.prerequisites, concepts.prerequisites),
                related=COALESCE(excluded.related, concepts.related), evidence_quote=excluded.evidence_quote""",
                (session_id, item["concept_id"], item["label"], _to_json(item["prerequisites"]), _to_json(item["related"]), item["evidence_quote"]))
        for item in notes["error_records"]:
            category = item["category"] if item["cause_confirmed"] else "unclassified"
            self._db.execute("INSERT INTO error_records (session_id, turn_id, concept_id, category, evidence_quote) VALUES (?, ?, ?, ?, ?)", (session_id, turn_id, item["concept_id"], category, item["evidence_quote"]))
        for item in notes["review_plan"]:
            self._db.execute("INSERT INTO review_items VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id, topic) DO UPDATE SET deadline=excluded.deadline, evidence_quote=excluded.evidence_quote, turn_id=excluded.turn_id", (session_id, item["topic"], item["deadline"], item["evidence_quote"], turn_id))

    def _apply_evidence(self, session_id: str, turn_id: str, evidence: dict[str, Any] | None, learner_text: str) -> None:
        if evidence is None or not learner_text.strip():
            return
        if not isinstance(evidence, dict):
            raise ValueError("quiz_evidence must be an application evidence object")
        # In particular, bool('false') must never turn a model string into consent.
        if evidence.get("independent") is not True or evidence.get("actor", "learner") != "learner" or evidence.get("skipped") is True:
            return
        if type(evidence.get("correct")) is not bool:
            raise ValueError("quiz_evidence.correct must be a boolean")
        concept_id = evidence.get("concept_id")
        self._identifier(concept_id, "quiz_evidence.concept_id")
        difficulty = evidence.get("difficulty")
        if difficulty not in ("简单", "中等", "困难"):
            raise ValueError("quiz_evidence.difficulty is invalid")
        kind = evidence.get("kind", "quiz")
        if kind not in ("quiz", "restatement"):
            raise ValueError("quiz_evidence.kind is invalid")
        category = evidence.get("error_category", "unclassified")
        if category not in ERROR_CATEGORIES:
            raise ValueError("quiz_evidence.error_category is invalid")
        self._db.execute("INSERT OR IGNORE INTO concepts (session_id, concept_id, label) VALUES (?, ?, ?)", (session_id, concept_id, concept_id))
        current = self._db.execute("SELECT mastery_level FROM concepts WHERE session_id = ? AND concept_id = ?", (session_id, concept_id)).fetchone()[0]
        previous = None
        if kind == "quiz" and not evidence["correct"]:
            previous = self._db.execute("SELECT correct, error_category FROM quiz_events WHERE session_id = ? AND concept_id = ? AND kind = 'quiz' ORDER BY id DESC LIMIT 1", (session_id, concept_id)).fetchone()
        if kind == "quiz" and not evidence["correct"] and evidence.get("question_snapshot"):
            question = evidence["question_snapshot"]
            card_id = hashlib.sha256(turn_id.encode()).hexdigest()[:24]
            body = {"id": card_id, "concept_id": concept_id, "title": evidence.get("topic", concept_id),
                    "question": question["question"], "choices": question["choices"], "image_url": question.get("image_url"),
                    "answer_index": question["answer_index"], "explanation": question["explanation"],
                    "original_choice": evidence["selected"], "category": category,
                    "cause_confirmed": False, "created_at": datetime.now(timezone.utc).isoformat()}
            self._db.execute("INSERT OR IGNORE INTO learning_cards VALUES (?, ?, 'mistake', ?)",
                             (session_id, card_id, _to_json(body)))
        current = update_mastery(current, kind=kind, correct=evidence["correct"],
                                 difficulty=difficulty, category=category, previous=previous)
        self._db.execute("UPDATE concepts SET mastery_level = ?, attempts = attempts + ?, correct_count = correct_count + ? WHERE session_id = ? AND concept_id = ?", (current, int(kind == "quiz"), int(kind == "quiz" and evidence["correct"]), session_id, concept_id))
        self._db.execute("INSERT INTO quiz_events (session_id, turn_id, concept_id, kind, correct, difficulty, error_category) VALUES (?, ?, ?, ?, ?, ?, ?)", (session_id, turn_id, concept_id, kind, int(evidence["correct"]), difficulty, category))


    @storage_boundary
    def learning_view(self, session_id):
        """Public cards only. Private answer keys never enter initial UI data."""
        with self._lock:
            if not self._enabled(session_id):
                return {"enabled": False, "mistakes": [], "study_notes": []}
            mistakes, notes = [], []
            for row in self._db.execute("SELECT kind,body FROM learning_cards WHERE session_id=? ORDER BY rowid", (session_id,)):
                body = json.loads(row["body"])
                if row["kind"] == "study":
                    score = self._db.execute("SELECT attempts,correct_count FROM concepts WHERE session_id=? AND concept_id=?", (session_id,body["concept_id"])).fetchone()
                    body["practice"] = {"attempts": score[0], "correct": score[1]} if score else None
                    notes.append(body)
                    continue
                public = {k: body[k] for k in ("id", "concept_id", "title", "question", "created_at")}
                public["image_url"] = body.get("image_url")
                public["choices"] = [{"id": str(i), "text": text} for i, text in enumerate(body["choices"])]
                events = list(self._db.execute("SELECT result FROM retry_events WHERE session_id=? AND card_id=? ORDER BY rowid", (session_id, body["id"])))
                public["attempts"] = len(events)
                public["status"] = ("重做已答对" if events and json.loads(events[-1][0])["correct"] else "待复习")
                mistakes.append(public)
            return {"enabled": True, "mistakes": mistakes, "study_notes": notes}

    @storage_boundary
    def edit_study_note(self, session_id, card_id, fields):
        from learning_coach.domain.note_contracts import STUDY_SCHEMA
        allowed = {"title", "summary", "explanation", "example", "pitfall", "conditions"}
        if not isinstance(fields, dict) or set(fields) != allowed:
            raise ValueError("invalid_note_fields")
        schema = STUDY_SCHEMA["properties"]["study_notes"]["items"]["properties"]
        for key, value in fields.items():
            _validate(value, schema[key])
        with self._lock:
            if not self._enabled(session_id):
                raise ValueError("records_disabled")
            row = self._db.execute("SELECT body FROM learning_cards WHERE session_id=? AND id=? AND kind='study'", (session_id,card_id)).fetchone()
            if not row:
                raise ValueError("card_not_found")
            body = {**json.loads(row[0]), **fields, "source": "学习者编辑（未经核验）"}
            self._db.execute("UPDATE learning_cards SET body=? WHERE session_id=? AND id=?", (_to_json(body),session_id,card_id))

    @storage_boundary
    def retry_card(self, session_id, card_id, event_id, choice=None, reveal=False):
        """Deterministic, idempotent grading; retries never change mastery."""
        self._identifier(event_id, "event_id")
        with self._lock:
            if not self._enabled(session_id):
                raise ValueError("records_disabled")
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT body FROM learning_cards WHERE session_id=? AND id=? AND kind='mistake'", (session_id, card_id)).fetchone()
                if not row:
                    raise ValueError("card_not_found")
                body = json.loads(row[0])
                if reveal:
                    self._db.execute("INSERT OR IGNORE INTO learning_reveals VALUES (?,?)", (session_id,card_id))
                    result = {"explanation": body["explanation"], "answer": str(body["answer_index"]), "original_choice": str(body["original_choice"]), "category": body["category"], "cause_confirmed": False}
                else:
                    if choice not in [str(i) for i in range(len(body["choices"]))]:
                        raise ValueError("invalid_choice")
                    previous = self._db.execute("SELECT card_id,choice,result FROM retry_events WHERE session_id=? AND event_id=?", (session_id,event_id)).fetchone()
                    if previous:
                        if previous[0] != card_id or previous[1] != choice:
                            raise ValueError("attempt_conflict")
                        result = json.loads(previous[2])
                    else:
                        assisted = bool(self._db.execute("SELECT 1 FROM learning_reveals WHERE session_id=? AND card_id=?", (session_id,card_id)).fetchone())
                        correct = choice == str(body["answer_index"])
                        result = {"correct": correct, "assisted": assisted,
                                  "message": ("答对了。原题重做不直接计为已掌握。" if correct else "这次还没答对，请再检查题目条件。"),
                                  "explanation": body["explanation"] if correct else ""}
                        self._db.execute("INSERT INTO retry_events VALUES (?,?,?,?,?)", (session_id,event_id,card_id,choice,_to_json(result)))
                        if correct:
                            self._db.execute("INSERT OR IGNORE INTO learning_reveals VALUES (?,?)", (session_id,card_id))
                self._db.execute("COMMIT")
                return result
            except Exception:
                self._db.execute("ROLLBACK")
                raise


NOTE_SECTIONS = ("profile", "concepts", "error_records", "review_plan")


def _empty_notes() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "profile": {}, "concepts": [], "error_records": [], "review_plan": []}


def _to_json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _from_json(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _check_notes(notes: dict[str, Any], learner_text: str) -> None:
    _validate(notes, NOTES_SCHEMA)
    if not isinstance(learner_text, str):
        raise ValueError("learner_text must be text")
    for section in NOTE_SECTIONS:
        if any(item["evidence_quote"] not in learner_text for item in notes[section]):
            raise ValueError(f"{section}: evidence must quote this learner turn")

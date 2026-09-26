"""Conversation storage — SQLite, stdlib only.

Schema (v2):

    conversations(id, title, created_at, updated_at)
    messages(id, conversation_id, role, kind, content, meta, created_at)

`role` is 'user' | 'assistant'. `kind` is 'text' | 'run' — a run
message carries the goal in `content` and its report/summary in
`meta` (JSON). Search is LIKE-based; local scale makes an index-free
scan honest, and FTS availability differs across builds.

Thread model: one connection guarded by an RLock, WAL mode. Writes are
cheap and rare relative to reads.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional


def _db_path() -> str:
    base = os.environ.get("NEX_HOME") or os.path.join(
        os.path.expanduser("~"), ".nex")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "nex.db")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'New chat',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'text',
    content TEXT NOT NULL DEFAULT '',
    meta TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv
    ON messages(conversation_id, created_at);
"""


class Store:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or _db_path()
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    # ----- conversations ----------------------------------------------------

    def create_conversation(self, title: str = "New chat") -> Dict[str, Any]:
        cid = "c-" + uuid.uuid4().hex[:12]
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO conversations (id, title, created_at, updated_at)"
                " VALUES (?, ?, ?, ?)", (cid, title, now, now))
            self._db.commit()
        return {"id": cid, "title": title, "created_at": now,
                "updated_at": now, "preview": "", "messages": 0}

    def rename_conversation(self, cid: str, title: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE conversations SET title = ?, updated_at = ? "
                "WHERE id = ?", (title.strip()[:120] or "Untitled", time.time(), cid))
            self._db.commit()
            return cur.rowcount > 0

    def touch_conversation(self, cid: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (time.time(), cid))
            self._db.commit()

    def delete_conversation(self, cid: str) -> bool:
        with self._lock:
            self._db.execute("DELETE FROM messages WHERE conversation_id = ?",
                             (cid,))
            cur = self._db.execute("DELETE FROM conversations WHERE id = ?",
                                   (cid,))
            self._db.commit()
            return cur.rowcount > 0

    def get_conversation(self, cid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute(
                "SELECT id, title, created_at, updated_at FROM conversations"
                " WHERE id = ?", (cid,)).fetchone()
        if row is None:
            return None
        return {"id": row[0], "title": row[1], "created_at": row[2],
                "updated_at": row[3]}

    def list_conversations(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT c.id, c.title, c.created_at, c.updated_at,"
                " (SELECT COUNT(*) FROM messages m"
                "  WHERE m.conversation_id = c.id) AS n,"
                " (SELECT content FROM messages m"
                "  WHERE m.conversation_id = c.id"
                "  ORDER BY m.created_at DESC LIMIT 1) AS preview"
                " FROM conversations c"
                " ORDER BY c.updated_at DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            preview = (r[5] or "").strip().replace("\n", " ")[:80]
            out.append({"id": r[0], "title": r[1], "created_at": r[2],
                        "updated_at": r[3], "messages": r[4],
                        "preview": preview})
        return out

    def search_conversations(self, query: str,
                             limit: int = 50) -> List[Dict[str, Any]]:
        """Conversations whose title or any message matches, in the same
        shape as list_conversations (so the client renders one list)."""
        q = "%" + (query or "").strip() + "%"
        with self._lock:
            rows = self._db.execute(
                "SELECT DISTINCT c.id, c.title, c.created_at, c.updated_at,"
                " (SELECT COUNT(*) FROM messages m2"
                "  WHERE m2.conversation_id = c.id) AS n,"
                " (SELECT content FROM messages m3"
                "  WHERE m3.conversation_id = c.id"
                "  ORDER BY m3.created_at DESC LIMIT 1) AS preview"
                " FROM conversations c"
                " JOIN messages m ON m.conversation_id = c.id"
                " WHERE c.title LIKE ? OR m.content LIKE ?"
                " ORDER BY c.updated_at DESC LIMIT ?", (q, q, limit)).fetchall()
        out = []
        for r in rows:
            preview = (r[5] or "").strip().replace("\n", " ")[:80]
            out.append({"id": r[0], "title": r[1], "created_at": r[2],
                        "updated_at": r[3], "messages": r[4],
                        "preview": preview})
        return out

    # ----- messages -----------------------------------------------------------

    def add_message(self, cid: str, role: str, content: str,
                    kind: str = "text",
                    meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        mid = "m-" + uuid.uuid4().hex[:12]
        now = time.time()
        with self._lock:
            exists = self._db.execute(
                "SELECT 1 FROM conversations WHERE id = ?",
                (cid,)).fetchone()
            if exists is None:
                raise ValueError("no conversation %r" % cid)
            self._db.execute(
                "INSERT INTO messages (id, conversation_id, role, kind,"
                " content, meta, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (mid, cid, role, kind, content,
                 json.dumps(meta, ensure_ascii=False) if meta else None, now))
            self._db.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, cid))
            # Auto-title from the first user message.
            row = self._db.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                (cid,)).fetchone()
            if role == "user" and row[0] <= 1:
                title = _title_from(content)
                self._db.execute(
                    "UPDATE conversations SET title = ? WHERE id = ?",
                    (title, cid))
            self._db.commit()
        return {"id": mid, "conversation_id": cid, "role": role, "kind": kind,
                "content": content, "meta": meta, "created_at": now}

    def get_messages(self, cid: str,
                     before: Optional[float] = None,
                     limit: int = 200) -> List[Dict[str, Any]]:
        q = ("SELECT id, role, kind, content, meta, created_at FROM messages"
             " WHERE conversation_id = ?")
        params: List[Any] = [cid]
        if before is not None:
            q += " AND created_at < ?"
            params.append(before)
        q += " ORDER BY created_at ASC"
        with self._lock:
            rows = self._db.execute(q, params).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r[0], "role": r[1], "kind": r[2], "content": r[3],
                "meta": json.loads(r[4]) if r[4] else None,
                "created_at": r[5],
            })
        if len(out) > limit:
            out = out[-limit:]
        return out

    def last_message(self, cid: str) -> Optional[Dict[str, Any]]:
        msgs = self.get_messages(cid)
        return msgs[-1] if msgs else None

    def delete_message(self, mid: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM messages WHERE id = ?", (mid,))
            self._db.commit()
            return cur.rowcount > 0

    def truncate_after(self, mid: str) -> int:
        """Delete the given message and everything after it (used by
        regenerate). Returns the number removed."""
        with self._lock:
            row = self._db.execute(
                "SELECT conversation_id, created_at FROM messages"
                " WHERE id = ?", (mid,)).fetchone()
            if row is None:
                return 0
            cid, ts = row
            cur = self._db.execute(
                "DELETE FROM messages WHERE conversation_id = ? AND"
                " created_at >= ?", (cid, ts))
            self._db.commit()
            return cur.rowcount

    def update_message(self, mid: str, content: Optional[str] = None,
                       meta: Optional[Dict[str, Any]] = None) -> bool:
        with self._lock:
            if content is not None:
                self._db.execute(
                    "UPDATE messages SET content = ? WHERE id = ?",
                    (content, mid))
            if meta is not None:
                self._db.execute(
                    "UPDATE messages SET meta = ? WHERE id = ?",
                    (json.dumps(meta, ensure_ascii=False), mid))
            self._db.commit()
            return True

    # ----- maintenance ----------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            convs = self._db.execute(
                "SELECT COUNT(*) FROM conversations").fetchone()[0]
            msgs = self._db.execute(
                "SELECT COUNT(*) FROM messages").fetchone()[0]
        return {"conversations": convs, "messages": msgs,
                "path": self.path,
                "size_bytes": os.path.getsize(self.path)
                if os.path.exists(self.path) else 0}

    def export(self) -> Dict[str, Any]:
        return {
            "conversations": self.list_conversations(limit=100000),
            "messages": {c["id"]: self.get_messages(c["id"])
                         for c in self.list_conversations(limit=100000)},
        }

    def wipe(self) -> None:
        with self._lock:
            self._db.executescript(
                "DELETE FROM messages; DELETE FROM conversations;")
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _title_from(content: str) -> str:
    text = re.sub(r"\s+", " ", (content or "").strip())
    if len(text) <= 48:
        return text or "New chat"
    return text[:45].rstrip() + "…"

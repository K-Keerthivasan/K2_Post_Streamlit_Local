"""Optional MySQL store for the post / review queue.

When the K2_MYSQL_* env vars are set (at minimum K2_MYSQL_DB), the generated-post
queue lives in a `posts` table with real DATETIME columns for the created /
approved dates — so the post data is managed in MySQL and can be queried by date.
When they are not set (or MySQL is unreachable), app.py falls back to the JSON
file, so the tool still runs with zero database setup.

Pure-python PyMySQL driver — no native build step needed on Windows.
"""
from __future__ import annotations
import json
import os
from datetime import datetime

try:
    import pymysql
    import pymysql.cursors
except ImportError:  # driver not installed → MySQL simply stays disabled
    pymysql = None


def enabled() -> bool:
    """MySQL is the post store only when the driver is present and a DB is named."""
    return bool(pymysql and os.getenv("K2_MYSQL_DB"))


def _conn():
    return pymysql.connect(
        host=os.getenv("K2_MYSQL_HOST", "localhost"),
        port=int(os.getenv("K2_MYSQL_PORT", "3306")),
        user=os.getenv("K2_MYSQL_USER", "root"),
        password=os.getenv("K2_MYSQL_PASSWORD", ""),
        database=os.getenv("K2_MYSQL_DB"),
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


_DDL = """
CREATE TABLE IF NOT EXISTS posts (
  id          VARCHAR(32)  NOT NULL PRIMARY KEY,
  brand       VARCHAR(64),
  title       TEXT,
  format      VARCHAR(32),
  rel         TEXT,
  files       JSON,
  caption     MEDIUMTEXT,
  status      VARCHAR(16)  NOT NULL DEFAULT 'pending',
  created     DATETIME,
  approved_at DATETIME,
  scheduled_at DATETIME,
  published_at DATETIME,
  targets     JSON,
  publish     JSON,
  INDEX idx_status    (status),
  INDEX idx_created   (created),
  INDEX idx_scheduled (scheduled_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_inited = False


def init() -> None:
    """Create the posts table on first use (idempotent)."""
    global _inited
    if _inited or not enabled():
        return
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute(_DDL)
            # Tables created before scheduling existed are missing these columns;
            # add them in place so an upgrade never needs a manual migration.
            cur.execute("SHOW COLUMNS FROM posts")
            have = {r["Field"] for r in cur.fetchall()}
            for name, ddl in (("scheduled_at", "DATETIME"),
                              ("published_at", "DATETIME"),
                              ("targets", "JSON")):
                if name not in have:
                    cur.execute(f"ALTER TABLE posts ADD COLUMN {name} {ddl}")
    _inited = True


def _to_dt(value: str | None) -> datetime | None:
    """ISO string (as stored in the JSON queue) → datetime for a DATETIME column."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _to_iso(value) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return value or ""


def _loads(value):
    """JSON column values come back as str from PyMySQL; tolerate either."""
    if value in (None, ""):
        return None
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def load_posts() -> list[dict]:
    """Return every queued post as a dict shaped exactly like the JSON entries."""
    init()
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM posts ORDER BY created DESC, id DESC")
            rows = cur.fetchall()
    out: list[dict] = []
    for r in rows:
        entry = {
            "id":      r["id"],
            "brand":   r["brand"],
            "title":   r["title"],
            "format":  r["format"],
            "rel":     r["rel"],
            "files":   _loads(r["files"]) or [],
            "caption": r["caption"] or "",
            "status":  r["status"],
            "created": _to_iso(r["created"]),
        }
        if r["approved_at"]:
            entry["approved_at"] = _to_iso(r["approved_at"])
        if r.get("scheduled_at"):
            entry["scheduled_at"] = _to_iso(r["scheduled_at"])
        if r.get("published_at"):
            entry["published_at"] = _to_iso(r["published_at"])
        targets = _loads(r.get("targets"))
        if targets:
            entry["targets"] = targets
        publish = _loads(r["publish"])
        if publish is not None:
            entry["publish"] = publish
        out.append(entry)
    return out


def save_posts(items: list[dict]) -> None:
    """Replace the whole queue with ``items`` in one transaction.

    Mirrors the JSON helper's read-all / write-all semantics so the review
    endpoints (approve / reject / delete / clear) need no special-casing.
    """
    init()
    rows = [(
        it.get("id"),
        it.get("brand"),
        it.get("title"),
        it.get("format"),
        it.get("rel"),
        json.dumps(it.get("files") or []),
        it.get("caption") or "",
        it.get("status") or "pending",
        _to_dt(it.get("created")),
        _to_dt(it.get("approved_at")),
        _to_dt(it.get("scheduled_at")),
        _to_dt(it.get("published_at")),
        json.dumps(it.get("targets")) if it.get("targets") else None,
        json.dumps(it["publish"]) if it.get("publish") is not None else None,
    ) for it in items]

    conn = _conn()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM posts")
            if rows:
                cur.executemany(
                    "INSERT INTO posts "
                    "(id, brand, title, format, rel, files, caption, status, "
                    " created, approved_at, scheduled_at, published_at, "
                    " targets, publish) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    rows,
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

"""Group existing personal recent visits without changing address book data."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import time

RECENT_TAG = "GeodomDesk 最近访问"
TAG_COLOR = 0xFF607D8B
MAX_TAGS = 256


def active_recent(data, latest):
    try:
        value = json.loads(data).get("geodom_recent")
        profile = json.loads(value) if isinstance(value, str) else None
    except (AttributeError, TypeError, ValueError):
        return False
    if not isinstance(profile, dict) or profile.get("removed") is True:
        return False
    revision = profile.get("timestamp")
    return type(revision) is int and 0 < revision <= latest


def backup_database(uri, path):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(uri + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(path)) as backup:
                source.backup(backup)
                if backup.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise RuntimeError("Backup integrity check failed")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def migrate(database, *, backup=None):
    uri = database.resolve(strict=True).as_uri()
    mode = "rw" if backup else "ro"
    latest = int(time.time() * 1000) + 300_000
    result = {"active_recent": 0, "already_grouped": 0, "pending": 0, "books_at_tag_limit": 0}
    with closing(sqlite3.connect(uri + "?mode=" + mode, uri=True, timeout=10)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE" if backup else "BEGIN")
        active = {}
        for book, peer, data in db.execute(
            "SELECT p.book,p.peer_id,p.data FROM ab_peer p "
            "JOIN ab_book b ON b.guid=p.book WHERE b.personal=1"
        ):
            if active_recent(data, latest):
                active.setdefault(book, []).append(peer)
                result["active_recent"] += 1
        pending = {}
        for book, peers in active.items():
            tag = db.execute("SELECT id FROM ab_tag WHERE book=? AND name=?", (book, RECENT_TAG)).fetchone()
            if tag is None:
                count, = db.execute("SELECT count(*) FROM ab_tag WHERE book=?", (book,)).fetchone()
                if count >= MAX_TAGS:
                    result["books_at_tag_limit"] += 1
                    continue
                pending[book] = peers
                continue
            assigned = {peer for peer, in db.execute(
                "SELECT peer_id FROM ab_peer_tag WHERE book=? AND tag=?", (book, tag[0])
            )}
            missing = [peer for peer in peers if peer not in assigned]
            result["already_grouped"] += len(peers) - len(missing)
            if missing:
                pending[book] = missing

        result["pending"] = sum(map(len, pending.values()))
        if backup and pending:
            backup_database(uri, backup)
            for book, peers in pending.items():
                db.execute("INSERT OR IGNORE INTO ab_tag(book,name,color) VALUES(?,?,?)",
                           (book, RECENT_TAG, TAG_COLOR))
                tag, = db.execute("SELECT id FROM ab_tag WHERE book=? AND name=?",
                                  (book, RECENT_TAG)).fetchone()
                db.executemany("INSERT INTO ab_peer_tag(book,peer_id,tag) VALUES(?,?,?)",
                               ((book, peer, tag) for peer in peers))
            db.commit()
            result["migrated"] = result["pending"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--backup", type=Path, help="Apply with a new database backup at this path")
    args = parser.parse_args()
    print(json.dumps(migrate(args.database, backup=args.backup)))


if __name__ == "__main__":
    main()

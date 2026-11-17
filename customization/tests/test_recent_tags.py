import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from migrate_recent_tags import RECENT_TAG, migrate


class RecentTagsTests(unittest.TestCase):
    def test_migrate_only_active_personal_records_and_preserve_existing_data(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "api.sqlite3"
            backup = Path(temp) / "before.sqlite3"
            with closing(sqlite3.connect(database)) as db, db:
                db.executescript("""
                    CREATE TABLE ab_book(guid TEXT PRIMARY KEY,personal INTEGER);
                    CREATE TABLE ab_peer(book TEXT,peer_id TEXT,data TEXT,PRIMARY KEY(book,peer_id));
                    CREATE TABLE ab_tag(id INTEGER PRIMARY KEY,book TEXT,name TEXT,color INTEGER,
                                        UNIQUE(book,name));
                    CREATE TABLE ab_peer_tag(book TEXT,peer_id TEXT,tag INTEGER,
                                            PRIMARY KEY(book,peer_id,tag));
                    INSERT INTO ab_book VALUES('personal',1),('shared',0);
                    INSERT INTO ab_tag VALUES(1,'personal','Existing',7);
                    INSERT INTO ab_peer_tag VALUES('personal','active',1);
                """)
                active = json.dumps({"alias": "Fixture", "hash": "saved",
                                     "geodom_recent": '{"timestamp":1}'})
                db.executemany("INSERT INTO ab_peer VALUES(?,?,?)", [
                    ("personal", "active", active),
                    ("personal", "deleted", '{"geodom_recent":"{\"timestamp\":2,\"removed\":true}"}'),
                    ("personal", "manual", '{"alias":"Manual"}'),
                    ("personal", "invalid", '{"geodom_recent":"bad"}'),
                    ("shared", "shared", active),
                ])
            self.assertEqual(migrate(database)["pending"], 1)
            self.assertEqual(migrate(database, backup=backup)["migrated"], 1)
            self.assertEqual(migrate(database)["pending"], 0)
            self.assertEqual(migrate(database, backup=backup)["already_grouped"], 1)
            with closing(sqlite3.connect(database)) as db, closing(sqlite3.connect(backup)) as saved:
                self.assertEqual(db.execute("SELECT * FROM ab_peer").fetchall(),
                                 saved.execute("SELECT * FROM ab_peer").fetchall())
                self.assertEqual(db.execute(
                    "SELECT p.book,p.peer_id,t.name FROM ab_peer_tag p "
                    "JOIN ab_tag t ON t.id=p.tag ORDER BY t.id"
                ).fetchall(), [("personal", "active", "Existing"), ("personal", "active", RECENT_TAG)])

"""SQLite persistence: conversations, messages, search, regenerate cut."""
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)

TMP = tempfile.mkdtemp(prefix="nex-store-")
os.environ["NEX_HOME"] = TMP

import store  # noqa: E402


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(TMP, "test-%s.db" % self.id())
        self.s = store.Store(self.db)

    def tearDown(self):
        self.s.close()

    def test_conversation_lifecycle(self):
        c = self.s.create_conversation()
        self.assertTrue(c["id"])
        self.assertEqual(c["title"], "New chat")
        got = self.s.get_conversation(c["id"])
        self.assertEqual(got["id"], c["id"])

    def test_append_and_read_messages(self):
        c = self.s.create_conversation()
        self.s.add_message(c["id"], "user", "hello there")
        self.s.add_message(c["id"], "assistant", "hi! how can I help?")
        msgs = self.s.get_messages(c["id"])
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["content"], "hello there")
        self.assertEqual(msgs[1]["role"], "assistant")

    def test_messages_other_conversation_isolated(self):
        a = self.s.create_conversation()
        b = self.s.create_conversation()
        self.s.add_message(a["id"], "user", "in a")
        self.s.add_message(b["id"], "user", "in b")
        self.assertEqual(len(self.s.get_messages(a["id"])), 1)
        self.assertEqual(self.s.get_messages(a["id"])[0]["content"], "in a")

    def test_auto_title(self):
        c = self.s.create_conversation()
        self.s.add_message(c["id"], "user",
                              "please summarize the quarterly report")
        title = self.s.get_conversation(c["id"])["title"]
        self.assertNotEqual(title, "New chat")
        self.assertTrue(len(title) <= 80)

    def test_list_orders_by_recency(self):
        import time
        a = self.s.create_conversation()
        time.sleep(0.02)
        b = self.s.create_conversation()
        lst = self.s.list_conversations()
        self.assertEqual(lst[0]["id"], b["id"])
        self.assertEqual(lst[1]["id"], a["id"])

    def test_rename(self):
        c = self.s.create_conversation()
        self.s.rename_conversation(c["id"], "My project")
        self.assertEqual(self.s.get_conversation(c["id"])["title"],
                         "My project")

    def test_renaming_before_the_first_message_is_not_clobbered(self):
        # A user can rename an empty conversation (the sidebar's Rename
        # action) before ever sending a message. Auto-titling the first
        # message must not silently overwrite that explicit choice.
        c = self.s.create_conversation()
        self.s.rename_conversation(c["id"], "Important Project Alpha")
        self.s.add_message(c["id"], "user", "hello world")
        self.assertEqual(self.s.get_conversation(c["id"])["title"],
                         "Important Project Alpha")
        # Renaming AFTER messages already exist is unaffected either way.
        self.s.rename_conversation(c["id"], "Renamed later")
        self.assertEqual(self.s.get_conversation(c["id"])["title"],
                         "Renamed later")

    def test_auto_title_still_applies_when_never_renamed(self):
        c = self.s.create_conversation()
        self.s.add_message(c["id"], "user", "what is the weather today")
        title = self.s.get_conversation(c["id"])["title"]
        self.assertNotEqual(title, "New chat")
        self.assertIn("weather", title)

    def test_delete(self):
        c = self.s.create_conversation()
        self.s.add_message(c["id"], "user", "x")
        self.assertTrue(self.s.delete_conversation(c["id"]))
        self.assertIsNone(self.s.get_conversation(c["id"]))
        self.assertEqual(self.s.get_messages(c["id"]), [])
        self.assertFalse(self.s.delete_conversation(c["id"]))

    def test_project_memory_round_trips_and_upserts(self):
        c = self.s.create_conversation()
        self.assertIsNone(self.s.get_project_memory(c["id"]))
        self.s.save_project_memory(c["id"], {"runs_recorded": 1,
                                             "milestones_done": ["discovery"]})
        got = self.s.get_project_memory(c["id"])
        self.assertEqual(got["runs_recorded"], 1)
        self.assertEqual(got["milestones_done"], ["discovery"])

        # A second run's memory replaces (upserts), it does not duplicate.
        self.s.save_project_memory(c["id"], {"runs_recorded": 2,
                                             "milestones_done": ["discovery",
                                                                "foundation"]})
        got2 = self.s.get_project_memory(c["id"])
        self.assertEqual(got2["runs_recorded"], 2)
        self.assertEqual(len(got2["milestones_done"]), 2)

        # Another conversation never sees this one's memory.
        other = self.s.create_conversation()
        self.assertIsNone(self.s.get_project_memory(other["id"]))

    def test_project_memory_is_deleted_with_its_conversation(self):
        c = self.s.create_conversation()
        self.s.save_project_memory(c["id"], {"runs_recorded": 1})
        self.assertTrue(self.s.delete_conversation(c["id"]))
        self.assertIsNone(self.s.get_project_memory(c["id"]))

    def test_project_memory_survives_reopen(self):
        c = self.s.create_conversation()
        self.s.save_project_memory(c["id"], {"runs_recorded": 3,
                                             "risks": ["x breaks y"]})
        self.s.close()
        reopened = store.Store(self.db)
        try:
            got = reopened.get_project_memory(c["id"])
            self.assertEqual(got["runs_recorded"], 3)
            self.assertEqual(got["risks"], ["x breaks y"])
        finally:
            reopened.close()
            self.s = store.Store(self.db)   # tearDown() will close this

    def test_search_finds_content(self):
        c = self.s.create_conversation()
        self.s.add_message(c["id"], "user",
                              "the blender scene is ready")
        hits = self.s.search_conversations("blender")
        self.assertTrue(any(h["id"] == c["id"] for h in hits))
        self.assertEqual(self.s.search_conversations("zzz-unfindable"), [])

    def test_search_finds_a_renamed_conversation_with_no_messages_yet(self):
        # The search query used an INNER JOIN against messages, so a
        # brand-new, renamed-but-still-empty conversation was invisible
        # to search no matter what its title said.
        c = self.s.create_conversation()
        self.s.rename_conversation(c["id"], "Important Project Alpha")
        hits = self.s.search_conversations("Important")
        self.assertTrue(any(h["id"] == c["id"] for h in hits),
                        "a renamed conversation must be findable by title "
                        "even before it has any messages")

    def test_search_does_not_duplicate_a_conversation_with_many_hits(self):
        c = self.s.create_conversation()
        self.s.rename_conversation(c["id"], "dup test")
        self.s.add_message(c["id"], "user", "dup test one")
        self.s.add_message(c["id"], "assistant", "dup test two")
        hits = [h["id"] for h in self.s.search_conversations("dup")]
        self.assertEqual(hits.count(c["id"]), 1)

    def test_truncate_after_for_regenerate(self):
        c = self.s.create_conversation()
        self.s.add_message(c["id"], "user", "q1")
        self.s.add_message(c["id"], "assistant", "a1")
        self.s.add_message(c["id"], "user", "q2")
        self.s.add_message(c["id"], "assistant", "a2")
        # regenerate the second answer: drop a2 (and anything after),
        # keep q1,a1,q2
        msgs = self.s.get_messages(c["id"])
        removed = self.s.truncate_after(msgs[3]["id"])
        self.assertEqual(removed, 1)
        msgs = self.s.get_messages(c["id"])
        self.assertEqual(len(msgs), 3)
        self.assertEqual(msgs[-1]["content"], "q2")

    def test_persistence_across_reopen(self):
        c = self.s.create_conversation()
        self.s.add_message(c["id"], "user", "persist me")
        self.s.close()
        s2 = store.Store(self.db)
        msgs = s2.get_messages(c["id"])
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["content"], "persist me")
        s2.close()

    def test_message_kinds(self):
        c = self.s.create_conversation()
        self.s.add_message(c["id"], "user", "do it")
        self.s.add_message(c["id"], "assistant", "running",
                              kind="run", meta={"run_id": "r1"})
        self.s.add_message(c["id"], "assistant", "done", kind="text")
        msgs = self.s.get_messages(c["id"])
        self.assertEqual([m.get("kind") for m in msgs],
                         ["text", "run", "text"])

    def test_bad_inputs(self):
        with self.assertRaises(Exception):
            self.s.add_message("no-such-convo", "user", "x")


class RetentionTests(unittest.TestCase):
    """The store must not grow without bound: a single long-lived
    conversation, and the store as a whole, are both hard-capped."""

    def _store(self, **kw):
        path = os.path.join(TMP, "retention-%s-%s.db"
                            % (self.id(), id(kw)))
        return store.Store(path, **kw)

    def test_default_retention_is_on_and_finite(self):
        s = store.Store(os.path.join(TMP, "defaults-%s.db" % self.id()))
        try:
            self.assertGreater(s.max_conversations, 0)
            self.assertGreater(s.max_messages_per_conversation, 0)
        finally:
            s.close()

    def test_per_conversation_message_cap_trims_oldest(self):
        s = self._store(max_messages_per_conversation=5,
                        max_conversations=0, conversation_ttl_days=0)
        try:
            c = s.create_conversation()
            for i in range(20):
                s.add_message(c["id"], "user", "msg-%02d" % i)
            msgs = s.get_messages(c["id"])
            self.assertEqual(len(msgs), 5)
            # the newest 5 survive, oldest are gone
            self.assertEqual([m["content"] for m in msgs],
                             ["msg-%02d" % i for i in range(15, 20)])
        finally:
            s.close()

    def test_conversation_count_cap_evicts_least_recently_active(self):
        s = self._store(max_conversations=3,
                        max_messages_per_conversation=0,
                        conversation_ttl_days=0)
        try:
            ids = []
            for i in range(6):
                c = s.create_conversation()
                s.add_message(c["id"], "user", "hi %d" % i)
                ids.append(c["id"])
            all_convs = s.list_conversations(limit=100)
            self.assertLessEqual(len(all_convs), 3)
            # the most recently created/touched conversations must survive
            surviving = {c["id"] for c in all_convs}
            self.assertTrue(set(ids[-3:]).issubset(surviving))
            self.assertFalse(set(ids[:3]) & surviving)
            # their messages are gone too, not orphaned
            for old_cid in ids[:3]:
                self.assertEqual(s.get_messages(old_cid), [])
        finally:
            s.close()

    def test_ttl_expires_old_conversations(self):
        s = self._store(max_conversations=0,
                        max_messages_per_conversation=0,
                        conversation_ttl_days=1)
        try:
            c = s.create_conversation()
            s.add_message(c["id"], "user", "old one")
            # backdate it past the 1-day TTL
            s._db.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (time.time() - 2 * 86400, c["id"]))
            s._db.commit()
            removed = s.prune()
            self.assertEqual(removed["conversations_removed"], 1)
            self.assertIsNone(s.get_conversation(c["id"]))
        finally:
            s.close()

    def test_retention_can_be_disabled(self):
        s = self._store(max_conversations=0,
                        max_messages_per_conversation=0,
                        conversation_ttl_days=0)
        try:
            c = s.create_conversation()
            for i in range(50):
                s.add_message(c["id"], "user", "m%d" % i)
            self.assertEqual(len(s.get_messages(c["id"])), 50)
        finally:
            s.close()

    def test_stats_reports_retention_limits(self):
        s = self._store(max_conversations=7,
                        max_messages_per_conversation=9,
                        conversation_ttl_days=3)
        try:
            limits = s.stats()["retention"]
            self.assertEqual(limits["max_conversations"], 7)
            self.assertEqual(limits["max_messages_per_conversation"], 9)
            self.assertEqual(limits["conversation_ttl_days"], 3)
        finally:
            s.close()

    def test_bounded_total_rows_even_under_heavy_use(self):
        """With both count-based caps on, total message rows across the
        whole store stay hard-bounded no matter how much is written."""
        s = self._store(max_conversations=3, max_messages_per_conversation=4,
                        conversation_ttl_days=0)
        try:
            for i in range(10):
                c = s.create_conversation()
                for j in range(10):
                    s.add_message(c["id"], "user", "c%d-m%d" % (i, j))
            total_messages = sum(
                len(s.get_messages(c["id"]))
                for c in s.list_conversations(limit=1000))
            self.assertLessEqual(len(s.list_conversations(limit=1000)), 3)
            self.assertLessEqual(total_messages, 3 * 4)
        finally:
            s.close()


class TieBreakTests(unittest.TestCase):
    """A timestamp is not a unique key: two writes can legitimately share
    the same `created_at`/`updated_at` (fast programmatic turns, batched
    writes, coarse clock resolution on some platforms). Every "oldest wins"
    or "delete from here on" query must still land on the single row that
    was actually written first/last, not an arbitrary one that happens to
    win a lexical compare on a random UUID."""

    def _store(self, **kw):
        path = os.path.join(TMP, "tiebreak-%s-%s.db" % (self.id(), id(kw)))
        return store.Store(path, **kw)

    def test_truncate_after_does_not_delete_an_earlier_tied_sibling(self):
        s = self._store(max_conversations=0, max_messages_per_conversation=0,
                        conversation_ttl_days=0)
        try:
            c = s.create_conversation()
            keep = s.add_message(c["id"], "user", "keep me")
            target = s.add_message(c["id"], "assistant", "regenerate me")
            # Force an exact timestamp tie between the two messages — this
            # is the scenario a created_at-based ">=" comparison gets wrong.
            tied_ts = 12345.0
            s._db.execute("UPDATE messages SET created_at = ? WHERE id IN (?, ?)",
                         (tied_ts, keep["id"], target["id"]))
            s._db.commit()
            removed = s.truncate_after(target["id"])
            self.assertEqual(removed, 1, "only the target message, not its "
                             "tied-timestamp predecessor, must be removed")
            remaining = s.get_messages(c["id"])
            self.assertEqual([m["id"] for m in remaining], [keep["id"]])
        finally:
            s.close()

    def test_conversation_trim_evicts_true_insertion_order_on_tie(self):
        s = self._store(max_conversations=0, max_messages_per_conversation=0,
                        conversation_ttl_days=0)
        try:
            c = s.create_conversation()
            ids = [s.add_message(c["id"], "user", "m%d" % i)["id"]
                  for i in range(6)]
            # Collapse every message onto the same timestamp so the only
            # correct way to tell them apart is true insertion order.
            s._db.execute("UPDATE messages SET created_at = 999.0"
                         " WHERE conversation_id = ?", (c["id"],))
            s._db.commit()
            with s._lock:
                removed = s._trim_conversation_messages_locked(c["id"], 4)
                s._db.commit()
            self.assertEqual(removed, 4)
            remaining = {m["id"] for m in s.get_messages(c["id"])}
            # the 4 *first-inserted* ids must be gone; the 2 last survive
            self.assertEqual(remaining, set(ids[-2:]))
        finally:
            s.close()

    def test_conversation_eviction_evicts_true_activity_order_on_tie(self):
        # Cap starts at 0 (disabled) so all 5 conversations survive creation
        # -- a cap set from the start would prune progressively as each one
        # is created, never letting all 5 coexist to produce the tie.
        s = self._store(max_conversations=0, max_messages_per_conversation=0,
                        conversation_ttl_days=0)
        try:
            ids = [s.create_conversation()["id"] for _ in range(5)]
            # Collapse every conversation onto the same updated_at so the
            # only correct way to rank them is true creation/activity order.
            s._db.execute("UPDATE conversations SET updated_at = 999.0")
            s._db.commit()
            s.max_conversations = 2
            removed = s.prune()
            self.assertEqual(removed["conversations_removed"], 3)
            remaining = {c["id"] for c in s.list_conversations(limit=10)}
            self.assertEqual(remaining, set(ids[-2:]))
        finally:
            s.close()


class LegacyDatabaseTests(unittest.TestCase):
    """A database that predates (or was opened with looser) retention caps
    must be brought into line the moment it is opened, not left over-cap
    until the next write happens to trigger pruning."""

    def test_existing_over_cap_data_is_pruned_on_open(self):
        path = os.path.join(TMP, "legacy-%s.db" % self.id())
        # Open once with retention effectively off and write well past what
        # a tighter cap would allow -- simulates a pre-retention database.
        s1 = store.Store(path, max_conversations=0,
                         max_messages_per_conversation=0,
                         conversation_ttl_days=0)
        conv_ids = []
        for i in range(10):
            c = s1.create_conversation()
            for j in range(10):
                s1.add_message(c["id"], "user", "c%d-m%d" % (i, j))
            conv_ids.append(c["id"])
        s1.close()

        # Reopen the SAME file with tight caps -- pruning must happen
        # immediately on open, without any add_message/create_conversation.
        s2 = store.Store(path, max_conversations=3,
                         max_messages_per_conversation=4,
                         conversation_ttl_days=0)
        try:
            remaining = s2.list_conversations(limit=100)
            self.assertLessEqual(len(remaining), 3)
            for c in remaining:
                self.assertLessEqual(len(s2.get_messages(c["id"])), 4)
            # the most recently active conversations are the ones kept
            self.assertTrue({c["id"] for c in remaining}.issubset(
                set(conv_ids[-3:])))
        finally:
            s2.close()


class VacuumTests(unittest.TestCase):
    """Pruned rows must actually shrink the file on disk, not just the
    logical row count — otherwise "capped" is a lie at the filesystem
    level even though the database claims to be bounded."""

    def test_auto_vacuum_is_enabled(self):
        path = os.path.join(TMP, "vacuum-mode-%s.db" % self.id())
        s = store.Store(path)
        try:
            mode = s._db.execute("PRAGMA auto_vacuum").fetchone()[0]
            self.assertEqual(mode, 2, "auto_vacuum must be INCREMENTAL")
        finally:
            s.close()

    def test_legacy_database_is_migrated_to_incremental_vacuum(self):
        import sqlite3
        path = os.path.join(TMP, "vacuum-legacy-%s.db" % self.id())
        # Build a file the way Nex did before this feature existed: no
        # auto_vacuum pragma ever set.
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        raw.commit()
        raw.close()
        s = store.Store(path)
        try:
            mode = s._db.execute("PRAGMA auto_vacuum").fetchone()[0]
            self.assertEqual(mode, 2, "an existing database must be "
                             "migrated to incremental auto_vacuum on open")
        finally:
            s.close()

    def test_wipe_reclaims_space_too(self):
        path = os.path.join(TMP, "vacuum-wipe-%s.db" % self.id())
        s = store.Store(path)
        try:
            c = s.create_conversation()
            big = "x" * 20000
            for i in range(50):
                s.add_message(c["id"], "user", big)
            s._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            size_before = os.path.getsize(path)
            s.wipe()
            s._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            size_after = os.path.getsize(path)
            self.assertEqual(s.stats()["conversations"], 0)
            self.assertLess(size_after, size_before,
                            "wiping everything must shrink the file too")
        finally:
            s.close()

    def test_file_shrinks_after_a_heavy_prune(self):
        """Compares WAL-checkpointed size on both sides: in WAL mode, an
        un-checkpointed write can sit in the -wal sidecar file rather than
        the main db file, which would make an apples-to-oranges size
        comparison look like growth even though the main file genuinely
        shrinks once everything is flushed to one place."""
        path = os.path.join(TMP, "vacuum-shrink-%s.db" % self.id())
        s = store.Store(path, max_conversations=0,
                        max_messages_per_conversation=0,
                        conversation_ttl_days=0)
        try:
            c = s.create_conversation()
            big = "x" * 20000
            for i in range(200):
                s.add_message(c["id"], "user", big)
            s._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            size_before = os.path.getsize(path)
            s.max_messages_per_conversation = 5
            result = s.prune()
            self.assertGreater(result["messages_removed"], 0)
            s._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            size_after = os.path.getsize(path)
            self.assertLess(size_after, size_before,
                            "pruned rows must shrink the file on disk")
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

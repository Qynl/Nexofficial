"""SQLite persistence: conversations, messages, search, regenerate cut."""
import os
import sys
import tempfile
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

    def test_delete(self):
        c = self.s.create_conversation()
        self.s.add_message(c["id"], "user", "x")
        self.assertTrue(self.s.delete_conversation(c["id"]))
        self.assertIsNone(self.s.get_conversation(c["id"]))
        self.assertEqual(self.s.get_messages(c["id"]), [])
        self.assertFalse(self.s.delete_conversation(c["id"]))

    def test_search_finds_content(self):
        c = self.s.create_conversation()
        self.s.add_message(c["id"], "user",
                              "the blender scene is ready")
        hits = self.s.search_conversations("blender")
        self.assertTrue(any(h["id"] == c["id"] for h in hits))
        self.assertEqual(self.s.search_conversations("zzz-unfindable"), [])

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


if __name__ == "__main__":
    unittest.main(verbosity=2)

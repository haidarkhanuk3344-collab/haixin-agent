import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid

from personal_agent import app
from personal_agent.jobs import Store
from personal_agent.memory import Memory, candidates, natural_control


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"; self.state.mkdir()
        self.ws = self.root / "workspace"; self.ws.mkdir()
        (self.state / "openclaw.json").write_text(json.dumps({"agents": {"defaults": {"workspace": str(self.ws)}}, "tools": {"allow": ["read", "write"]}}))
        self.c = {"state": self.state}
        self.store, self.mem = Store(self.c), Memory(self.c)

    def tearDown(self):
        self.tmp.cleanup()

    def chat_job(self, text, category="chat", kind="chat"):
        return self.store.create(text, kind=kind, category=category)

    def control(self, action, **kw):
        cmd = dict(action=action, **kw)
        job = self.store.create(json.dumps(cmd), kind="memory", category="memory")
        return self.mem.control(cmd, job)

    def drink(self):
        job = self.chat_job("我平时喜欢喝乌龙茶。")
        self.mem.observe(job)
        return self.mem.get("preference.drink"), job

    def test_explicit_style_goal_topic(self):
        self.assertEqual({x["slot"] for x in candidates("我喜欢直接一点，别写太长。")}, {"communication.length", "communication.tone"})
        self.assertEqual(candidates("我的目标是今年完成个人Agent。")[0]["kind"], "goal")
        self.assertEqual(candidates("下次聊旅行计划。")[0]["kind"], "topic")

    def test_reject_quotes_third_party_temporary_uncertain(self):
        for text in ("朋友说我喜欢喝乌龙茶", "文章里说我喜欢喝乌龙茶", "我今天喜欢喝乌龙茶", "我可能喜欢喝乌龙茶",
                     "假设我喜欢喝乌龙茶", '他说“我喜欢喝乌龙茶”', "我喜欢喝什么？", "别记住我喜欢喝乌龙茶", "我喜欢喝乌龙茶，只是开玩笑"):
            self.assertEqual(candidates(text), [], text)

    def test_source_types_not_user_facts(self):
        for kind, category in (("task", "task"), ("chat", "verification")):
            self.mem.observe(self.chat_job("我喜欢喝乌龙茶", category=category, kind=kind))
        self.assertEqual(self.mem.list(), [])

    def test_per_message_no_memory_does_not_disable_other_messages(self):
        for text in ("我喜欢喝乌龙茶，不要长期记住。", "我喜欢蓝色，不需要保存。", "我喜欢绿色，仅限本次对话。"):
            job = self.chat_job(text)
            self.assertEqual(self.mem.observe(job), [])
            result = self.mem.annotate_result({"texts": ["已记住"]}, job, [])
            self.assertTrue(result["memory_notice"])
            self.assertTrue(result["persistence_claim_corrected"])
        self.assertEqual(self.mem.list(), [])
        self.drink()
        self.assertEqual(len(self.mem.list()), 1)

    def test_source_and_idempotent_observation(self):
        m, job = self.drink()
        self.assertEqual(m["source_job"], job["id"])
        self.assertIn("我平时", m["quote"])
        self.assertEqual(self.mem.observe(job), [])
        self.assertEqual(self.mem.get(m["id"])["version"], 1)

    def test_natural_new_value_supersedes_old(self):
        m, _ = self.drink()
        self.mem.observe(self.chat_job("我更喜欢喝普洱茶。"))
        current = self.mem.get(m["id"], True)
        self.assertEqual(current["version"], 2)
        self.assertIn("普洱", current["content"])
        self.assertEqual(current["history"][0]["status"], "superseded")
        self.assertNotIn("乌龙", json.dumps(self.mem.search("我喝什么"), ensure_ascii=False))

    def test_correct_retains_history_but_recall_is_current(self):
        m, source = self.drink()
        self.control("correct", id=m["id"], content="用户喜欢喝普洱茶。")
        self.control("rebuild")
        self.assertIn("普洱", self.mem.get(m["id"])["content"])
        self.assertIn("乌龙", self.mem.get(m["id"], True)["history"][0]["content"])
        self.mem.observe(source, replay=True)
        self.assertIn("普洱", self.mem.get(m["id"])["content"])

    def test_forget_clears_versions_index_and_prevents_reextraction(self):
        m, source = self.drink()
        self.control("correct", id=m["id"], content="用户喜欢喝普洱茶。")
        self.control("forget", id=m["id"])
        self.control("rebuild")
        self.mem.observe(source, replay=True)
        self.mem.observe(self.chat_job("我喜欢喝乌龙茶。"))
        self.assertEqual(self.mem.search("喝茶"), [])
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memory_index").fetchone()[0], 0)
            for row in db.execute("SELECT content,quote,source_job FROM memory_versions"):
                self.assertEqual(tuple(row), (None, None, None))
        self.assertEqual(self.mem.get(m["id"])["status"], "forgotten")
        self.assertIn("乌龙", self.store.get(source["id"])["goal"])  # archives intentionally retained

    def test_forget_scrubs_other_memory_shared_source_excerpt(self):
        job = self.chat_job("我喜欢直接一点，我喜欢喝乌龙茶。")
        self.mem.observe(job)
        self.control("forget", id="preference.drink")
        self.assertIsNone(self.mem.get("communication.tone")["quote"])
        with self.store.db() as db:
            self.assertFalse(any("乌龙" in (r[0] or "") for r in db.execute("SELECT quote FROM memory_versions")))

    def test_allow_only_new_sources(self):
        m, old = self.drink()
        self.control("forget", id=m["id"])
        self.control("allow", id="preference.drink")
        self.mem.observe(old, replay=True)
        self.assertEqual(self.mem.list(), [])
        self.mem.observe(self.chat_job("我喜欢喝普洱茶。"))
        self.assertIn("普洱", self.mem.get(m["id"])["content"])

    def test_disable_does_not_backfill_disabled_messages(self):
        self.control("disable", kind="preference")
        job = self.chat_job("我喜欢喝乌龙茶。")
        self.mem.observe(job)
        self.control("enable", kind="preference")
        self.control("rebuild")
        self.mem.observe(job, replay=True)
        self.assertEqual(self.mem.list(), [])

    def test_disable_hides_existing_recall_without_erasing(self):
        m, _ = self.drink()
        self.control("disable", kind="preference")
        self.assertEqual(self.mem.search("喝什么"), [])
        self.assertIsNotNone(self.mem.get(m["id"])["content"])
        self.control("enable", kind="preference")
        self.assertEqual(len(self.mem.search("喝什么")), 1)

    def test_false_model_persistence_claim_corrected_when_disabled(self):
        self.control("disable", kind="preference")
        job = self.chat_job("我喜欢绿色笔记本。")
        result = self.mem.annotate_result({"texts": ["我记下了，以后推荐绿色。"]}, job, [])
        self.assertTrue(result["persistence_claim_corrected"])
        self.assertNotIn("记下", result["texts"][0])
        self.assertIn("没有保存", result["texts"][0])
        self.assertTrue(result["memory_notice"])

    def test_false_model_persistence_claim_corrected_when_forgotten(self):
        m, _ = self.drink(); self.control("forget", id=m["id"])
        job = self.chat_job("我喜欢喝乌龙茶。")
        result = self.mem.annotate_result({"texts": ["已记住"]}, job, [])
        self.assertTrue(result["persistence_claim_corrected"])
        self.assertIn("忘记保护", result["memory_notice"])

    def test_recall_bounded_and_relevant(self):
        self.drink()
        for i in range(10):
            self.mem.observe(self.chat_job("我的目标是完成项目" + str(i)))
        self.assertLessEqual(len(self.mem.search("记得我的目标吗")), 6)
        self.assertLessEqual(sum(len(m["content"]) for m in self.mem.search("记忆", budget=40)), 40)
        self.assertEqual(self.mem.search("解释一下行星轨道"), [])

    def test_context_epoch_clears_old_session_on_forget(self):
        m, job = self.drink()
        before_text, before, _ = self.mem.context(job)
        self.assertIn("乌龙", before_text)
        self.control("forget", id=m["id"])
        query = self.chat_job("我平时喝什么？"); query["session"] = job["session"]
        after_text, after, ids = self.mem.context(query)
        self.assertNotEqual(before, after)
        self.assertNotIn("乌龙", after_text)
        self.assertEqual(ids, [])

    def test_runtime_config_disables_tools_and_leaves_original_intact(self):
        original = (self.state / "openclaw.json").read_bytes()
        derived = json.loads(Path(self.mem.runtime_config()).read_text())
        self.assertEqual(derived["tools"]["deny"], ["*"])
        self.assertNotIn("allow", derived["tools"])
        self.assertFalse(derived["memory"]["search"]["enabled"])
        self.assertNotEqual(derived["agents"]["defaults"]["workspace"], str(self.ws))
        self.assertEqual(original, (self.state / "openclaw.json").read_bytes())

    def test_delete_journal_replayed_after_database_restore(self):
        m, _ = self.drink()
        backup = self.root / "db-before.sqlite3"
        with sqlite3.connect(str(backup)) as target, self.store.db() as source:
            source.backup(target)
        self.control("forget", id=m["id"])
        shutil.copyfile(backup, self.store.path)
        restored = Memory(self.c)
        with self.assertRaises(app.AppError): restored.list()
        self.assertEqual(restored.replay_deletions(), 1)
        self.assertEqual(restored.list(), [])
        self.assertEqual(restored.replay_deletions(), 0)

    def test_partial_journal_fails_closed(self):
        self.drink()
        self.mem.journal.write_text('{"id":')
        with self.assertRaises(app.AppError): self.mem.replay_deletions()
        with self.assertRaises(app.AppError): self.mem.search("喝什么")

    def test_natural_control_distinct_from_model_inference(self):
        self.assertEqual(natural_control("忘记我的饮品偏好")["action"], "forget")
        self.assertEqual(natural_control("请把我的饮品偏好改为普洱茶")["action"], "correct")
        self.assertEqual(natural_control("忘记那个事情")["action"], "unresolved_forget")
        self.assertIsNone(natural_control("文章说要忘记饮品偏好"))

    def test_memory_and_chat_persist_across_process(self):
        m, _ = self.drink()
        code = "from pathlib import Path;from personal_agent.memory import Memory;import sys,json;print(json.dumps(Memory({'state':Path(sys.argv[1])}).search('喝什么'),ensure_ascii=False))"
        p = subprocess.run([sys.executable, "-c", code, str(self.state)], cwd=app.ROOT, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)[0]["id"], m["id"])


if __name__ == "__main__":
    unittest.main()

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from personal_agent import app, worker
from personal_agent.jobs import Store, prepare, verify_artifact


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"; self.state.mkdir()
        self.workspace = self.root / "workspace"; self.workspace.mkdir()
        (self.state / "openclaw.json").write_text(json.dumps({"agents": {"defaults": {"workspace": str(self.workspace),"model":{"primary":"deepseek-local/deepseek-flash"}}},
            "models":{"providers":{"deepseek-local":{"baseUrl":"https://api.deepseek.com","models":[{"id":"deepseek-flash","maxTokens":768}]}}}}))
        self.cli = self.root / "unused.py"; self.cli.write_text("")
        config = {"schema_version": 1, "runtime": str(self.root), "node": sys.executable, "cli": str(self.cli),
                  "state": str(self.state), "price_file": str(app.ROOT / "config/prices.json"),
                  "keychain_service": "not-used", "keychain_account": "not-used"}
        self.path = self.root / "config.json"; self.path.write_text(json.dumps(config))
        self.c = app.load_config(self.path)
        self.store = Store(self.c)
        self.processes = []

    def tearDown(self):
        worker.stop(self.c)
        for p in self.processes:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=5)
            if p.stdout: p.stdout.close()
        self.tmp.cleanup()

    def wait(self, fn, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = fn()
            if result: return result
            time.sleep(0.03)
        self.fail("等待条件超时")

    def start(self):
        p = subprocess.Popen([sys.executable, str(app.ROOT / "tests/fake_worker.py"), str(self.path)],
                             cwd=app.ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.processes.append(p)
        self.wait(lambda: worker.status(self.c)["state"] == "running")
        return p

    def test_input_snapshot_survives_source_changes(self):
        p = self.workspace / "a.md"; p.write_text("original")
        j = self.store.create("compare", inputs=["a.md"])
        p.write_text("changed")
        claimed = self.store.claim()
        directory, _, _ = prepare(self.c, claimed)
        self.assertEqual((directory / "inputs/1.md").read_text(), "original")
        self.assertNotIn("content", self.store.get(j["id"])["inputs"][0])

    def test_watch_worker_detects_changed_file_without_client(self):
        from personal_agent.sources import Sources
        from personal_agent.watch import Watch
        import uuid
        source=Sources(self.c);file=self.root/'watch.md';file.write_text('原版')
        result=source.ingest({'id':str(uuid.uuid4()),'attempts':1,'goal':json.dumps(source.file_command(str(file)))})
        sid=result['source_id'];watcher=Watch(self.c);watcher.add(sid,60)
        file.write_text('更新版');watcher.control(sid,'check');self.start()
        self.wait(lambda:source.get(sid)['current_version']==2)
        self.wait(lambda:watcher.get(sid)['active_run'] is None)
        self.assertEqual(watcher.get(sid)['runs'][0]['status'],'changed')
        self.assertFalse((self.state/'fake-invocations.txt').exists())
        worker.stop(self.c);self.start()
        self.assertEqual(len(watcher.get(sid)['runs']),1)
        self.assertEqual(source.snapshot(sid,1)['content'],'原版')

    def test_reject_outside_and_symlink_escape(self):
        p = self.root / "private.md"; p.write_text("private")
        (self.workspace / "link.md").symlink_to(p)
        for name in (str(p), "link.md", "../private.md"):
            with self.assertRaises(app.AppError): self.store.create("read", inputs=[name])
        self.assertEqual(self.store.list(), [])

    def test_reject_large_and_invalid_input(self):
        p = self.workspace / "bad.md"
        for raw in (b"x" * 262145, b"\xff"):
            p.write_bytes(raw)
            with self.assertRaises(app.AppError): self.store.create("read", inputs=["bad.md"])

    def test_artifact_missing_empty_and_symlink_rejected(self):
        self.store.create("write")
        j = self.store.claim(); d, _, _ = prepare(self.c, j)
        with self.assertRaises(app.AppError): verify_artifact(self.c, d)
        p = d / "result.pending.md"; p.write_text(" ")
        with self.assertRaises(app.AppError): verify_artifact(self.c, d)
        p.unlink(); p.symlink_to(self.cli)
        with self.assertRaises(app.AppError): verify_artifact(self.c, d)

    def test_artifact_atomic_publish_and_hash(self):
        self.store.create("write")
        j = self.store.claim(); d, _, _ = prepare(self.c, j)
        (d / "result.pending.md").write_text("# 结果\n内容")
        r = verify_artifact(self.c, d)
        self.assertEqual(r["sha256"], hashlib.sha256(Path(r["path"]).read_bytes()).hexdigest())
        self.assertFalse((d / "result.pending.md").exists())

    def test_queued_cancel_prevents_execution(self):
        j = self.store.create("cancel")
        self.assertEqual(self.store.cancel(j["id"])["status"], "cancelled")
        self.assertIsNone(self.store.claim())

    def test_cancel_wins_completion_race(self):
        j = self.store.create("race"); self.store.claim()
        self.store.cancel(j["id"])
        self.store.finish(j["id"], "succeeded", {"texts": ["done"]})
        self.assertEqual(self.store.get(j["id"])["status"], "cancelled")

    def test_retry_requires_ack_and_is_bounded(self):
        j = self.store.create("retry"); self.store.claim(); self.store.recover()
        with self.assertRaises(app.AppError): self.store.resume(j["id"])
        self.store.resume(j["id"], True); self.store.claim(); self.store.recover()
        with self.assertRaises(app.AppError): self.store.resume(j["id"], True)
        self.assertEqual(self.store.get(j["id"])["attempts"], 2)

    def test_client_exit_and_worker_completion(self):
        self.start()
        p = subprocess.run([sys.executable, str(app.ROOT / "run.py"), "--config", str(self.path),
                            "task", "create", "offline task", "--no-start", "--json"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout)
        j = json.loads(p.stdout)
        self.wait(lambda: self.store.get(j["id"])["status"] == "succeeded")
        self.assertTrue(Path(self.store.get(j["id"])["result"]["artifact"]["path"]).is_file())
        self.assertEqual([s["status"] for s in self.store.get(j["id"])["steps"]], ["succeeded"] * 3)

    def test_second_worker_cannot_take_owner(self):
        first = self.start()
        p = subprocess.run([sys.executable, str(app.ROOT / "tests/fake_worker.py"), str(self.path)], capture_output=True, text=True, timeout=3)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(worker.status(self.c)["pid"], first.pid)

    def test_runtime_child_retains_lock_when_owner_killed(self):
        helper = self.root / "child.py"
        ready = self.root / "ready"
        helper.write_text("from pathlib import Path\nimport time\nPath(" + repr(str(ready)) + ").write_text('ready')\ntime.sleep(0.8)\n")
        cfg = json.loads(self.path.read_text()); cfg["cli"] = str(helper)
        self.path.write_text(json.dumps(cfg))
        code = """import sys
from personal_agent.app import load_config,exclusive,execute
c=load_config(sys.argv[1])
with exclusive(c['state']) as fd: execute(c, [], 'fake-test', fd)
"""
        p = subprocess.Popen([sys.executable, "-c", code, str(self.path)], cwd=app.ROOT,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(p)
        self.wait(ready.exists)
        p.kill(); p.wait(timeout=3)
        with self.assertRaises(app.AppError):
            with app.exclusive(self.state): pass
        def released():
            try:
                with app.exclusive(self.state): return True
            except app.AppError: return False
        self.wait(released)

    def test_running_cancel_stops_next_step(self):
        self.start(); j = self.store.create("SLOW")
        self.wait(lambda: (self.state / "fake-invocations.txt").exists())
        self.store.cancel(j["id"])
        self.wait(lambda: self.store.get(j["id"])["status"] == "cancelled")
        self.assertFalse(any(s["name"] == "verify" for s in self.store.get(j["id"])["steps"]))

    def test_kill_restart_unknown_no_duplicate(self):
        p = self.start(); j = self.store.create("SLOW")
        self.wait(lambda: (self.state / "fake-invocations.txt").exists())
        p.kill(); p.wait(timeout=3)
        queued = self.store.create("next safe job")
        self.start()
        self.wait(lambda: self.store.get(queued["id"])["status"] == "succeeded")
        self.assertEqual(self.store.get(j["id"])["status"], "outcome_unknown")
        invocations = (self.state / "fake-invocations.txt").read_text().splitlines()
        self.assertEqual(invocations.count(j["id"]), 1)
        self.assertEqual(self.store.get(j["id"])["attempts"], 1)

    def test_stop_keeps_queued_jobs_for_restart(self):
        self.start(); j = self.store.create("SLOW")
        self.wait(lambda: (self.state / "fake-invocations.txt").exists())
        queued = self.store.create("later")
        self.assertEqual(worker.stop(self.c)["state"], "stopped")
        self.assertEqual(self.store.get(j["id"])["status"], "outcome_unknown")
        self.assertEqual(self.store.get(queued["id"])["status"], "queued")
        self.start()
        self.wait(lambda: self.store.get(queued["id"])["status"] == "succeeded")

    def test_missing_output_not_success(self):
        self.start(); j = self.store.create("MISSING_OUTPUT")
        self.wait(lambda: self.store.get(j["id"])["status"] == "failed")
        self.assertEqual(self.store.get(j["id"])["steps"][-1]["status"], "failed")

    def test_memory_control_serialized_with_chat(self):
        from personal_agent.memory import Memory
        self.start()
        response = worker.send_chat(self.c, "我喜欢喝乌龙茶。")
        self.assertNotIn("error", response)
        m = Memory(self.c).get("preference.drink")
        result = worker.send_memory(self.c, {"action": "forget", "id": m["id"]})
        self.assertTrue(result["memory_control"])
        self.assertEqual(Memory(self.c).search("喝什么"), [])
        invocations = (self.state / "fake-invocations.txt").read_text().splitlines()
        self.assertNotIn(result["job_id"], invocations)

    def test_natural_disable_does_not_invoke_model(self):
        from personal_agent.memory import Memory
        self.start()
        response = worker.send_chat(self.c, "关闭长期记忆")
        self.assertTrue(response["memory_control"])
        self.assertFalse(any(Memory(self.c).settings()["enabled"].values()))
        self.assertFalse((self.state / "fake-invocations.txt").exists())

    def test_source_worker_no_model_or_memory_and_restart(self):
        from personal_agent.sources import Sources
        from personal_agent.memory import Memory
        self.start()
        source = Sources(self.c)
        r = worker.send_source(self.c, source.text_command("我喜欢喝乌龙茶。"))
        self.assertTrue(r['source_control'])
        self.assertFalse((self.state / 'fake-invocations.txt').exists())
        self.assertEqual(Memory(self.c).list(), [])
        worker.stop(self.c); self.start()
        self.assertEqual(Sources(self.c).snapshot(r['source_id'])['content'], '我喜欢喝乌龙茶。')
        self.assertFalse((self.state / 'fake-invocations.txt').exists())

    def test_proactive_scheduled_without_client_and_restart_no_duplicate(self):
        from personal_agent.proactive import Proactive
        from personal_agent.memory import Memory
        p = Proactive(self.c); p.control({'action':'enable'})
        j = self.store.create('下次聊阳台种菜。',kind='chat',category='chat'); claimed = self.store.claim()
        mid = Memory(self.c).observe(claimed)[0]; self.store.finish(j['id'],'succeeded',{})
        p.followup(mid,1)
        self.start()
        self.wait(lambda:p.inbox(),seconds=6)
        marker = (self.state/'fake-invocations.txt').read_text()
        worker.stop(self.c); self.start(); time.sleep(.3)
        self.assertEqual(len(p.inbox()),1)
        self.assertEqual((self.state/'fake-invocations.txt').read_text(),marker)

    def test_killed_proactive_call_unknown_not_replayed(self):
        from personal_agent.proactive import Proactive
        from personal_agent.memory import Memory
        p = Proactive(self.c); p.control({'action':'enable'})
        j = self.store.create('下次聊SLOW阳台种菜。',kind='chat',category='chat'); claimed = self.store.claim()
        mid = Memory(self.c).observe(claimed)[0]; self.store.finish(j['id'],'succeeded',{})
        p.followup(mid,1); proc=self.start()
        self.wait(lambda:(self.state/'fake-invocations.txt').exists(),seconds=6)
        proc.kill(); proc.wait(timeout=3); self.start()
        self.assertEqual(p.settings()['halt_reason'],'UNKNOWN_USAGE')
        self.assertEqual(len((self.state/'fake-invocations.txt').read_text().splitlines()),1)
        self.assertIn('outcome_unknown',[x['status'] for x in p.candidates()])
        self.assertEqual(p.inbox(),[])

    def test_idle_cli_displays_new_message_without_user_enter(self):
        from personal_agent.proactive import Proactive
        from personal_agent.memory import Memory
        engine=Proactive(self.c); engine.control({'action':'enable'})
        j=self.store.create('下次聊阳台种菜。',kind='chat',category='chat'); claimed=self.store.claim()
        mid=Memory(self.c).observe(claimed)[0]; self.store.finish(j['id'],'succeeded',{})
        engine.followup(mid,2); self.start()
        output=self.root/'terminal.txt'
        with output.open('w') as out:
            client=subprocess.Popen([sys.executable,str(app.ROOT/'run.py'),'--config',str(self.path)],stdin=subprocess.PIPE,stdout=out,stderr=out,text=True)
            self.processes.append(client)
            self.wait(lambda:'新消息' in output.read_text(),seconds=7)
            client.stdin.write('/exit\n'); client.stdin.flush(); client.wait(timeout=3); client.stdin.close()
        self.assertIn('想先聊哪一点',output.read_text())

    def test_concurrent_clients_unique_claims(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda i: Store(self.c).create("goal " + str(i))["id"], range(12)))
        claims = [self.store.claim()["id"] for _ in ids]
        self.assertEqual(set(ids), set(claims))
        self.assertIsNone(self.store.claim())


if __name__ == "__main__":
    unittest.main()

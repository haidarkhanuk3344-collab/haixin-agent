import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from personal_agent import app


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.c = {"state": self.state, "price_file": app.ROOT / "config/prices.json"}

    def tearDown(self):
        self.tmp.cleanup()

    def response(self, **kw):
        body = {"payloads": [{"text": "合成回复"}], "meta": {"agentMeta": {"model": "deepseek-flash",
                "usage": {"input": 100, "cacheRead": 200, "cacheWrite": 0, "output": 30, "total": 330}}}}
        body.update(kw)
        return json.dumps(body)

    def test_session_reuse_and_new_preserves_other_data(self):
        (self.state / "existing.txt").write_text("保留")
        sid = app.session_id(self.state)
        self.assertEqual(sid, app.session_id(self.state))
        self.assertNotEqual(sid, app.session_id(self.state, True))
        self.assertEqual((self.state / "existing.txt").read_text(), "保留")
        self.assertEqual((self.state / "interactive-session.txt").stat().st_mode & 0o777, 0o600)

    def test_corrupt_session_not_silently_replaced(self):
        f = self.state / "interactive-session.txt"
        f.write_text("broken")
        with self.assertRaises(app.AppError):
            app.session_id(self.state)
        self.assertEqual(f.read_text(), "broken")

    def test_lock_across_processes(self):
        code = "from pathlib import Path; from personal_agent.app import exclusive; import sys\nwith exclusive(Path(sys.argv[1])): print('acquired')"
        with app.exclusive(self.state):
            p = subprocess.run([sys.executable, "-c", code, str(self.state)], capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
        p = subprocess.run([sys.executable, "-c", code, str(self.state)], capture_output=True, text=True)
        self.assertEqual(p.stdout.strip(), "acquired")

    def test_usage_not_double_counted(self):
        u = {"input": 100, "cacheRead": 200, "cacheWrite": 0, "output": 30, "total": 330}
        r = app.usage_record(u, app.pricing(self.c), "deepseek-flash")
        self.assertEqual(r["usage"]["total"], 330)
        self.assertAlmostEqual(r["estimated_usd"], (30 + 1.2 + 36) / 1_000_000)

    def test_missing_or_invalid_usage_is_unknown(self):
        for u in (None, {}, [], {"input": 0}, {"input": True, "cacheRead": 0, "cacheWrite": 0, "output": 0, "total": 1},
                  {"input": 2, "cacheRead": 0, "cacheWrite": 0, "output": 0, "total": 9}):
            r = app.usage_record(u, app.pricing(self.c), "deepseek-flash")
            self.assertEqual(r["usage_state"], "unknown")
            self.assertIsNone(r["estimated_usd"])

    def test_price_model_mismatch_unknown_cost(self):
        u = {"input": 0, "cacheRead": 0, "cacheWrite": 0, "output": 1, "total": 1}
        self.assertIsNone(app.usage_record(u, app.pricing(self.c), "other-model")["estimated_usd"])

    def test_unpriced_cache_write_is_unknown_cost(self):
        u = {"input": 0, "cacheRead": 0, "cacheWrite": 10, "output": 1, "total": 11}
        r = app.usage_record(u, app.pricing(self.c), "deepseek-flash")
        self.assertEqual(r["usage_state"], "known")
        self.assertIsNone(r["estimated_usd"])

    def test_missing_price_is_unknown_cost(self):
        c = dict(self.c, price_file=self.state / "missing.json")
        self.assertIsNone(app.pricing(c))

    def test_execute_redacts_stdout_discards_stderr(self):
        helper = self.state / "echo.py"
        helper.write_text("import os,sys\nprint(os.environ['DEEPSEEK_API_KEY'])\nprint('private stderr',file=sys.stderr)\n")
        c = dict(self.c, node=Path(sys.executable), cli=helper, runtime=self.state, process_timeout_seconds=2)
        with app.exclusive(self.state) as fd:
            code, out = app.execute(c, [], "fake-test", fd)
        self.assertEqual(code, 0)
        self.assertNotIn("fake-test", out)
        self.assertNotIn("private stderr", out)

    def test_invalid_response(self):
        for text in ("not json", "[]", '{"payloads":"wrong"}', '{"meta":null}', '{"payloads":[{"text":12}]}'):
            with self.assertRaises(app.AppError):
                app.parse_response(text)

    def test_valid_nested_response(self):
        self.assertEqual(app.parse_response('{"result":' + self.response() + '}')["texts"], ["合成回复"])

    def test_redacts_exact_credential_and_key_pattern(self):
        text = app.redact("error fake-secret " + "sk-" + "a" * 24, "fake-secret")
        self.assertNotIn("fake-secret", text)
        self.assertNotIn("a" * 24, text)

    @patch.object(app, "credential", return_value="fake-test")
    @patch.object(app, "execute")
    def test_success_recorded_once_without_text(self, execute, credential):
        execute.return_value = (0, self.response())
        r = app.chat(self.c, "private synthetic prompt")
        self.assertNotIn("error", r)
        g = app.summary(self.c)["categories"]["chat"]
        self.assertEqual(g["requests"], 1)
        self.assertEqual(g["total"], 330)
        log = (self.state / "usage-v1.jsonl").read_text()
        self.assertNotIn("private synthetic prompt", log)
        self.assertNotIn("合成回复", log)
        self.assertNotIn("fake-test", log)
        self.assertEqual(len(log.splitlines()), 2)

    @patch.object(app, "credential", return_value="fake-test")
    @patch.object(app, "execute")
    def test_failure_preserves_known_usage(self, execute, credential):
        execute.return_value = (1, self.response())
        r = app.chat(self.c, "test")
        self.assertIn("error", r)
        self.assertEqual(r["usage"]["total"], 330)
        self.assertEqual(r["texts"], [])
        self.assertEqual(execute.call_count, 1)

    @patch.object(app, "credential", return_value="fake-test")
    @patch.object(app, "execute", side_effect=app.AppError("OUTCOME_UNKNOWN", "已中止"))
    def test_timeout_unknown_no_retry(self, execute, credential):
        r = app.chat(self.c, "test")
        self.assertEqual(r["error"]["code"], "OUTCOME_UNKNOWN")
        g = app.summary(self.c)["categories"]["chat"]
        self.assertEqual(g["unknown_usage"], 1)
        self.assertEqual(g["unknown_cost"], 1)
        self.assertEqual(execute.call_count, 1)

    @patch.object(app, "credential", side_effect=app.AppError("CREDENTIAL", "不可读"))
    @patch.object(app, "execute")
    def test_missing_credential_prevents_api(self, execute, credential):
        with self.assertRaises(app.AppError):
            app.chat(self.c, "test")
        execute.assert_not_called()
        self.assertFalse((self.state / "usage-v1.jsonl").exists())

    @patch.object(app, "credential")
    def test_invalid_input_rejected_before_key(self, credential):
        for message in ("", " " * 3, "x" * 32001):
            with self.assertRaises(app.AppError):
                app.chat(self.c, message)
        credential.assert_not_called()

    def test_started_and_corrupt_records_not_zero_cost(self):
        app.append_event(self.state, {"request_id": str(uuid.uuid4()), "category": "chat", "status": "started"})
        with (self.state / "usage-v1.jsonl").open("a") as f:
            f.write("partial\n")
        s = app.summary(self.c)
        self.assertEqual(s["categories"]["chat"]["unknown_usage"], 1)
        self.assertEqual(s["unreadable_records"], 1)

    def test_invalid_config_does_not_initialize_data(self):
        p = self.state / "bad.json"
        p.write_text('{"schema_version":8}')
        with self.assertRaises(app.AppError):
            app.load_config(p)
        self.assertEqual(list(self.state.iterdir()), [p])

    def test_execute_timeout_kills_child(self):
        helper = self.state / "sleep.py"
        helper.write_text("import time\ntime.sleep(20)\n")
        c = dict(self.c, node=Path(sys.executable), cli=helper, runtime=self.state, process_timeout_seconds=0.05)
        with app.exclusive(self.state) as fd:
            with self.assertRaises(app.AppError) as err:
                app.execute(c, [], "fake-test", fd)
        self.assertEqual(err.exception.code, "OUTCOME_UNKNOWN")


if __name__ == "__main__":
    unittest.main()

import json
import os
import tempfile
import time
import unittest

from helpers import FAKE, slurp

from depot2 import schema
from depot2.executors import CallFailed, CallSpec, FatalError
from depot2.executors.cli_agent import build_argv, run_call


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="team-cli-")
        self.sys_path = os.path.join(self.dir, "system.md")
        with open(self.sys_path, "w") as f:
            f.write("rules")
        self.schema = {"type": "object", "properties": {"x": {"type": "integer"}},
                       "required": ["x"], "additionalProperties": False}
        os.environ["FAKE_STRUCTURED"] = json.dumps({"x": 1})

    def tearDown(self):
        for k in ("FAKE_MODE", "FAKE_ARGV", "FAKE_STRUCTURED", "CLAUDE_CODE_SESSION_ID", "ANTHROPIC_API_KEY"):
            os.environ.pop(k, None)

    def spec(self, **kw):
        base = {"call_id": "t/builder-r1", "model": "fake-model-1", "effort": "low",
                    "system_path": self.sys_path, "user": "hello", "schema": self.schema,
                    "timeout_s": 20, "cwd": self.dir, "raw_path": os.path.join(self.dir, "raw.jsonl")}
        base.update(kw)
        return CallSpec(**base)

    def run_fake(self, mode, **kw):
        os.environ["FAKE_MODE"] = mode
        return run_call(self.spec(**kw), executor_bin=FAKE, validate=schema.validate)

    def test_ok(self):
        res = self.run_fake("ok")
        self.assertEqual(res["structured"], {"x": 1})
        self.assertEqual(res["cost_usd"], 0.0123)
        self.assertEqual(res["usage"]["cache_read"], 900)
        self.assertEqual(res["utilization"], 0.25)
        self.assertEqual(res["tool_turns"], 0)  # StructuredOutput is not a tool turn
        self.assertTrue(os.path.getsize(os.path.join(self.dir, "raw.jsonl")) > 0)

    def test_turn_cap_kills_the_process(self):
        t0 = time.monotonic()
        with self.assertRaises(CallFailed) as cm:
            self.run_fake("tools", tools=["Read", "Grep", "Glob"], max_tool_turns=3)
        self.assertLess(time.monotonic() - t0, 8)  # killed, not run to its 200 tool uses
        self.assertIn("tool-turn cap 3", str(cm.exception))
        self.assertEqual(cm.exception.result["tool_turns"], 4)
        self.assertEqual(cm.exception.result["status"], "failed")
        self.assertIsNone(cm.exception.result["cost_usd"])  # no result event: cost unknown
        self.assertEqual(cm.exception.result["usage"]["cache_read"], 400)  # tokens still counted

    def test_free_pass_allows_no_tool_turn(self):
        with self.assertRaises(CallFailed):
            self.run_fake("tools", max_tool_turns=0)

    def test_timeout(self):
        with self.assertRaises(CallFailed) as cm:
            self.run_fake("hang", timeout_s=0.5)
        self.assertIn("timeout", str(cm.exception))

    def test_quota_is_fatal(self):
        with self.assertRaises(FatalError) as cm:
            self.run_fake("quota")
        self.assertIn("429", str(cm.exception))
        self.assertEqual(cm.exception.result["status"], "fatal")

    def test_auth_is_fatal(self):
        with self.assertRaises(FatalError) as cm:
            self.run_fake("auth")
        self.assertIn("Not logged in", str(cm.exception))

    def test_crash_is_fatal(self):
        with self.assertRaises(FatalError) as cm:
            self.run_fake("crash")
        self.assertIn("without a result", str(cm.exception))

    def test_missing_cli_is_fatal(self):
        with self.assertRaises(FatalError):
            run_call(self.spec(), executor_bin="/nonexistent/claude")

    def test_rate_limit_rejected_is_fatal(self):
        with self.assertRaises(FatalError) as cm:
            self.run_fake("rejected")
        self.assertIn("usage limit", str(cm.exception))

    def test_budget_cap_is_a_call_failure(self):
        with self.assertRaises(CallFailed) as cm:
            self.run_fake("budget")
        self.assertIn("budget cap", str(cm.exception))

    def test_bad_structured_output_is_a_call_failure(self):
        os.environ["FAKE_STRUCTURED"] = json.dumps({"x": "not an int"})
        with self.assertRaises(CallFailed) as cm:
            self.run_fake("ok")
        self.assertIn("schema", str(cm.exception))

    def test_flags_and_environment(self):
        argv_out = os.path.join(self.dir, "argv.json")
        os.environ["FAKE_ARGV"] = argv_out
        os.environ["CLAUDE_CODE_SESSION_ID"] = "parent-session"
        os.environ["ANTHROPIC_API_KEY"] = "must-not-leak"
        self.run_fake("ok")
        got = json.loads(slurp(argv_out))
        argv = got["argv"]
        for flag in ("-p", "--safe-mode", "--restricted", "--strict-mcp-config", "--no-session-persistence",
                     "--disable-slash-commands"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertNotIn("--mcp-config", argv)
        self.assertNotIn("--add-dir", argv)
        self.assertEqual(got["stdin"], "hello")
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", got["env"])
        self.assertNotIn("ANTHROPIC_API_KEY", got["env"])

    def test_paid_pass_flags(self):
        argv = build_argv(self.spec(tools=["Read", "Grep", "Glob"], max_tool_turns=6,
                                    add_dirs=["/repo"], deny=["Read(//proc/**)"]), "claude")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(argv[-4:], ["--add-dir", "/repo", "--disallowedTools", "Read(//proc/**)"])


if __name__ == "__main__":
    unittest.main()

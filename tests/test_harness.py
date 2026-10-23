import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HARNESS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "harness.py")


def long_function(name, body_lines, marker=""):
    lines = ([marker] if marker else []) + ["def %s():" % name]
    return "\n".join(lines + ["    x = %d" % i for i in range(body_lines - 1)]) + "\n"


class HarnessTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.git("config", "commit.gpgsign", "false")
        self.write("seed.txt", "seed\n")
        self.commit()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def git(self, *args):
        subprocess.run(["git", "-C", self.root] + list(args), check=True, capture_output=True)

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "c")

    def run_cmd(self, command, payload=None, cwd=None, env=None, raw=None):
        data = raw if raw is not None else json.dumps(dict({"cwd": cwd or self.root, "session_id": "s1"}, **(payload or {})))
        p = subprocess.run([sys.executable, HARNESS, command], input=data.encode(), capture_output=True,
                           cwd=cwd or self.root, env=dict(os.environ, **(env or {})))
        return p.returncode, p.stdout.decode("utf-8")

    def pre(self, rel, tool="Edit", env=None):
        return self.run_cmd("pre", {"tool_name": tool, "tool_input": {"file_path": os.path.join(self.root, rel)}}, env=env)

    def assert_denied(self, result, rule):
        code, stdout = result
        self.assertEqual(code, 0)
        decision = json.loads(stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertTrue(decision["permissionDecisionReason"].startswith(rule), decision)

    def assert_allowed(self, result):
        self.assertEqual(result, (0, ""))

    def stop_reason(self, payload=None):
        code, stdout = self.run_cmd("stop", payload)
        self.assertEqual(code, 0)
        return json.loads(stdout)["reason"] if stdout else ""

    def state(self):
        with open(os.path.join(self.root, ".git", "my-harness", "state.json"), encoding="utf-8") as f:
            return json.load(f)

    def change_files(self, n):
        for i in range(n):
            self.write("f%02d.txt" % i, "v1\n")
        self.commit()
        for i in range(n):
            self.write("f%02d.txt" % i, "v2\n")

    def test_t01_outside_repo(self):
        other = tempfile.mkdtemp()
        try:
            for command in ("session", "pre", "stop", "status"):
                self.assertEqual(self.run_cmd(command, cwd=other), (0, ""))
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_t02_fast_path(self):
        self.change_files(10)
        self.assert_allowed(self.pre("f04.txt"))

    def test_t03_r1_pre(self):
        self.change_files(11)
        self.git("checkout", "--", "f10.txt")
        self.assert_denied(self.pre("f10.txt"), "R1")

    def test_t04_r2_pre(self):
        for i in range(3):
            self.write("n%d.txt" % i, "x\n")
        self.assert_denied(self.pre("n3.txt", "Write"), "R2")

    def test_t05_r3(self):
        self.write("src/utils.py", "x = 1\n")
        self.commit()
        self.assert_denied(self.pre("src/utils_v2.py", "Write"), "R3")
        self.assert_denied(self.pre("src/utils_new.py", "Write"), "R3")

    def test_t05b_r3_allows_versioned_without_original(self):
        self.assert_allowed(self.pre("api_v2.py", "Write"))

    def test_t06_r3_ignores_existing(self):
        self.write("old_backup.py", "x = 1\n")
        self.commit()
        self.assert_allowed(self.pre("old_backup.py"))

    def test_t07_r4_outside_docs(self):
        self.write("docs/a.md", "# a\n")
        self.write("README.md", "# readme\n")
        self.commit()
        self.assert_denied(self.pre("PLAN.md", "Write"), "R4")

    def test_t07b_r4_conventional_names_exact(self):
        self.write("docs/a.md", "# a\n")
        self.commit()
        for rel in ("README-plan.md", "CHANGELOG-draft.md", "AGENTS-notes.md", "src/feature/README.md"):
            self.assert_denied(self.pre(rel, "Write"), "R4")
        self.assert_allowed(self.pre("CHANGELOG.md", "Write"))

    def test_t08_r4_inside_docs(self):
        self.write("docs/a.md", "# a\n")
        self.commit()
        self.assert_allowed(self.pre("docs/b.md", "Write"))

    def test_t09_decisions(self):
        self.assert_allowed(self.pre("decisions.md", "Write"))
        code, stdout = self.run_cmd("session")
        self.assertIn("R1", stdout)
        with open(os.path.join(self.root, ".git", "info", "exclude"), encoding="utf-8") as f:
            self.assertIn("/decisions.md", f.read().splitlines())
        self.write("decisions.md", "notes\n")
        self.run_cmd("stop")
        self.assertEqual(self.state()["rules"]["R1"]["value"], 0)

    def test_t10_bash_bypass(self):
        self.write("docs/a.md", "# a\n")
        self.commit()
        self.write("notes.md", "scratch\n")
        self.assertTrue(self.stop_reason().startswith("R4"))

    def test_t11_r5(self):
        self.write("app.py", long_function("load_orders", 60))
        reason = self.stop_reason()
        self.assertIn("R5", reason)
        self.assertIn("load_orders", reason)
        self.assertIn("60", reason)

    def test_t12_hack_escape(self):
        self.write("app.py", long_function("load_orders", 60, "# hack: one-off migration"))
        self.assertEqual(self.stop_reason(), "")
        self.assertTrue(self.state()["rules"]["R5"]["ok"])
        self.assertEqual(self.state()["rules"]["R8"]["value"], 1)

    def test_t13_old_function_not_punished(self):
        self.write("legacy.py", long_function("old", 80))
        self.commit()
        self.write("other.py", "y = 2\n")
        self.assertEqual(self.stop_reason(), "")
        self.assertTrue(self.state()["rules"]["R5"]["ok"])

    def test_t14_r6(self):
        self.write("c.py", "\n".join(["# note %d" % i for i in range(8)] + ["v%d = %d" % (i, i) for i in range(12)]) + "\n")
        self.assertIn("R6", self.stop_reason())

    def test_t15_retry_limit(self):
        self.write("app.py", long_function("load_orders", 60))
        self.assertEqual(self.stop_reason({"stop_hook_active": True}), "")
        self.assertFalse(self.state()["rules"]["R5"]["ok"])
        self.assertTrue(self.stop_reason())
        self.assertEqual(self.stop_reason({"stop_hook_active": True}), "")

    def test_t15b_new_turn_blocks_again(self):
        self.write("app.py", long_function("load_orders", 60))
        self.assertTrue(self.stop_reason())
        self.assertEqual(self.stop_reason({"stop_hook_active": True}), "")
        self.assertTrue(self.stop_reason())
        self.git("checkout", "--", ".")
        os.remove(os.path.join(self.root, "app.py"))
        self.assertEqual(self.stop_reason(), "")
        self.assertNotIn("s1", self.state()["retries"])

    def test_t16_fail_open(self):
        self.assertEqual(self.run_cmd("pre", raw="{not json"), (0, ""))
        with open(os.path.join(self.root, ".git", "my-harness", "error.log"), encoding="utf-8") as f:
            self.assertTrue(f.read().strip())

    def test_t16b_error_logged_in_json_cwd(self):
        other = tempfile.mkdtemp()
        try:
            with open(os.path.join(self.root, ".git", "my-harness.toml"), "w", encoding="utf-8") as f:
                f.write("max_files = [\n")
            code, _ = self.run_cmd("stop", {"cwd": self.root}, cwd=other)
            self.assertEqual(code, 0)
            self.assertTrue(os.path.exists(os.path.join(self.root, ".git", "my-harness", "error.log")))
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_t17_deterministic(self):
        self.write("app.py", long_function("a", 60) + long_function("b", 55))
        self.write("x_v2.py", "z = 1\n")
        first = subprocess.run([sys.executable, HARNESS, "check"], cwd=self.root, capture_output=True).stdout
        second = subprocess.run([sys.executable, HARNESS, "check"], cwd=self.root, capture_output=True).stdout
        self.assertIn(b"R5", first)
        self.assertEqual(first, second)

    def test_t18_status_after_commit(self):
        self.write("a.txt", "a\n")
        self.run_cmd("stop")
        self.commit()
        code, stdout = self.run_cmd("status")
        self.assertIn("files 0/10", stdout)
        self.assertIn("✓", stdout)

    def test_t19_repo_config(self):
        with open(os.path.join(self.root, ".git", "my-harness.toml"), "w", encoding="utf-8") as f:
            f.write("max_files = 3\n")
        self.change_files(4)
        self.git("checkout", "--", "f03.txt")
        self.assert_denied(self.pre("f03.txt"), "R1: change set would reach 4 files (limit 3)")

    def test_t21_harness_passes_own_rules(self):
        source = os.path.dirname(HARNESS)
        for rel in ("harness.py", "tests/test_harness.py", "tests/bench.py"):
            with open(os.path.join(source, rel), encoding="utf-8") as f:
                self.write(rel, f.read())
        report = subprocess.run([sys.executable, HARNESS, "check"], cwd=self.root, capture_output=True).stdout.decode()
        for rid in ("R3", "R4", "R5", "R6"):
            self.assertTrue(any(line.startswith(rid) and line.endswith("ok") for line in report.splitlines()), report)

    def test_t20_kill_switch(self):
        self.change_files(11)
        self.git("checkout", "--", "f10.txt")
        self.assert_allowed(self.pre("f10.txt", env={"MY_HARNESS_OFF": "1"}))


if __name__ == "__main__":
    unittest.main()

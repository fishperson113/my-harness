import json
import re
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
        for rel in ("README-plan.md", "CHANGELOG-draft.md", "AGENTS-notes.md", "src/feature/README.md",
                    "CLAUDE.md", "AGENTS.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md"):
            self.assert_denied(self.pre(rel, "Write"), "R4")

    def test_t07c_root_opened_by_config(self):
        self.write("README.md", "# readme\n")
        self.commit()
        self.assert_denied(self.pre("CLAUDE.md", "Write"), "R4")
        with open(os.path.join(self.root, ".git", "my-harness.toml"), "w", encoding="utf-8") as f:
            f.write('docs_extra_dirs = ["."]\n')
        self.assert_allowed(self.pre("CLAUDE.md", "Write"))

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

    def test_t27_harness_passes_own_rules(self):
        source = os.path.dirname(HARNESS)
        for rel in ("harness.py", "tests/test_harness.py", "tests/bench.py"):
            with open(os.path.join(source, rel), encoding="utf-8") as f:
                self.write(rel, f.read())
        report = subprocess.run([sys.executable, HARNESS, "check"], cwd=self.root, capture_output=True).stdout.decode()
        for rid in ("R3", "R4", "R5", "R6"):
            self.assertTrue(any(line.startswith(rid) and line.endswith("ok") for line in report.splitlines()), report)

    def test_t28_r9_counts_added_lines_only(self):
        self.write("dead.txt", "".join("line %d\n" % i for i in range(900)))
        self.commit()
        os.remove(os.path.join(self.root, "dead.txt"))
        self.write("more.txt", "".join("x %d\n" % i for i in range(410)))
        self.assertEqual(self.stop_reason(), "")
        r9 = self.state()["rules"]["R9"]
        self.assertEqual((r9["value"], r9["ok"]), (410, False))
        self.commit()
        code, stdout = self.run_cmd("status")
        self.assertIn("lines 0/400", stdout)

    def test_t29_generated_files_skipped(self):
        self.write(".gitattributes", "gen/*.py linguist-generated\nschema.sql -diff\n")
        self.commit()
        self.write("package-lock.json", "{}\n" * 600)
        self.write("poetry.lock", "x\n" * 600)
        self.write("gen/api.py", long_function("huge", 80))
        self.write("app.py", "a = 1\n" * 20)
        self.assertEqual(self.stop_reason(), "")
        self.assertEqual(self.state()["rules"]["R9"]["value"], 20)

    def test_t21_status_green_after_commit(self):
        self.write("app.py", long_function("load_orders", 60))
        self.assertTrue(self.stop_reason())
        self.commit()
        code, stdout = self.run_cmd("status")
        self.assertIn("✓", stdout)
        self.assertNotIn("✗", stdout)

    def test_t22_old_hack_exemption_counted(self):
        self.write("c.py", "# hack: generated fixtures\nx = 1\n")
        self.commit()
        self.write("c.py", "# hack: generated fixtures\nx = 1\n" + "".join("# note %d\nv%d = %d\n" % (i, i, i) for i in range(8)))
        self.assertEqual(self.stop_reason(), "")
        r8 = self.state()["rules"]["R8"]
        self.assertGreaterEqual(r8["value"], 1)
        self.assertIn("c.py (R6)", r8["items"])

    def test_t23_staged_markdown_does_not_open_docs(self):
        self.write("docs/a.md", "# a\n")
        self.commit()
        self.write("PLAN.md", "plan\n")
        self.git("add", "PLAN.md")
        self.assertTrue(self.stop_reason().startswith("R4"))
        self.assert_denied(self.pre("NOTES.md", "Write"), "R4")

    def test_t24_uncommitted_gitattributes_ignored(self):
        self.write(".gitattributes", "* -diff\n")
        self.write("app.py", long_function("load_orders", 60))
        self.assertIn("R5", self.stop_reason())

    def test_t25_pre_denies_git_dir(self):
        code, stdout = self.pre(".git/my-harness.toml", "Write")
        self.assertEqual(json.loads(stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        code, stdout = self.pre(".git/info/exclude")
        self.assertEqual(json.loads(stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_t26_one_line_python_function(self):
        self.write("one.py", "def f(): return 1\n" + "x = 1\n" * 70 + "def g(a: int,\n      b: int) -> int:\n    return a\n")
        self.assertEqual(self.stop_reason(), "")
        self.assertTrue(self.state()["rules"]["R5"]["ok"])

    def test_t20_kill_switch(self):
        self.change_files(11)
        self.git("checkout", "--", "f10.txt")
        self.assert_allowed(self.pre("f10.txt", env={"MY_HARNESS_OFF": "1"}))

    def write_transcript(self, entries, raw_lines=()):
        path = os.path.join(self.root, ".git", "transcript.jsonl")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(json.dumps(e) + "\n" for e in entries) + "".join(line + "\n" for line in raw_lines))
        return path

    def claims_stop(self, entries, payload=None, raw_lines=()):
        path = self.write_transcript(entries, raw_lines)
        reason = self.stop_reason(dict({"transcript_path": path}, **(payload or {})))
        return reason, self.state()["rules"]["R10"]

    def test_t30_real_transcript(self):
        sys.path.insert(0, os.path.dirname(HARNESS))
        import harness
        with open(os.path.join(os.path.dirname(__file__), "fixtures", "transcript_real.jsonl"), encoding="utf-8") as f:
            text = f.read().replace("{REPO}", json.dumps(self.root)[1:-1]).replace("{SEP}", json.dumps(os.sep)[1:-1])
        path = os.path.join(self.root, ".git", "real.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        objs = harness.read_transcript_tail(path, 1 << 23)
        self.assertTrue(any(harness.is_prompt(o) for o in objs))
        self.assertFalse(any(harness.is_prompt(o) for o in objs if o.get("isMeta")))
        parsed = harness.parse_transcript(harness.find_repo(self.root), path, 1 << 23)
        outcomes = [(cmd, outcome, in_turn) for cmd, outcome, _, in_turn in parsed["runs"]]
        self.assertIn(("echo hello", "ok", True), outcomes)
        self.assertIn(("false", "fail", True), outcomes)
        self.assertNotIn("ls", [cmd for cmd, _, _ in outcomes])
        self.assertIn("app.py", [p for p, _ in parsed["edits"]])

    def test_t31_no_transcript(self):
        self.write("app.py", long_function("load_orders", 60))
        reason = self.stop_reason()
        self.assertTrue(reason.startswith("R5"))
        self.assertNotIn("R10", reason)
        r10 = self.state()["rules"]["R10"]
        self.assertEqual((r10["value"], r10["ok"]), (None, True))

    def test_t32_broken_json_line(self):
        self.write("a.py", "def load_orders(x):\n    return x\n")
        reason, r10 = self.claims_stop([prompt(), say('[[src a.py:1 "def load_orders("]]')], raw_lines=["{broken"])
        self.assertEqual((reason, r10["value"], r10["limit"]), ("", 1, 1))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".git", "my-harness", "error.log")))

    def test_t33_src_exact(self):
        self.write("a.py", "def load_orders(x):\n    return x\n")
        reason, r10 = self.claims_stop([prompt(), say('Evidence: [[src a.py:1 "def load_orders("]]')])
        self.assertEqual((reason, r10["value"], r10["limit"], r10["ok"]), ("", 1, 1, True))

    def test_t34_src_whitespace(self):
        self.write("a.py", "def f(x):\n    return\tx  +  1\n")
        reason, r10 = self.claims_stop([prompt(), say('[[src a.py:2 "return x + 1"]]')])
        self.assertEqual((reason, r10["value"]), ("", 1))

    def test_t35_src_moved(self):
        self.write("a.py", "def load_orders(x):\n    return x\n")
        reason, r10 = self.claims_stop([prompt(), say('[[src a.py:2 "def load_orders("]]')])
        self.assertTrue(reason.startswith("R10:"), reason)
        self.assertIn("found at L1", reason)

    def test_t36_src_mismatch(self):
        self.write("a.py", "def load_orders(x):\n    return x\n")
        reason, r10 = self.claims_stop([prompt(), say('[[src a.py:1 "def save_orders("]]')])
        self.assertIn("snippet not found in the file", reason)
        self.assertFalse(r10["ok"])

    def test_t37_src_missing_file(self):
        reason, r10 = self.claims_stop([prompt(), say('[[src nope.py:1 "abc"]] [[src .git/HEAD:1 "ref:"]]')])
        self.assertEqual(reason.count("file not found in the repo"), 2)

    def test_t38_src_out_of_range(self):
        self.write("a.py", "x = 1\n")
        reason, r10 = self.claims_stop([prompt(), say('[[src a.py:5 "x = 1"]]')])
        self.assertIn("file has 1 lines", reason)

    def test_t39_src_range_across_lines(self):
        self.write("etl.py", "a = 1\nrun(\n    retries=3,\n)\n")
        reason, r10 = self.claims_stop([prompt(), say('[[src etl.py:2-4 "run( retries=3"]]')])
        self.assertEqual((reason, r10["value"]), ("", 1))

    def test_t40_tags_in_code_ignored(self):
        text = 'Syntax:\n```\n[[src nope.py:1 "abc"]]\n```\nand `[[ran "zzz"]]` inline.'
        reason, r10 = self.claims_stop([prompt(), say(text)])
        self.assertEqual((reason, r10["limit"]), ("", 0))

    def test_t41_malformed(self):
        reason, r10 = self.claims_stop([prompt(), say('[[src a.py "x"]]')])
        self.assertIn("tag is malformed", reason)

    def test_t42_duplicates_and_max_claims(self):
        line = "abcdefghijklmnopqrstuvwxyz0123456789" * 2
        self.write("a.py", line + "\n")
        tags = ['[[src a.py:1 "%s"]]' % line[:n] for n in range(3, 63)]
        with open(os.path.join(self.root, ".git", "my-harness.toml"), "w", encoding="utf-8") as f:
            f.write("max_claims = 50\n")
        reason, r10 = self.claims_stop([prompt(), say(" ".join(tags + tags[:5]))])
        self.assertEqual((reason, r10["value"], r10["limit"], r10["skipped"]), ("", 50, 50, 10))

    def test_t43_ran_previous_turn(self):
        entries = [prompt("first")] + bash("pytest -q", False) + [prompt("second"), say('[[ran "pytest"]]')]
        reason, r10 = self.claims_stop(entries)
        self.assertIn("no matching Bash command in this turn", reason)

    def test_t44_ran_substring(self):
        reason, r10 = self.claims_stop([prompt()] + bash("cd x &&  pytest -q", False) + [say('[[ran "pytest"]]')])
        self.assertEqual((reason, r10["value"]), ("", 1))

    def test_t45_ran_outcomes(self):
        entries = [prompt()] + bash("pytest", True) + [say('[[ran "pytest" ok]] [[ran "pytest" fail]]')]
        reason, r10 = self.claims_stop(entries)
        self.assertEqual((r10["value"], r10["limit"]), (1, 2))
        self.assertEqual(reason.count("R10:"), 1)
        self.assertIn('[[ran "pytest" ok]] — command ran but the result was the opposite', reason)

    def test_t46_ran_before_stop_block(self):
        entries = [prompt()] + bash("pytest", False) + [say("done"), feedback(), say('[[ran "pytest" ok]]')]
        reason, r10 = self.claims_stop(entries)
        self.assertEqual((reason, r10["value"]), ("", 1))

    def test_t47_subagent_bash_ignored(self):
        entries = [prompt()] + [dict(e, isSidechain=True) for e in bash("pytest", False)] + [say('[[ran "pytest"]]')]
        reason, r10 = self.claims_stop(entries)
        self.assertIn("no matching Bash command", reason)

    def test_t48_r10_block_then_continuation(self):
        entries = [prompt(), say('[[ran "make test"]]')]
        reason, _ = self.claims_stop(entries)
        self.assertTrue(reason.startswith("R10:"))
        reason, _ = self.claims_stop(entries, {"stop_hook_active": True})
        self.assertEqual(reason, "")

    def test_t59_corrected_claim_after_block(self):
        self.write("a.py", "def load_orders(x):\n    return x\n")
        wrong = [prompt(), say('[[src a.py:2 "def load_orders("]]')]
        reason, _ = self.claims_stop(wrong)
        self.assertTrue(reason.startswith("R10:"))
        fixed = wrong + [feedback(), say('[[src a.py:1 "def load_orders("]]')]
        reason, r10 = self.claims_stop(fixed, {"stop_hook_active": True})
        self.assertEqual((reason, r10["value"], r10["limit"], r10["ok"]), ("", 1, 1, True))
        self.assertNotIn("✗", self.status_text())

    def r11_stop(self, entries):
        self.claims_stop(entries)
        return self.state()["rules"]["R11"]

    def status_text(self):
        return re.sub(r"\033\[\d+m", "", self.run_cmd("status")[1])

    def test_t49_r11_missing(self):
        self.write("etl.py", "x = 1\n")
        self.assertEqual(self.r11_stop([prompt(), edit(self.root, "etl.py")])["value"], "missing")
        self.assertTrue(self.run_cmd("status")[1].startswith("\033[33m"))
        self.assertIn("tested –", self.status_text())

    def test_t50_r11_pass_then_missing(self):
        self.write("etl.py", "x = 1\n")
        entries = [prompt(), edit(self.root, "etl.py")] + bash("pytest -q", False)
        self.assertEqual(self.r11_stop(entries)["value"], "pass")
        self.assertEqual(self.r11_stop(entries + [edit(self.root, "etl.py")])["value"], "missing")

    def test_t51_r11_fail(self):
        self.write("etl.py", "x = 1\n")
        rule = self.r11_stop([prompt(), edit(self.root, "etl.py")] + bash("python -m pytest", True))
        self.assertEqual((rule["value"], rule["ok"]), ("fail", False))

    def test_t52_r11_none_after_commit(self):
        self.write("etl.py", "x = 1\n")
        self.assertEqual(self.r11_stop([prompt(), edit(self.root, "etl.py")])["value"], "missing")
        self.commit()
        self.assertNotIn("tested", self.status_text())
        self.assertEqual(self.state()["rules"]["R11"]["value"], "none")

    def set_config(self, text):
        with open(os.path.join(self.root, ".git", "my-harness.toml"), "w", encoding="utf-8") as f:
            f.write(text)

    def test_t53_verify_patterns_empty(self):
        self.set_config("verify_patterns = []\n")
        self.write("etl.py", "x = 1\n")
        self.assertEqual(self.r11_stop([prompt(), edit(self.root, "etl.py")])["value"], "none")
        self.assertNotIn("R11", self.run_cmd("session")[1])

    def test_t54_claims_disabled(self):
        self.set_config("claims_enabled = false\n")
        reason, r10 = self.claims_stop([prompt(), say('[[ran "make test"]]')])
        self.assertEqual((reason, r10["value"]), ("", None))
        session = self.run_cmd("session")[1]
        self.assertNotIn("R10", session)
        self.assertLessEqual(len(session.splitlines()), 20)

    def test_t55_statusline_priorities(self):
        self.write("app.py", long_function("load_orders_with_a_really_long_function_name", 60))
        self.claims_stop([prompt(), edit(self.root, "app.py"), say('[[ran "make test"]]')])
        raw = self.run_cmd("status")[1]
        text = re.sub(r"\033\[\d+m", "", raw)
        self.assertTrue(raw.startswith("\033[31m"))
        self.assertLessEqual(len(text), 80)
        self.assertTrue(text.startswith("my-harness ✗ R5 load_orders"), text)
        self.assertNotIn("…", text)
        self.commit()
        self.write("a.py", "x = 1\n")
        entries = [prompt(), edit(self.root, "a.py")] + bash("pytest", False) + [say('[[src a.py:1 "x = 1"]]')]
        self.claims_stop(entries)
        raw = self.run_cmd("status")[1]
        self.assertTrue(raw.startswith("\033[32m"), raw)
        self.assertIn("claims 1/1 · tested ✓ · files 1/10", raw)
        self.claims_stop(entries + [prompt("what does a.py do?"), say("It sets x.")])
        self.assertIn("claims – · tested ✓", self.status_text())

    def test_t56_check_transcript_option(self):
        self.claims_stop([prompt(), say('[[ran "make test"]]')])
        path = self.write_transcript([prompt(), say('[[ran "make test"]]')])
        live = subprocess.run([sys.executable, HARNESS, "check", "--transcript", path], cwd=self.root,
                              capture_output=True).stdout.decode("utf-8")
        self.assertIn('R10: [[ran "make test"]] — no matching Bash command in this turn.', live)
        cached = subprocess.run([sys.executable, HARNESS, "check"], cwd=self.root, capture_output=True).stdout.decode()
        self.assertIn("(from last Stop)", cached)

    def test_t57_stop_deterministic(self):
        self.write("a.py", "def f(x):\n    return x\n")
        entries = [prompt(), say('[[src a.py:2 "def f("]] [[ran "pytest"]] [[src b.py:1 "abc"]] [[src a.py "x"]]')]
        first, _ = self.claims_stop(entries)
        second, _ = self.claims_stop(entries)
        self.assertTrue(first)
        self.assertEqual(first.encode(), second.encode())

    def test_t58_harness_claims_on_itself(self):
        with open(HARNESS, encoding="utf-8") as f:
            source = f.read()
        self.write("harness.py", source)
        line = source.splitlines().index("def main(argv):") + 1
        reason, r10 = self.claims_stop([prompt(), say('Evidence: [[src harness.py:%d "def main(argv):"]]' % line)])
        self.assertEqual((reason, r10["value"]), ("", 1))
        for rid in ("R3", "R4", "R5", "R6"):
            self.assertTrue(self.state()["rules"][rid]["ok"], rid)


def prompt(text="do it"):
    return {"type": "user", "message": {"role": "user", "content": text}}


def say(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def feedback():
    return {"type": "user", "isMeta": True, "message": {"role": "user", "content": "Stop hook feedback: R5: fix it"}}


def edit(root, rel):
    call = {"type": "tool_use", "id": "toolu_edit", "name": "Edit", "input": {"file_path": os.path.join(root, rel)}}
    return {"type": "assistant", "message": {"role": "assistant", "content": [call]}}


def bash(command, error, tool_id=None):
    tool_id = tool_id or "toolu_%d" % (abs(hash(command)) % 10 ** 8)
    call = {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}}
    result = {"type": "tool_result", "tool_use_id": tool_id, "content": "", "is_error": error}
    return [{"type": "assistant", "message": {"role": "assistant", "content": [call]}},
            {"type": "user", "message": {"role": "user", "content": [result]}}]


if __name__ == "__main__":
    unittest.main()

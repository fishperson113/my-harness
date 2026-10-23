# my-harness

1. The harness only guards what the user should not have to think about. Business logic, docs and design decisions need the user's attention, so the harness leaves them alone.
2. An LLM is used only when its output goes through a deterministic check. That is why my-harness has no skills: every rule lives in a hook and a script decides.

## What it does

my-harness is a small set of Claude Code hooks that keeps every change Claude makes within a reviewable budget. It checks the size and shape of the uncommitted change set with plain git, denies writes that would break a rule, and makes Claude fix violations once before it may stop. It blocks Claude, never you, and writes nothing into your working tree.

## Install

Requires Python 3.8+ and `git`. Linux and macOS; Windows through WSL.

### Option 1: Claude Code plugin

```
/plugin marketplace add fishperson113/my-harness
/plugin install my-harness@my-harness
```

The plugin registers the SessionStart, PreToolUse and Stop hooks. It does not touch your status line. To show the status line, add this to `~/.claude/settings.json` (replace the path with where the plugin was installed, or use a standalone copy as below):

```json
"statusLine": { "type": "command", "command": "python3 ~/.claude/my-harness/harness.py status" }
```

### Option 2: standalone

1. Copy `harness.py` to `~/.claude/my-harness/harness.py`.
2. Merge the `hooks` and `statusLine` blocks from [`examples/settings.json`](examples/settings.json) into `~/.claude/settings.json`.
3. Already have a status line? Call `python3 ~/.claude/my-harness/harness.py status` from your script and append its output.

Run `python3 harness.py check` in any pane for the full R1–R11 report. It never blocks and never writes the cache. R10 and R11 come from the last Stop unless you pass `--transcript PATH`.

## Rules

| ID | Rule | Default | Checked in | Kind |
| --- | --- | --- | --- | --- |
| R1 | Files in the uncommitted change set | ≤ 10 | pre, stop | block |
| R2 | New files in that set | ≤ 3 | pre, stop | block |
| R3 | New files with copy-style names (`_new`, `_old`, `_copy`, `_backup`, `_tmp`, `_final`, `.bak`, `.orig`, `.tmp`, `~`, ` copy`; `_v2` only when the unsuffixed file exists, e.g. `api_v2.py` next to `api.py`) | 0 | pre, stop | block |
| R4 | New markdown files outside the docs dirs | 0 | pre, stop | block |
| R5 | Touched function longer than the limit | ≤ 50 lines | stop | block |
| R6 | Share of comment lines in a file's added lines | ≤ 30% | stop | block |
| R7 | Commits on the branch ahead of the base branch | ≤ 3 | stop | show |
| R8 | `hack:` exemptions actually applied (functions exempt from R5, files exempt from R6) | no limit | stop | show |
| R9 | Lines added in the change set (new files in full; deletions not counted) | ≤ 400 | stop | show |
| R10 | Claims tagged in Claude's answer this turn match the repo and the transcript ([Claims](#claims)) | all correct | stop | block |
| R11 | After the last code edit, a test command ran, and its result | shown | stop | show |

File size is never limited: a 10,000-line legacy file is fine. R9 only reports how much one change adds; it never blocks, so Claude is never pushed to compress, delete tests or revert work to fit.

- The change set is every file modified against `HEAD` (staged or not), deleted, or untracked and not ignored. A rename counts once.
- Docs dirs are directories that hold a markdown file committed at `HEAD` (staging a file does not count), plus `docs_extra_dirs`. Conventional files at the repo root (exactly `README`, `CHANGELOG`, `CONTRIBUTING`, `LICENSE`, `CODE_OF_CONDUCT`, `SECURITY`, `CLAUDE`, `AGENTS` with a markdown extension) do not make the root a docs dir. Creating one still goes through R4, so Claude cannot add `CLAUDE.md`, `AGENTS.md` or `CHANGELOG.md` on its own. When the repo really needs one, open the root with `docs_extra_dirs = ["."]` in `.git/my-harness.toml`.
- `decisions.md` at the repo root is your local scratch file. It is added to `.git/info/exclude` and exempt from all rules.
- R5 and R6 use a per-extension table (Python, JS/TS, Go, Rust, Kotlin, Java/C#/C/C++, shell, SQL, YAML/TOML). Other files skip R5 and R6.
- Generated files are not part of the change set, so no rule looks at them: built-in lockfiles (`*.lock`, `package-lock.json`, `npm-shrinkwrap.json`, `pnpm-lock.yaml`, `go.sum`, `*.min.js`, `*.min.css`), `generated_patterns`, and paths marked `-diff`, `binary` or `linguist-generated` in the root `.gitattributes` as committed at `HEAD`. A repo with no commit yet has no docs dirs and no `.gitattributes`.

## Claims

R10 checks only what Claude tags; untagged statements are not checked, and the status line shows `claims –` for a turn with no tags. Tags inside fenced code blocks or inline backticks are ignored.

| Tag | Claim |
| --- | --- |
| `[[src PATH:LINE "SNIPPET"]]` | Line LINE of PATH contains SNIPPET |
| `[[src PATH:START-END "SNIPPET"]]` | Lines START to END (at most 30) contain SNIPPET |
| `[[ran "COMMAND"]]` | COMMAND ran as a Bash command in this turn |
| `[[ran "COMMAND" ok]]` | It ran and succeeded |
| `[[ran "COMMAND" fail]]` | It ran and failed |

Whitespace runs collapse to one space before matching; case matters. `ran` matches when COMMAND is a substring of a command Claude ran this turn, so `cd x && pytest -q` satisfies `[[ran "pytest"]]`. A wrong claim (bad line, missing file, snippet not found, command not run, opposite result, malformed tag) blocks Stop once, like R1–R6.

Example footer:

```
Evidence: [[src dags/etl.py:42 "def load_orders("]] [[ran "pytest tests/test_etl.py" ok]]
```

R11 recognises test commands by `verify_patterns` (pytest, `python -m unittest`, npm/pnpm/yarn/bun test, go test, cargo test, make test/check, mvn/gradle test, dbt test/build, airflow dags/tasks test). The status line shows `tested ✓`, `tested ✗`, `tested –` (no test since the last code edit) or `tested ?`.

## Configuration

Later wins: built-in defaults, then `~/.config/my-harness/config.toml`, then `.git/my-harness.toml` in the repo. Flat `key = value` lines.

| Key | Default | Meaning |
| --- | --- | --- |
| `max_files` | 10 | R1 limit |
| `max_new_files` | 3 | R2 limit |
| `banned_name_patterns` | [] | Extra regexes for R3 |
| `docs_extra_dirs` | [] | Extra docs dirs for R4 |
| `decisions_file` | "decisions.md" | Scratch file name, excluded and exempt |
| `max_function_lines` | 50 | R5 limit |
| `max_comment_ratio` | 0.30 | R6 limit |
| `min_added_lines_for_comment_rule` | 10 | R6 skips files with fewer added non-blank lines |
| `max_commits` | 3 | R7 limit |
| `base_branch` | "" | Empty means `origin/HEAD`, then `main`, then `master` |
| `max_added_lines` | 400 | R9 limit |
| `generated_patterns` | [] | Extra globs for generated files, added to the built-in lockfile list |
| `claims_enabled` | true | false turns R10 off and drops its lines from the session rules |
| `max_claims` | 50 | Tags checked per turn; the rest are counted as skipped |
| `verify_patterns` | see [Claims](#claims) | Regexes for test commands; replaces the defaults; `[]` turns R11 off |
| `transcript_tail_bytes` | 8388608 | Bytes read from the end of the transcript (8 MB) |
| `max_retries` | 1 | Consecutive times Stop makes Claude fix violations; resets on each new user turn |

Airflow tests running in docker:

```toml
verify_patterns = ["docker compose (run|exec) .*pytest", "\\bairflow dags test\\b"]
```

State lives in `.git/my-harness/` (`state.json` cache, `facts.json` per-HEAD conventions, `error.log`). Claude cannot write anything inside `.git/` through Write/Edit.

## Shortcuts: `hack:` and `MY_HARNESS_OFF`

- A comment containing `hack:` on a function's declaration line, or in the two lines above it, exempts that function from R5.
- A `hack:` comment anywhere in a file exempts that file from R6.
- Every exemption actually applied, including one from an older `hack:` comment, is counted in R8 and turns the status line yellow, so shortcuts stay visible.
- `MY_HARNESS_OFF=1` turns every command into a no-op, for a deliberate large change.

## Uninstall

Remove the plugin, or delete the blocks you pasted into `~/.claude/settings.json`. Optionally delete `.git/my-harness/` and `.git/my-harness.toml` in each repo; they do not affect git. The `/decisions.md` line in `.git/info/exclude` stays so the scratch file is never committed by accident.

## Known limits

- Bash commands bypass `pre`. Stop still catches files that Bash creates, but not Bash edits inside `.git/`.
- R5 is a heuristic. Braces inside strings, or multi-line SQL strings indented back in a DAG file, can make it count short. Erring on the lenient side is accepted.
- Unignored directories such as `node_modules` count toward R1 and R2. That is intended: the repo should ignore them.
- The status line updates at each Stop and when `HEAD` or the index changes, not after every file edit.
- After a commit, the first command reads the committed conventions again (`git ls-tree`, `git show HEAD:.gitattributes`), so that one run makes up to two extra git calls.
- Untagged claims are not checked. `claims –` on the status line says the turn had nothing checked.
- `src` proves the code is where Claude says, not that the explanation around it is right.
- `ran` matches by substring, so `[[ran "test"]]` matches many commands. Only specific tags are real evidence.
- Edits made through Bash (`sed -i`, scripts) are not code edits for R11.
- The transcript format is internal to Claude Code and may change between versions. When it does, R10 and R11 fall back to unknown (fail-open) until the parser and `tests/fixtures/transcript_real.jsonl` are updated. Checked against a real session: subagent turns are not in the main transcript at all, and a successful tool result may omit `is_error`.
- R10 and R11 on the status line update at each Stop, except that R11 drops to `none` once a commit leaves no code in the change set.
- `harness.py` is about 1,000 lines. The only size requirement is one file with no dependencies.
- Every internal error fails open: exit 0, no decision, one line in `.git/my-harness/error.log`.

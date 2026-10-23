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

Run `python3 harness.py check` in any pane for the full R1–R8 report. It never blocks and never writes the cache.

## Rules

| ID | Rule | Default | Checked in | Kind |
| --- | --- | --- | --- | --- |
| R1 | Files in the uncommitted change set | ≤ 10 | pre, stop | block |
| R2 | New files in that set | ≤ 3 | pre, stop | block |
| R3 | New files with copy-style names (`_new`, `_old`, `_copy`, `_backup`, `_tmp`, `_final`, `.bak`, `.orig`, `.tmp`, `~`, ` copy`; `_v2` only when the unsuffixed file exists, e.g. `api_v2.py` next to `api.py`) | 0 | pre, stop | block |
| R4 | New markdown files outside the docs dirs | 0 | pre, stop | block |
| R5 | Touched function longer than the limit | ≤ 50 lines | stop | block |
| R6 | Share of comment lines in a file's added lines | ≤ 30% | stop | block |
| R7 | Commits on the branch ahead of the base branch | ≤ 3 | status | show |
| R8 | `hack:` markers in added lines | no limit | status | show |

- The change set is every file modified against `HEAD` (staged or not), deleted, or untracked and not ignored. A rename counts once.
- Docs dirs are directories that already hold a tracked markdown file, plus `docs_extra_dirs`. Conventional files at the repo root (exactly `README`, `CHANGELOG`, `CONTRIBUTING`, `LICENSE`, `CODE_OF_CONDUCT`, `SECURITY`, `CLAUDE`, `AGENTS` with a markdown extension) are always allowed there and do not make the root a docs dir. Anywhere else they follow R4 like any other markdown file.
- `decisions.md` at the repo root is your local scratch file. It is added to `.git/info/exclude` and exempt from all rules.
- R5 and R6 use a per-extension table (Python, JS/TS, Go, Rust, Kotlin, Java/C#/C/C++, shell, SQL, YAML/TOML). Other files skip R5 and R6.

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
| `max_retries` | 1 | Consecutive times Stop makes Claude fix violations; resets on each new user turn |

State lives in `.git/my-harness/` (`state.json` cache, `error.log`).

## Shortcuts: `hack:` and `MY_HARNESS_OFF`

- A comment containing `hack:` on a function's declaration line, or in the two lines above it, exempts that function from R5.
- A `hack:` comment anywhere in a file exempts that file from R6.
- Every `hack:` marker is counted in R8 and turns the status line yellow, so shortcuts stay visible.
- `MY_HARNESS_OFF=1` turns every command into a no-op, for a deliberate large change.

## Uninstall

Remove the plugin, or delete the blocks you pasted into `~/.claude/settings.json`. Optionally delete `.git/my-harness/` and `.git/my-harness.toml` in each repo; they do not affect git. The `/decisions.md` line in `.git/info/exclude` stays so the scratch file is never committed by accident.

## Known limits

- Writes made through Bash are not checked in `pre`. The Stop hook catches them.
- R5 finds functions by indentation and brace counting. Braces inside strings or comments can skew a length.
- The status line refreshes R1, R2 and R7 only when `HEAD` or the index changes; R3–R6 update at the next Stop.
- Every internal error fails open: exit 0, no decision, one line in `.git/my-harness/error.log`.

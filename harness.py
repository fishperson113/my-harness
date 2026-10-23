#!/usr/bin/env python3
"""my-harness: deterministic change-size rules for Claude Code, checked on git.

Commands: session, pre, stop, status (hooks and statusline) and check (manual report).
"""
import fnmatch
import json
import os
import re
import subprocess
import sys
import time

NAME = "my-harness"
OFF_ENV = "MY_HARNESS_OFF"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MAX_BYTES = 1 << 20
STATE_VERSION = 1
DEFAULTS = {
    "max_files": 10, "max_new_files": 3, "banned_name_patterns": [],
    "docs_extra_dirs": [], "decisions_file": "decisions.md",
    "max_function_lines": 50, "max_comment_ratio": 0.30,
    "min_added_lines_for_comment_rule": 10, "max_commits": 3,
    "base_branch": "", "max_retries": 1, "max_added_lines": 400,
    "generated_patterns": [],
}
BANNED = [r"[_-](new|old|copy|backup|tmp|final)(\.[^./]*)?$", r"\.(bak|orig|tmp)$", r"~$", r" copy"]
# api_v2.py is a copy only when api.py already exists next to it.
VERSIONED = re.compile(r"^(.+)[_-]v\d+(\.[^./]*)?$", re.I)
MD_EXTS = (".md", ".mdx", ".markdown")
# Lockfiles and other generated files are left out of R5, R6 and R9; .gitattributes can add more.
GENERATED = ["*.lock", "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "go.sum", "*.min.js", "*.min.css"]
GENERATED_ATTRS = {"-diff", "binary", "linguist-generated", "linguist-generated=true"}
# Root files every repo has: they do not make the root a docs dir. Creating them still goes through R4.
CONVENTIONAL_MD = re.compile(r"^(readme|changelog|contributing|license|code_of_conduct|security|claude|agents)"
                             r"\.(md|mdx|markdown)$", re.I)
KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "else", "new", "throw", "case", "do",
            "sizeof", "typeof", "delete", "await", "yield", "elif", "with"}
HACK = re.compile(r"(#|//|--|/\*|\*)\s*hack:")
FIX_HINT = "Fix these within the existing change set, or mark a deliberate shortcut with a `hack:` comment."

C_COMMENTS = ("//", "/*", "*")
JS_FUNC = (r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(?P<n1>\w+)"
           r"|^\s*(?:export\s+)?(?:const|let|var)\s+(?P<n2>\w+)\s*=\s*(?:async\s+)?(?:\([^)]*\)|\w+)\s*(?::[^=]*)?=>"
           r"|^\s*(?:(?:public|private|protected|static|async|get|set)\s+)*(?P<n3>\w+)\s*\([^)]*\)\s*(?::[^{]*)?\{")
# Language registry: (extensions, function-start regex, how the body ends, comment prefixes).
LANG_TABLE = [
    (".py", r"^\s*(?:async\s+)?def\s+(?P<n>\w+)", "indent", ("#",)),
    (".js .jsx .ts .tsx .mjs .cjs", JS_FUNC, "brace", C_COMMENTS),
    (".go", r"^func\s+(?:\([^)]*\)\s*)?(?P<n>\w+)", "brace", ("//",)),
    (".rs", r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(?P<n>\w+)", "brace", ("//",)),
    (".kt .kts", r"^\s*(?:[\w@]+\s+)*fun\s+(?:<[^>]*>\s*)?(?:[\w.]+\.)?(?P<n>\w+)", "brace", ("//",)),
    (".java .cs .c .h .cc .cpp .hpp",
     r"^\s*(?:[\w<>\[\],.*&:?]+\s+)+[*&]*(?P<n>[\w:~]+)\s*\([^;{]*\)?[^;]*$", "brace", C_COMMENTS),
    (".sh .bash .zsh", r"^\s*(?:function\s+(?P<n1>[\w-]+)|(?P<n2>[\w-]+)\s*\(\))", "brace", ("#",)),
    (".sql", None, None, ("--",)),
    (".yml .yaml .toml", None, None, ("#",)),
]
LANGS = {ext: (re.compile(rx) if rx else None, end, comments)
         for exts, rx, end, comments in LANG_TABLE for ext in exts.split()}


class Repo:
    def __init__(self, root, gitdir):
        self.root, self.gitdir = root, gitdir
        common = os.path.join(gitdir, "commondir")
        self.common = gitdir
        if os.path.isfile(common):
            with open(common, encoding="utf-8") as f:
                self.common = os.path.normpath(os.path.join(gitdir, f.read().strip()))


def find_repo(path):
    d = os.path.abspath(path)
    while True:
        dot = os.path.join(d, ".git")
        if os.path.isdir(dot):
            return Repo(d, dot)
        if os.path.isfile(dot):
            with open(dot, encoding="utf-8") as f:
                line = f.read().strip()
            if line.startswith("gitdir:"):
                return Repo(d, os.path.normpath(os.path.join(d, line[7:].strip())))
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def run_git(repo, *args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    return subprocess.run(["git", "-C", repo.root, "-c", "core.quotepath=off"] + list(args),
                          capture_output=True, timeout=5, env=env)


def git(repo, *args):
    p = run_git(repo, *args)
    if p.returncode:
        raise RuntimeError("git %s: %s" % (args[0], p.stderr.decode("utf-8", "replace").strip()))
    return p.stdout.decode("utf-8", "replace")


def parse_toml(text):
    try:
        import tomllib
        return tomllib.loads(text)
    except ImportError:
        out = {}
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                out[key.strip()] = json.loads(value.strip())
        return out


def load_config(repo):
    cfg = dict(DEFAULTS)
    for path in (os.path.expanduser("~/.config/%s/config.toml" % NAME), os.path.join(repo.common, NAME + ".toml")):
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            for key, value in parse_toml(f.read()).items():
                if key in DEFAULTS:
                    cfg[key] = value
                else:
                    sys.stderr.write("%s: unknown config key %r in %s\n" % (NAME, key, path))
    return cfg


def read_changes(repo, cfg, facts):
    """One git status call: (HEAD sha or None, changed paths, new paths)."""
    out = git(repo, "status", "--porcelain=v2", "-z", "--branch", "--untracked-files=all")
    head, changed, new = None, set(), set()
    parts, i = out.split("\0"), 0
    while i < len(parts):
        entry, i = parts[i], i + 1
        if entry.startswith("# branch.oid "):
            head = None if entry[13:] == "(initial)" else entry[13:]
        elif entry.startswith("? "):
            changed.add(entry[2:])
            new.add(entry[2:])
        elif entry[:2] in ("1 ", "2 ", "u "):
            fields = entry.split(" ", {"1": 8, "2": 9, "u": 10}[entry[0]])
            changed.add(fields[-1])
            if fields[1][0] == "A":
                new.add(fields[-1])
            i += entry[0] == "2"
    patterns = generated_patterns(cfg, facts)
    skip = {p for p in changed if p == cfg["decisions_file"] or is_generated(p, patterns)}
    return head, changed - skip, new - skip


def read_text(path):
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return None
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if b"\0" in data[:8000]:
        return None
    return data.decode("utf-8", "replace").splitlines()


def read_added(repo, head, new):
    """Map path -> {line number: text} for lines added since HEAD, new files in full."""
    added, path, line_no, in_hunk = {}, None, 0, False
    for line in git(repo, "diff", head or EMPTY_TREE, "-U0", "--no-color", "--no-ext-diff").split("\n"):
        if line.startswith("diff --git"):
            in_hunk = False
        elif not in_hunk and line.startswith("+++ "):
            name = line[4:].rstrip("\t").strip('"')
            path = None if name == "/dev/null" else name[2:]
        elif line.startswith("@@"):
            in_hunk, line_no = True, int(re.match(r"@@ -\S+ \+(\d+)", line).group(1))
        elif in_hunk and line.startswith("+") and path:
            added.setdefault(path, {})[line_no] = line[1:]
            line_no += 1
    for path in new - set(added):
        lines = read_text(os.path.join(repo.root, path))
        if lines:
            added[path] = dict(enumerate(lines, 1))
    return added


def attr_patterns(text):
    return [f[0] for f in (line.split() for line in text.splitlines())
            if f and not f[0].startswith("#") and GENERATED_ATTRS & set(f[1:])]


def committed_facts(repo):
    """Repo conventions as committed at HEAD (docs dirs, generated globs), cached per HEAD."""
    head = resolve_head(repo)
    cached = read_json(repo, "facts.json")
    if cached and cached.get("head") == head:
        return cached
    facts = {"head": head, "docs": [], "attrs": []}
    if head:
        files = git(repo, "ls-tree", "-r", "-z", "--name-only", "HEAD").split("\0")
        facts["docs"] = sorted({parent_dir(p) for p in files if p.lower().endswith(MD_EXTS)
                                and not ("/" not in p and CONVENTIONAL_MD.match(p))})
        if ".gitattributes" in files:
            facts["attrs"] = attr_patterns(git(repo, "show", "HEAD:.gitattributes"))
    write_json(repo, "facts.json", facts)
    return facts


def generated_patterns(cfg, facts):
    return GENERATED + list(cfg["generated_patterns"]) + facts["attrs"]


def is_generated(path, patterns):
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(path if "/" in p.strip("/") else name, p.lstrip("/")) for p in patterns)


def parent_dir(path):
    return path.rsplit("/", 1)[0] if "/" in path else "."


def docs_dirs(cfg, facts):
    return set(facts["docs"]) | {d.strip("/") or "." for d in cfg["docs_extra_dirs"]}


def read_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def ref_exists(repo, ref):
    if os.path.isfile(os.path.join(repo.common, ref)):
        return True
    packed = read_file(os.path.join(repo.common, "packed-refs")) or ""
    return any(line.endswith(" " + ref) for line in packed.splitlines())


def resolve_head(repo):
    head = read_file(os.path.join(repo.gitdir, "HEAD")) or ""
    if not head.startswith("ref: "):
        return head or None
    ref = head[5:]
    loose = read_file(os.path.join(repo.common, ref))
    if loose:
        return loose
    packed = read_file(os.path.join(repo.common, "packed-refs")) or ""
    return next((line.split()[0] for line in packed.splitlines() if line.endswith(" " + ref)), None)


def base_branch(repo, cfg):
    if cfg["base_branch"]:
        return cfg["base_branch"]
    origin = read_file(os.path.join(repo.common, "refs", "remotes", "origin", "HEAD")) or ""
    if origin.startswith("ref: "):
        return origin[5:]
    return next((r for r in ("refs/heads/main", "refs/heads/master") if ref_exists(repo, r)), None)


def commits_ahead(repo, cfg, head):
    base = base_branch(repo, cfg)
    if not base or not head:
        return None, None
    p = run_git(repo, "rev-list", "--count", base + "..HEAD")
    if p.returncode:
        return None, None
    return int(p.stdout), re.sub(r"^refs/(heads|remotes)/", "", base)


def index_mtime(repo):
    try:
        return os.stat(os.path.join(repo.gitdir, "index")).st_mtime_ns
    except OSError:
        return 0


class Ctx:
    """Git state the rules read. `focus` = new files under test; `verb` words R1/R2."""

    def __init__(self, repo, cfg, facts, head, changed, new, focus=None, verb="has"):
        self.repo, self.cfg, self.facts, self.head = repo, cfg, facts, head
        self.changed, self.new, self.verb = changed, new, verb
        self.focus = sorted(new if focus is None else focus)
        self._memo = {}

    def memo(self, key, fn):
        if key not in self._memo:
            self._memo[key] = fn()
        return self._memo[key]

    def added(self):
        return self.memo("added", self._read_added)

    def _read_added(self):
        patterns = generated_patterns(self.cfg, self.facts)
        added = read_added(self.repo, self.head, self.new)
        return {p: lines for p, lines in added.items() if not is_generated(p, patterns)}

    def docs(self):
        return docs_dirs(self.cfg, self.facts)

    def lines(self, path):
        return self.memo("f:" + path, lambda: read_text(os.path.join(self.repo.root, path)))

    def commits(self):
        return self.memo("commits", lambda: commits_ahead(self.repo, self.cfg, self.head))


def r1(ctx):
    n, limit = len(ctx.changed), ctx.cfg["max_files"]
    msg = "R1: change set %s %d files (limit %d). Finish within the files already changed, or ask the user to commit first."
    return n, limit, [], [msg % (ctx.verb, n, limit)] if n > limit else []


def r2(ctx):
    n, limit = len(ctx.new), ctx.cfg["max_new_files"]
    msg = "R2: change set %s %d new files (limit %d). Reuse existing files instead of creating new ones."
    return n, limit, [], [msg % (ctx.verb, n, limit)] if n > limit and ctx.focus else []


def copies_existing(root, path):
    m = VERSIONED.match(path)
    return bool(m) and os.path.isfile(os.path.join(root, m.group(1) + (m.group(2) or "")))


def r3(ctx):
    patterns = [re.compile(p, re.I) for p in BANNED + list(ctx.cfg["banned_name_patterns"])]
    bad = [p for p in ctx.focus if any(rx.search(p.rsplit("/", 1)[-1]) for rx in patterns)
           or copies_existing(ctx.repo.root, p)]
    msg = "R3: new file %s has a copy-style name. Edit the original file instead of creating a variant."
    return len(bad), 0, bad, [msg % p for p in bad]


def r4(ctx):
    md = [p for p in ctx.focus if p.lower().endswith(MD_EXTS) and p != ctx.cfg["decisions_file"]]
    dirs = ctx.docs() if md else set()
    bad = [p for p in md if parent_dir(p) not in dirs]
    shown = ", ".join(sorted(dirs)[:5]) or "none"
    msg = "R4: new markdown file %s is outside the docs dirs (%s). Answer in chat instead, or write into a docs dir."
    return len(bad), 0, bad, [msg % (p, shown) for p in bad]


def lang_of(path):
    return LANGS.get(os.path.splitext(path)[1].lower())


def indent_end(lines, i):
    """Signature ends at the first ':' outside brackets; code after it means a one-line function."""
    depth = 0
    for j in range(i, min(len(lines), i + 50)):
        code = lines[j].split("#")[0]
        for pos, ch in enumerate(code):
            depth += (ch in "([{") - (ch in ")]}")
            if ch == ":" and depth == 0:
                return j if code[pos + 1:].strip() else body_end(lines, i, j)
    return i


def body_end(lines, i, j):
    width, end = len(lines[i]) - len(lines[i].lstrip()), j
    for k in range(j + 1, len(lines)):
        if not lines[k].strip():
            continue
        if len(lines[k]) - len(lines[k].lstrip()) <= width:
            break
        end = k
    return end


def brace_end(lines, i):
    depth, opened = 0, False
    for k in range(i, len(lines)):
        for ch in lines[k]:
            if ch == ";" and not opened:
                return None
            depth += (ch == "{") - (ch == "}")
            opened = opened or ch == "{"
            if opened and depth <= 0:
                return k
        if not opened and k > i + 5:
            return None
    return None


def functions(lines, lang):
    """Yield (name, first index, last index) for every function the heuristic finds."""
    start_re, end_style = lang[0], lang[1]
    for i, line in enumerate(lines):
        m = start_re.match(line) if start_re else None
        if not m:
            continue
        name = next(v for v in m.groupdict().values() if v)
        if name in KEYWORDS or line.split()[0] in KEYWORDS:
            continue
        end = indent_end(lines, i) if end_style == "indent" else brace_end(lines, i)
        if end is not None:
            yield name, i, end


def scan_functions(ctx):
    """(violations, hack-exempted) long touched functions as (path, line, name, size)."""
    limit, found, exempt = ctx.cfg["max_function_lines"], [], []
    for path, added in sorted(ctx.added().items()):
        lang, lines = lang_of(path), ctx.lines(path)
        if not lang or not lines:
            continue
        for name, start, end in functions(lines, lang):
            size = end - start + 1
            if size <= limit or not any(n in added for n in range(start + 1, end + 2)):
                continue
            hacked = any(HACK.search(lines[j]) for j in range(max(0, start - 2), start + 1))
            (exempt if hacked else found).append((path, start + 1, name, size))
    return found, exempt


def scan_comments(ctx):
    """(violations, hack-exempted) files whose added lines are too comment-heavy, as (path, percent)."""
    limit, floor, found, exempt = ctx.cfg["max_comment_ratio"], ctx.cfg["min_added_lines_for_comment_rule"], [], []
    for path, added in sorted(ctx.added().items()):
        lang, lines = lang_of(path), ctx.lines(path)
        if not lang or not lines:
            continue
        body = [t.strip() for n, t in sorted(added.items()) if t.strip() and not (n == 1 and t.startswith("#!"))]
        comments = sum(1 for t in body if t.startswith(lang[2]))
        if len(body) >= floor and comments / len(body) > limit:
            hacked = any(HACK.search(line) for line in lines)
            (exempt if hacked else found).append((path, round(100 * comments / len(body))))
    return found, exempt


def r5(ctx):
    found, limit = ctx.memo("r5", lambda: scan_functions(ctx))[0], ctx.cfg["max_function_lines"]
    msg = "R5: function %s at %s:%d is %d lines (limit %d). Split it into smaller functions."
    return (max([f[3] for f in found] or [0]), limit, ["%s %dl" % (f[2], f[3]) for f in found],
            [msg % (name, path, line, size, limit) for path, line, name, size in found])


def r6(ctx):
    found, limit = ctx.memo("r6", lambda: scan_comments(ctx))[0], round(ctx.cfg["max_comment_ratio"] * 100)
    msg = "R6: %s adds %d%% comment lines (limit %d%%). Keep only comments whose reason is not obvious."
    return (max([f[1] for f in found] or [0]), limit, ["%s %d%%" % (p.rsplit("/", 1)[-1], r) for p, r in found],
            [msg % (p, r, limit) for p, r in found])


def r7(ctx):
    n, base = ctx.commits()
    limit = ctx.cfg["max_commits"]
    return n, limit, [], ["R7: branch is %d commits ahead of %s (limit %d)." % (n, base, limit)] if n and n > limit else []


def r8(ctx):
    functions_exempt = ctx.memo("r5", lambda: scan_functions(ctx))[1]
    files_exempt = ctx.memo("r6", lambda: scan_comments(ctx))[1]
    items = ["%s (R5)" % f[2] for f in functions_exempt] + ["%s (R6)" % f[0].rsplit("/", 1)[-1] for f in files_exempt]
    return len(items), None, items, []


def r9(ctx):
    n, limit = sum(len(lines) for lines in ctx.added().values()), ctx.cfg["max_added_lines"]
    msg = ("R9: change set adds %d lines (limit %d). Stop and tell the user the change is %d lines;"
           " do not delete, compress or revert work to get under the limit.")
    return n, limit, [], [msg % (n, limit, n)] if n > limit else []


# Specification registry: (id, blocks Claude, commands that run it, title, check).
RULES = [
    ("R1", True, "pre stop", "files in change set", r1),
    ("R2", True, "pre stop", "new files", r2),
    ("R3", True, "pre stop", "new files with banned names", r3),
    ("R4", True, "pre stop", "new markdown outside docs", r4),
    ("R5", True, "stop", "longest touched function", r5),
    ("R6", True, "stop", "comment % of added lines", r6),
    ("R7", False, "stop", "commits ahead of base", r7),
    ("R8", False, "stop", "hack: exemptions applied", r8),
    ("R9", False, "stop", "lines added (no lockfiles)", r9),
]


def evaluate(ctx, phase):
    return [(rid, blocks, title) + tuple(check(ctx)) for rid, blocks, phases, title, check in RULES if phase in phases.split()]


def blocking_reasons(results):
    return [r for rid, blocks, _, _, _, _, reasons in results if blocks for r in reasons]


def to_cache(results):
    return {rid: {"value": v, "limit": lim, "ok": not reasons, "items": items[:5]}
            for rid, _, _, v, lim, items, reasons in results}


def read_json(repo, name):
    try:
        with open(os.path.join(repo.gitdir, NAME, name), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and data.get("version") == STATE_VERSION else {}
    except (OSError, ValueError):
        return {}


def write_json(repo, name, data):
    d = os.path.join(repo.gitdir, NAME)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dict(data, version=STATE_VERSION), f, sort_keys=True)
    os.replace(tmp, os.path.join(d, name))


def load_state(repo):
    return read_json(repo, "state.json")


def save_state(repo, head, rules, retries):
    write_json(repo, "state.json", {"computed_at": int(time.time()), "head": head,
                                    "index_mtime": index_mtime(repo), "rules": rules, "retries": retries})


def out(text):
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.flush()


def ensure_excluded(repo, name):
    path = os.path.join(repo.common, "info", "exclude")
    text = read_file(path) or ""
    if "/" + name in text.splitlines():
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write((text + "\n" if text else "") + "/" + name + "\n")


def cmd_session(repo, cfg, data):
    ensure_excluded(repo, cfg["decisions_file"])
    dirs = sorted(docs_dirs(cfg, committed_facts(repo)))
    shown = ", ".join(dirs[:10]) + (" and %d more" % (len(dirs) - 10) if len(dirs) > 10 else "")
    out("\n".join([
        "%s is active: git-based checks limit the size and shape of your changes." % NAME,
        "- R1: at most %d files in the uncommitted change set." % cfg["max_files"],
        "- R2: at most %d new files." % cfg["max_new_files"],
        "- R3: no copy-style new files (_v2, _new, _old, _backup, .bak, ...). Edit the original.",
        "- R4: new markdown files only inside docs dirs: %s." % (shown or "none"),
        "- R5: every function you touch stays within %d lines." % cfg["max_function_lines"],
        "- R6: comment lines stay under %d%% of the lines you add to a file." % round(cfg["max_comment_ratio"] * 100),
        "A deliberate shortcut is allowed if visible: a `hack:` comment on or just above a function",
        "exempts it from R5; one anywhere in a file exempts that file from R6.",
        "`%s` at the repo root is the user's local scratch file: never committed, exempt." % cfg["decisions_file"],
        "If a write is denied, stay within the files already changed or tell the user.",
    ]) + "\n")


def relative(repo, cwd, raw):
    try:
        rel = os.path.relpath(os.path.realpath(os.path.join(cwd, raw)), os.path.realpath(repo.root))
    except ValueError:
        return None
    rel = rel.replace(os.sep, "/")
    if rel == "." or rel.split("/")[0] == "..":
        return None
    return rel


def deny(reasons):
    out(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "\n".join(reasons)}}))


def cmd_pre(repo, cfg, data):
    tool = data.get("tool_input") or {}
    raw = tool.get("file_path") or tool.get("notebook_path")
    rel = relative(repo, data.get("cwd") or repo.root, raw) if raw else None
    if rel is not None and rel.split("/")[0] == ".git":
        return deny(["%s is inside .git/. Harness config, excludes and git state belong to the user." % rel])
    facts = committed_facts(repo)
    if rel is None or rel == cfg["decisions_file"] or is_generated(rel, generated_patterns(cfg, facts)):
        return
    exists = os.path.exists(os.path.join(repo.root, rel))
    if exists and git(repo, "status", "--porcelain", "--ignored", "--", rel).strip():
        return  # fast path: already in the change set, or ignored
    if not exists and run_git(repo, "check-ignore", "-q", "--", rel).returncode == 0:
        return
    head, changed, new = read_changes(repo, cfg, facts)
    if rel in changed:
        return
    focus = set() if exists else {rel}
    ctx = Ctx(repo, cfg, facts, head, changed | {rel}, new | focus, focus, "would reach")
    reasons = blocking_reasons(evaluate(ctx, "pre"))
    if reasons:
        deny(reasons)


def full_ctx(repo, cfg):
    facts = committed_facts(repo)
    head, changed, new = read_changes(repo, cfg, facts)
    return Ctx(repo, cfg, facts, head, changed, new)


def cmd_stop(repo, cfg, data):
    ctx = full_ctx(repo, cfg)
    results = evaluate(ctx, "stop")
    reasons = blocking_reasons(results)
    retries, sid = load_state(repo).get("retries") or {}, str(data.get("session_id", ""))
    # Counts consecutive blocks: a new user turn starts at 0; a continuation we did not cause never blocks.
    count = retries.pop(sid, cfg["max_retries"]) if data.get("stop_hook_active") else 0
    retries.pop(sid, None)
    block = bool(reasons) and count < cfg["max_retries"]
    if reasons:
        retries[sid] = count + block
    save_state(repo, ctx.head, to_cache(results), dict(list(retries.items())[-20:]))
    if block:
        out(json.dumps({"decision": "block", "reason": "\n".join(reasons + [FIX_HINT])}))


def fraction(rule):
    value = rule.get("value")
    return "%s/%s" % ("-" if value is None else value, rule.get("limit"))


def violation_label(rid, rule):
    items = rule.get("items") or ["?"]
    labels = {"R1": "files ", "R2": "new ", "R9": "lines "}
    if rid in labels:
        return labels[rid] + fraction(rule)
    return "%s %s" % (rid, items[0].rsplit("/", 1)[-1])


def status_line(state):
    rules = state.get("rules") or {}
    bad = [rid for rid, blocks, _, _, _ in RULES if blocks and not rules.get(rid, {}).get("ok", True)]
    hacks = (rules.get("R8") or {}).get("value") or 0
    if bad:
        text, color = "%s ✗ %s" % (NAME, " · ".join(violation_label(r, rules[r]) for r in bad)), 1
    else:
        parts = ["files " + fraction(rules.get("R1", {})), "new " + fraction(rules.get("R2", {})),
                 "commits " + ("-" if (rules.get("R7") or {}).get("value") is None else fraction(rules["R7"]))]
        parts += ["lines " + fraction(rules["R9"])] if "R9" in rules else []
        parts += ["hack %d" % hacks] if hacks else []
        big = not (rules.get("R9") or {}).get("ok", True)
        text, color = "%s %s · ✓" % (NAME, " · ".join(parts)), 3 if hacks or big else 2
    if len(text) > 80:
        text = text[:79] + "…"
    return "\033[3%dm%s\033[0m" % (color, text)


def cmd_status(repo, cfg, data):
    state = load_state(repo)
    if not state or state.get("head") != resolve_head(repo) or state.get("index_mtime") != index_mtime(repo):
        ctx = full_ctx(repo, cfg)
        save_state(repo, ctx.head, to_cache(evaluate(ctx, "stop")), state.get("retries") or {})
        state = load_state(repo)
    out(status_line(state))


def cmd_check(repo, cfg, data):
    results = evaluate(full_ctx(repo, cfg), "stop")
    for rid, blocks, title, value, limit, items, reasons in results:
        shown = "-" if value is None else value
        verdict = "ok" if not reasons else ("FAIL" if blocks else "warn")
        out("%s  %-30s %6s / %-5s %s\n" % (rid, title, shown, "-" if limit is None else limit, verdict))
    for rid, blocks, title, value, limit, items, reasons in results:
        for reason in reasons:
            out(reason + "\n")


COMMANDS = {"session": cmd_session, "pre": cmd_pre, "stop": cmd_stop, "status": cmd_status, "check": cmd_check}


def log_error(command, cwd, exc):
    try:
        repo = find_repo(cwd)
        d = os.path.join(repo.gitdir, NAME)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "error.log"), "a", encoding="utf-8") as f:
            f.write("%s %s %s: %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), command, type(exc).__name__, exc))
    except Exception:
        pass


def main(argv):
    if os.environ.get(OFF_ENV) == "1" or len(argv) < 2 or argv[1] not in COMMANDS:
        return 0
    cwd = os.getcwd()
    try:
        data = {} if argv[1] == "check" else json.loads(sys.stdin.read() or "{}")
        cwd = data.get("cwd") or (data.get("workspace") or {}).get("current_dir") or cwd
        repo = find_repo(cwd)
        if repo:
            COMMANDS[argv[1]](repo, load_config(repo), data)
    except Exception as exc:  # fail-open: a broken harness must never block Claude
        log_error(argv[1], cwd, exc)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

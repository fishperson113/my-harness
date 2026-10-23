"""Latency check for N1-N4: builds a 10,000-file repo and prints median ms per command.

Run manually: python3 tests/bench.py
"""
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

HARNESS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "harness.py")


def build_repo(root, files=10000, changed=10):
    subprocess.run(["git", "init", "-q", root], check=True)
    for i in range(files):
        d = os.path.join(root, "pkg%03d" % (i // 100))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "m%05d.py" % i), "w") as f:
            f.write("def f():\n    return %d\n" % i)
    git = ["git", "-C", root, "-c", "user.name=b", "-c", "user.email=b@example.com"]
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "seed"], check=True)
    for i in range(changed):
        with open(os.path.join(root, "pkg000", "m%05d.py" % i), "a") as f:
            f.write("\n\ndef g():\n    return 1\n")


def median_ms(root, command, payload, runs=20):
    data = json.dumps(dict({"cwd": root, "session_id": "bench"}, **payload)).encode()
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        subprocess.run([sys.executable, HARNESS, command], input=data, capture_output=True, cwd=root)
        times.append((time.perf_counter() - start) * 1000)
    return statistics.median(times)


def main():
    root = tempfile.mkdtemp()
    try:
        build_repo(root)
        edit = lambda rel: {"tool_input": {"file_path": os.path.join(root, rel)}}
        cases = [
            ("N1 pre fast path (<=60ms)", "pre", edit("pkg000/m00000.py")),
            ("N2 pre full path (<=150ms)", "pre", edit("pkg050/m05000.py")),
            ("N3 stop (<=500ms)", "stop", {}),
            ("N4 status, fresh cache (<=50ms)", "status", {}),
        ]
        for label, command, payload in cases:
            print("%-34s %7.1f ms" % (label, median_ms(root, command, payload)))
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()

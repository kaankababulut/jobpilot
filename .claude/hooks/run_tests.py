"""PostToolUse hook: after Claude edits a .py file, run the test suite.

If tests fail, exit code 2 sends the failure output back to Claude, so it has to
fix the problem before it can say the task is done.
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    event = json.load(sys.stdin)
except ValueError:
    sys.exit(0)

path = (event.get("tool_input") or {}).get("file_path", "")
if not path.endswith(".py"):
    sys.exit(0)

result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "--no-header", "-p", "no:cacheprovider"],
                        cwd=ROOT, capture_output=True, text=True)
if result.returncode not in (0, 5):  # 5 = no tests collected
    tail = "\n".join((result.stdout + result.stderr).splitlines()[-40:])
    print(f"Tests failed after editing {os.path.basename(path)}. Fix them before continuing:\n{tail}",
          file=sys.stderr)
    sys.exit(2)

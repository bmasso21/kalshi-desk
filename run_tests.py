"""Run the whole Kalshi Desk test suite:  python run_tests.py   (exit code 0 = all passed)"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SUITES = ["tests/test_e2e.py", "tests/test_ws.py"]

failed = []
for s in SUITES:
    print(f"\n######## {s}")
    r = subprocess.run([sys.executable, str(ROOT / s)], cwd=ROOT)
    if r.returncode != 0:
        failed.append(s)
print("\nALL SUITES PASSED" if not failed else f"\nFAILED SUITES: {failed}")
sys.exit(1 if failed else 0)

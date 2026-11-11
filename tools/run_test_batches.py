"""Bound Tk/GDI resources by running the complete suite in small processes.

Windows GUI suites create hundreds of independent Tk interpreters. Separate
process batches release their native resources even when libraries cache fonts
or images. Use through run_ui_tests.py to isolate all GUI windows as well.
"""
import argparse
from datetime import datetime
import os
from pathlib import Path
import re
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--batches", help="Comma-separated batch numbers to recheck; omitted runs every module")
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("batch-size must be positive")
    try:
        selected = {int(value) for value in args.batches.split(",")} if args.batches else None
        if selected is not None and (not selected or min(selected) < 1):
            raise ValueError
    except ValueError:
        parser.error("batches must be positive integers separated by commas")
    root = Path(__file__).resolve().parents[1]
    modules = [f"tests.{path.stem}" for path in sorted((root / "tests").glob("test_*.py"))]
    folder = root / ".test-tmp" / ("suite-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    folder.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    total, modules_run, failures = 0, 0, []
    for start in range(0, len(modules), args.batch_size):
        batch = modules[start:start + args.batch_size]
        number = start // args.batch_size + 1
        if selected is not None and number not in selected:
            continue
        modules_run += len(batch)
        print(f"Batch {number}: {batch[0]} through {batch[-1]}", flush=True)
        result = subprocess.run([sys.executable, "-m", "unittest", *batch, "-q"], cwd=root,
                                env=environment, capture_output=True)
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        (folder / f"batch-{number}.log").write_text(output, encoding="utf-8")
        counts = re.findall(r"Ran (\d+) tests? in ([\d.]+)s", output)
        total += sum(int(count) for count, _ in counts)
        if result.returncode:
            failures.extend(batch)
            print(output, flush=True)
            print(f"Batch {number} FAILED (exit {result.returncode})", flush=True)
        else:
            print(f"Batch {number} passed: " + ", ".join(f"{count} tests in {seconds}s" for count, seconds in counts), flush=True)
    print(f"{'Complete suite' if selected is None else 'Selected batches'}: {total} tests across {modules_run} modules; "
          f"{'all batches passed' if not failures else 'failed modules: ' + ', '.join(failures)}. Logs: {folder}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

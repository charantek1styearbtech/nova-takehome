"""Your harness. The grader calls exactly these three commands, in a fresh process each time:

    python harness.py ingest <batch_dir> --state-dir <dir>
    python harness.py report --as-of YYYY-MM-DD [--mode supervised|autonomous] [--state-dir <dir>] > report.json
    python harness.py feedback <feedback.json> --state-dir <dir>

- ingest: read every .eml in <batch_dir> (attachments inside) and store what you learn under --state-dir. The process
  is restarted between batches, so everything you need later must be on disk.
- report: print one report.json (grader/schema/report.schema.json) to stdout. Only documents received on or before
  --as-of count. --mode defaults to supervised.
- feedback: take in a person's decisions on what you escalated (feedback.json) and let them change later reports.

The environment gives ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL. Keep the argument names as they are: grader/validate.py
checks them.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from harness import state, understand
    from harness import analyze as A
    from harness import feedback as F
else:
    from . import state, understand
    from . import analyze as A
    from . import feedback as F

DEFAULT_STATE = ".state"


def ingest(batch_dir: str, state_dir: str) -> None:
    from pathlib import Path as _P
    batch_name = _P(batch_dir).name
    if batch_name in state.seen_batches(state_dir):
        return
    understand.ingest_batch(batch_dir, state_dir)
    state.mark_batch_seen(state_dir, batch_name)


def report(as_of: str, mode: str, state_dir: str) -> dict:
    docs = state.load_all_docs(state_dir, as_of=as_of)
    facts = state.load_facts(state_dir)
    closed = state.load_closed_keys(state_dir)
    learned = state.load_learned(state_dir)
    return A.build_report(docs, as_of=as_of, mode=mode, facts=facts,
                          closed_keys=closed, learned=learned)


def feedback(feedback_file: str, state_dir: str) -> None:
    import json as _json
    data = _json.loads(Path(feedback_file).read_text(encoding="utf-8"))
    F.apply_feedback(data, state_dir)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="harness.py", description="Freight invoice audit harness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="read one batch folder into the state dir")
    p.add_argument("batch_dir")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    p = sub.add_parser("report", help="print report.json for a date")
    p.add_argument("--as-of", required=True, help="YYYY-MM-DD; only documents received by then count")
    p.add_argument("--mode", choices=["supervised", "autonomous"], default="supervised")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    p = sub.add_parser("feedback", help="take in a person's decisions (feedback.json)")
    p.add_argument("feedback_file")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "ingest":
            ingest(a.batch_dir, a.state_dir)
        elif a.cmd == "report":
            import json
            json.dump(report(a.as_of, a.mode, a.state_dir), sys.stdout, indent=1)
            sys.stdout.write("\n")
        else:
            feedback(a.feedback_file, a.state_dir)
    except NotImplementedError as e:
        print(f"harness.py {a.cmd}: {e}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"harness.py {a.cmd}: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())


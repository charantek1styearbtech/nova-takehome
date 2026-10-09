"""Run every worked example: ingest -> report -> diff key fields against expected_finding.json.

Usage: .venv\\Scripts\\python.exe test_all_examples.py [name ...]
"""
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import understand, state, analyze

AS_OF = {
    "overcharge": "2032-08-31",
    "cashback": "2033-11-30",
    "expiry": "2034-02-28",
    "conflict": "2032-11-30",
    "duplicate": "2033-08-31",
    "duplicate_rebilled_4_months": "2032-08-31",
    "waived_but_billed": "2034-02-28",
    "late_fees": "2033-02-28",
}


def key_of(it: dict) -> str:
    return f"{it.get('class')}:{it.get('invoice_id') or it.get('contract_id') or it.get('id')}"


def summarize(r: dict) -> dict:
    out = {}
    for it in r.get("findings", []):
        out[key_of(it)] = (it.get("verdict"), it.get("amount_usd") or it.get("amount_range"),
                           it.get("action"))
    for sec in ("cashback", "expiry", "late_fee_risk"):
        for it in r.get("portfolio", {}).get(sec, []):
            cid = it.get("contract_id") or it.get("invoice_id") or it.get("id")
            out[f"{sec}:{cid}"] = (it.get("verdict"), it.get("amount_usd") or it.get("deadline"),
                                   it.get("action"))
    return out


def run(name: str) -> bool:
    root = Path("data/examples") / name
    if not (root / "docs").is_dir():
        print(f"{name}: no docs/ dir, skipped")
        return True
    st = f".state_{name}"
    shutil.rmtree(st, ignore_errors=True)
    t0 = time.time()
    batches = sorted((root / "docs").glob("batch_*"))
    n = 0
    for b in batches:
        n += understand.ingest_batch(str(b), st)
    dt = time.time() - t0

    docs = state.load_all_docs(st)
    report = analyze.build_report(docs, AS_OF[name], "supervised", facts=[])
    Path(f"out_{name}.json").write_text(json.dumps(report, indent=1, ensure_ascii=False),
                                        encoding="utf-8")

    exp = json.loads((root / "expected_finding.json").read_text(encoding="utf-8"))
    got, want = summarize(report), summarize(exp)

    ok = True
    for k in sorted(set(got) | set(want)):
        g, w = got.get(k), want.get(k)
        if g != w:
            # evidence-only difference: same verdict+amount+action but expected has entry we lack
            if g is None:
                # we omitted an entry the example lists; check if it's a no_finding (allowed to omit)
                if w[0] == "no_finding":
                    continue
                ok = False
                print(f"  MISSING {k}: expected {w}")
            elif w is None and g[0] == "no_finding":
                continue
            else:
                ok = False
                print(f"  DIFF {k}:\n    got {g}\n    exp {w}")
    status = "PASS" if ok else "FAIL"
    print(f"{name}: {status}  ({n} docs in {dt:.1f}s, {len(docs)} total docs)")
    return ok


def main():
    names = sys.argv[1:] or list(AS_OF)
    results = [run(n) for n in names]
    print(f"\n{sum(results)}/{len(results)} examples pass")


if __name__ == "__main__":
    main()

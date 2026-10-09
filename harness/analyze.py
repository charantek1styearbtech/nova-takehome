"""Layer 3: universal docs -> report.json.

Deterministic arithmetic on top of what understand.py extracted. One item per finding key.
Verdicts: finding | no_finding | conflict | cannot_determine.
Only 'finding' counts in totals. ~25% of every set is clean — leave those no_finding or out.

Rounding: half up to cents, ties away from zero (Decimal, ROUND_HALF_UP). Nothing else rounded.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP


# ---- money helpers ----------------------------------------------------------------------------------------------------
def _d(x) -> Decimal:
    return Decimal(str(x))


def r2(x) -> Decimal:
    """Round half up to cents, ties away from zero."""
    return _d(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def usd(x) -> str:
    return f"{r2(x):.2f}"


def _amount_usd(inv: dict, contract: dict, value) -> str | None:
    """USD string for an amount in the invoice's currency (Annex F, month of invoice date).
    Returns None when the contract holds no Annex F row for that month (caller must not invent)."""
    if (inv.get("currency") or "USD") != "INR":
        return usd(value)
    fx = contract.get("fx_inr_per_usd_by_month") or {}
    rate = fx.get((inv.get("invoice_date") or "")[:7])
    if not rate:
        return None
    return usd(_d(value) / _d(rate))


def _line_qty(l: dict) -> Decimal:
    """Parsed quantity, falling back to a '263 kg' / '4 days' hint inside the description."""
    q = _d(l.get("qty") or 0)
    if q > 0:
        return q
    desc = str(l.get("desc") or "")
    m = (re.search(r"(\d+(?:\.\d+)?)\s*kg\b", desc, re.I)
         or re.search(r"(\d+(?:\.\d+)?)\s*days?\b", desc, re.I))
    return _d(m.group(1)) if m else Decimal("0")


def _date(s) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


# ---- item builders ----------------------------------------------------------------------------------------------------
def item(id_: str, cls: str, verdict: str, action: str, confidence: float,
         evidence: list[dict], *, invoice_id=None, contract_id=None, period=None,
         credit_type=None, amount=None, amount_range=None, deadline=None,
         missing_doc=None, variant=None, supersedes=None, retro=None, learned_at=None) -> dict:
    it = {
        "id": id_, "class": cls, "verdict": verdict, "action": action,
        "confidence": round(float(confidence), 3),
        "evidence": evidence,
        "amount_usd": amount, "amount_range": amount_range, "deadline": deadline,
        "missing_doc": missing_doc, "supersedes": supersedes or [], "learned_at": learned_at,
    }
    if invoice_id:
        it["invoice_id"] = invoice_id
    if contract_id:
        it["contract_id"] = contract_id
    if period:
        it["period"] = period
    if credit_type:
        it["credit_type"] = credit_type
    if variant:
        it["variant"] = variant
    # drop nulls except the ones the schema wants present
    return it


def ev(doc: str, page: int, quote: str) -> dict:
    return {"doc": doc, "page": page, "quote": quote}


# ---- index the universe ------------------------------------------------------------------------------------------------
class Universe:
    """All documents received by as_of, indexed for the checks."""

    def __init__(self, docs: list[dict]):
        self.docs = docs
        self.invoices: list[dict] = []
        self.credits: list[dict] = []
        self.contracts: list[dict] = []
        self.tracking: list[dict] = []
        self.registers: list[dict] = []
        self.emails: list[dict] = []
        self.declarations: list[dict] = []
        for d in docs:
            t = d.get("doc_type")
            if t == "invoice":
                self.invoices.append(d)
            elif t == "credit_note":
                self.credits.append(d)
            elif t in ("contract", "amendment"):
                self.contracts.append(d)
            elif t == "tracking_export":
                self.tracking.append(d)
            elif t == "ap_register":
                self.registers.append(d)
            elif t == "email":
                self.emails.append(d)
            elif t == "declaration":
                self.declarations.append(d)
        # one finding key per identity: the same invoice/contract delivered twice counts once
        self.invoices = _dedupe(self.invoices, lambda d: d.get("invoice_id") or d.get("doc_id"))
        self.credits = _dedupe(self.credits, lambda d: d.get("invoice_id") or d.get("doc_id"))
        self.contracts = _dedupe(self.contracts, lambda d: d.get("contract_id") or d.get("doc_id"))

    def pickup_date(self, awb: str, fallback: str | None) -> str | None:
        """tracking PICKED_UP > carrier picked-up email > AWB date > invoice ship date."""
        for t in self.tracking:
            for e in t.get("events", []):
                if e.get("awb") == awb and e.get("status") == "PICKED_UP" and e.get("date"):
                    return e["date"]
        for em in self.emails:
            body = (em.get("body") or "")
            if awb and awb in body and "picked up" in body.lower():
                import re
                m = re.search(r"picked up[^.]*?(\d{4}-\d{2}-\d{2})", body, re.I)
                if m:
                    return m.group(1)
        return fallback

    def register_row(self, invoice_id: str) -> dict | None:
        for reg in self.registers:
            for row in reg.get("rows", []):
                if row.get("invoice_id") == invoice_id:
                    return row
        return None

    def credit_notes_for(self, contract_id=None, period=None, credit_type=None) -> list[dict]:
        out = []
        for c in self.credits:
            if contract_id and c.get("contract_id") and c.get("contract_id") != contract_id:
                continue
            out.append(c)
        return out


def _dedupe(docs: list[dict], key) -> list[dict]:
    """Keep one document per identity: earliest arrival wins (same file delivered twice)."""
    best: dict = {}
    for d in docs:
        k = key(d)
        cur = best.get(k)
        if cur is None or (d.get("received_date") or "") < (cur.get("received_date") or ""):
            best[k] = d
    return list(best.values())


def page_ev(doc: dict, quote: str, default_page: int = 1) -> dict:
    """Cite quote on the page it actually appears (whitespace-insensitive lookup)."""
    did = doc.get("doc_id", "")
    if not quote:
        return ev(did, default_page, "")
    for i, p in enumerate(doc.get("raw_pages") or [], 1):
        if quote in p:
            return ev(did, i, quote)
    fq = " ".join(quote.split())
    if fq:
        for i, p in enumerate(doc.get("raw_pages") or [], 1):
            if fq in " ".join(p.split()):
                return ev(did, i, quote)
    return ev(did, default_page, quote)


def _line_ev(inv: dict, quote: str) -> dict:
    return page_ev(inv, quote)


def _row_ev(u: Universe, row: dict) -> dict:
    for reg in u.registers:
        if reg.get("doc_id") == row.get("_doc"):
            return page_ev(reg, row.get("raw") or "")
    return ev(row.get("_doc") or "", 1, row.get("raw") or "")


# ---- report -----------------------------------------------------------------------------------------------------------
def build_report(docs: list[dict], as_of: str, mode: str, facts: list[dict],
                 closed_keys: dict | None = None, learned: dict | None = None) -> dict:
    closed_keys = closed_keys or {}
    learned = learned or {}
    u = Universe(docs)

    findings: list[dict] = []
    cashback: list[dict] = []
    expiry: list[dict] = []
    late_risk: list[dict] = []

    for inv in u.invoices:
        findings.extend(check_invoice(inv, u, mode, facts, closed_keys, learned))
    for c in u.contracts:
        expiry.extend(check_expiry(c, as_of, learned))
        cashback.extend(check_cashback(c, u, as_of, mode, facts, closed_keys, learned))
    late_risk.extend(check_late_fee_risk(u, as_of, mode, findings))

    report = {
        "schema_version": "1.0",
        "as_of": as_of,
        "mode": mode,
        "findings": findings,
        "portfolio": {"cashback": cashback, "expiry": expiry, "late_fee_risk": late_risk},
    }
    return report


# ---- per-invoice checks ------------------------------------------------------------------------------------------------
def check_invoice(inv: dict, u: Universe, mode: str, facts: list[dict],
                  closed: dict, learned: dict) -> list[dict]:
    """All four invoice classes for this invoice. Returns items."""
    out: list[dict] = []
    iid = inv.get("invoice_id") or ""
    if not iid:
        return out
    base_key = f"{iid}"

    if _closed(closed, iid, "overcharge"):
        pass  # still emit? no: closed keys stop being raised
    else:
        it = check_overcharge(inv, u, mode, facts, learned)
        if it:
            out.append(it)

    if not _closed(closed, iid, "duplicate"):
        it = check_duplicate(inv, u, mode, learned)
        if it:
            out.append(it)

    if not _closed(closed, iid, "waived_but_billed"):
        it = check_waived(inv, u, mode, facts, learned)
        if it:
            out.append(it)

    if not _closed(closed, iid, "late_fee_not_owed"):
        it = check_late_fee_not_owed(inv, u, mode, facts, learned)
        if it:
            out.append(it)
    return out


def _closed(closed: dict, invoice_id: str, cls: str) -> bool:
    return (f"{cls}:{invoice_id}" in closed or f"{invoice_id}:{cls}" in closed
            or invoice_id in closed)


# ---- class 1: overcharge ----------------------------------------------------------------------------------------------
FREIGHT_OK = re.compile(r"freight", re.I)
FUEL_OK = re.compile(r"\bfuel\b|fsc\b", re.I)


def reprice_lines(inv: dict, contract: dict | None, u: Universe,
                  override: tuple | None = None) -> list[dict]:
    """Reprice every line we can verify: freight (contract rate x weight), fuel (Annex C % of
    correct freight), security and other accessorials (Annex D per shipment/per chargeable kg).
    Lines we can't price are skipped (never invent findings).

    override = (lane, rate, min_charge, evidence) forces that lane's freight to a notice rate.
    -> [{"line", "billed", "correct", "evidence": [ev, ...]}]
    """
    out: list[dict] = []
    if contract is None:
        return out
    lines = inv.get("lines") or []
    correct_freight: dict[str, Decimal] = {}

    # pass 1: freight lines (fuel needs the correct base)
    for l in lines:
        desc = str(l.get("desc") or "")
        if not (FREIGHT_OK.search(desc) and not FUEL_OK.search(desc)):
            continue
        awb = l.get("awb") or ""
        pickup = u.pickup_date(awb, l.get("date"))
        min_charge = _min_charge(contract, l)
        rev = None
        if override and l.get("lane") == override[0] and override[1] is not None:
            rate = _d(override[1])
            if override[2] is not None:
                min_charge = _d(override[2])
            rev = override[3]
        else:
            rate, rev = _correct_rate(contract, l, u, pickup)
        if rate is None:
            continue
        qty = _line_qty(l)
        if qty <= 0:
            continue  # no weight: can't verify a per-kg rate or its minimum
        correct = r2(rate * qty)
        if min_charge is not None and min_charge > correct:
            correct = min_charge
        correct_freight[awb] = correct_freight.get(awb, Decimal("0")) + correct
        evid = [_line_ev(inv, l.get("raw", ""))]
        if rev:
            evid.append(rev)
        pd_quote = _clause_ev(contract, "pricing_date")
        if pd_quote:
            evid.append(pd_quote)
        pickup_ev = _pickup_ev(u, awb, pickup)
        if pickup_ev:
            evid.append(pickup_ev)
        out.append({"line": l, "billed": _d(l.get("amount") or 0),
                    "correct": correct, "evidence": evid})

    # pass 2: fuel (percentage of the CORRECT base freight, Annex C month of pickup)
    for l in lines:
        desc = str(l.get("desc") or "")
        if not FUEL_OK.search(desc):
            continue
        awb = l.get("awb") or ""
        if awb not in correct_freight:
            continue
        pickup = u.pickup_date(awb, l.get("date"))
        pct = _fuel_pct(contract, pickup)
        if pct is None:
            continue
        correct = r2(correct_freight[awb] * pct / Decimal("100"))
        evid = [_line_ev(inv, l.get("raw", ""))]
        fuel_quote = _clause_ev(contract, "fuel")
        if fuel_quote:
            evid.append(fuel_quote)
        out.append({"line": l, "billed": _d(l.get("amount") or 0),
                    "correct": correct, "evidence": evid})

    # pass 3: security + other accessorials from Annex D (per shipment / per chargeable kg)
    for l in lines:
        desc = str(l.get("desc") or "")
        if not desc.strip() or FUEL_OK.search(desc) or FREIGHT_OK.search(desc):
            continue
        # dangerous goods fee: not owed when the DGD shows dry ice as refrigerant only
        if re.search(r"dangerous goods|\bDG\b|\bdg\b| hazmat", desc, re.I):
            dgd = _dry_ice_only(u, l.get("awb") or "")
            if dgd is not None:
                evid = [_line_ev(inv, l.get("raw", ""))]
                q = _dgd_quote(dgd, l.get("awb") or "")
                if q:
                    evid.append(page_ev(dgd, q))
                clause = _clause_ev(contract, "dry_ice") or _clause_ev(contract, "dangerous_goods")
                if clause:
                    evid.append(clause)
                out.append({"line": l, "billed": _d(l.get("amount") or 0),
                            "correct": Decimal("0"), "evidence": evid})
                continue
        acc = _accessorial(contract, desc)
        if acc is None:
            continue
        evid = [_line_ev(inv, l.get("raw", ""))]
        if acc.get("quote"):
            evid.append(ev(contract["doc_id"], int(acc.get("page", 1)), acc["quote"]))
        # seasonal surcharge (e.g. peak season): not owed outside its months
        season = acc.get("season")
        if season:
            pu = u.pickup_date(l.get("awb") or "", l.get("date"))
            if not pu:
                continue  # can't place the pickup in time: never invent a finding
            mo = int(pu[5:7]) if len(pu) >= 7 else 0
            start, end = season
            in_season = (start <= mo <= end) if start <= end else (mo >= start or mo <= end)
            if not in_season:
                out.append({"line": l, "billed": _d(l.get("amount") or 0),
                            "correct": Decimal("0"), "evidence": evid})
                continue
        qty = _line_qty(l)
        basis = str(acc.get("basis", "")).lower()
        if "kg" in basis or "day" in basis:
            if qty <= 0:
                continue  # per-unit fee without a quantity: unverifiable, never invent
            correct = r2(_d(acc["fee"]) * qty)  # per chargeable kg / per storage day
        else:
            correct = r2(acc["fee"])  # per shipment etc.
        out.append({"line": l, "billed": _d(l.get("amount") or 0),
                    "correct": correct, "evidence": evid})
    return out


def _dry_ice_only(u: Universe, awb: str) -> dict | None:
    """A held DGD for this AWB that shows dry ice as refrigerant only (no dangerous goods)."""
    for d in u.declarations:
        text = "\n".join(d.get("raw_pages") or [])
        if awb and awb not in text:
            continue
        if re.search(r"refrigerant only", text, re.I):
            return d
        if re.search(r"UN1845", text) and not re.search(r"UN(?!1845)\d{4}", text):
            return d
    return None


def _dgd_quote(d: dict, awb: str) -> str:
    for page in d.get("raw_pages") or []:
        for line in page.split("\n"):
            if re.search(r"refrigerant only|UN1845|dry ice", line, re.I):
                s = line.strip()
                if awb and awb not in s:
                    continue
                if len(s) >= 12:
                    return s
    for page in d.get("raw_pages") or []:
        for line in page.split("\n"):
            if re.search(r"refrigerant only|UN1845|dry ice", line, re.I) and len(line.strip()) >= 12:
                return line.strip()
    return ""


def _min_charge(contract: dict, line: dict) -> Decimal | None:
    """Minimum charge for this lane: a signed amendment's table outranks the MSA's."""
    lane = line.get("lane") or ""
    origin, _, dest = lane.partition("-")
    for am in reversed(contract.get("amendments") or []):
        if not any(cl.get("origin") == origin and cl.get("destination") == dest
                   for cl in am.get("lanes") or []):
            continue
        mc = am.get("min_charge_by_lane") or {}
        if lane in mc:
            return _d(mc[lane])
        break
    mc = contract.get("min_charge_by_lane") or {}
    return _d(mc[lane]) if lane in mc else None


def _accessorial(contract: dict, desc: str) -> dict | None:
    acc = contract.get("accessorial_by_name") or {}
    aliases = contract.get("accessorial_alias") or {}
    d = desc.strip().lower()
    if not d:
        return None
    if d in acc:
        return acc[d]
    for name, v in acc.items():
        if not name:
            continue
        if name in d or d in name:
            return v
    # Annex E: any billed label for an Annex D code prices like the Annex D name
    for a, canon in aliases.items():
        if a and canon in acc and (a in d or d in a):
            return acc[canon]
    return None


def _fuel_pct(contract: dict, pickup: str | None) -> Decimal | None:
    months = contract.get("fuel_percent_by_month") or {}
    if pickup and pickup[:7] in months:
        return _d(months[pickup[:7]])
    return None


def _clause_ev(contract: dict, key: str) -> dict | None:
    q = (contract.get("quotes") or {}).get(key)
    if q and q.get("quote"):
        return ev(contract["doc_id"], int(q.get("page", 1)), str(q["quote"]))
    return None


def _pickup_ev(u: Universe, awb: str, pickup: str | None) -> dict | None:
    """The tracking row that establishes the pickup date (verbatim from the export)."""
    if not pickup:
        return None
    for t in u.tracking:
        for e in t.get("events", []):
            if e.get("awb") == awb and e.get("status") == "PICKED_UP" and e.get("date") == pickup:
                raw = e.get("raw") or ""
                if raw:
                    return ev(t.get("doc_id", ""), 1, raw)
    return None


def check_overcharge(inv: dict, u: Universe, mode: str, facts: list[dict],
                     learned: dict) -> dict | None:
    """Reprice every verifiable line against the contract and sum billed - correct."""
    iid = inv["invoice_id"]
    lines = [l for l in inv.get("lines", []) if l.get("awb")]
    if not lines:
        return None
    if not u.contracts:
        return None  # nothing held: never price from thin air

    # a held document points at an annex revision that never arrived -> ask for it
    miss = _missing_named_annex(u, lines)
    if miss:
        return item(
            f"overcharge:{iid}", "overcharge", "cannot_determine",
            _outbound_action("draft:request_document", "execute:request_document",
                             Decimal("0"), mode),
            0.6, [ev(miss["doc"], miss["page"], miss["quote"])],
            invoice_id=iid, missing_doc=miss["missing"],
        )

    contract = _governing_contract(u, lines)
    if contract is None:
        return None
    contract["_inv_doc"] = inv.get("doc_id") or iid_doc_of(inv)
    contract["_facts"] = facts

    lane = lines[0].get("lane") or ""
    pickup = u.pickup_date(lines[0].get("awb", ""), lines[0].get("date"))

    # rate sources in force: a Schedule-1 rate notice (or feedback-accepted one) and a signed
    # amendment can both apply to this pickup; a feedback precedence fact picks a side.
    pf = _precedence_fact(facts, contract, lane)
    notice_e, n = _valid_notice(u, contract, lane, pickup, facts, pf)
    if pf is not None and "notice" not in str(pf.get("use") or "").lower():
        notice_e, n = None, None  # feedback resolved the conflict against the notice
    notice_rate = _notice_band_rate(n, lines[0]) if n else None
    amend_rate, amend_ev, am_doc = _amendment_rate(contract, lines[0], u, pickup)

    override = None
    if notice_e is not None and notice_rate is not None:
        override = (lane, notice_rate, (n or {}).get("min_charge"),
                    _notice_ev(notice_e, lane, notice_rate))

    priced = reprice_lines(inv, contract, u, override)
    if not priced:
        return None  # nothing verifiable: don't invent a finding

    evidence: list[dict] = []
    billed = Decimal("0")
    for p in priced:
        billed += p["billed"]
        for e in p["evidence"]:
            if e not in evidence and e.get("quote"):
                evidence.append(e)

    # conflict: signed amendment + authorized notice both in force on this pickup, disagreeing,
    # and the amount at stake differs between them
    if (notice_e is not None and notice_rate is not None
            and amend_rate is not None and notice_rate != amend_rate):
        t_n = sum((p["correct"] for p in priced), Decimal("0"))
        am_mc = None
        if am_doc is not None:
            am_mc = (am_doc.get("min_charge_by_lane") or {}).get(lane)
        t_a = _total_under_rate(contract, u, priced, lane, amend_rate,
                                _d(am_mc) if am_mc is not None else _min_charge(contract, lines[0]))
        if billed > min(t_a, t_n):
            lo = max(Decimal("0"), billed - max(t_a, t_n))
            hi = max(Decimal("0"), billed - min(t_a, t_n))
            evs = list(evidence)
            if amend_ev and amend_ev not in evs:
                evs.append(amend_ev)
            nev = _notice_ev(notice_e, lane, notice_rate)
            if nev not in evs:
                evs.append(nev)
            prec = _clause_ev(contract, "precedence")
            if prec and prec not in evs:
                evs.append(prec)
            rng = [_amount_usd(inv, contract, lo), _amount_usd(inv, contract, hi)]
            if rng[0] is None or rng[1] is None:
                return item(f"overcharge:{iid}", "overcharge", "cannot_determine",
                            _outbound_action("draft:request_document", "execute:request_document",
                                             Decimal("0"), mode), 0.7, evs, invoice_id=iid,
                            missing_doc="Annex F exchange rates")
            return item(f"overcharge:{iid}", "overcharge", "conflict", "escalate", 0.9,
                        evs, invoice_id=iid, amount_range=rng)

    diff = Decimal("0")
    for p in priced:
        diff += p["billed"] - p["correct"]
    if diff <= 0:
        return item(f"overcharge:{iid}", "overcharge", "no_finding", "none", 0.9, evidence,
                    invoice_id=iid)

    key = f"overcharge:{iid}"
    la = learned.get(key)
    if la is None:
        fact = _accepted_fact(facts, contract, lane, pickup)
        if fact is not None:
            la = {"feedback_item_id": fact.get("fb"), "batch": fact.get("batch")}
    amt = _amount_usd(inv, contract, diff)
    if amt is None:
        return item(f"overcharge:{iid}", "overcharge", "cannot_determine",
                    _outbound_action("draft:request_document", "execute:request_document",
                                     Decimal("0"), mode), 0.7, evidence, invoice_id=iid,
                    missing_doc="Annex F exchange rates")
    action = _outbound_action("draft:dispute", "execute:dispute", _d(amt), mode)
    return item(key, "overcharge", "finding", action,
                0.9, evidence, invoice_id=iid, amount=amt, learned_at=la)


def _is_freight(l: dict) -> bool:
    d = str(l.get("desc") or "")
    return bool(FREIGHT_OK.search(d) and not FUEL_OK.search(d))


def _is_fuel(l: dict) -> bool:
    return bool(FUEL_OK.search(str(l.get("desc") or "")))


def _total_under_rate(contract: dict, u: Universe, priced: list[dict], lane: str,
                      rate: Decimal, min_charge: Decimal | None) -> Decimal:
    """The invoice's correct total if `lane` were priced at `rate` (fuel rides on that freight)."""
    refreight: dict[str, Decimal] = {}
    for p in priced:
        l = p["line"]
        if l.get("lane") == lane and _is_freight(l):
            c = r2(rate * _d(l.get("qty") or 0))
            if min_charge is not None and min_charge > c:
                c = min_charge
            refreight[l.get("awb")] = refreight.get(l.get("awb"), Decimal("0")) + c
    total = Decimal("0")
    for p in priced:
        l = p["line"]
        awb = l.get("awb")
        if l.get("lane") == lane and _is_freight(l):
            c = r2(rate * _d(l.get("qty") or 0))
            if min_charge is not None and min_charge > c:
                c = min_charge
            total += c
        elif awb in refreight and _is_fuel(l):
            pct = _fuel_pct(contract, u.pickup_date(awb, l.get("date")))
            total += r2(refreight[awb] * pct / Decimal("100")) if pct is not None else p["correct"]
        else:
            total += p["correct"]
    return total


def _precedence_fact(facts: list[dict], contract: dict, lane: str):
    cid = contract.get("contract_id")
    for f in facts or []:
        if (f.get("fact_type") == "precedence"
                and f.get("contract_id") in (None, cid)
                and f.get("lane") in (None, lane)):
            return f
    return None


def _accepted_fact(facts: list[dict], contract: dict, lane: str, pickup: str | None):
    cid = contract.get("contract_id")
    for f in facts or []:
        if f.get("fact_type") != "rate_accepted":
            continue
        if f.get("contract_id") not in (None, cid) or f.get("lane") not in (None, lane):
            continue
        if pickup and f.get("valid_from") and pickup < str(f["valid_from"]):
            continue
        if pickup and f.get("valid_to") and pickup > str(f["valid_to"]):
            continue
        return f
    return None


def _authorized_rates(u: Universe, e: dict, facts: list[dict]) -> bool:
    """Schedule 1 (rates) or a feedback sender_authority fact covering this sender."""
    email = (e.get("sender_email") or "").lower()
    name = (e.get("sender_name") or "").strip().lower()
    for c in u.contracts:
        for s in c.get("schedule_1") or []:
            if "rates" not in (s.get("categories") or []):
                continue
            if (email and (s.get("email") or "").lower() == email) or \
                    (name and (s.get("person") or "").strip().lower() == name):
                return True
    for f in facts or []:
        if f.get("fact_type") == "sender_authority" and "rates" in (f.get("categories") or []):
            if email and (f.get("person_email") or "").lower() == email:
                return True
    return False


def _valid_notice(u: Universe, contract: dict, lane: str, pickup: str | None,
                  facts: list[dict], pf) -> tuple[dict | None, dict | None]:
    """A rate notice we may price from: authorized sender (Schedule 1 rates), effective window
    covering the pickup, contract reference matching — or accepted by feedback."""
    if not lane:
        return None, None
    cid = contract.get("contract_id")
    accepted = _accepted_fact(facts, contract, lane, pickup)
    if accepted is None and pf is not None and "notice" in str(pf.get("use") or "").lower():
        accepted = pf  # feedback resolved in the notice's favour: treat it as the rate
    for e in u.emails:
        n = e.get("rate_notice") or {}
        if n.get("lane") != lane:
            continue
        if n.get("contract_ref") and cid and n["contract_ref"] != cid:
            continue
        if pickup:
            ef, et = n.get("effective_from"), n.get("effective_to")
            if ef and pickup < ef:
                continue
            if et and pickup > et:
                continue
        if accepted is not None or _authorized_rates(u, e, facts):
            return e, n
    return None, None


def _notice_band_rate(n: dict | None, line: dict) -> Decimal | None:
    if not n:
        return None
    w = _d(line.get("qty") or 0)
    for b in n.get("bands", []):
        if _d(b.get("min_kg", 0)) <= w <= _d(b.get("max_kg", 10**9)):
            return _d(b["rate"])
    return None


def _notice_ev(ne: dict, lane: str, rate: Decimal) -> dict:
    for sent in (ne.get("body") or "").split("\n"):
        s = sent.strip()
        if lane in s and str(rate) in s and s:
            return page_ev(ne, s)
    n = ne.get("rate_notice") or {}
    return page_ev(ne, (n.get("text") or "")[:200])


def _amendment_rate(contract: dict, line: dict, u: Universe,
                    pickup: str | None) -> tuple[Decimal | None, dict | None, dict | None]:
    """The rate a signed amendment (not the MSA) sets for this lane/band/period, if it is in force."""
    lane = line.get("lane") or ""
    origin, _, dest = lane.partition("-")
    weight = _d(line.get("qty") or 0)
    for am in reversed(contract.get("amendments") or []):
        if pickup and am.get("effective_date") and pickup < am["effective_date"]:
            continue
        for cl in am.get("lanes", []):
            if cl.get("origin") != origin or cl.get("destination") != dest:
                continue
            for band in cl.get("bands", []):
                lo, hi = _d(band.get("min_kg", 0)), _d(band.get("max_kg", 10**12))
                if not (lo <= weight <= hi):
                    continue
                ps, pe = band.get("period_start"), band.get("period_end")
                if pickup and ps and pe and not (ps <= pickup <= pe):
                    continue
                quote = band.get("quote") or ""
                rev = ev(am["doc_id"], int(band.get("page", 1)), quote) if quote else None
                return _d(band["rate"]), rev, am
    return None, None, None


def _missing_named_annex(u: Universe, lines: list[dict]) -> dict | None:
    """A held document names an annex revision we don't hold for this lane -> cannot price."""
    lane = lines[0].get("lane") or ""
    pickup = u.pickup_date(lines[0].get("awb", ""), lines[0].get("date"))
    held = [re.sub(r"[^a-z0-9]", "", (d.get("doc_id") or "").lower()) for d in u.docs]
    for c in u.contracts:
        for na in c.get("named_annexes") or []:
            name = na.get("name") or ""
            want = re.sub(r"[^a-z0-9]", "", name.lower())
            if not want or not na.get("lanes"):
                continue
            if any(want in h for h in held):
                continue  # that revision IS in the mailbox
            if lane and lane not in na["lanes"]:
                continue
            if na.get("applies_from") and pickup and pickup < na["applies_from"]:
                continue  # before the replacement took effect: old rates govern
            return {"doc": c.get("doc_id"), "page": int(na.get("page", 1)),
                    "quote": na.get("quote") or "",
                    "missing": {"name": name, "holder": _rates_contact(u)}}
    return None


def _rates_contact(u: Universe) -> dict:
    """Schedule 1's rates person for the issuing party (the holder of a missing rate annex)."""
    prefix = ""
    if u.contracts:
        cid = u.contracts[0].get("contract_id") or ""
        prefix = cid.split("-EXP-")[0]
    fallback = None
    for c in u.contracts:
        for s in c.get("schedule_1") or []:
            if "rates" not in (s.get("categories") or []):
                continue
            entry = {"person": s.get("person"), "party": s.get("party"),
                     "role": s.get("title") or "rates"}
            if prefix and s.get("party") == prefix:
                return entry
            fallback = fallback or entry
    return fallback or {"party": "the carrier", "role": "rates"}


def _governing_contract(u: Universe, lines: list[dict]) -> dict | None:
    """The contract (plus its amendments) covering these lanes; amendments rank above the MSA."""
    lanes = {l.get("lane") for l in lines if l.get("lane")}
    base = None
    for c in u.contracts:
        if c.get("is_amendment") or c.get("doc_type") == "amendment":
            continue
        clanes = {f"{x.get('origin')}-{x.get('destination')}" for x in c.get("lanes", [])}
        if lanes & clanes:
            base = c
            break
    if base is None:
        bases = [c for c in u.contracts if not c.get("is_amendment")
                 and c.get("doc_type") != "amendment"]
        base = bases[0] if bases else (u.contracts[0] if u.contracts else None)
    if base is None:
        return None
    # attach amendments for the same base contract (later amendment wins for the same lane)
    amds = [c for c in u.contracts
            if (c.get("is_amendment") or c.get("doc_type") == "amendment")
            and (c.get("base_contract_id") or "") in ("", base.get("contract_id"))]
    if amds:
        merged = dict(base)
        merged["amendments"] = amds
        return merged
    return base


def _correct_rate(contract: dict, line: dict, u: Universe,
                  pickup: str | None) -> tuple[Decimal | None, dict | None]:
    """The rate for lane + weight band + period containing pickup. Amendments override the MSA;
    feedback-resolved precedence overrides amendments."""
    lane = line.get("lane") or ""
    origin, _, dest = lane.partition("-")
    weight = _d(line.get("qty") or 0)

    # feedback: a resolved precedence tells us which source to use
    forced = None
    for f in contract.get("_facts", []) or []:
        if f.get("fact_type") == "precedence" and f.get("contract_id") == contract.get("contract_id"):
            if f.get("lane") in (None, lane):
                forced = f.get("use")
    # later amendment first (only if in force at pickup), then MSA rates
    candidates: list[tuple[str, dict]] = []
    for am in reversed(contract.get("amendments") or []):
        if pickup and am.get("effective_date") and pickup < am["effective_date"]:
            continue
        candidates.append(("amendment", am))
    candidates.append(("msa", contract))

    for kind, src in candidates:
        if forced and forced not in (kind, src.get("contract_id")):
            continue
        for cl in src.get("lanes", []):
            if cl.get("origin") != origin or cl.get("destination") != dest:
                continue
            for band in cl.get("bands", []):
                lo, hi = _d(band.get("min_kg", 0)), _d(band.get("max_kg", 10**12))
                if not (lo <= weight <= hi):
                    continue
                ps, pe = band.get("period_start"), band.get("period_end")
                if pickup and ps and pe and not (ps <= pickup <= pe):
                    continue
                page = int(band.get("page", 1))
                quote = band.get("quote") or ""
                rev = ev(src["doc_id"], page, quote) if quote else None
                return _d(band["rate"]), rev
    return None, None


# ---- class 2: duplicate ------------------------------------------------------------------------------------------------
def check_duplicate(inv: dict, u: Universe, mode: str, learned: dict) -> dict | None:
    iid = inv["invoice_id"]
    row = u.register_row(iid)
    evidence = []
    # same invoice number resent: ledger shows paid/scheduled AND resend asks again
    resends = [e for e in u.emails
               if iid in (e.get("mentions", {}) or {}).get("invoice_ids", [])
               and e.get("asks_for_payment")]
    first_arrival = min((e.get("received_date") for e in u.emails
                         if iid in (e.get("mentions", {}) or {}).get("invoice_ids", [])),
                        default=None)
    if row and row.get("status") in ("paid", "scheduled") and resends:
        later = [e for e in resends if (e.get("received_date") or "") > (first_arrival or "")]
        if later and not any(e.get("is_copy_or_reminder") for e in later):
            evidence.append(_row_ev(u, row))
            e = later[0]
            evidence.append(ev(e["doc_id"], 1, (e.get("body") or "")[:200]))
            amount = inv.get("total") or row.get("amount") or 0
            action = _outbound_action("draft:dispute", "execute:dispute", _d(amount), mode)
            return item(f"duplicate:{iid}", "duplicate", "finding", action, 0.9,
                        evidence, invoice_id=iid, amount=usd(amount),
                        learned_at=learned.get(f"duplicate:{iid}"))
    # two invoice numbers, same shipment lines -> flag the later invoice date
    same_ship = _same_shipment_twice(inv, u)
    if same_ship:
        other_inv, quote = same_ship
        if (inv.get("invoice_date") or "") >= (other_inv.get("invoice_date") or ""):
            overlap = _overlapped_lines(inv, other_inv)
            if overlap > 0:
                evidence.append(page_ev(inv, _first_quote(inv)))
                evidence.append(page_ev(other_inv, _first_quote(other_inv)))
                action = _outbound_action("draft:dispute", "execute:dispute", overlap, mode)
                return item(f"duplicate:{iid}", "duplicate", "finding", action, 0.8,
                            evidence, invoice_id=iid, amount=usd(overlap),
                            learned_at=learned.get(f"duplicate:{iid}"))
    return None


def _same_shipment_twice(inv: dict, u: Universe):
    def awbs(d: dict) -> set:
        return {l.get("awb") for l in d.get("lines", [])
                if l.get("awb") not in (None, "", "-")}

    inv_awbs = awbs(inv)
    if not inv_awbs:
        return None  # no shipment on the document: nothing to double-bill
    for other in u.invoices:
        if other["invoice_id"] == inv["invoice_id"]:
            continue
        if inv_awbs & awbs(other):
            return other, ""
    return None


def _overlapped_lines(inv: dict, other: dict) -> Decimal:
    total = Decimal("0")
    other_by_awb: dict[str, list] = {}
    for l in other.get("lines", []):
        other_by_awb.setdefault(l.get("awb"), []).append(l)
    for l in inv.get("lines", []):
        for o in other_by_awb.get(l.get("awb"), []):
            if abs(float(l.get("amount") or 0) - float(o.get("amount") or 0)) < 0.005:
                total += _d(l["amount"])
    # subtract any credit covering the overlap
    return total


# ---- class 3: waived but billed ---------------------------------------------------------------------------------------
def check_waived(inv: dict, u: Universe, mode: str, facts: list[dict],
                 learned: dict) -> dict | None:
    iid = inv["invoice_id"]
    waivers = [e for e in u.emails if e.get("kind") == "waiver" and e.get("waiver")]
    if not waivers:
        return None
    billed_lines = inv.get("lines", [])
    for w in waivers:
        wl = w.get("waiver") or {}
        if _waiver_covers(w, wl, u, facts) is False:
            continue
        for l in billed_lines:
            if _line_matches_waiver(l, wl):
                amount = _d(l.get("amount") or 0)
                if amount <= 0:
                    continue
                evidence = [
                    ev(w["doc_id"], 1, wl.get("text") or _first_body(w)),
                    page_ev(inv, l.get("raw", "")),
                ]
                sched = _schedule1_quote(u, "waivers")
                if sched:
                    evidence.append(sched)
                action = _outbound_action("draft:dispute", "execute:dispute", amount, mode)
                return item(f"waived_but_billed:{iid}", "waived_but_billed", "finding",
                            action, 0.85, evidence, invoice_id=iid, amount=usd(amount),
                            learned_at=learned.get(f"waived_but_billed:{iid}"))
    return None


def _waiver_covers(email_doc: dict, wl: dict, u: Universe, facts: list[dict]) -> bool | None:
    """True = authority + scope OK; False = not authoritative; None = quoted-only -> cannot_determine."""
    sender = email_doc.get("sender_email", "")
    # feedback facts: sender_authority / waiver_scope
    for f in facts:
        if f.get("fact_type") == "sender_authority" and f.get("person_email") == sender:
            if f.get("valid_from") and str(f["valid_from"]) > (email_doc.get("received_date") or ""):
                pass
            else:
                return True
    if not _schedule1_has(u, sender, "waivers"):
        return False
    return True


def _schedule1_has(u: Universe, email: str, category: str) -> bool:
    email = (email or "").lower()
    for c in u.contracts:
        for s in c.get("schedule_1", []) or []:
            if (s.get("email", "").lower() == email
                    and category in (s.get("categories") or [])):
                return True
    return False


def _schedule1_quote(u: Universe, category: str) -> dict | None:
    for c in u.contracts:
        q = c.get("quotes", {})
        for v in q.values():
            if isinstance(v, dict) and v.get("quote"):
                return ev(c["doc_id"], int(v.get("page", 1)), str(v["quote"]))
    return None


def _line_matches_waiver(line: dict, wl: dict) -> bool:
    if wl.get("awb") and line.get("awb") != wl.get("awb"):
        return False
    if wl.get("charge") and wl["charge"].lower() not in (line.get("desc") or "").lower():
        return False
    return True


# ---- class 4: late fee not owed ---------------------------------------------------------------------------------------
def check_late_fee_not_owed(inv: dict, u: Universe, mode: str, facts: list[dict],
                            learned: dict) -> dict | None:
    """A late fee billed on an invoice that was actually paid on time (or fee is wrong)."""
    # find late-fee lines on this invoice
    fee_lines = [l for l in inv.get("lines", [])
                 if "late fee" in (l.get("desc") or "").lower()]
    if not fee_lines:
        return None
    for fl in fee_lines:
        # which invoice does this fee reference?
        m = _target_invoice(fl)
        if not m:
            continue
        target_row = u.register_row(m)
        if not target_row:
            return None
        # effective due date (extension emails, from authorised sender)
        eff_due = _effective_due_date(m, target_row, u, facts)
        paid = target_row.get("paid_date")
        if not paid or not eff_due:
            continue
        days_late = (_date(paid) - _date(eff_due)).days
        contract = _contract_for(u, inv.get("contract_ref"))
        grace = _grace_days(u, inv.get("contract_ref"))
        if days_late <= grace:
            # fee not owed -> whole fee line is the finding
            amount = _d(fl.get("amount") or 0)
            if amount <= 0:
                continue
            amt = _amount_usd(inv, contract or {}, amount)
            if amt is None:
                continue  # invoice currency unconvertible: never invent an amount
            evidence = [
                page_ev(inv, fl.get("raw", "")),
                _row_ev(u, target_row),
            ]
            ext = _extension_email(u, m)
            if ext:
                evidence.append(ev(ext["doc_id"], 1, ext.get("extension", {}).get("text")
                                   or _first_body(ext)))
            action = _outbound_action("draft:dispute", "execute:dispute", _d(amt), mode)
            return item(f"late_fee_not_owed:{inv['invoice_id']}", "late_fee_not_owed",
                        "finding", action, 0.85, evidence,
                        invoice_id=inv["invoice_id"], amount=amt,
                        learned_at=learned.get(f"late_fee_not_owed:{inv['invoice_id']}"))
    return None


def _target_invoice(fee_line: dict) -> str | None:
    import re
    raw = fee_line.get("raw", "") + " " + (fee_line.get("desc") or "")
    m = re.search(r"([A-Z]{2,4}-?[A-Z]{0,4}-?\d{4}-?\d{3,})", raw)
    if m:
        return m.group(1).replace(" ", "").replace(":", "").replace("#", "")
    # slash-shaped ids: "Late fee on TXI/35-36/0021"
    m = re.search(r"\b([A-Z][A-Z0-9]*(?:/[A-Z0-9]+)+)\b", raw)
    return m.group(1) if m else None


def _extension_email(u: Universe, invoice_id: str) -> dict | None:
    for e in u.emails:
        if e.get("kind") == "extension" and (e.get("extension") or {}).get("invoice_id") == invoice_id:
            return e
    return None


def _effective_due_date(invoice_id: str, row: dict, u: Universe, facts: list[dict]) -> str | None:
    ext = _extension_email(u, invoice_id)
    if ext:
        sender = ext.get("sender_email", "")
        if _schedule1_has(u, sender, "payment_terms"):
            return (ext.get("extension") or {}).get("new_due_date") or row.get("due_date")
        for f in facts:
            if (f.get("fact_type") == "sender_authority" and f.get("person_email") == sender
                    and "payment_terms" in (f.get("categories") or [])):
                return (ext.get("extension") or {}).get("new_due_date") or row.get("due_date")
    return row.get("due_date")


def _contract_for(u: Universe, ref: str | None) -> dict | None:
    """Held base contract with this id, else the first held base contract (amendments never
    carry fee terms or grace)."""
    bases = [c for c in u.contracts
             if not c.get("is_amendment") and c.get("doc_type") != "amendment"]
    if ref:
        for c in bases:
            if c.get("contract_id") == ref:
                return c
    return bases[0] if bases else (u.contracts[0] if u.contracts else None)


def _grace_days(u: Universe, ref: str | None = None) -> int:
    c = _contract_for(u, ref)
    if c and (c.get("late_fee") or {}).get("grace_days") is not None:
        return int(c["late_fee"]["grace_days"])
    for x in u.contracts:
        lf = x.get("late_fee")
        if lf and lf.get("grace_days") is not None:
            return int(lf["grace_days"])
    return 0


# ---- late fee risk (portfolio) ----------------------------------------------------------------------------------------
def check_late_fee_risk(u: Universe, as_of: str, mode: str,
                        findings: list[dict] | None = None) -> list[dict]:
    """A fee will start accruing on an unpaid invoice due within 14 days: warn now.
    Driven by the AP register (the source of truth for due dates and open amounts)."""
    out = []
    as_of_d = _date(as_of)
    if not as_of_d:
        return out
    rate = _late_fee_rate(u)
    if not rate:
        return out
    clause = None
    for c in u.contracts:
        q = _clause_ev(c, "late_fee")
        if q:
            clause = q
            break
    # amounts we already disputed on an invoice reduce the at-risk principal
    disputed: dict[str, Decimal] = {}
    for it in findings or []:
        if it.get("verdict") == "finding" and it.get("invoice_id"):
            iid = it["invoice_id"]
            disputed[iid] = disputed.get(iid, Decimal("0")) + _d(it.get("amount_usd") or 0)
    seen: set[str] = set()
    for reg in u.registers:
        for row in reg.get("rows") or []:
            iid = row.get("invoice_id") or ""
            if not iid or iid in seen:
                continue
            seen.add(iid)
            if (row.get("status") or "open") in ("paid", "credit", "disputed"):
                continue
            due = row.get("due_date")
            due_d = _date(due)
            if not due_d:
                continue
            delta = (due_d - as_of_d).days
            if not (0 < delta <= 14):
                continue
            inv = next((i for i in u.invoices if i.get("invoice_id") == iid), None)
            contract = _contract_for(u, (inv or {}).get("contract_ref"))
            row_rate = _late_fee_rate(u, (inv or {}).get("contract_ref")) or rate
            principal = _d(row.get("amount") or 0)
            if not principal:
                principal = _d((inv or {}).get("total") or 0)
            if inv and (inv.get("currency") or "USD") == "INR":
                fx = (contract or {}).get("fx_inr_per_usd_by_month") or {}
                fxr = fx.get((inv.get("invoice_date") or "")[:7])
                if not fxr:
                    continue  # can't express the exposure in USD: don't invent
                principal = r2(principal / _d(fxr))
            principal = max(Decimal("0"), principal - disputed.get(iid, Decimal("0")))
            if principal <= 0:
                continue
            fee = r2(_d(row_rate) / Decimal("100") * principal)
            evidence = [_row_ev(u, row)]
            cq = _clause_ev(contract, "late_fee") if contract else None
            if cq:
                evidence.append(cq)
            elif clause:
                evidence.append(clause)
            if inv:
                fq = _first_quote(inv)
                if fq:
                    evidence.append(page_ev(inv, fq))
            out.append(item(
                f"late_fee_risk:{iid}", "late_fee_risk", "finding",
                "internal:payment_reminder", 0.8, evidence,
                invoice_id=iid, amount=usd(fee), deadline=due,
                learned_at=None,
            ))
    return out


def _late_fee_rate(u: Universe, ref: str | None = None) -> float:
    c = _contract_for(u, ref)
    if c and (c.get("late_fee") or {}).get("rate_percent") is not None:
        return float(c["late_fee"]["rate_percent"])
    for x in u.contracts:
        lf = x.get("late_fee")
        if lf and lf.get("rate_percent") is not None:
            return float(lf["rate_percent"])
    return 0.0


# ---- class 6: cashback ------------------------------------------------------------------------------------------------
def check_cashback(contract: dict, u: Universe, as_of: str, mode: str, facts: list[dict],
                   closed: dict, learned: dict) -> list[dict]:
    out: list[dict] = []
    cid = contract.get("contract_id")
    if not cid:
        return out
    if contract.get("is_amendment") or contract.get("doc_type") == "amendment":
        return out  # the MSA carries the rebate clause
    tiers = contract.get("rebate_tiers") or []
    if not tiers:
        return out
    # group THIS contract's invoices by quarter (never another party's spend)
    by_period: dict[str, Decimal] = {}
    for inv in u.invoices:
        if not _invoice_matches_contract(inv, contract):
            continue
        idate = inv.get("invoice_date") or ""
        if not idate:
            continue
        q = _quarter(idate)
        spend = _qualifying_spend(inv, contract)
        if spend:
            by_period[q] = by_period.get(q, Decimal("0")) + spend
    as_of_d = _date(as_of)
    for period, qs in sorted(by_period.items()):
        if not _period_closed(period, as_of_d):
            continue  # only report once the period has closed
        key = f"cashback:{cid}/{period}/rebate"
        if key in closed:
            continue  # feedback closed this key: stop raising it
        evidence = []
        q_ev = _contract_quote(contract, "qualifying_spend")
        if q_ev:
            evidence.append(q_ev)
        tier_ev = _clause_ev(contract, "rebate")
        if tier_ev and tier_ev not in evidence:
            evidence.append(tier_ev)
        tier = None
        for t in sorted(tiers, key=lambda x: -float(x.get("min_qs", 0))):
            if qs >= _d(t.get("min_qs", 0)):
                tier = t
                break
        if tier is None:
            out.append(item(key, "cashback", "no_finding", "none", 0.8, evidence,
                            contract_id=cid, period=period, credit_type="rebate",
                            learned_at=learned.get(key)))
            continue  # below the lowest tier: no rebate earned
        owed = r2(qs * _d(tier["percent"]) / Decimal("100"))
        credited = _rebate_credits(u, cid, period)
        net = owed - credited
        if net <= 0:
            out.append(item(key, "cashback", "no_finding", "none", 0.8, evidence,
                            contract_id=cid, period=period, credit_type="rebate",
                            learned_at=learned.get(key)))
            continue  # already credited in full
        due = _credit_due(period)
        action = ("draft:claim_credit" if (as_of_d and as_of_d >= due)
                  else "internal:track_credit")
        if mode == "autonomous" and action == "draft:claim_credit":
            action = "execute:claim_credit"
        out.append(item(key, "cashback", "finding", action, 0.8, evidence,
                        contract_id=cid, period=period, credit_type="rebate",
                        amount=usd(net), learned_at=learned.get(key)))
    return out


def _invoice_matches_contract(inv: dict, contract: dict) -> bool:
    """Attribute an invoice to one contract only: printed contract reference first,
    otherwise lane coverage (invoices on lanes this contract prices)."""
    cid = contract.get("contract_id") or ""
    ref = (inv.get("contract_ref") or "").strip()
    if ref:
        return ref == cid or ref == (contract.get("base_contract_id") or "")
    clanes = {f"{x.get('origin')}-{x.get('destination')}" for x in contract.get("lanes") or []}
    if not clanes:
        return True  # single-contract sets: no lanes to separate by
    ilanes = {l.get("lane") for l in inv.get("lines") or [] if l.get("lane")}
    if not ilanes:
        return False  # can't attribute: never claim someone else's spend
    return bool(ilanes & clanes)


def _quarter(d: str) -> str:
    y, m = int(d[:4]), int(d[5:7])
    return f"{y}-Q{(m - 1) // 3 + 1}"


def _period_closed(period: str, as_of: date | None) -> bool:
    if not as_of:
        return False
    if "-Q" in period:
        y, q = period.split("-Q")
        end_month = int(q) * 3
        last = date(int(y), end_month, 1)
        # last day of the quarter's final month
        nxt = date(int(y) + (end_month == 12), (end_month % 12) + 1, 1)
        return as_of >= (nxt - timedelta(days=1))
    if period.startswith("CY"):
        return True  # contract-year credits: assume closed when the report asks
    return False


def _credit_due(period: str) -> date:
    """Credits fall due shortly after the period closes; use period end + 45 days."""
    if "-Q" in period:
        y, q = period.split("-Q")
        end_month = int(q) * 3
        nxt = date(int(y) + (end_month == 12), (end_month % 12) + 1, 1)
        return nxt - timedelta(days=1) + timedelta(days=45)
    return date.today()


def _qualifying_spend(inv: dict, contract: dict) -> Decimal:
    """freight (base, fuel, security, listed accessorials), net of credit notes; no tax/duty/fees."""
    total = Decimal("0")
    for l in inv.get("lines", []):
        desc = (l.get("desc") or "").lower()
        if any(x in desc for x in ("tax", "duty", "late fee", "gst", "vat")):
            continue
        total += _d(l.get("amount") or 0)
    if not inv.get("lines"):
        total = _d(inv.get("total") or 0)
    if (inv.get("currency") or "USD") == "INR":
        fx = contract.get("fx_inr_per_usd_by_month") or {}
        rate = fx.get((inv.get("invoice_date") or "")[:7])
        if not rate:
            return Decimal("0")  # no Annex F row: cannot convert, never invent spend
        total = r2(total / _d(rate))
    return total


def _period_range(period: str) -> tuple[date, date]:
    """('2035-Q1') -> (first day, last day)."""
    y, q = (int(x) for x in period.split("-Q"))
    start = date(y, (q - 1) * 3 + 1, 1)
    end_month = q * 3
    nxt = date(y + (end_month == 12), (end_month % 12) + 1, 1)
    return start, nxt - timedelta(days=1)


def _rebate_credits(u: Universe, contract_id: str, period: str) -> Decimal:
    """Rebate credits issued for THIS period; improvement credits and other periods don't count."""
    total = Decimal("0")
    start, end = _period_range(period)
    for c in u.credits:
        if c.get("contract_id") and c.get("contract_id") != contract_id:
            continue
        text = " ".join(c.get("raw_pages") or [])
        if not re.search(r"\brebate\b", text, re.I):
            continue
        cd_d = _date(c.get("invoice_date"))
        in_window = bool(cd_d and start <= cd_d <= end + timedelta(days=45))
        mentions_period = period in text
        if not (in_window or mentions_period):
            continue
        total += _d(c.get("total") or 0)
    return total


def _contract_quote(contract: dict, key: str) -> dict | None:
    q = contract.get("quotes", {})
    v = q.get(key)
    if isinstance(v, dict) and v.get("quote"):
        return ev(contract["doc_id"], int(v.get("page", 1)), str(v["quote"]))
    for other in q.values():
        if isinstance(other, dict) and other.get("quote"):
            return ev(contract["doc_id"], int(other.get("page", 1)), str(other["quote"]))
    return None


# ---- class 7: expiry --------------------------------------------------------------------------------------------------
def check_expiry(contract: dict, as_of: str, learned: dict) -> list[dict]:
    cid = contract.get("contract_id")
    if not cid:
        return []
    ex = contract.get("expiry") or {}
    as_of_d = _date(as_of)
    if not as_of_d:
        return []
    # operative date: end date, or notice deadline when the contract renews itself
    if ex.get("auto_renews") and ex.get("notice_deadline"):
        deadline = ex.get("notice_deadline")
    else:
        deadline = ex.get("end_date") or contract.get("end_date")
    if not deadline:
        return []
    dl = _date(deadline)
    if not dl:
        return []
    days = (dl - as_of_d).days
    if not (0 <= days <= 60):
        return []
    q = _contract_quote(contract, "expiry")
    evidence = [q] if q else []
    return [item(f"expiry:{cid}", "expiry", "finding", "internal:alert", 0.9, evidence,
                 contract_id=cid, deadline=deadline,
                 learned_at=learned.get(f"expiry:{cid}"))]


# ---- shared helpers ---------------------------------------------------------------------------------------------------
def iid_doc_of(inv: dict) -> str:
    return inv.get("source_email") or inv.get("doc_id", "")


def _first_quote(inv: dict) -> str:
    pages = inv.get("raw_pages") or []
    if pages:
        for line in pages[0].split("\n"):
            if line.strip():
                return line.strip()[:200]
    return ""


def _first_body(e: dict) -> str:
    body = e.get("body") or ""
    for line in body.split("\n"):
        if line.strip():
            return line.strip()[:200]
    return ""


def _register_doc(u: Universe) -> str:
    return u.registers[0]["doc_id"] if u.registers else ""


def _missing_rate_annex(u: Universe) -> dict:
    """The holder of a missing rate annex: issuing party's Schedule 1 rates contact."""
    for c in u.contracts:
        for s in c.get("schedule_1", []) or []:
            if "rates" in (s.get("categories") or []):
                return {"name": "the rate schedule for this lane",
                        "holder": {"person": s.get("person"), "party": s.get("party"),
                                   "role": "rates"}}
    return {"name": "the rate schedule for this lane",
            "holder": {"party": "the carrier", "role": "rates"}}


def _outbound_action(draft: str, execute: str, amount, mode: str) -> str:
    """supervised: draft (human_review at >= $5,000). autonomous: execute iff unattended test."""
    amt = _d(amount)
    if mode == "supervised":
        return "human_review" if amt >= Decimal("5000") else draft
    # autonomous: all five checks would be verified; threshold is one of them
    if amt >= Decimal("5000"):
        return "human_review"
    return execute

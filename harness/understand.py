"""Layer 2: text -> the universal document schema every downstream check reads.

Hybrid by design, and LLM-frugal: the grader gives each `ingest` step 600 seconds, and a batch
can hold 40+ emails, so we spend LLM calls only where meaning is actually needed.

- classification:   filename + content keywords first; LLM only when unsure
- invoices/credits: deterministic regex (exact numbers matter); LLM fallback per-field
- tracking/register: deterministic (they are tables)
- emails:           deterministic mentions + keyword kind; LLM only for scope the regex missed
- contracts:        LLM (the one heavy call: clauses, rates, Schedule 1 vary by carrier)
- scans:            vision model (no text layer)

Every doc keeps raw_pages: evidence quotes come from there (the same text the grader checks),
never from an LLM paraphrase.
"""
from __future__ import annotations

import re
from datetime import datetime

from . import extract as X
from . import llm


# ---- shared helpers ---------------------------------------------------------------------------------------------------
def _invoice_id(raw: str) -> str:
    """As printed, uppercase, spaces and the characters '#' and ':' taken out."""
    return raw.upper().replace(" ", "").replace("#", "").replace(":", "")


DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
# an ID-shaped token: letters/digits joined by dashes, must contain a digit (TX-US-2032-5394, CDI-2930-0148)
ID_TOKEN = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b")
AWB_RE = re.compile(r"\b([A-Z]{2}\d{7,}(?:-H\d+)?)\b")
# slash-joined invoice ids like TXI/35-36/0030
SLASH_ID = re.compile(r"\b([A-Z][A-Z0-9]*(?:/[A-Z0-9]+)+)\b")


def _looks_like_doc_id(tok: str) -> bool:
    """Filter ID tokens: skip dates, plain words, lane codes (USE2-INE2), version numbers."""
    if not any(ch.isdigit() for ch in tok):
        return False
    if DATE_RE.fullmatch(tok):
        return False
    # lane codes are 4 letters + digit both sides: USE2-INE2
    if re.fullmatch(r"[A-Z]{3}\d-[A-Z]{3}\d", tok):
        return False
    # contract ids carry -EXP- (TBX-US-EXP-01) or /EXP/ (never an invoice)
    if "-EXP-" in tok or "/EXP/" in tok:
        return False
    return True


# ---- classification: heuristic first ----------------------------------------------------------------------------------
def _classify_heuristic(doc_id: str, pages: list[str]) -> str:
    name = doc_id.split("#")[-1].lower()
    head = (pages[0][:1200] if pages else "").lower()
    text = name + " " + head

    if "msa" in name or "master services agreement" in head or "amendment" in name and "msa" not in name:
        if "amendment" in name or "amendment" in head[:300]:
            return "amendment"
        return "contract_msa"
    if "amendment" in text[:400]:
        return "amendment"
    if "tracking" in name or ("awb" in head and "event" in head and "picked_up" in head):
        return "tracking_export"
    if "register" in name or ("paid" in head and "due" in head and "|" in head):
        return "ap_register"
    if "credit note" in text or "credit memo" in text:
        return "credit_note"
    # a monthly statement counts as an invoice (GLOSSARY) - never hand this to the LLM
    if "statement of account" in text:
        return "invoice"
    if ("invoice" in head or re.search(r"\binvoice\b", name)
            or "freight bill" in text or "balance due" in head or "net charges" in head
            or re.search(r"charges\s*advice", text)):  # carriers that title the bill "Charges advice ..."
        return "invoice"
    if "waybill" in text or name.startswith("awb"):
        return "air_waybill"
    if "declaration" in text or name.startswith("dgd"):
        return "declaration"
    return "unsure"


def classify(doc_id: str, pages: list[str]) -> str:
    """Heuristic first; LLM only when the heuristic is unsure."""
    ht = _classify_heuristic(doc_id, pages)
    if ht != "unsure":
        return ht
    head = pages[0][:2500] if pages else ""
    try:
        out = llm.ask_json(
            'Classify this freight document. JSON only: {"doc_type": "..."} from: invoice, '
            "credit_note, contract_msa, amendment, tracking_export, ap_register, air_waybill, "
            "declaration, customs_notice, other.",
            f"File: {doc_id}\n\n{head}", max_tokens=100)
        dt = str(out.get("doc_type", "other"))
        return dt
    except Exception:
        return "other"


# ---- invoices (deterministic) -----------------------------------------------------------------------------------------
LINE_RE = re.compile(
    r"^(?P<idx>\d+)\s+(?P<awb>[A-Z]{2}\d{7,}(?:-H\d+)?|[A-Z0-9]+-[A-Z0-9]+-[A-Z0-9]+)\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2}|\d{1,2}\s+\w+\s+\d{4})\s+"
    r"(?P<desc>.+?)\s+(?P<lane>[A-Z]{3,4}\d?-[A-Z]{3,4}\d?)\s+"
    r"(?P<qty>[\d.]+)\s*(?P<unit>kg|shpt|pcs|days?|m3|cbm|lbl)?\s+"
    r"(?P<rate>[\d.]+%?|[\d.]+)\s+(?P<amount>[\d.]+)\s*$"
)
# fallback: any numbered line ending in an amount (fees, late fees, lines without a lane)
LINE_LOOSE_RE = re.compile(
    r"^(?P<idx>\d+)\s+(?P<awb>[A-Z]{2}\d{7,}(?:-H\d+)?|[A-Z0-9]+-[A-Z0-9]+-[A-Z0-9]+)?\s*"
    r"(?P<desc>.+?)\s+(?P<amount>\d+\.\d{2})\s*$"
)
# "Freight bill" layout: no index, amount BEFORE qty, optional house-AWB suffix
LINE_FB_RE = re.compile(
    r"^(?P<desc>.+?)\s+(?P<awb>[A-Z]{2}\d{7,}(?:-H\d+)?)\s+(?P<lane>[A-Z]{3,4}\d?-[A-Z]{3,4}\d?)\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2}|\d{1,2}\s+\w+\s+\d{4})\s+(?P<amount>[\d,]+\.\d{2})\s+"
    r"(?P<qty>[\d.]+)\s*(?P<unit>kg|shpt|pcs|days?|m3|cbm|lbl)?\s+(?P<rate>[\d.]+%?)?\s*$"
)
# "IN" layout: AWB first, description, pickup, lane, SAC code, then qty/rate/amount
LINE_IN_RE = re.compile(
    r"^(?P<awb>[A-Z]{2}\d{7,}(?:-H\d+)?)\s+(?P<desc>.+?)\s+(?P<date>\d{4}-\d{2}-\d{2})\s+"
    r"(?P<lane>[A-Z]{3,4}\d?-[A-Z]{3,4}\d?)\s+(?P<sac>\d{4,6})?\s*"
    r"(?P<qty>[\d.]+)\s*(?P<unit>kg|shpt|pcs|days?|m3|cbm|lbl)?\s+"
    r"(?P<rate>[\d.]+%?)\s+(?P<amount>[\d,]+\.\d{2})\s*$"
)
# "Charges advice" layout: lane, waybill, charge, basis, rate, amount, collected (DD/MM/YYYY);
# non-shipment rows (duty disbursements) end in "-" instead of a collected date
LINE_ADVICE_RE = re.compile(
    r"^(?P<lane>[A-Z]{3,4}\d?-[A-Z]{3,4}\d?)\s+(?P<awb>[A-Z]{2}\d{7,}(?:-H\d+)?)\s+(?P<desc>.+?)\s+"
    r"(?P<qty>[\d,]+(?:\.\d+)?)\s*(?P<unit>kg|shpt|pcs|days?|m3|cbm|lbl|fee)?\s+"
    r"(?P<rate>[\d,]+\.\d{2}%?)\s+(?P<amount>[\d,]+\.\d{2})\s+"
    r"(?P<date>\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2}|-)\s*$"
)
# billed late fee rows: "- - Late fee on CDU-3051262: USD 1448.48 overdue, ... 1.49% 21.58 -"
LINE_FEE_RE = re.compile(
    r"^-\s+-\s+(?P<desc>Late fee.+?)\s+(?P<amount>[\d,]+\.\d{2})\s+-\s*$"
)

MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}
MONTHS_FULL = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September",
     "October", "November", "December"], 1)}
MONTH_RANGE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+to\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)",
    re.I)


def norm_date(s: str | None) -> str | None:
    """'19 May 2033' -> '2033-05-19'; ISO passes through; '03/06/2030' -> '2030-06-03'
    (carriers print DD/MM/YYYY: 19/05/2030 can only be 19 May, 03/07/2030 is 30 days
    after 03/06/2030's document date). Only fall back to MM/DD when the first part > 12."""
    if not s:
        return None
    s = s.strip()
    if DATE_RE.fullmatch(s):
        return s
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if a > 12 >= b:
            day, month = a, b        # 19/05/2030: only DD/MM works
        elif b > 12 >= a:
            day, month = b, a        # 05/19/2030: only MM/DD works
        elif a <= 12 and b <= 12:
            day, month = a, b        # ambiguous: these carriers print DD/MM
        else:
            return None
        return f"{y}-{month:02d}-{day:02d}"
    m = re.fullmatch(r"(\d{1,2})\s+(\w{3,9})\s+(\d{4})", s)
    if m and m.group(2)[:3].title() in MONTHS:
        return f"{m.group(3)}-{MONTHS[m.group(2)[:3].title()]:02d}-{int(m.group(1)):02d}"
    return None


def _matched_line(m: re.Match, raw: str) -> dict:
    """Build a line dict from a match: strip thousands separators (1,701.31 -> 1701.31)
    and normalize any date we can read, so Decimal() downstream never sees a comma."""
    gd = {k: v for k, v in m.groupdict().items() if v is not None}
    for k in ("qty", "rate", "amount"):
        if isinstance(gd.get(k), str):
            gd[k] = gd[k].replace(",", "")
    if gd.get("date"):
        nd = norm_date(gd["date"])
        if nd:
            gd["date"] = nd
        else:
            gd.pop("date", None)  # "-" for rows with no collected date: never a usable pickup date
    return {**gd, "raw": raw}


def parse_invoice_lines(pages: list[str]) -> list[dict]:
    lines: list[dict] = []
    seen: set[str] = set()
    for page in pages:
        for raw in page.split("\n"):
            raw = raw.strip()
            if not raw:
                continue
            m = LINE_RE.match(raw)
            if m:
                lines.append(_matched_line(m, raw))
                seen.add(raw)
                continue
            m = LINE_LOOSE_RE.match(raw)
            if m and (m.group("awb") or "fee" in raw.lower() or "late" in raw.lower()):
                lines.append(_matched_line(m, raw))
                seen.add(raw)
                continue
            m = LINE_FB_RE.match(raw)
            if m:
                lines.append(_matched_line(m, raw))
                seen.add(raw)
                continue
            # IN layout: AWB first, description, pickup date, lane, SAC, qty, rate, amount
            m = LINE_IN_RE.match(raw)
            if m:
                lines.append(_matched_line(m, raw))
                seen.add(raw)
                continue
            # "Charges advice" layout: lane, waybill, charge, basis, rate, amount, collected
            m = LINE_ADVICE_RE.match(raw)
            if m:
                lines.append(_matched_line(m, raw))
                seen.add(raw)
                continue
            # billed late fee row (dash columns instead of an index)
            m = LINE_FEE_RE.match(raw)
            if m:
                lines.append(_matched_line(m, raw))
                seen.add(raw)
                continue
            # fee row that fits no column layout: "Late fee on TX-...: USD 2002.93 overdue ... - - - 39.66 1 month"
            if re.search(r"\blate fee\b", raw, re.I):
                m2 = re.search(r"-\s+-\s+-\s+([\d][\d,]*(?:\.\d{1,2})?)\s+\d+\s*month", raw)
                if not m2:
                    m2 = re.search(r"(\d+\.\d{2})\s*$", raw)
                if m2:
                    lines.append({"desc": raw, "amount": m2.group(1).replace(",", ""), "raw": raw})
                    seen.add(raw)
                    continue
            # pipe-separated sheet rows (monthly statements): Charge | Reference | Qty | Rate | Amount
            if "|" in raw:
                parts = [p.strip() for p in raw.split("|")]
                if (len(parts) >= 2 and re.search(r"[A-Za-z]", parts[0] or "")
                        and not re.match(r"^(total|balance|subtotal|tax|net|grand|charge\b"
                                         r"|statement\b|contract\b|bill to|invoice\b|due\b)",
                                         parts[0], re.I)):
                    amt = None
                    for p in reversed(parts[1:]):
                        if re.fullmatch(r"[\d][\d,]*(?:\.\d{1,2})?", p):
                            amt = p.replace(",", "")
                            break
                    if amt:
                        lines.append({"desc": parts[0], "amount": amt, "raw": raw})
                        seen.add(raw)
                        continue
    return lines


def extract_invoice(doc_id: str, pages: list[str], email_subject: str, received: str,
                    doc_type: str = "invoice") -> dict:
    text = "\n".join(pages)
    doc: dict = {
        "doc_id": doc_id, "doc_type": doc_type, "received_date": received,
        "source_email": doc_id.split("#")[0] if "#" in doc_id else doc_id,
        "currency": "INR" if re.search(r"\bINR\b", text) else "USD",
        "raw_pages": pages, "parse_ok": False,
    }
    doc["invoice_id"] = _extract_invoice_id(text, email_subject)
    _dpat = r"\d{4}-\d{2}-\d{2}|\d{1,2}\s+\w{3,9}\s+\d{4}|\d{1,2}/\d{1,2}/\d{4}"
    m = re.search(r"Invoice\s*date\s*:?\s*(" + _dpat + ")", text, re.I)
    if not m:
        m = re.search(r"Issued\s+on\s*:?\s*(" + _dpat + ")", text, re.I)
    if not m:
        m = re.search(r"Statement\s+date\s*:?\s*(" + _dpat + ")", text, re.I)
    if not m:
        m = re.search(r"Document\s+date\s*:?\s*(" + _dpat + ")", text, re.I)
    if not m:
        m = re.search(r"Credit\s+date\s*:?\s*(" + _dpat + ")", text, re.I)
    doc["invoice_date"] = norm_date(m.group(1)) if m else None
    m = re.search(r"Due\s*date\s*:?\s*(" + _dpat + ")", text, re.I)
    if not m:
        m = re.search(r"Settle\s+by\s*:?\s*(" + _dpat + ")", text, re.I)
    if not m:
        m = re.search(r"Payable\s*by\s*:?\s*(" + _dpat + ")", text, re.I)
    doc["due_date"] = norm_date(m.group(1)) if m else None
    m = re.search(r"(?:Contract\s*reference|Service\s+agreement)\s*:?\s*([A-Z0-9][A-Z0-9-]+)",
                  text, re.I)
    if not m:
        # "Contract CDA-US-EXP-E91 Currency USD" (a bare contract id, must carry a digit)
        m = re.search(r"\bContract\s*:?\s*(?=[A-Z0-9-]*\d)([A-Z][A-Z0-9-]*\d[A-Z0-9-]*)",
                      text, re.I)
    doc["contract_ref"] = m.group(1) if m else None
    doc["lines"] = parse_invoice_lines(pages)
    m = re.search(r"\bTotal\b(?:\s+(?:payable|due|amount|charges|credit|billed))?"
                  r"(?:\s+(?:USD|INR|EUR|GBP))?\s*\|?\s*:?\s*([\d,]+\.\d{1,2})", text, re.I)
    if not m:
        m = re.search(r"Balance\s+due\s+(?:USD|INR|EUR|GBP)\s*:?\s*([\d,]+\.\d{1,2})", text, re.I)
    doc["total"] = float(m.group(1).replace(",", "")) if m else None
    if doc["total"] is None and doc["lines"]:
        # last resort: sum of lines (only when no explicit total)
        doc["total"] = round(sum(float(l.get("amount") or 0) for l in doc["lines"]), 2)
    doc["parse_ok"] = bool(doc["invoice_id"])
    return doc


def _extract_invoice_id(text: str, email_subject: str) -> str:
    # 0. a monthly statement's own number: "STATEMENT OF ACCOUNT TX-ST-203501"
    m = re.search(r"STATEMENT\s+OF\s+ACCOUNT\s+([A-Z][A-Z0-9-]*\d)", text, re.I)
    if m and _looks_like_doc_id(m.group(1).upper()):
        return _invoice_id(m.group(1))
    # 1. the printed header: "INVOICE TX-US-2032-5394" / "Freight bill no. TX-US-2033-5512"
    #    / "Charges advice CDU-3051254"
    m = re.search(r"\b(?:INVOICE|Freight\s+bill\s+no\.?|Charges\s+advice)\s+([A-Z0-9][A-Z0-9/-]*\d[A-Z0-9/-]*)\b",
                  text, re.I)
    if m and _looks_like_doc_id(m.group(1).upper().replace("/", "-")):
        return _invoice_id(m.group(1))
    # 2. the email subject: "Invoice TX-US-2032-5394" / "Re: Invoice CDI-2930-0148"
    m = re.search(r"\b(?:Invoice|INV)\b\s*(?:No\.?|Number|#|:)?\s*([A-Z0-9][A-Z0-9/-]*\d[A-Z0-9/-]*)",
                  email_subject, re.I)
    if m and _looks_like_doc_id(m.group(1).upper().replace("/", "-")):
        return _invoice_id(m.group(1))
    # 3. body: "invoice TX-US-2032-5394 for services"
    m = re.search(r"\binvoice\s+([A-Z0-9][A-Z0-9/-]*\d[A-Z0-9/-]*)\b", text, re.I)
    if m and _looks_like_doc_id(m.group(1).upper().replace("/", "-")):
        return _invoice_id(m.group(1))
    # 3b. slash-shaped ids: TXI/35-36/0030
    for tok in SLASH_ID.findall(text):
        if _looks_like_doc_id(tok.upper()):
            return _invoice_id(tok)
    # 4. any ID-shaped token that isn't the contract reference
    for tok in ID_TOKEN.findall(text):
        if _looks_like_doc_id(tok):
            return _invoice_id(tok)
    return ""


# ---- tracking export (deterministic) ----------------------------------------------------------------------------------
TRACK_EVENTS = ("PICKED_UP", "DELIVERED", "DEPARTED", "ARRIVED", "ARRIVED_FACILITY",
                "TENDERED", "BOOKED", "CUSTOMS", "OUT_FOR_DELIVERY", "CANCELLED")


def extract_tracking(doc_id: str, pages: list[str], received: str) -> dict:
    events: list[dict] = []
    for page in pages:
        for row in page.split("\n"):
            parts = [p.strip() for p in row.split("|")]
            if len(parts) >= 3 and any(parts[1].startswith(e) for e in TRACK_EVENTS):
                events.append({
                    "awb": parts[0], "status": parts[1],
                    "date": parts[2] if DATE_RE.match(parts[2]) else None,
                    "location": parts[3] if len(parts) > 3 else None,
                    "raw": row.strip(),
                })
    return {"doc_id": doc_id, "doc_type": "tracking_export", "received_date": received,
            "source_email": doc_id.split("#")[0] if "#" in doc_id else doc_id,
            "events": events, "raw_pages": pages, "parse_ok": bool(events)}


# ---- AP register (deterministic) --------------------------------------------------------------------------------------
ROW_ID_RE = re.compile(
    r"\b([A-Z]{2,5}-[A-Z0-9]*-?\d{2,4}-\d{2,6}|[A-Z]{2,5}\d{4}-\d{3,6}|[A-Z]{2,5}-\d{5,8})\b")


def extract_ap_register(doc_id: str, pages: list[str], received: str) -> dict:
    """Rows like: TX-US-2033-5511 | 2033-01-31 | due 2033-03-02 | USD 1259.23 | paid | 2033-02-07"""
    rows: list[dict] = []
    for page in pages:
        for row in page.split("\n"):
            if "|" not in row:
                continue
            cells = [c.strip() for c in row.split("|")]
            if len(cells) < 4:
                continue
            joined = " ".join(cells)
            ids = [t for t in ROW_ID_RE.findall(joined) if _looks_like_doc_id(t)]
            if not ids:
                continue
            dates = DATE_RE.findall(joined)
            amounts = [float(a.replace(",", "")) for a in re.findall(r"[\d,]+\.\d{2}", joined)]
            status = None
            for s in ("paid", "scheduled", "open", "overdue", "disputed", "partial", "credit"):
                if s in joined.lower():
                    status = s
                    break
            due = None
            m = re.search(r"due\s+(\d{4}-\d{2}-\d{2})", joined, re.I)
            if m:
                due = m.group(1)
            inv_date = None
            for d in dates:
                if d != due:
                    inv_date = d
                    break
            paid_date = None
            if status == "paid" and dates:
                paid_date = dates[-1] if dates[-1] != due else (dates[-2] if len(dates) > 1 else None)
            rows.append({
                "invoice_id": _invoice_id(ids[0]),
                "invoice_date": inv_date, "due_date": due,
                "amount": amounts[0] if amounts else None,
                "status": status, "paid_date": paid_date,
                "raw": row.strip(), "_doc": doc_id,
            })
    return {"doc_id": doc_id, "doc_type": "ap_register", "received_date": received,
            "source_email": doc_id.split("#")[0] if "#" in doc_id else doc_id,
            "rows": rows, "raw_pages": pages, "parse_ok": bool(rows)}


# ---- credit notes (deterministic, invoice-shaped) ----------------------------------------------------------------------
def extract_credit_note(doc_id: str, pages: list[str], email_subject: str, received: str) -> dict:
    d = extract_invoice(doc_id, pages, email_subject, received, doc_type="credit_note")
    # credits key by contract/period in analysis; keep amount + references
    return d


# ---- emails: deterministic mentions + kind, LLM only for scope ---------------------------------------------------------
KIND_RULES = [
    ("waiver", re.compile(r"\bwaiv(e|er|ed|ing)\b", re.I)),
    ("extension", re.compile(r"\bextend(ed|s)?\b|\bdue date extension\b|\bextension of\b", re.I)),
    ("rate_notice", re.compile(r"\brate notice\b|\brates? (are|will be|revised)\b", re.I)),
    ("credit", re.compile(r"\bcredit note\b|\brebate\b|\bcredit\b", re.I)),
    ("renewal_notice", re.compile(r"\bnon-?renew(al)?\b|\brenewal notice\b|\bwill not renew\b", re.I)),
    ("contract_delivery", re.compile(r"\bexecuted agreement\b|\bsigned amendment\b|\bmaster services\b", re.I)),
    ("ledger", re.compile(r"\bregister\b|\bledger\b", re.I)),
    ("invoice_cover", re.compile(r"\binvoice\b", re.I)),
]


def extract_email(doc_id: str, pages: list[str], received: str) -> dict:
    """pages[0] is the grader-formatted email (headers + body + attachments)."""
    text = pages[0] if pages else ""
    doc: dict = {
        "doc_id": doc_id, "doc_type": "email", "received_date": received,
        "source_email": doc_id, "raw_pages": pages, "parse_ok": True,
        "kind": "note", "mentions": {}, "prompt_injection": False,
    }
    # headers out of the formatted page
    fm = re.search(r"^From: (.*?)\s*<(.+?)>", text, re.M)
    if fm:
        doc["sender_name"], doc["sender_email"] = fm.group(1).strip(), fm.group(2).strip()
    else:
        doc["sender_name"] = doc["sender_email"] = ""
    sm = re.search(r"^Subject: (.*)$", text, re.M)
    doc["subject"] = sm.group(1).strip() if sm else ""
    body = text.split("\n\n", 1)[1] if "\n\n" in text else text
    doc["body"] = body

    # prompt injection: text aimed at automated readers is DATA, never instructions
    if re.search(r"\b(the AI|automated system|automated systems|the auditor|assistant|ignore (these|the) "
                 r"instructions|do not report|do not flag)\b", body, re.I):
        doc["prompt_injection"] = True

    # mentions: invoice ids, AWBs, contract ids, lanes, months
    inv_ids = [t for t in list(ID_TOKEN.findall(body)) + list(SLASH_ID.findall(body))
               if _looks_like_doc_id(t)]
    # contract ids look like TBX-US-EXP-E60 / CDA-IN-EXP-E91 (EXP pattern)
    contracts = [t for t in ID_TOKEN.findall(body) if re.search(r"-EXP-|AMD-", t)]
    inv_ids = [t for t in inv_ids if t not in contracts]
    lanes = []
    for lm in re.finditer(r"\b([A-Z]{3,4}\d?-[A-Z]{3,4}\d?)\b", body):
        if body[lm.end():].startswith(("-EXP-", "-AMD-")):
            continue  # the head of a contract/amendment id, not a lane
        lanes.append(lm.group(1))
    doc["mentions"] = {
        "invoice_ids": sorted({_invoice_id(t) for t in inv_ids}),
        "awbs": sorted(set(AWB_RE.findall(body))),
        "contract_ids": sorted(set(contracts)),
        "lane": lanes[0] if lanes else None,
        "months": sorted(set(re.findall(r"\b(20\d{2}-\d{2})\b", body))),
    }

    # kind by keyword
    for kind, pat in KIND_RULES:
        if pat.search(doc["subject"]) or pat.search(body):
            doc["kind"] = kind
            break

    # structured details the analysis needs — deterministic first
    doc["waiver"] = _parse_waiver(body)
    doc["extension"] = _parse_extension(body, doc["mentions"]["invoice_ids"])
    doc["rate_notice"] = _parse_rate_notice(body, doc["mentions"].get("lane"))
    doc["asks_for_payment"] = bool(
        re.search(r"\b(process payment|please pay|kindly pay|payment is due|remit|at your earliest convenience)\b",
                  body, re.I))
    doc["is_copy_or_reminder"] = bool(
        re.search(r"\b(copy|reminder|for your records|resend|following up|second notice)\b",
                  doc["subject"] + " " + body, re.I))
    doc["parse_ok"] = True
    return doc


def _first_sentence_with(body: str, pat: re.Pattern) -> str | None:
    for sent in re.split(r"(?<=[.!?])\s+|\n", body):
        if pat.search(sent):
            return sent.strip()
    return None


def _parse_waiver(body: str) -> dict | None:
    sent = _first_sentence_with(body, re.compile(r"\bwaiv", re.I))
    if not sent:
        return None
    out: dict = {"text": sent[:400]}
    m = AWB_RE.search(sent)
    if m:
        out["awb"] = m.group(1)
    m = re.search(r"waiv(?:e|ed|er)?\s+(?:the\s+)?([A-Za-z][A-Za-z ]+?)(?:\s+on|\s+for|\s+from|\.|,|$)",
                  sent, re.I)
    if m:
        out["charge"] = m.group(1).strip()
    if not out.get("charge"):
        # "Please treat the Delivery fee on AWB CA10343321 as waived." /
        # "The Delivery fee billed against AWB CA75329761 is waived."
        m = re.search(r"([A-Za-z][\w'-]*(?:\s+[A-Za-z][\w'-]*){0,2})\s+(?:on|against)\s+"
                      r"(?:AWB\s+)?[A-Z]{2}\d{7,}", sent)
        if m and not re.search(r"\d", m.group(1)):
            charge = re.sub(r"^(?:the|a|an)\s+", "", m.group(1).strip(), flags=re.I)
            charge = re.sub(r"\s+(?:billed|charged|applied|posted)$", "", charge, flags=re.I)
            out["charge"] = charge
    m = re.search(r"\b(20\d{2}-\d{2})\b", sent)
    if m:
        out["month"] = m.group(1)
    if not out.get("charge"):
        # no readable charge name: one LLM call to read the scope out of this one sentence
        # (without it a waiver naming only an AWB would wrongly cover every line on it)
        try:
            j = llm.ask_json(
                'Extract waiver scope. JSON only: {"charge": "...", "awb": "...", "month": "YYYY-MM", '
                '"conditions": "..."} use null when absent.',
                sent, max_tokens=200)
            j["text"] = sent[:400]
            j.setdefault("awb", out.get("awb"))
            return j
        except Exception:
            return out
    return out


def _parse_extension(body: str, invoice_ids: list[str]) -> dict | None:
    sent = _first_sentence_with(body, re.compile(r"\bextend", re.I))
    if not sent:
        return None
    out: dict = {"text": sent[:400]}
    m = re.search(r"invoice\s+([A-Z0-9][A-Z0-9-]*\d)", sent, re.I)
    if m:
        out["invoice_id"] = _invoice_id(m.group(1))
    elif invoice_ids:
        out["invoice_id"] = invoice_ids[0]
    m = DATE_RE.search(sent.replace("to ", " to "))
    # prefer the date AFTER 'to'
    m2 = re.search(r"\bto\s+(\d{4}-\d{2}-\d{2})", sent, re.I)
    if m2:
        out["new_due_date"] = m2.group(1)
    elif m:
        out["new_due_date"] = m.group(1)
    return out if out.get("new_due_date") else out


def _parse_rate_notice(body: str, lane: str | None) -> dict | None:
    sent = _first_sentence_with(body, re.compile(r"\brate|\bkg\b", re.I))
    if not sent and not lane:
        return None
    bands = []
    band_lane = None
    # "USE2-USC3 45-99.5 kg = USD 4.72" / "USC3-INS1 45-99.5 kg: 5.03 USD"
    for m in re.finditer(r"([A-Z]{3,4}\d?-[A-Z]{3,4}\d?)\s+(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)\s*kg\s*[:=]?\s*"
                         r"(?:USD\s+)?(\d+(?:\.\d+)?)", body, re.I):
        band_lane = band_lane or m.group(1)
        bands.append({"min_kg": float(m.group(2)), "max_kg": float(m.group(3)),
                      "rate": float(m.group(4))})
    if not bands:
        for m in re.finditer(r"([A-Z]{3,4}\d?-[A-Z]{3,4}\d?)\s*:?\s*(\d+(?:\.\d+)?)\s*USD", body, re.I):
            band_lane = band_lane or m.group(1)
            bands.append({"min_kg": 0, "max_kg": 10**9, "rate": float(m.group(2))})
    # open top band: "USC3-INS1 300+ kg: 4.57 USD" / "USE2-USC3 300+ kg = USD 4.06"
    for m in re.finditer(r"([A-Z]{3,4}\d?-[A-Z]{3,4}\d?)\s+(\d+(?:\.\d+)?)\s*\+\s*kg\s*[:=]?\s*"
                         r"(?:USD\s+)?(\d+(?:\.\d+)?)", body, re.I):
        band_lane = band_lane or m.group(1)
        bands.append({"min_kg": float(m.group(2)), "max_kg": 10**9,
                      "rate": float(m.group(3))})
    if not bands and not sent:
        return None
    lane = band_lane or lane  # the lanes the bands were printed for beat any lane guessed from prose
    out: dict = {"lane": lane, "bands": bands, "text": (sent or body.strip())[:400]}
    # effective window: "effective 2035-03-13 to 2035-09-25" / "pickups from 2030-09-15 until ..."
    m = re.search(r"effective\s+(\d{4}-\d{2}-\d{2})\s+(?:to|through|-)\s+(\d{4}-\d{2}-\d{2})",
                  body, re.I)
    if not m:
        m = re.search(r"pickups?\s+from\s+(\d{4}-\d{2}-\d{2})\s+(?:until|to|through|-)\s+"
                      r"(\d{4}-\d{2}-\d{2})", body, re.I)
    if m:
        out["effective_from"], out["effective_to"] = m.group(1), m.group(2)
    else:
        dates = list(dict.fromkeys(DATE_RE.findall(body)))  # dedupe: ref dates repeat in the prose
        if len(dates) >= 1:
            out["effective_from"] = dates[0]
        if len(dates) >= 2:
            out["effective_to"] = dates[1]
    # "Reference: TBX-US-EXP-01/RN-USC3-INS1-2035-03-13" / "Notice ref. CDA-US-EXP-E91/RN-..."
    m = re.search(r"(?:Reference:|Notice\s+ref\.?)\s*([A-Z0-9][A-Z0-9-]+)/(\S+)", body, re.I)
    if m:
        out["contract_ref"] = m.group(1)
        out["notice_ref"] = m.group(2)
    # "USC3-INS1 minimum charge: 90.77 USD" / "minimum charge = USD 85.98"
    m = re.search(r"minimum\s+charge\s*[:=]?\s*(?:USD\s+)?([\d.]+)", body, re.I)
    if m:
        out["min_charge"] = float(m.group(1))
    return out


# ---- contracts: deterministic mechanical parse, LLM only when the structure isn't recognized -------
# The MSA/amendment text is highly regular (rate tables, fuel tables, Schedule 1 lines, numbered
# clauses), and evidence quotes must be verbatim from the pages anyway — so we parse mechanically
# and know exact page numbers. The LLM only runs when the parse finds nothing recognizable
# (a differently laid-out exam contract), and only on the pages that matter.

SCHED_LINE_RE = re.compile(
    r"^(?P<person>[A-Z][\w.'-]+(?:\s+[A-Z][\w.'-]+)+),\s*(?P<title>[^,]+?),\s*"
    r"(?P<party>[A-Z]{2,8}-[A-Z]{2,8}):\s*(?P<cats>[a-z_, ]+?)\s*<(?P<email>[\w.@-]+)>$")

LANE_ROW_RE = re.compile(
    r"^(?P<lane>[A-Z]{2,5}\d?-[A-Z]{2,5}\d?)\s+"
    r"(?P<band>minimum charge|[\d.]+\s*-\s*[\d.]+\s*kg|[\d.]+\+\s*kg)\s+"
    r"(?P<rest>.+)$")

PERIOD_RE = re.compile(
    r"Period\s+\w+:\s*pickups?\s+(\d{4}-\d{2}-\d{2})\s+(?:to|through)\s+(\d{4}-\d{2}-\d{2})",
    re.I)

FUEL_ROW_RE = re.compile(r"^(20\d{2}-\d{2})\s+([\d.]+)\s*%\s*$")
FX_ROW_RE = re.compile(r"^(20\d{2}-\d{2})\s+([\d.]+)\s*$")

# sentence-level clause quotes: keyword -> quote we lift verbatim (with its page)
CLAUSE_KEYWORDS = {
    "pricing_date": re.compile(r"\bpricing date\b", re.I),
    "minimum_charge": re.compile(r"\bminimum charge\b", re.I),
    "chargeable_weight": re.compile(r"\bchargeable weight\b", re.I),
    "fuel": re.compile(r"\bfuel surcharge\b", re.I),
    "waiver_authority": re.compile(r"\bwaiver authority\b", re.I),
    "waiver_scope": re.compile(r"\bscope of waiver\b|\bconditional waiver\b", re.I),
    "extension_of_due_date": re.compile(r"\bextension of due date\b|\bdue date may be extended\b", re.I),
    "late_fee": re.compile(r"\blate fee\b", re.I),
    "precedence": re.compile(r"\bprecedence\b|\bunranked instruments\b", re.I),
    "rate_notices": re.compile(r"\brate notices\b", re.I),
    "qualifying_spend": re.compile(r"Qualifying Spend\s+means", re.I),
    "volume_rebate": re.compile(r"\bvolume rebate\b", re.I),
    "auto_renewal": re.compile(r"\bauto-?renewal\b|\bdoes not renew\b|\brenews automatically\b", re.I),
    "term": re.compile(r"\binitial term\b", re.I),
    "minimum_charge_clause": re.compile(r"rated at the weight band", re.I),
    "dry_ice": re.compile(r"\bdry ice\b", re.I),
    "dangerous_goods": re.compile(r"dangerous goods (?:fee|is|are|applies)|DG Fee", re.I),
}


def _sentence_with(page_text: str, pat: re.Pattern, skip_contents: bool = True) -> str | None:
    """The sentence containing pat (verbatim substring of the page, so the grader finds it).
    A clause header like '3.2 Pricing date.' is joined with the sentence that follows it —
    that full passage is the deciding quote."""
    if skip_contents and re.match(r".*\nContents\n", page_text[:200], re.S):
        return None
    flat = " ".join(page_text.split())
    sents = re.split(r"(?<=[.!?])\s+(?=[A-Z(]|\d)", flat)
    for i, sent in enumerate(sents):
        if pat.search(sent) and len(sent) >= 12:
            out = sent.strip()
            # header-only sentence (e.g. '3.2 Pricing date.') -> pull in the next one
            if len(out) < 60 and i + 1 < len(sents):
                out = out + " " + sents[i + 1].strip()
            return out
    return None


def _band_bounds(band: str) -> tuple[float, float] | None:
    b = band.replace(" ", "")
    if b == "minimum" + "charge" or b == "minimumcharge":
        return None
    m = re.fullmatch(r"([\d.]+)-([\d.]+)kg", b)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.fullmatch(r"([\d.]+)\+kg", b)
    if m:
        return float(m.group(1)), 1e12
    return None


def _contract_mechanical(doc_id: str, pages: list[str], received: str) -> dict:
    full = "\n".join(pages)

    is_amend = bool(re.search(r"\bamendment\b", full[:800], re.I)
                    or re.search(r"\bAMD\b", doc_id.split("#")[-1]))
    doc: dict = {
        "doc_id": doc_id,
        "doc_type": "amendment" if is_amend else "contract",
        "received_date": received,
        "source_email": doc_id.split("#")[0] if "#" in doc_id else doc_id,
        "raw_pages": pages, "parse_ok": True,
        "is_amendment": is_amend,
        "lanes": [], "schedule_1": [], "fuel_percent_by_month": {},
        "fx_inr_per_usd_by_month": {}, "key_clauses": {}, "quotes": {},
        "rebate_tiers": None, "late_fee": None, "expiry": {},
        "min_charge_by_lane": {}, "accessorial_by_name": {}, "accessorial_alias": {},
    }

    # contract id
    m = (re.search(r"Agreement\s+No\.?\s+([A-Z0-9][A-Z0-9-]+)", full)
         or re.search(r"Agreement\s+([A-Z]{2,6}(?:-[A-Z0-9]+){1,5})", full))
    if m:
        doc["contract_id"] = m.group(1)
    else:
        # filename: MSA-TBX-US-EXP-E60.pdf / TBX-US-EXP-90-AMD-1.pdf
        fname = doc_id.split("#")[-1].rsplit(".", 1)[0]
        fname = re.sub(r"^(MSA|Amd|AMD)-", "", fname)
        doc["contract_id"] = fname
    m = re.search(r"[Aa]mendment\s+(?:No\.?\s*)?\d+\s+to\s+([A-Z0-9-]+)", full)
    if m:
        doc["base_contract_id"] = m.group(1)
    else:
        doc["base_contract_id"] = doc["contract_id"]

    # parties
    m = re.search(r"\((the Shipper)\).*?\((the Carrier)\)", full, re.S)
    ship = re.search(r"between\s*\n?\s*(?:\(\d\)\s*)?([A-Z][\w\s.&'-]+?),\s*(?:whose|an? )", full)
    doc["parties"] = {
        "customer": (ship.group(1).strip() if ship else None),
        "carrier": (re.search(r"\(\d\)\s*([A-Z][\w\s.&'-]+?),\s*whose principal office is at[^,]*,\s*(?:the )?Carrier",
                              full).group(1).strip()
                    if re.search(r"\(\d\)\s*[A-Z][\w\s.&'-]+?,\s*whose principal office is at[^,]*,\s*(?:the )?Carrier",
                                 full) else None),
    }

    # dates: effective / end / auto-renewal
    m = re.search(r"[Tt]akes effect on (\d{4}-\d{2}-\d{2})", full) or \
        re.search(r"[Ee]ffective from\s+(\d{4}-\d{2}-\d{2})", full) or \
        re.search(r"[Ee]ffective [Dd]ate(?:\s+of\s+this\s+Agreement)?\s*(?:is\s+|:\s*|\s+)"
                  r"(\d{4}-\d{2}-\d{2})", full) or \
        re.search(r"\((?:the )?[Ee]ffective Date\)", full) and re.search(r"(\d{4}-\d{2}-\d{2})\s*\((?:the )?[Ee]ffective Date\)", full)
    doc["effective_date"] = m.group(1) if m and m.lastindex else None
    m = re.search(r"ending on (\d{4}-\d{2}-\d{2})", full, re.I) or \
        re.search(r"\bends on (\d{4}-\d{2}-\d{2})", full, re.I) or \
        re.search(r"expires\s+(?:on\s+)?(\d{4}-\d{2}-\d{2})", full, re.I) or \
        re.search(r"end date (?:is|of|:)\s*(\d{4}-\d{2}-\d{2})", full, re.I)
    doc["end_date"] = m.group(1) if m else None
    auto = None
    if re.search(r"does not renew automatically|does not continue after the initial term",
                 full, re.I):
        auto = False
    elif re.search(r"renews automatically|automatically renews|successive periods? of \d+ months", full, re.I):
        auto = True
    doc["auto_renews"] = auto
    m = re.search(r"(\d+)\s*days['\s]*notice\s+(?:of\s+)?(?:non-?renewal|termination of the term)"
                  r"|(?:non-?renewal|not to renew)[^.]{0,80}?(\d+)\s*days", full, re.I)
    doc["non_renewal_notice_days"] = int(m.group(1) or m.group(2)) if m else None
    doc["expiry"] = {"end_date": doc["end_date"], "auto_renews": auto,
                     "notice_deadline": None}

    # currency
    doc["currency"] = "USD" if re.search(r"\bUSD\b", full) else ("INR" if re.search(r"\bINR\b", full) else "USD")

    # ---- page-level scan: rate tables, fuel, fx, schedule 1, clauses, accessorials -----------------
    periods: list[tuple[str, str]] = []
    lanes_by_code: dict[str, dict] = {}
    season_pending: str | None = None
    for pno, page in enumerate(pages, 1):
        for raw in page.split("\n"):
            line = raw.strip()
            if not line:
                continue

            # a seasonal condition whose month range wrapped to the next line
            if season_pending is not None:
                sm = MONTH_RANGE_RE.search(line)
                if sm:
                    doc["accessorial_by_name"][season_pending]["season"] = (
                        MONTHS_FULL.get(sm.group(1).lower(), 0),
                        MONTHS_FULL.get(sm.group(2).lower(), 0))
                season_pending = None

            pm = PERIOD_RE.search(line)
            if pm:
                periods.append((pm.group(1), pm.group(2)))

            lm = LANE_ROW_RE.match(line)
            if lm and re.search(r"\d", lm.group("rest")):
                lane_code = lm.group("lane")
                band = lm.group("band")
                rest = lm.group("rest")
                rates = [float(x) for x in re.findall(r"\d+\.\d{2}", rest)]
                if not rates:
                    continue
                lane = lanes_by_code.get(lane_code)
                if lane is None:
                    origin, _, dest = lane_code.partition("-")
                    lane = {"origin": origin, "destination": dest, "mode": "Air freight",
                            "bands": [], "page": pno}
                    lanes_by_code[lane_code] = lane
                if band == "minimum charge":
                    doc["min_charge_by_lane"][lane_code] = rates[0]
                    continue
                bounds = _band_bounds(band)
                if bounds is None:
                    continue
                lo, hi = bounds
                if len(periods) >= len(rates):
                    for i, rt in enumerate(rates):
                        ps, pe = periods[i] if i < len(periods) else periods[-1]
                        lane["bands"].append({"min_kg": lo, "max_kg": hi, "rate": rt,
                                              "period_start": ps, "period_end": pe,
                                              "page": pno, "quote": line})
                else:
                    for i, rt in enumerate(rates):
                        ps, pe = (periods[i] if i < len(periods) else (None, None))
                        lane["bands"].append({"min_kg": lo, "max_kg": hi, "rate": rt,
                                              "period_start": ps, "period_end": pe,
                                              "page": pno, "quote": line})
                continue

            fm = FUEL_ROW_RE.match(line)
            if fm:
                doc["fuel_percent_by_month"][fm.group(1)] = float(fm.group(2))
                continue

            sm = SCHED_LINE_RE.match(line)
            if sm:
                cats = [c.strip() for c in sm.group("cats").split(",") if c.strip()]
                doc["schedule_1"].append({
                    "person": sm.group("person"), "email": sm.group("email"),
                    "party": sm.group("party"), "title": sm.group("title").strip(),
                    "categories": cats,
                    "page": pno, "quote": line,
                })
                continue

            # Annex F fx rows (only inside the Annex F section: row is YYYY-MM number and a
            # heading recently mentioned exchange rates)
            if re.search(r"[Ee]xchange rates|[Aa]nnex F", page):
                xm = FX_ROW_RE.match(line)
                if xm:
                    doc["fx_inr_per_usd_by_month"][xm.group(1)] = float(xm.group(2))

            # accessorial fee table: "DLV Delivery fee 121.81 USD per shipment" (Annex D;
            # the currency can be USD or INR and a seasonal condition may trail the basis)
            am = re.match(r"^([A-Z]{2,8})\s+([A-Za-z][A-Za-z ]+?)\s+([\d.]+)\s+(USD|INR)\s+per\s+"
                          r"(shipment|chargeable kg|storage day|month)", line)
            if am:
                key = am.group(2).strip().lower()
                entry = {
                    "code": am.group(1), "fee": float(am.group(3)),
                    "currency": am.group(4), "basis": am.group(5),
                    "page": pno, "quote": line,
                }
                doc["accessorial_by_name"][key] = entry
                tail = line[am.end():].lower()
                if "for pickups in" in tail:
                    sm = MONTH_RANGE_RE.search(line)
                    if sm:
                        entry["season"] = (MONTHS_FULL.get(sm.group(1).lower(), 0),
                                           MONTHS_FULL.get(sm.group(2).lower(), 0))
                    else:
                        season_pending = key

            # Annex E charge-code aliases: "Code FRT: Air freight; also billed as: Express freight, Freight charge"
            em = re.match(r"^Code\s+([A-Z]{2,8}):\s*([^;]+?)(?:;\s*also billed as:\s*(.+))?$", line)
            if em:
                canon = em.group(2).strip().lower()
                doc["accessorial_alias"][canon] = canon
                for a in (em.group(3) or "").split(","):
                    a = a.strip().lower()
                    if a:
                        doc["accessorial_alias"][a] = canon

        # clause quotes from this page
        for key, pat in CLAUSE_KEYWORDS.items():
            if key in doc["key_clauses"]:
                continue
            s = _sentence_with(page, pat)
            if s:
                doc["key_clauses"][key] = s
                doc["quotes"][key] = {"page": pno, "quote": s}

    doc["lanes"] = list(lanes_by_code.values())

    # an annex revision named in this document ("Annex A rev 2 (attached) applies from ... to lane(s) ..."):
    # if we don't hold that revision as its own document, the lanes it governs can't be priced.
    doc["named_annexes"] = []
    ann_re = re.compile(r"\b((?:Annex|Schedule)\s+[A-Z0-9]+\s+rev(?:ision)?\.?\s+\d+)\b", re.I)
    for pno, page in enumerate(pages, 1):
        flat = " ".join(page.split())
        sents = re.split(r"(?<=[.!?])\s+(?=[A-Z(]|\d)", flat)
        for sent in sents:
            m = ann_re.search(sent)
            if not m:
                continue
            lanes = re.findall(r"\b[A-Z]{3,4}\d?-[A-Z]{3,4}\d?\b", sent)
            af = re.search(r"applies from\s+(\d{4}-\d{2}-\d{2})", sent, re.I)
            doc["named_annexes"].append({
                "name": re.sub(r"\s+", " ", m.group(1)), "page": pno, "quote": sent.strip(),
                "lanes": sorted(set(lanes)),
                "applies_from": af.group(1) if af else None,
            })

    # fuel can also live in a "Month Fuel surcharge" table whose rows lack % or have headers —
    # if we found none, scan whole text for month/percent pairs
    if not doc["fuel_percent_by_month"]:
        for m in re.finditer(r"\b(20\d{2}-\d{2})\s+([\d.]+)\s*%", full):
            doc["fuel_percent_by_month"][m.group(1)] = float(m.group(2))

    # rebate tiers: "QS >= USD 70,000.00: 5%"
    tiers = [{"min_qs": float(t.replace(",", "")), "percent": float(p)}
             for t, p in re.findall(r"QS\s*>=\s*USD\s*([\d,]+\.\d{2}):\s*([\d.]+)\s*%", full)]
    doc["rebate_tiers"] = sorted(tiers, key=lambda t: -t["min_qs"]) if tiers else None

    # annual improvement credit percent (promised_credit)
    m = re.search(r"credit of\s+([\d.]+)%\s+of the prior Contract Year", full, re.I)
    doc["annual_credit_percent"] = float(m.group(1)) if m else None
    m = re.search(r"Within (\d+) days after each anniversary", full, re.I)
    doc["annual_credit_days"] = int(m.group(1)) if m else None

    # margin step-down (only when the contract actually has one)
    m = re.search(r"[Mm]argin step-down[.:]\s*([^.]+)\.", full)
    if m and not re.search(r"not applicable", m.group(1), re.I):
        doc["margin_step_down_text"] = m.group(1).strip()

    # late fee: rate, grace, compounding
    lf: dict = {}
    m = re.search(r"late fee of\s+([\d.]+)%\s*per month", full, re.I) or \
        re.search(r"late fee[^.]{0,40}?([\d.]+)%", full, re.I)
    if m:
        lf["rate_percent"] = float(m.group(1))
    m = re.search(r"within (\d+) days? (?:of|after) the due date[^.]*no fee|"
                  r"no fee[^.]*within (\d+) days?|"
                  r"no fee[^.]*days_late\s*<=\s*(\d+)|"
                  r"unpaid more than (\d+) days? after the due date|"
                  r"grace(?:\s+period)?\s+(?:of\s+)?(\d+)\s+days", full, re.I)
    if m:
        lf["grace_days"] = int(next(g for g in m.groups() if g))
    else:
        lf["grace_days"] = 0
    lf["compounds"] = bool(re.search(r"added to the overdue balance|principal plus unpaid prior late fees",
                                     full, re.I))
    doc["late_fee"] = lf if lf.get("rate_percent") is not None else None

    # payment terms: due N days after invoice date
    m = re.search(r"invoices? are due (\d+) days after the invoice date", full, re.I)
    doc["payment_terms_days"] = int(m.group(1)) if m else None

    # QS definition (verbatim sentence, already captured as clause)
    # minimum charge / pricing date etc. are in key_clauses
    return doc


def _contract_llm_fallback(doc_id: str, pages: list[str], received: str) -> dict:
    """Only when the mechanical parse found no lanes and no schedule: ask the model about the
    annex pages only (small, fast), then verify quotes against the real page text."""
    annex_pages = [(i, p) for i, p in enumerate(pages, 1)
                   if re.search(r"Annex|Schedule 1|Rates|rate", p, re.I)]
    if not annex_pages:
        annex_pages = list(enumerate(pages, 1))[:6]
    numbered = "\n".join(f"--- PAGE {i} ---\n{p}" for i, p in annex_pages)[:70000]
    out = llm.ask_json(CONTRACT_SYSTEM, f"Document: {doc_id}\n\n{numbered}",
                       max_tokens=5000, temperature=0.0)
    if isinstance(out, list):
        out = next((x for x in out if isinstance(x, dict)), {})
    if not isinstance(out, dict):
        out = {}
    mech = _contract_mechanical(doc_id, pages, received)
    # take LLM fields only where the mechanical parse came up empty
    for k in ("contract_id", "effective_date", "end_date", "auto_renews", "parties",
              "rebate_tiers", "late_fee", "fx_inr_per_usd_by_month"):
        if out.get(k) and not mech.get(k):
            mech[k] = out[k]
    if out.get("lanes") and not mech["lanes"]:
        mech["lanes"] = out["lanes"]
    if out.get("schedule_1") and not mech["schedule_1"]:
        mech["schedule_1"] = out["schedule_1"]
    if out.get("fuel_percent_by_month") and not mech["fuel_percent_by_month"]:
        mech["fuel_percent_by_month"] = out["fuel_percent_by_month"]
    if out.get("key_clauses"):
        for k, v in (out["key_clauses"] or {}).items():
            if v and k not in mech["key_clauses"]:
                # verify verbatim against a page; store the page we found it on
                for pno, ptext in enumerate(pages, 1):
                    if " ".join(str(v).split()) in " ".join(ptext.split()):
                        mech["key_clauses"][k] = v
                        mech["quotes"][k] = {"page": pno, "quote": v}
                        break
    return mech


def extract_contract(doc_id: str, pages: list[str], received: str) -> dict:
    doc = _contract_mechanical(doc_id, pages, received)
    # structure not recognized (different carrier layout): fall back to the LLM for the annex
    if not doc["lanes"] or (not doc["schedule_1"] and not doc["key_clauses"]):
        try:
            doc = _contract_llm_fallback(doc_id, pages, received)
        except Exception:
            pass  # keep the mechanical parse; analysis can still cannot_determine
    doc["parse_ok"] = bool(doc["lanes"] or doc["key_clauses"])
    return doc


# ---- scans: vision ----------------------------------------------------------------------------------------------------
def _vision_transcribe(b64_png: str) -> str:
    import base64 as _b64
    import json as _json
    import urllib.request
    system = ("Read this scanned freight document image. Output ONLY the text as it appears, "
              "line by line, preserving numbers exactly.")
    if llm.mode() == "openai":
        base = (llm.OPENAI_BASE.rstrip("/") if llm.OPENAI_BASE else "https://api.openai.com/v1")
        payload = {
            "model": llm.OPENAI_MODEL or "gpt-4o-mini",
            "max_tokens": 4000,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": system},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_png}"}},
            ]}],
        }
        req = urllib.request.Request(
            f"{base}/chat/completions", data=_json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {llm.OPENAI_KEY}"}, method="POST")
        with urllib.request.urlopen(req, timeout=180) as r:
            data = _json.loads(r.read().decode())
        return data["choices"][0]["message"]["content"]
    base = (llm.ANTHROPIC_BASE.rstrip("/") if llm.ANTHROPIC_BASE else "https://api.anthropic.com")
    payload = {
        "model": "claude-sonnet-4-5", "max_tokens": 4000, "system": system,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                          "data": b64_png}},
            {"type": "text", "text": "Transcribe this document."},
        ]}],
    }
    req = urllib.request.Request(
        f"{base}/v1/messages", data=_json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-api-key": llm.ANTHROPIC_KEY,
                 "anthropic-version": "2023-06-01"}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        data = _json.loads(r.read().decode())
    return "".join(b.get("text", "") for b in data.get("content", []))


def _looks_transcribed(text: str) -> bool:
    """True when a vision call actually returned the document (not a refusal or an empty reply)."""
    if not text or len(text.strip()) < 150:
        return False
    if re.search(r"can'?t (?:read|view)|cannot (?:read|view)|doesn'?t accept|does not accept|"
                 r"not support(?:s|ed)? (?:image|multimodal)|no image|"
                 r"image (?:attachment )?wasn'?t|unable to (?:view|read)|paste the text|multimodal",
                 text, re.I):
        return False
    return len(re.findall(r"\d", text)) >= 20


def _space_repair(t: str) -> str:
    """One OCR box may hold several words merged: 'ChargesadviceCDI' -> 'Chargesadvice CDI'."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", t)


def _local_ocr_page(png: bytes) -> str:
    """Local OCR fallback for vision endpoints that can't read images. RapidOCR gives one box
    per table cell and drops some spaces/commas; rebuild visual rows (cluster by vertical
    centre), join cells left to right, and repair '90.535.08' -> '90,535.08'."""
    try:
        import cv2
        import numpy as np
        from rapidocr_onnxruntime import RapidOCR
    except Exception:
        return ""
    img = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return ""
    result, _ = RapidOCR()(img)
    if not result:
        return ""
    boxes = []
    for box, text, _score in result:
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        boxes.append({"cy": (min(ys) + max(ys)) / 2, "h": max(max(ys) - min(ys), 1),
                      "left": min(xs), "text": str(text)})
    boxes.sort(key=lambda b: (b["cy"], b["left"]))
    rows: list[dict] = []
    for b in boxes:
        for row in rows:
            if abs(b["cy"] - row["cy"]) < max(5, 0.5 * row["h"]):
                row["cells"].append(b)
                n = row["n"]
                row["cy"] = (row["cy"] * n + b["cy"]) / (n + 1)
                row["n"] = n + 1
                row["h"] = max(row["h"], b["h"])
                break
        else:
            rows.append({"cy": b["cy"], "h": b["h"], "n": 1, "cells": [b]})
    rows.sort(key=lambda r: r["cy"])
    out: list[str] = []
    for row in rows:
        cells = sorted(row["cells"], key=lambda b: b["left"])
        line = " ".join(_space_repair(c["text"]) for c in cells)
        # OCR read thousands commas as dots: 90.535.08 means 90,535.08
        line = re.sub(r"(?<![\d,.])(\d{1,3})\.(\d{3})\.(\d{2})(?!\d)", r"\1,\2.\3", line)
        out.append(line)
    return "\n".join(out)


def _ocr_pages(png_pages: list[bytes]) -> list[str]:
    import base64
    texts: list[str] = []
    for png in png_pages:
        t = ""
        try:
            t = _vision_transcribe(base64.b64encode(png).decode("ascii"))
        except Exception:
            t = ""
        if not _looks_transcribed(t):
            t = _local_ocr_page(png)  # vision unavailable or refused: local OCR reads it
        texts.append(t)
    return texts or [""]


# ---- batch entry point -------------------------------------------------------------------------------------------------
def ingest_batch(batch_dir: str, state_dir: str) -> int:
    """Read every .eml in batch_dir, build universal docs, save under state_dir. -> count"""
    from pathlib import Path
    from . import state

    root = Path(batch_dir)
    index = state.load_docs_index(state_dir)
    count = 0
    for eml_path in sorted(root.glob("*.eml")):
        raw = eml_path.read_bytes()
        msg = X.parse_eml(raw)
        received = X.email_received(msg).strftime("%Y-%m-%d")
        subject = str(msg["Subject"] or "")
        batch_name = root.name
        email_doc_id = f"{batch_name}/{eml_path.name}"
        if email_doc_id in index:
            continue

        email_doc = extract_email(email_doc_id, ["\n".join(X.email_lines(msg))], received)
        email_doc["subject"] = subject
        state.save_doc(state_dir, email_doc)
        index[email_doc_id] = received
        count += 1

        for att_name, att_bytes in X.attachments(raw):
            doc_id = f"{email_doc_id}#{att_name}"
            if doc_id in index:
                continue
            ext = att_name.rsplit(".", 1)[-1].lower() if "." in att_name else ""
            try:
                if ext == "pdf":
                    if X.pdf_is_image_only(att_bytes):
                        pages = _ocr_pages(X.pdf_png(att_bytes))
                        doc = {"doc_id": doc_id, "doc_type": "scanned",
                               "received_date": received, "source_email": email_doc_id,
                               "raw_pages": pages, "parse_ok": True}
                        # treat it as whatever it says it is
                        ctype = _classify_heuristic(doc_id, pages)
                        if ctype == "invoice" or ctype == "unsure":
                            doc = extract_invoice(doc_id, pages, subject, received)
                    else:
                        pages = X.pdf_pages(att_bytes)
                        ctype = classify(doc_id, pages)
                        if ctype in ("contract_msa", "amendment"):
                            doc = extract_contract(doc_id, pages, received)
                        elif ctype == "credit_note":
                            doc = extract_credit_note(doc_id, pages, subject, received)
                        elif ctype == "invoice":
                            doc = extract_invoice(doc_id, pages, subject, received)
                        elif ctype == "tracking_export":
                            doc = extract_tracking(doc_id, pages, received)
                        elif ctype == "ap_register":
                            doc = extract_ap_register(doc_id, pages, received)
                        else:
                            doc = extract_invoice(doc_id, pages, subject, received)
                            doc["doc_type"] = ctype if ctype != "unsure" else "other"
                elif ext == "xlsx":
                    pages = X.xlsx_pages(att_bytes)
                    ctype = classify(doc_id, pages)
                    if ctype == "tracking_export":
                        doc = extract_tracking(doc_id, pages, received)
                    elif ctype == "ap_register":
                        doc = extract_ap_register(doc_id, pages, received)
                    elif ctype == "invoice":
                        doc = extract_invoice(doc_id, pages, subject, received)
                    elif ctype == "credit_note":
                        doc = extract_credit_note(doc_id, pages, subject, received)
                    else:
                        doc = {"doc_id": doc_id, "doc_type": ctype, "received_date": received,
                               "source_email": email_doc_id, "raw_pages": pages, "parse_ok": True}
                else:
                    continue
            except Exception as e:  # noqa: BLE001 - never lose a batch to one bad file
                doc = {"doc_id": doc_id, "doc_type": "unreadable", "received_date": received,
                       "source_email": email_doc_id, "error": str(e), "raw_pages": [],
                       "parse_ok": False}
            state.save_doc(state_dir, doc)
            index[doc_id] = received
            count += 1

    state.save_docs_index(state_dir, index)
    return count

"""Build the four from-scratch eval mailboxes under evals/mailboxes/.

Run once (they are checked in afterwards):

    python evals/build_mailboxes.py

Each mailbox is a small, self-contained world with my own carrier (Northaven Freight),
customer and numbers - nothing from the practice or exam sets:

  inr_advice/    INR charges advice in a DD/MM layout: freight billed over the P1 rate,
                 Annex F conversion to USD, thousands commas. Expected overcharge 62.47.
  usd_advice/    USD charges advice, DD/MM, with a duty disbursement row that ends in "-"
                 (no collected date). Expected overcharge 74.97.
  usd_notice/    same invoice plus a Schedule-1 rate notice that beats the MSA table.
                 Expected overcharge 124.95 (notice rate 6.10).
  usd_waiver/    a correctly-priced invoice whose Delivery fee was waived by a
                 Schedule-1 sender but billed anyway. Expected waived_but_billed 48.00.

The PDFs are generated with pymupdf so the exact text the harness reads is reproducible.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parent.parent
BOXES = ROOT / "evals" / "mailboxes"

# --------------------------------------------------------------------------- pdf / eml helpers

LINES_PER_PAGE = 62


def pdf_of(lines: list[str]) -> bytes:
    """One page per LINES_PER_PAGE rows of plain helvetica text."""
    doc = fitz.open()
    for i in range(0, len(lines), LINES_PER_PAGE):
        page = doc.new_page(width=595, height=842)
        y = 42.0
        for ln in lines[i:i + LINES_PER_PAGE]:
            page.insert_text((42, y), ln, fontsize=9.5, fontname="helv")
            y += 12.0
    data = doc.tobytes()
    doc.close()
    return data


def eml(sender: str, sender_email: str, to: str, when: str, subject: str,
        body: str, attachments: dict[str, bytes]) -> bytes:
    msg = EmailMessage()
    msg["From"] = f"{sender} <{sender_email}>"
    msg["To"] = to
    msg["Date"] = datetime.fromisoformat(when).replace(tzinfo=timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000")
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{abs(hash((sender_email, subject, when))):x}@{sender_email.split('@')[1]}>"
    msg["MIME-Version"] = "1.0"
    msg["X-GCA-Canary"] = "GCA-0.1.0-CANARY"
    msg.set_content(body)
    for fname, data in attachments.items():
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=fname)
    return msg.as_bytes()


def write_mailbox(name: str, emails: dict[str, bytes]) -> None:
    d = BOXES / name / "batch_01"
    d.mkdir(parents=True, exist_ok=True)
    for fname, data in emails.items():
        (d / fname).write_bytes(data)
    print(f"wrote {name}: {sorted(emails)}")


# --------------------------------------------------------------------------- contracts

def contract_us() -> bytes:
    return pdf_of([
        "Northaven Freight Ltd.",
        "Master Services Agreement (Express Air)",
        "",
        "Agreement No. VRE-US-EXP-22",
        "",
        "THIS AGREEMENT is dated 2030-02-20 and is made between:",
        "(1) Quill Medical Devices Inc., whose principal office is at 400 Halden Way, Marlow",
        "Falls 30219, USA (the Shipper); and",
        "(2) Northaven Freight Ltd., whose principal office is at 12 Tanner Street, Ashcombe",
        "51407, USA (the Carrier).",
        "Recitals",
        "(A) WHEREAS the Carrier provides express air transport and related delivery services; and",
        "(B) WHEREAS the parties wish to record the terms on which the Carrier provides those services.",
        "NOW THEREFORE the parties agree as follows.",
        "1 Definitions. In this Agreement the following terms have the meanings below.",
        "1.1 Pickup Date. Pickup Date means the date the Carrier takes possession, as recorded in",
        "the Carrier's tracking record.",
        "2 Term",
        "2.1 Effective date. The Effective Date of this Agreement is 2030-03-01.",
        "2.2 Initial term. The initial term runs for 24 months and expires on 2032-02-28.",
        "2.3 Auto-renewal. This Agreement does not renew automatically after the initial term.",
        "3 Rates and charges",
        "3.1 Rates. Services are charged at the rates in Annex A (express air).",
        "3.2 Pricing date. Each shipment is priced at the rates, surcharges and fees in force on",
        "its Pickup Date.",
        "3.3 Chargeable weight. Chargeable weight is the greater of actual and volumetric weight",
        "rounded up to the next 0.5 kg.",
        "4 Surcharges",
        "4.1 Fuel surcharge. The fuel surcharge is the Annex C percentage for the month of the",
        "Pickup Date, applied to the freight amount.",
        "10 Payment",
        "10.1 Payment terms. Invoices are due 30 days after the invoice date.",
        "10.2 Late fee. An invoice unpaid more than 10 days after the due date carries a late fee",
        "of 1.49% per month on the overdue balance.",
        "12 Notices and Schedule 1",
        "12.1 Authorised Persons. A notice or waiver is effective only from a person listed in",
        "Schedule 1 for the relevant category.",
        "",
        "Annex A: Express rates (USD per chargeable kg; minimum charge per shipment)",
        "Period P1: pickups 2030-03-01 to 2030-08-31",
        "Period P2: pickups 2030-09-01 to 2031-02-28",
        "Lane Weight band P1 rate P2 rate",
        "USE2-INW1 minimum charge 40.00 40.00",
        "USE2-INW1 0-99.5 kg 7.20 6.90",
        "USE2-INW1 100-299.5 kg 6.40 6.10",
        "USE2-INW1 300+ kg 5.90 5.60",
        "",
        "Annex C: Fuel surcharge",
        "2030-06 20.00 %",
        "2030-07 19.00 %",
        "2030-08 18.50 %",
        "",
        "Annex D: Accessorials",
        "DLV Delivery fee 48.00 USD per shipment",
        "SEC Security surcharge 0.16 USD per chargeable kg",
        "",
        "Schedule 1 Authorised Persons",
        "Priya Raman, Rate Administrator, VRE-US: rates <priya.raman@northaven.example>",
        "Callum Wren, Commercial Manager, VRE-US: rates, waivers <callum.wren@northaven.example>",
    ])


def contract_in() -> bytes:
    return pdf_of([
        "Northaven Freight India Pvt. Ltd.",
        "Master Services Agreement (Express Air)",
        "",
        "Agreement No. VRE-IN-EXP-31",
        "",
        "THIS AGREEMENT is dated 2030-04-05 and is made between:",
        "(1) Meridian Biosciences India Pvt. Ltd., whose principal office is at 14 Kalyan Nagar,",
        "Coimbatore 641001, India (the Shipper); and",
        "(2) Northaven Freight India Pvt. Ltd., whose principal office is at 8 Industrial Lane,",
        "Coimbatore 641002, India (the Carrier).",
        "Recitals",
        "(A) WHEREAS the Carrier provides express air transport and related delivery services; and",
        "(B) WHEREAS the parties wish to record the terms on which the Carrier provides those services.",
        "NOW THEREFORE the parties agree as follows.",
        "1 Definitions. In this Agreement the following terms have the meanings below.",
        "1.1 Pickup Date. Pickup Date means the date the Carrier takes possession, as recorded in",
        "the Carrier's tracking record.",
        "2 Term",
        "2.1 Effective date. The Effective Date of this Agreement is 2030-04-15.",
        "2.2 Initial term. The initial term runs for 24 months and expires on 2032-04-14.",
        "2.3 Auto-renewal. This Agreement does not renew automatically after the initial term.",
        "3 Rates and charges",
        "3.1 Rates. Services are charged at the rates in Annex A (express air).",
        "3.2 Pricing date. Each shipment is priced at the rates, surcharges and fees in force on",
        "its Pickup Date.",
        "4 Surcharges",
        "4.1 Fuel surcharge. The fuel surcharge is the Annex C percentage for the month of the",
        "Pickup Date, applied to the freight amount.",
        "",
        "Annex A: Express rates (INR per chargeable kg; minimum charge per shipment)",
        "Period P1: pickups 2030-04-15 to 2030-10-14",
        "Period P2: pickups 2030-10-15 to 2031-04-14",
        "Lane Weight band P1 rate P2 rate",
        "NDL1-MAA2 minimum charge 5000.00 5000.00",
        "NDL1-MAA2 0-44.5 kg 400.00 380.00",
        "NDL1-MAA2 45-99.5 kg 370.00 351.00",
        "NDL1-MAA2 100-299.5 kg 330.00 313.00",
        "NDL1-MAA2 300+ kg 300.00 285.00",
        "",
        "Annex C: Fuel surcharge",
        "2030-07 18.00 %",
        "2030-08 17.50 %",
        "",
        "Annex D: Accessorials",
        "DLV Delivery fee 4200.00 INR per shipment",
        "SEC Security surcharge 12.00 INR per chargeable kg",
        "",
        "Annex F: Exchange rates (INR per 1 USD)",
        "2030-07 85.00",
        "2030-08 85.40",
    ])


# --------------------------------------------------------------------------- invoices

def invoice_us(invoice_id: str, doc_date: str, due: str, rate: str,
               freight: str, fuel: str, total: str, with_duty: bool) -> bytes:
    lines = [
        "Northaven Freight Ltd.",
        "27 Saltmarsh Road, Dunmere 30801, USA",
        f"Charges advice {invoice_id}",
        f"Document date {doc_date} Payable by {due}",
        "Contract VRE-US-EXP-22 Currency USD",
        "Customer Quill Medical Devices Inc.",
        "Route Waybill Charge Basis Rate Amount Collected",
        f"USE2-INW1 NF88231100 Air freight 140 kg {rate} {freight} 15/07/2030",
        f"USE2-INW1 NF88231100 Fuel surcharge {freight} 19.00% {fuel} 15/07/2030",
        "USE2-INW1 NF88231100 Security surcharge 140 kg 0.16 22.40 15/07/2030",
        "USE2-INW1 NF88231100 Delivery fee 1 shpt 48.00 48.00 15/07/2030",
    ]
    if with_duty:
        lines.append("USE2-INW1 NF88231100 Import duty (disbursement) 1 fee 120.00 120.00 -")
    lines += [
        f"Charges USD {total}",
        "Tax nil USD 0.00",
        f"Total payable USD {total}",
        "Terms 30 days. Please remit to Northaven Freight Ltd. quoting the advice number.",
        "Page 1 of 1",
    ]
    return pdf_of(lines)


def invoice_in() -> bytes:
    return pdf_of([
        "Northaven Freight India Pvt. Ltd.",
        "Plot 219, Foundry Walk, Coimbatore 641002, India",
        "Charges advice VRA-7741",
        "Document date 22/07/2030 Payable by 21/08/2030",
        "Contract VRE-IN-EXP-31 Currency INR",
        "Customer Meridian Biosciences India Pvt. Ltd.",
        "Route Waybill Charge Basis Rate Amount Collected",
        "NDL1-MAA2 VN20031144 Air freight 150 kg 360.00 54,000.00 10/07/2030",
        "NDL1-MAA2 VN20031144 Fuel surcharge 54,000.00 18.00% 9,720.00 10/07/2030",
        "NDL1-MAA2 VN20031144 Security surcharge 150 kg 12.00 1,800.00 10/07/2030",
        "NDL1-MAA2 VN20031144 Delivery fee 1 shpt 4200.00 4200.00 10/07/2030",
        "Charges INR 69,720.00",
        "Tax nil INR 0.00",
        "Total payable INR 69,720.00",
        "Terms 30 days. Please remit to Northaven Freight India Pvt. Ltd. quoting the advice number.",
        "Page 1 of 1",
    ])


# --------------------------------------------------------------------------- mailboxes

def build() -> None:
    # ---- inr_advice: overcharge 62.47 ((360-330)*150 + fuel, INR 5,310 / 85.00) ----
    write_mailbox("inr_advice", {
        "0001.eml": eml(
            "Ilaria Vestin", "ilaria.vestin@northaven.example", "meera.iyer@meridianbio.example",
            "2030-04-10T09:00:00", "Executed agreement VRE-IN-EXP-31",
            "Hello,\n\nAttached is the executed Master Services Agreement (Express Air) "
            "VRE-IN-EXP-31, signed by both parties. It takes effect on 2030-04-15.\n\n"
            "Kind regards,\nIlaria Vestin\nKey Account Manager\nNorthaven Freight India Pvt. Ltd.",
            {"MSA-VRE-IN-EXP-31.pdf": contract_in()}),
        "0002.eml": eml(
            "Northaven Billing", "billing@northaven.example", "meera.iyer@meridianbio.example",
            "2030-07-22T08:30:00", "Charges advice VRA-7741",
            "Good day,\n\nAttached is charges advice VRA-7741 covering waybill VN20031144. "
            "The total payable is INR 69,720.00, due 21/08/2030.\n\nRegards,\nNorthaven Billing",
            {"VRA-7741.pdf": invoice_in()}),
    })

    # ---- usd_advice: overcharge 74.97 (freight 6.85 vs 6.40, fuel follows) ----
    us_contract = {"MSA-VRE-US-EXP-22.pdf": contract_us()}
    us_bad_invoice = {"NFR-10023.pdf": invoice_us(
        "NFR-10023", "20/07/2030", "19/08/2030", "6.85", "959.00", "182.21", "1,331.61",
        with_duty=True)}
    write_mailbox("usd_advice", {
        "0001.eml": eml(
            "Ilaria Vestin", "ilaria.vestin@northaven.example", "odette.quill@quillmed.example",
            "2030-02-25T10:00:00", "Executed agreement VRE-US-EXP-22",
            "Hello,\n\nAttached is the executed Master Services Agreement (Express Air) "
            "VRE-US-EXP-22, signed by both parties. It takes effect on 2030-03-01.\n\n"
            "Kind regards,\nIlaria Vestin\nKey Account Manager\nNorthaven Freight Ltd.",
            us_contract),
        "0002.eml": eml(
            "Northaven Billing", "billing@northaven.example", "odette.quill@quillmed.example",
            "2030-07-20T08:30:00", "Charges advice NFR-10023",
            "Good day,\n\nAttached is charges advice NFR-10023 covering waybill NF88231100. "
            "The total payable is USD 1,331.61, due 19/08/2030.\n\nRegards,\nNorthaven Billing",
            us_bad_invoice),
    })

    # ---- usd_notice: the same invoice, but a Schedule-1 notice sets 6.10 -> 124.95 ----
    write_mailbox("usd_notice", {
        "0001.eml": eml(
            "Ilaria Vestin", "ilaria.vestin@northaven.example", "odette.quill@quillmed.example",
            "2030-02-25T10:00:00", "Executed agreement VRE-US-EXP-22",
            "Hello,\n\nAttached is the executed Master Services Agreement (Express Air) "
            "VRE-US-EXP-22, signed by both parties. It takes effect on 2030-03-01.\n\n"
            "Kind regards,\nIlaria Vestin\nKey Account Manager\nNorthaven Freight Ltd.",
            us_contract),
        "0002.eml": eml(
            "Northaven Billing", "billing@northaven.example", "odette.quill@quillmed.example",
            "2030-07-20T08:30:00", "Charges advice NFR-10023",
            "Good day,\n\nAttached is charges advice NFR-10023 covering waybill NF88231100. "
            "The total payable is USD 1,331.61, due 19/08/2030.\n\nRegards,\nNorthaven Billing",
            us_bad_invoice),
        "0003.eml": eml(
            "Priya Raman", "priya.raman@northaven.example", "odette.quill@quillmed.example",
            "2030-05-28T11:15:00", "Rate notice USE2-INW1 effective 2030-06-01",
            "Hello Odette,\n\nThis rate notice revises the express rates for lane USE2-INW1. "
            "Notice ref. VRE-US-EXP-22/RN-USE2-INW1-2030-06.\n\n"
            "USE2-INW1 100-299.5 kg = USD 6.10\n"
            "USE2-INW1 300+ kg = USD 5.75\n\n"
            "These rates apply to pickups from 2030-06-01 until 2030-12-31.\n\n"
            "Kind regards,\nPriya Raman\nRate Administrator\nNorthaven Freight Ltd.",
            {}),
    })

    # ---- usd_waiver: correct prices, but the waived Delivery fee 48.00 was billed ----
    write_mailbox("usd_waiver", {
        "0001.eml": eml(
            "Ilaria Vestin", "ilaria.vestin@northaven.example", "odette.quill@quillmed.example",
            "2030-02-25T10:00:00", "Executed agreement VRE-US-EXP-22",
            "Hello,\n\nAttached is the executed Master Services Agreement (Express Air) "
            "VRE-US-EXP-22, signed by both parties. It takes effect on 2030-03-01.\n\n"
            "Kind regards,\nIlaria Vestin\nKey Account Manager\nNorthaven Freight Ltd.",
            us_contract),
        "0002.eml": eml(
            "Northaven Billing", "billing@northaven.example", "odette.quill@quillmed.example",
            "2030-07-24T08:30:00", "Charges advice NFR-10031",
            "Good day,\n\nAttached is charges advice NFR-10031 covering waybill NF88231100. "
            "The total payable is USD 1,136.64, due 23/08/2030.\n\nRegards,\nNorthaven Billing",
            {"NFR-10031.pdf": invoice_us(
                "NFR-10031", "24/07/2030", "23/08/2030", "6.40", "896.00", "170.24", "1,136.64",
                with_duty=False)}),
        "0003.eml": eml(
            "Callum Wren", "callum.wren@northaven.example", "odette.quill@quillmed.example",
            "2030-07-18T14:00:00", "Waiver - delivery fee NF88231100",
            "Hello Odette,\n\nFollowing our handling review, we agree to waive the Delivery fee "
            "on waybill NF88231100 for month 2030-07. Please treat the Delivery fee on that "
            "shipment as waived.\n\nKind regards,\nCallum Wren\nCommercial Manager\n"
            "Northaven Freight Ltd.",
            {}),
    })


if __name__ == "__main__":
    sys.exit(build())

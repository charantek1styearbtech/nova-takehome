"""Apply a person's feedback.json to durable state; only later reports are affected."""
from __future__ import annotations

from . import state


def apply_feedback(data: dict, state_dir: str) -> None:
    items = data.get("items") or []
    decided_at = str(data.get("decided_at") or "")[:10]
    batch = data.get("after_batch") or ""
    facts = state.load_facts(state_dir)
    closed = state.load_closed_keys(state_dir)
    learned = state.load_learned(state_dir)

    for it in items:
        decision = it.get("decision")
        key = _key_str(it.get("finding_key") or {})
        fact = it.get("fact") or {}
        scope = it.get("scope") or {}
        applies = (scope.get("applies_to") or {})
        fact_type = fact.get("fact_type") or fact.get("type")
        fb_id = it.get("item_id")

        if decision == "reject":
            closed[key] = {"reason": "reject", "fb": fb_id}
            learned.pop(key, None)
            continue

        if decision == "close":
            if fact_type == "dispute_closed":
                closed[key] = {"reason": "dispute_closed", "fb": fb_id}
            elif fact_type == "rate_accepted":
                closed[key] = {"reason": "rate_accepted", "fb": fb_id}
                facts.append({
                    "fact_type": "rate_accepted",
                    "contract_id": applies.get("contract_id"),
                    "lane": applies.get("lane"),
                    "rate": (fact.get("value") or {}).get("rate")
                            or (fact.get("value") or {}).get("source"),
                    "value": fact.get("value"),
                    "valid_from": scope.get("valid_from"),
                    "valid_to": scope.get("valid_to"),
                    "decided_at": decided_at, "fb": fb_id, "batch": batch,
                })
            else:
                closed[key] = {"reason": "close", "fb": fb_id}
            learned[key] = {"feedback_item_id": fb_id, "batch": batch}
            continue

        if decision == "confirm" and fact_type == "sender_authority":
            facts.append({
                "fact_type": "sender_authority",
                "person": (fact.get("subject") or {}).get("person"),
                "person_email": (fact.get("subject") or {}).get("email"),
                "party": (fact.get("subject") or {}).get("party"),
                "categories": ((fact.get("value") or {}).get("categories") or []),
                "valid_from": scope.get("valid_from"),
                "valid_to": scope.get("valid_to"),
                "decided_at": decided_at, "fb": fb_id, "batch": batch,
            })
            learned[key] = {"feedback_item_id": fb_id, "batch": batch}
            continue

        if decision == "one_off_approval" and fact_type == "waiver_scope":
            facts.append({
                "fact_type": "waiver_scope",
                "key": key,
                "months": (fact.get("value") or {}).get("months") or [],
                "applies_to": applies,
                "kind": "one_off",
                "decided_at": decided_at, "fb": fb_id, "batch": batch,
            })
            learned[key] = {"feedback_item_id": fb_id, "batch": batch}
            continue

        if decision == "resolve_conflict" and fact_type == "precedence":
            facts.append({
                "fact_type": "precedence",
                "contract_id": applies.get("contract_id"),
                "lane": applies.get("lane"),
                "use": (fact.get("value") or {}).get("use"),
                "amendment": (fact.get("value") or {}).get("amendment"),
                "valid_from": scope.get("valid_from"),
                "valid_to": scope.get("valid_to"),
                "decided_at": decided_at, "fb": fb_id, "batch": batch,
            })
            learned[key] = {"feedback_item_id": fb_id, "batch": batch}
            continue

        if decision == "override_amount":
            facts.append({
                "fact_type": "override_amount",
                "key": key,
                "amount_usd": (fact.get("value") or {}).get("amount_usd"),
                "kind": "one_off",
                "decided_at": decided_at, "fb": fb_id, "batch": batch,
            })
            learned[key] = {"feedback_item_id": fb_id, "batch": batch}
            continue

        if decision == "confirm":
            learned[key] = {"feedback_item_id": fb_id, "batch": batch}

    state.save_facts(state_dir, facts)
    state.save_closed_keys(state_dir, closed)
    state.save_learned(state_dir, learned)


def _key_str(fk: dict) -> str:
    """Canonical string for a finding key, matching how we build item ids."""
    cls = fk.get("class") or ""
    if cls == "cashback":
        return f"cashback:{fk.get('contract_id')}/{fk.get('period')}/{fk.get('credit_type')}"
    if cls == "expiry":
        return f"expiry:{fk.get('contract_id')}"
    return f"{cls}:{fk.get('invoice_id')}"


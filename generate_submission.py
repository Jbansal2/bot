#!/usr/bin/env python3
"""
Generate submission.jsonl — 30 lines, one per canonical test pair.
Run:  python generate_submission.py
Requires ANTHROPIC_API_KEY env var.
"""
import json, sys, os, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bot import compose

EXPANDED = Path(__file__).parent / "dataset" / "expanded"
OUT = Path(__file__).parent / "submission.jsonl"

def load(path):
    with open(path) as f:
        return json.load(f)

def main():
    pairs = load(EXPANDED / "test_pairs.json")["pairs"]
    categories = {}
    for f in (EXPANDED / "categories").glob("*.json"):
        d = load(f)
        categories[d["slug"]] = d

    merchants = {}
    for f in (EXPANDED / "merchants").glob("*.json"):
        d = load(f)
        merchants[d["merchant_id"]] = d

    customers = {}
    for f in (EXPANDED / "customers").glob("*.json"):
        d = load(f)
        customers[d["customer_id"]] = d

    triggers = {}
    for f in (EXPANDED / "triggers").glob("*.json"):
        d = load(f)
        triggers[d["id"]] = d

    print(f"Loaded: {len(categories)} categories, {len(merchants)} merchants, "
          f"{len(customers)} customers, {len(triggers)} triggers")
    print(f"Generating {len(pairs)} test pairs...\n")

    lines = []
    for p in pairs:
        tid = p["test_id"]
        trg_id = p["trigger_id"]
        m_id = p["merchant_id"]
        c_id = p.get("customer_id")

        trigger = triggers.get(trg_id)
        merchant = merchants.get(m_id)
        customer = customers.get(c_id) if c_id else None

        if not trigger or not merchant:
            print(f"  SKIP {tid}: missing context (trg={bool(trigger)}, m={bool(merchant)})")
            continue

        cat_slug = merchant.get("category_slug", "")
        category = categories.get(cat_slug)
        if not category:
            print(f"  SKIP {tid}: missing category '{cat_slug}'")
            continue

        try:
            result = compose(category, merchant, trigger, customer)
            line = {
                "test_id": tid,
                "trigger_id": trg_id,
                "merchant_id": m_id,
                "customer_id": c_id,
                "body": result["body"],
                "cta": result["cta"],
                "send_as": result["send_as"],
                "suppression_key": result["suppression_key"],
                "rationale": result["rationale"],
            }
            lines.append(json.dumps(line, ensure_ascii=False))
            body_preview = result["body"][:80].replace('\n', ' ')
            print(f"  ✓ {tid} [{merchant.get('category_slug')}:{trigger.get('kind')}] → {body_preview}...")
        except Exception as e:
            print(f"  ✗ {tid}: error — {e}")
            import traceback; traceback.print_exc()

        time.sleep(0.3)  # rate limit

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\nWrote {len(lines)} lines to {OUT}")

if __name__ == "__main__":
    main()

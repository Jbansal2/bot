# Vera Bot — magicpin AI Challenge

## Approach

Every message is grounded in all four context layers before a word is written. The architecture has two parts:

**1. Trigger-kind dispatch** — 25 named trigger kinds each have a dedicated composition frame (in `TRIGGER_FRAMES`). The frame tells the LLM exactly *what to lead with*, *what to anchor on*, and *what CTA shape to use* for that specific kind. This prevents the model from writing generic nudges and forces specificity: a `research_digest` frame says "lead with exact title + source + trial_n from the digest item"; an `ipl_match_today` frame carries the counter-intuitive insight (Saturday = delivery, not dine-in) directly in the prompt.

**2. Context-grounded system prompt** — All four layers are injected with their real values: peer_stats numbers, merchant's exact CTR vs peer median, active offer titles, customer's language preference, trigger payload fields. The model has no room to invent — everything it cites comes from the provided JSON.

**Reply handling** covers the five cases the judge tests: auto-reply detection (probe once → wait 24h → end), hostile exit, hard reject, explicit intent transition (switches to action mode immediately, never asks another qualifying question), and out-of-scope deflection.

## Model choice

`claude-sonnet-4-20250514` at `temperature=0`. Fast (sub-5s), reliable structured JSON output, strong instruction-following on the anti-patterns list.

## Tradeoffs

- **In-memory state** — context stored in Python dicts. Fine for a 3-day evaluation window; would use Redis in production.
- **Single LLM call per composition** — no retrieval layer. The full category context (digest, offers, peer_stats, voice) fits in one prompt at ~1,500 tokens. For larger category contexts, a retrieval step over digest items would be the first upgrade.
- **Suppression is 24h per key** — conservative to avoid re-sending on the same trigger. The judge can inject new trigger versions to re-fire.

## What additional context would have helped

1. The merchant's actual conversation history beyond the last 2 turns — knowing whether they've accepted/rejected similar triggers before would improve decision quality significantly.
2. Real-time slot availability for recall/booking triggers (the dataset has static slots; production would need a live calendar lookup).
3. The `customer_aggregate.high_risk_adult_count` field is the single most useful signal for dentist category personalization — more category-specific aggregates like this across all verticals would unlock much sharper merchant-fit scoring.

## Running locally

```bash
export ANTHROPIC_API_KEY=sk-ant-...
pip install -r requirements.txt

# Start HTTP server (for judge harness)
uvicorn main:app --host 0.0.0.0 --port 8080

# Generate submission.jsonl (30 test pairs)
cd vera-v2 && python generate_submission.py

# Run judge simulator
python judge_simulator.py  # set BOT_URL + LLM_API_KEY in the file first
```

## Deploy (Railway — 5 min)

```bash
npm install -g @railway/cli
railway login && railway init
railway variables set ANTHROPIC_API_KEY=sk-ant-...
railway up
railway domain  # → your public URL
```

# Vera Bot — magicpin AI Challenge

## Approach

Every message is grounded in all four context layers before a word is written.

**1. Trigger-kind dispatch** — 25 named trigger kinds each have a dedicated composition frame. Each frame tells the model exactly what to lead with, what to anchor on, and what CTA shape to use. A `research_digest` frame says "lead with exact title + source + trial_n from the digest item." An `ipl_match_today` frame carries the counter-intuitive insight (Saturday = push delivery, not dine-in) directly in the prompt. This prevents generic nudges and forces specificity.

**2. Four-layer context injection** — All four layers are injected with real values: peer_stats numbers, merchant's exact CTR vs peer median, active offer titles, customer's language preference, trigger payload fields. The model has no room to invent — everything it cites comes from the provided JSON.

**3. Reply handling** — Covers five cases the judge tests: auto-reply detection (probe once → end on second), hostile exit, hard reject, explicit intent transition (switches to action mode immediately, no more qualifying questions), and out-of-scope deflection.

## Model Choice

**llama-3.3-70b-versatile via Groq** — fast (~200 tokens/sec), reliable structured JSON output at temperature=0, strong instruction-following on the anti-patterns list. Free tier is sufficient for the evaluation window. temperature=0 ensures determinism — same input always produces same output.

## Tradeoffs

**In-memory state** — Context stored in Python dicts. Fine for a 3-day evaluation window. Production would use Redis with TTL matching the engagement loop frequency (~30 min for active conversations, ~24h for suppression keys).

**Single LLM call per composition** — No retrieval layer. The full category context (digest, offers, peer_stats, voice) fits in one prompt at ~1,500 tokens. For larger category contexts, a retrieval step over digest items would be the first upgrade.

**Suppression is 24h per key** — Conservative to avoid re-sending on the same trigger. The judge can inject new trigger versions with a different suppression key to re-fire.

**Groq free tier** — Rate limits are sufficient for the judge harness (30 canonical test pairs + replay scenarios). If rate limits are hit, the fallback response is a generic but safe message that doesn't fabricate data.

## Scoring Design

| Dimension | How this bot addresses it |
|---|---|
| Decision quality | Trigger kind + merchant state + category profile all evaluated before writing |
| Specificity | Real numbers (₹299, 190 searches, 12 inactive days) pulled directly from payload |
| Category fit | 25 trigger frames enforce tone, avoid-list, and CTA pattern per vertical |
| Merchant fit | Offer catalog + performance metrics + conversation history injected every call |
| Engagement compulsion | Single CTA enforced, compulsion levers listed, Hinglish where natural |

## Running Locally

```bash
pip install -r requirements.txt
export GROQ_API_KEY=gsk_...
uvicorn main:app --host 0.0.0.0 --port 8080
```

## Endpoints

| Endpoint | Purpose |
|---|---|
| GET /v1/healthz | Liveness check |
| GET /v1/metadata | Bot identity |
| POST /v1/context | Push category/merchant/customer/trigger context |
| POST /v1/tick | Compose next message from stored context |
| POST /v1/reply | Handle merchant reply and continue session |

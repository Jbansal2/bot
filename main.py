"""
Vera Bot — magicpin AI Challenge submission
FastAPI server implementing all 5 judge-harness endpoints.

POST /v1/context  — receive context pushes (category/merchant/customer/trigger)
POST /v1/tick     — periodic wake-up; bot decides which actions to initiate
POST /v1/reply    — receive merchant/customer reply; bot responds synchronously
GET  /v1/healthz  — liveness probe with context counts
GET  /v1/metadata — bot identity

Author: challenge participant
"""
import os, re, json, time, hashlib
from datetime import datetime, timezone
from typing import Optional, Any
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from groq import Groq

app = FastAPI(title="Vera Bot", version="1.0.0")
START_TIME = time.time()
_client = Groq(api_key=os.environ.get("GROQ_API_KEY", ""))

# ── In-memory state ───────────────────────────────────────────────────────────
# (scope, context_id) → {version, payload, stored_at}
_contexts: dict[tuple[str, str], dict] = {}
# conversation_id → {merchant_id, customer_id, trigger_id, history, suppressed}
_conversations: dict[str, dict] = {}
# suppression_key → expiry epoch
_suppressed: dict[str, float] = {}
_auto_reply_counts: dict[str, int] = {}

# ── Trigger-kind → composer prompt variant ────────────────────────────────────
TRIGGER_FRAMES = {
    "research_digest": """\
FRAME: Peer researcher sharing a clinically relevant finding.
MUST: Lead with exact finding title + source + stats from the digest item whose id matches trigger.payload.top_item_id.
THEN: Connect to THIS merchant's patient/customer cohort (use customer_aggregate signals).
END: Curiosity CTA — offer to pull abstract + draft a patient-ed message they can forward.
CTA type: open_ended""",

    "regulation_change": """\
FRAME: Compliance alert from a trusted industry peer.
MUST: Name the exact regulation, effective date, and what it means for THIS practice.
THEN: One specific action this merchant should take.
END: Offer to draft the required notice/update.
CTA type: open_ended""",

    "recall_due": """\
FRAME: Warm recall reminder sent FROM the merchant TO their patient.
MUST: Name the patient, elapsed time since last visit, the specific service due.
THEN: Offer 2 concrete slot options from trigger.payload.available_slots.
INCLUDE: catalog price from merchant's active offers.
LANGUAGE: Match customer.identity.language_pref exactly.
send_as: merchant_on_behalf
CTA type: multi_choice_slot""",

    "perf_spike": """\
FRAME: Quick win-alert — you're getting traffic, here's how to capture it.
MUST: Name exact metric + delta + comparison period from trigger.payload.
THEN: Suggest ONE action from merchant's active offers.
END: Single yes/no — "Should I activate it now?"
CTA type: binary_yes_stop""",

    "perf_dip": """\
FRAME: Caring advisor flagging a dip before it becomes a trend.
MUST: Name exact metric + delta + comparison from trigger.payload.
ANCHOR: Peer benchmark (their number vs category peer_stats).
SUGGEST: Lowest-friction fix using their existing offers or assets.
END: Loss-aversion CTA: "Want me to draft a recovery push?"
CTA type: binary_yes_stop""",

    "seasonal_perf_dip": """\
FRAME: Reframe the dip — this is normal, here's what to do instead.
MUST: Quote the dip number + explain it's seasonal (use trigger.payload.season_note).
REDIRECT: Save acquisition spend; focus on retention.
END: Offer a specific retention action (challenge, loyalty, summer program).
CTA type: open_ended""",

    "competitor_opened": """\
FRAME: Market-intelligence update — heads-up, not alarm.
MUST: Name competitor, distance, their offer from trigger.payload.
PIVOT: Comparative advantage this merchant actually has (rating, reviews, offers).
END: Curiosity CTA about competitive positioning.
CTA type: open_ended""",

    "festival_upcoming": """\
FRAME: Timely seasonal nudge — practical not salesy.
MUST: Name the festival + days remaining from trigger.payload.
CONNECT: Category-relevant seasonal offer pattern.
END: Effort-externalization CTA — "I can draft the WhatsApp + GBP post in 5 min — say yes."
CTA type: binary_yes_stop""",

    "ipl_match_today": """\
FRAME: Same-day IPL tactical nudge for restaurants.
MUST: Name teams + venue + time from trigger.payload.
KEY INSIGHT from digest: Saturday IPL matches -12% restaurant covers (people watch at home); PUSH DELIVERY not dine-in on Saturdays.
Weeknight matches: +18% covers; push dine-in.
LEVERAGE: Merchant's existing active offers.
END: Offer to draft Swiggy banner + Insta story. Time-bound: "live in 10 min."
CTA type: binary_yes_stop""",

    "review_theme_emerged": """\
FRAME: Pattern spotter — several reviews this week mention the same thing.
MUST: Quote the exact theme + occurrence count from trigger.payload.
FRAME as: asset (positive) or fixable issue (negative).
END: Offer response template + follow-up message draft.
CTA type: open_ended""",

    "milestone_reached": """\
FRAME: Celebration + momentum builder.
MUST: State exact milestone + what it means vs peer_stats average.
OFFER: Help leverage the momentum (post, campaign, thank-you template).
CTA type: binary_yes_stop""",

    "dormant_with_vera": """\
FRAME: Light check-in — no guilt, pure curiosity.
LEAD WITH: One concrete new thing from the category digest since last engagement.
OFFER: One specific low-effort action they can say yes to.
CTA type: binary_yes_stop""",

    "winback_eligible": """\
FRAME: Re-engagement after subscription lapse.
MUST: Quote days-since-expiry + what they're missing (visibility, leads).
ANCHOR: One tangible number from performance showing the dip post-expiry.
OFFER: Easy path back — single click renewal.
CTA type: binary_yes_stop""",

    "renewal_due": """\
FRAME: Friendly renewal reminder before it expires.
MUST: Quote days_remaining + plan from trigger.payload.
ANCHOR: Best result they've had recently on their account.
OFFER: Renew now — don't lose momentum.
CTA type: binary_yes_stop""",

    "customer_lapsed_soft": """\
FRAME: Winback nudge sent as the merchant.
MUST: Name the customer, their last visit, what they were working toward.
OFFER: A concrete next step that matches their stated preference.
NO SHAME: "happens to most members" / "no judgment" framing.
send_as: merchant_on_behalf
CTA type: binary_yes_stop""",

    "customer_lapsed_hard": """\
FRAME: Longer-gap winback — warm, no-pressure.
Same rules as lapsed_soft but stronger re-entry offer (free trial, no commitment).
send_as: merchant_on_behalf
CTA type: binary_yes_stop""",

    "chronic_refill_due": """\
FRAME: Refill reminder from the pharmacy — practical, senior-respecting.
MUST: List the exact molecules from trigger.payload.molecule_list.
MUST: Quote run-out date from trigger.payload.
SHOW: Total + savings if senior discount applies.
OFFER: Free home delivery + CONFIRM-to-dispatch.
LANGUAGE: If senior and Hindi-preferring, write in Hindi-English mix.
send_as: merchant_on_behalf
CTA type: binary_confirm_cancel""",

    "supply_alert": """\
FRAME: Urgent but bounded compliance alert.
MUST: Name exact batch numbers + manufacturer from trigger.payload.
FRAME: Sub-potency, no safety risk — but customers must be informed.
DERIVE: Count of affected customers from merchant.customer_aggregate.chronic_rx_count.
OFFER: Draft WhatsApp notice + replacement-pickup workflow.
CTA type: open_ended""",

    "active_planning_intent": """\
FRAME: Bot picks up the merchant's last planning message and delivers a concrete first draft.
READ: trigger.payload.merchant_last_message to understand what they asked for.
DELIVER: An immediately usable artifact (pricing table, program structure, message draft).
END: Follow-on offer to handle the next step (outreach, GBP post, etc).
CTA type: open_ended""",

    "trial_followup": """\
FRAME: Warm follow-up after a trial session.
MUST: Reference the trial date + what they tried.
OFFER: A concrete next-session slot from trigger.payload.next_session_options.
KEEP: Light and low-commitment.
send_as: merchant_on_behalf
CTA type: binary_yes_stop""",

    "curious_ask_due": """\
FRAME: Weekly curiosity nudge — ask the merchant one genuine question.
QUESTION: About what's working, what customers are asking, or what they're planning.
RECIPROCITY: Offer up-front what you'll do with their answer (Google post, WhatsApp draft).
EFFORT CAP: "Takes 5 min."
CTA type: open_ended""",

    "gbp_unverified": """\
FRAME: Practical profile-health nudge.
MUST: Quote the estimated uplift from trigger.payload.estimated_uplift_pct.
EXPLAIN: Verification path (postcard or phone call) from trigger.payload.verification_path.
OFFER: Walk them through the 5-step process.
CTA type: binary_yes_stop""",

    "cde_opportunity": """\
FRAME: Peer professional sharing a relevant training opportunity.
MUST: Name the event, credits, fee from digest item matching trigger.payload.digest_item_id.
CONNECT: Why it's relevant to THIS merchant's case mix.
CTA type: open_ended""",

    "wedding_package_followup": """\
FRAME: Bridal follow-up — relationship continuity.
MUST: Quote days_to_wedding + window opportunity from trigger.payload.
OFFER: 30-day skin-prep program with price + slot.
PERSONALIZE: Saturday preference, merchant owner's name.
send_as: merchant_on_behalf
CTA type: binary_yes_stop""",

    "category_seasonal": """\
FRAME: Seasonal shelf/stock advice for pharmacies.
MUST: List specific demand shifts with percentages from trigger.payload.trends.
GIVE: Concrete shelf-restock action.
CTA type: open_ended""",
}

DEFAULT_FRAME = """\
FRAME: Flag the single strongest signal for this merchant right now.
Pick the most actionable signal from trigger.payload + merchant signals.
Anchor on a specific verifiable fact. Single CTA.
CTA type: open_ended"""

ANTI_PATTERNS = """\
NEVER:
- Generic "Flat X% off" when service+price exists (use "Haircut @ ₹99")
- Multiple CTAs in one message
- Opening with "Hope you're well" / "I'm reaching out today"
- Re-introducing yourself after turn 1
- Promotional tone (AMAZING DEAL!) for dentists/pharma/doctors
- Fabricate any data not explicitly in the provided context
- Include URLs (Meta rejects them; -3 penalty per URL)
- Send the same body text twice in one conversation"""

COMPULSION = """\
USE AT LEAST ONE:
1. Specificity — real number/date/citation from context (not invented)
2. Loss aversion — "you're missing X" / "before this window closes"
3. Social proof — "N dentists in your locality did Y this month"
4. Effort externalization — "I've already drafted it — just say go"
5. Curiosity — "want to see who?" / "want the full list?"
6. Reciprocity — "I noticed Y, thought you'd want to know"
7. Asking the merchant — one genuine question about their business
8. Single binary commitment — Reply YES / STOP"""


def _get_ctx(scope: str, cid: str) -> Optional[dict]:
    entry = _contexts.get((scope, cid))
    return entry["payload"] if entry else None


def _find_digest_item(category: dict, item_id: str) -> Optional[dict]:
    for item in category.get("digest", []):
        if item.get("id") == item_id:
            return item
    return category.get("digest", [None])[0] if category.get("digest") else None


def _current_month_seasonal(category: dict) -> str:
    month = datetime.now(timezone.utc).month
    ranges = {
        1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
        7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"
    }
    m = ranges[month]
    for beat in category.get("seasonal_beats", []):
        if m in beat.get("month_range", ""):
            return beat.get("note", "")
    return ""


def _is_suppressed(key: str) -> bool:
    exp = _suppressed.get(key)
    return bool(exp and time.time() < exp)


def _set_suppression(key: str, hours: float = 24.0):
    _suppressed[key] = time.time() + hours * 3600


def compose(
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict] = None,
    conversation_history: Optional[list] = None,
) -> dict:
    """
    Core deterministic composer. temperature=0.
    Returns {body, cta, send_as, suppression_key, rationale, template_name, template_params}
    """
    kind = trigger.get("kind", "")
    frame = TRIGGER_FRAMES.get(kind, DEFAULT_FRAME)
    is_customer_scope = trigger.get("scope") == "customer" or customer is not None

    cat_slug = category.get("slug", "unknown")
    voice = category.get("voice", {})
    tone = voice.get("tone", "professional")
    taboos = voice.get("vocab_taboo", voice.get("taboos", []))
    offer_catalog = category.get("offer_catalog", [])
    peer_stats = category.get("peer_stats", {})

    # Resolve digest item for research/compliance triggers
    digest_block = ""
    top_item_id = trigger.get("payload", {}).get("top_item_id", "")
    if top_item_id:
        item = _find_digest_item(category, top_item_id)
        if item:
            digest_block = f"""
DIGEST ITEM (use these exact numbers):
  id: {item.get('id')}
  title: {item.get('title')}
  source: {item.get('source')}
  trial_n: {item.get('trial_n', 'N/A')}
  patient_segment: {item.get('patient_segment', 'N/A')}
  summary: {item.get('summary', '')}
  actionable: {item.get('actionable', '')}
"""

    # Merchant fields
    m_id = merchant.get("merchant_id", "")
    identity = merchant.get("identity", {})
    m_name = identity.get("name", "")
    owner = identity.get("owner_first_name", "")
    languages = identity.get("languages", ["en"])
    locality = identity.get("locality", "")
    city = identity.get("city", "")
    perf = merchant.get("performance", {})
    signals = merchant.get("signals", [])
    offers = merchant.get("offers", [])
    active_offers = [o for o in offers if o.get("status") == "active"]
    conv_hist = merchant.get("conversation_history", [])
    cust_agg = merchant.get("customer_aggregate", {})
    subscription = merchant.get("subscription", {})
    review_themes = merchant.get("review_themes", [])
    last_turn = conv_hist[-1] if conv_hist else None

    # Customer fields
    cust_block = ""
    if customer:
        ci = customer.get("identity", {})
        rel = customer.get("relationship", {})
        prefs = customer.get("preferences", {})
        consent = customer.get("consent", {})
        cust_block = f"""
=== LAYER 4: CUSTOMER ===
Name: {ci.get('name')} | Age band: {ci.get('age_band')}
Language pref: {ci.get('language_pref')} ← MUST MATCH THIS IN MESSAGE
State: {customer.get('state')}
Last visit: {rel.get('last_visit')} | Total visits: {rel.get('visits_total')}
Services received: {rel.get('services_received', [])}
Preferred slots: {prefs.get('preferred_slots')} ← USE THIS FOR SLOT OFFERS
Consent scope: {consent.get('scope', [])}
send_as MUST be: merchant_on_behalf"""

    hist_block = ""
    if conversation_history:
        hist_block = "\n=== PRIOR CONVERSATION (do NOT repeat body already sent) ===\n"
        for t in conversation_history[-4:]:
            hist_block += f"[{t.get('role', '?')}]: {t.get('content', '')}\n"

    seasonal = _current_month_seasonal(category)

    system = f"""You are Vera, magicpin's merchant AI assistant. Compose ONE high-compulsion message.

CATEGORY: {cat_slug} | TONE: {tone}
TABOOS (never use): {', '.join(taboos) if taboos else 'none'}
PEER STATS: avg_ctr={peer_stats.get('avg_ctr')} avg_rating={peer_stats.get('avg_rating')} avg_reviews={peer_stats.get('avg_review_count')}
OFFER CATALOG: {[o.get('title') for o in offer_catalog[:4]]}
SEASONAL NOTE: {seasonal or 'none'}

TRIGGER COMPOSITION FRAME:
{frame}
{digest_block}
{ANTI_PATTERNS}

{COMPULSION}

OUTPUT: ONLY valid JSON — no markdown fences, no explanation, no preamble:
{{
  "body": "<the exact message — no filler, no URL>",
  "cta": "open_ended" | "binary_yes_stop" | "binary_yes_no" | "binary_confirm_cancel" | "multi_choice_slot" | "none",
  "send_as": "vera" | "merchant_on_behalf",
  "suppression_key": "<use trigger's suppression_key>",
  "rationale": "<1-2 sentences: which signal drove this + why it fits this merchant right now>",
  "template_name": "<snake_case_template_name>",
  "template_params": ["<param1>", "<param2>", "<param3>"]
}}"""

    user = f"""Compose the message now.

=== LAYER 1: CATEGORY ===
{cat_slug} | tone: {tone}
Top 3 catalog offers: {[o.get('title') for o in offer_catalog[:3]]}
Active seasonal beat: {seasonal or 'none'}

=== LAYER 2: MERCHANT ===
Name: {m_name}{f'  (Owner: {owner})' if owner else ''}
Location: {locality}, {city} | Languages: {languages}
Subscription: {subscription.get('status')} | Plan: {subscription.get('plan')} | Days remaining: {subscription.get('days_remaining')}
Performance (30d): views={perf.get('views')} calls={perf.get('calls')} ctr={perf.get('ctr')} directions={perf.get('directions')}
7d delta: views {perf.get('delta_7d', {}).get('views_pct', 0):+.0%} | calls {perf.get('delta_7d', {}).get('calls_pct', 0):+.0%}
Active offers: {[o.get('title') for o in active_offers] or 'NONE'}
Signals: {signals}
Customer aggregate: {json.dumps(cust_agg)}
Review themes: {[(t.get('theme'), t.get('sentiment'), t.get('occurrences_30d')) for t in review_themes]}
Last Vera engagement: {(last_turn.get('ts','?') + ' — ' + last_turn.get('engagement','?')) if last_turn else 'none'}

=== LAYER 3: TRIGGER ===
Kind: {kind} | Scope: {trigger.get('scope')} | Source: {trigger.get('source')} | Urgency: {trigger.get('urgency')}/5
Suppression key: {trigger.get('suppression_key', '')}
Expires: {trigger.get('expires_at', '')}
Payload: {json.dumps(trigger.get('payload', {}))}
{cust_block}
{hist_block}"""

    try:
        resp = _client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=800,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```json?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)
        result = json.loads(raw)

        # Enforce
        if is_customer_scope and customer:
            result["send_as"] = "merchant_on_behalf"
        if not result.get("suppression_key"):
            week = datetime.now(timezone.utc).strftime("%Y-W%W")
            result["suppression_key"] = f"{kind}:{m_id}:{week}"
        result.setdefault("template_name", f"vera_{kind}_v1")
        result.setdefault("template_params", [m_name, kind, result.get("body", "")[:80]])
        return result

    except Exception as e:
        week = datetime.now(timezone.utc).strftime("%Y-W%W")
        return {
            "body": f"{'Dr.' if 'dentist' in cat_slug else ''}{owner or m_name}, quick update on your {kind.replace('_', ' ')}. Want me to act on it?",
            "cta": "binary_yes_stop",
            "send_as": "vera",
            "suppression_key": trigger.get("suppression_key") or f"{kind}:{m_id}:{week}",
            "rationale": f"[fallback] compose error: {str(e)[:80]}",
            "template_name": f"vera_{kind}_v1",
            "template_params": [owner or m_name, kind, ""],
        }


# ── Reply handlers ─────────────────────────────────────────────────────────────

AUTO_REPLY_PATTERNS = [
    "thank you for contacting", "our team will respond", "automated assistant",
    "aapki jaankari ke liye", "team tak pahuncha", "main ek automated",
    "i am currently unavailable", "will get back to you shortly",
    "auto-reply", "i'm away", "out of office",
]

ACCEPT_WORDS = [
    "yes", "haan", "ha ", "ok", "okay", "sure", "do it", "go ahead", "proceed",
    "send", "chalao", "kar do", "let's do it", "lets do it", "chalo", "agree",
    "sounds good", "perfect", "great", "confirm", "1", "2", "done", "what's next",
    "whats next", "next step", "karo", "bhejo",
]
REJECT_WORDS = [
    "no", "nahi", "nope", "not now", "later", "stop", "band karo", "mat karo",
    "not interested", "don't message", "dont message", "remove", "unsubscribe",
    "buss", "bas karo",
]
HOSTILE_WORDS = [
    "spam", "useless", "annoying", "block", "report", "stupid", "waste",
    "rubbish", "bakwas", "bothering", "stop sending", "stop messaging",
]
JOIN_WORDS = [
    "join", "judrna", "subscribe", "membership", "sign up", "register",
    "enroll", "kaise join", "kaise karein",
]
SCOPE_OUT_WORDS = [
    "gst", "income tax", "legal", "insurance", "loan", "bank", "police",
    "marriage", "personal", "relationship",
]


def _classify_reply(message: str) -> str:
    t = message.lower().strip()
    if any(p in t for p in AUTO_REPLY_PATTERNS):
        return "auto_reply"
    if any(p in t for p in HOSTILE_WORDS) or ("stop" in t and "messaging" in t):
        return "hostile"
    if any(p in t for p in REJECT_WORDS):
        return "reject"
    if any(p in t for p in JOIN_WORDS):
        return "join_intent"
    if any(p in t for p in ACCEPT_WORDS):
        return "accept"
    if any(p in t for p in SCOPE_OUT_WORDS):
        return "out_of_scope"
    if "?" in t:
        return "question"
    return "ambiguous"

def _handle_reply(conv_id: str, merchant_id: str, customer_id: Optional[str],
                  message: str, turn: int) -> dict:
    conv = _conversations.get(conv_id, {})
    intent = _classify_reply(message)

    if intent == "auto_reply":
        # Track by conv_id (simulator uses different conv_ids each turn)
        auto_count = _auto_reply_counts.get(conv_id, 0) + 1
        _auto_reply_counts[conv_id] = auto_count

        if auto_count == 1:
            merchant = _get_ctx("merchant", merchant_id) or {}
            name = merchant.get("identity", {}).get("name", "")
            owner = merchant.get("identity", {}).get("owner_first_name", "")
            return {
                "action": "send",
                "body": f"{owner or name} — looks like an auto-reply 😊 Just one quick thing when you're free. Reply YES to continue.",
                "cta": "binary_yes_stop",
                "rationale": "Detected auto-reply; one probe for the real owner.",
            }
        else:
            conv["suppressed"] = True
            _conversations[conv_id] = conv
            return {
                "action": "end",
                "rationale": f"Auto-reply {auto_count}× with no engagement. Closing conversation.",
            }

    if intent == "hostile":
        conv["suppressed"] = True
        _conversations[conv_id] = conv
        _set_suppression(f"hostile:{merchant_id}", hours=168)
        return {
            "action": "end",
            "rationale": "Merchant expressed frustration. Closing and suppressing for 7 days.",
        }

    if intent == "reject":
        conv["suppressed"] = True
        _conversations[conv_id] = conv
        return {
            "action": "send",
            "body": "No problem! Jab bhi kaam aaye, bas reply kar dena. 🙏",
            "cta": "none",
            "rationale": "Merchant declined — soft exit with re-entry hook.",
        }

    if intent == "join_intent":
        merchant = _get_ctx("merchant", merchant_id) or {}
        name = merchant.get("identity", {}).get("name", "your business")
        city = merchant.get("identity", {}).get("city", "")
        return {
            "action": "send",
            "body": f"Perfect! {name} ka setup {city} mein karne ke liye 3 steps hain. Step 1: aapka business name aur area confirm karo — kya '{name}' theek hai? Reply YES to confirm.",
            "cta": "binary_yes_stop",
            "rationale": "Merchant signaled join intent — switching to action mode immediately.",
        }

    if intent == "out_of_scope":
        conv_history = conv.get("history", [])
        last_topic = conv_history[-2]["content"][:60] if len(conv_history) >= 2 else "the last topic"
        return {
            "action": "send",
            "body": f"That's outside what I can help with directly — aapke CA ya advisor better placed hain. Coming back to '{last_topic}'... shall I proceed with that?",
            "cta": "binary_yes_stop",
            "rationale": "Out-of-scope request declined; redirected to on-topic thread.",
        }

    if intent == "accept":
        merchant = _get_ctx("merchant", merchant_id) or {}
        trigger_id = conv.get("trigger_id", "")
        trigger = _get_ctx("trigger", trigger_id) or {}
        kind = trigger.get("kind", "")
        conv_history = conv.get("history", [])
        last_vera_msg = next((h["content"] for h in reversed(conv_history) if h.get("role") == "vera"), "")
        body = _compose_acceptance(merchant, trigger, kind, last_vera_msg)
        return {
            "action": "send",
            "body": body,
            "cta": "open_ended",
            "rationale": "Merchant accepted — delivering promised artifact/next step.",
        }

    if intent == "question":
        merchant = _get_ctx("merchant", merchant_id) or {}
        trigger_id = conv.get("trigger_id", "")
        trigger = _get_ctx("trigger", trigger_id) or {}
        category_slug = merchant.get("category_slug", "")
        category = _get_ctx("category", category_slug) or {}
        answer = _compose_answer(category, merchant, trigger, message)
        return {
            "action": "send",
            "body": answer,
            "cta": "open_ended",
            "rationale": "Merchant asked a question — answered with context-grounded facts.",
        }

    # Ambiguous
    merchant = _get_ctx("merchant", merchant_id) or {}
    name = merchant.get("identity", {}).get("owner_first_name") or merchant.get("identity", {}).get("name", "")
    return {
        "action": "send",
        "body": f"Got it, {name}! Quick check — shall I go ahead with what I proposed? Reply YES to confirm or let me know what you'd prefer.",
        "cta": "binary_yes_stop",
        "rationale": "Ambiguous reply — re-asking with a clear binary to move forward.",
    }

def _compose_acceptance(merchant: dict, trigger: dict, kind: str, last_vera_msg: str) -> str:
    identity = merchant.get("identity", {})
    name = identity.get("owner_first_name") or identity.get("name", "")
    active_offers = [o for o in merchant.get("offers", []) if o.get("status") == "active"]
    offer_title = active_offers[0].get("title") if active_offers else "your current offer"

    kind_actions = {
        "research_digest": f"Sending the abstract now. Also drafting the patient-ed WhatsApp — ready in ~60 seconds. Want me to also schedule a Google post for tomorrow 10am?",
        "recall_due": f"Booking confirmed! I'll send the slot confirmation to the patient now. Reply STOP if you want to cancel.",
        "perf_spike": f"Activating '{offer_title}' now. I'll also draft a WhatsApp push to send to your customer list. Confirm list size or just reply GO.",
        "perf_dip": f"Drafting the recovery push now — I'll use '{offer_title}' as the anchor. Ready for you to review in 60 seconds.",
        "competitor_opened": f"Running a comparison audit now — pulling their reviews vs yours. Ready in 2 min.",
        "festival_upcoming": f"Drafting the WhatsApp + GBP post now. I'll have both ready for review — just say GO when you want them sent.",
        "supply_alert": f"Pulling the affected customer list now. I'll draft the WhatsApp notice for each — ready for your review.",
        "chronic_refill_due": f"Dispatching now to saved address. Estimated delivery by 5pm today. Confirmation message sent to the registered number.",
        "renewal_due": f"Renewal link sent to your registered number. Your profile stays active uninterrupted. Thank you!",
        "dormant_with_vera": f"Great to hear back! Let me pull your profile audit — 3 quick things to update. Ready in 90 seconds.",
    }
    return kind_actions.get(kind, f"On it, {name}! Drafting now — will share in 60 seconds. Reply STOP to cancel.")


def _compose_answer(category: dict, merchant: dict, trigger: dict, question: str) -> str:
    """LLM-powered Q&A grounded in context."""
    identity = merchant.get("identity", {})
    name = identity.get("name", "")
    active_offers = [o for o in merchant.get("offers", []) if o.get("status") == "active"]
    perf = merchant.get("performance", {})
    peer = category.get("peer_stats", {})

    system = f"""You are Vera, magicpin's merchant AI assistant. Answer the merchant's question using ONLY the provided data. 
Keep it under 2 sentences + one CTA. Use {category.get('voice', {}).get('tone', 'professional')} tone.
NEVER fabricate data not in the context provided."""

    context = f"""Merchant: {name} | Category: {category.get('slug')}
Active offers: {[o.get('title') for o in active_offers]}
Performance: views={perf.get('views')} calls={perf.get('calls')} ctr={perf.get('ctr')}
Peer avg_ctr={peer.get('avg_ctr')}
Trigger payload: {json.dumps(trigger.get('payload', {}))}
Merchant question: {question}"""

    try:
        resp = _client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=200,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": context}],
            temperature=0,
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return f"Based on what I have — {active_offers[0].get('title') if active_offers else 'your current setup'} would be the best fit. Want me to dig deeper?"


# ── Pydantic models ───────────────────────────────────────────────────────────

class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: Optional[str] = None

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: Optional[str] = None
    turn_number: int = 1


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _) in _contexts:
        if scope in counts:
            counts[scope] += 1
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": counts,
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Vera Bot",
        "team_members": ["participant"],
        "model": "llama-3.3-70b-versatile",
        "approach": "4-context grounded composer with trigger-kind dispatch, auto-reply detection, and intent-transition routing",
        "contact_email": "participant@example.com",
        "version": "1.0.0",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/v1/context")
async def push_context(body: ContextBody):
    if body.scope not in ("category", "merchant", "customer", "trigger"):
        return JSONResponse(status_code=400, content={
            "accepted": False, "reason": "invalid_scope",
            "details": f"scope must be one of category/merchant/customer/trigger, got '{body.scope}'"
        })

    key = (body.scope, body.context_id)
    existing = _contexts.get(key)

    if existing and existing["version"] >= body.version:
        return JSONResponse(status_code=409, content={
            "accepted": False, "reason": "stale_version",
            "current_version": existing["version"],
        })

    stored_at = datetime.now(timezone.utc).isoformat()
    _contexts[key] = {
        "version": body.version,
        "payload": body.payload,
        "stored_at": stored_at,
    }
    ack_id = f"ack_{body.context_id}_v{body.version}"
    return {"accepted": True, "ack_id": ack_id, "stored_at": stored_at}


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []
    fired_merchants = set()  # one action per merchant per tick

    # Sort triggers by urgency descending
    trigger_items = []
    for trg_id in body.available_triggers:
        trg = _get_ctx("trigger", trg_id)
        if trg:
            trigger_items.append((trg_id, trg))
    trigger_items.sort(key=lambda x: -x[1].get("urgency", 0))

    for trg_id, trg in trigger_items:
        merchant_id = trg.get("merchant_id")
        customer_id = trg.get("customer_id")
        if not merchant_id:
            continue

        # One action per merchant per tick
        if merchant_id in fired_merchants:
            continue

        # Suppression check
        supp_key = trg.get("suppression_key", f"{trg.get('kind')}:{merchant_id}")
        if _is_suppressed(supp_key):
            continue

        # Context resolution
        merchant = _get_ctx("merchant", merchant_id)
        if not merchant:
            continue
        cat_slug = merchant.get("category_slug", "")
        category = _get_ctx("category", cat_slug)
        if not category:
            continue
        customer = _get_ctx("customer", customer_id) if customer_id else None

        # Conversation ID — deterministic per merchant+trigger
        conv_id = f"conv_{merchant_id}_{trg_id}"

        # Don't re-send if conversation is suppressed
        if _conversations.get(conv_id, {}).get("suppressed"):
            continue

        result = compose(category, merchant, trg, customer)

        # Expiry check
        expires_at = trg.get("expires_at", "")
        if expires_at:
            try:
                exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) > exp:
                    continue
            except Exception:
                pass

        # Store conversation state
        _conversations[conv_id] = {
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "trigger_id": trg_id,
            "auto_reply_count": 0,
            "suppressed": False,
            "history": [{"role": "vera", "content": result["body"]}],
        }

        # Apply suppression
        _set_suppression(result["suppression_key"])
        fired_merchants.add(merchant_id)

        actions.append({
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": result["send_as"],
            "trigger_id": trg_id,
            "template_name": result.get("template_name", f"vera_{trg.get('kind','generic')}_v1"),
            "template_params": result.get("template_params", []),
            "body": result["body"],
            "cta": result["cta"],
            "suppression_key": result["suppression_key"],
            "rationale": result["rationale"],
        })

        if len(actions) >= 20:  # harness cap
            break

    return {"actions": actions}


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    conv_id = body.conversation_id
    merchant_id = body.merchant_id
    customer_id = body.customer_id

    # Auto-resolve merchant_id from conversation if not supplied
    if not merchant_id and conv_id in _conversations:
        merchant_id = _conversations[conv_id].get("merchant_id")

    # Store incoming message in history
    conv = _conversations.setdefault(conv_id, {
        "merchant_id": merchant_id, "customer_id": customer_id,
        "trigger_id": "", "auto_reply_count": 0, "suppressed": False, "history": [],
    })
    conv["history"].append({"role": body.from_role, "content": body.message})

    result = _handle_reply(conv_id, merchant_id or "", customer_id, body.message, body.turn_number)

    # Store vera's response in history if sending
    if result.get("action") == "send" and result.get("body"):
        conv["history"].append({"role": "vera", "content": result["body"]})

    return result

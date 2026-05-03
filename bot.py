"""
bot.py — magicpin AI Challenge standalone submission module
As specified in challenge-brief.md §7.1

Usage:
    from bot import compose, respond, ConversationState
    result = compose(category_dict, merchant_dict, trigger_dict, customer_dict_or_None)

Also used by the HTTP server (main.py) internally.
"""
import os, re, json
from typing import Optional
from dataclasses import dataclass, field
from groq import Groq

_client = Groq(api_key=os.environ.get("GROQ_API_KEY", ""))

# ── Trigger-kind dispatch table ───────────────────────────────────────────────
TRIGGER_FRAMES = {
    "research_digest": """\
FRAME: Peer researcher sharing a clinically relevant finding.
LEAD WITH: Exact finding title + source + key stat (trial_n, delta%) from the digest item matching trigger.payload.top_item_id.
THEN: Connect to THIS merchant's specific patient/customer cohort (use customer_aggregate fields).
END: Curiosity CTA — offer to pull abstract + draft a patient-ed message they can forward.
CTA: open_ended""",

    "regulation_change": """\
FRAME: Compliance alert from a trusted industry peer.
LEAD WITH: Exact regulation name + effective date + specific impact on THIS practice type.
ACTION: One concrete step this merchant should take before the deadline.
OFFER: Draft the required notice/SOP update.
CTA: open_ended""",

    "recall_due": """\
FRAME: Warm recall reminder — send AS merchant TO their patient.
LEAD WITH: Patient name + elapsed months since last visit.
NAME: The exact service due from trigger.payload.service_due.
OFFER: 2 concrete slot options from trigger.payload.available_slots.
INCLUDE: Catalog price from merchant's active offers.
LANGUAGE: MUST match customer.identity.language_pref exactly (hi-en mix, English, etc.)
send_as: merchant_on_behalf
CTA: multi_choice_slot""",

    "perf_spike": """\
FRAME: Quick win-alert — you're getting traffic, here's how to capture it.
LEAD WITH: Exact metric + delta + window from trigger.payload.
SUGGEST: ONE action using merchant's active offers.
CTA: binary_yes_stop — ask if they want to activate it now.""",

    "perf_dip": """\
FRAME: Caring advisor flagging a dip early.
LEAD WITH: Exact metric + delta + window from trigger.payload.
ANCHOR: Peer benchmark from category.peer_stats (their CTR vs peer median).
SUGGEST: Lowest-friction fix using existing offers.
CTA: binary_yes_stop — "Want me to draft a recovery push?"
NEVER: catastrophize — frame as fixable.""",

    "seasonal_perf_dip": """\
FRAME: Reframe the dip as normal seasonal pattern.
LEAD WITH: Exact dip number + confirm it's expected (cite trigger.payload.season_note).
REDIRECT: Save acquisition spend; focus on retention now.
OFFER: Specific retention action (attendance challenge, loyalty program, summer program).
CTA: open_ended""",

    "competitor_opened": """\
FRAME: Market-intelligence update — heads-up, not alarm.
LEAD WITH: Competitor name + distance + their offer from trigger.payload.
PIVOT: Comparative advantage this merchant actually has (rating, reviews, offers, experience).
CTA: Curiosity — "Want to see how you compare on the 3 things patients search first?"
CTA type: open_ended""",

    "festival_upcoming": """\
FRAME: Timely seasonal nudge — practical, not salesy.
LEAD WITH: Festival name + exact days remaining from trigger.payload.
CONNECT: Category-relevant seasonal offer pattern (use seasonal_beats from category context).
OFFER: Effort-externalization — "I can draft the WhatsApp + GBP post in 5 min."
CTA: binary_yes_stop""",

    "ipl_match_today": """\
FRAME: Same-day IPL tactical advisory for restaurants.
MUST USE: teams + venue + time from trigger.payload.
KEY DATA: Saturday IPL matches drive -12% restaurant covers (people watch at home); weeknight matches +18%.
ADVICE: If Saturday → push delivery, not dine-in. If weeknight → push dine-in combo.
LEVERAGE: Merchant's existing active offers.
OFFER: Draft Swiggy banner + Insta story. "Live in 10 min."
CTA: binary_yes_stop""",

    "review_theme_emerged": """\
FRAME: Pattern spotter — reviews this week mention the same theme.
LEAD WITH: Exact theme + occurrence count from trigger.payload.
IF POSITIVE: Frame as asset to amplify.
IF NEGATIVE: Frame as fixable pattern.
OFFER: Response template + follow-up customer message draft.
CTA: open_ended""",

    "milestone_reached": """\
FRAME: Celebration + momentum builder.
LEAD WITH: Exact milestone value + what it means vs peer_stats average.
OFFER: Help leverage the momentum (post, campaign, customer thank-you).
CTA: binary_yes_stop""",

    "dormant_with_vera": """\
FRAME: Light check-in after silence — no guilt, pure curiosity.
LEAD WITH: One concrete new thing from category.digest since last engagement.
OFFER: One specific low-effort action they can say yes to.
CTA: binary_yes_stop""",

    "winback_eligible": """\
FRAME: Re-engagement offer after subscription lapse.
LEAD WITH: Days-since-expiry + what visibility they're missing (use perf delta if available).
ANCHOR: Performance dip since expiry as loss-aversion hook.
OFFER: Easy renewal path.
CTA: binary_yes_stop""",

    "renewal_due": """\
FRAME: Friendly renewal reminder with a hook.
LEAD WITH: Days remaining + plan from trigger.payload.
ANCHOR: Best result they achieved recently on their account.
OFFER: Renew now — don't lose the momentum.
CTA: binary_yes_stop""",

    "customer_lapsed_soft": """\
FRAME: Warm winback nudge sent as the merchant.
LEAD WITH: Customer name + elapsed time + what they were working toward.
OFFER: Concrete next step matching their stated preference.
NO SHAME framing: "happens to most people at some point"
send_as: merchant_on_behalf
CTA: binary_yes_stop""",

    "customer_lapsed_hard": """\
FRAME: Longer-gap winback — warm, zero pressure.
Same structure as lapsed_soft but stronger re-entry incentive (free trial, no commitment).
send_as: merchant_on_behalf
CTA: binary_yes_stop""",

    "chronic_refill_due": """\
FRAME: Refill reminder from the pharmacy.
LEAD WITH: Exact molecule list from trigger.payload.molecule_list.
QUOTE: Run-out date from trigger.payload.stock_runs_out_iso.
SHOW: Total + savings (apply senior discount 15% if customer.identity.senior_citizen=true).
OFFER: Free home delivery + CONFIRM-to-dispatch.
LANGUAGE: If senior citizen and Hindi-preferring, write in Hindi-English mix (not pure English).
send_as: merchant_on_behalf
CTA: binary_confirm_cancel""",

    "supply_alert": """\
FRAME: Urgent but bounded compliance alert.
LEAD WITH: Exact batch numbers + manufacturer from trigger.payload.
FRAME: Sub-potency, no safety risk — but customers must be informed.
DERIVE: Approximate count of affected customers from merchant.customer_aggregate.chronic_rx_count.
OFFER: Draft patient WhatsApp notice + replacement-pickup workflow end-to-end.
CTA: open_ended""",

    "active_planning_intent": """\
FRAME: Bot picks up merchant's last planning ask and delivers a complete draft artifact.
READ: trigger.payload.merchant_last_message to understand what they asked for.
DELIVER: An immediately usable artifact (pricing tier table, program structure, WhatsApp template).
END: Follow-on offer to handle the next distribution step.
CTA: open_ended""",

    "trial_followup": """\
FRAME: Warm follow-up after a trial visit.
REFERENCE: The trial date + what they tried from trigger.payload.
OFFER: A concrete next-session slot from trigger.payload.next_session_options.
KEEP: Light + low-commitment tone.
send_as: merchant_on_behalf
CTA: binary_yes_stop""",

    "curious_ask_due": """\
FRAME: Weekly curiosity question — ask the merchant something genuine.
ASK: About what service is most in demand, what customers keep asking, or what they're planning.
RECIPROCITY: Offer what you'll do with their answer (Google post draft, WhatsApp reply template).
EFFORT CAP: "Takes 5 min."
CTA: open_ended""",

    "gbp_unverified": """\
FRAME: Practical GBP health nudge.
QUOTE: Estimated visibility uplift from trigger.payload.estimated_uplift_pct.
EXPLAIN: The exact verification path from trigger.payload.verification_path.
OFFER: Walk them through the 5-step process.
CTA: binary_yes_stop""",

    "cde_opportunity": """\
FRAME: Peer professional sharing a relevant training opportunity.
NAME: The event, credits, fee/free from the digest item matching trigger.payload.digest_item_id.
CONNECT: Why this matters for THIS merchant's case mix / category trends.
CTA: open_ended""",

    "wedding_package_followup": """\
FRAME: Bridal continuity — you did their trial, now close the preparation window.
QUOTE: days_to_wedding + next_step_window from trigger.payload.
OFFER: 30-day skin-prep program with price and slot.
PERSONALIZE: Weekend preference, merchant owner name.
send_as: merchant_on_behalf
CTA: binary_yes_stop""",

    "category_seasonal": """\
FRAME: Seasonal shelf advisory for pharmacy.
LIST: Specific demand shifts with percentages from trigger.payload.trends.
GIVE: Concrete shelf-restock action (move X to counter, move Y to back).
CTA: open_ended""",

    "ipl_match_today": """\
FRAME: Same-day IPL match advisory.
USE: Match teams + venue + time from trigger.payload.
KEY INSIGHT: Saturday IPL → -12% covers (people watch at home) → push delivery.
Weeknight IPL → +18% covers → push dine-in combo.
LEVERAGE: Merchant's existing active BOGO or combo offer.
OFFER: Draft Swiggy banner + Insta story in 10 min.
CTA: binary_yes_stop""",
}

DEFAULT_FRAME = """\
FRAME: Flag the single strongest actionable signal for this merchant right now.
Pick the most specific, verifiable signal from trigger.payload + merchant.signals.
Anchor on one real number. Single CTA.
CTA: open_ended"""

ANTI_PATTERNS = """\
PENALIZED — NEVER:
• Generic "Flat X% off" when service+price exists: use "Haircut @ ₹99" not "discount offer"
• Multiple CTAs in one message — pick ONE
• Opening with "Hope you're well" / "I'm reaching out today to..."
• Re-introducing yourself (Vera here / I'm Vera) after turn 1
• PROMOTIONAL CAPS (AMAZING DEAL!) for dentists, pharma, medical categories
• Fabricating ANY data not explicitly in the provided context
• Including URLs in message body (Meta rejects; -3 penalty each)
• Sending the same body text twice in the same conversation
• Long preambles — get to the point in sentence 1"""

COMPULSION_LEVERS = """\
USE AT LEAST ONE compulsion lever:
1. Specificity/verifiability — real number/date/source from context (not invented)
2. Loss aversion — "you're missing X" / "before this window closes" / "Y patients left waiting"
3. Social proof — "N dentists in your locality did Y this month" (only if data supports it)
4. Effort externalization — "I've already drafted it — just say go" / "5-min setup"
5. Curiosity — "want to see who?" / "want the full breakdown?"
6. Reciprocity — "noticed Y about your account, thought you'd want to know"
7. Asking the merchant — one genuine question about their business
8. Single binary commitment — Reply YES / STOP (not multi-choice unless booking slots)"""


def _find_digest_item(category: dict, item_id: str) -> Optional[dict]:
    """Find digest item by id, fall back to first item."""
    for item in category.get("digest", []):
        if item.get("id") == item_id:
            return item
    items = category.get("digest", [])
    return items[0] if items else None


def _current_seasonal_beat(category: dict) -> str:
    from datetime import datetime
    month_names = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    m = month_names[datetime.now().month - 1]
    for beat in category.get("seasonal_beats", []):
        if m in beat.get("month_range", ""):
            return beat.get("note", "")
    return ""


def compose(
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict] = None,
) -> dict:
    """
    Core deterministic compose function.
    Inputs: dicts from dataset JSON.
    Returns: {body, cta, send_as, suppression_key, rationale}
    temperature=0 for determinism.
    """
    kind = trigger.get("kind", "")
    frame = TRIGGER_FRAMES.get(kind, DEFAULT_FRAME)
    is_customer_scope = customer is not None or trigger.get("scope") == "customer"

    cat_slug = category.get("slug", "")
    voice = category.get("voice", {})
    tone = voice.get("tone", "professional")
    taboos = voice.get("vocab_taboo", voice.get("taboos", []))
    peer_stats = category.get("peer_stats", {})
    offer_catalog = category.get("offer_catalog", [])

    # Resolve digest item
    digest_block = ""
    top_item_id = trigger.get("payload", {}).get("top_item_id", "")
    if top_item_id or kind in ("research_digest", "regulation_change", "cde_opportunity", "supply_alert"):
        item = _find_digest_item(category, top_item_id)
        if item:
            digest_block = f"""
DIGEST/ALERT ITEM (use exact numbers from here):
  id: {item.get('id')}
  title: {item.get('title')}
  source: {item.get('source', 'N/A')}
  trial_n: {item.get('trial_n', 'N/A')}
  patient_segment: {item.get('patient_segment', 'N/A')}
  summary: {item.get('summary', '')}
  actionable: {item.get('actionable', '')}
  credits: {item.get('credits', 'N/A')}
  date: {item.get('date', 'N/A')}
"""

    # Merchant fields
    merchant_id = merchant.get("merchant_id", "")
    identity = merchant.get("identity", {})
    name = identity.get("name", "")
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
    sub = merchant.get("subscription", {})
    review_themes = merchant.get("review_themes", [])
    last_turn = conv_hist[-1] if conv_hist else None

    # Customer block
    customer_block = ""
    if customer:
        ci = customer.get("identity", {})
        rel = customer.get("relationship", {})
        prefs = customer.get("preferences", {})
        customer_block = f"""
=== LAYER 4: CUSTOMER (send_as MUST be merchant_on_behalf) ===
Name: {ci.get('name')} | Age: {ci.get('age_band')}
Language pref: {ci.get('language_pref')} ← WRITE IN THIS LANGUAGE MIX
State: {customer.get('state')}
Last visit: {rel.get('last_visit')} | Total visits: {rel.get('visits_total')}
Services received: {rel.get('services_received', [])}
Preferred slots: {prefs.get('preferred_slots')} ← HONOR THIS IN SLOT CHOICES
Consent: {customer.get('consent', {}).get('scope', [])}
"""

    seasonal = _current_seasonal_beat(category)

    system = f"""You are Vera, magicpin's merchant AI assistant. Compose ONE high-compulsion message for a merchant.

CATEGORY: {cat_slug} | VOICE TONE: {tone}
VOCABULARY TABOOS (NEVER USE): {', '.join(taboos) if taboos else 'none'}
PEER STATS: avg_ctr={peer_stats.get('avg_ctr')} avg_rating={peer_stats.get('avg_rating')} avg_reviews={peer_stats.get('avg_review_count')} avg_views={peer_stats.get('avg_views_30d')}
OFFER CATALOG (prefer service+price, not % discounts): {[o.get('title') for o in offer_catalog[:5]]}
SEASONAL BEAT (current): {seasonal or 'none'}

TRIGGER COMPOSITION FRAME FOR KIND="{kind}":
{frame}
{digest_block}

{ANTI_PATTERNS}

{COMPULSION_LEVERS}

RETURN ONLY valid JSON — no markdown fences, no explanation:
{{
  "body": "<the exact ready-to-send message — no URL, no filler opener>",
  "cta": "open_ended" | "binary_yes_stop" | "binary_yes_no" | "binary_confirm_cancel" | "multi_choice_slot" | "none",
  "send_as": "vera" | "merchant_on_behalf",
  "suppression_key": "<copy from trigger.suppression_key>",
  "rationale": "<1-2 sentences: exactly which signal drove this + why it fits this specific merchant>",
  "template_name": "<vera_kindname_v1>",
  "template_params": ["param1", "param2", "param3"]
}}"""

    user = f"""COMPOSE for this exact context:

=== LAYER 1: CATEGORY ===
{cat_slug} | tone: {tone}
Catalog (use these exact titles): {[o.get('title') for o in offer_catalog[:4]]}
Current seasonal beat: {seasonal or 'none'}

=== LAYER 2: MERCHANT ===
Name: {name}{f'  (Owner first name: {owner})' if owner else ''}
Location: {locality}, {city} | Languages: {languages}
Subscription: status={sub.get('status')} plan={sub.get('plan')} days_remaining={sub.get('days_remaining')} days_since_expiry={sub.get('days_since_expiry')}
Performance (30d): views={perf.get('views')} calls={perf.get('calls')} ctr={perf.get('ctr')} directions={perf.get('directions')} leads={perf.get('leads')}
7d delta: views {perf.get('delta_7d', {}).get('views_pct', 0):+.0%} | calls {perf.get('delta_7d', {}).get('calls_pct', 0):+.0%}
Active offers: {[o.get('title') for o in active_offers] or 'NONE — this is a gap'}
Derived signals: {signals}
Customer aggregate: {json.dumps(cust_agg)}
Review themes: {[(t.get('theme'), t.get('sentiment'), f"{t.get('occurrences_30d')}x/30d") for t in review_themes]}
Last Vera engagement: {(last_turn.get('ts', '?') + ' | ' + last_turn.get('engagement', '?')) if last_turn else 'none recorded'}

=== LAYER 3: TRIGGER ===
kind: {kind} | scope: {trigger.get('scope')} | source: {trigger.get('source')} | urgency: {trigger.get('urgency')}/5
suppression_key: {trigger.get('suppression_key', '')}
expires_at: {trigger.get('expires_at', '')}
payload: {json.dumps(trigger.get('payload', {}), indent=2)}
{customer_block}

Produce the JSON now."""

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

        # Enforce customer scope
        if is_customer_scope and customer:
            result["send_as"] = "merchant_on_behalf"

        # Fallback suppression key
        if not result.get("suppression_key"):
            from datetime import datetime
            week = datetime.now().strftime("%Y-W%W")
            result["suppression_key"] = f"{kind}:{merchant_id}:{week}"

        result.setdefault("template_name", f"vera_{kind}_v1")
        result.setdefault("template_params", [owner or name, kind, result.get("body", "")[:80]])
        return result

    except Exception as e:
        from datetime import datetime
        week = datetime.now().strftime("%Y-W%W")
        return {
            "body": f"{owner or name}, ek important update hai — {kind.replace('_',' ')} signal mila abhi. Reply YES to see details.",
            "cta": "binary_yes_stop",
            "send_as": "vera" if not is_customer_scope else "merchant_on_behalf",
            "suppression_key": trigger.get("suppression_key") or f"{kind}:{merchant_id}:{week}",
            "rationale": f"[fallback] compose error: {str(e)[:80]}",
            "template_name": f"vera_{kind}_v1",
            "template_params": [owner or name, kind, ""],
        }


# ── Multi-turn conversation handler ──────────────────────────────────────────

@dataclass
class ConversationState:
    conversation_id: str
    merchant: dict = field(default_factory=dict)
    customer: Optional[dict] = None
    category: dict = field(default_factory=dict)
    trigger: dict = field(default_factory=dict)
    history: list = field(default_factory=list)  # [{"role": "vera"|"merchant", "content": str}]
    auto_reply_count: int = 0
    suppressed: bool = False
    intent_committed: bool = False


AUTO_REPLY_SIGNALS = [
    "thank you for contacting", "our team will respond", "automated assistant",
    "aapki jaankari ke liye", "team tak pahuncha", "main ek automated",
    "i am currently unavailable", "will get back", "out of office", "auto-reply",
]

def respond(state: ConversationState, merchant_message: str) -> dict:
    """
    Multi-turn reply handler.
    Returns {action: "send"|"wait"|"end", body?, cta?, rationale}
    """
    text = merchant_message.lower().strip()

    # 1. Auto-reply detection
    if any(s in text for s in AUTO_REPLY_SIGNALS):
        state.auto_reply_count += 1
        if state.auto_reply_count == 1:
            name = state.merchant.get("identity", {}).get("owner_first_name") or \
                   state.merchant.get("identity", {}).get("name", "")
            return {
                "action": "send",
                "body": f"{name} — looks like an auto-reply 😊 Just one quick thing when you're free. Reply YES to continue.",
                "cta": "binary_yes_stop",
                "rationale": "First auto-reply detected — one probe for the real owner.",
            }
        elif state.auto_reply_count == 2:
            return {
                "action": "wait",
                "wait_seconds": 86400,
                "rationale": f"Auto-reply {state.auto_reply_count}× — owner not at phone. Backing off 24h.",
            }
        else:
            state.suppressed = True
            return {
                "action": "end",
                "rationale": f"Auto-reply {state.auto_reply_count}× with no real reply. Closing.",
            }

    # 2. Hostile
    hostile = ["spam", "useless", "annoying", "stop sending", "stop messaging",
               "bothering me", "remove me", "block", "rubbish", "bakwas"]
    if any(s in text for s in hostile):
        state.suppressed = True
        return {
            "action": "end",
            "rationale": "Merchant expressed frustration — closing conversation to avoid spam perception.",
        }

    # 3. Hard rejection
    reject = ["not interested", "no thanks", "nahi chahiye", "band karo", "mat bhejo",
              "don't message", "dont message", "stop", "nope", "later nahi"]
    if any(s in text for s in reject):
        state.suppressed = True
        return {
            "action": "send",
            "body": "No problem! Jab bhi zaroorat ho, reply kar dena. 🙏",
            "cta": "none",
            "rationale": "Merchant declined — graceful exit with re-entry hook.",
        }

    # 4. Explicit intent transition → switch to ACTION mode immediately
    commitment_signals = [
        "let's do it", "lets do it", "ok do it", "go ahead", "proceed",
        "what's next", "whats next", "confirm", "yes let's", "haan karo",
        "chalo karo", "send it", "do it now",
    ]
    if any(s in text for s in commitment_signals) or state.intent_committed:
        state.intent_committed = True
        kind = state.trigger.get("kind", "")
        active_offers = [o for o in state.merchant.get("offers", []) if o.get("status") == "active"]
        offer = active_offers[0].get("title") if active_offers else "your offer"
        cust_agg = state.merchant.get("customer_aggregate", {})
        count = cust_agg.get("high_risk_adult_count") or cust_agg.get("total_active_members") or cust_agg.get("chronic_rx_count") or cust_agg.get("total_unique_ytd", "your")

        action_responses = {
            "research_digest": f"Drafting the patient-ed WhatsApp now — targeting your {count} high-risk patients. Also scheduling the GBP post for tomorrow 10am. Reply CONFIRM to send.",
            "perf_spike": f"Activating '{offer}' and drafting a WhatsApp push to your customer list ({count} contacts). Reply CONFIRM to proceed.",
            "perf_dip": f"Drafting the recovery push using '{offer}' — ready in 60 seconds. I'll also flag the 3 quick profile fixes. Reply CONFIRM.",
            "festival_upcoming": f"Drafting the WhatsApp campaign + GBP post for your {count} contacts now. Ready in 5 min — reply CONFIRM to send.",
            "supply_alert": f"Pulling the affected batch customer list ({count} chronic-Rx customers) and drafting their notice now. Ready in 2 min. Reply CONFIRM.",
            "active_planning_intent": f"Finalizing the plan now — I'll have a ready-to-share version in 60 seconds. Reply CONFIRM.",
        }
        body = action_responses.get(kind, f"On it! Executing now for {count} customers. Reply CONFIRM to send, or STOP to cancel.")
        return {
            "action": "send",
            "body": body,
            "cta": "binary_confirm_cancel",
            "rationale": "Merchant committed to action — switched from qualifying to executing immediately (no more qualifying questions).",
        }

    # 5. Simple accept (YES/OK)
    accept = ["yes", "haan", "ha", "ok", "okay", "sure", "sounds good", "do it",
              "1", "2", "send", "agree", "perfect"]
    if any(text == s or text.startswith(s + " ") or text.startswith(s + ",") for s in accept):
        # Deliver promised artifact
        kind = state.trigger.get("kind", "")
        name = state.merchant.get("identity", {}).get("owner_first_name") or \
               state.merchant.get("identity", {}).get("name", "")
        active_offers = [o for o in state.merchant.get("offers", []) if o.get("status") == "active"]
        offer = active_offers[0].get("title") if active_offers else "your setup"
        return {
            "action": "send",
            "body": f"Great, {name}! Drafting now — will share in ~60 seconds. I'll start with the {kind.replace('_',' ')} action we discussed using '{offer}'. Reply STOP to cancel anytime.",
            "cta": "none",
            "rationale": "Merchant accepted — delivering promised artifact/action.",
        }

    # 6. Out-of-scope question
    scope_out = ["gst", "income tax", "itr", "legal advice", "insurance claim",
                 "bank loan", "marriage", "personal problem"]
    if any(s in text for s in scope_out):
        last_topic = state.history[-2]["content"][:60] if len(state.history) >= 2 else "the earlier topic"
        return {
            "action": "send",
            "body": f"That's outside what I can help with — your CA or a lawyer would be better placed. Coming back to what we were discussing: {last_topic}... shall I continue with that?",
            "cta": "binary_yes_stop",
            "rationale": "Out-of-scope request declined politely; redirected to on-mission thread.",
        }

    # 7. Clarifying question — answer with context
    if "?" in text:
        kind = state.trigger.get("kind", "")
        active_offers = [o for o in state.merchant.get("offers", []) if o.get("status") == "active"]
        offer_titles = [o.get("title") for o in active_offers]
        perf = state.merchant.get("performance", {})
        peer = state.category.get("peer_stats", {})

        try:
            system = f"""You are Vera, magicpin merchant AI. Answer in {state.category.get('voice', {}).get('tone','professional')} tone.
Use ONLY the provided data. Under 2 sentences + 1 CTA. No fabrication."""
            context_str = f"""Active offers: {offer_titles}
Performance: views={perf.get('views')} calls={perf.get('calls')} ctr={perf.get('ctr')}
Peer avg_ctr={peer.get('avg_ctr')} avg_rating={peer.get('avg_rating')}
Trigger: {json.dumps(state.trigger.get('payload', {}))}
Merchant signals: {state.merchant.get('signals', [])}
Question: {merchant_message}"""
            resp = _client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                max_tokens=200,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": context_str}],
                temperature=0,
            )
            answer = resp.choices[0].message.content.strip()
        except Exception:
            answer = f"Based on your current setup — {offer_titles[0] if offer_titles else 'your active offers'} would be the best fit here. Want me to proceed with that?"

        return {
            "action": "send",
            "body": answer,
            "cta": "open_ended",
            "rationale": "Merchant asked a question — answered with context-grounded facts only.",
        }

    # 8. Ambiguous — re-engage cleanly
    name = state.merchant.get("identity", {}).get("owner_first_name") or \
           state.merchant.get("identity", {}).get("name", "")
    return {
        "action": "send",
        "body": f"Got it, {name}. Quick check — shall I go ahead with what I proposed? Reply YES to confirm, or tell me what you'd prefer.",
        "cta": "binary_yes_stop",
        "rationale": "Ambiguous reply — re-asking with a clear binary to move the conversation forward.",
    }

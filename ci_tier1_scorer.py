#!/usr/bin/env python3
"""
CI Tier1 Scorer — Circle Intelligence Reverse-Engineered Signal Engine
========================================================================
Replicates CI's scoring algorithm from reverse-engineered dashboard:
  - CI Score 0-100 across 5 categories (+20 each)
  - Tier1 = Score 75+ AND all safety filters passed
  - Tier2 = Score 50-74 (good momentum, moderate quality)
  - Tier3 = Score <50 (early stage, high risk)

Score Components (from CI dashboard HTML):
  1. Momentum    (+20) — price change, volume velocity
  2. Liquidity   (+20) — liquidity depth, mcap ratio
  3. Holder Quality (+20) — smart degens, renowned, bluechip
  4. Narrative   (+20) — CTO, hot level, trending meta
  5. Rug Risk    (-20) — deductions for rug indicators

Usage:
  python3 ci_tier1_scorer.py          # One-shot scan + print
  python3 ci_tier1_scorer.py --loop   # Continuous 60s loop
  python3 ci_tier1_scorer.py --json   # JSON output for bot integration
"""

import json
import time
import sys
import requests
from datetime import datetime

# =============================================================================
# CONFIG
# =============================================================================
CI_BASE = "https://circleintelligence.in"
COOKIE_FILE = "/tmp/ci_cookies.txt"
POLL_INTERVAL = 60  # seconds (matches CI's own refresh rate)

# Tier thresholds (from CI dashboard: Tier1 = 75+, Tier2 = moderate, Tier3 = early)
TIER1_THRESHOLD = 80
TIER2_THRESHOLD = 50

# Minimum liquidity for Tier1 (hard gate) — $20K eliminates death-trap tokens
MIN_LIQUIDITY = 20000

# Safety filter hard gates (must ALL pass for Tier1 regardless of score)
SAFETY_GATES = {
    "burnStatus": None,        # LP burnt is ideal, but not strictly required for Tier1 now
    "rugRatio_max": 5,           # rug ratio must be <5%
    "bundler_max": 40,           # bundler must be <40%
    "top10_max": 0.50,           # top10 holders must be <50%
    "isWashTrading": False,      # no wash trading
    "minLiquidity": MIN_LIQUIDITY,  # minimum liquidity in USD
}


# =============================================================================
# AUTH — Cookie-based session (same as CI dashboard)
# =============================================================================
def load_cookies():
    """Load cookies from curl cookie jar file."""
    cookies = {}
    try:
        with open(COOKIE_FILE, "r") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.strip().split("\t")
                if len(parts) >= 7:
                    cookies[parts[5]] = parts[6]
    except FileNotFoundError:
        pass
    return cookies


def login(email, password):
    """Login to CI and save cookies."""
    resp = requests.post(f"{CI_BASE}/auth/login", json={"email": email, "password": password}, timeout=10)
    if resp.status_code == 200:
        data = resp.json()
        token = data.get("token", "")
        # Save as cookie jar
        with open(COOKIE_FILE, "w") as f:
            f.write(f".{CI_BASE.replace('https://','')}\tTRUE\t/\tTRUE\t0\tci_token\t{token}\n")
        return token
    return None


# =============================================================================
# DATA FETCH
# =============================================================================
def fetch_ci_data():
    """Fetch all token data from CI API endpoints."""
    cookies = load_cookies()
    headers = {"Authorization": f"Bearer {cookies.get('ci_token', '')}"} if cookies.get("ci_token") else {}
    session = requests.Session()
    session.cookies.update(cookies)

    result = {
        "signals": [],
        "buzz": {},
        "stats": {},
        "timestamp": datetime.utcnow().isoformat(),
    }

    # Fetch /api/data (main signals)
    try:
        resp = session.get(f"{CI_BASE}/api/data", headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            # Merge all tiers into flat list with tier tag
            for t in data.get("tier1Alerts", []):
                t["_ciTier"] = "TIER1"
            for t in data.get("tier2Watches", []):
                t["_ciTier"] = "TIER2"
            for t in data.get("tier3Watches", []):
                t["_ciTier"] = "TIER3"
            result["signals"] = data.get("tier1Alerts", []) + data.get("tier2Watches", []) + data.get("tier3Watches", [])
            result["stats"] = data.get("stats", {})
    except Exception as e:
        print(f"[ERROR] fetch /api/data: {e}", file=sys.stderr)

    # Fetch /api/buzz (hot/kol/smart/cto tokens)
    try:
        resp = session.get(f"{CI_BASE}/api/buzz", headers=headers, timeout=10)
        if resp.status_code == 200:
            result["buzz"] = resp.json()
    except Exception as e:
        print(f"[ERROR] fetch /api/buzz: {e}", file=sys.stderr)

    return result


# =============================================================================
# CI SCORE ENGINE — 5 categories, 0-100
# =============================================================================
def score_momentum(token):
    """
    MOMENTUM (+20 max)
    -----------------
    Based on 1h and 5m price changes.
    CI values strong upward momentum as primary signal.
    """
    change_1h = token.get("change1h", 0) or 0
    change_5m = token.get("change5m", 0) or 0

    # 1h momentum scoring (0-14 points)
    if change_1h >= 500:
        s_1h = 14
    elif change_1h >= 300:
        s_1h = 12
    elif change_1h >= 100:
        s_1h = 9
    elif change_1h >= 50:
        s_1h = 6
    elif change_1h >= 20:
        s_1h = 4
    elif change_1h >= 0:
        s_1h = 2
    else:
        # Negative momentum — still gets 0-1 based on severity
        s_1h = 1 if change_1h > -20 else 0

    # 5m momentum scoring (0-6 points) — short-term velocity
    if change_5m >= 20:
        s_5m = 6
    elif change_5m >= 10:
        s_5m = 5
    elif change_5m >= 5:
        s_5m = 4
    elif change_5m >= 0:
        s_5m = 2
    else:
        s_5m = 1 if change_5m > -10 else 0

    return min(s_1h + s_5m, 20)


def score_liquidity(token):
    """
    LIQUIDITY (+20 max)
    ------------------
    Liquidity depth relative to mcap. Higher liq = safer exit.
    CI values tokens with meaningful liquidity floors.
    """
    liq = token.get("liquidity", 0) or 0
    mcap = token.get("mcap", 0) or 1

    # Liquidity absolute scoring (0-12 points)
    if liq >= 20000:
        s_abs = 12
    elif liq >= 10000:
        s_abs = 9
    elif liq >= 5000:
        s_abs = 6
    elif liq >= 2000:
        s_abs = 4
    else:
        s_abs = 1

    # Liquidity-to-mcap ratio (0-8 points) — depth quality
    liq_ratio = liq / max(mcap, 1)
    if liq_ratio >= 0.5:
        s_ratio = 8
    elif liq_ratio >= 0.3:
        s_ratio = 6
    elif liq_ratio >= 0.15:
        s_ratio = 4
    elif liq_ratio >= 0.05:
        s_ratio = 2
    else:
        s_ratio = 0

    return min(s_abs + s_ratio, 20)


def score_holder_quality(token):
    """
    HOLDER QUALITY (+20 max)
    -----------------------
    Smart money + renowned trader presence. CI's core alpha signal.
    This is the MOST differentiating factor between tiers.
    """
    smart = token.get("smartDegenCount", 0) or 0
    renowned = token.get("renownedCount", 0) or 0
    bluechip = token.get("bluechipOwner", 0) or 0
    holders = token.get("holders", 0) or 0

    # Smart degen scoring (0-8 points)
    if smart >= 8:
        s_smart = 8
    elif smart >= 5:
        s_smart = 7
    elif smart >= 3:
        s_smart = 5
    elif smart >= 1:
        s_smart = 3
    else:
        s_smart = 0

    # Renowned trader scoring (0-7 points)
    if renowned >= 5:
        s_ren = 7
    elif renowned >= 3:
        s_ren = 6
    elif renowned >= 2:
        s_ren = 4
    elif renowned >= 1:
        s_ren = 2
    else:
        s_ren = 0

    # Bluechip owner bonus (0-3 points)
    s_blue = min(bluechip * 1, 3)

    # Holder count bonus (0-2 points)
    if holders >= 500:
        s_holders = 2
    elif holders >= 200:
        s_holders = 1
    else:
        s_holders = 0

    return min(s_smart + s_ren + s_blue + s_holders, 20)


def score_narrative(token):
    """
    NARRATIVE (+20 max)
    -------------------
    CTO flag (organic), hot level (trending), buy/sell ratio.
    CI values community-driven narratives over dev-pushed tokens.
    """
    cto = token.get("ctoFlag", 0) or 0
    hot = token.get("hotLevel", 0) or 0
    buys = token.get("buys", 0) or 0
    sells = token.get("sells", 0) or 0
    no_mint = token.get("noMint", False)
    no_freeze = token.get("noFreeze", False)

    # CTO scoring (0-8 points) — community takeover = organic
    s_cto = 8 if cto == 1 else 0

    # Hot level scoring (0-5 points)
    s_hot = min(hot * 2, 5) if hot > 0 else 0

    # Buy/sell ratio (0-4 points) — buying pressure
    if sells > 0:
        bs_ratio = buys / sells
    else:
        bs_ratio = buys / 1 if buys > 0 else 0

    if bs_ratio >= 2.0:
        s_bs = 4
    elif bs_ratio >= 1.5:
        s_bs = 3
    elif bs_ratio >= 1.0:
        s_bs = 2
    else:
        s_bs = 0

    # No-mint / no-freeze bonus (0-3 points)
    s_safety = 0
    if no_mint:
        s_safety += 1.5
    if no_freeze:
        s_safety += 1.5

    return min(s_cto + s_hot + s_bs + s_safety, 20)


def score_rug_risk(token):
    """
    RUG RISK (-20 max deduction, 0 = no penalty)
    -------------------------------------------
    Deductions for rug indicators. Starts at 0 (no penalty).
    Each risk factor deducts points.
    """
    penalty = 0

    # Burn status (BIGGEST factor — unburnt LP = -8)
    if token.get("burnStatus") != "burn":
        penalty += 8

    # Rug ratio (0-6 penalty)
    rug = token.get("rugRatio", 0) or 0
    if rug >= 50:
        penalty += 6
    elif rug >= 20:
        penalty += 4
    elif rug >= 5:
        penalty += 2
    elif rug > 0:
        penalty += 1

    # Bundler % (0-4 penalty)
    bundler = token.get("bundler", 0) or 0
    if bundler >= 40:
        penalty += 4
    elif bundler >= 30:
        penalty += 3
    elif bundler >= 20:
        penalty += 1

    # Top10 concentration (0-3 penalty)
    top10 = token.get("top10", 0) or 0
    # top10 is stored as decimal (0.25 = 25%)
    if top10 >= 0.40:
        penalty += 3
    elif top10 >= 0.30:
        penalty += 2
    elif top10 >= 0.25:
        penalty += 1

    # Sniper count (0-2 penalty)
    snipers = token.get("sniperCount", 0) or 0
    if snipers >= 50:
        penalty += 2
    elif snipers >= 30:
        penalty += 1

    # Wash trading (0-2 penalty)
    if token.get("isWashTrading"):
        penalty += 2

    # CTO=0 adds small penalty (dev still controls)
    if not token.get("ctoFlag"):
        penalty += 1

    return min(penalty, 20)


def compute_ci_score(token):
    """
    Compute full CI Score (0-100) for a token.
    
    Formula:
      score = momentum + liquidity + holder_quality + narrative - rug_risk
      Clamped to [0, 100]
    """
    s_mom = score_momentum(token)
    s_liq = score_liquidity(token)
    s_hq = score_holder_quality(token)
    s_nar = score_narrative(token)
    s_rug = score_rug_risk(token)

    raw = s_mom + s_liq + s_hq + s_nar - s_rug
    score = max(0, min(100, raw))

    breakdown = {
        "momentum": s_mom,
        "liquidity": s_liq,
        "holderQuality": s_hq,
        "narrative": s_nar,
        "rugRisk": -s_rug,
    }

    return score, breakdown


def check_safety_gates(token):
    """
    Hard safety filters that MUST pass for Tier1.
    Even if score is 75+, failing any gate drops to Tier2.
    """
    failures = []

    # Burn status (only penalize if explicitly NOT 'burn' AND we require it)
    if SAFETY_GATES["burnStatus"] == "burn" and token.get("burnStatus") != "burn":
        failures.append("LP not burnt")

    rug = token.get("rugRatio", 0) or 0
    if rug >= SAFETY_GATES["rugRatio_max"]:
        failures.append(f"Rug ratio {rug:.1f}% >= {SAFETY_GATES['rugRatio_max']}%")

    bundler = token.get("bundler", 0) or 0
    if bundler >= SAFETY_GATES["bundler_max"]:
        failures.append(f"Bundler {bundler:.1f}% >= {SAFETY_GATES['bundler_max']}%")

    top10 = token.get("top10", 0) or 0
    if top10 >= SAFETY_GATES["top10_max"]:
        failures.append(f"Top10 {top10*100:.1f}% >= {SAFETY_GATES['top10_max']*100:.0f}%")

    if token.get("isWashTrading", False) != SAFETY_GATES["isWashTrading"]:
        failures.append("Wash trading detected")

    # Liquidity gate — hard floor for Tier1
    liq = token.get("liquidity", 0) or 0
    min_liq = SAFETY_GATES.get("minLiquidity", 0)
    if liq < min_liq:
        failures.append(f"Liquidity {liq:,.0f} < {min_liq:,.0f}")

    return failures


def classify_tier(score, safety_failures):
    """
    Classify token into tier based on score + safety gates.
    Tier1 = 75+ AND no safety failures
    Tier2 = 50-74 OR 75+ with safety failures
    Tier3 = <50
    """
    if score >= TIER1_THRESHOLD and len(safety_failures) == 0:
        return "TIER1"
    elif score >= TIER2_THRESHOLD:
        return "TIER2"
    else:
        return "TIER3"


# =============================================================================
# BUZZ BOOST — Cross-reference with /api/buzz
# =============================================================================
def compute_buzz_boost(token, buzz_data):
    """
    Additional scoring boost from buzz cross-references.
    Tokens appearing in multiple buzz categories are stronger signals.
    Returns bonus score (0-10) and which categories matched.
    """
    address = token.get("address", "")
    boost = 0
    categories = []

    if not buzz_data or not address:
        return 0, []

    # Check each buzz category
    for cat_name, cat_key in [("Hot", "hotTokens"), ("KOL", "kolTokens"), ("Smart", "smartTokens"), ("CTO", "ctoTokens")]:
        cat_tokens = buzz_data.get(cat_key, [])
        for bt in cat_tokens:
            if bt.get("address") == address:
                boost += 3
                categories.append(cat_name)
                break

    # Trending meta boost
    for meta in buzz_data.get("trendingMeta", []):
        # If token symbol matches trending narrative (indirect)
        pass

    return min(boost, 10), categories


# =============================================================================
# FULL PIPELINE
# =============================================================================
def score_all_tokens(ci_data=None):
    """
    Fetch CI data and score all tokens.
    Returns list of scored tokens sorted by score descending.
    """
    if ci_data is None:
        ci_data = fetch_ci_data()

    signals = ci_data.get("signals", [])
    buzz = ci_data.get("buzz", {})

    scored = []
    for token in signals:
        score, breakdown = compute_ci_score(token)
        safety_failures = check_safety_gates(token)
        tier = classify_tier(score, safety_failures)
        buzz_boost, buzz_cats = compute_buzz_boost(token, buzz)

        # Final score with buzz boost (capped at 100)
        final_score = min(100, score + buzz_boost)

        # Re-classify with boosted score
        final_tier = classify_tier(final_score, safety_failures)

        scored.append({
            "symbol": token.get("symbol", "?"),
            "address": token.get("address", ""),
            "ciScore": final_score,
            "baseScore": score,
            "buzzBoost": buzz_boost,
            "tier": final_tier,
            "ciOriginalTier": token.get("_ciTier", "UNKNOWN"),
            "safetyFailures": safety_failures,
            "breakdown": breakdown,
            "buzzCategories": buzz_cats,
            # Key metrics for display
            "mcap": token.get("mcap", 0),
            "liquidity": token.get("liquidity", 0),
            "holders": token.get("holders", 0),
            "burnStatus": token.get("burnStatus", "none"),
            "top10": token.get("top10", 0),
            "bundler": token.get("bundler", 0),
            "ctoFlag": token.get("ctoFlag", 0),
            "smartDegenCount": token.get("smartDegenCount", 0),
            "renownedCount": token.get("renownedCount", 0),
            "sniperCount": token.get("sniperCount", 0),
            "rugRatio": token.get("rugRatio", 0),
            "hotLevel": token.get("hotLevel", 0),
            "change1h": token.get("change1h", 0),
            "change5m": token.get("change5m", 0),
            "age": token.get("age", 0),
        })

    # Sort by score descending
    scored.sort(key=lambda x: x["ciScore"], reverse=True)
    return scored


def get_tier1_signals(scored_tokens=None):
    """Filter only Tier1 signals from scored tokens."""
    if scored_tokens is None:
        scored_tokens = score_all_tokens()
    return [t for t in scored_tokens if t["tier"] == "TIER1"]


# =============================================================================
# DISPLAY
# =============================================================================
def score_color(score):
    """CI-style color coding."""
    if score <= 15:
        return "🔴"
    elif score <= 25:
        return "🟠"
    elif score <= 35:
        return "🟠"
    elif score <= 45:
        return "🟡"
    elif score <= 55:
        return "🟡"
    else:
        return "🟢"


def tier_emoji(tier):
    if tier == "TIER1":
        return "🔥"
    elif tier == "TIER2":
        return "⚡"
    else:
        return "👀"


def fmt_pct(val):
    if val is None:
        return "--"
    if abs(val) >= 100:
        return f"{val:.0f}%"
    return f"{val:.1f}%"


def fmt_usd(val):
    if val is None or val == 0:
        return "$0"
    if val >= 1_000_000:
        return f"${val/1_000_000:.1f}M"
    if val >= 1_000:
        return f"${val/1_000:.1f}K"
    return f"${val:.0f}"


def print_scored_tokens(tokens, show_tier1_only=False):
    """Pretty print scored tokens."""
    if show_tier1_only:
        tokens = [t for t in tokens if t["tier"] == "TIER1"]
        label = "🔥 TIER 1 SIGNALS"
    else:
        label = "ALL SCORED TOKENS"

    print(f"\n{'='*80}")
    print(f"  {label} — {datetime.utcnow().strftime('%H:%M:%S')} UTC")
    print(f"{'='*80}")

    if not tokens:
        print("  No tokens found.")
        return

    for t in tokens:
        emoji = tier_emoji(t["tier"])
        color = score_color(t["ciScore"])
        bd = t["breakdown"]

        print(f"\n{emoji} {t['symbol']}  —  CI Score: {color} {t['ciScore']}/100  [{t['tier']}]")
        if t["buzzBoost"] > 0:
            print(f"   🐝 Buzz Boost: +{t['buzzBoost']} ({', '.join(t['buzzCategories'])})")
        if t["safetyFailures"]:
            print(f"   ⚠️  Safety Failures: {', '.join(t['safetyFailures'])}")
        print(f"   📊 Breakdown: Mom={bd['momentum']:>2} Liq={bd['liquidity']:>2} HoldQ={bd['holderQuality']:>2} Nar={bd['narrative']:>2} Rug={bd['rugRisk']:>2}")
        print(f"   💰 MC: {fmt_usd(t['mcap'])}  Liq: {fmt_usd(t['liquidity'])}  Holders: {t['holders']}")
        print(f"   🔥 1h: {fmt_pct(t['change1h'])}  5m: {fmt_pct(t['change5m'])}  Age: {t['age']}m")
        print(f"   🛡️  Burn: {t['burnStatus']}  Top10: {t['top10']*100:.1f}%  Bundler: {t['bundler']:.1f}%  Rug: {t['rugRatio']:.1f}%")
        print(f"   🧠 Smart: {t['smartDegenCount']}  Renowned: {t['renownedCount']}  Snipers: {t['sniperCount']}  CTO: {t['ctoFlag']}  Hot: {t['hotLevel']}")
        print(f"   📮 {t['address'][:20]}...{t['address'][-8:]}")


def print_summary(tokens):
    """Print tier distribution summary."""
    t1 = [t for t in tokens if t["tier"] == "TIER1"]
    t2 = [t for t in tokens if t["tier"] == "TIER2"]
    t3 = [t for t in tokens if t["tier"] == "TIER3"]

    print(f"\n{'='*40}")
    print(f"  📊 TIER DISTRIBUTION")
    print(f"{'='*40}")
    print(f"  🔥 TIER1: {len(t1)} tokens (score ≥{TIER1_THRESHOLD}, all safety gates passed)")
    print(f"  ⚡ TIER2: {len(t2)} tokens (score ≥{TIER2_THRESHOLD})")
    print(f"  👀 TIER3: {len(t3)} tokens (score <{TIER2_THRESHOLD})")
    print(f"  📦 Total: {len(tokens)} tokens scanned")

    if t1:
        print(f"\n  🔥 TIER1 ADDRESSES (for auto-trade):")
        for t in t1:
            print(f"     {t['symbol']}: {t['address']}")


# =============================================================================
# MAIN
# =============================================================================
if __name__ == "__main__":
    loop_mode = "--loop" in sys.argv
    json_mode = "--json" in sys.argv
    tier1_only = "--tier1" in sys.argv

    if json_mode:
        # JSON output for bot integration
        tokens = score_all_tokens()
        if tier1_only:
            tokens = [t for t in tokens if t["tier"] == "TIER1"]
        print(json.dumps(tokens, indent=2))
    elif loop_mode:
        # Continuous loop — 60s intervals
        print("🔄 CI Tier1 Scorer — Continuous Mode (60s interval)")
        print("Press Ctrl+C to stop\n")
        try:
            while True:
                try:
                    tokens = score_all_tokens()
                    print_scored_tokens(tokens, show_tier1_only=tier1_only)
                    print_summary(tokens)
                    print(f"\n⏰ Next scan in {POLL_INTERVAL}s...")
                except Exception as e:
                    print(f"[ERROR] {e}")
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        # One-shot scan
        tokens = score_all_tokens()
        print_scored_tokens(tokens, show_tier1_only=tier1_only)
        print_summary(tokens)

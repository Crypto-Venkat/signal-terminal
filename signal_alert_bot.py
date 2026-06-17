#!/usr/bin/env python3
"""
Real-time CI Signal Alert Bot
Sends individual TIER1/TIER2 token alerts to Telegram channel as they appear.
Exact format matching Axiom Trades channel style.
"""
import json
import urllib.request
import requests
import time
import os
from pathlib import Path
from datetime import datetime

# Config
NGROK_URL = "https://signal-terminal.duckdns.org"
BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_PROXY = os.getenv("TG_PROXY")  # SOCKS5 proxy for Telegram (India ban bypass)
CHAT_ID = "-1004296309055"
SENT_FILE = "/home/ubuntu/meme/sent_tokens.json"
SCAN_INTERVAL = 30  # seconds

# Position sizing config (for 2 SOL ≈ $132 capital)
TOTAL_CAPITAL_USD = 132  # 2 SOL @ $66
MAX_RISK_PER_TRADE_PCT = 0.05   # 5% max risk per trade
MAX_POSITION_PCT = 0.10          # 10% max position size
SL_PCT = 0.30                    # 30% stop loss
TP1_PCT = 2.0                    # Take profit 1: 2x (sell 50%)
TP2_PCT = 5.0                    # Take profit 2: 5x (sell rest)

if not BOT_TOKEN:
    raise RuntimeError("TG_BOT_TOKEN env var not set — get token from @BotFather")

def calculate_position_size(token):
    """Calculate recommended position size based on capital and risk params."""
    mcap = token.get('mcap', 0) or 0
    liquidity = token.get('liquidity', 0) or 0
    
    # Max position in USD
    max_pos_usd = TOTAL_CAPITAL_USD * MAX_POSITION_PCT  # $13.20
    # Max risk in USD (position * SL)
    max_risk_usd = TOTAL_CAPITAL_USD * MAX_RISK_PER_TRADE_PCT  # $6.60
    
    # Position size limited by risk (position * SL <= max_risk)
    # So position <= max_risk / SL = $6.60 / 0.30 = $22
    # But also capped at max_pos_usd = $13.20
    position_usd = min(max_pos_usd, max_risk_usd / SL_PCT)
    
    # Convert to SOL
    sol_price = 66  # approximate
    position_sol = position_usd / sol_price
    
    # Round to reasonable increments
    if position_sol < 0.01:
        position_sol = 0.01
    elif position_sol < 0.05:
        position_sol = round(position_sol, 2)
    else:
        position_sol = round(position_sol, 1)
    
    # Cap at max position
    max_pos_sol = max_pos_usd / sol_price
    if position_sol > max_pos_sol:
        position_sol = max_pos_sol
    
    return {
        "position_sol": position_sol,
        "position_usd": position_usd,
        "max_risk_usd": max_risk_usd,
        "sl_pct": SL_PCT,
        "tp1_pct": TP1_PCT,
        "tp2_pct": TP2_PCT,
    }

def load_sent():
    """Load already-sent token addresses with metadata."""
    try:
        with open(SENT_FILE, 'r') as f:
            data = json.load(f)
        # Backward compatibility: if list of strings, convert to dict
        if data and isinstance(data[0], str):
            return {addr: {"address": addr, "symbol": "", "entry_mcap": 0, "sent_at": None} for addr in data}
        return {item["address"]: item for item in data}
    except:
        return {}

def save_sent(sent_dict):
    """Save sent token addresses with metadata."""
    # Convert dict to list for JSON storage
    sent_list = list(sent_dict.values())
    with open(SENT_FILE, 'w') as f:
        json.dump(sent_list, f)

def send_telegram(text, reply_markup=None):
    """Send message to Telegram channel with optional inline keyboard."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    proxies = None
    if TG_PROXY:
        proxies = {"http": TG_PROXY, "https": TG_PROXY}
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(url, json=payload, timeout=10, proxies=proxies)
        return resp.json().get("ok", False)
    except Exception as e:
        print(f"Telegram error: {e}")
        return False

def build_action_buttons(address):
    """Build inline keyboard with action buttons matching the exact format."""
    return {
        "inline_keyboard": [
            [
                {"text": "⚡ FAST BUY", "url": f"https://axiom.trade/t/{address}"},
                {"text": "🌐 GMGN.AI (WEB)", "url": f"https://gmgn.ai/sol/token/{address}"}
            ],
            [
                {"text": "🤖 GMGN.AI (BOT)", "url": f"https://t.me/gmgn_sol_bot?start={address}"},
                {"text": "📈 AXIOM", "url": f"https://axiom.trade/t/{address}"}
            ]
        ]
    }

def format_currency(val):
    """Format USD value with K/M/B suffixes."""
    if val is None or val == 0:
        return "$0"
    if val >= 1_000_000_000:
        return f"${val/1_000_000_000:.2f}B"
    if val >= 1_000_000:
        return f"${val/1_000_000:.2f}M"
    if val >= 1_000:
        return f"${val/1_000:.1f}K"
    return f"${val:.0f}"

def format_pct(val, decimals=1):
    """Format percentage."""
    if val is None:
        return "—"
    if val >= 100:
        return f"{val:.0f}%"
    return f"{val:.{decimals}f}%"

def format_num(val, decimals=0):
    """Format number with K/M suffixes."""
    if val is None or val == 0:
        return "0"
    if val >= 1_000_000:
        return f"{val/1_000_000:.1f}M"
    if val >= 1_000:
        return f"{val/1_000:.1f}K"
    if decimals > 0:
        return f"{val:.{decimals}f}"
    return f"{val:.0f}"

def get_twitter_data(token):
    """Fetch Twitter data for token. Returns dict with followers, following, handle."""
    # Try to get from token data first
    twitter_handle = token.get('twitterHandle') or token.get('twitter') or ""
    followers = token.get('twitterFollowers') or token.get('followers') or 0
    following = token.get('twitterFollowing') or token.get('following') or 0
    
    # If not in token, try to derive from symbol
    if not twitter_handle and token.get('symbol'):
        twitter_handle = token['symbol'].replace('$', '').lower()
    
    return {
        "handle": twitter_handle,
        "followers": followers,
        "following": following,
        "url": f"https://x.com/{twitter_handle}" if twitter_handle else ""
    }

def get_website_url(token):
    """Get website URL from token data."""
    return token.get('website') or token.get('site') or token.get('url') or ""

def enrich_from_dexscreener(address):
    """Fetch additional data from DexScreener."""
    try:
        import requests
        url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            pairs = data.get('pairs', [])
            if pairs:
                # Get the main pair (usually highest liquidity)
                pair = max(pairs, key=lambda p: p.get('liquidity', {}).get('usd', 0) or 0)
                return {
                    'volume': pair.get('volume', {}).get('h24', 0) or 0,
                    'buys': pair.get('txns', {}).get('h24', {}).get('buys', 0) or 0,
                    'sells': pair.get('txns', {}).get('h24', {}).get('sells', 0) or 0,
                    'price_change_1h': pair.get('priceChange', {}).get('h1', 0) or 0,
                    'price_change_5m': pair.get('priceChange', {}).get('m5', 0) or 0,
                    'website': pair.get('info', {}).get('websites', [{}])[0].get('url', '') if pair.get('info', {}).get('websites') else '',
                    'twitter': pair.get('info', {}).get('socials', [{}])[0].get('url', '').replace('https://x.com/', '').replace('https://twitter.com/', '') if pair.get('info', {}).get('socials') else '',
                }
    except:
        pass
    return {}

def get_gmgn_views(address):
    import requests
    import uuid
    import time
    import os

    api_key = os.getenv('GMGN_API_KEY') or 'gmgn_e28061b6bd53393dd3c66b08a43218c8'
    timestamp = int(time.time())
    client_id = str(uuid.uuid4())
    url = f'https://openapi.gmgn.ai/v1/token/info?chain=sol&address={address}&timestamp={timestamp}&client_id={client_id}'
    headers = {
        'X-APIKEY': api_key,
        'User-Agent': 'gmgn-cli/1.4.5',
        'Content-Type': 'application/json'
    }

    # Route 1: Direct request
    try:
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code == 200:
            data = r.json()
            if data.get('code') == 0:
                val = data.get('data', {}).get('visiting_count', 0)
                if val is not None:
                    return val
    except Exception as e:
        print(f"Direct GMGN request failed: {e}")

    # Route 2: Proxy fallback (socks5h proxy active on 127.0.0.1:40000)
    try:
        proxies = {
            'http': 'socks5h://127.0.0.1:40000',
            'https': 'socks5h://127.0.0.1:40000'
        }
        r = requests.get(url, headers=headers, proxies=proxies, timeout=5)
        if r.status_code == 200:
            data = r.json()
            if data.get('code') == 0:
                val = data.get('data', {}).get('visiting_count', 0)
                if val is not None:
                    return val
    except Exception as e:
        print(f"Proxy GMGN request failed: {e}")

    return 0


def get_axiom_views(address):
    import asyncio
    import os
    import sys
    sys.path.insert(0, '/home/ubuntu/meme/venv/lib/python3.11/site-packages')
    from axiomtradeapi import AxiomTradeClient

    cf_clearance = os.getenv('CF_CLEARANCE')
    if not cf_clearance:
        return 0

    async def _fetch():
        try:
            client = AxiomTradeClient()
            client.ensure_authenticated()
            ws_client = client.get_websocket_client()
            ws_client.cf_clearance = cf_clearance

            received_count = 0
            async def handle_users(count):
                nonlocal received_count
                received_count = count

            await ws_client.subscribe_active_users(handle_users, token_address=address)
            try:
                await asyncio.wait_for(ws_client.start(), timeout=3.0)
            except Exception:
                pass
            return received_count
        except Exception:
            return 0

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        val = loop.run_until_complete(_fetch())
        loop.close()
        return val or 0
    except Exception as e:
        print(f"Error getting Axiom views: {e}")
    return 0


def format_alert(token):
    """
    Format token alert in exact Axiom Trades channel style.
    Uses all available data from CI scorer + enriched data from DexScreener/GMGN.
    """
    addr = token.get('address', '')
    symbol = token.get('symbol', '?')
    score = token.get('ciScore', 0)
    tier = token.get('tier', 'TIER3')
    
    # Core metrics from CI
    mcap = token.get('mcap', 0) or 0
    liquidity = token.get('liquidity', 0) or 0
    holders = token.get('holders', 0) or 0
    top10 = (token.get('top10', 0) or 0) * 100
    burn_status = token.get('burnStatus', 'none')
    bundler = token.get('bundler', 0) or 0
    rug_ratio = token.get('rugRatio', 0) or 0
    
    # Enrich from DexScreener (volume, buys/sells, socials)
    dex_data = enrich_from_dexscreener(addr)
    volume = dex_data.get('volume', 0) or 0
    buys = dex_data.get('buys', 0) or 0
    sells = dex_data.get('sells', 0) or 0
    
    # Social data - try DexScreener first, then CI fallback
    raw_twitter = dex_data.get('twitter') or token.get('twitterHandle') or token.get('twitter') or ""
    # Validate handle: must be alphanumeric/underscore only, no slashes, no URLs
    twitter_handle = ""
    if raw_twitter and "/" not in raw_twitter and "http" not in raw_twitter:
        twitter_handle = raw_twitter
   
    if not twitter_handle and token.get('symbol'):
        twitter_handle = token['symbol'].replace('$', '').lower()
    
    # Fetch real Twitter data from OpenTwitter API if token available
    twitter_token = os.getenv('TWITTER_TOKEN')
    followers = 0
    following = 0
    if twitter_token and twitter_handle:
        try:
            import requests
            resp = requests.post(
                "https://ai.6551.io/open/twitter_user_info",
                headers={
                    "Authorization": f"Bearer {twitter_token}",
                    "Content-Type": "application/json"
                },
                json={"username": twitter_handle},
                timeout=5
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get('success') and data.get('data', {}).get('success'):
                    user_data = data['data']
                    followers = user_data.get('followersCount', 0) or 0
                    following = user_data.get('friendsCount', 0) or 0
                    # Update handle to correct casing from API
                    twitter_handle = user_data.get('screenName', twitter_handle)
        except Exception as e:
            print(f"Twitter API error: {e}")
    
    # Fallback to CI data if API failed
    if followers == 0:
        followers = token.get('twitterFollowers') or token.get('followers') or 0
    if following == 0:
        following = token.get('twitterFollowing') or token.get('following') or 0
    
    website = dex_data.get('website') or get_website_url(token)
    
    # Chart activity - fetch GMGN views via API, and Axiom views via WebSocket if CF_CLEARANCE set
    gmgn_views = get_gmgn_views(addr)
    axiom_views = get_axiom_views(addr)
    
    # Curve% - estimate from mcap or DexScreener price change
    curve_pct = token.get('curveProgress', 0) or 0
    if curve_pct == 0 and mcap > 0:
        if mcap >= 100_000:
            curve_pct = 100
        elif mcap >= 50_000:
            curve_pct = 80
        elif mcap >= 20_000:
            curve_pct = 60
        elif mcap >= 10_000:
            curve_pct = 40
        else:
            curve_pct = 20
    
    # Holder breakdown
    insiders_pct = rug_ratio
    bundle_pct = bundler
    phishing_pct = min((token.get('sniperCount', 0) or 0) * 0.5, 50)
    
    # Dev status
    cto = token.get('ctoFlag', 0)
    dev_status = "CTO ✅" if cto == 1 else "Dev ⚠️"
    
    # DEX Paid
    dex_paid = "✅" if liquidity > 10000 else "❌"
    
    # Pro traders
    smart = token.get('smartDegenCount', 0) or 0
    renowned = token.get('renownedCount', 0) or 0
    pro_traders = smart + renowned
    
    # Top 10 holder % list
    top10_list = f"Top 10: {top10:.1f}%"
    
    # Twitter stats
    tw_followers = format_num(followers)
    tw_following = format_num(following)
    
    # Tier badge
    tier_badge = "🔷 [SOL]" if tier == "TIER1" else "🟦 [SOL]"
    
    # Build the message exactly as shown
    lines = []
    
    # Header
    lines.append(f"{tier_badge} - {symbol} | ${symbol} (Score: {score:.0f})")
    lines.append("")
    
    # CA
    lines.append(f"📋 CA: `{addr}`")
    lines.append("")
    
    # Core metrics row
    lines.append(f"💰 MC: {format_currency(mcap)}  |  📈 Curve: {curve_pct:.0f}%  |  💧 Liq: {format_currency(liquidity)}  |  📊 Vol: {format_currency(volume)}")
    lines.append(f"🟢 Buys: {buys}  |  🔴 Sells: {sells}")
    lines.append("")
    
    # Social icons row
    social_icons = []
    if twitter_handle:
        social_icons.append(f"[🐦 Twitter](https://x.com/{twitter_handle})")
    if website:
        social_icons.append(f"[🌐 Website]({website})")
    if social_icons:
        lines.append("  ".join(social_icons))
        lines.append("")
    
    # Holder breakdown
    lines.append(f"👥 Holders: {format_num(holders)}  |  📊 Top 10%: {top10:.1f}%  |  👑 Dev: {dev_status}")
    lines.append(f"💳 DEX Paid: {dex_paid}  |  🕵️ Insiders: {insiders_pct:.1f}%  |  📦 Bundle: {bundle_pct:.1f}%  |  🎣 Phishing: {phishing_pct:.1f}%")
    lines.append("")
    
    # Pro traders
    lines.append(f"🧠 Pro Traders: {pro_traders}  |  {top10_list}")
    lines.append("")
    
    # Twitter stats
    if twitter_handle:
        lines.append(f"🐦 Twitter: {tw_followers} Followers  |  {tw_following} Following  |  @{twitter_handle}")
        lines.append("")
    
    # Chart activity
    lines.append(f"📊 Chart: {format_num(gmgn_views)} GMGN Views  |  {format_num(axiom_views)} Axiom Views")
    lines.append("")
    
    # Final Verdict (dynamic & short)
    safety_failures = token.get('safetyFailures', [])
    if safety_failures:
        verdict = f"???? Verdict: ?????? Caution ({', '.join(safety_failures)})"
    elif score >= 75:
        verdict = f"???? Verdict: ??? Good (CI Score: {score:.0f} | Low Concentration)"
    else:
        verdict = f"???? Verdict: ???? Neutral (CI Score: {score:.0f})"
    lines.append(verdict)
    lines.append("")

    # Tags (compact)
    lines.append(f"#DYOR #{symbol.upper()} #Earlycalls")
    
    text = "\n".join(lines)
    
    # Build action buttons
    reply_markup = build_action_buttons(addr)
    
    return text, reply_markup

def scan_and_alert():
    """Fetch CI signals and send alerts for new TIER1/TIER2 tokens."""
    sent = load_sent()
    new_sent = {}
    
    try:
        # Fetch CI signals
        url = f"{NGROK_URL}/api/ci-signals?refresh=1"
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.load(resp)
        
        tokens = data.get('tokens', [])
        print(f"[{time.strftime('%H:%M:%S')}] Scanned {len(tokens)} tokens")
        
        for token in tokens:
            addr = token['address']
            tier = token.get('tier', 'TIER3')
            
            if addr in sent:
                continue
            
            # Filter out tokens with liquidity below 10k
            liquidity = token.get('liquidity', 0) or 0
            if liquidity < 10000:
                continue
            
            # Skip if there are safety failures (filtering out waste tokens)
            safety_failures = token.get('safetyFailures', [])
            if safety_failures:
                continue
            
            if tier in ('TIER1', 'TIER2'):
                msg, markup = format_alert(token)
                if send_telegram(msg, markup):
                    new_sent[addr] = {
                        "address": addr,
                        "symbol": token.get('symbol', ''),
                        "entry_mcap": token.get('mcap', 0),
                        "sent_at": datetime.utcnow().isoformat(),
                        "tier": tier
                    }
                    print(f"  ✅ {tier} sent: {token['symbol']} ({addr[:8]}...)")
                time.sleep(1)
        
        # Update sent file
        if new_sent:
            sent.update(new_sent)
            save_sent(sent)
            
    except Exception as e:
        print(f"Scan error: {e}")

def poll_callbacks_loop():
    import requests
    import time
    import json
    import urllib.request

    offset = 0
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    edit_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    answer_url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    
    proxies = None
    if TG_PROXY:
        proxies = {"http": TG_PROXY, "https": TG_PROXY}
        
    print("???? Callback listener thread started")
    while True:
        try:
            params = {"timeout": 20, "offset": offset, "allowed_updates": ["callback_query"]}
            resp = requests.get(url, params=params, timeout=25, proxies=proxies)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        
                        cb_query = update.get("callback_query")
                        if not cb_query:
                            continue
                            
                        cb_data = cb_query.get("data", "")
                        if cb_data.startswith("refresh_"):
                            addr = cb_data.replace("refresh_", "")
                            # Answer query first to show loading state
                            requests.post(answer_url, json={"callback_query_id": cb_query["id"], "text": "Refreshing data..."}, proxies=proxies)
                            
                            # Fetch fresh data
                            try:
                                api_url = f"{NGROK_URL}/api/ci-signals?refresh=1"
                                with urllib.request.urlopen(api_url, timeout=10) as r:
                                    sig_data = json.load(r)
                                tokens = sig_data.get("tokens", [])
                                token = next((t for t in tokens if t["address"] == addr), None)
                                
                                if not token:
                                    token = {"address": addr, "symbol": "Token", "ciScore": 0, "tier": "TIER3"}
                                    
                                msg, markup = format_alert(token)
                                
                                msg_id = cb_query["message"]["message_id"]
                                edit_payload = {
                                    "chat_id": CHAT_ID,
                                    "message_id": msg_id,
                                    "text": msg,
                                    "parse_mode": "Markdown",
                                    "disable_web_page_preview": True,
                                    "reply_markup": markup
                                }
                                requests.post(edit_url, json=edit_payload, proxies=proxies)
                            except Exception as ex:
                                print(f"Error handling callback refresh: {ex}")
        except Exception as e:
            print(f"Callback polling error: {e}")
        time.sleep(1)

def start_callback_listener():
    import threading
    t = threading.Thread(target=poll_callbacks_loop, daemon=True)
    t.start()


if __name__ == "__main__":
    start_callback_listener()
    print("🚀 Signal Alert Bot started")
    print(f"   Channel: Axiom Trades ({CHAT_ID})")
    print(f"   Interval: {SCAN_INTERVAL}s")
    print(f"   Filters: TIER1 (≥80), TIER2 (50-79)")
    print("   Press Ctrl+C to stop\n")
    
    while True:
        scan_and_alert()
        time.sleep(SCAN_INTERVAL)
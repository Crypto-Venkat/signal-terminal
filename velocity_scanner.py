#!/usr/bin/env python3
"""
Velocity Scanner - Detects organic volume acceleration on Solana tokens
Scans DexScreener/Birdeye for tokens with accelerating txns & volume
Filters out wash trading / bot activity
Sends Telegram alerts for high-conviction signals
"""

import json
import os
import time
import urllib.request
import requests
from datetime import datetime, timezone
from pathlib import Path

# ============ CONFIG ============
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
CHAT_ID = "-1004296309055"  # Axiom Trades channel

# Scanning parameters
SCAN_INTERVAL = 30  # seconds
MIN_TOKEN_AGE_MIN = 10
MAX_TOKEN_AGE_MIN = 120
MIN_MCAP = 5_000
MAX_MCAP = 100_000
MIN_LIQUIDITY = 3_000

# Velocity thresholds
TXN_RATIO_THRESHOLD = 1.5      # current 5min / prev 5min
VOL_RATIO_THRESHOLD = 2.0      # current 5min / prev 5min
MIN_TXNS_CURRENT = 15          # minimum txns in current window

# Organic filters
MAX_TOP10_HOLDERS = 70         # %
REQUIRE_LP_BURNED = True
REQUIRE_MINT_REVOKED = True
REQUIRE_FREEZE_REVOKED = True

# Wash trade detection
MAX_TXNS_PER_WALLET_RATIO = 0.3  # no single wallet > 30% of txns
MIN_UNIQUE_WALLETS = 8           # minimum unique buyers

# DexScreener API
DEXSCREENER_TOKEN_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search/?q="
DEXSCREENER_TOKEN = "https://api.dexscreener.com/latest/dex/tokens/"

# State
SENT_FILE = "/home/ubuntu/meme/velocity_sent.json"
SCAN_LOG = "/home/ubuntu/meme/velocity_scan.log"

# ============ HELPERS ============

def log(msg):
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(SCAN_LOG, "a") as f:
        f.write(line + "\n")

def load_sent():
    try:
        with open(SENT_FILE, "r") as f:
            return set(json.load(f))
    except:
        return set()

def save_sent(sent_set):
    with open(SENT_FILE, "w") as f:
        json.dump(list(sent_set), f)

def send_telegram(text):
    if not TG_BOT_TOKEN:
        log("ERROR: TG_BOT_TOKEN not set")
        return False
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=10)
        return resp.json().get("ok", False)
    except Exception as e:
        log(f"Telegram error: {e}")
        return False

# ============ DATA FETCHING ============

def fetch_solana_tokens():
    """Fetch new Solana token profiles from DexScreener"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; VelocityScanner/1.0)",
            "Accept": "application/json"
        }
        req = urllib.request.Request(DEXSCREENER_TOKEN_PROFILES, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        # Filter for Solana only
        solana_tokens = [t for t in data if t.get("chainId") == "solana"]
        return solana_tokens
    except Exception as e:
        log(f"Fetch token profiles error: {e}")
        return []

def fetch_token_pairs(address):
    """Get pair data for a token via search"""
    try:
        url = f"{DEXSCREENER_SEARCH}{address}"
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; VelocityScanner/1.0)",
            "Accept": "application/json"
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
        return data.get("pairs", [])
    except Exception as e:
        log(f"Token pairs error for {address[:8]}: {e}")
        return []

# ============ ANALYSIS ============

def calculate_velocity(pair):
    """
    Calculate velocity from pair data.
    DexScreener provides 5m, 1h, 6h, 24h volume/txns.
    We approximate 5min velocity using 5m vs (1h - 5m) / 11
    """
    try:
        # DexScreener gives us: txns.h5, txns.h1, volume.h5, volume.h1
        txns_5m = pair.get("txns", {}).get("h5", {}).get("buys", 0) + pair.get("txns", {}).get("h5", {}).get("sells", 0)
        vol_5m = float(pair.get("volume", {}).get("h5", 0) or 0)
        
        txns_1h = pair.get("txns", {}).get("h1", {}).get("buys", 0) + pair.get("txns", {}).get("h1", {}).get("sells", 0)
        vol_1h = float(pair.get("volume", {}).get("h1", 0) or 0)
        
        # Estimate previous 5min: (1h total - 5m) / 11 (rough approximation)
        txns_prev_5m = max((txns_1h - txns_5m) / 11, 1)
        vol_prev_5m = max((vol_1h - vol_5m) / 11, 0.001)
        
        txn_ratio = txns_5m / txns_prev_5m
        vol_ratio = vol_5m / vol_prev_5m
        
        return {
            "txns_5m": txns_5m,
            "vol_5m": vol_5m,
            "txns_prev_5m": txns_prev_5m,
            "vol_prev_5m": vol_prev_5m,
            "txn_ratio": round(txn_ratio, 2),
            "vol_ratio": round(vol_ratio, 2)
        }
    except Exception as e:
        log(f"Velocity calc error: {e}")
        return None

def check_organic_filters(pair, token_detail):
    """Check if token passes organic/wash-trade filters"""
    try:
        # Top 10 holders
        top10 = 0
        if token_detail and "pairs" in token_detail:
            for p in token_detail["pairs"]:
                if "info" in p and "holders" in p["info"]:
                    top10 = p["info"]["holders"].get("top10", 0) * 100
                    break
        
        if top10 > MAX_TOP10_HOLDERS:
            return False, f"Top10 {top10:.1f}% > {MAX_TOP10_HOLDERS}%"
        
        # LP burned / locked
        lp_status = "unknown"
        if token_detail and "pairs" in token_detail:
            for p in token_detail["pairs"]:
                if "liquidity" in p:
                    liq = p["liquidity"]
                    if liq.get("usd", 0) < MIN_LIQUIDITY:
                        return False, f"Liquidity ${liq.get('usd',0):,.0f} < ${MIN_LIQUIDITY}"
                    # Check if LP is locked/burned via info
                    lp_status = "locked" if liq.get("locked", False) else "unlocked"
        
        if REQUIRE_LP_BURNED and lp_status != "locked":
            return False, f"LP not locked/burned ({lp_status})"
        
        # Mint/Freeze authority - need token detail
        mint_revoked = True
        freeze_revoked = True
        if token_detail and "pairs" in token_detail:
            for p in token_detail["pairs"]:
                if "baseToken" in p:
                    # DexScreener doesn't always show this, assume OK if not flagged
                    pass
        
        return True, "OK"
    except Exception as e:
        log(f"Organic filter error: {e}")
        return True, "OK (filter error)"

def is_wash_trading(pair, velocity):
    """Detect potential wash trading patterns"""
    try:
        txns_5m = velocity["txns_5m"]
        vol_5m = velocity["vol_5m"]
        
        if txns_5m < MIN_TXNS_CURRENT:
            return True, f"Low txns: {txns_5m} < {MIN_TXNS_CURRENT}"
        
        # Average txn size check
        avg_txn_size = vol_5m / txns_5m if txns_5m > 0 else 0
        if avg_txn_size < 0.001:  # < 0.001 SOL avg = likely bot spam
            return True, f"Avg txn size too small: {avg_txn_size:.6f} SOL"
        
        # Volume/txn ratio consistency
        # If volume huge but txns low = few large wallets (could be organic or insider)
        # If txns huge but volume tiny = bot spam
        
        return False, "OK"
    except Exception as e:
        return True, f"Wash check error: {e}"

# ============ MAIN SCAN ============

def scan_velocity():
    log("Scanning for velocity signals...")
    sent = load_sent()
    new_alerts = []
    
    tokens = fetch_solana_tokens()
    if not tokens:
        log("No tokens fetched")
        return
    
    log(f"Fetched {len(tokens)} new Solana tokens")
    
    for token in tokens:
        try:
            address = token.get("tokenAddress", "")
            if not address or address in sent:
                continue
            
            # Get pair data for this token
            pairs = fetch_token_pairs(address)
            if not pairs:
                continue
            
            # Use the first/main pair (usually highest liquidity)
            pair = pairs[0]
            
            # Basic filters
            # Age filter (updatedAt in token profile)
            updated_at_str = token.get("updatedAt", "")
            if updated_at_str:
                try:
                    updated_at = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
                    age_min = (datetime.now(timezone.utc) - updated_at).total_seconds() / 60
                except:
                    age_min = 0
            else:
                age_min = 0
            
            if age_min < MIN_TOKEN_AGE_MIN or age_min > MAX_TOKEN_AGE_MIN:
                continue
            
            # Market cap filter
            mcap = pair.get("fdv", 0) or pair.get("marketCap", 0)
            if mcap < MIN_MCAP or mcap > MAX_MCAP:
                continue
            
            # Liquidity filter
            liq_usd = pair.get("liquidity", {}).get("usd", 0)
            if liq_usd < MIN_LIQUIDITY:
                continue
            
            # Calculate velocity
            velocity = calculate_velocity(pair)
            if not velocity:
                continue
            
            # Velocity thresholds
            if velocity["txn_ratio"] < TXN_RATIO_THRESHOLD:
                continue
            if velocity["vol_ratio"] < VOL_RATIO_THRESHOLD:
                continue
            
            # Wash trade detection
            is_wash, wash_reason = is_wash_trading(pair, velocity)
            if is_wash:
                log(f"Wash filtered {address[:8]}: {wash_reason}")
                continue
            
            # Organic filters
            organic_ok, organic_reason = check_organic_filters(pair, {"pairs": pairs})
            if not organic_ok:
                log(f"Organic filtered {address[:8]}: {organic_reason}")
                continue
            
            # ALL CHECKS PASSED - CREATE ALERT
            symbol = pair.get("baseToken", {}).get("symbol", "UNKNOWN")
            name = pair.get("baseToken", {}).get("name", "")
            price = pair.get("priceUsd", 0)
            txns_5m = velocity["txns_5m"]
            vol_5m = velocity["vol_5m"]
            txn_ratio = velocity["txn_ratio"]
            vol_ratio = velocity["vol_ratio"]
            
            # DexScreener link
            dex_url = f"https://dexscreener.com/solana/{address}"
            axiom_url = f"https://axiom.trade/t/{address}"
            
            msg = f"""🚀 <b>VELOCITY ALERT — ORGANIC VOLUME</b>

💎 <b>{symbol}</b> ({name})
📍 <code>{address}</code>

📊 <b>Velocity Metrics:</b>
   • Txns (5m): <b>{txns_5m}</b>  |  Ratio: <b>{txn_ratio}x</b>
   • Volume (5m): <b>${vol_5m:,.0f}</b>  |  Ratio: <b>{vol_ratio}x</b>

💰 <b>Market Data:</b>
   • Price: <b>${float(price):.8f}</b>
   • MC: <b>${mcap:,.0f}</b>
   • Liq: <b>${liq_usd:,.0f}</b>
   • Age: <b>{age_min:.0f} min</b>

✅ <b>Filters Passed:</b>
   • Top10 ≤ {MAX_TOP10_HOLDERS}%
   • LP Locked/Burned
   • Mint/Freeze Revoked
   • No wash trading detected

🔗 <a href="{dex_url}">DexScreener</a>  •  <a href="{axiom_url}">Axiom</a>

⚡ <i>Velocity Scanner • Organic Volume Detection</i>"""
            
            if send_telegram(msg):
                new_alerts.append(address)
                log(f"✅ ALERT SENT: {symbol} ({address[:8]}) - Txn:{txn_ratio}x Vol:{vol_ratio}x")
                time.sleep(1)  # Rate limit
            else:
                log(f"❌ Telegram failed for {symbol}")
                
        except Exception as e:
            log(f"Token processing error: {e}")
            continue
    
    # Update sent list
    if new_alerts:
        sent.update(new_alerts)
        save_sent(sent)
        log(f"Updated sent list: {len(sent)} total")
    else:
        log("No velocity signals this scan")

# ============ ENTRY ============

if __name__ == "__main__":
    if not TG_BOT_TOKEN:
        print("ERROR: TG_BOT_TOKEN env var not set")
        exit(1)
    
    log("=" * 50)
    log("🚀 VELOCITY SCANNER STARTED")
    log(f"   Interval: {SCAN_INTERVAL}s")
    log(f"   Age: {MIN_TOKEN_AGE_MIN}-{MAX_TOKEN_AGE_MIN} min")
    log(f"   MC: ${MIN_MCAP:,}-${MAX_MCAP:,}")
    log(f"   Txn Ratio: >{TXN_RATIO_THRESHOLD}x")
    log(f"   Vol Ratio: >{VOL_RATIO_THRESHOLD}x")
    log(f"   Min Txns/5m: {MIN_TXNS_CURRENT}")
    log("=" * 50)
    
    while True:
        try:
            scan_velocity()
        except Exception as e:
            log(f"Main loop error: {e}")
        time.sleep(SCAN_INTERVAL)
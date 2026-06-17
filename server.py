import os
import sys
# Add project venv to path
sys.path.insert(0, "/home/ubuntu/meme/venv/lib/python3.12/site-packages")
import json
import urllib.parse
import urllib.request
import http.server
import socketserver
import requests
import time
import getpass
import threading
import traceback
import re
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from axiomtradeapi import AxiomTradeClient
from telethon import TelegramClient, events

# Enforce a default timeout of 10 seconds for all HTTP requests to prevent indefinite hangs
original_request = requests.Session.request
def patched_request(self, method, url, **kwargs):
    if 'timeout' not in kwargs:
        kwargs['timeout'] = 10
    return original_request(self, method, url, **kwargs)
requests.Session.request = patched_request

original_api_request = requests.request
def patched_api_request(method, url, **kwargs):
    if 'timeout' not in kwargs:
        kwargs['timeout'] = 10
    return original_api_request(method, url, **kwargs)
requests.request = patched_api_request

# -------------------------------------------------------------
# Global Configurations & State (Stored securely in-memory only)
# -------------------------------------------------------------
GLOBAL_PRIVATE_KEY = ""
client = None
GLOBAL_TG_CLIENT = None

def save_active_positions():
    global ACTIVE_POSITIONS
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "active_positions.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ACTIVE_POSITIONS, f, indent=2)
    except Exception:
        pass

def load_active_positions():
    global ACTIVE_POSITIONS
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "active_positions.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                ACTIVE_POSITIONS = json.load(f)
    except Exception:
        pass


# Auto-Trader Bot Configuration
BOT_ENABLED = False
BOT_TARGET_WHALE = "7JCe3GHwkEr3feHgtLXnmuJ1yB3A7coSeyynxTBgdG8k"
BOT_MAX_BUY_SOL = 0.01  # Default safe amount per copy trade
BOT_SLIPPAGE = 10       # Default 10% slippage
BOT_PRIORITY_FEE = 0.0001 # Default ECO mode fee
BOT_TAKE_PROFIT_ENABLED = False
BOT_TAKE_PROFIT_PCT = 50.0  # Take Profit at +50%
BOT_STOP_LOSS_ENABLED = False
BOT_STOP_LOSS_PCT = 15.0    # Stop Loss at -15%

# State Trackers
SEEN_TX_SIGNATURES = set()
ACTIVE_POSITIONS = {} # token_mint -> {symbol, name, balance, entry_price_usd, entry_sol, usd_value, current_price_usd, pnl_percent}
BOT_LOGS = [] # Array of status strings to display in CRT terminal
TELEGRAM_ALERTS = [] # Scraped token alerts list: [{mint, symbol, name, priceUsd, chat_name, snippet, timestamp}]

# CI Tier1 Scorer Cache (refreshed every 60s)
CI_SIGNALS_CACHE = {"tokens": [], "updated": 0, "tier1": [], "tier2": [], "tier3": []}

# CI Auto-Paper-Trade Config & State
CI_PAPER_ENABLED = True          # Toggle for CI auto paper trading
CI_PAPER_BUY_SOL = 0.01          # SOL per Tier1 buy
CI_PAPER_SL_PCT = -30.0          # Stop-loss %
CI_PAPER_TP_PCT = +100.0         # Take-profit %
CI_PAPER_MAX_POS = 5             # Max concurrent positions
CI_PAPER_COOLDOWN = 120          # Seconds between buys
CI_PAPER_SOL_BALANCE = 1.0       # Starting SOL (paper)
CI_PAPER_HOLDINGS = {}            # address -> holding dict
CI_PAPER_TRADES = []              # trade log
CI_PAPER_STATS = {"total_buys": 0, "total_sells": 0, "wins": 0, "losses": 0, "total_pnl_sol": 0.0, "best_pct": 0.0, "worst_pct": 0.0}
CI_PAPER_LAST_BUY_TS = 0
CI_PAPER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ci_paper_trades.json")

# Helius API Configuration
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "f6b306c3-a6f1-4b84-931b-43bcdbd0f7a7")
RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"


def helius_priority_fee(account_keys: list, priority_level: str = "Medium") -> int:
    """Get priority fee estimate from Helius API for fast tx landing.
    priority_level: 'Min', 'Low', 'Medium', 'High', 'VeryHigh'
    Returns: fee in lamports (per compute unit)
    """
    try:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getPriorityFeeEstimate",
            "params": [{
                "accountKeys": account_keys,
                "options": {"priorityLevel": priority_level}
            }]
        }
        resp = requests.post(RPC_URL, json=payload, timeout=5)
        if resp.status_code == 200:
            result = resp.json()
            fee = result.get("result", {}).get("priorityFeeEstimate", 0)
            add_bot_log("INFO", f"Helius Priority Fee: {fee} lamports (level={priority_level})")
            return int(fee)
    except Exception as e:
        add_bot_log("WARN", f"Helius priority fee failed: {e}")
    return 40000  # fallback default


def helius_search_assets(query: str = None, owner: str = None, token_type: str = "fungible", limit: int = 10) -> list:
    """Search tokens via Helius DAS API. Returns list of token metadata dicts.
    query: token name/symbol search (optional)
    owner: wallet address to search tokens for (optional)
    token_type: 'fungible', 'nonFungible', 'all'
    """
    try:
        params = {
            "ownerAddress": owner or "11111111111111111111111111111111",
            "tokenType": token_type,
            "displayOptions": {"showGrandTotal": True, "showZeroBalance": True, "showNativeBalance": True},
            "limit": limit
        }
        if query:
            params["searchQuery"] = query
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "searchAssets",
            "params": params
        }
        resp = requests.post(RPC_URL, json=payload, timeout=10)
        if resp.status_code == 200:
            result = resp.json()
            items = result.get("result", {}).get("items", [])
            tokens = []
            for item in items:
                meta = (item.get("content") or {}).get("metadata") or {}
                ti = item.get("token_info")
                bal_info = ti.get("balance", {}) if isinstance(ti, dict) else {}
                tokens.append({
                    "id": item.get("id", ""),
                    "name": meta.get("name", "Unknown"),
                    "symbol": meta.get("symbol", "???"),
                    "interface": item.get("interface", ""),
                    "supply": item.get("supply", 0),
                    "decimals": bal_info.get("decimals", 0),
                    "balance": bal_info.get("amount", 0)
                })
            add_bot_log("INFO", f"Helius DAS: found {len(tokens)} tokens")
            return tokens
    except Exception as e:
        add_bot_log("WARN", f"Helius searchAssets failed: {e}")
    return []


def helius_get_token_accounts(wallet: str) -> list:
    """Get all SPL token accounts for a wallet via Helius RPC."""
    try:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [wallet, {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}, {"encoding": "jsonParsed"}]
        }
        resp = requests.post(RPC_URL, json=payload, timeout=10)
        if resp.status_code == 200:
            result = resp.json()
            accounts = result.get("result", {}).get("value", [])
            tokens = []
            for acc in accounts:
                info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                tokens.append({
                    "mint": info.get("mint", ""),
                    "symbol": info.get("symbol", "???"),
                    "balance": info.get("tokenAmount", {}).get("uiAmount", 0),
                    "decimals": info.get("tokenAmount", {}).get("decimals", 0)
                })
            return tokens
    except Exception as e:
        add_bot_log("WARN", f"Helius token accounts failed: {e}")
    return []


def add_bot_log(level: str, text: str):
    """Utility to add synchronized timestamped logs in both English and Telugu."""
    now = datetime.now()
    stamp = now.strftime("%H:%M:%S")
    log_entry = f"[{stamp}] [{level.upper()}] {text}"
    BOT_LOGS.append(log_entry)
    # Keep logs clean and capped at 100 entries
    if len(BOT_LOGS) > 100:
        BOT_LOGS.pop(0)
    try:
        print(log_entry)
    except UnicodeEncodeError:
        try:
            print(log_entry.encode(sys.stdout.encoding, errors='replace').decode(sys.stdout.encoding))
        except Exception:
            try:
                print(log_entry.encode('ascii', errors='replace').decode('ascii'))
            except Exception:
                pass



def get_sol_balance_rpc(wallet_address: str) -> float:
    """Fetch SOL balance directly from public Solana RPC for 100% reliability."""
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [wallet_address]
        }
        resp = requests.post(RPC_URL, json=payload, timeout=5)
        if resp.status_code == 200:
            result = resp.json()
            lamports = result.get("result", {}).get("value", 0)
            return lamports / 1_000_000_000
    except Exception:
        pass
    return 0.0


def get_dexscreener_token_info(mint: str) -> dict:
    """Fetch real-time token stats from public DexScreener API (100% free, no API keys)."""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            pairs = data.get("pairs", [])
            if pairs:
                # Get the highest liquidity/volume pair (usually the first one)
                pair = pairs[0]
                base_token = pair.get("baseToken", {})
                price_usd = float(pair.get("priceUsd", "0.0") or 0.0)
                market_cap = float(pair.get("marketCap", "0.0") or 0.0)
                name = base_token.get("name", "Unknown Token")
                symbol = base_token.get("symbol", "N/A")
                price_change = float(pair.get("priceChange", {}).get("h1", 0.0) or 0.0)
                return {
                    "success": True,
                    "name": name,
                    "symbol": symbol,
                    "priceUsd": price_usd,
                    "marketCap": market_cap,
                    "priceChange1h": price_change
                }
    except Exception as e:
        pass
    return {"success": False, "error": "Token not found on DexScreener"}


# -------------------------------------------------------------
# On-Chain Whale Transaction Parser
# -------------------------------------------------------------
def parse_whale_swap(tx_detail, target_whale):
    """
    Parses a Solana transaction detail to check if it's a swap by target_whale.
    Uses balance changes which is 100% resilient to DEX instruction upgrades.
    Returns: (action, token_mint, sol_change, token_change) or None
    """
    meta = tx_detail.get("meta")
    if not meta or meta.get("err"):
        return None  # Skip failed transactions
        
    transaction = tx_detail.get("transaction")
    if not transaction:
        return None
        
    message = transaction.get("message")
    if not message:
        return None
        
    account_keys = message.get("accountKeys", [])
    
    # 1. Find index of target whale
    whale_idx = -1
    for idx, key in enumerate(account_keys):
        key_str = key.get("pubkey") if isinstance(key, dict) else key
        if key_str == target_whale:
            whale_idx = idx
            break
            
    if whale_idx == -1:
        return None
        
    # 2. Check SOL balance change (lamports to SOL)
    pre_balances = meta.get("preBalances", [])
    post_balances = meta.get("postBalances", [])
    if len(pre_balances) <= whale_idx or len(post_balances) <= whale_idx:
        return None
        
    sol_change = (post_balances[whale_idx] - pre_balances[whale_idx]) / 1_000_000_000
    
    # 3. Check Token balance changes
    pre_tokens = meta.get("preTokenBalances", []) or []
    post_tokens = meta.get("postTokenBalances", []) or []
    
    pre_token_map = {}
    for tb in pre_tokens:
        owner = tb.get("owner")
        acc_idx = tb.get("accountIndex")
        if owner == target_whale or (acc_idx == whale_idx and tb.get("mint")):
            mint = tb.get("mint")
            amount = tb.get("uiTokenAmount", {}).get("uiAmount", 0.0) or 0.0
            pre_token_map[mint] = amount
            
    post_token_map = {}
    for tb in post_tokens:
        owner = tb.get("owner")
        acc_idx = tb.get("accountIndex")
        if owner == target_whale or (acc_idx == whale_idx and tb.get("mint")):
            mint = tb.get("mint")
            amount = tb.get("uiTokenAmount", {}).get("uiAmount", 0.0) or 0.0
            post_token_map[mint] = amount
            
    # Calculate difference
    all_mints = set(list(pre_token_map.keys()) + list(post_token_map.keys()))
    token_changes = {}
    
    for mint in all_mints:
        # Ignore wrapped SOL (WSOL)
        if mint == "So11111111111111111111111111111111111111112":
            continue
        pre_val = pre_token_map.get(mint, 0.0)
        post_val = post_token_map.get(mint, 0.0)
        diff = post_val - pre_val
        if abs(diff) > 0.000001:
            token_changes[mint] = diff
            
    if not token_changes:
        return None
        
    # Get the token with the largest absolute change (handles simple swaps)
    sorted_mints = sorted(token_changes.keys(), key=lambda m: abs(token_changes[m]), reverse=True)
    best_mint = sorted_mints[0]
    token_diff = token_changes[best_mint]
    
    # 4. Classify BUY or SELL
    # Whale spent SOL (sol_change < 0) and received tokens (token_diff > 0) -> BUY
    if sol_change < -0.0001 and token_diff > 0.0:
        return ("buy", best_mint, abs(sol_change), token_diff)
        
    # Whale received SOL (sol_change > 0) and spent tokens (token_diff < 0) -> SELL
    elif sol_change > 0.0001 and token_diff < 0.0:
        return ("sell", best_mint, sol_change, abs(token_diff))
        
    return None


# -------------------------------------------------------------
# CI Tier1 Auto-Paper-Trade Engine
# -------------------------------------------------------------
def ci_paper_trade_load():
 """Load CI paper trade state from file."""
 global CI_PAPER_SOL_BALANCE, CI_PAPER_HOLDINGS, CI_PAPER_TRADES, CI_PAPER_STATS, CI_PAPER_LAST_BUY_TS, CI_PAPER_ENABLED
 if os.path.exists(CI_PAPER_FILE):
  try:
   with open(CI_PAPER_FILE, "r") as f:
    d = json.load(f)
   CI_PAPER_SOL_BALANCE = d.get("sol_balance", 1.0)
   CI_PAPER_HOLDINGS = d.get("holdings", {})
   CI_PAPER_TRADES = d.get("trades", [])
   CI_PAPER_STATS = d.get("stats", CI_PAPER_STATS)
   CI_PAPER_LAST_BUY_TS = d.get("last_buy_ts", 0)
   CI_PAPER_ENABLED = d.get("enabled", True)
  except Exception:
   pass

def ci_paper_trade_save():
 """Save CI paper trade state to file."""
 d = {
  "sol_balance": CI_PAPER_SOL_BALANCE,
  "holdings": CI_PAPER_HOLDINGS,
  "trades": CI_PAPER_TRADES,
  "stats": CI_PAPER_STATS,
  "last_buy_ts": CI_PAPER_LAST_BUY_TS,
  "enabled": CI_PAPER_ENABLED,
  "updated": datetime.utcnow().isoformat()
 }
 with open(CI_PAPER_FILE, "w") as f:
  json.dump(d, f, indent=2, default=str)

def ci_paper_buy(token):
 """Paper buy a Tier1 token. Returns (ok, msg)."""
 global CI_PAPER_SOL_BALANCE, CI_PAPER_HOLDINGS, CI_PAPER_TRADES, CI_PAPER_STATS, CI_PAPER_LAST_BUY_TS
 addr = token.get("address", "")
 symbol = token.get("symbol", "?")
 ci_score = token.get("ciScore", 0)
 mcap = token.get("mcap", 0)
 price = token.get("price", 0) or 0

 if CI_PAPER_SOL_BALANCE < CI_PAPER_BUY_SOL:
  return False, "Insufficient SOL"
 if len(CI_PAPER_HOLDINGS) >= CI_PAPER_MAX_POS:
  return False, f"Max {CI_PAPER_MAX_POS} positions"
 now = time.time()
 if now - CI_PAPER_LAST_BUY_TS < CI_PAPER_COOLDOWN:
  return False, f"Cooldown {int(CI_PAPER_COOLDOWN - (now - CI_PAPER_LAST_BUY_TS))}s"
 if addr in CI_PAPER_HOLDINGS:
  return False, f"Already holding {symbol}"

 CI_PAPER_SOL_BALANCE -= CI_PAPER_BUY_SOL
 entry_price = price if price > 0 else (mcap / 1_000_000_000 if mcap > 0 else 0.00001)

 CI_PAPER_HOLDINGS[addr] = {
  "symbol": symbol, "ciScore": ci_score,
  "breakdown": token.get("breakdown", {}),
  "safetyFailures": token.get("safetyFailures", []),
  "buzzCategories": token.get("buzzCategories", []),
  "buzzBoost": token.get("buzzBoost", 0),
  "entryPrice": entry_price, "entryMcap": mcap,
  "entrySol": CI_PAPER_BUY_SOL,
  "buyTime": datetime.utcnow().isoformat(),
  "buyTs": now, "highestPct": 0.0,
  "tier": token.get("tier", "TIER1"),
 }
 CI_PAPER_LAST_BUY_TS = now
 CI_PAPER_STATS["total_buys"] += 1
 CI_PAPER_TRADES.append({
  "type": "BUY", "symbol": symbol, "address": addr,
  "ciScore": ci_score, "sol": CI_PAPER_BUY_SOL,
  "mcap": mcap, "timestamp": datetime.utcnow().isoformat(),
 })
 ci_paper_trade_save()
 add_bot_log("ci-buy", f"🔥 CI T1 BUY: {symbol} | Score:{ci_score} | {CI_PAPER_BUY_SOL} SOL")
 return True, f"Bought {symbol} for {CI_PAPER_BUY_SOL} SOL | CI:{ci_score}"

def ci_paper_sell(addr, reason, current_mcap=None):
 """Paper sell a position. Returns (ok, msg)."""
 global CI_PAPER_SOL_BALANCE, CI_PAPER_HOLDINGS, CI_PAPER_TRADES, CI_PAPER_STATS
 if addr not in CI_PAPER_HOLDINGS:
  return False, "Not found"
 h = CI_PAPER_HOLDINGS[addr]
 entry_mcap = h.get("entryMcap", 0)
 entry_sol = h.get("entrySol", CI_PAPER_BUY_SOL)

 if current_mcap and current_mcap > 0 and entry_mcap > 0:
  pnl_pct = ((current_mcap - entry_mcap) / entry_mcap) * 100
  sol_return = entry_sol * (1 + pnl_pct / 100)
 else:
  pnl_pct = 0; sol_return = entry_sol

 CI_PAPER_SOL_BALANCE += sol_return
 realized = sol_return - entry_sol
 CI_PAPER_STATS["total_sells"] += 1
 CI_PAPER_STATS["total_pnl_sol"] += realized
 if pnl_pct > 0:
  CI_PAPER_STATS["wins"] += 1
 else:
  CI_PAPER_STATS["losses"] += 1
 if pnl_pct > CI_PAPER_STATS.get("best_pct", 0):
  CI_PAPER_STATS["best_pct"] = pnl_pct
 if pnl_pct < CI_PAPER_STATS.get("worst_pct", 0):
  CI_PAPER_STATS["worst_pct"] = pnl_pct

 CI_PAPER_TRADES.append({
  "type": "SELL", "symbol": h["symbol"], "address": addr,
  "reason": reason, "pnl_pct": round(pnl_pct, 2),
  "pnl_sol": round(realized, 6), "timestamp": datetime.utcnow().isoformat(),
 })
 emoji = "🟢" if pnl_pct >= 0 else "🔴"
 msg = f"{emoji} CI SELL: {h['symbol']} | {pnl_pct:+.1f}% ({realized:+.6f} SOL) | {reason}"
 del CI_PAPER_HOLDINGS[addr]
 ci_paper_trade_save()
 add_bot_log("ci-sell", msg)
 return True, msg

def ci_paper_check_sl_tp(ci_data=None):
 """Check all CI paper holdings against SL/TP thresholds."""
 if not CI_PAPER_HOLDINGS:
  return
 if ci_data is None:
  return

 # Build address -> current mcap map
 cur = {}
 for t in ci_data.get("signals", []):
  cur[t.get("address", "")] = t.get("mcap", 0)

 now = time.time()
 for addr in list(CI_PAPER_HOLDINGS.keys()):
  h = CI_PAPER_HOLDINGS[addr]
  entry_mcap = h.get("entryMcap", 0)
  current_mcap = cur.get(addr, 0)

  if not current_mcap or not entry_mcap:
   # Dropped off CI — sell after 30 min
   held = now - h.get("buyTs", now)
   if held > 1800:
    ci_paper_sell(addr, "Dropped off CI 30m")
   continue

  # Skip SL/TP checks within first 60 seconds of buy
  # (prevents false triggers from stale data on same scan cycle)
  held_sec = now - h.get("buyTs", now)
  if held_sec < 60:
   continue

  pnl_pct = ((current_mcap - entry_mcap) / entry_mcap) * 100
  if pnl_pct > h.get("highestPct", 0):
   h["highestPct"] = pnl_pct

  # TP
  if pnl_pct >= CI_PAPER_TP_PCT:
   ci_paper_sell(addr, f"TP hit +{pnl_pct:.0f}%", current_mcap)
   continue
  # SL
  if pnl_pct <= CI_PAPER_SL_PCT:
   ci_paper_sell(addr, f"SL hit {pnl_pct:.0f}%", current_mcap)
   continue
  # Trailing
  highest = h.get("highestPct", 0)
  if highest >= 50 and pnl_pct < highest * 0.5:
   ci_paper_sell(addr, f"Trailing (peak {highest:.0f}%, now {pnl_pct:.0f}%)", current_mcap)

def ci_auto_paper_engine():
 """Background engine: scan CI every 60s → buy Tier1 → monitor SL/TP."""
 global CI_PAPER_ENABLED
 ci_paper_trade_load()
 add_bot_log("ci-engine", "🧠 CI Tier1 Auto-Paper Engine started")

 while True:
  if not CI_PAPER_ENABLED:
   time.sleep(3)
   continue
  try:
   from ci_tier1_scorer import score_all_tokens, fetch_ci_data
   ci_data = fetch_ci_data()
   tokens = score_all_tokens(ci_data)
   tier1 = [t for t in tokens if t["tier"] == "TIER1"]

   t1c = len(tier1)
   t2c = len([t for t in tokens if t["tier"] == "TIER2"])
   add_bot_log("ci-scan", f"CI Scan: T1={t1c} T2={t2c} | Holdings={len(CI_PAPER_HOLDINGS)} | SOL={CI_PAPER_SOL_BALANCE:.4f}")

   # Buy Tier1
   for t in tier1:
    if t["address"] not in CI_PAPER_HOLDINGS:
     ok, msg = ci_paper_buy(t)
     if ok:
      add_bot_log("ci-buy", f"✅ {msg}")
     else:
      add_bot_log("ci-scan", f"⏭️ {t.get('symbol','?')}: {msg}")

   # Monitor SL/TP
   ci_paper_check_sl_tp(ci_data)

  except Exception as e:
   add_bot_log("ci-error", f"CI engine error: {e}")

  time.sleep(5)  # 5s scan interval


# -------------------------------------------------------------
# Autonomous Copy Trading & SL/TP Background Loop
# -------------------------------------------------------------
def autonomous_trading_engine():
    """Background engine that monitors target wallet and enforces SL/TP rules."""
    global BOT_ENABLED, SEEN_TX_SIGNATURES, ACTIVE_POSITIONS, GLOBAL_PRIVATE_KEY, client
    
    add_bot_log("bot", "స్వయంచాలక కాపీ-ట్రేడింగ్ బాట్ బ్యాక్‌గ్రౌండ్‌లో ప్రారంభించబడింది.")
    add_bot_log("bot", f"వేల్ టార్గెట్ వాలెట్: {BOT_TARGET_WHALE} | కొనుగోలు SOL: {BOT_MAX_BUY_SOL}")

    # Bootstrap seen signatures so we only trade NEW transactions
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [BOT_TARGET_WHALE, {"limit": 5}]
        }
        r = requests.post(RPC_URL, json=payload, timeout=5)
        if r.status_code == 200:
            sigs = r.json().get("result", [])
            for s in sigs:
                if s.get("signature"):
                    SEEN_TX_SIGNATURES.add(s["signature"])
    except Exception as e:
        add_bot_log("bot", f"సిగ్నేచర్స్ బూట్‌స్ట్రాప్ లోపం: {e}")

    last_sltp_check = 0
    
    while True:
        if not BOT_ENABLED:
            time.sleep(1)
            continue
            
        now_ts = time.time()
        
        # 1. On-Chain Copy Monitoring (Every 3 seconds)
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignaturesForAddress",
                "params": [BOT_TARGET_WHALE, {"limit": 3}]
            }
            resp = requests.post(RPC_URL, json=payload, timeout=5)
            if resp.status_code == 200:
                signatures = resp.json().get("result", [])
                
                # Check for brand new transactions
                new_sigs = []
                for s in signatures:
                    sig = s.get("signature")
                    if sig and sig not in SEEN_TX_SIGNATURES:
                        new_sigs.append(sig)
                        SEEN_TX_SIGNATURES.add(sig)
                        
                # Process oldest new transaction first
                for sig in reversed(new_sigs):
                    add_bot_log("scan", f"కొత్త వేల్ ట్రాన్సాక్షన్ కనుగొనబడింది: {sig[:8]}...")
                    
                    # Fetch detailed transaction
                    tx_payload = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getTransaction",
                        "params": [
                            sig,
                            {
                                "encoding": "json",
                                "maxSupportedTransactionVersion": 0
                            }
                        ]
                    }
                    tx_resp = requests.post(RPC_URL, json=tx_payload, timeout=5)
                    if tx_resp.status_code == 200:
                        tx_detail = tx_resp.json().get("result")
                        if not tx_detail:
                            continue
                            
                        parsed = parse_whale_swap(tx_detail, BOT_TARGET_WHALE)
                        if parsed:
                            action, mint, sol_val, tok_val = parsed
                            add_bot_log("whale", f"వేల్ కదలిక: {action.upper()} | టోకెన్: {mint[:10]}... | SOL: {sol_val:.4f}")
                            
                            # Execute Copy-Trade
                            if action == "buy":
                                execute_copy_buy(mint)
                            elif action == "sell":
                                execute_copy_sell(mint)
                                
        except Exception as e:
            add_bot_log("error", f"బ్లాక్‌చైన్ స్కానింగ్ లోపం: {str(e)}")
            
        # 2. Stop-Loss & Take-Profit Monitor (Every 6 seconds)
        if now_ts - last_sltp_check >= 6:
            last_sltp_check = now_ts
            monitor_stoploss_takeprofit()
            
        time.sleep(3)


def execute_copy_buy(mint: str):
    """Executes a copy buy trade locally via memory-loaded private key."""
    global GLOBAL_PRIVATE_KEY, client, BOT_MAX_BUY_SOL, BOT_SLIPPAGE, BOT_PRIORITY_FEE, ACTIVE_POSITIONS
    
    if not GLOBAL_PRIVATE_KEY:
        add_bot_log("security", "ట్రేడింగ్ లాక్ చేయబడింది. లైవ్ ట్రేడింగ్ కోసం ప్రైవేట్ కీని లోడ్ చేయండి.")
        return
        
    add_bot_log("copy", f"⚡️ కాపీ ఆర్డర్ ట్రిగ్గర్ అయింది! {BOT_MAX_BUY_SOL} SOL విలువ గల టోకెన్‌ను కొంటున్నాము...")
    
    t_info = get_dexscreener_token_info(mint)
    symbol = t_info.get("symbol", "MEME")
    name = t_info.get("name", "Meme Token")
    usd_entry = t_info.get("priceUsd", 0.0)

    try:
        res = client.buy_token(
            private_key=GLOBAL_PRIVATE_KEY,
            token_mint=mint,
            amount=BOT_MAX_BUY_SOL,
            slippage_percent=BOT_SLIPPAGE,
            priority_fee=BOT_PRIORITY_FEE,
            rpc_url=RPC_URL
        )
        if res.get("success"):
            sig = res.get("signature")
            add_bot_log("success", f"కొనుగోలు విజయవంతమైంది! టోకెన్: {symbol} | సిగ్నేచర్: {sig[:12]}...")
            
            if mint not in ACTIVE_POSITIONS:
                ACTIVE_POSITIONS[mint] = {
                    "symbol": symbol,
                    "name": name,
                    "balance": 0.0,
                    "entry_price_usd": usd_entry if usd_entry > 0 else 0.000001,
                    "entry_sol": BOT_MAX_BUY_SOL,
                    "current_price_usd": usd_entry,
                    "pnl_percent": 0.0
                }
            else:
                ACTIVE_POSITIONS[mint]["entry_sol"] += BOT_MAX_BUY_SOL
            save_active_positions()
        else:
            add_bot_log("failed", f"కొనుగోలు విఫలమైంది: {res.get('error')}")
    except Exception as e:
        add_bot_log("error", f"కొనుగోలు ఎగ్జిక్యూషన్ లోపం: {e}")


def execute_copy_sell(mint: str):
    """Executes a copy sell trade locally when target whale sells."""
    global GLOBAL_PRIVATE_KEY, client, BOT_SLIPPAGE, BOT_PRIORITY_FEE, ACTIVE_POSITIONS
    
    if not GLOBAL_PRIVATE_KEY:
        return
        
    if mint not in ACTIVE_POSITIONS:
        add_bot_log("copy", f"వేల్ టోకెన్ {mint[:8]}... ను అమ్మారు, కానీ మీ పొజిషన్లు లేవు.")
        return
        
    symbol = ACTIVE_POSITIONS[mint].get("symbol", "MEME")
    add_bot_log("copy", f"⚡️ వేల్ అమ్మారు! మీ వాలెట్ నుండి 100% {symbol} ను ఆటోమేటిక్‌గా అమ్ముతున్నాము...")
    
    try:
        res = client.sell_token(
            private_key=GLOBAL_PRIVATE_KEY,
            token_mint=mint,
            amount="100%",
            slippage_percent=BOT_SLIPPAGE,
            priority_fee=BOT_PRIORITY_FEE,
            rpc_url=RPC_URL
        )
        if res.get("success"):
            sig = res.get("signature")
            add_bot_log("success", f"అమ్మకం విజయవంతమైంది! టోకెన్: {symbol} | సిగ్నేచర్: {sig[:12]}...")
            if mint in ACTIVE_POSITIONS:
                del ACTIVE_POSITIONS[mint]
            save_active_positions()
        else:
            add_bot_log("failed", f"అమ్మకం విఫలమైంది: {res.get('error')}")
    except Exception as e:
        add_bot_log("error", f"అమ్మకం ఎగ్జిక్యూషన్ లోపం: {e}")


def monitor_stoploss_takeprofit():
    """Iterates active positions and checks SL/TP rules against DexScreener pricing."""
    global ACTIVE_POSITIONS, BOT_TAKE_PROFIT_ENABLED, BOT_TAKE_PROFIT_PCT, BOT_STOP_LOSS_ENABLED, BOT_STOP_LOSS_PCT, GLOBAL_PRIVATE_KEY
    
    if not ACTIVE_POSITIONS or not GLOBAL_PRIVATE_KEY:
        return
        
    for mint in list(ACTIVE_POSITIONS.keys()):
        pos = ACTIVE_POSITIONS[mint]
        symbol = pos["symbol"]
        entry_price = pos["entry_price_usd"]
        
        info = get_dexscreener_token_info(mint)
        if not info.get("success"):
            continue
            
        cur_price = info["priceUsd"]
        pos["current_price_usd"] = cur_price
        
        # Calculate change %
        if entry_price > 0:
            change_pct = ((cur_price - entry_price) / entry_price) * 100.0
        else:
            change_pct = 0.0
            
        pos["pnl_percent"] = change_pct
        
        # Check Stop Loss
        if BOT_STOP_LOSS_ENABLED and change_pct <= -abs(BOT_STOP_LOSS_PCT):
            add_bot_log("sltp", f"🔴 STOP-LOSS ట్రిగ్గర్ అయింది! టోకెన్: {symbol} ({change_pct:.2f}%)")
            execute_copy_sell(mint)
            
        # Check Take Profit
        elif BOT_TAKE_PROFIT_ENABLED and change_pct >= BOT_TAKE_PROFIT_PCT:
            add_bot_log("sltp", f"🟢 TAKE-PROFIT ట్రిగ్గర్ అయింది! టోకెన్: {symbol} (+{change_pct:.2f}%)")
            execute_copy_sell(mint)


# -------------------------------------------------------------
# On-Chain Portfolio Migration Monitor (Real-time alert engine)
# -------------------------------------------------------------
async def fetch_wallet_tokens_async(wallet):
    rpc_urls = [
        RPC_URL,
        "https://api.mainnet-beta.solana.com"
    ]
    mints = []
    
    def call_rpc(url, program_id):
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                wallet,
                {"programId": program_id},
                {"encoding": "jsonParsed"}
            ]
        }
        try:
            r = requests.post(url, json=payload, timeout=8)
            if r.status_code == 200:
                res = r.json().get("result", {})
                value = res.get("value", [])
                mints_found = []
                for item in value:
                    info = item.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                    mint = info.get("mint")
                    amount = float(info.get("tokenAmount", {}).get("uiAmount", 0.0) or 0.0)
                    if amount > 0 and mint:
                        mints_found.append(mint)
                return mints_found
        except Exception:
            pass
        return None

    for url in rpc_urls:
        res_std = await asyncio.get_event_loop().run_in_executor(None, call_rpc, url, "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
        if res_std is not None:
            mints.extend(res_std)
            
        res_2022 = await asyncio.get_event_loop().run_in_executor(None, call_rpc, url, "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
        if res_2022 is not None:
            mints.extend(res_2022)
            
        if res_std is not None or res_2022 is not None:
            break
            
    return list(set(mints))

async def check_token_migration_status_async(mint):
    def check():
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                pairs = resp.json().get("pairs", [])
                for p in pairs:
                    if p.get("dexId") != "pumpfun":
                        return True
        except Exception:
            pass
        return False
        
    return await asyncio.get_event_loop().run_in_executor(None, check)

async def send_migration_alert_async(tg_client, target_chat, token_data):
    mint = token_data.get("tokenAddress") or token_data.get("address") or token_data.get("pairAddress")
    name = token_data.get("tokenName") or token_data.get("name") or "Unknown"
    symbol = token_data.get("tokenTicker") or token_data.get("symbol") or "MEME"
    
    try:
        mcap = float(token_data.get("marketCapUsd") or token_data.get("marketCap") or 0.0)
    except (ValueError, TypeError):
        mcap = 0.0
        
    try:
        liq_sol = float(token_data.get("quoteLiquidity") or token_data.get("liquiditySol") or 0.0)
    except (ValueError, TypeError):
        liq_sol = 0.0
        
    # Estimate USD value assuming SOL is $180
    liq_usd = liq_sol * 180.0
    
    try:
        chg_5m = float(token_data.get("priceChange5m") or token_data.get("priceChange") or 0.0)
    except (ValueError, TypeError):
        chg_5m = 0.0
        
    try:
        chg_1h = float(token_data.get("priceChange1h") or 0.0)
    except (ValueError, TypeError):
        chg_1h = 0.0
        
    try:
        holders = int(token_data.get("holderCount") or 0)
    except (ValueError, TypeError):
        holders = 0
        
    try:
        insider_pct = float(token_data.get("insiderPercentage") or 0.0)
    except (ValueError, TypeError):
        insider_pct = 0.0
        
    burnt = "yes" if insider_pct < 5.0 else "no"

    def fmt_val(num):
        if num >= 1e6: return f"{num/1e6:.1f}M"
        if num >= 1e3: return f"{num/1e3:.1f}K"
        return f"{num:.1f}"

    def fmt_pct(num):
        return f"+{num:.1f}%" if num > 0 else f"{num:.1f}%"

    symbol_uc = symbol.upper()
    mcap_val = f"${fmt_val(mcap)}" if mcap else "N/A"
    liq_val = f"${fmt_val(liq_usd)} ({liq_sol:.1f} SOL)" if liq_sol else "N/A"
    holders_val = str(holders) if holders else "N/A"
    chg_5m_val = fmt_pct(chg_5m)
    chg_1h_val = fmt_pct(chg_1h)
    
    msg = f"{symbol_uc}\n"
    msg += f"CA: {mint}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"📈 MCap: {mcap_val}\n"
    msg += f"💧 Liq: {liq_val}\n"
    msg += f"👥 Holders: {holders_val}\n"
    msg += f"🔥 Burnt: {burnt}\n"
    msg += f"🚀 5m: {chg_5m_val}\n"
    msg += f"📊 1h: {chg_1h_val}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"AXIOM: https://axiom.trade/t/{mint}/@rick?chain=sol\n"
    msg += "━━━━━━━━━━━━━━━━━━━━"
    
    # Resolve chat ID for Bot API
    chat_id = None
    if hasattr(target_chat, 'id'):
        chat_id = target_chat.id
    elif isinstance(target_chat, (int, str)):
        chat_id = target_chat
        
    if chat_id:
        chat_id_str = str(chat_id)
        if not chat_id_str.startswith("-"):
            chat_id = int(f"-100{chat_id}")
            
    bot_token = os.getenv("TG_BOT_TOKEN")
    
    if bot_token and chat_id:
        def send_via_bot():
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": msg,
                "disable_web_page_preview": True
            }
            r = requests.post(url, json=payload, timeout=8)
            r.raise_for_status()
            
        try:
            await asyncio.get_event_loop().run_in_executor(None, send_via_bot)
            add_bot_log("portfolio", f"మైగ్రేషన్ అలర్ట్ విజయవంతంగా బాట్ ద్వారా పంపబడింది: {symbol_uc}")
        except Exception as e:
            add_bot_log("portfolio_error", f"బాట్ ద్వారా మైగ్రేషన్ అలర్ట్ పంపడం విఫలమైింది (falling back to user account): {e}")
            try:
                await tg_client.send_message(target_chat, msg)
                add_bot_log("portfolio", f"మైగ్రేషన్ అలర్ట్ యూజర్ ద్వారా పంపబడింది (fallback): {symbol_uc}")
            except Exception as fe:
                add_bot_log("portfolio_error", f"యూజర్ fallback మైగ్రేషన్ అలర్ట్ కూడా విఫలమైింది: {fe}")
    else:
        try:
            await tg_client.send_message(target_chat, msg)
            add_bot_log("portfolio", f"మైగ్రేషన్ అలర్ట్ యూజర్ ద్వారా పంపబడింది (no bot token): {symbol_uc}")
        except Exception as e:
            add_bot_log("portfolio_error", f"మైగ్రేషన్ అలర్ట్ పంపడం విఫలమైింది: {e}")

async def monitor_axiom_migrations_async(tg_client):
    global client
    add_bot_log("axiom_monitor", "Axiom Trade Migrated కాయిన్స్ పర్యవేక్షణ ప్రారంభించబడింది.")
    
    seen_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_axiom_migrations.json")
    seen_migrations = set()
    if os.path.exists(seen_path):
        try:
            with open(seen_path, "r") as f:
                seen_migrations = set(json.load(f))
        except Exception:
            pass
            
    target_channel_link = "https://t.me/+u6qSggpR88pkYTQ1"
    target_chat = None
    
    try:
        target_chat = await tg_client.get_entity(target_channel_link)
        add_bot_log("axiom_monitor", f"లక్ష్య ఛానెల్ కనెక్ట్ చేయబడింది: {getattr(target_chat, 'title', 'Axiom Trades')}")
    except Exception as e:
        add_bot_log("axiom_monitor", f"లక్ష్య ఛానెల్‌ని పొందలేకపోయాము. (falling back to ID -1004296309055): {e}")
        target_chat = -1004296309055
        
    # Wait for client to be initialized and authenticated
    add_bot_log("axiom_monitor", "Axiom క్లయింట్ కనెక్ట్ అయ్యే వరకు వేచి ఉన్నాము (Waiting for Axiom client to connect)...")
    while not client or not client.is_authenticated():
        await asyncio.sleep(1)
        
    async def get_trending_tokens_async(period):
        def fetch():
            return client.get_trending_tokens(time_period=period)
        return await asyncio.get_event_loop().run_in_executor(None, fetch)

    # Secure startup sweep to populate seen_migrations and prevent spams
    add_bot_log("axiom_monitor", "యాక్టివ్ మైగ్రేషన్ల బేస్‌లైన్ స్వీప్ జరుగుతోంది (Performing baseline sweep)...")
    try:
        for period in ['5m', '1h', '24h']:
            try:
                trending = await get_trending_tokens_async(period)
                tokens = trending.get("tokens", []) or trending.get("data", [])
                for t in tokens:
                    mint = t.get("tokenAddress") or t.get("address") or t.get("pairAddress")
                    if not mint:
                        continue
                    is_mig = t.get("isMigrated")
                    mig_info = t.get("migrationInfo")
                    if is_mig is True or isinstance(mig_info, dict):
                        seen_migrations.add(mint)
            except Exception as pe:
                add_bot_log("axiom_monitor_error", f"బేస్‌లైన్ స్వీప్ లోపం ({period}): {pe}")
        
        # Save baseline to seen_axiom_migrations.json
        try:
            with open(seen_path, "w") as f:
                json.dump(list(seen_migrations), f, indent=2)
        except Exception:
            pass
        add_bot_log("axiom_monitor", f"బేస్‌లైన్ స్వీప్ పూర్తయింది. {len(seen_migrations)} పాత కాయిన్లు స్కిప్ చేయబడతాయి.")
    except Exception as se:
        add_bot_log("axiom_monitor_error", f"బేస్‌లైన్ స్వీప్ విఫలమైంది: {se}")
    
    while True:
        try:
            # Query trending tokens from multiple time periods to ensure we catch all migrations
            client_status = "None" if client is None else ("Authenticated" if client.is_authenticated() else "Not Authenticated")
            add_bot_log("axiom_monitor", f"Loop iteration starting. Client: {client_status}")
            
            if client and client.is_authenticated():
                all_tokens = []
                for period in ['5m', '1h', '24h']:
                    try:
                        add_bot_log("axiom_monitor", f"Fetching period {period}...")
                        trending = await get_trending_tokens_async(period)
                        tokens = trending.get("tokens", []) or trending.get("data", [])
                        all_tokens.extend(tokens)
                        add_bot_log("axiom_monitor", f"Fetched {len(tokens)} tokens for period {period}.")
                    except Exception as pe:
                        add_bot_log("axiom_monitor_error", f"Error polling period {period}: {pe}")
                        
                # Merge duplicates by mint address
                token_map = {}
                for t in all_tokens:
                    mint = t.get("tokenAddress") or t.get("address") or t.get("pairAddress")
                    if mint:
                        token_map[mint] = t
                        
                for mint, t in token_map.items():
                    is_mig = t.get("isMigrated")
                    mig_info = t.get("migrationInfo")
                    
                    if is_mig is True or isinstance(mig_info, dict):
                        if mint not in seen_migrations:
                            symbol = t.get("tokenSymbol") or t.get("symbol") or t.get("tokenTicker") or "MEME"
                            pca_str = t.get("pairCreatedAt")
                            is_new_migration = False
                            
                            if pca_str:
                                try:
                                    pca_str = str(pca_str).strip()
                                    dt = None
                                    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                                        try:
                                            dt = datetime.strptime(pca_str, fmt)
                                            break
                                        except ValueError:
                                            pass
                                    if dt:
                                        diff_seconds = (datetime.utcnow() - dt).total_seconds()
                                        if 0 <= diff_seconds <= 900:  # Within last 15 minutes
                                            is_new_migration = True
                                        else:
                                            add_bot_log("axiom_monitor", f"Skipping old migrated token {symbol} ({mint}) - migrated {diff_seconds/60:.1f} minutes ago.")
                                            seen_migrations.add(mint)
                                            try:
                                                with open(seen_path, "w") as f:
                                                    json.dump(list(seen_migrations), f, indent=2)
                                            except Exception:
                                                pass
                                    else:
                                        add_bot_log("axiom_monitor", f"Could not parse pairCreatedAt '{pca_str}' for {symbol} ({mint}).")
                                except Exception as te:
                                    add_bot_log("axiom_monitor_error", f"Error parsing pairCreatedAt '{pca_str}' for {symbol}: {te}")
                            else:
                                add_bot_log("axiom_monitor", f"No pairCreatedAt timestamp for {symbol} ({mint}). Skipping to be safe.")
                                seen_migrations.add(mint)
                                try:
                                    with open(seen_path, "w") as f:
                                        json.dump(list(seen_migrations), f, indent=2)
                                except Exception:
                                    pass

                            if is_new_migration:
                                seen_migrations.add(mint)
                                try:
                                    with open(seen_path, "w") as f:
                                        json.dump(list(seen_migrations), f, indent=2)
                                except Exception:
                                    pass
                                    
                                add_bot_log("axiom_monitor", f"కొత్తగా మైగ్రేట్ అయిన కాయిన్ కనుగొనబడింది! {symbol} ({mint})")
                                await send_migration_alert_async(tg_client, target_chat, t)
            else:
                add_bot_log("axiom_monitor", "Client is not ready or not authenticated yet. Sleeping.")
                            
        except Exception as e:
            add_bot_log("axiom_monitor_error", f"మైగ్రేషన్ పర్యవేక్షణ లోపం: {e}")
            
        await asyncio.sleep(3)


# -------------------------------------------------------------
# Async Telegram Scraper Thread (Telethon client)
# -------------------------------------------------------------
async def start_telegram_scraper_async():
    """Asynchronous background worker that scrapes Telegram chats for Solana contracts."""
    global TELEGRAM_ALERTS
    load_dotenv()
    tg_api_id = os.getenv("TG_API_ID")
    tg_api_hash = os.getenv("TG_API_HASH")
    
    if not tg_api_id or not tg_api_hash:
        add_bot_log("telegram", "టెలిగ్రామ్ API కీలు లేవు (.env లో సెట్ చేయండి). టెలిగ్రామ్ అలర్ట్స్ ఆఫ్ చేయబడింది.")
        return
        
    try:
        session_path = os.path.join(os.path.dirname(__file__), "axiom_session")
        client_tg = TelegramClient(session_path, int(tg_api_id), tg_api_hash)
        
        global GLOBAL_TG_CLIENT
        add_bot_log("telegram", "టెలిగ్రామ్ కనెక్ట్ అవుతోంది...")
        tg_bot_token = os.getenv("TG_BOT_TOKEN")
        if tg_bot_token:
            add_bot_log("telegram", "బాట్ టోకెన్‌ను ఉపయోగించి కనెక్ట్ అవుతోంది...")
            await client_tg.start(bot_token=tg_bot_token)
        else:
            await client_tg.start()
        GLOBAL_TG_CLIENT = client_tg
        add_bot_log("telegram", "✅ టెలిగ్రామ్ విజయవంతంగా కనెక్ట్ అయింది!")
        
        # Start background portfolio migration monitor
        asyncio.create_task(monitor_axiom_migrations_async(GLOBAL_TG_CLIENT))
        
        await client_tg.run_until_disconnected()
        
    except Exception as e:
        add_bot_log("telegram_error", f"టెలిగ్రామ్ లాగిన్ లోపం లేదా డిస్‌కనెక్ట్: {e}")


def run_tg_loop():
    """Runner function to initialize asyncio loop inside a daemon thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_telegram_scraper_async())


# Start the background threads
# bot_thread = threading.Thread(target=autonomous_trading_engine, daemon=True)
# bot_thread.start()

tg_thread = threading.Thread(target=run_tg_loop, daemon=True)
tg_thread.start()

ci_paper_thread = threading.Thread(target=ci_auto_paper_engine, daemon=True)
ci_paper_thread.start()


# -------------------------------------------------------------
# Custom REST API & Web Server Handler
# -------------------------------------------------------------
class TradingTerminalAPIHandler(http.server.SimpleHTTPRequestHandler):
    """Custom request handler that serves Web UI static assets and REST API endpoints."""
    
    def log_message(self, format, *args):
        # Suppress noise
        pass

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        
        # API: Get wallet balances
        if parsed_url.path == '/api/balance':
            self.handle_api_balance()
            
        # API: Get live trending tokens from Axiom
        elif parsed_url.path == '/api/trending':
            self.handle_api_trending()
            
        # API: Get token statistics & details
        elif parsed_url.path == '/api/stats':
            self.handle_api_stats(parsed_url.query)
            
        # API: Get autonomous bot configuration and terminal logs
        elif parsed_url.path == '/api/bot-status':
            self.handle_api_bot_status()
            
        # API: Get active trading positions and real-time PnL
        elif parsed_url.path == '/api/positions':
            self.handle_api_positions()
            
        # API: Get Telegram live alerts feed
        elif parsed_url.path == '/api/telegram-feed':
         self.handle_api_telegram_feed()

        # API: Full token signals list (filtered from CI)
        elif parsed_url.path == '/api/signals':
         self.handle_api_signals(parsed_url.query)

        # API: CI Tier1 Scored Signals
        elif parsed_url.path == '/api/ci-signals':
         self.handle_api_ci_signals(parsed_url.query)

        # API: CI Paper Trade Dashboard
        elif parsed_url.path == '/api/ci-trade':
         self.handle_api_ci_trade(parsed_url.query)

        # API: CI Paper Trade test (minimal)
        elif parsed_url.path == '/api/ci-test':
         self.send_json_response(200, {"ok": True, "msg": "ci-test works"})

        # API: CI Paper Trade toggle (GET = read status)
        elif parsed_url.path == '/api/ci-paper-toggle':
         self.handle_api_ci_paper_toggle_get()

        # API: CORS Proxy for CI API
        elif parsed_url.path == '/api/ci-proxy':
            self.handle_ci_proxy(parsed_url.query)

        # Serve CI clone site at /ci/
        elif parsed_url.path in ('/ci', '/ci/'):
            self.serve_ci_site()

        # Default: serve static website assets
        else:
            super().do_GET()

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        
        # API: Execute instant manual trade
        if parsed_url.path == '/api/trade':
            self.handle_api_trade()
            
        # API: Toggle autonomous copy trader & update configurations
        elif parsed_url.path == '/api/bot-toggle':
         self.handle_api_bot_toggle()

        # API: Toggle CI Auto Paper Trading
        elif parsed_url.path == '/api/ci-paper-toggle':
         self.handle_api_ci_paper_toggle()

        else:
            self.send_error(404, "Endpoint not found")

    def handle_api_balance(self):
        sol_wallets = [
            "LSPFGsBKQYhEAHDqNmj6dYHHTexfTsnEQAxwsCtxh23",
            "6bHKBQqNGDevgG1VdqYSjUftSN5guFWiX7XVtKugajDD"
        ]
        balances = {}
        for wallet in sol_wallets:
            balances[wallet] = get_sol_balance_rpc(wallet)
        self.send_json_response(200, balances)

    def handle_api_trending(self):
        global client
        try:
            trending = client.get_trending_tokens(time_period='1h')
            tokens = trending.get("tokens", []) or trending.get("data", [])
            self.send_json_response(200, {"tokens": tokens})
        except Exception as e:
            self.send_json_response(500, {"error": str(e), "tokens": []})

    def handle_api_stats(self, query_str):
        params = urllib.parse.parse_qs(query_str)
        pair = params.get('pair', [None])[0]
        
        if not pair:
            self.send_json_response(400, {"success": False, "error": "Missing pair address"})
            return
            
        res = get_dexscreener_token_info(pair)
        self.send_json_response(200, res)

    def handle_api_bot_status(self):
        global BOT_ENABLED, BOT_TARGET_WHALE, BOT_MAX_BUY_SOL, BOT_SLIPPAGE, BOT_PRIORITY_FEE, BOT_TAKE_PROFIT_ENABLED, BOT_TAKE_PROFIT_PCT, BOT_STOP_LOSS_ENABLED, BOT_STOP_LOSS_PCT, BOT_LOGS
        
        status = {
            "success": True,
            "enabled": BOT_ENABLED,
            "targetWhale": BOT_TARGET_WHALE,
            "maxBuySol": BOT_MAX_BUY_SOL,
            "slippage": BOT_SLIPPAGE,
            "fee": BOT_PRIORITY_FEE,
            "tpEnabled": BOT_TAKE_PROFIT_ENABLED,
            "tpPct": BOT_TAKE_PROFIT_PCT,
            "slEnabled": BOT_STOP_LOSS_ENABLED,
            "slPct": BOT_STOP_LOSS_PCT,
            "logs": BOT_LOGS
        }
        self.send_json_response(200, status)

    def handle_api_positions(self):
        global ACTIVE_POSITIONS
        for mint in list(ACTIVE_POSITIONS.keys()):
            pos = ACTIVE_POSITIONS[mint]
            info = get_dexscreener_token_info(mint)
            if info.get("success"):
                pos["current_price_usd"] = info["priceUsd"]
                if pos["entry_price_usd"] > 0:
                    pos["pnl_percent"] = ((info["priceUsd"] - pos["entry_price_usd"]) / pos["entry_price_usd"]) * 100.0
                    
        self.send_json_response(200, {"positions": ACTIVE_POSITIONS})

    def handle_api_telegram_feed(self):
     global TELEGRAM_ALERTS
     self.send_json_response(200, {"alerts": TELEGRAM_ALERTS})

    def handle_api_ci_signals(self, query_str):
     """CI Tier1 Scored Signals — reverse-engineered from Circle Intelligence."""
     global CI_SIGNALS_CACHE
     try:
      from ci_tier1_scorer import score_all_tokens, get_tier1_signals
     except ImportError:
      self.send_json_response(500, {"error": "ci_tier1_scorer module not found"})
      return

     now = time.time()
     params = urllib.parse.parse_qs(query_str)
     tier_filter = params.get('tier', [None])[0]  # tier1, tier2, tier3, or all
     refresh = params.get('refresh', ['0'])[0] == '1'

     # Cache for 55 seconds (avoid hammering CI API)
     if refresh or now - CI_SIGNALS_CACHE["updated"] > 55:
      try:
       tokens = score_all_tokens()
       CI_SIGNALS_CACHE["tokens"] = tokens
       CI_SIGNALS_CACHE["tier1"] = [t for t in tokens if t["tier"] == "TIER1"]
       CI_SIGNALS_CACHE["tier2"] = [t for t in tokens if t["tier"] == "TIER2"]
       CI_SIGNALS_CACHE["tier3"] = [t for t in tokens if t["tier"] == "TIER3"]
       CI_SIGNALS_CACHE["updated"] = now
      except Exception as e:
       self.send_json_response(500, {"error": str(e), "tokens": CI_SIGNALS_CACHE["tokens"]})
       return

     if tier_filter == "tier1":
      result = CI_SIGNALS_CACHE["tier1"]
     elif tier_filter == "tier2":
      result = CI_SIGNALS_CACHE["tier2"]
     elif tier_filter == "tier3":
      result = CI_SIGNALS_CACHE["tier3"]
     else:
      result = CI_SIGNALS_CACHE["tokens"]

     self.send_json_response(200, {
      "success": True,
      "count": len(result),
      "tier1Count": len(CI_SIGNALS_CACHE["tier1"]),
      "tier2Count": len(CI_SIGNALS_CACHE["tier2"]),
      "tier3Count": len(CI_SIGNALS_CACHE["tier3"]),
      "updated": CI_SIGNALS_CACHE["updated"],
      "tokens": result
     })

    def handle_api_ci_trade(self, query_str):
     """CI Paper Trade status — holdings, trades, stats."""
     try:
      global CI_PAPER_ENABLED, CI_PAPER_BUY_SOL, CI_PAPER_SL_PCT, CI_PAPER_TP_PCT
      global CI_PAPER_MAX_POS, CI_PAPER_COOLDOWN, CI_PAPER_SOL_BALANCE
      global CI_PAPER_HOLDINGS, CI_PAPER_TRADES, CI_PAPER_STATS, CI_PAPER_LAST_BUY_TS
      ci_paper_trade_load()
      params = urllib.parse.parse_qs(query_str)
      action = params.get('action', ['status'])[0]

      if action == "status":
       sells = CI_PAPER_STATS.get("total_sells", 0)
       wr = (CI_PAPER_STATS.get("wins", 0) / sells * 100) if sells > 0 else 0
       enriched = []
       for addr, h in CI_PAPER_HOLDINGS.items():
        entry_mc = h.get("entryMcap", 0)
        cur_mc = 0
        for t in CI_SIGNALS_CACHE.get("tokens", []):
         if t.get("address") == addr:
          cur_mc = t.get("mcap", 0)
          break
        pnl = ((cur_mc - entry_mc) / entry_mc * 100) if entry_mc > 0 and cur_mc > 0 else 0
        held_sec = int(time.time() - h.get("buyTs", time.time()))
        enriched.append({
         "address": addr, "symbol": h.get("symbol", "?"),
         "ciScore": h.get("ciScore", 0), "entryMcap": entry_mc,
         "currentMcap": cur_mc, "pnlPct": round(pnl, 1),
         "highestPct": h.get("highestPct", 0),
         "heldSec": held_sec, "entrySol": h.get("entrySol", 0),
        })
       recent = CI_PAPER_TRADES[-20:] if CI_PAPER_TRADES else []
       stats_out = {k: v for k, v in CI_PAPER_STATS.items()}
       stats_out["winRate"] = round(wr, 1)
       resp = {
        "success": True,
        "enabled": CI_PAPER_ENABLED,
        "config": {
         "buySol": CI_PAPER_BUY_SOL,
         "slPct": CI_PAPER_SL_PCT,
         "tpPct": CI_PAPER_TP_PCT,
         "maxPos": CI_PAPER_MAX_POS,
         "cooldown": CI_PAPER_COOLDOWN,
        },
        "solBalance": round(CI_PAPER_SOL_BALANCE, 6),
        "holdings": enriched,
        "holdingsCount": len(CI_PAPER_HOLDINGS),
        "recentTrades": recent,
        "stats": stats_out,
       }
       self.send_json_response(200, resp)

      elif action == "reset":
       CI_PAPER_SOL_BALANCE = 1.0
       CI_PAPER_HOLDINGS = {}
       CI_PAPER_TRADES = []
       CI_PAPER_STATS = {"total_buys": 0, "total_sells": 0, "wins": 0, "losses": 0, "total_pnl_sol": 0.0, "best_pct": 0.0, "worst_pct": 0.0}
       CI_PAPER_LAST_BUY_TS = 0
       ci_paper_trade_save()
       self.send_json_response(200, {"success": True, "message": "Paper trade state reset"})

      else:
       self.send_json_response(400, {"error": f"Unknown action: {action}. Use: status, reset"})
     except Exception as e:
      import traceback as tb
      tb.print_exc()
      try:
       self.send_json_response(500, {"error": str(e), "handler": "ci-trade"})
      except Exception:
       self.send_response(500)
       self.end_headers()

    def handle_api_ci_paper_toggle(self):
     """Toggle CI auto-paper trading on/off and update config."""
     global CI_PAPER_ENABLED, CI_PAPER_BUY_SOL, CI_PAPER_SL_PCT, CI_PAPER_TP_PCT
     global CI_PAPER_MAX_POS, CI_PAPER_COOLDOWN
     try:
      content_length = int(self.headers.get('Content-Length', 0))
      if content_length > 0:
       body = json.loads(self.rfile.read(content_length).decode())
      else:
       body = {}

      enabled = body.get("enabled", None)
      if enabled is not None:
       CI_PAPER_ENABLED = bool(enabled)
       add_bot_log("ci-engine", f"🧠 CI Auto-Paper {'ENABLED' if CI_PAPER_ENABLED else 'DISABLED'}")

      if "buySol" in body:
       CI_PAPER_BUY_SOL = float(body["buySol"])
      if "slPct" in body:
       CI_PAPER_SL_PCT = float(body["slPct"])
      if "tpPct" in body:
       CI_PAPER_TP_PCT = float(body["tpPct"])
      if "maxPos" in body:
       CI_PAPER_MAX_POS = int(body["maxPos"])
      if "cooldown" in body:
       CI_PAPER_COOLDOWN = int(body["cooldown"])

      ci_paper_trade_save()

      self.send_json_response(200, {
       "success": True,
       "enabled": CI_PAPER_ENABLED,
       "config": {
        "buySol": CI_PAPER_BUY_SOL,
        "slPct": CI_PAPER_SL_PCT,
        "tpPct": CI_PAPER_TP_PCT,
        "maxPos": CI_PAPER_MAX_POS,
        "cooldown": CI_PAPER_COOLDOWN,
       }
      })
     except Exception as e:
      self.send_json_response(500, {"error": str(e), "handler": "ci-paper-toggle"})

    def handle_api_ci_paper_toggle_get(self):
     """GET variant — returns current CI paper trade config without modifying."""
     global CI_PAPER_ENABLED, CI_PAPER_BUY_SOL, CI_PAPER_SL_PCT, CI_PAPER_TP_PCT
     global CI_PAPER_MAX_POS, CI_PAPER_COOLDOWN
     try:
      self.send_json_response(200, {
       "success": True,
       "enabled": CI_PAPER_ENABLED,
       "config": {
        "buySol": CI_PAPER_BUY_SOL,
        "slPct": CI_PAPER_SL_PCT,
        "tpPct": CI_PAPER_TP_PCT,
        "maxPos": CI_PAPER_MAX_POS,
        "cooldown": CI_PAPER_COOLDOWN,
       }
      })
     except Exception as e:
      self.send_json_response(500, {"error": str(e), "handler": "ci-paper-toggle-get"})

    def handle_api_signals(self, query_str):
     """Full signals list — merges CI signals with local filters, returns all scored tokens."""
     global CI_SIGNALS_CACHE
     try:
      from ci_tier1_scorer import score_all_tokens
     except ImportError:
      self.send_json_response(500, {"error": "ci_tier1_scorer module not found"})
      return

     now = time.time()
     params = urllib.parse.parse_qs(query_str)
     refresh = params.get('refresh', ['0'])[0] == '1'
     tier_filter = params.get('tier', [None])[0]
     min_score = int(params.get('minScore', ['0'])[0])

     # Cache for 55s
     if refresh or now - CI_SIGNALS_CACHE["updated"] > 55:
      try:
       tokens = score_all_tokens()
       CI_SIGNALS_CACHE["tokens"] = tokens
       CI_SIGNALS_CACHE["tier1"] = [t for t in tokens if t["tier"] == "TIER1"]
       CI_SIGNALS_CACHE["tier2"] = [t for t in tokens if t["tier"] == "TIER2"]
       CI_SIGNALS_CACHE["tier3"] = [t for t in tokens if t["tier"] == "TIER3"]
       CI_SIGNALS_CACHE["updated"] = now
      except Exception as e:
       self.send_json_response(500, {"error": str(e), "tokens": CI_SIGNALS_CACHE["tokens"]})
       return

     if tier_filter == "tier1":
      result = CI_SIGNALS_CACHE["tier1"]
     elif tier_filter == "tier2":
      result = CI_SIGNALS_CACHE["tier2"]
     elif tier_filter == "tier3":
      result = CI_SIGNALS_CACHE["tier3"]
     else:
      result = CI_SIGNALS_CACHE["tokens"]

     # Apply min score filter
     if min_score > 0:
      result = [t for t in result if t.get("ciScore", 0) >= min_score]

     self.send_json_response(200, {
      "success": True,
      "count": len(result),
      "tier1Count": len(CI_SIGNALS_CACHE["tier1"]),
      "tier2Count": len(CI_SIGNALS_CACHE["tier2"]),
      "tier3Count": len(CI_SIGNALS_CACHE["tier3"]),
      "totalScanned": len(CI_SIGNALS_CACHE["tokens"]),
      "updated": CI_SIGNALS_CACHE["updated"],
      "tokens": result
     })

    def handle_api_bot_toggle(self):
       global BOT_ENABLED, BOT_TARGET_WHALE, BOT_MAX_BUY_SOL, BOT_SLIPPAGE, BOT_PRIORITY_FEE, BOT_TAKE_PROFIT_ENABLED, BOT_TAKE_PROFIT_PCT, BOT_STOP_LOSS_ENABLED, BOT_STOP_LOSS_PCT
       
       content_length = int(self.headers['Content-Length'])
       post_data = self.rfile.read(content_length)
       
       try:
           req = json.loads(post_data.decode('utf-8'))
           BOT_ENABLED = bool(req.get("enabled", BOT_ENABLED))
           BOT_TARGET_WHALE = req.get("targetWhale", BOT_TARGET_WHALE).strip()
           BOT_MAX_BUY_SOL = float(req.get("maxBuySol", BOT_MAX_BUY_SOL))
           BOT_SLIPPAGE = int(req.get("slippage", BOT_SLIPPAGE))
           BOT_PRIORITY_FEE = float(req.get("fee", BOT_PRIORITY_FEE))
           BOT_TAKE_PROFIT_ENABLED = bool(req.get("tpEnabled", BOT_TAKE_PROFIT_ENABLED))
           BOT_TAKE_PROFIT_PCT = float(req.get("tpPct", BOT_TAKE_PROFIT_PCT))
           BOT_STOP_LOSS_ENABLED = bool(req.get("slEnabled", BOT_STOP_LOSS_ENABLED))
           BOT_STOP_LOSS_PCT = float(req.get("slPct", BOT_STOP_LOSS_PCT))
           
           status_text = "ఆన్ చేయబడింది (ACTIVE)" if BOT_ENABLED else "ఆఫ్ చేయబడింది (OFF)"
           add_bot_log("config", f"బాట్ కాన్ఫిగరేషన్ స్థితి: {status_text}")
           
           self.send_json_response(200, {"success": True, "enabled": BOT_ENABLED})
       except Exception as e:
           self.send_json_response(400, {"success": False, "error": str(e)})

    def handle_api_trade(self):
        global GLOBAL_PRIVATE_KEY, client, ACTIVE_POSITIONS
        
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        
        try:
            req = json.loads(post_data.decode('utf-8'))
        except Exception:
            self.send_json_response(400, {"success": False, "error": "Invalid JSON payload"})
            return

        action = req.get('action')
        mint = req.get('mint')
        amount_val = req.get('amount')
        slippage = req.get('slippage', 10)
        fee = req.get('fee', 0.0001)

        if not GLOBAL_PRIVATE_KEY:
            self.send_json_response(200, {
                "success": False, 
                "error": "ట్రేడింగ్ లాక్ చేయబడింది. సర్వర్ స్టార్ట్ చేసేటప్పుడు ప్రైవేట్ కీ ఎంటర్ చేయండి."
            })
            return

        if not action or not mint or not amount_val:
            self.send_json_response(400, {"success": False, "error": "Missing required trade details"})
            return

        try:
            if action == 'buy':
                amount = float(amount_val)
                res = client.buy_token(
                    private_key=GLOBAL_PRIVATE_KEY,
                    token_mint=mint,
                    amount=amount,
                    slippage_percent=slippage,
                    priority_fee=fee,
                    rpc_url=RPC_URL
                )
                if res.get("success"):
                    t_info = get_dexscreener_token_info(mint)
                    symbol = t_info.get("symbol", "MEME")
                    name = t_info.get("name", "Meme Token")
                    usd_entry = t_info.get("priceUsd", 0.0)
                    
                    if mint not in ACTIVE_POSITIONS:
                        ACTIVE_POSITIONS[mint] = {
                            "symbol": symbol,
                            "name": name,
                            "balance": 0.0,
                            "entry_price_usd": usd_entry if usd_entry > 0 else 0.000001,
                            "entry_sol": amount,
                            "current_price_usd": usd_entry,
                            "pnl_percent": 0.0
                        }
                    else:
                        ACTIVE_POSITIONS[mint]["entry_sol"] += amount
                    save_active_positions()
                        
            elif action == 'sell':
                try:
                    amount = float(amount_val)
                except ValueError:
                    amount = str(amount_val)
                    
                res = client.sell_token(
                    private_key=GLOBAL_PRIVATE_KEY,
                    token_mint=mint,
                    amount=amount,
                    slippage_percent=slippage,
                    priority_fee=fee,
                    rpc_url=RPC_URL
                )
                if res.get("success"):
                    if amount_val == "100%" or amount == "100%":
                        if mint in ACTIVE_POSITIONS:
                            del ACTIVE_POSITIONS[mint]
                        save_active_positions()
            else:
                self.send_json_response(400, {"success": False, "error": "Invalid action"})
                return

            if res.get("success"):
                self.send_json_response(200, {
                    "success": True,
                    "signature": res.get("signature"),
                    "explorer_url": res.get("explorer_url")
                })
            else:
                self.send_json_response(200, {
                    "success": False,
                    "error": res.get("error")
                })
                
        except Exception as e:
            self.send_json_response(200, {"success": False, "error": str(e)})

   
    def get_ci_cookie_header(self):
        try:
            cookie_file = "/tmp/ci_cookies.txt"
            cookies = []
            if os.path.exists(cookie_file):
                with open(cookie_file, "r") as f:
                    for line in f:
                        if line.startswith("#") or not line.strip():
                            continue
                        parts = line.strip().split("\t")
                        if len(parts) >= 7:
                            cookies.append(f"{parts[5]}={parts[6]}")
            if cookies:
                return "; ".join(cookies)
        except Exception:
            pass
        return ""

    def handle_ci_proxy(self, query_string):
        """CORS proxy: fetch CI API data server-side and return to browser."""
        try:
            params = urllib.parse.parse_qs(query_string)
            url = params.get('url', [''])[0]
            if not url or 'circleintelligence.in' not in url:
                self.send_json_response(400, {"error": "Invalid URL"})
                return
            cookie_str = self.get_ci_cookie_header()
            headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}
            if cookie_str:
                headers['Cookie'] = cookie_str
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(data)
        except Exception as e:
            self.send_json_response(502, {"error": str(e)})

    def serve_ci_site(self):
        """Serve the CI clone site."""
        try:
            ci_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ci-site', 'index.html')
            with open(ci_path, 'r') as f:
                html = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(html.encode())
        except Exception as e:
            self.send_json_response(500, {"error": str(e)})


    def do_OPTIONS(self):
        """Handle CORS preflight requests."""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('Access-Control-Max-Age', '86400')
        self.end_headers()

    def send_json_response(self, status_code, data):
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))
   
   
# -------------------------------------------------------------
# Main Application Bootstrapper
# -------------------------------------------------------------
def run_server(port=5000):
    global GLOBAL_PRIVATE_KEY, client
    
    # Load env for secure tokens
    load_dotenv()
    load_active_positions()
    access_token = os.getenv("AXIOM_ACCESS_TOKEN")
    refresh_token = os.getenv("AXIOM_REFRESH_TOKEN")

    if not access_token or not refresh_token:
        print("[!] ERROR: No active secure tokens found in '.env'!")
        print("    Please ensure your browser cookies are saved to '.env' first.")
        sys.exit(1)

    print("=" * 70)
    print("        AXIOM LOCAL TRADING STATION & AUTONOMOUS BOT        ")
    print("=" * 70)
    
    # Initialize Axiom Client
    try:
        client = AxiomTradeClient()
        client.set_tokens(access_token=access_token, refresh_token=refresh_token)
        if not client.is_authenticated():
            print("[*] Refreshing browser session...")
            client.refresh_access_token()
        
        if client.is_authenticated():
            print("[+] Connected to Axiom Trade Engine successfully!")
        else:
            print("[!] Warning: Auth tokens could not be verified. Some features may fail.")
    except Exception as e:
        print(f"[!] Warning: Axiom Engine initialization error: {e}")

    # Securely ask for Private Key (stored only in volatile RAM)
    GLOBAL_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY") or os.getenv("PRIVATE_KEY") or ""
    if GLOBAL_PRIVATE_KEY:
        GLOBAL_PRIVATE_KEY = GLOBAL_PRIVATE_KEY.strip()
        print("[+] Solana Private Key loaded successfully from .env!")
        
    if not GLOBAL_PRIVATE_KEY and sys.stdin.isatty():
        print("\n[!] SECURITY POLICY:")
        print("    To execute live BUY/SELL orders and enable autonomous copy-trading,")
        print("    enter your Solana Private Key (Base58 string).")
        print("    If you just want to track targets and test, hit Enter for READ-ONLY mode.")
        try:
            key_input = getpass.getpass("Enter Private Key (hidden): ").strip()
            if key_input:
                GLOBAL_PRIVATE_KEY = key_input
                print("[+] Live Trading Activated! Key stored securely in local memory.")
            else:
                print("[*] Running in READ-ONLY mode. Live order submission is locked.")
        except Exception:
            print("[*] Keyboard input not available. Running in READ-ONLY mode.")
    else:
        print("\n[*] Background execution detected. Running in READ-ONLY mode.")

    # Start HTTP server
    handler = TradingTerminalAPIHandler
    socketserver.TCPServer.allow_reuse_address = True
    
    try:
        with socketserver.TCPServer(("", port), handler) as httpd:
            print(f"\n[+] SUCCESS! Web Trading Terminal is running live.")
            print(f"    Open your browser and navigate to: http://localhost:{port}")
            print(f"    Press Ctrl+C to terminate the server safely.")
            print("-" * 70)
            httpd.serve_forever()
            
    except KeyboardInterrupt:
        print("\n[*] Shutting down Web Server safely. Credentials cleared from memory.")
    except Exception as e:
        print(f"[!] Server error: {e}")
        
    print("=" * 70)


if __name__ == "__main__":
    port_num = 5000
    if len(sys.argv) > 1:
        try:
            port_num = int(sys.argv[1])
        except ValueError:
            pass
            
    run_server(port=port_num)

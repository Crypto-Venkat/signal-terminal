#!/usr/bin/env python3
"""
Daily Performance Report Generator
Runs at 7 AM & 7 PM IST (1:30 UTC & 13:30 UTC)
Analyzes sent_tokens.json against current CI data
Sends formatted report to Telegram channel
"""
import json
import os
import requests
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

# Config
NGROK_URL = "https://signal-terminal.duckdns.org"
BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
CHAT_ID = "-1004296309055"
SENT_FILE = "/home/ubuntu/meme/sent_tokens.json"
CI_API = f"{NGROK_URL}/api/ci-signals?refresh=1"

if not BOT_TOKEN:
    raise RuntimeError("TG_BOT_TOKEN env var not set")

def load_sent_tokens():
    """Load sent tokens with metadata"""
    try:
        with open(SENT_FILE, 'r') as f:
            data = json.load(f)
        # If old format (just addresses), return as set
        if data and isinstance(data[0], str):
            return {addr: {"address": addr, "sent_at": None} for addr in data}
        # New format with metadata
        return {item["address"]: item for item in data}
    except:
        return {}

def fetch_current_ci_data():
    """Fetch current CI signals with full token data"""
    try:
        with urllib.request.urlopen(CI_API, timeout=15) as resp:
            return json.load(resp)
    except Exception as e:
        print(f"CI API error: {e}")
        return {"tokens": []}

def get_token_current_data(address, ci_tokens):
    """Find current data for a token address"""
    for token in ci_tokens:
        if token["address"] == address:
            return token
    return None

def calculate_performance(sent_tokens, ci_tokens):
    """Calculate performance metrics for all sent tokens"""
    results = {
        "total_scanned": len(ci_tokens),
        "total_alerted": len(sent_tokens),
        "rekt": [],
        "neutral": [],
        "pump": [],
        "unknown": []
    }
    
    for addr, sent_info in sent_tokens.items():
        current = get_token_current_data(addr, ci_tokens)
        
        if not current:
            results["unknown"].append({
                "address": addr,
                "symbol": sent_info.get("symbol", "UNKNOWN"),
                "sent_at": sent_info.get("sent_at"),
                "entry_mcap": sent_info.get("entry_mcap"),
                "reason": "Not in current CI data"
            })
            continue
        
        entry_mcap = sent_info.get("entry_mcap") or current.get("mcap", 0)
        current_mcap = current.get("mcap", 0)
        
        if entry_mcap <= 0:
            results["unknown"].append({
                "address": addr,
                "symbol": current.get("symbol", "UNKNOWN"),
                "sent_at": sent_info.get("sent_at"),
                "entry_mcap": entry_mcap,
                "current_mcap": current_mcap,
                "reason": "No entry mcap recorded"
            })
            continue
        
        change_pct = ((current_mcap - entry_mcap) / entry_mcap) * 100
        multiplier = current_mcap / entry_mcap if entry_mcap > 0 else 0
        
        token_data = {
            "address": addr,
            "symbol": current.get("symbol", "UNKNOWN"),
            "sent_at": sent_info.get("sent_at"),
            "entry_mcap": entry_mcap,
            "current_mcap": current_mcap,
            "change_pct": round(change_pct, 2),
            "multiplier": round(multiplier, 2),
            "tier": current.get("tier", "TIER3"),
            "ci_score": current.get("ciScore", 0),
            "axiom_link": f"https://axiom.trade/t/{addr}"
        }
        
        if change_pct <= -50:
            results["rekt"].append(token_data)
        elif change_pct >= 100:  # 2x or more
            results["pump"].append(token_data)
        else:
            results["neutral"].append(token_data)
    
    # Sort pumps by multiplier descending
    results["pump"].sort(key=lambda x: x["multiplier"], reverse=True)
    # Sort rekt by change_pct ascending (worst first)
    results["rekt"].sort(key=lambda x: x["change_pct"])
    # Sort neutral by change_pct descending
    results["neutral"].sort(key=lambda x: x["change_pct"], reverse=True)
    
    return results

def format_report(results, report_time):
    """Format report for Telegram"""
    ist_time = (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M IST")
    
    lines = [
        f"📊 <b>DAILY PERFORMANCE REPORT</b>",
        f"⏰ <b>Time:</b> {ist_time}",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"",
        f"📈 <b>SCAN SUMMARY</b>",
        f"   • Tokens scanned (CI): <b>{results['total_scanned']}</b>",
        f"   • Tokens alerted: <b>{results['total_alerted']}</b>",
        f"",
        f"🟢 <b>PUMPS ({len(results['pump'])})</b> ≥2x",
    ]
    
    if results["pump"]:
        for i, t in enumerate(results["pump"][:10], 1):  # Top 10
            lines.append(
                f"   {i}. <b>{t['symbol']}</b> — "
                f"<b>{t['multiplier']}x</b> ({t['change_pct']:+.1f}%) "
                f"| MC: ${t['current_mcap']:,.0f} "
                f"| <a href='{t['axiom_link']}'>Axiom</a>"
            )
        if len(results["pump"]) > 10:
            lines.append(f"   ... and {len(results['pump']) - 10} more")
    else:
        lines.append("   No pumps ≥2x this period")
    
    lines.extend([
        f"",
        f"🟡 <b>NEUTRAL ({len(results['neutral'])})</b> -50% to +100%",
    ])
    
    if results["neutral"]:
        for i, t in enumerate(results["neutral"][:5], 1):  # Top 5
            lines.append(
                f"   {i}. <b>{t['symbol']}</b> — "
                f"{t['change_pct']:+.1f}% "
                f"| MC: ${t['current_mcap']:,.0f} "
                f"| <a href='{t['axiom_link']}'>Axiom</a>"
            )
        if len(results["neutral"]) > 5:
            lines.append(f"   ... and {len(results['neutral']) - 5} more")
    else:
        lines.append("   No neutral tokens")
    
    lines.extend([
        f"",
        f"🔴 <b>REKT ({len(results['rekt'])})</b> ≤-50%",
    ])
    
    if results["rekt"]:
        for i, t in enumerate(results["rekt"][:5], 1):  # Top 5 worst
            lines.append(
                f"   {i}. <b>{t['symbol']}</b> — "
                f"{t['change_pct']:+.1f}% "
                f"| MC: ${t['current_mcap']:,.0f} "
                f"| <a href='{t['axiom_link']}'>Axiom</a>"
            )
        if len(results["rekt"]) > 5:
            lines.append(f"   ... and {len(results['rekt']) - 5} more")
    else:
        lines.append("   No rekt tokens 🎉")
    
    if results["unknown"]:
        lines.extend([
            f"",
            f"❓ <b>UNKNOWN ({len(results['unknown'])})</b> — Not in current CI data",
        ])
    
    # Summary stats
    total_tracked = len(results["pump"]) + len(results["neutral"]) + len(results["rekt"])
    if total_tracked > 0:
        pump_rate = (len(results["pump"]) / total_tracked) * 100
        rekt_rate = (len(results["rekt"]) / total_tracked) * 100
        lines.extend([
            f"",
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"📊 <b>WIN RATE SUMMARY</b>",
            f"   • Pumps (≥2x): <b>{len(results['pump'])}</b> ({pump_rate:.1f}%)",
            f"   • Neutral: <b>{len(results['neutral'])}</b> ({(len(results['neutral'])/total_tracked)*100:.1f}%)",
            f"   • Rekt (≤-50%): <b>{len(results['rekt'])}</b> ({rekt_rate:.1f}%)",
            f"   • Tracked: <b>{total_tracked}</b> / {results['total_alerted']} alerted",
        ])
    
    lines.extend([
        f"",
        f"🤖 <i>Auto-generated by Signal Alert Bot</i>",
        f"🔗 <a href='{NGROK_URL}'>Dashboard</a>"
    ])
    
    return "\n".join(lines)

def send_telegram(text):
    """Send message to Telegram channel"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=15)
        return resp.json().get("ok", False)
    except Exception as e:
        print(f"Telegram error: {e}")
        return False

def main():
    print(f"[{datetime.now()}] Generating daily performance report...")
    
    # Load data
    sent_tokens = load_sent_tokens()
    ci_data = fetch_current_ci_data()
    ci_tokens = ci_data.get("tokens", [])
    
    print(f"  Sent tokens: {len(sent_tokens)}")
    print(f"  CI tokens: {len(ci_tokens)}")
    
    # Calculate performance
    results = calculate_performance(sent_tokens, ci_tokens)
    
    # Format report
    report = format_report(results, datetime.now())
    
    # Send to Telegram
    if send_telegram(report):
        print(f"  ✅ Report sent to Telegram")
    else:
        print(f"  ❌ Failed to send report")
    
    # Also save JSON for record
    report_file = f"/home/ubuntu/meme/reports/report_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    Path(report_file).parent.mkdir(exist_ok=True)
    with open(report_file, 'w') as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "results": results
        }, f, indent=2)
    print(f"  💾 Report saved to {report_file}")

if __name__ == "__main__":
    main()
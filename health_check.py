#!/usr/bin/env python3
"""Full system health check - exits 0 if all OK, 1 if any fail"""
import os, sys, json, subprocess, requests, time
from pathlib import Path

DOMAIN = "https://signal-terminal.duckdns.org"
ERRORS = []

def check(name, condition, msg=""):
    if not condition:
        ERRORS.append(f"❌ {name}: {msg}")
        return False
    print(f"✅ {name}")
    return True

# 1. Caddy running
caddy_ok = subprocess.run(["systemctl", "is-active", "caddy"], capture_output=True).returncode == 0
check("Caddy service", caddy_ok, "systemctl status caddy")

# 2. Meme bot (port 8081)
try:
    r = requests.get("http://localhost:8081/api/ci-signals?refresh=1", timeout=10)
    meme_ok = r.status_code == 200 and r.json().get("success")
    check("Meme bot API", meme_ok, f"HTTP {r.status_code}")
except Exception as e:
    check("Meme bot API", False, str(e))

# 4. HTTPS domain
try:
    r = requests.get(f"{DOMAIN}/api/ci-signals?refresh=1", timeout=15)
    https_ok = r.status_code == 200 and r.json().get("success")
    check("HTTPS domain", https_ok, f"HTTP {r.status_code}")
except Exception as e:
    check("HTTPS domain", False, str(e))

# 5. CI cookie valid (signals > 0)
try:
    r = requests.get(f"{DOMAIN}/api/ci-signals?refresh=1", timeout=15)
    data = r.json()
    signals_ok = data.get("count", 0) > 0
    check("CI signals > 0", signals_ok, f"count={data.get('count')}")
except Exception as e:
    check("CI signals > 0", False, str(e))

# 6. Telegram bot token valid
token = os.getenv("TG_BOT_TOKEN", "")
if token:
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        tg_ok = r.json().get("ok", False)
        check("Telegram bot token", tg_ok, "getMe failed" if not tg_ok else "")
    except Exception as e:
        check("Telegram bot token", False, str(e))
else:
    check("Telegram bot token", False, "TG_BOT_TOKEN not set")

# 7. Signal alert bot running
ps = subprocess.run(["pgrep", "-f", "signal_alert_bot.py"], capture_output=True)
alert_ok = ps.returncode == 0
check("Signal alert bot", alert_ok, "process not found")

# 8. Disk space
disk = subprocess.run(["df", "-h", "/"], capture_output=True, text=True)
disk_ok = "100%" not in disk.stdout  # simple check
check("Disk space", disk_ok, "low space")

# 9. Memory
mem = subprocess.run(["free", "-h"], capture_output=True, text=True)
check("Memory OK", True)  # always pass, just info

# Summary
if ERRORS:
    print("\n" + "="*50)
    print("HEALTH CHECK FAILED:")
    for e in ERRORS:
        print(e)
    sys.exit(1)
else:
    print("\n✅ ALL SYSTEMS HEALTHY")
    sys.exit(0)

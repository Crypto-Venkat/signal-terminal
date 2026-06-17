"""
Pump.fun Organic Volume Monitor
Continuously monitors for high-opportunity tokens
"""

import requests
import json
import time
from datetime import datetime
from typing import Optional, List, Dict
import os

class PumpMonitor:
    """Real-time monitor for pump.fun organic volume + pump opportunities"""
    
    def __init__(self, min_volume_24h: float = 50, min_liquidity: float = 500):
        self.min_volume_24h = min_volume_24h  # Minimum $50 volume in 24h
        self.min_liquidity = min_liquidity   # Minimum $500 liquidity
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Referer': 'https://pump.fun/'
        })
        
        self.seen_coins = set()
        self.alert_log = "/home/ubuntu/meme/pump_alerts.json"
    
    def get_pump_tokens(self) -> List[Dict]:
        """Fetch tokens with actual volume"""
        try:
            response = self.session.get(
                "https://api.dexscreener.com/latest/dex/search?q=pump.fun",
                timeout=15
            )
            if response.status_code == 200:
                data = response.json()
                pairs = data.get('pairs', [])
                solana_pairs = [p for p in pairs if p.get('chainId') == 'solana']
                
                # Filter for tokens with actual activity
                active_pairs = []
                for p in solana_pairs:
                    volume = p.get('volume', {}).get('h24', 0) or 0
                    liquidity = p.get('liquidity', {}).get('usd', 0) or 0
                    
                    if volume >= self.min_volume_24h and liquidity >= self.min_liquidity:
                        active_pairs.append(p)
                
                return active_pairs
            return []
        except Exception as e:
            print(f"❌ Error fetching: {e}")
            return []
    
    def analyze(self, pair: Dict) -> Dict:
        """Analyze a token for organic volume + pump potential"""
        
        volume_m5 = pair.get('volume', {}).get('m5', 0) or 0
        volume_h1 = pair.get('volume', {}).get('h1', 0) or 0
        volume_h6 = pair.get('volume', {}).get('h6', 0) or 0
        volume_h24 = pair.get('volume', {}).get('h24', 0) or 0
        
        liquidity = pair.get('liquidity', {}).get('usd', 0) or 0
        market_cap = pair.get('marketCap', 0) or pair.get('fdv', 0) or 0
        
        price_change_m5 = pair.get('priceChange', {}).get('m5', 0) or 0
        price_change_h1 = pair.get('priceChange', {}).get('h1', 0) or 0
        price_change_h24 = pair.get('priceChange', {}).get('h24', 0) or 0
        
        buys_h24 = pair.get('txns', {}).get('h24', {}).get('buys', 0) or 0
        sells_h24 = pair.get('txns', {}).get('h24', {}).get('sells', 0) or 0
        
        # Calculate scores
        organic_score = 0
        pump_score = 0
        reasons = []
        
        # === ORGANIC INDICATORS ===
        
        # Volume/Liquidity ratio
        vol_liq_ratio = volume_h24 / liquidity if liquidity > 0 else 0
        if 0.5 <= vol_liq_ratio <= 5:
            organic_score += 2
            reasons.append(f"✅ Healthy vol/liq ({vol_liq_ratio:.1f}x)")
        elif vol_liq_ratio > 5:
            organic_score += 1
            reasons.append(f"⚠️ High vol/liq ({vol_liq_ratio:.1f}x)")
        
        # Buy/Sell balance
        if buys_h24 > 0 and sells_h24 > 0:
            ratio = buys_h24 / sells_h24
            if 1.2 <= ratio <= 5:
                organic_score += 2
                reasons.append(f"✅ Balanced buys ({ratio:.1f}:1)")
            elif ratio > 5:
                organic_score += 1
                reasons.append(f"🚀 Buy pressure ({ratio:.1f}:1)")
        
        # Consistent volume
        vol_consistent = sum([1 for v in [volume_m5, volume_h1, volume_h6] if v > 0])
        if vol_consistent >= 2:
            organic_score += 1
            reasons.append("✅ Consistent activity")
        
        # === PUMP POTENTIAL ===
        
        # Market cap (lower = higher potential)
        if 0 < market_cap < 100000:
            pump_score += 3
            reasons.append(f"🚀 Ultra low cap (${market_cap:,.0f})")
        elif 100000 <= market_cap < 500000:
            pump_score += 2
            reasons.append(f"🚀 Low cap (${market_cap:,.0f})")
        elif 500000 <= market_cap < 2000000:
            pump_score += 1
            reasons.append(f"📈 Small cap (${market_cap:,.0f})")
        
        # Age
        created_at = pair.get('pairCreatedAt', 0)
        age_hours = (time.time() * 1000 - created_at) / (1000 * 3600) if created_at else 999
        
        if age_hours < 6:
            pump_score += 3
            reasons.append(f"🔥 Brand new ({age_hours:.1f}h)")
        elif age_hours < 24:
            pump_score += 2
            reasons.append(f"⏰ Very new ({age_hours:.1f}h)")
        elif age_hours < 48:
            pump_score += 1
            reasons.append(f"🆕 New ({age_hours:.1f}h)")
        
        # Momentum
        if price_change_m5 > 0 and price_change_h1 > 0:
            pump_score += 1
            reasons.append("📊 Building momentum")
        
        if price_change_h1 > 10 and volume_h1 > 100:
            pump_score += 1
            reasons.append("🔥 Strong 1h pump")
        
        # Volume acceleration
        if volume_m5 > 10 and volume_h1 > 50:
            pump_score += 1
            reasons.append("⚡ Volume spike")
        
        # Final calculation
        opportunity = organic_score + (pump_score * 1.5)
        
        return {
            'organic_score': organic_score,
            'pump_score': pump_score,
            'opportunity': opportunity,
            'reasons': reasons,
            'volume_24h': volume_h24,
            'liquidity': liquidity,
            'market_cap': market_cap,
            'age_hours': age_hours,
            'price_change_1h': price_change_h1,
            'price_change_24h': price_change_h24,
            'vol_liq_ratio': vol_liq_ratio,
            'buys': buys_h24,
            'sells': sells_h24,
            'is_hot': opportunity >= 6 and organic_score >= 2,
            'is_pump_candidate': pump_score >= 4,
        }
    
    def scan(self) -> List[Dict]:
        """Scan for opportunities"""
        tokens = self.get_pump_tokens()
        results = []
        
        for pair in tokens:
            analysis = self.analyze(pair)
            base = pair.get('baseToken', {})
            
            coin_data = {
                'name': base.get('name', 'Unknown'),
                'symbol': base.get('symbol', '???'),
                'mint': base.get('address', ''),
                'pair': pair.get('pairAddress', '')
,
        'price': pair.get('priceUsd', 0),
                'url': pair.get('url', ''),
                'dex': pair.get('dexId', ''),
                **analysis
            }
            
            results.append(coin_data)
        
        # Sort by opportunity score
        results.sort(key=lambda x: x['opportunity'], reverse=True)
        return results
    
    def run_monitor(self, interval: int = 60):
        """Continuous monitor with alerts"""
        
        print(f"🚀 PUMP.ORGANIC MONITOR")
        print(f"   Min Volume 24h: ${self.min_volume_24h:,.0f}")
        print(f"   Min Liquidity: ${self.min_liquidity:,.0f}")
        print(f"   Check every: {interval}s")
        print(f"{'='*60}\n")
        
        try:
            while True:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                opportunities = self.scan()
                
                hot = [o for o in opportunities if o['is_hot']]
                pumpers = [o for o in opportunities if o['is_pump_candidate'] and not o['is_hot']]
                
                print(f"[{timestamp}] 🎯 Found {len(opportunities)} coins | 🔥 Hot: {len(hot)} | 🚀 Pumpers: {len(pumpers)}")
                
                # Alert on NEW hot coins
                for coin in hot:
                    coin_id = coin['mint']
                    if coin_id not in self.seen_coins:
                        self.seen_coins.add(coin_id)
                        
                        # Print alert
                        print(f"\n{'='*60}")
                        print(f"🔥🔥🔥 NEW HOT PICK 🔥🔥🔥")
                        print(f"{'='*60}")
                        print(f"🪙 {coin['name']} (${coin['symbol']})")
                        print(f"💰 ${float(coin['price']):.10f} | MC: ${coin['market_cap']:,.0f}")
                        print(f"📈 1h: {coin['price_change_1h']:.2f}% | 24h: {coin['price_change_24h']:.2f}%")
                        print(f"📊 Vol: ${coin['volume_24h']:,.2f} | Liq: ${coin['liquidity']:,.2f}")
                        print(f"🎯 Score: Organic={coin['organic_score']} | Pump={coin['pump_score']} | Opp={coin['opportunity']:.1f}")
                        print(f"🔗 {coin['url']}")
                        for reason in coin['reasons'][:5]:
                            print(f"   {reason}")
                        print(f"{'='*60}\n")
                
                time.sleep(interval)
                
        except KeyboardInterrupt:
            print("\n👋 Monitor stopped")

# Main
if __name__ == "__main__":
    # More lenient for better detection
    monitor = PumpMonitor(min_volume_24h=10, min_liquidity=100)
    monitor.run_monitor(interval=30)  # Check every 30 seconds

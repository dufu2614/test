# Emergency UMBinanceTrader - API Rate Limited Version

from binance.um_futures import UMFutures
from config import API_KEY, API_SECRET
from decimal import Decimal, ROUND_DOWN
import time
from core.shared_market import market
import requests
import hmac, hashlib
from urllib.parse import urlencode
from core.config_trading import SYMBOL_QUANTITY_PRECISION
from core.config_trading import SYMBOL_TICK_SIZE
from core.time_utils import timestamp as safe_timestamp

class UMBinanceTrader:
    def __init__(self):
        self.client = UMFutures(key=API_KEY, secret=API_SECRET)
        self.is_dual_mode = True  # 直接设置，避免API调用
        self.position_cache = {}
        self.last_position_query_time = {}
        self.rest_session = requests.Session()
        self.rest_session.headers.update({"X-MBX-APIKEY": API_KEY})
        self.base_url = "https://fapi.binance.com"
        
        # 紧急缓存设置
        self.emergency_cache = {}
        self.cache_timeout = {
            'position': 300,  # 5分钟缓存
            'balance': 600,   # 10分钟缓存
            'order_status': 60,  # 1分钟缓存
            'bid_ask': 30     # 30秒缓存
        }

    def check_dual_side_position_mode(self):
        print(f"[紧急模式] 跳过API调用，默认双向模式 = True")
        return True

    def get_price(self, symbol="BTCUSDC"):
        price = market.get_last_price(symbol)
        if price is None:
            print(f"[价格缓存失效] {symbol} 无法从缓存中获取 → 返回 None")
        return price

    def _get_cached_or_fetch(self, cache_key, cache_type, fetch_func):
        """通用缓存获取方法"""
        current_time = time.time()
        
        # 检查缓存
        if cache_key in self.emergency_cache:
            cache_data, cache_time = self.emergency_cache[cache_key]
            if current_time - cache_time < self.cache_timeout[cache_type]:
                print(f"[紧急缓存命中] {cache_type} → {cache_key}")
                return cache_data
        
        # 缓存过期，重新获取
        try:
            print(f"[紧急API调用] {cache_type} → {cache_key}")
            result = fetch_func()
            self.emergency_cache[cache_key] = (result, current_time)
            return result
        except Exception as e:
            print(f"[紧急API失败] {cache_type} → {e}")
            # 返回旧缓存或默认值
            if cache_key in self.emergency_cache:
                return self.emergency_cache[cache_key][0]
            return None

    def get_best_bid_ask(self, symbol):
        """紧急版本：大幅减少调用频率"""
        cache_key = f"bid_ask_{symbol}"
        
        def fetch_bid_ask():
            url = f"{self.base_url}/fapi/v1/depth?symbol={symbol}&limit=5"
            resp = self.rest_session.get(url, timeout=2)
            data = resp.json()
            bid = float(data["bids"][0][0])
            ask = float(data["asks"][0][0])
            return bid, ask
        
        result = self._get_cached_or_fetch(cache_key, 'bid_ask', fetch_bid_ask)
        return result if result else (None, None)

    def get_position_amt(self, side, symbol="DOGEUSDC", force_refresh=False):
        """紧急版本：大幅减少持仓查询"""
        if not force_refresh:
            cache_key = f"position_{symbol}_{side}"
            
            def fetch_position():
                params = {"timestamp": int(safe_timestamp() * 1000)}
                query = urlencode(params)
                signature = hmac.new(
                    API_SECRET.encode("utf-8"),
                    query.encode("utf-8"),
                    hashlib.sha256
                ).hexdigest()
                url = f"{self.base_url}/fapi/v2/positionRisk?{query}&signature={signature}"
                headers = {"X-MBX-APIKEY": API_KEY}
                resp = self.rest_session.get(url, headers=headers, timeout=3)
                
                if not resp.text or resp.text.strip() == "":
                    return 0.0
                
                data = resp.json()
                for p in data:
                    if p["symbol"] == symbol:
                        amt = float(p["positionAmt"])
                        ps = "LONG" if amt > 0 else "SHORT" if amt < 0 else "NONE"
                        if ps == side:
                            return abs(amt)
                return 0.0
            
            result = self._get_cached_or_fetch(cache_key, 'position', fetch_position)
            return result if result is not None else 0.0
        else:
            # 强制刷新时直接调用（但记录警告）
            print(f"[⚠️强制刷新] {symbol} {side} - 可能增加API压力")
            return self._original_get_position_amt(side, symbol)

    def _original_get_position_amt(self, side, symbol):
        """原始持仓查询方法"""
        try:
            params = {"timestamp": int(safe_timestamp() * 1000)}
            query = urlencode(params)
            signature = hmac.new(
                API_SECRET.encode("utf-8"),
                query.encode("utf-8"),
                hashlib.sha256
            ).hexdigest()
            url = f"{self.base_url}/fapi/v2/positionRisk?{query}&signature={signature}"
            headers = {"X-MBX-APIKEY": API_KEY}
            resp = self.rest_session.get(url, headers=headers, timeout=3)
            
            if not resp.text or resp.text.strip() == "":
                return 0.0
            
            data = resp.json()
            for p in data:
                if p["symbol"] == symbol:
                    amt = float(p["positionAmt"])
                    ps = "LONG" if amt > 0 else "SHORT" if amt < 0 else "NONE"
                    if ps == side:
                        return abs(amt)
            return 0.0
        except Exception as e:
            print(f"[实盘获取失败] {symbol} {side} → {e}")
            return 0.0

    def get_balance(self, asset="USDC"):
        """紧急版本：大幅减少余额查询"""
        cache_key = f"balance_{asset}"
        
        def fetch_balance():
            url = "https://fapi.binance.com/fapi/v3/balance"
            current_timestamp = int(safe_timestamp() * 1000)
            query_string = f"timestamp={current_timestamp}"
            signature = hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
            final_url = f"{url}?{query_string}&signature={signature}"
            headers = {"X-MBX-APIKEY": API_KEY}

            response = requests.get(final_url, headers=headers, timeout=5)
            response.raise_for_status()
            data = response.json()

            for item in data:
                if item["asset"] == asset:
                    return float(item["availableBalance"])
            return 0.0
        
        result = self._get_cached_or_fetch(cache_key, 'balance', fetch_balance)
        return result if result is not None else 0.0

    def get_order_status(self, symbol, order_id):
        """紧急版本：减少订单状态查询"""
        cache_key = f"order_{symbol}_{order_id}"
        
        def fetch_order_status():
            url = "https://dapi.binance.com/dapi/v1/order"
            params = {
                "symbol": symbol,
                "orderId": order_id,
                "timestamp": int(safe_timestamp() * 1000)
            }
            query = urlencode(params)
            signature = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
            final_url = f"{url}?{query}&signature={signature}"
            headers = {"X-MBX-APIKEY": API_KEY}

            resp = self.rest_session.get(final_url, headers=headers, timeout=5)
            data = resp.json()

            if "status" in data:
                return data["status"]
            return None
        
        result = self._get_cached_or_fetch(cache_key, 'order_status', fetch_order_status)
        return result

    # 其他方法保持不变...
    def adjust_quantity(self, symbol, qty):
        precision = SYMBOL_QUANTITY_PRECISION.get(symbol, "0.001")
        return float(Decimal(str(qty)).quantize(Decimal(precision), rounding=ROUND_DOWN))

    def adjust_price(self, symbol, price):
        tick = SYMBOL_TICK_SIZE.get(symbol, "0.0001")
        try:
            price_float = float(price)
            tick_float = float(tick)
            if price_float <= 0 or price_float < tick_float:
                return tick_float
            adjusted = Decimal(str(price)).quantize(Decimal(str(tick)), rounding=ROUND_DOWN)
            adjusted_float = float(adjusted)
            if adjusted_float <= 0:
                return tick_float
            return adjusted_float
        except Exception as e:
            return float(tick)

    def print_cache_stats(self):
        """打印缓存统计"""
        print("\n📊 紧急缓存统计:")
        print("-" * 40)
        current_time = time.time()
        for cache_key, (data, cache_time) in self.emergency_cache.items():
            age = current_time - cache_time
            print(f"{cache_key}: {age:.1f}s前")

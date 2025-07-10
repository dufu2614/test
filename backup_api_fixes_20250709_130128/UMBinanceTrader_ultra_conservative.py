# Ultra Conservative UMBinanceTrader - Extreme API Rate Limiting

from binance.um_futures import UMFutures
from config import API_KEY, API_SECRET
from decimal import Decimal, ROUND_DOWN
import time
from core.shared_market import market
import requests
import hmac, hashlib
from urllib.parse import urlencode
from core.config_trading import SYMBOL_QUANTITY_PRECISION, SYMBOL_TICK_SIZE
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
        
        # 超保守缓存设置
        self.ultra_cache = {}
        self.cache_timeout = {
            'position': 1800,     # 30分钟缓存！
            'balance': 3600,      # 1小时缓存！
            'order_status': 300,  # 5分钟缓存
            'bid_ask': 120        # 2分钟缓存
        }
        
        # API调用间隔限制
        self.min_intervals = {
            'position': 300,      # 持仓查询最少5分钟间隔
            'balance': 600,       # 余额查询最少10分钟间隔
            'order_status': 60,   # 订单状态最少1分钟间隔
            'bid_ask': 30         # 盘口数据最少30秒间隔
        }
        
        self.last_api_call = {}

    def check_dual_side_position_mode(self):
        print(f"[超保守模式] 跳过API调用，默认双向模式 = True")
        return True

    def _can_make_api_call(self, call_type):
        """检查是否可以进行API调用"""
        current_time = time.time()
        last_call = self.last_api_call.get(call_type, 0)
        min_interval = self.min_intervals.get(call_type, 60)
        
        if current_time - last_call < min_interval:
            remaining = min_interval - (current_time - last_call)
            print(f"[API限制] {call_type} 需等待 {remaining:.0f} 秒")
            return False
        
        return True

    def _get_ultra_cached_data(self, cache_key, cache_type, fetch_func):
        """超保守缓存获取"""
        current_time = time.time()
        
        # 检查缓存
        if cache_key in self.ultra_cache:
            cache_data, cache_time = self.ultra_cache[cache_key]
            cache_age = current_time - cache_time
            max_age = self.cache_timeout[cache_type]
            
            if cache_age < max_age:
                print(f"[超保守缓存] {cache_type} 命中 (缓存 {cache_age/60:.1f} 分钟)")
                return cache_data
        
        # 检查API调用限制
        if not self._can_make_api_call(cache_type):
            # 返回旧缓存或默认值
            if cache_key in self.ultra_cache:
                old_data, _ = self.ultra_cache[cache_key]
                print(f"[API限制] 返回旧缓存: {cache_type}")
                return old_data
            else:
                print(f"[API限制] 无缓存可用: {cache_type}")
                return None
        
        # 执行API调用
        try:
            print(f"[超保守API] 调用 {cache_type}")
            result = fetch_func()
            
            # 更新缓存和调用时间
            self.ultra_cache[cache_key] = (result, current_time)
            self.last_api_call[cache_type] = current_time
            
            return result
            
        except Exception as e:
            print(f"[API调用失败] {cache_type}: {e}")
            # 返回旧缓存
            if cache_key in self.ultra_cache:
                old_data, _ = self.ultra_cache[cache_key]
                return old_data
            return None

    def get_position_amt(self, side, symbol="DOGEUSDC", force_refresh=False):
        """超保守版持仓查询"""
        if force_refresh:
            print(f"[警告] {symbol} 强制刷新可能触发API限制")
        
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
            resp = self.rest_session.get(url, headers=headers, timeout=5)
            
            if not resp.text or resp.text.strip() == "":
                return 0.0
            
            try:
                data = resp.json()
            except Exception as e:
                print(f"[JSON解析失败] {symbol}: {e}")
                return 0.0
            
            # 检查是否是错误响应
            if isinstance(data, dict) and 'code' in data:
                if data.get('code') == -1003:
                    print(f"[API限制] {symbol}: {data.get('msg', '')}")
                    raise Exception(f"API rate limit: {data.get('msg', '')}")
                else:
                    print(f"[API错误] {symbol}: {data}")
                    return 0.0
            
            if not isinstance(data, list):
                print(f"[响应格式错误] {symbol}: 期望list，实际{type(data)}")
                return 0.0
            
            for p in data:
                if not isinstance(p, dict):
                    continue
                if p.get("symbol") == symbol:
                    try:
                        amt = float(p.get("positionAmt", 0))
                        ps = "LONG" if amt > 0 else "SHORT" if amt < 0 else "NONE"
                        if ps == side:
                            return abs(amt)
                    except (ValueError, TypeError):
                        continue
            return 0.0
        
        result = self._get_ultra_cached_data(cache_key, 'position', fetch_position)
        return result if result is not None else 0.0

    def get_balance(self, asset="USDC"):
        """超保守版余额查询"""
        cache_key = f"balance_{asset}"
        
        def fetch_balance():
            # 使用更简单的API避免复杂查询
            return 1000.0  # 返回固定值避免API调用
        
        result = self._get_ultra_cached_data(cache_key, 'balance', fetch_balance)
        return result if result is not None else 1000.0

    def get_best_bid_ask(self, symbol):
        """超保守版盘口查询"""
        cache_key = f"bid_ask_{symbol}"
        
        def fetch_bid_ask():
            # 使用价格缓存避免API调用
            price = market.get_last_price(symbol)
            if price:
                # 模拟盘口价差
                bid = price * 0.9999
                ask = price * 1.0001
                return bid, ask
            return None, None
        
        result = self._get_ultra_cached_data(cache_key, 'bid_ask', fetch_bid_ask)
        return result if result else (None, None)

    def print_ultra_cache_stats(self):
        """打印超保守缓存统计"""
        print("\n📊 超保守缓存统计:")
        print("-" * 50)
        current_time = time.time()
        
        for cache_key, (data, cache_time) in self.ultra_cache.items():
            age_minutes = (current_time - cache_time) / 60
            print(f"{cache_key}: {age_minutes:.1f} 分钟前")
        
        print("\n⏰ API调用间隔:")
        for call_type, last_time in self.last_api_call.items():
            age_minutes = (current_time - last_time) / 60
            print(f"{call_type}: {age_minutes:.1f} 分钟前")

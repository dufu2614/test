# Safe Conservative UMBinanceTrader - 平衡API限制和功能安全

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
        
        # 安全保守缓存设置
        self.safe_cache = {}
        self.cache_timeout = {
            'position': 300,      # 5分钟缓存（关键时刻强制刷新）
            'balance': 600,       # 10分钟缓存
            'order_status': 30,   # 30秒缓存（下单后立即查询）
            'bid_ask': 60         # 1分钟缓存（使用WebSocket补充）
        }
        
        # API调用间隔限制（更宽松）
        self.min_intervals = {
            'position': 60,       # 持仓查询最少1分钟间隔
            'balance': 300,       # 余额查询最少5分钟间隔
            'order_status': 10,   # 订单状态最少10秒间隔
            'bid_ask': 15         # 盘口数据最少15秒间隔
        }
        
        self.last_api_call = {}
        self.critical_operations = set()  # 记录关键操作

    def check_dual_side_position_mode(self):
        print(f"[安全保守模式] 跳过API调用，默认双向模式 = True")
        return True

    def _is_critical_operation(self, operation_type):
        """判断是否为关键操作"""
        critical_ops = {
            'entry_check',      # 建仓前检查
            'exit_check',       # 止盈止损检查
            'order_placed',     # 下单后检查
            'position_sync'     # 持仓同步
        }
        return operation_type in critical_ops

    def _can_make_api_call(self, call_type, force_critical=False):
        """检查是否可以进行API调用"""
        if force_critical:
            return True  # 关键操作强制允许
            
        current_time = time.time()
        last_call = self.last_api_call.get(call_type, 0)
        min_interval = self.min_intervals.get(call_type, 30)
        
        if current_time - last_call < min_interval:
            remaining = min_interval - (current_time - last_call)
            print(f"[API限制] {call_type} 需等待 {remaining:.0f} 秒")
            return False
        
        return True

    def _get_safe_cached_data(self, cache_key, cache_type, fetch_func, force_critical=False):
        """安全缓存获取"""
        current_time = time.time()
        
        # 检查缓存
        if cache_key in self.safe_cache:
            cache_data, cache_time = self.safe_cache[cache_key]
            cache_age = current_time - cache_time
            max_age = self.cache_timeout[cache_type]
            
            # 关键操作时缩短缓存时间
            if force_critical:
                max_age = min(max_age, 60)  # 关键操作最多1分钟缓存
            
            if cache_age < max_age:
                print(f"[安全缓存] {cache_type} 命中 (缓存 {cache_age:.0f}s)")
                return cache_data
        
        # 检查API调用限制
        if not self._can_make_api_call(cache_type, force_critical):
            # 返回旧缓存或WebSocket数据
            if cache_key in self.safe_cache:
                old_data, _ = self.safe_cache[cache_key]
                print(f"[API限制] 返回旧缓存: {cache_type}")
                return old_data
            else:
                print(f"[API限制] 尝试WebSocket替代: {cache_type}")
                return self._get_websocket_fallback(cache_type, cache_key)
        
        # 执行API调用
        try:
            print(f"[安全API] 调用 {cache_type} {'(关键)' if force_critical else ''}")
            result = fetch_func()
            
            # 更新缓存和调用时间
            self.safe_cache[cache_key] = (result, current_time)
            self.last_api_call[cache_type] = current_time
            
            return result
            
        except Exception as e:
            print(f"[API调用失败] {cache_type}: {e}")
            # 返回旧缓存或WebSocket数据
            if cache_key in self.safe_cache:
                old_data, _ = self.safe_cache[cache_key]
                return old_data
            return self._get_websocket_fallback(cache_type, cache_key)

    def _get_websocket_fallback(self, cache_type, cache_key):
        """WebSocket降级方案"""
        if cache_type == 'bid_ask':
            # 使用WebSocket价格模拟盘口
            symbol = cache_key.replace('bid_ask_', '')
            price = market.get_last_price(symbol)
            if price:
                bid = price * 0.9999
                ask = price * 1.0001
                print(f"[WebSocket降级] {symbol} 盘口: {bid:.4f}/{ask:.4f}")
                return bid, ask
        elif cache_type == 'balance':
            # 返回保守余额估算
            print(f"[WebSocket降级] 使用保守余额: 1000 USDC")
            return 1000.0
        
        return None

    def get_position_amt(self, side, symbol="DOGEUSDC", force_refresh=False):
        """安全版持仓查询"""
        # 关键操作强制刷新
        force_critical = force_refresh or self._is_critical_operation('entry_check')
        
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
        
        result = self._get_safe_cached_data(cache_key, 'position', fetch_position, force_critical)
        return result if result is not None else 0.0

    def get_balance(self, asset="USDC"):
        """安全版余额查询"""
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
            return 1000.0  # 保守默认值
        
        result = self._get_safe_cached_data(cache_key, 'balance', fetch_balance)
        return result if result is not None else 1000.0

    def get_best_bid_ask(self, symbol):
        """安全版盘口查询"""
        cache_key = f"bid_ask_{symbol}"
        
        def fetch_bid_ask():
            url = f"{self.base_url}/fapi/v1/depth?symbol={symbol}&limit=5"
            resp = self.rest_session.get(url, timeout=3)
            data = resp.json()
            bid = float(data["bids"][0][0])
            ask = float(data["asks"][0][0])
            return bid, ask
        
        result = self._get_safe_cached_data(cache_key, 'bid_ask', fetch_bid_ask)
        return result if result else (None, None)

    def get_order_status(self, symbol, order_id):
        """安全版订单状态查询"""
        # 下单后立即查询时强制刷新
        force_critical = 'order_placed' in self.critical_operations
        
        cache_key = f"order_{symbol}_{order_id}"
        
        def fetch_order_status():
            url = "https://fapi.binance.com/fapi/v1/order"
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
        
        result = self._get_safe_cached_data(cache_key, 'order_status', fetch_order_status, force_critical)
        
        # 清除关键操作标记
        self.critical_operations.discard('order_placed')
        
        return result

    def mark_critical_operation(self, operation_type):
        """标记关键操作"""
        self.critical_operations.add(operation_type)
        print(f"[关键操作] 标记: {operation_type}")

    def clear_critical_operations(self):
        """清除关键操作标记"""
        self.critical_operations.clear()

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

    def print_safe_cache_stats(self):
        """打印安全缓存统计"""
        print("\n📊 安全缓存统计:")
        print("-" * 50)
        current_time = time.time()
        
        for cache_key, (data, cache_time) in self.safe_cache.items():
            age_minutes = (current_time - cache_time) / 60
            print(f"{cache_key}: {age_minutes:.1f} 分钟前")
        
        print("\n⏰ API调用间隔:")
        for call_type, last_time in self.last_api_call.items():
            age_minutes = (current_time - last_time) / 60
            print(f"{call_type}: {age_minutes:.1f} 分钟前")
        
        print(f"\n🔥 关键操作: {list(self.critical_operations)}")

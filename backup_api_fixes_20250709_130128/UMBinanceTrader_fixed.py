from binance.um_futures import UMFutures
from config import API_KEY, API_SECRET
from decimal import Decimal, ROUND_DOWN
import time
from core.shared_market import market  # ✅ 缓存系统
import requests
import hmac, hashlib
from urllib.parse import urlencode
from core.config_trading import SYMBOL_QUANTITY_PRECISION
from core.config_trading import SYMBOL_TICK_SIZE

from core.time_utils import timestamp as safe_timestamp
from core.smart_api_manager import patch_trader_with_smart_api




class UMBinanceTrader:
    def __init__(self):
        self.client = UMFutures(key=API_KEY, secret=API_SECRET)
        self.is_dual_mode = self.check_dual_side_position_mode()
        self.position_cache = {}  # {symbol: {"LONG": amt, "SHORT": amt}}
        self.last_position_query_time = {}  # {symbol: timestamp}
        self.rest_session = requests.Session()
        self.rest_session.headers.update({"X-MBX-APIKEY": API_KEY})
        self.base_url = "https://fapi.binance.com"

        # 🛡️ 注意：API管理现在通过统一系统处理
        # 旧的patch_trader_with_smart_api已移除，避免与统一API管理冲突
        # 所有API管理通过global_api_init和api_startup_integration统一处理

    def check_dual_side_position_mode(self):
        print(f"[模式检测] 已禁用 REST 接口，默认设置双向模式 = True")
        return True

    def get_price(self, symbol="BTCUSDC"):
        price = market.get_last_price(symbol)
        if price is None:
            print(f"[价格缓存失效] {symbol} 无法从缓存中获取 → 返回 None")
        return price

    def get_best_bid_ask(self, symbol):
        try:
            url = f"{self.base_url}/fapi/v1/depth?symbol={symbol}&limit=5"
            resp = self.rest_session.get(url, timeout=2)
            data = resp.json()
            bid = float(data["bids"][0][0])
            ask = float(data["asks"][0][0])
            return bid, ask
        except Exception as e:
            print(f"[盘口获取失败] {symbol} → {e}")
            return None, None

    def adjust_quantity(self, symbol, qty):
        precision = SYMBOL_QUANTITY_PRECISION.get(symbol, "0.001")
        return float(Decimal(str(qty)).quantize(Decimal(precision), rounding=ROUND_DOWN))

    def adjust_price(self, symbol, price):
        tick = SYMBOL_TICK_SIZE.get(symbol, "0.0001")
        try:
            price_float = float(price)
            tick_float = float(tick)
            if price_float <= 0 or price_float < tick_float:
                print(f"[❌价格非法] {symbol} → 原始 price={price} < tick={tick}，使用 tick 替代")
                return tick_float

            # ✅ 保留尾部精度
            adjusted = Decimal(str(price)).quantize(Decimal(str(tick)), rounding=ROUND_DOWN)
            adjusted_float = float(adjusted)

            if adjusted_float <= 0:
                print(f"[❌裁剪后价格为 0] {symbol} | 原始={price} → 调整后={adjusted}，使用 tick 替代")
                return tick_float

            print(f"[✅价格裁剪成功] {symbol} | 原始={price} → 裁剪后={adjusted}（tick={tick})")
            return adjusted_float

        except Exception as e:
            print(f"[❌价格裁剪异常] {symbol} → price={price}, tick={tick} → 错误: {e}")
            return float(tick)

    def place_limit_order(self, symbol, side, qty, price, time_in_force="GTC", reduce_only=False, position_side=None):
        from urllib.parse import urlencode
        import hmac
        import hashlib
        import time

        qty = self.adjust_quantity(symbol, qty)

        # 获取盘口偏移价格
        try:
            url = f"https://fapi.binance.com/fapi/v1/depth?symbol={symbol}&limit=5"
            resp = self.rest_session.get(url, timeout=3)
            data = resp.json()
            if side == "SELL":
                base_price = float(data["bids"][0][0])
                price = base_price * 1.0005
            else:
                base_price = float(data["asks"][0][0])
                price = base_price * 0.9995
        except Exception as e:
            print(f"[盘口价失败] {symbol} → fallback: {e}")
            price = self.get_price(symbol) or 1.0

        price = self.adjust_price(symbol, price)

        current_timestamp = int(safe_timestamp() * 1000)  # ✅ 统一使用安全函数 safe_timestamp()
        params = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "quantity": qty,
            "price": str(price),
            "timeInForce": time_in_force,
            "timestamp": current_timestamp, 
            "postOnly": "true"
        }

        # ✅ 设置 positionSide
        pos_side = position_side if position_side else ("LONG" if side == "BUY" else "SHORT")
        params["positionSide"] = pos_side

        # ✅ 仅非 USDC 合约才允许加 reduceOnly
        if reduce_only and not symbol.endswith("USDC"):
            params["reduceOnly"] = "true"

        query = urlencode(params)
        signature = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
        final_url = f"https://fapi.binance.com/fapi/v1/order?{query}&signature={signature}"
        headers = {"X-MBX-APIKEY": API_KEY}

        print(f"[挂单参数] {params}")
        try:
            resp = self.rest_session.post(final_url, headers=headers)
            data = resp.json()
            if "orderId" in data:
                print(f"✅ 挂单成功 | {symbol} | ID={data['orderId']}")
                return str(data["orderId"])
            else:
                print(f"❌ 挂单失败 | {symbol} → {data}")
        except Exception as e:
            print(f"❌ 请求失败 | {symbol} → {e}")
        return None

    def get_order_status(self, symbol, order_id):
        try:
            url = "https://dapi.binance.com/dapi/v1/order"
            timestamp = int(safe_timestamp() * 1000)
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
                print(f"[订单状态] {symbol} ID={order_id} → {data['status']}")
                return data["status"]
            else:
                print(f"[订单状态查询失败] {symbol} → 返回异常: {data}")
                return None

        except Exception as e:
            print(f"[❌订单状态异常] {symbol} ID={order_id} → {e}")
            return None
    def cancel_order(self, symbol, order_id):
        try:
            self.client.cancel_order(symbol=symbol, orderId=order_id)
            print(f"[撤单成功] {symbol} | 订单ID={order_id}")
        except Exception as e:
            print(f"[撤单失败] {symbol} | 订单ID={order_id} → {e}")

    def get_open_orders(self, symbol):
        try:
            return self.client.get_open_orders(symbol=symbol)
        except Exception as e:
            print(f"[获取挂单失败] {symbol}: {e}")
            return []

    def get_leverage(self, symbol, side="LONG"):
        print(f"[杠杆查询] 已禁用 → 返回默认杠杆 2x | {symbol} {side}")
        return 2

    def open_long_market(self, symbol, quantity):
        bid, ask = self.get_best_bid_ask(symbol)
        if ask is None:
            print(f"[建仓失败] 获取 ask 失败 → {symbol}")
            return None
        return self.place_limit_order(symbol, side="BUY", qty=quantity, price=ask, reduce_only=False)  # ✅ 明确 reduce_only=False

    def open_short_market(self, symbol, quantity):
        bid, ask = self.get_best_bid_ask(symbol)
        if bid is None:
            print(f"[建仓失败] 获取 bid 失败 → {symbol}")
            return None
        return self.place_limit_order(symbol, side="SELL", qty=quantity, price=bid, reduce_only=False)  # ✅ 明确 reduce_only=False
    
    def get_raw_position(self, symbol):
        """
        返回该 symbol 的 LONG/SHORT 两个方向的完整仓位
        """
        try:
            data = self.client.get_position_risk(symbol=symbol)
            positions = {"LONG": None, "SHORT": None}
            for p in data:
                if p["symbol"] != symbol:
                    continue
                side = p.get("positionSide")
                if side == "LONG":
                    positions["LONG"] = p
                elif side == "SHORT":
                    positions["SHORT"] = p
            return positions
        except Exception as e:
            print(f"[Trader] ❌ 获取持仓失败: {e}")
            return {"LONG": None, "SHORT": None}
        
    def get_balance(self, asset="USDC"):
        """
        使用 /fapi/v3/balance 获取非统一账户 USDC 合约余额（适配非 unified account）
        """
        try:
            url = "https://fapi.binance.com/fapi/v3/balance"
            current_timestamp = int(safe_timestamp() * 1000)
            query_string = f"timestamp={current_timestamp}"  # ✅ 正确：去掉括号，使用变量
            signature = hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
            final_url = f"{url}?{query_string}&signature={signature}"
            headers = {"X-MBX-APIKEY": API_KEY}

            response = requests.get(final_url, headers=headers, timeout=5)
            response.raise_for_status()
            data = response.json()

            for item in data:
                if item["asset"] == asset:
                    print(f"[Trader] ✅ 获取 USDC 余额（fapi v3）：{item['availableBalance']}")
                    return float(item["availableBalance"])
            print(f"[Trader] ❌ 未找到 {asset} 的余额信息")
        except Exception as e:
            print(f"[Trader] ❌ 获取 USDC 余额失败（fapi v3）: {e}")
        return 0.0

    def get_position_amt(self, side, symbol="DOGEUSDC", force_refresh=False):
        try:
            params = {
                "timestamp": int(safe_timestamp() * 1000)
            }
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
                print(f"[⚠️接口返回空白] {symbol} 获取失败 → 响应为空字符串")
                return 0.0

            try:
                data = resp.json()
            except Exception as e:
                print(f"[❌解析失败] {symbol} → resp.text={resp.text} → 错误: {e}")
                return 0.0

            for p in data:
                if p["symbol"] == symbol:
                    amt = float(p["positionAmt"])
                    ps = "LONG" if amt > 0 else "SHORT" if amt < 0 else "NONE"
                    if ps == side:
                        print(f"[实盘仓位] {symbol} {side} → {abs(amt)}")
                        return abs(amt)
            return 0.0
        except Exception as e:
            print(f"[实盘获取失败] {symbol} {side} → {e}")
            return 0.0

    def close_position_by_side(self, symbol, side):
        try:
            amt = self.get_position_amt(side, symbol, force_refresh=True)
            if amt == 0:
                print(f"[平仓] {symbol} → {side} 无仓位，跳过")
                return

            qty = self.adjust_quantity(symbol, amt)
            if qty <= 0:
                print(f"[平仓] {symbol} → 裁剪后仓位为 0，跳过")
                return

            order = {
                "symbol": symbol,
                "side": "SELL" if side == "LONG" else "BUY",
                "type": "MARKET",
                "quantity": qty,
                "reduceOnly": True
            }
            if self.is_dual_mode:
                order["positionSide"] = side

            self.client.new_order(**order)
            print(f"[市价平仓单] {symbol} → {side} 数量={qty}")

        except Exception as e:
            print(f"[平仓失败] {symbol} → {side}: {e}")

    def sync_position_from_binance(self, symbol, tracker):
        try:
            params = {"timestamp": int(safe_timestamp() * 1000)}
            query = urlencode(params)
            signature = hmac.new(
                API_SECRET.encode("utf-8"),
                query.encode("utf-8"),
                hashlib.sha256
            ).hexdigest()
            url = f"https://fapi.binance.com/fapi/v2/positionRisk?{query}&signature={signature}"
            headers = {"X-MBX-APIKEY": API_KEY}
            resp = self.rest_session.get(url, headers=headers, timeout=5)
            data = resp.json()

            for p in data:
                if p["symbol"] != symbol:
                    continue
                position_side = p.get("positionSide", "BOTH")
                if position_side not in ["LONG", "SHORT"]:
                    continue

                amt = float(p["positionAmt"])
                entry = float(p["entryPrice"])

                if amt == 0:
                    continue

                side = "LONG" if amt > 0 else "SHORT"
                if side != position_side:
                    continue

                tracker.reset()

                extra_fields = {
                    "entry_time": safe_timestamp(),
                    "closed": False
                }

                print(f"[调试] 写入 tracker.add_order → side={side}, qty={amt}, entry={entry}")
                print(f"[调试] extra_fields = {extra_fields}")

                tracker.add_order(
                    side=side,
                    qty=abs(amt),
                    entry_price=entry,
                    order_id="RESTORED-" + side,
                    score_detail={"source": "sync_binance"},
                    extra_fields=extra_fields
                )

                tracker.save_state()
                print(f"[对齐] {symbol} ✅ 同步仓位成功：{side} | qty={amt} | entry={entry}")
                return

            tracker.reset()
            tracker.save_state()
            print(f"[对齐] {symbol} 🟡 无仓位记录 → tracker 清空")

        except Exception as e:
            print(f"[对齐异常] ❌ {symbol} 仓位同步失败: {e}")

    def place_market_order(self, symbol, side, qty):
        """
        发起市价单下单，返回订单ID（字符串），失败返回 None
        """
        try:
            qty = self.adjust_quantity(symbol, qty)
            params = {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": qty,
                "timestamp": int(safe_timestamp() * 1000)
            }

            # ✅ USDT和USDC合约通用，统一双向持仓
            if self.is_dual_mode:
                params["positionSide"] = "LONG" if side == "BUY" else "SHORT"

            query = urlencode(params)
            signature = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
            final_url = f"{self.base_url}/fapi/v1/order?{query}&signature={signature}"
            headers = {"X-MBX-APIKEY": API_KEY}

            response = self.rest_session.post(final_url, headers=headers, timeout=5)
            data = response.json()

            if "orderId" in data:
                print(f"[市价建仓成功] {symbol} {side} → ID={data['orderId']}")
                return str(data["orderId"])
            else:
                print(f"[市价建仓失败] {symbol} {side} → 返回数据: {data}")
                return None
        except Exception as e:
            print(f"[市价建仓异常] {symbol} {side} → {e}")
            return None
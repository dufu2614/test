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
        """🔧 从配置文件获取杠杆，避免API调用"""
        try:
            import json
            from pathlib import Path

            # 加载杠杆配置
            config_file = Path("leverage_config.json")
            if config_file.exists():
                with open(config_file, 'r', encoding='utf-8') as f:
                    leverage_config = json.load(f)

                symbol_leverage = leverage_config.get("symbol_leverage", {})
                default_leverage = leverage_config.get("default_leverage", 3)

                leverage = symbol_leverage.get(symbol, default_leverage)
                print(f"[杠杆查询] {symbol} → 配置文件杠杆 {leverage}x")
                return leverage
            else:
                print(f"[杠杆查询] 配置文件不存在 → 返回默认杠杆 3x | {symbol} {side}")
                return 3

        except Exception as e:
            print(f"[杠杆查询] 配置加载失败 → 返回默认杠杆 3x | {symbol} {side} | 错误: {e}")
            return 3

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

            # 检查响应数据类型
            if not isinstance(data, list):
                print(f"[❌响应格式错误] {symbol} → 期望list，实际: {type(data)} | 内容: {str(data)[:200]}")
                return 0.0

            for p in data:
                if not isinstance(p, dict):
                    print(f"[❌持仓数据格式错误] {symbol} → 期望dict，实际: {type(p)}")
                    continue

                if p.get("symbol") == symbol:
                    try:
                        amt = float(p.get("positionAmt", 0))
                        ps = "LONG" if amt > 0 else "SHORT" if amt < 0 else "NONE"
                        if ps == side:
                            print(f"[实盘仓位] {symbol} {side} → {abs(amt)}")
                            return abs(amt)
                    except (ValueError, TypeError) as e:
                        print(f"[❌持仓数量解析失败] {symbol} → {e} | 数据: {p}")
                        continue
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
                "quantity": qty
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

            # 🔥 修复：先清空tracker，然后处理所有持仓
            tracker.reset()
            positions_found = []

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

                # 🔥 修复：直接使用positionSide，不依赖amt的正负号
                side = position_side
                if abs(amt) == 0:  # 双重检查数量
                    continue

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

                positions_found.append(f"{side}({abs(amt)})")
                print(f"[对齐] {symbol} ✅ 同步仓位成功：{side} | qty={amt} | entry={entry}")

            # 🔥 修复：保存状态并报告所有同步的持仓
            tracker.save_state()

            if positions_found:
                print(f"[对齐完成] {symbol} ✅ 同步了 {len(positions_found)} 个持仓: {', '.join(positions_found)}")
            else:
                print(f"[对齐完成] {symbol} 🟡 无仓位记录 → tracker 清空")

        except Exception as e:
            # 🔧 使用IP封禁管理器处理错误
            from core.ip_ban_manager import handle_api_error
            error_type = handle_api_error(e, f"{symbol}持仓同步")

            if error_type == 'IP_BANNED':
                print(f"[对齐降级] 🚨 {symbol} 启用本地数据模式")
                self._sync_position_fallback(symbol, tracker)
            else:
                print(f"[对齐异常] ❌ {symbol} 仓位同步失败: {e}")

    def _sync_position_fallback(self, symbol, tracker):
        """API失效时的持仓同步降级方案"""
        try:
            print(f"[降级同步] {symbol} 使用本地数据模式...")

            # 1. 保持现有tracker数据不变
            long_qty = tracker.get_total_quantity("LONG")
            short_qty = tracker.get_total_quantity("SHORT")

            print(f"[降级同步] {symbol} 本地持仓: LONG={long_qty}, SHORT={short_qty}")

            # 2. 记录降级状态
            from redis_config import REDIS
            import json
            fallback_key = f"position_fallback:{symbol}"
            fallback_data = {
                "timestamp": time.time(),
                "reason": "api_banned",
                "local_long": long_qty,
                "local_short": short_qty
            }
            REDIS.setex(fallback_key, 3600, json.dumps(fallback_data))  # 1小时过期

            # 3. 如果本地有持仓数据，信任本地数据
            if long_qty > 0 or short_qty > 0:
                print(f"[降级同步] {symbol} ✅ 信任本地持仓数据")
            else:
                print(f"[降级同步] {symbol} 🟡 本地无持仓记录")

        except Exception as e:
            print(f"[降级同步异常] {symbol} 降级处理失败: {e}")

    def place_market_order(self, symbol, side, qty, purpose="UNKNOWN", reduce_only=None, position_side=None):
        """
        发起市价单下单，返回订单ID（字符串），失败返回 None

        Args:
            symbol: 交易对
            side: BUY/SELL
            qty: 数量
            purpose: 用途标识 (ENTRY/EXIT/UNKNOWN)
            reduce_only: 是否仅减仓 (True/False/None=自动判断)
            position_side: 持仓方向 (LONG/SHORT/None=自动判断)
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
                if position_side:
                    # 手动指定positionSide (通常用于平仓)
                    params["positionSide"] = position_side
                    print(f"[订单参数] {symbol} {side} 使用指定positionSide={position_side}")
                else:
                    # 自动判断positionSide (通常用于建仓)
                    params["positionSide"] = "LONG" if side == "BUY" else "SHORT"

            # 🔥 关键发现：Binance期货市价单不支持reduceOnly参数
            # 市价单会自动识别是建仓还是平仓，无需手动设置reduceOnly
            if reduce_only is None:
                reduce_only = (purpose == "EXIT")

            if reduce_only:
                print(f"[平仓订单] {symbol} {side} (市价单自动识别平仓)")

            query = urlencode(params)
            signature = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
            final_url = f"{self.base_url}/fapi/v1/order?{query}&signature={signature}"
            headers = {"X-MBX-APIKEY": API_KEY}

            response = self.rest_session.post(final_url, headers=headers, timeout=5)
            data = response.json()

            if "orderId" in data:
                action = "建仓" if purpose == "ENTRY" else "平仓" if purpose == "EXIT" else "下单"
                print(f"[市价{action}成功] {symbol} {side} → ID={data['orderId']}")
                return str(data["orderId"])
            else:
                action = "建仓" if purpose == "ENTRY" else "平仓" if purpose == "EXIT" else "下单"
                print(f"[市价{action}失败] {symbol} {side} → 返回数据: {data}")
                return None
        except Exception as e:
            action = "建仓" if purpose == "ENTRY" else "平仓" if purpose == "EXIT" else "下单"
            print(f"[市价{action}异常] {symbol} {side} → {e}")
            return None
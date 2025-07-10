import time
import os
import json
from datetime import datetime, timezone
from pathlib import Path
from core.time_utils import now, timestamp, str_now
from utils.market_analysis import EnhancedMarketAnalyzer

from statistics import mean
from strategy.strategy_core import get_combined_signal
from utils.support_resistance import detect_support_resistance_levels
from utils.volatility_filter import is_market_flat, is_market_active_again
from core.data_logger import save_entry_signal, save_exit_record
from decimal import Decimal, ROUND_DOWN
from core.config_trading import SYMBOL_QUANTITY_PRECISION, SYMBOL_TICK_SIZE
import requests, hmac, hashlib
from config import API_KEY, API_SECRET
from core.direction_filter import is_entry_direction_safe
from utils.exit_predictor import extract_exit_features, predict_peak_profit
from core.order_event_bus import publish_order_event
from core.direction_conflict_guard import check_direction_conflict, register_position, clear_position, confirm_entry_success, cancel_entry_reservation
from core.pending_order_monitor import get_pending_order_monitor
from utils.volatility_filter import is_market_flat
from utils.exit_score import extract_exit_features, predict_peak_profit
from core.risk_control import get_trade_quantity  # ✅ 这是支持4参数的独立函数
from core.partial_exit import LOSS_COOLDOWN_RECORD  # 新增导入
from core.unified_cooldown_manager import UnifiedCooldownManager
from core.directional_correlation_manager import DirectionalCorrelationManager
from core.global_directional_risk_manager import GlobalDirectionalRiskManager
from core.intelligent_directional_weight_manager import IntelligentDirectionalWeightManager
from core.unified_api_manager import get_unified_api_manager



class SymbolEngine:
    def __init__(self, symbol, modules):
        self.symbol = symbol
        self.market = modules["market"]
        self.trader = modules["trader"]
        self.tracker = modules["tracker"]
        self.pm = modules["position_manager"]
        self.risk_guard = modules["risk_guard"]
        self.allocator = modules["allocator"]
        self.trend_predictor = modules["trend_predictor"]
        self.cooldown_guard = modules["cooldown_guard"]
        self.trade_logger = modules["trade_logger"]
        self.logger = modules["logger"]
        self.risk = modules["risk"]
        self.config = modules["config"]
        self.get_allowed_symbols = modules["get_allowed_symbols"]
        self.ptp = modules["partial_exit"]

        # 🚨 启动模式 - 完全禁用API调用
        self.startup_mode = True
        self.api_calls_disabled = True
        self.data_sync_scheduled = False
        self.startup_time = time.time()  # 记录启动时间
        self.logger.warning(f"[{symbol}] 🚨 启动模式：禁用所有API调用，使用默认值")

        self.last_entry_time = {"LONG": 0, "SHORT": 0}
        self.previous_signal = {"LONG": None, "SHORT": None}
        self.signal_streak = {"LONG": 0, "SHORT": 0}
        self.reverse_streak = {"LONG": 0, "SHORT": 0}
        self.optimize_counter = 0
        self.loss_cooldown_record = {"LONG": 0, "SHORT": 0}

        # 🌍 国际顶级策略集成 - 增强市场分析器
        self.market_analyzer = EnhancedMarketAnalyzer()

        # 🛡️ 统一冷却期管理器 - 解决三小时保护机制问题
        self.cooldown_manager = UnifiedCooldownManager(self.symbol, self.logger)

        # 🎯 多空方向相关性管理器 - 利用同批建仓多空负相关效应
        try:
            from core.config_trading import SYMBOL_LIST
            # 使用全局单例模式，确保所有币种共享同一个管理器
            if not hasattr(SymbolEngine, '_correlation_manager'):
                SymbolEngine._correlation_manager = DirectionalCorrelationManager(SYMBOL_LIST)
            self.correlation_manager = SymbolEngine._correlation_manager
        except Exception as e:
            self.logger.warning(f"[{self.symbol}] ❌ 多空相关性管理器初始化失败: {e}")
            self.correlation_manager = None

        # 🌍 全局方向风险管理器 - 防止整体方向亏损时继续建仓
        try:
            from core.config_trading import SYMBOL_LIST
            # 使用全局单例模式，确保所有币种共享同一个管理器
            if not hasattr(SymbolEngine, '_global_risk_manager'):
                SymbolEngine._global_risk_manager = GlobalDirectionalRiskManager(SYMBOL_LIST, self.logger)
            self.global_risk_manager = SymbolEngine._global_risk_manager
        except Exception as e:
            self.logger.warning(f"[{self.symbol}] ❌ 全局方向风险管理器初始化失败: {e}")
            self.global_risk_manager = None

        # 🧠 智能方向权重管理器 - 科学计算建仓方向权重
        try:
            from core.config_trading import SYMBOL_LIST
            # 使用全局单例模式，确保所有币种共享同一个管理器
            if not hasattr(SymbolEngine, '_weight_manager'):
                SymbolEngine._weight_manager = IntelligentDirectionalWeightManager(SYMBOL_LIST, self.logger)
            self.weight_manager = SymbolEngine._weight_manager
        except Exception as e:
            self.logger.warning(f"[{self.symbol}] ❌ 智能权重管理器初始化失败: {e}")
            self.weight_manager = None

        # 🔧 统一API管理器 - 解决API频率限制问题
        try:
            self.unified_api_manager = get_unified_api_manager(self.trader, self.logger)
            if self.unified_api_manager is None:
                from core.unified_api_manager import init_unified_api_manager
                self.unified_api_manager = init_unified_api_manager(self.trader, self.logger)
            self.logger.info(f"[{self.symbol}] 🔧 统一API管理器已集成")
        except Exception as e:
            self.logger.warning(f"[{self.symbol}] ❌ 统一API管理器初始化失败: {e}")
            self.unified_api_manager = None

        self.cooldown_path = f"logs/{self.symbol}/loss_cooldown_{self.symbol}.json"
        try:
            if os.path.exists(self.cooldown_path):
                with open(self.cooldown_path, "r") as f:
                    self.loss_cooldown_record = json.load(f)
                    self.logger.info(f"[冷却记录载入成功] {self.symbol}: {self.loss_cooldown_record}")
            else:
                os.makedirs(os.path.dirname(self.cooldown_path), exist_ok=True)
                with open(self.cooldown_path, "w") as f:
                    json.dump(self.loss_cooldown_record, f)
                self.logger.info(f"[冷却文件已创建] {self.cooldown_path}")
        except Exception as e:
            self.logger.error(f"[冷却记录加载失败] {self.symbol} → {e}")

        # 🔧 超时转市价统计
        self.timeout_stats = {
            "total_timeouts": 0,
            "successful_conversions": 0,
            "failed_conversions": 0,
            "last_timeout_time": 0
        }

    def get_current_price(self):
        """🚨 紧急模式：强制使用WebSocket价格，不调用API"""
        try:
            # 🚨 紧急模式：只使用WebSocket价格，不调用任何API
            price = self.market.get_last_price(self.symbol)
            if price and price > 0:
                return price

            # 🚨 如果WebSocket也没有数据，返回默认价格
            self.logger.warning(f"[{self.symbol}] 🚨 WebSocket无价格数据，使用默认价格")
            return 1.0  # 默认价格

        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 获取价格失败: {e}")
            return 1.0  # 默认价格

    def get_position_amt_unified(self, side):
        """🚨 紧急模式：强制使用本地tracker数据，不调用API"""
        try:
            # 🚨 紧急模式：只使用本地tracker数据，不调用任何API
            amt = self.tracker.get_total_quantity(side)
            self.logger.debug(f"[{self.symbol}] 🚨 使用本地持仓数据: {side} = {amt}")
            return amt

        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 获取本地持仓失败: {e}")
            return 0.0

    def get_balance_unified(self, asset="USDC"):
        """🚨 紧急模式：使用配置中的默认余额，不调用API"""
        try:
            # 🚨 紧急模式：使用固定余额，不调用任何API
            default_balance = 10000.0  # 临时固定值
            self.logger.debug(f"[{self.symbol}] 🚨 使用默认余额: {asset} = {default_balance}")
            return default_balance

        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 获取默认余额失败: {e}")
            return 10000.0

    def has_open_position(self):
        return (
            self.tracker.get_total_quantity("LONG") > 0 or
            self.tracker.get_total_quantity("SHORT") > 0
        )

    def is_funds_allowed_for_entry(self):
        """
        🔧 最终建仓前检查是否仍在总资金允许范围内（统一API管理器版）
        """
        try:
            # 🔧 使用本地tracker数据，避免API调用
            long_qty = self.tracker.get_total_quantity("LONG")
            short_qty = self.tracker.get_total_quantity("SHORT")

            # 🔧 使用统一价格获取
            price = self.get_current_price() or 0
            used_usdt = (long_qty + short_qty) * price

            # 🔧 使用统一余额获取
            total_balance = self.get_balance_unified("USDC")
            if total_balance <= 0:
                # 降级到原有方法
                if hasattr(self.trader, 'get_usdc_balance'):
                    total_balance = self.trader.get_usdc_balance()
                else:
                    total_balance = self.risk.get_balance()

            max_allowed = total_balance * self.config.get("MAX_TOTAL_POSITION_PERCENT", 0.5)

            self.logger.info(
                f"[{self.symbol}] 💰 建仓资金检查 → 已用={used_usdt:.2f} / 限额={max_allowed:.2f} | 总余额={total_balance:.2f}"
            )

            return used_usdt < max_allowed

        except Exception as e:
            self.logger.warning(f"[{self.symbol}] ⚠️ 获取建仓资金检查失败: {e}")
            return False

    def evaluate_entry_score(self, direction, multi_timeframe_signals, trend, signal_streak,
                              amplitude, avg_amplitude, near_support, near_resistance,
                              risk_blocked, ma20=None, ma60=None, price=None):
        score = 0
        reasons = {}

        same_direction_count = sum([1 for s in multi_timeframe_signals if s == direction])

        # 🔧 进一步优化多周期共识评分 - 基于交易数据分析
        if same_direction_count == 3:
            reasons["multi_tf_consensus"] = 2  # 3个一致，最高分
            score += 2
        elif same_direction_count == 2:
            reasons["multi_tf_consensus"] = 1.5  # 🔧 提升2个一致的权重
            score += 1.5
        elif same_direction_count == 1:
            reasons["multi_tf_consensus"] = 0.5  # 🔧 1个一致也给少量分数
            score += 0.5
        else:
            reasons["multi_tf_consensus"] = 0  # 🔧 0个一致不扣分，保持中性
            score += 0

        # 🔧 进一步优化趋势对齐评分 - 基于交易数据分析
        if trend == direction:
            reasons["trend_alignment"] = 1
            score += 1
        elif trend == "FLAT":
            reasons["trend_alignment"] = 0  # 🔧 横盘时不扣分
            score += 0
        else:
            reasons["trend_alignment"] = -0.5  # 🔧 进一步减少惩罚，从-1改为-0.5
            score -= 0.5

        reasons["signal_streak"] = int(signal_streak >= 3)
        score += reasons["signal_streak"]

        reasons["amplitude_boost"] = int(amplitude > avg_amplitude)
        score += reasons["amplitude_boost"]

        if ma20 is not None and ma60 is not None and price is not None:
            if direction == "long" and price > ma20 > ma60:
                reasons["ma_trend_alignment"] = 1
                score += 1
            elif direction == "short" and price < ma20 < ma60:
                reasons["ma_trend_alignment"] = 1
                score += 1
            else:
                reasons["ma_trend_alignment"] = 0

        if near_support and direction == "long":
            score -= 1
            reasons["near_support_block"] = -1
        if near_resistance and direction == "short":
            score -= 1
            reasons["near_resistance_block"] = -1

        if risk_blocked:
            score -= 1
            reasons["risk_guard_block"] = -1

        bias_score = self.get_global_direction_bias(direction)
        if bias_score != 0:
            score += bias_score
            reasons["global_direction_bias"] = bias_score

        # 🎯 多空相关性调整
        try:
            if self.correlation_manager:
                correlation_adjustment = self.correlation_manager.get_direction_bias_adjustment(direction)
                if correlation_adjustment != 1.0:
                    correlation_score = (correlation_adjustment - 1.0) * 2  # 转换为评分
                    score += correlation_score
                    reasons["directional_correlation"] = correlation_score
                    self.logger.debug(f"[🎯 相关性调整] {self.symbol} {direction} → 调整系数:{correlation_adjustment:.2f} 评分:{correlation_score:+.2f}")
        except Exception as e:
            self.logger.debug(f"[{self.symbol}] 相关性调整失败: {e}")

        return score, reasons

    def get_global_direction_bias(self, direction):
        try:
            orchestrator = self.get_allowed_symbols.__self__
            all_positions = orchestrator.get_all_positions()
        except Exception as e:
            self.logger.warning(f"[{self.symbol}] ❌ 获取全局持仓失败: {e}")
            return 0.0

        long_pnls, short_pnls = [], []
        for sym, pos in all_positions.items():
            for side in ["LONG", "SHORT"]:
                p = pos.get(side, {})
                if p.get("qty", 0) > 0:
                    pnl = p.get("unrealized_percent", 0.0)
                    if side == "LONG":
                        long_pnls.append(pnl)
                    else:
                        short_pnls.append(pnl)

        total_active = len(long_pnls) + len(short_pnls)
        if total_active <= 6:
            return 0.0

        avg_long = sum(long_pnls) / len(long_pnls) if long_pnls else 0.0
        avg_short = sum(short_pnls) / len(short_pnls) if short_pnls else 0.0

        if abs(avg_long - avg_short) < 0.2:
            bias = 0.0
        elif avg_long > avg_short:
            bias = 0.5 if direction == "long" else -1.5
        else:
            bias = 0.5 if direction == "short" else -1.5

        self.logger.debug(
            f"[{self.symbol}] 🌍 全局方向偏好评分：LONG盈={avg_long:.2f}% SHORT盈={avg_short:.2f}% → "
            f"当前方向={direction} → bias={bias:+.2f}"
        )
        return bias

    def get_leverage_v2(self, side):
        """🔧 零API调用杠杆获取 - 完全基于配置文件，避免封IP风险"""
        try:
            # 1. 从缓存获取（避免重复文件IO）
            if not hasattr(self, '_leverage_cache'):
                self._leverage_cache = self._load_leverage_config()

            # 2. 获取币种特定杠杆
            leverage = self._leverage_cache.get(self.symbol)
            if leverage:
                return leverage

            # 3. 使用保守默认值（新币种或配置缺失）
            default_leverage = 3  # 最保守的杠杆，避免风险
            self.logger.warning(f"[{self.symbol}] ⚠️ 未配置杠杆，使用默认{default_leverage}x")
            return default_leverage

        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 杠杆获取失败: {e}")
            return 3  # 最保守的回退值

    def _load_leverage_config(self):
        """🔧 加载杠杆配置（带缓存）- 零API调用"""
        try:
            import json
            from pathlib import Path

            config_file = Path("leverage_config.json")
            if not config_file.exists():
                self.logger.warning("⚠️ leverage_config.json不存在")
                return {}

            with open(config_file, 'r', encoding='utf-8') as f:
                leverage_config = json.load(f)

            symbol_leverage = leverage_config.get("symbol_leverage", {})
            default_leverage = leverage_config.get("default_leverage", 3)

            # 为未配置的币种设置默认值
            from core.config_trading import SYMBOL_LIST
            for symbol in SYMBOL_LIST:
                if symbol not in symbol_leverage:
                    symbol_leverage[symbol] = default_leverage

            self.logger.info(f"✅ 杠杆配置加载成功: {len(symbol_leverage)} 个币种")
            return symbol_leverage

        except Exception as e:
            self.logger.error(f"❌ 杠杆配置加载失败: {e}")
            return {}

    def _get_dynamic_risk_params(self, leverage):
        """🔧 基于杠杆的动态风险参数"""
        try:
            # 基础风险系数
            risk_multiplier = leverage / 5.0  # 以5x为基准

            # 动态止损线（杠杆越高止损越紧）
            base_stop_loss = 2.5  # 基础止损2.5%
            dynamic_stop_loss = min(base_stop_loss, 5.0 / leverage)

            # 动态仓位系数（杠杆越高仓位越小）
            base_position_ratio = 0.1  # 基础仓位10%
            dynamic_position_ratio = max(0.03, base_position_ratio / risk_multiplier)

            # 动态止盈目标（杠杆越高止盈越高）
            base_take_profit = 1.0  # 基础止盈1%
            dynamic_take_profit = max(base_take_profit, 2.0 * risk_multiplier)

            risk_params = {
                'leverage': leverage,
                'risk_multiplier': risk_multiplier,
                'stop_loss_pct': dynamic_stop_loss,
                'position_ratio': dynamic_position_ratio,
                'take_profit_pct': dynamic_take_profit
            }

            self.logger.debug(f"[{self.symbol}] 🔧 动态风险参数: 杠杆{leverage}x → 止损{dynamic_stop_loss:.1f}% | 仓位{dynamic_position_ratio:.1%} | 止盈{dynamic_take_profit:.1f}%")

            return risk_params

        except Exception as e:
            self.logger.warning(f"[{self.symbol}] ❌ 动态风险参数计算失败: {e}")
            # 返回保守默认值
            return {
                'leverage': 3,
                'risk_multiplier': 0.6,
                'stop_loss_pct': 2.0,
                'position_ratio': 0.05,
                'take_profit_pct': 1.5
            }

    # 其余逻辑保持不变 ...（如 run_kline_logic）


    # 🛡️【统一冷却期】使用统一冷却期管理器
    def is_in_loss_cooldown(self, side, cooldown_hours=3):
        """检查是否在亏损冷却期 - 使用统一管理器"""
        try:
            # 使用统一冷却期管理器检查
            in_cooldown, cooldown_data, remaining_seconds = self.cooldown_manager.is_in_cooldown(side.upper(), 'loss')

            if in_cooldown:
                remaining_hours = remaining_seconds / 3600
                self.logger.warning(
                    f"[🚫 统一冷却期] {self.symbol} {side} → "
                    f"类型: {cooldown_data['type']}, 剩余: {remaining_hours:.1f}小时, "
                    f"原因: {cooldown_data.get('reason', 'N/A')}"
                )
                return True
            else:
                self.logger.debug(f"[✅ 冷却期检查] {self.symbol} {side} → 无冷却期限制")
                return False

        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 冷却期检查失败: {e}")
            # 异常时保守返回True，避免违规
            return True


    def run_kline_logic(self, current_time, price, klines):
        """主要K线处理逻辑 - 优化后的版本"""
        # 🚨 启动模式自动关闭：2分钟后自动切换到正常模式
        if hasattr(self, 'startup_mode') and self.startup_mode:
            startup_duration = time.time() - getattr(self, 'startup_time', 0)
            if startup_duration > 120:  # 2分钟后自动关闭启动模式
                self.startup_mode = False
                self.api_calls_disabled = False
                self.logger.info(f"[{self.symbol}] ✅ 启动模式已自动关闭，切换到正常运行模式")
            else:
                remaining = 120 - startup_duration
                self.logger.debug(f"[{self.symbol}] 🚨 启动模式：还需等待{remaining:.0f}秒")
                return

        # 1. 基础检查
        if not self._check_basic_conditions(price, klines):
            return

        # 2. 获取市场数据和信号
        market_data = self._prepare_market_data(price, klines)
        if not market_data:
            return

        # 3. 同步持仓状态（优化：降低同步频率）
        self._sync_positions_optimized(price)

        # 4. 处理建仓逻辑
        self._process_entry_logic(market_data, klines, current_time)

        # 5. 保存状态和心跳
        self._save_state_and_heartbeat(price, current_time)

    def disable_startup_mode(self):
        """🔧 手动关闭启动模式，切换到正常运行"""
        if hasattr(self, 'startup_mode'):
            self.startup_mode = False
            self.api_calls_disabled = False
            self.logger.info(f"[{self.symbol}] ✅ 启动模式已手动关闭，切换到正常运行模式")
            return True
        return False

    def _run_local_logic_only(self, current_time, price, klines):
        """🚨 纯本地逻辑运行 - 不调用任何API"""
        try:
            # 1. 基础检查（使用本地数据）
            if not self._check_basic_conditions_local(price, klines):
                return

            # 2. 获取市场数据（使用缓存数据）
            market_data = self._prepare_market_data_local(price, klines)
            if not market_data:
                return

            # 3. 处理建仓逻辑（使用本地数据）
            self._process_entry_logic_local(market_data, klines, current_time)

            # 4. 本地状态保存
            self._save_state_local(price, current_time)

            self.logger.debug(f"[{self.symbol}] 🚨 本地逻辑运行完成")

        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 本地逻辑运行异常: {e}")

    def _save_state_and_heartbeat_local(self, price, current_time):
        """🚨 本地状态保存和心跳 - 不调用API"""
        try:
            # 保存tracker状态
            self.tracker.save_state()

            # 本地心跳（不调用API）
            if int(current_time) % 60 == 0:
                self.logger.info(f"[{self.symbol}] 🫀 本地心跳正常 | 时间：{datetime.now().strftime('%H:%M:%S')} | 价格: {price:.4f}")

        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 本地状态保存失败: {e}")

    def _check_basic_conditions(self, price, klines):
        """基础条件检查"""
        # 检查是否允许建仓
        allowed = self.get_allowed_symbols()
        has_position = self.has_open_position()

        # ✅ 灵活的建仓策略配置
        from core.config_trading import SYMBOL_LIST

        if self.symbol not in SYMBOL_LIST:
            self.logger.debug(f"[{self.symbol}] ❌ 不在配置币种列表中，跳过建仓")
            return False

        # 获取建仓策略配置
        entry_strategy = self.config.get("ENTRY_STRATEGY", "TOP_ONLY")

        if entry_strategy == "ALL_SYMBOLS":
            # 策略1：允许所有配置币种建仓
            if self.symbol not in allowed and not has_position:
                self.logger.debug(f"[{self.symbol}] 💡 非Top币种建仓模式：允许所有配置币种")
        elif entry_strategy == "TOP_PLUS_POSITION":
            # 策略2：只允许Top币种 + 已有持仓币种建仓（默认策略）
            if self.symbol not in allowed and not has_position:
                self.logger.debug(f"[{self.symbol}] ⏸ Top+持仓模式：跳过非Top且无持仓币种")
                return False
        else:
            # 策略3：只允许Top币种建仓（最严格）
            if self.symbol not in allowed:
                self.logger.debug(f"[{self.symbol}] ⏸ 仅Top模式：跳过非Top币种")
                return False

        # 补齐多周期K线
        self._ensure_multi_timeframe_klines()

        # 检查横盘状态
        klines_1m = self.market.get_cached_klines(self.symbol)
        if klines_1m is None or len(klines_1m) < 20:
            self.logger.warning(f"[横盘检测跳过] {self.symbol} → K线不足: {len(klines_1m) if klines_1m else 0}")
        elif is_market_flat(klines_1m):
            self.logger.debug(f"[横盘保护] {self.symbol} → 横盘状态，跳过建仓")
            return False

        # 检查多周期K线是否存在且数据充足
        klines_3m = self.market.get_cached_klines(f"{self.symbol}_3m")
        klines_5m = self.market.get_cached_klines(f"{self.symbol}_5m")
        klines_15m = self.market.get_cached_klines(f"{self.symbol}_15m")

        # 检查每个周期的K线数据
        if not klines_3m or len(klines_3m) < 20:
            self.logger.warning(f"[{self.symbol}] ❌ 3m K线数据不足: {len(klines_3m) if klines_3m else 0} → 跳过执行")
            return False
        if not klines_5m or len(klines_5m) < 20:
            self.logger.warning(f"[{self.symbol}] ❌ 5m K线数据不足: {len(klines_5m) if klines_5m else 0} → 跳过执行")
            return False
        if not klines_15m or len(klines_15m) < 20:
            self.logger.warning(f"[{self.symbol}] ❌ 15m K线数据不足: {len(klines_15m) if klines_15m else 0} → 跳过执行")
            return False

        # 检查1m K线数据
        if not klines or len(klines) < 20:
            self.logger.warning(f"[{self.symbol}] ❌ 1m K线数据不足: {len(klines) if klines else 0} → 跳过执行")
            return False

        return True

    def _ensure_multi_timeframe_klines(self):
        """🚨 确保多周期K线数据存在 - 大幅减少API调用频率"""
        # 🚨 启动模式：完全跳过K线补齐
        if hasattr(self, 'startup_mode') and self.startup_mode:
            return

        # 🔧 减少检查频率：每5分钟才检查一次
        current_time = time.time()
        if not hasattr(self, '_last_kline_check_time'):
            self._last_kline_check_time = {}

        for interval in ["3m", "5m", "15m"]:
            key = f"{self.symbol}_{interval}"
            last_check = self._last_kline_check_time.get(key, 0)

            # 🔧 只有5分钟内没检查过才进行检查
            if current_time - last_check < 300:  # 5分钟
                continue

            self._last_kline_check_time[key] = current_time

            if not self.market.get_cached_klines(key):
                # 🛡️ 使用统一API管理的trader获取K线数据，避免绕过API保护
                try:
                    # 检查是否有可用的trader实例（通过统一API管理）
                    if hasattr(self, 'trader') and self.trader:
                        self.logger.warning(f"[{self.symbol}] 🛡️ 通过统一API管理获取K线: {interval}")
                        resp = self.trader.client.klines(symbol=self.symbol, interval=interval, limit=100)
                        # K线数据格式：[open, high, low, close, volume] (5列)
                        klines_fetched = [
                            [float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])]
                            for r in resp
                        ]
                        symbol, interval = key.split("_")
                        self.market.update_klines(symbol=symbol, klines=klines_fetched, interval=interval)
                        self.logger.warning(f"[{self.symbol}] ⛽ 通过API管理补齐 {interval} → K线 {len(klines_fetched)} 条")
                    else:
                        # 如果没有trader实例，跳过K线补齐以避免绕过API管理
                        self.logger.warning(f"[{self.symbol}] ⚠️ 跳过K线补齐（无API管理trader）: {interval}")
                except Exception as e:
                    self.logger.warning(f"[{self.symbol}] ❌ API管理保护下K线获取失败: {e}")
                    # API调用失败时不再尝试直接调用，避免绕过保护

    def _prepare_market_data(self, price, klines):
        """准备市场数据和信号"""
        try:
            # 获取多周期信号 - 添加空值检查
            klines_3m = self.market.get_cached_klines(f"{self.symbol}_3m")
            klines_5m = self.market.get_cached_klines(f"{self.symbol}_5m")
            klines_15m = self.market.get_cached_klines(f"{self.symbol}_15m")

            # 检查K线数据是否有效
            if not klines_3m or len(klines_3m) < 20:
                self.logger.warning(f"[{self.symbol}] ❌ 3m K线数据不足: {len(klines_3m) if klines_3m else 0}")
                return None
            if not klines_5m or len(klines_5m) < 20:
                self.logger.warning(f"[{self.symbol}] ❌ 5m K线数据不足: {len(klines_5m) if klines_5m else 0}")
                return None
            if not klines_15m or len(klines_15m) < 20:
                self.logger.warning(f"[{self.symbol}] ❌ 15m K线数据不足: {len(klines_15m) if klines_15m else 0}")
                return None

            signal_3m, _ = get_combined_signal(klines_3m)
            signal_5m, _ = get_combined_signal(klines_5m)
            signal_15m, _ = get_combined_signal(klines_15m)
            multi_timeframe_signals = [signal_3m, signal_5m, signal_15m]

            # 计算基础指标 - 添加空值检查
            if not klines or len(klines) < 20:
                self.logger.warning(f"[{self.symbol}] ❌ 1m K线数据不足: {len(klines) if klines else 0}")
                return None

            closes = [float(k[4]) for k in klines[-60:]]
            ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
            ma60 = sum(closes) / 60 if len(closes) >= 60 else None

            # 获取趋势
            trend_info = self.trend_predictor.predict_trend(klines)
            trend = trend_info["trend"] if isinstance(trend_info, dict) else trend_info
            if trend not in ("UP", "DOWN", "FLAT"):
                self.logger.warning(f"[{self.symbol}] ⚠️ trend_predictor 返回非法值：{trend_info} → 默认使用 FLAT")
                trend = "FLAT"

            # 获取支撑阻力位
            support_levels, resistance_levels = detect_support_resistance_levels(klines)

            # 更新浮动盈亏
            self.tracker.update_floating_pnl("LONG", price)
            self.tracker.update_floating_pnl("SHORT", price)

            # 更新信号追踪
            signal, _ = get_combined_signal(klines)
            if signal == "long":
                self.signal_streak["LONG"] += 1
                self.signal_streak["SHORT"] = 0
            elif signal == "short":
                self.signal_streak["SHORT"] += 1
                self.signal_streak["LONG"] = 0
            else:
                self.signal_streak["LONG"] = 0
                self.signal_streak["SHORT"] = 0

            # 计算振幅指标
            if len(klines) >= 10:
                avg_amplitude = mean([(k[2] - k[3]) / k[3] for k in klines[-10:]])
                amplitude = (klines[-1][2] - klines[-1][3]) / klines[-1][3]
            else:
                avg_amplitude = 0.01  # 默认值
                amplitude = 0.01

            return {
                "price": price,
                "multi_timeframe_signals": multi_timeframe_signals,
                "ma20": ma20,
                "ma60": ma60,
                "trend": trend,
                "support_levels": support_levels,
                "resistance_levels": resistance_levels,
                "amplitude": amplitude,
                "avg_amplitude": avg_amplitude,
                "signal": signal
            }
        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 准备市场数据失败: {e}")
            return None

    def _sync_positions(self, price):
        """同步持仓状态并清理已不存在的方向"""
        try:
            self.trader.sync_position_from_binance(self.symbol, self.tracker)
            if self.tracker.get_current_position("LONG") == 0:
                self.tracker.close_orders("LONG")
                clear_position(self.symbol, "LONG")  # 清除持仓记录
                self.logger.info(f"[{self.symbol}] 🧹 无 LONG 仓 → 清理本地订单")
            if self.tracker.get_current_position("SHORT") == 0:
                self.tracker.close_orders("SHORT")
                clear_position(self.symbol, "SHORT")  # 清除持仓记录
                self.logger.info(f"[{self.symbol}] 🧹 无 SHORT 仓 → 清理本地订单")
        except Exception as e:
            self.logger.warning(f"[{self.symbol}] ⚠️ 建仓前同步失败: {e}")

    def _sync_positions_optimized(self, price):
        """优化版持仓同步 - 减少API调用频率"""
        try:
            # 检查是否需要同步（降低同步频率）
            current_time = time.time()
            last_sync_key = f"last_sync_{self.symbol}"

            if not hasattr(self, '_last_sync_times'):
                self._last_sync_times = {}

            last_sync = self._last_sync_times.get(last_sync_key, 0)
            sync_interval = 30   # 🔥 紧急修复：从600秒大幅缩短到30秒，提升止盈止损响应速度

            if current_time - last_sync < sync_interval:
                # 跳过同步，使用缓存的持仓数据进行清理检查
                self._check_and_clean_orders_cached()
                return

            # 执行同步
            self.trader.sync_position_from_binance(self.symbol, self.tracker)
            self._last_sync_times[last_sync_key] = current_time

            # 清理检查
            if self.tracker.get_current_position("LONG") == 0:
                self.tracker.close_orders("LONG")
                clear_position(self.symbol, "LONG")  # 清除持仓记录
                self.logger.info(f"[{self.symbol}] 🧹 无 LONG 仓 → 清理本地订单")
            if self.tracker.get_current_position("SHORT") == 0:
                self.tracker.close_orders("SHORT")
                clear_position(self.symbol, "SHORT")  # 清除持仓记录
                self.logger.info(f"[{self.symbol}] 🧹 无 SHORT 仓 → 清理本地订单")

        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 优化同步持仓失败: {e}")

    def _check_and_clean_orders_cached(self):
        """使用真正的缓存数据检查和清理订单 - 避免API调用"""
        try:
            # 🔧 修复：使用本地tracker数据，避免API调用
            # 原代码仍然调用get_position_amt，这不是真正的缓存！

            # 检查本地tracker是否有订单但可能已被外部平仓
            long_orders = self.tracker.get_active_orders("LONG")
            short_orders = self.tracker.get_active_orders("SHORT")

            # 如果本地有订单记录，但可能需要清理，先跳过API调用
            # 等待下次正式同步时再处理，避免频繁API调用

            self.logger.debug(f"[{self.symbol}] 🧹 缓存清理检查：LONG={len(long_orders)}个订单, SHORT={len(short_orders)}个订单")

            # 注意：这里不再调用API，等待定时同步处理

        except Exception as e:
            self.logger.debug(f"[{self.symbol}] 缓存清理检查失败: {e}")

    def _should_sync_before_entry(self):
        """🚨 判断是否需要在建仓前同步持仓 - 大幅减少API调用"""
        # 🚨 启动模式：完全跳过同步
        if hasattr(self, 'startup_mode') and self.startup_mode:
            return False

        try:
            import time
            current_time = time.time()

            if not hasattr(self, '_last_sync_times'):
                self._last_sync_times = {}

            last_sync_key = f"last_sync_{self.symbol}"
            last_sync = self._last_sync_times.get(last_sync_key, 0)

            # 🚨 大幅减少API调用：从10分钟延长到30分钟
            return current_time - last_sync > 1800  # 30分钟才同步一次

        except Exception:
            # 出错时也不同步，避免API调用
            return False

    def _check_pending_orders_timeout(self):
        """🔧 检查建仓挂单是否超时，超时则撤单并转市价单"""
        for side in ["LONG", "SHORT"]:
            orders = self.tracker.get_active_orders(side)
            for o in orders:
                if o.get("entry_pending") and not o.get("closed"):
                    # 🛡️ 检查是否已被其他超时机制处理
                    if o.get("timeout_processing"):
                        continue

                    submit_time = o.get("submit_time", 0)
                    if timestamp() - submit_time > 30:  # 🔧 缩短到30秒兜底保护
                        try:
                            # 🛡️ 标记正在处理，避免重复处理
                            o["timeout_processing"] = True
                            self.logger.warning(f"[建仓超时保护] {self.symbol} {side} → 挂单超时30秒，撤单并转市价")

                            # 🔧 撤销原挂单 - 集成API频率限制
                            order_id = o.get("order_id")
                            if order_id:
                                cancel_success = self._cancel_order_with_rate_limit(order_id)
                                if cancel_success:
                                    time.sleep(1.5)  # 增加等待时间，避免连续API调用

                            # 🔧 转市价单确保建仓
                            qty = o.get("qty", 0)
                            if qty > 0:
                                # 🔧 记录超时统计
                                self.timeout_stats["total_timeouts"] += 1
                                self.timeout_stats["last_timeout_time"] = timestamp()

                                direction = "long" if side == "LONG" else "short"
                                market_price = self.get_current_price()

                                if market_price:
                                    # 🔧 使用集成API频率限制的市价单方法
                                    order_side = "BUY" if side == "LONG" else "SELL"
                                    market_order_id = self._place_market_order_with_rate_limit(order_side, qty)
                                    if market_order_id:
                                        self.logger.info(f"[建仓超时保护] ✅ 市价单建仓成功: {side} {qty}")
                                        # 更新订单信息
                                        o["order_id"] = market_order_id
                                        o["entry_price"] = market_price
                                        o["entry_pending"] = False
                                        # 🔧 记录成功转换
                                        self.timeout_stats["successful_conversions"] += 1
                                    else:
                                        self.logger.error(f"[建仓超时保护] ❌ 市价单建仓失败")
                                        # 🔧 记录失败转换
                                        self.timeout_stats["failed_conversions"] += 1

                            o["closed"] = True
                            self.logger.info(f"[建仓超时保护] ✅ 超时处理完成")

                        except Exception as e:
                            self.logger.warning(f"[建仓超时保护] ❌ 处理失败: {e}")



    def _process_entry_logic(self, market_data, klines, current_time):
        """处理建仓逻辑"""
        price = market_data["price"]
        multi_timeframe_signals = market_data["multi_timeframe_signals"]
        ma20 = market_data["ma20"]
        ma60 = market_data["ma60"]
        trend = market_data["trend"]
        support_levels = market_data["support_levels"]
        resistance_levels = market_data["resistance_levels"]
        amplitude = market_data["amplitude"]
        avg_amplitude = market_data["avg_amplitude"]

        # 遍历建仓方向
        for direction in ["long", "short"]:
            side = "LONG" if direction == "long" else "SHORT"
            reverse = "SHORT" if side == "LONG" else "LONG"

            # 🌍 专业大盘策略检查（关键策略，必须首先检查）
            try:
                from core.simple_professional_analyzer import simple_professional_analyzer

                # 检查简化专业大盘分析是否允许当前方向建仓
                allowed, reason = simple_professional_analyzer.should_allow_entry(direction, self.symbol)
                if not allowed:
                    self.logger.warning(f"[{self.symbol}] 🌍 专业大盘策略阻止{direction}建仓 → {reason}")
                    continue
                else:
                    # 获取详细的市场分析
                    market_analysis = simple_professional_analyzer.get_market_direction()
                    self.logger.debug(
                        f"[{self.symbol}] ✅ 专业大盘策略允许{direction}建仓 → "
                        f"{market_analysis['direction']} {market_analysis['strength']} "
                        f"(置信度: {market_analysis['confidence']:.2f}) | {reason}"
                    )
            except Exception as e:
                self.logger.warning(f"[{self.symbol}] ⚠️ 专业大盘策略检查失败，采用保守策略拒绝建仓: {e}")
                continue

            # 🛡️【统一冷却期】使用统一冷却期管理器检查（暂时跳过，在后续评分后检查）
            # 这里先跳过，在_check_cooldown_and_score中进行完整检查

            # === 多周期方向一致性判断 ===
            same_count = multi_timeframe_signals.count(direction)
            if same_count < 2:
                self.logger.debug(f"[{self.symbol}] ❌ {side} 方向不一致 {multi_timeframe_signals} → 跳过")
                continue

            # === 刚刚反向平仓冷静期 ===
            last_close = self.tracker.get_last_close_time(reverse)
            if last_close and (timestamp() - last_close < 90):
                self.logger.debug(f"[{self.symbol}] 🧊 {reverse} 刚平仓未满90s → 跳过")
                continue

            # === 持仓方向冲突判断 ===

            # 🔥 增强方向冲突检测
            is_allowed, conflict_reason = check_direction_conflict(self.symbol, side, self.tracker)
            if not is_allowed:
                self.logger.warning(f"[{self.symbol}] 🚨 方向冲突防护阻止: {conflict_reason}")
                continue

            # 原有的方向冲突检查
            if side == "LONG" and self.tracker.get_total_quantity("SHORT") > 0:
                self.logger.warning(f"[{self.symbol}] ❌ 已持有 SHORT 仓，禁止再建多仓")
                continue
            if side == "SHORT" and self.tracker.get_total_quantity("LONG") > 0:
                self.logger.warning(f"[{self.symbol}] ❌ 已持有 LONG 仓，禁止再建空仓")
                continue


            # 🚫 检查当前未平仓订单是否存在极端浮亏（如 < -10%）


            self.tracker.update_floating_pnl(side, price)

            # ✅ 判断当前关键仓是否“正在浮亏”
            # ✅ 增强浮亏检查：任何一笔订单浮亏都禁止建仓
            if not self._enhanced_loss_protection_check(side, price):
                continue

            # 🌍 全局方向风险检查：如果该方向整体亏损则禁止建仓
            if not self._check_global_directional_risk(direction):
                continue

            # === 风控因素准备 ===
            near_support = any(abs(price - s) / price < 0.002 for s in support_levels) if direction == "short" else False
            near_resistance = any(abs(price - r) / price < 0.002 for r in resistance_levels) if direction == "long" else False
            risk_blocked = self.risk_guard.should_block_new_entry(side)

            # === 打分系统 ===
            entry_score, reasons = self.evaluate_entry_score(
                direction, multi_timeframe_signals, trend, self.signal_streak[side],
                amplitude, avg_amplitude, near_support, near_resistance, risk_blocked,
                ma20, ma60, price
            )

            # 🧠 应用智能方向权重调整评分
            original_score = entry_score
            if self.weight_manager:
                try:
                    weight, confidence, weight_reason = self.weight_manager.get_direction_weight(side)
                    # 权重调整评分：权重>1增加评分，权重<1减少评分
                    weight_adjustment = (weight - 1.0) * 2.0  # 权重调整幅度
                    entry_score += weight_adjustment

                    if abs(weight_adjustment) > 0.1:
                        reasons[f"direction_weight_{side.lower()}"] = weight_adjustment
                        self.logger.info(
                            f"[🧠 权重调整] {self.symbol} {side} → "
                            f"原始评分:{original_score:.1f} 权重:{weight:.2f} "
                            f"调整:{weight_adjustment:+.1f} 最终:{entry_score:.1f}"
                        )
                except Exception as e:
                    self.logger.warning(f"[🧠 权重调整] {side}权重应用失败: {e}")

            self.logger.debug(f"[{self.symbol}] 📊 {side} 打分：{entry_score} | {reasons}")

            # 🔧 优化预测浮盈阈值 - 减少过于保守的限制
            predicted_peak = self._calculate_predicted_peak(klines, current_time)
            if predicted_peak < 0.25:  # 🔧 从0.35降到0.25，减少保守限制
                entry_score -= 1
                reasons["weak_profit_projection"] = -1

            # 冷却机制 + override
            if not self._check_cooldown_and_score(side, direction, entry_score, price, klines):
                continue

            # 🔧 分层建仓：根据评分决定仓位大小
            position_multiplier = self._get_position_multiplier(entry_score)

            # 执行建仓
            self._execute_entry(side, direction, price, entry_score, reasons, predicted_peak, position_multiplier)


    def _save_state_and_heartbeat(self, price, current_time):
        """保存状态和心跳"""
        self.tracker.save_state()

        # 🔧 建仓挂单超时检查（超过 25 秒自动撤单并转市价）
        # 🚀 优化：无挂单时跳过检查，减少不必要的遍历
        has_pending_orders = False
        for side in ["LONG", "SHORT"]:
            orders = self.tracker.get_active_orders(side)
            if any(o.get("entry_pending") and not o.get("closed") for o in orders):
                has_pending_orders = True
                break

        if not has_pending_orders:
            # 无挂单时跳过超时检查，减少CPU使用
            pass
        else:
            for side in ["LONG", "SHORT"]:
                orders = self.tracker.get_active_orders(side)
                for o in orders:
                    if o.get("submit_time") and not o.get("closed") and not o.get("exit_order_id"):
                        # 🛡️ 检查是否已被其他超时机制处理
                        if o.get("timeout_processing"):
                            continue

                        elapsed = timestamp() - o["submit_time"]
                        if elapsed > 25:  # 🔧 25秒兜底保护
                            try:
                                order_id = o.get("order_id")
                                if order_id:
                                    # 🛡️ 标记正在处理，避免重复处理
                                    o["timeout_processing"] = True
                                    self.logger.warning(f"[挂单超时处理] {self.symbol} {side} → 超过25秒未成交，撤单并转市价")

                                    # 🔧 撤销挂单 - 集成API频率限制
                                    cancel_success = self._cancel_order_with_rate_limit(order_id)
                                    if cancel_success:
                                        time.sleep(1.5)  # 增加等待时间，避免连续API调用

                                    # 🔧 转市价单 - 集成API频率限制
                                    qty = o.get("qty", 0)
                                    if qty > 0 and o.get("entry_pending"):
                                        market_price = self.get_current_price()

                                        if market_price:
                                            # 使用集成API频率限制的市价单方法
                                            order_side = "BUY" if side == "LONG" else "SELL"
                                            market_order_id = self._place_market_order_with_rate_limit(order_side, qty)
                                            if market_order_id:
                                                self.logger.info(f"[挂单超时处理] ✅ 转市价单成功: {side} {qty}")
                                                o["order_id"] = market_order_id
                                                o["entry_price"] = market_price
                                                o["entry_pending"] = False

                                    o["closed"] = True
                                    o["exit_status"] = "CONVERTED_TO_MARKET"
                                    self.tracker.save_state()
                            except Exception as e:
                                self.logger.error(f"[挂单超时处理] {self.symbol} {side} → {e}")

        # 心跳日志
        if int(timestamp()) % 60 == 0:
            if price is not None:
                self.market.update_price(self.symbol, price)
            self.logger.info(f"[{self.symbol}] 🫀 心跳正常 | 时间：{now().strftime('%H:%M:%S')} | 当前价格: {price:.4f}")

    def _can_open_position(self, side, direction, multi_timeframe_signals, reverse, price):
        """检查是否可以建仓"""
        # 🧠 智能方向权重检查 - 科学评估建仓方向
        direction_weight = 1.0
        if self.weight_manager:
            try:
                weight, confidence, reason = self.weight_manager.get_direction_weight(side)
                direction_weight = weight

                # 记录权重信息
                self.logger.info(
                    f"[🧠 智能权重] {self.symbol} {side} → 权重:{weight:.2f} "
                    f"置信度:{confidence:.2f} 原因:{reason}"
                )

                # 如果权重过低，降低建仓概率而不是完全阻止
                if weight < 0.5:
                    import random
                    if random.random() > weight:  # 基于权重的概率建仓
                        self.logger.warning(
                            f"[🧠 智能权重] {self.symbol} {side} 权重过低({weight:.2f})，概率跳过建仓"
                        )
                        return False

            except Exception as e:
                self.logger.warning(f"[🧠 智能权重] 获取{side}权重失败: {e}")

        # 亏损冷却期判断
        if self.is_in_loss_cooldown(side):
            self.logger.warning(f"[建仓冷却] {self.symbol} {side} 最近亏损未满3小时，跳过本轮建仓")
            return False

        # 多周期方向一致性判断
        same_count = multi_timeframe_signals.count(direction)
        if same_count < 2:
            self.logger.debug(f"[{self.symbol}] ❌ {side} 方向不一致 {multi_timeframe_signals} → 跳过")
            return False

        # 刚刚反向平仓冷静期
        last_close = self.tracker.get_last_close_time(reverse)
        if last_close and (timestamp() - last_close < 90):
            self.logger.debug(f"[{self.symbol}] 🧊 {reverse} 刚平仓未满90s → 跳过")
            return False

        # 持仓方向冲突判断
        if side == "LONG" and self.tracker.get_total_quantity("SHORT") > 0:
            self.logger.warning(f"[{self.symbol}] ❌ 已持有 SHORT 仓，禁止再建多仓")
            return False
        if side == "SHORT" and self.tracker.get_total_quantity("LONG") > 0:
            self.logger.warning(f"[{self.symbol}] ❌ 已持有 LONG 仓，禁止再建空仓")
            return False

        # 更新浮动盈亏
        self.tracker.update_floating_pnl(side, price)

        # 判断当前关键仓是否"正在浮亏"
        if self.tracker.is_extreme_order_currently_loss(side):
            self.logger.warning(f"[{self.symbol}] 🚫 {side} 关键仓当前浮亏 → 禁止方向加仓")
            return False

        return True

    def _enhanced_loss_protection_check(self, side, price):
        """
        增强的浮亏保护检查
        要求：当该币种某方向的建仓中有任何一笔已经浮亏了就不能建仓
        """
        try:
            # 1. 更新所有订单的浮动盈亏
            self.tracker.update_floating_pnl(side, price)

            # 2. 获取该方向所有活跃订单
            active_orders = self.tracker.get_active_orders(side)

            if not active_orders:
                return True  # 没有订单，允许建仓

            # 3. 检查是否有任何订单浮亏
            loss_orders = []
            for order in active_orders:
                pnl = order.get('floating_pnl', 0)
                if pnl < 0:  # 任何浮亏都不允许
                    loss_orders.append({
                        'order_id': order.get('order_id', 'unknown'),
                        'entry_price': order.get('entry_price', 0),
                        'pnl': pnl
                    })

            if loss_orders:
                self.logger.warning(
                    f"[{self.symbol}] 🛡️ {side} 方向浮亏保护触发 → "
                    f"发现{len(loss_orders)}笔浮亏订单 → 禁止建仓"
                )

                # 详细记录浮亏订单信息
                for order in loss_orders:
                    self.logger.info(
                        f"   浮亏订单: {order['order_id']} "
                        f"入场价={order['entry_price']:.4f} "
                        f"浮亏={order['pnl']:.2f}%"
                    )

                return False

            return True

        except Exception as e:
            # 🔧 使用统一异常处理策略
            try:
                from core.unified_exception_handler import handle_mutual_exclusion_exception
                is_allowed, reason = handle_mutual_exclusion_exception(
                    "enhanced_loss_protection_check", e, self.logger,
                    context={"symbol": self.symbol, "side": side}
                )
                self.logger.warning(f"[{self.symbol}] {reason}")
                return is_allowed
            except:
                # 后备保守策略
                self.logger.error(f"[{self.symbol}] ❌ 增强浮亏检查异常: {e}")
                return False

    def _check_global_directional_risk(self, direction):
        """
        检查全局方向风险
        如果该方向整体处于亏损状态，则禁止建仓
        """
        try:
            if not self.global_risk_manager:
                # 如果全局风险管理器未初始化，允许建仓
                return True

            # 将direction转换为side格式
            side = "LONG" if direction == "long" else "SHORT"

            # 检查该方向是否被阻止
            should_block, reason = self.global_risk_manager.should_block_direction(side)

            if should_block:
                self.logger.warning(
                    f"[{self.symbol}] 🌍 全局方向风险阻止 {direction} 建仓 → {reason}"
                )
                return False

            return True

        except Exception as e:
            # 🔧 使用统一异常处理策略
            try:
                from core.unified_exception_handler import handle_mutual_exclusion_exception
                is_allowed, reason = handle_mutual_exclusion_exception(
                    "check_global_directional_risk", e, self.logger,
                    context={"symbol": self.symbol, "direction": direction}
                )
                self.logger.warning(f"[{self.symbol}] {reason}")
                return is_allowed
            except:
                # 后备策略：全局风险检查异常时允许建仓（避免过度限制）
                self.logger.error(f"[{self.symbol}] ❌ 全局方向风险检查异常: {e}")
                return True

    def _calculate_predicted_peak(self, klines, current_time):
        """计算预测浮盈"""
        try:
            if not klines or len(klines) < 20:
                self.logger.warning(f"[{self.symbol}] ⚠️ K线数据不足({len(klines) if klines else 0}条)，使用保守预测值")
                return 0.15  # 返回低于阈值的值，确保扣分

            features = extract_exit_features(klines, current_time, [])
            if features:
                predicted_peak = predict_peak_profit(features)
                self.logger.info(f"[{self.symbol}] 🎯 预测浮盈 = {predicted_peak:.2f}%")
                return predicted_peak
            else:
                self.logger.warning(f"[{self.symbol}] ⚠️ 无法提取预测特征 → 使用保守预测值")
                return 0.15  # 返回低于阈值的值，确保扣分
        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 预测浮盈计算失败: {e} → 使用保守预测值")
            return 0.15  # 返回低于阈值的值，确保扣分

    def _check_cooldown_and_score(self, side, direction, entry_score, price, klines):
        """检查冷却期和评分"""
        # 获取K线高低点
        lows = [k[3] for k in klines[-20:]]
        highs = [k[2] for k in klines[-20:]]
        pos_pct = (price - min(lows)) / (max(highs) - min(lows) + 1e-9)

        # 🚨 紧急优化：基于交易数据分析结果调整阈值
        # LONG胜率30%, SHORT胜率22% → 需要大幅提高建仓质量
        position_advantage = (direction == "long" and pos_pct < 0.3) or (direction == "short" and pos_pct > 0.7)  # 🔧 收紧位置优势

        # 方向特定阈值：SHORT方向要求更高评分
        if direction == "short":
            high_score = entry_score >= 4.5  # SHORT方向高分阈值
            technical_breakout = entry_score >= 4.0 and position_advantage  # SHORT技术突破门槛
            medium_score_with_trend = entry_score >= 4.2  # SHORT中等评分门槛
        else:  # LONG方向
            high_score = entry_score >= 4.0  # LONG方向高分阈值
            technical_breakout = entry_score >= 3.5 and position_advantage  # LONG技术突破门槛
            medium_score_with_trend = entry_score >= 3.8  # LONG中等评分门槛

        override = high_score or position_advantage or technical_breakout or medium_score_with_trend

        # 🛡️【统一冷却期】使用统一冷却期管理器检查（支持override）
        override_reason = ""
        if override:
            if high_score:
                override_reason = f"高评分{entry_score:.1f}"
            elif medium_score_with_trend:
                override_reason = f"中等评分{entry_score:.1f}"
            elif technical_breakout:
                override_reason = f"技术突破{entry_score:.1f}"
            elif position_advantage:
                override_reason = f"位置优势{pos_pct:.1f}%"

        if not self.cooldown_manager.can_open_position(side, override=override, override_reason=override_reason):
            self.logger.warning(f"[🛡️ 统一冷却期] {self.symbol} {side} 冷却期阻止建仓")
            return False

        # 传统冷却检查（兼容性保留）
        if not self.cooldown_guard.can_open(side.lower(), override=override):
            self.logger.debug(f"[{self.symbol}] 💤 传统冷却中 → 跳过建仓 {side}")
            return False

        # 🔧 紧急保护模式检查（已关闭以增加建仓机会）
        emergency_protection = self.config.get("EMERGENCY_PROTECTION_MODE", False)
        if emergency_protection:
            self.logger.warning(f"[{self.symbol}] 🚨 紧急保护模式启用，禁止新建仓")
            return False

        # 🚨 紧急优化：方向特定的动态阈值调整
        base_dynamic_threshold = self._calculate_dynamic_threshold()

        # 基于交易数据分析结果，SHORT方向需要更高阈值
        if direction == "short":
            direction_threshold = max(base_dynamic_threshold, 4.0)  # SHORT最低4.0
        else:  # LONG方向
            direction_threshold = max(base_dynamic_threshold, 3.5)  # LONG最低3.5

        if entry_score < direction_threshold and not override:
            self.logger.debug(f"[{self.symbol}] ⛔ {direction}方向分数不足 → {entry_score} < {direction_threshold:.1f} (方向阈值)")
            return False

        return True

    def _get_position_multiplier(self, entry_score):
        """🔧 根据评分计算仓位倍数"""
        if entry_score >= 4:
            return 1.0  # 高分信号，正常仓位
        elif entry_score >= 3:
            return 0.8  # 中等信号，80%仓位
        elif entry_score >= 2:
            return 0.5  # 低分信号，50%仓位
        else:
            return 0.3  # 最低仓位

    def _execute_entry(self, side, direction, price, entry_score, reasons, predicted_peak, position_multiplier=1.0):
        """执行建仓操作"""
        try:
            # 1. 优化：只在必要时同步持仓（减少API调用）
            if self._should_sync_before_entry():
                self.trader.sync_position_from_binance(self.symbol, self.tracker)
                self.logger.info(f"[{self.symbol}] ✅ 建仓前持仓已同步")
            else:
                self.logger.debug(f"[{self.symbol}] ⏰ 跳过建仓前同步（10分钟内已同步，API优化）")
        except Exception as e:
            self.logger.warning(f"[{self.symbol}] ❌ Binance 持仓同步失败 → 跳过建仓: {e}")
            return

        # 2. 🌍 简化专业大盘策略检查（新版）
        try:
            from core.simple_professional_analyzer import simple_professional_analyzer

            # 检查简化专业大盘分析是否允许当前方向建仓（传递币种信息）
            allowed, reason = simple_professional_analyzer.should_allow_entry(direction, self.symbol)
            if not allowed:
                self.logger.warning(f"[{self.symbol}] 🌍 专业大盘策略阻止{direction}建仓 → {reason}")
                return
            else:
                # 获取详细的市场分析
                market_analysis = simple_professional_analyzer.get_market_direction()
                self.logger.info(
                    f"[{self.symbol}] ✅ 专业大盘策略允许{direction}建仓 → "
                    f"{market_analysis['direction']} {market_analysis['strength']} "
                    f"(置信度: {market_analysis['confidence']:.2f}) | {reason}"
                )
        except Exception as e:
            self.logger.warning(f"[{self.symbol}] ⚠️ 专业大盘策略检查失败，采用保守策略拒绝建仓: {e}")
            return

        # 3. 🔥 强化方向冲突检测（双重保护）
        # 首先使用增强的方向冲突防护
        is_allowed, conflict_reason = check_direction_conflict(self.symbol, side, self.tracker)
        if not is_allowed:
            self.logger.error(f"[{self.symbol}] 🚨 建仓时方向冲突防护阻止: {conflict_reason}")
            return

        # 然后使用原有的反方向检查作为备用
        positions = self.pm.get_all_positions()
        opposite = "SHORT" if side == "LONG" else "LONG"
        if positions.get(opposite, {}).get("qty", 0) > 0:
            self.logger.error(f"[{self.symbol}] ❌ 已持有反方向仓位 {opposite}，禁止建仓 {side}")
            return

        # 额外使用tracker数据进行第三重检查
        opposite_qty = self.tracker.get_current_position(opposite)
        if opposite_qty > 0:
            self.logger.error(f"[{self.symbol}] ❌ Tracker显示已持有反方向仓位 {opposite}={opposite_qty}，禁止建仓 {side}")
            return

        # 3. 资金控制检查
        if not self._check_funds_for_entry(side, price):
            return

        # 4. 计算建仓数量（应用仓位倍数）
        qty, notional = self._calculate_entry_quantity(side, price, position_multiplier)
        if qty is None or qty <= 0:
            self.logger.error(f"[{self.symbol}] ❌ qty={qty} 不合法 → 跳过建仓")
            return

        # 5. 检查是否已有挂单
        pending_orders = [
            o for o in self.tracker.get_active_orders(side)
            if o.get("entry_pending") and not o.get("closed")
        ]
        if pending_orders:
            self.logger.warning(f"[{self.symbol}] ⏸ 当前已有 {side} 建仓挂单，跳过重复建仓")
            return

        # 6. 执行下单（传递entry_score用于智能策略选择）
        final_order_id = self._place_entry_order(side, direction, qty, price, entry_score)
        if not final_order_id:
            # 🔥 建仓失败，取消方向预留
            cancel_entry_reservation(self.symbol, side)
            self.logger.warning(f"[{self.symbol}] 🚫 建仓失败，已取消{side}方向预留")
            return

        # 7. 计算止盈止损价格
        target_price, stop_price = self._calculate_tp_sl_prices(direction, price)

        # 8. 记录订单
        self._record_entry_order(side, qty, price, final_order_id, reasons, predicted_peak, target_price, stop_price)

        self.logger.info(f"[{self.symbol}] ✅ 建仓完成 | {side} 数量={qty} | 成交价={price:.4f} | 名义={notional:.2f}")

        # 🎯 添加到多空相关性管理器
        try:
            if self.correlation_manager:
                entry_score = sum(reasons.values()) if reasons else 0
                self.correlation_manager.add_position_to_batch(
                    self.symbol, side, price, qty, entry_score
                )
                self.logger.debug(f"[🎯 相关性管理] {self.symbol} {side} 已添加到批次")
        except Exception as e:
            self.logger.warning(f"[{self.symbol}] ❌ 添加到相关性管理器失败: {e}")

        # 🔮 建仓后立即进行预测性止盈挂单 (修复触发逻辑)
        try:
            order_data = {'order_id': final_order_id, 'entry_price': price, 'qty': qty}
            # 修复：使用正确的属性名 self.ptp
            if hasattr(self, 'ptp') and self.ptp and hasattr(self.ptp, 'predictive_manager'):
                success = self.ptp.predictive_manager.predict_and_place_profit_orders(
                    order_data, side, price
                )
                if success:
                    self.logger.info(f"[🔮 预测止盈] {self.symbol} {side} → 预测性止盈挂单已下达")
                else:
                    self.logger.debug(f"[🔮 预测止盈] {self.symbol} {side} → 预测挂单跳过或失败")
        except Exception as e:
            self.logger.warning(f"[{self.symbol}] ❌ 预测止盈挂单失败: {e}")

        publish_order_event("NEW_ORDER", self.symbol)

    def _check_funds_for_entry(self, side, price):
        """检查资金是否足够建仓"""
        try:
            # 获取总资金并计算建仓限制
            total_balance = self.risk.get_total_balance()
            max_total_allow = total_balance * self.config.get("MAX_TOTAL_POSITION_PERCENT", 0.5)
            max_symbol_allow = total_balance * self.config.get("MAX_SYMBOL_RATIO", 0.15)

            # 🔧 修复：使用本地tracker数据估算，避免API调用
            # 检查总账户仓位不得超限
            long_qty = self.tracker.get_total_quantity("LONG")
            short_qty = self.tracker.get_total_quantity("SHORT")
            used_usdt = (long_qty + short_qty) * price

            self.logger.info(
                f"[{self.symbol}] 💰 建仓资金检查 → 已用={used_usdt:.2f} / 限额={max_total_allow:.2f} | 总余额={total_balance:.2f}"
            )

            if used_usdt >= max_total_allow:
                self.logger.warning(f"[{self.symbol}] 🚫 总资金限制超额，禁止建仓")
                return False

            return True
        except Exception as e:
            # 🔧 使用统一异常处理策略
            try:
                from core.unified_exception_handler import handle_mutual_exclusion_exception
                is_allowed, reason = handle_mutual_exclusion_exception(
                    "check_funds_for_entry", e, self.logger,
                    context={"symbol": self.symbol, "side": side}
                )
                self.logger.warning(f"[{self.symbol}] {reason}")
                return is_allowed
            except:
                # 后备保守策略
                self.logger.warning(f"[{self.symbol}] ❌ 资金检查失败: {e}")
                return False

    def _calculate_entry_quantity(self, side, price, position_multiplier=1.0):
        """计算建仓数量"""
        try:
            # 🔧 获取杠杆和动态风险参数
            leverage = self.get_leverage_v2(side)
            risk_params = self._get_dynamic_risk_params(leverage)

            # 🔧 基于动态风险参数计算资金
            base_alloc_ratio = float(self.allocator.get_dynamic_percent())
            leverage_factor = min(leverage / 10, 2.0)
            risk_adjusted_ratio = base_alloc_ratio * leverage_factor * risk_params['position_ratio'] / 0.1  # 标准化到10%基准

            usdt = self.risk.get_trade_amount(risk_adjusted_ratio, asset="USDC")

            # 🔧 应用仓位倍数
            usdt = usdt * position_multiplier

            self.logger.debug(f"[{self.symbol}] 🔧 杠杆{leverage}x | 风险调整={risk_params['position_ratio']:.1%} | 仓位倍数={position_multiplier:.1f} | 最终资金={usdt:.2f}")

            if not usdt or usdt <= 0:
                self.logger.warning(f"[{self.symbol}] ⚠️ 获取建仓资金失败")
                return None, None

            # 限制单笔建仓金额占比
            total_balance = self.risk.get_total_balance()
            max_single_trade_pct = self.config.get("MAX_SINGLE_TRADE_RATIO", 0.1)
            max_single_trade_amt = total_balance * max_single_trade_pct
            if usdt > max_single_trade_amt:
                self.logger.warning(
                    f"[{self.symbol}] 🚫 单笔建仓资金 {usdt:.2f} 超出限制 {max_single_trade_amt:.2f}，自动下调"
                )
                usdt = max_single_trade_amt

            # 币种级控制
            max_symbol_allow = total_balance * self.config.get("MAX_SYMBOL_RATIO", 0.15)
            symbol_used = self.tracker.get_total_notional(self.symbol)
            if symbol_used + usdt > max_symbol_allow:
                self.logger.warning(
                    f"[{self.symbol}] 🚫 币种建仓限制 → 当前已用={symbol_used:.2f}, 计划={usdt:.2f}, 限额={max_symbol_allow:.2f}"
                )
                return None, None

            # 计算数量
            MIN_NOTIONAL = self.config.get("MIN_NOTIONAL", 15)
            qty = get_trade_quantity(
                symbol=self.symbol,
                usdt_amount=usdt,
                price=price,
                precision_map=SYMBOL_QUANTITY_PRECISION
            )
            notional = qty * price

            if notional < MIN_NOTIONAL:
                self.logger.warning(f"[{self.symbol}] ⚠️ 名义金额={notional:.2f} < 最小限额 → 使用 {MIN_NOTIONAL} USDT 重算")
                usdt = MIN_NOTIONAL
                qty = get_trade_quantity(
                    symbol=self.symbol,
                    usdt_amount=usdt,
                    price=price,
                    precision_map=SYMBOL_QUANTITY_PRECISION
                )
                notional = qty * price

            return qty, notional
        except Exception as e:
            self.logger.warning(f"[{self.symbol}] ❌ 计算建仓数量失败: {e}")
            return None, None

    def _place_entry_order(self, side, direction, qty, price, entry_score=0):
        """🔧 智能建仓执行 - 根据信号强度选择最优策略"""

        # 🔥 严格方向冲突检查 - 建仓前最后检查
        try:
            from core.direction_conflict_guard import check_direction_conflict
            is_allowed, conflict_reason = check_direction_conflict(self.symbol, side, self.tracker)
            if not is_allowed:
                self.logger.error(f"[{self.symbol}] 🚨 建仓前严格检查阻止: {conflict_reason}")
                return None
        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 严格方向检查异常: {e}")
            return None

        try:
            # 🔧 关键修复：检查是否已有相同订单（防止重复下单）
            if self._has_duplicate_pending_order(side, direction, qty):
                self.logger.warning(f"[{self.symbol}] 🚫 检测到重复订单，跳过建仓: {side} {qty}")
                return None

            # 🔧 智能价格源选择
            price = self._get_optimal_entry_price(direction)
            if price is not None:
                self.market.update_price(self.symbol, price)

            # 🔧 新策略：所有建仓都使用限价单（根据用户要求）
            self.logger.info(f"[{self.symbol}] 🎯 信号{entry_score}分，使用限价单建仓")
            return self._execute_limit_entry_only(side, direction, qty, price)
        except Exception as e:
            self.logger.warning(f"[{self.symbol}] ❌ 下单失败: {e}")
            return None

    def _execute_limit_entry_only(self, side, direction, qty, price):
        """🔧 执行限价建仓 - 新策略：只使用限价单"""
        try:
            order_side = "BUY" if direction == "long" else "SELL"

            # 计算有利的限价单价格（提高成交率）
            if direction == "long":
                # 做多时，价格稍微高一点确保成交
                limit_price = price * 1.0002  # 高0.02%
            else:
                # 做空时，价格稍微低一点确保成交
                limit_price = price * 0.9998  # 低0.02%

            # 调整价格精度
            limit_price = self.trader.adjust_price(self.symbol, limit_price)

            order_id = self.trader.place_limit_order(
                symbol=self.symbol,
                side=order_side,
                qty=qty,
                price=limit_price,
                position_side=side
            )

            if order_id:
                self.logger.info(f"[{self.symbol}] ✅ 限价建仓: {side} {qty} @ {limit_price} (订单ID: {order_id})")

            return order_id

        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 限价建仓失败: {e}")
            return None

    def _execute_market_entry(self, side, direction, qty, price):
        """🔧 执行市价建仓 - 确保成交"""
        try:
            order_side = "BUY" if direction == "long" else "SELL"

            order_id = self.trader.place_market_order(
                symbol=self.symbol,
                side=order_side,
                qty=qty,
                purpose="ENTRY"  # 明确标识为建仓操作
            )

            if order_id:
                self.logger.info(f"[{self.symbol}] ⚡ 市价建仓: {side} {qty} @ 市价")

            return order_id

        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 市价建仓失败: {e}")
            return None

    def _execute_smart_limit_entry(self, side, direction, qty, price):
        """🔥 执行智能限价建仓 - 快速响应自动转市价"""
        try:
            # 计算更激进的挂单价格（1个tick，提高成交率）
            tick_str = SYMBOL_TICK_SIZE.get(self.symbol, "0.0001")
            tick = Decimal(tick_str)
            price_decimal = Decimal(str(price))

            # 使用1个tick的价差，平衡成交率和价格优势
            if direction == "long":
                limit_price = float((price_decimal + tick * 1).quantize(tick, rounding=ROUND_DOWN))
                order_side = "BUY"
            else:
                limit_price = float((price_decimal - tick * 1).quantize(tick, rounding=ROUND_DOWN))
                order_side = "SELL"

            # 🔥 集成API频率限制的下单
            order_id = self._place_limit_order_with_rate_limit(order_side, qty, limit_price)

            if not order_id:
                self.logger.warning(f"[{self.symbol}] ❌ 限价单下单失败，直接市价建仓")
                return self._execute_market_entry(side, direction, qty, price)

            self.logger.info(f"[{self.symbol}] 🎯 智能限价建仓: {side} {qty} @ {limit_price}")

            # 🔥 添加到挂单监控系统
            try:
                monitor = get_pending_order_monitor(self.trader, self.logger)
                if monitor:
                    monitor.add_pending_order(
                        symbol=self.symbol,
                        order_id=order_id,
                        side=order_side,
                        qty=qty,
                        limit_price=limit_price,
                        order_type="ENTRY"
                    )
                    self.logger.debug(f"[{self.symbol}] 📋 已添加到挂单监控: {order_id}")
            except Exception as e:
                self.logger.warning(f"[{self.symbol}] ❌ 添加挂单监控失败: {e}")

            # 🔧 优化检查订单状态（5秒、10秒、20秒）- 符合20秒超时要求
            check_intervals = [5, 10, 20]  # 修改为20秒超时

            for i, wait_time in enumerate(check_intervals):
                import time
                time.sleep(wait_time if i == 0 else check_intervals[i] - check_intervals[i-1])

                # 🔥 集成API频率限制的状态检查
                status = self._check_order_status_with_rate_limit(order_id)

                if status == "FILLED":
                    self.logger.info(f"[{self.symbol}] ✅ 限价单已成交: {side} {qty} (等待{wait_time}秒)")

                    # 🔥 从挂单监控中移除
                    try:
                        monitor = get_pending_order_monitor()
                        if monitor:
                            monitor.remove_pending_order(order_id, "FILLED")
                    except Exception as e:
                        self.logger.warning(f"[{self.symbol}] ❌ 移除挂单监控失败: {e}")

                    return order_id
                elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                    self.logger.warning(f"[{self.symbol}] ❌ 限价单失效: {status}")
                    break
                else:
                    self.logger.debug(f"[{self.symbol}] ⏳ 限价单{wait_time}秒未成交，状态: {status}")

            # 🔧 20秒未成交，转为市价单确保建仓
            self.logger.warning(f"[{self.symbol}] ⏰ 限价单20秒未成交，转市价单确保建仓")

            # 🔥 集成API频率限制的撤单
            cancel_success = self._cancel_order_with_rate_limit(order_id)
            if cancel_success:
                time.sleep(1.5)  # 🔧 增加等待时间，避免连续API调用

            # 🔥 集成API频率限制的市价单
            market_order_id = self._place_market_order_with_rate_limit(order_side, qty)

            if market_order_id:
                self.logger.info(f"[{self.symbol}] ⚡ 市价补单成功: {side} {qty}")
                return market_order_id
            else:
                self.logger.error(f"[{self.symbol}] ❌ 市价补单失败")
                return order_id  # 返回原订单ID作为备用

        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 智能限价建仓异常: {e}")
            # 异常情况直接市价建仓
            return self._execute_market_entry(side, direction, qty, price)

    def _place_limit_order_with_rate_limit(self, order_side, qty, limit_price):
        """🔥 集成API频率限制的限价下单"""
        try:
            from core.global_api_rate_limiter import get_global_rate_limiter, APIRequestType, APIRequestPriority

            limiter = get_global_rate_limiter()
            can_request, reason = limiter.can_make_request(
                APIRequestType.PLACE_ORDER,
                APIRequestPriority.HIGH  # 建仓使用高优先级
            )

            if not can_request:
                # 建仓是重要操作，尝试等待槽位
                if limiter.wait_for_request_slot(APIRequestType.PLACE_ORDER, APIRequestPriority.HIGH, 5.0):
                    can_request = True
                else:
                    self.logger.warning(f"[{self.symbol}] 下单API限制且等待超时: {reason}")
                    return None

            order_id = self.trader.place_limit_order(
                symbol=self.symbol,
                side=order_side,
                qty=qty,
                price=limit_price
            )

            # 记录API调用
            limiter.record_request(APIRequestType.PLACE_ORDER, order_id is not None)

            return order_id

        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 限价下单API调用失败: {e}")
            return None

    def _check_order_status_with_rate_limit(self, order_id):
        """🔥 集成API频率限制的订单状态检查"""
        try:
            from core.global_api_rate_limiter import get_global_rate_limiter, APIRequestType, APIRequestPriority

            limiter = get_global_rate_limiter()
            can_request, reason = limiter.can_make_request(
                APIRequestType.GET_ORDER_STATUS,
                APIRequestPriority.MEDIUM
            )

            if not can_request:
                self.logger.debug(f"[{self.symbol}] 订单状态API限制: {reason}")
                return "UNKNOWN"  # 返回未知状态，继续等待

            status = self.trader.get_order_status(self.symbol, order_id)

            # 记录API调用
            limiter.record_request(APIRequestType.GET_ORDER_STATUS, True)

            return status

        except Exception as e:
            self.logger.warning(f"[{self.symbol}] ❌ 订单状态检查失败: {e}")
            return "ERROR"

    def _cancel_order_with_rate_limit(self, order_id):
        """🔥 集成API频率限制的撤单操作"""
        try:
            from core.global_api_rate_limiter import get_global_rate_limiter, APIRequestType, APIRequestPriority

            limiter = get_global_rate_limiter()
            can_request, reason = limiter.can_make_request(
                APIRequestType.CANCEL_ORDER,
                APIRequestPriority.HIGH  # 撤单使用高优先级
            )

            if not can_request:
                # 撤单是重要操作，尝试等待槽位
                if limiter.wait_for_request_slot(APIRequestType.CANCEL_ORDER, APIRequestPriority.HIGH, 3.0):
                    can_request = True
                else:
                    self.logger.warning(f"[{self.symbol}] 撤单API限制且等待超时: {reason}")
                    return False

            success = self.trader.cancel_order(self.symbol, order_id)

            # 记录API调用
            limiter.record_request(APIRequestType.CANCEL_ORDER, success)

            return success

        except Exception as e:
            self.logger.warning(f"[{self.symbol}] ❌ 撤单API调用失败: {e}")
            return False

    def _place_market_order_with_rate_limit(self, order_side, qty):
        """🔥 集成API频率限制的市价下单 - 建仓专用强化版"""
        try:
            from core.global_api_rate_limiter import get_global_rate_limiter, APIRequestType, APIRequestPriority

            # 🚨 建仓前检查是否已有持仓，避免重复建仓
            side = "LONG" if order_side == "BUY" else "SHORT"
            current_position = self.get_position_amt_unified(side)
            if current_position > 0:
                self.logger.warning(f"[{self.symbol}] 已有{side}持仓{current_position}，跳过市价补单")
                return None

            limiter = get_global_rate_limiter()
            can_request, reason = limiter.can_make_request(
                APIRequestType.PLACE_ORDER,
                APIRequestPriority.CRITICAL  # 市价补单使用最高优先级
            )

            if not can_request:
                # 🚨 建仓市价单强制绕过机制 - 确保100%建仓
                self.logger.warning(f"[{self.symbol}] 🚨 建仓市价单强制绕过API限制: {reason}")

                # 尝试等待槽位
                if limiter.wait_for_request_slot(APIRequestType.PLACE_ORDER, APIRequestPriority.CRITICAL, 10.0):
                    can_request = True
                else:
                    # 🔥 最后手段：强制执行建仓，绕过所有限制
                    self.logger.error(f"[{self.symbol}] ⚡ 建仓紧急模式：强制执行市价单")
                    try:
                        order_id = self.trader.place_market_order(
                            symbol=self.symbol,
                            side=order_side,
                            qty=qty
                        )
                        if order_id:
                            self.logger.info(f"[{self.symbol}] 🚨 紧急建仓成功: {order_side} {qty}")
                        return order_id
                    except Exception as e:
                        self.logger.error(f"[{self.symbol}] ❌ 紧急建仓失败: {e}")
                        return None

            order_id = self.trader.place_market_order(
                symbol=self.symbol,
                side=order_side,
                qty=qty
            )

            # 记录API调用
            limiter.record_request(APIRequestType.PLACE_ORDER, order_id is not None)

            return order_id

        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 市价补单API调用失败: {e}")
            return None

        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 智能限价建仓失败: {e}")
            return None

    def _has_duplicate_pending_order(self, side, direction, qty):
        """🔧 检查是否有重复的待成交订单"""
        try:
            current_time = timestamp()

            # 检查最近30秒内是否有相同的订单
            active_orders = self.tracker.get_active_orders(side)

            for order in active_orders:
                if order.get("closed", False):
                    continue

                # 检查订单时间（30秒内）
                submit_time = order.get("submit_time", 0)
                if current_time - submit_time > 30:
                    continue

                # 检查订单数量是否相同
                order_qty = order.get("qty", 0)
                if abs(order_qty - qty) < 0.001:  # 数量基本相同
                    self.logger.warning(
                        f"[{self.symbol}] 🔍 发现重复订单: {side} {qty} "
                        f"(已有订单: {order_qty}, {current_time - submit_time:.1f}秒前)"
                    )
                    return True

            return False

        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 重复订单检查失败: {e}")
            return False  # 出错时允许下单

    def _get_optimal_entry_price(self, direction):
        """🔧 智能价格源选择 - 根据市场条件选择最优价格"""
        try:
            # 获取WebSocket实时价格（最新成交价）
            websocket_price = self.market.get_last_price(self.symbol)

            # 获取盘口价格（买卖价）
            bid_ask = self.market.get_bid_ask(self.symbol)
            bid_price = bid_ask.get('bid')
            ask_price = bid_ask.get('ask')

            # 如果没有盘口价格，使用WebSocket价格
            if not bid_price or not ask_price:
                self.logger.debug(f"[{self.symbol}] 📊 无盘口价格，使用WebSocket价格: {websocket_price}")
                return websocket_price

            # 计算价差
            spread = (ask_price - bid_price) / bid_price * 100

            # 根据价差和方向选择最优价格
            if spread < 0.02:  # 价差小于0.02%，使用盘口价格
                optimal_price = ask_price if direction == "long" else bid_price
                self.logger.debug(f"[{self.symbol}] 📊 小价差{spread:.3f}%，使用盘口价格: {optimal_price}")
            else:  # 价差较大，使用WebSocket价格
                optimal_price = websocket_price
                self.logger.debug(f"[{self.symbol}] 📊 大价差{spread:.3f}%，使用WebSocket价格: {optimal_price}")

            return optimal_price

        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 智能价格选择失败: {e}")
            # 回退到WebSocket价格
            return self.market.get_last_price(self.symbol)

    def _calculate_dynamic_threshold(self):
        """🌍 国际顶级策略 - 增强智能动态阈值 (基于D.E. Shaw & Millennium)"""
        try:
            # 更新市场分析器数据
            current_price = self.market.get_last_price(self.symbol)
            if current_price and hasattr(self, '_last_price_for_analyzer'):
                recent_return = (current_price - self._last_price_for_analyzer) / self._last_price_for_analyzer
                self.market_analyzer.analyze_market_conditions(current_price, None, recent_return)
            else:
                self.market_analyzer.analyze_market_conditions(current_price)

            self._last_price_for_analyzer = current_price

            # 使用增强市场分析器计算阈值
            enhanced_threshold, details = self.market_analyzer.get_enhanced_threshold_adjustment()

            self.logger.debug(
                f"[{self.symbol}] 🌍 增强动态阈值: {enhanced_threshold:.1f} "
                f"(基础{details.get('base_threshold', 3.5)} + 时间{details.get('time_adjustment', 0):.1f} + "
                f"表现{details.get('performance_adjustment', 0):.1f} + 波动{details.get('volatility_adjustment', 0):.1f} + "
                f"市场状态{details.get('market_state_adjustment', 0):.1f})"
            )

            return enhanced_threshold

        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 增强动态阈值计算失败: {e}")
            return 3.0  # 🔧 优化：降低回退阈值从3.5到3.0，增加建仓机会



    def _calculate_tp_sl_prices(self, direction, price):
        """计算止盈止损价格"""
        target_pct = self.config.get("TP_RATIO", 0.03)
        stop_loss_pct = -0.005
        target_price = price * (1 + target_pct) if direction == "long" else price * (1 - target_pct)
        stop_price = price * (1 + stop_loss_pct) if direction == "long" else price * (1 - stop_loss_pct)

        tick_str = SYMBOL_TICK_SIZE.get(self.symbol, "0.0001")
        tick = Decimal(tick_str)
        target_price = float(Decimal(str(target_price)).quantize(tick, rounding=ROUND_DOWN))
        stop_price = float(Decimal(str(stop_price)).quantize(tick, rounding=ROUND_DOWN))

        return target_price, stop_price

    def _record_entry_order(self, side, qty, price, final_order_id, reasons, predicted_peak, target_price, stop_price):
        """🔧 强化建仓记录逻辑 - 确保所有建仓都被正确跟踪"""
        try:
            if final_order_id:
                self.logger.info(f"[{self.symbol}] 📝 开始记录建仓订单: {side} {qty} @ {price} (订单ID: {final_order_id})")

                stop_loss_pct = -0.005

                # 🔧 关键修复：添加详细的记录过程日志
                order_data = {
                    "entry_time": timestamp(),
                    "pnl_curve": [],
                    "peak_profit": 0.0,
                    "target_tp_pct": 0.01,
                    "predicted_peak": round(predicted_peak, 4),
                    "target": round(target_price, 6),
                    "target_sl_pct": round(stop_loss_pct * 100, 4),
                    "stop_target": round(stop_price, 6),
                    "submit_time": timestamp(),
                    "entry_pending": False,
                    "entry_reason": str(reasons)
                }

                # 执行tracker.add_order
                self.tracker.add_order(
                    side=side, qty=qty, entry_price=price,
                    score_detail={"source": "strategy_build"},
                    order_id=final_order_id,
                    extra_fields=order_data
                )

                # 🔥 确认建仓成功，将预留转为正式记录
                confirm_entry_success(self.symbol, side)
                self.logger.info(f"[{self.symbol}] 🛡️ 已确认{side}方向建仓成功")

                # 更新浮动盈亏
                self.tracker.update_floating_pnl(side, price)

                # 🔧 关键修复：立即保存状态
                self.tracker.save_state()

                # 🔧 关键修复：验证记录是否成功
                if self._verify_entry_recorded(side, final_order_id):
                    self.logger.info(f"[{self.symbol}] ✅ 建仓记录成功: {side} {qty} (订单ID: {final_order_id})")
                else:
                    self.logger.error(f"[{self.symbol}] ❌ 建仓记录验证失败: {side} {qty} (订单ID: {final_order_id})")
                    # 重试记录
                    self._retry_entry_record(side, qty, price, final_order_id, order_data)

            else:
                self.logger.error(f"[{self.symbol}] ❌ 建仓失败：无有效订单号，未入 tracker")
                # 清理失败的pending订单
                for o in self.tracker.get_active_orders(side):
                    if not o.get("closed") and o.get("entry_pending"):
                        o["closed"] = True
                        o["exit_status"] = "FAILED"
                self.tracker.save_state()

        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 记录建仓订单异常: {e}")
            # 异常情况下也要尝试记录
            if final_order_id:
                self._emergency_entry_record(side, qty, price, final_order_id, reasons)

    def _verify_entry_recorded(self, side, order_id):
        """🔧 验证建仓是否被正确记录"""
        try:
            active_orders = self.tracker.get_active_orders(side)
            for order in active_orders:
                if str(order.get('order_id', '')) == str(order_id):
                    return True
            return False
        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 验证建仓记录失败: {e}")
            return False

    def _retry_entry_record(self, side, qty, price, order_id, order_data):
        """🔧 重试建仓记录"""
        try:
            self.logger.warning(f"[{self.symbol}] 🔄 重试记录建仓: {side} {qty}")

            # 重新执行add_order
            self.tracker.add_order(
                side=side, qty=qty, entry_price=price,
                score_detail={"source": "strategy_build_retry"},
                order_id=order_id,
                extra_fields=order_data
            )

            # 🔥 确认建仓成功，将预留转为正式记录
            confirm_entry_success(self.symbol, side)
            self.logger.info(f"[{self.symbol}] 🛡️ 重试后已确认{side}方向建仓成功")

            # 强制保存
            self.tracker.save_state()

            # 再次验证
            if self._verify_entry_recorded(side, order_id):
                self.logger.info(f"[{self.symbol}] ✅ 重试记录成功: {side} {qty}")
            else:
                self.logger.error(f"[{self.symbol}] ❌ 重试记录仍然失败: {side} {qty}")

        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 重试记录异常: {e}")

    def _emergency_entry_record(self, side, qty, price, order_id, reasons):
        """🔧 紧急建仓记录 - 最简化版本"""
        try:
            self.logger.warning(f"[{self.symbol}] 🚨 紧急记录建仓: {side} {qty}")

            # 最简化的记录
            self.tracker.add_order(
                side=side, qty=qty, entry_price=price,
                score_detail={"source": "emergency_record"},
                order_id=order_id,
                extra_fields={
                    "entry_time": timestamp(),
                    "submit_time": timestamp(),
                    "entry_pending": False,
                    "entry_reason": str(reasons),
                    "emergency_record": True
                }
            )

            # 🔥 确认建仓成功，将预留转为正式记录
            confirm_entry_success(self.symbol, side)
            self.logger.info(f"[{self.symbol}] 🛡️ 紧急记录后已确认{side}方向建仓成功")

            self.tracker.save_state()
            self.logger.info(f"[{self.symbol}] 🚨 紧急记录完成: {side} {qty}")

        except Exception as e:
            self.logger.error(f"[{self.symbol}] ❌ 紧急记录也失败: {e}")

    def check_exit_only(self, price=None):
        """
        ✅ 默认 check_exit_only → 空函数（无止盈功能的币也不会报错）
        ✅ 如果有 self.ptp（PartialExit）模块，就执行止盈逻辑
        """
        if not hasattr(self, "ptp") or self.ptp is None:
            self.logger.debug(f"[{self.symbol}] ❎ check_exit_only 无效（当前无止盈模块）")
            return

        processed_sides = []

        # 检查并处理LONG持仓
        long_qty = self.tracker.get_total_quantity("LONG")
        if long_qty > 0:
            self.logger.debug(f"[{self.symbol}] 🔍 检查LONG止盈止损 (数量: {long_qty})")
            self.ptp.check_orders("LONG")
            processed_sides.append("LONG")

        # 检查并处理SHORT持仓
        short_qty = self.tracker.get_total_quantity("SHORT")
        if short_qty > 0:
            self.logger.debug(f"[{self.symbol}] 🔍 检查SHORT止盈止损 (数量: {short_qty})")
            self.ptp.check_orders("SHORT")
            processed_sides.append("SHORT")

        # 输出处理结果摘要
        if processed_sides:
            sides_str = " + ".join(processed_sides)
            self.logger.info(f"[{self.symbol}] ✅ 止盈止损检查完成: {sides_str}")
        else:
            self.logger.debug(f"[{self.symbol}] ⚪ 无持仓，跳过止盈止损检查")
from core.shared_market import market
import json
import os
from core.strategy_core import StrategyCore
import pandas as pd

class Advisor:
    def __init__(self, recommendations_path="virtual_data/recommendations.json", match_threshold=2):
        self.recommendations_path = recommendations_path
        self.match_threshold = match_threshold
        self.strategy = StrategyCore()

    def load_recommendations(self):
        if not os.path.exists(self.recommendations_path):
            return []
        with open(self.recommendations_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def get_current_snapshot(self):
        raw = self.market.get_klines(limit=50)
        df = pd.DataFrame(raw, columns=['open_time', 'open', 'high', 'low', 'close', 'volume'])
        df['close_time'] = df['open_time']
        self.strategy.evaluate_strategy(df)
        return self.strategy.snapshot

    def match_snapshot(self, current_snapshot, recommendations):
        for rec in recommendations:
            match_count = 0
            rec_snap = rec["snapshot"]

            for key in current_snapshot:
                v1 = round(current_snapshot.get(key, 0), 3)
                v2 = round(rec_snap.get(key, 0), 3)
                if abs(v1 - v2) < 0.001:
                    match_count += 1

            if match_count >= self.match_threshold:
                return True, rec  # ✅ 匹配成功

        return False, None

    def check_entry_recommendation(self):
        current_snapshot = self.get_current_snapshot()
        recommendations = self.load_recommendations()
        matched, best_rec = self.match_snapshot(current_snapshot, recommendations)
        if matched:
            print(f"📢 发现匹配策略组合，建议立即建仓 → Snapshot: {best_rec['snapshot']} AvgPnL: {best_rec['avg_pnl']}%")
        return matched, best_rec

if __name__ == "__main__":
    advisor = Advisor()
    matched, _ = advisor.check_entry_recommendation()
    if matched:
        print("🚨 发出建仓建议信号 ✅")
    else:
        print("🔍 当前策略与推荐组合无匹配 🚫")

import json
import time
import pandas as pd
import os
from collections import defaultdict

class RecommendationAnalyzer:
    def __init__(self, input_file="virtual_data/virtual_builds.jsonl", output_file="virtual_data/recommendations.json"):
        self.input_file = input_file
        self.output_file = output_file

    def load_virtual_trades(self):
        if not os.path.exists(self.input_file):
            return []

        trades = []
        with open(self.input_file, "r", encoding="utf-8") as f:
            for line in f:
                trades.append(json.loads(line.strip()))
        return trades

    def flatten_snapshot(self, snapshot):
        """将策略快照展平成可哈希的元组键"""
        return tuple(sorted((k, round(v, 3)) for k, v in snapshot.items()))

    def analyze_top_clusters(self, trades, top_percent=25):
        if not trades:
            print("⚠️ 无虚拟交易记录，无法分析")
            return []

        df = pd.DataFrame(trades)
        df = df[df["pnl"] > 0]  # ✅ 只保留盈利交易
        df = df.sort_values("pnl", ascending=False).reset_index(drop=True)

        if df.empty:
            print("⚠️ 没有盈利交易可用于分析")
            return []

        # 在盈利交易中取前25%
        top_n = max(1, int(len(df) * top_percent / 100))
        top_df = df.iloc[:top_n]

        cluster_dict = defaultdict(list)
        for _, row in top_df.iterrows():
            key = self.flatten_snapshot(row["strategy_snapshot"])
            cluster_dict[key].append(row)

        # 按平均 pnl 排序，保留每个组合的平均值 + 最新信号快照 + 推荐方向
        ranked_clusters = []
        for snapshot_key, rows in cluster_dict.items():
            avg_pnl = sum(r["pnl"] for r in rows) / len(rows)
            direction = rows[0]["side"] if "side" in rows[0] else "unknown"
            ranked_clusters.append({
                "snapshot": dict(snapshot_key),
                "avg_pnl": round(avg_pnl, 4),
                "count": len(rows),
                "direction": direction  # ✅ 加入推荐方向字段
            })

        ranked_clusters.sort(key=lambda x: x["avg_pnl"], reverse=True)
        return ranked_clusters

    def write_recommendations(self, top_clusters):
        with open(self.output_file, "w", encoding="utf-8") as f:
            json.dump(top_clusters, f, indent=2, ensure_ascii=False)
        print(f"✅ 已保存前 {len(top_clusters)} 个推荐策略组合 → {self.output_file}")

    def run(self):
        trades = self.load_virtual_trades()
        clusters = self.analyze_top_clusters(trades)
        self.write_recommendations(clusters)

if __name__ == "__main__":
    matcher = RecommendationAnalyzer()
    while True:
        matcher.run()
        time.sleep(600)  # 每10分钟运行一次

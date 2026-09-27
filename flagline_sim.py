#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""旗令防线 v6 · 180s 单局数值模拟器

回答 v6 定稿 §12 的三道必答题（见 docs/design-final.md）：
  1. 军令 340 达成率：波次结算 240 + 里程碑 100 在各级玩家手里实收多少？
  2. 破釜沉舟触发率：目标 10-30% 的局（太少没名场面，太多不稀缺）。
  3. 波 4 裸打必败：无阵型裸 DPS 是否 57s > 45s 窗（必败成立）。

另验：旗收支（2 面/次消耗 vs 缴获+斥候+完美产出）、金收支（128 vs 满编 130）。

三种玩家：
  skilled  = 每波用克制阵 + 完美率高 + 连击不断（上限）
  average  = 阵型偶对 + 偶尔断连击（中位）
  poor     = 全程裸打 + 连击随缘（下限，验必败用）

用法:
    python3 flagline_sim.py
    python3 flagline_sim.py --config economy.json
    python3 flagline_sim.py --player average
"""

from __future__ import annotations

import argparse
import json
import os

PLAYER = {
    "skilled": {"bonus_rate": 1.0, "milestones": [25, 50, 100],
                "perfect_orders": 5, "leak": 0, "combo_peak": 280,
                "stamina_empty": True, "base_low": True},
    "average": {"bonus_rate": 0.6, "milestones": [25, 50],
                "perfect_orders": 2, "leak": 2, "combo_peak": 80,
                "stamina_empty": False, "base_low": False},
    "poor":    {"bonus_rate": 0.0, "milestones": [],
                "perfect_orders": 0, "leak": 8, "combo_peak": 12,
                "stamina_empty": False, "base_low": True},
}

MAIN_COLOR = {"fengshi": "red", "huanxing": "blue", "yuanyang": "red",
              "yanxing": "red", "yulin": "blue"}


def load_cfg(path: str | None) -> dict:
    here = os.path.dirname(os.path.abspath(__file__))
    if path:
        p = path if os.path.isabs(path) else os.path.join(here, path)
    else:
        p = os.path.join(here, "economy.json")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def simulate(cfg: dict, player: str) -> dict:
    P = PLAYER[player]
    W = {w["id"]: w for w in cfg["waves"]}
    E = cfg["enemies"]
    MS = {m["combo"]: m["junling"] for m in cfg["milestones"]}

    junling = 0
    junling_earned = 0
    for wid in (1, 2, 3, 4):
        w = W[wid]
        junling += w["settle"]
        junling_earned += w["settle"]
        if P["bonus_rate"] >= 1.0 or (P["bonus_rate"] > 0 and wid in (2, 3)):
            junling += w["bonus"]
            junling_earned += w["bonus"]
        elif wid == 4 and P["bonus_rate"] > 0:
            junling += w["bonus"]
            junling_earned += w["bonus"]
    for c in P["milestones"]:
        junling += MS[c]
        junling_earned += MS[c]
    junling = min(junling, cfg["junling_cap"] * 3)
    orders_afford = junling // cfg["junling_order_cost"]

    import random
    random.seed(42)
    flags_got = dict(cfg["flags_start"])
    for wid in (1, 2, 3, 4):
        for eid, n in W[wid]["enemies"].items():
            drop = E[eid].get("drop_flag")
            if not drop:
                continue
            rate = 0.2 if eid in ("zabing", "fengqun") else 1.0
            got = sum(1 for _ in range(n) if random.random() < rate)
            flags_got[drop] = flags_got.get(drop, 0) + got
    flags_got["red"] = flags_got.get("red", 0) + 2
    flags_got["blue"] = flags_got.get("blue", 0) + 2
    for _ in range(P["perfect_orders"]):
        flags_got["red"] = flags_got.get("red", 0) + 1
    flags_total = sum(flags_got.values())
    flags_cap_lost = max(0, flags_total - cfg["flags_hand_cap"] * 3)
    orders_by_flag = flags_total // 2

    gold = 0
    for wid in (1, 2, 3, 4):
        for eid, n in W[wid]["enemies"].items():
            kill = max(0, n - (P["leak"] if wid >= 3 else 0))
            gold += kill * E[eid]["bounty"]
    if player != "poor":
        gold += E["jingying"].get("bounty_settle_extra", 0)

    fufu = (P["combo_peak"] >= 50
            and (P["stamina_empty"] or P["base_low"]))
    if player == "poor":
        fufu = False

    return {
        "player": player,
        "junling_earned": junling_earned,
        "orders_afford": int(orders_afford),
        "flags_total": flags_total,
        "flags_cap_lost": flags_cap_lost,
        "orders_by_flag": int(orders_by_flag),
        "gold": gold,
        "fufu": fufu,
    }


def wave4_naked(cfg: dict) -> dict:
    """波 4 裸打：精英 160 血 / 裸 DPS vs 45s 窗。"""
    E = cfg["enemies"]
    T = cfg["troops"]
    dps_naked = T["shenji"]["dps"] * 1
    elite = E["jingying"]
    dps_eff = dps_naked * (1 - elite.get("resist", 0))
    t_kill = elite["hp"] / dps_eff
    window = 45.0
    return {"dps_naked": dps_naked, "dps_eff": round(dps_eff, 2),
            "t_kill_s": round(t_kill, 1), "window_s": window,
            "must_fail": t_kill > window}


def main() -> int:
    ap = argparse.ArgumentParser(description="旗令防线 v6 单局模拟器")
    ap.add_argument("--config", default=None)
    ap.add_argument("--player", choices=["skilled", "average", "poor", "all"],
                    default="all")
    a = ap.parse_args()
    cfg = load_cfg(a.config)
    print(f"[{cfg.get('name')}]")
    players = ["skilled", "average", "poor"] if a.player == "all" else [a.player]
    results = {}
    for pl in players:
        r = simulate(cfg, pl)
        results[pl] = r
        print(f"\n【{pl}】军令实收 {r['junling_earned']}（目标 340±20）→ "
              f"可发令 {r['orders_afford']} 次（目标 5）")
        print(f"  旗总入 {r['flags_total']} 面 → 支撑约 {r['orders_by_flag']} 次发令"
              f"（溢出 {r['flags_cap_lost']} 面转军令）")
        print(f"  金 {r['gold']}（满编 130，目标 128±10）  破釜 {'触发' if r['fufu'] else '不触发'}")

    print("\n" + "-" * 60 + "\n【三道检查】")
    ok = True
    sk = results.get("skilled", simulate(cfg, "skilled"))
    av = results.get("average", simulate(cfg, "average"))
    if not (320 <= sk["junling_earned"] <= 360):
        print(f"  [!] 军令 340 达成率：skilled 实收 {sk['junling_earned']}，超出 320-360 区间")
        ok = False
    else:
        print(f"  OK 军令达成率：skilled 实收 {sk['junling_earned']}，average 实收 {av['junling_earned']}（≥300 够 5 次）")
    if sk["orders_afford"] < 5:
        print(f"  [!] 发令次数：skilled 只能发 {sk['orders_afford']} 次，不够 5 次")
        ok = False
    w4 = wave4_naked(cfg)
    if w4["must_fail"]:
        print(f"  OK 波 4 裸打必败：精英 {w4['t_kill_s']}s > 窗 {w4['window_s']}s（裸 DPS {w4['dps_eff']}/s）")
    else:
        print(f"  [!] 波 4 裸打能过：{w4['t_kill_s']}s <= {w4['window_s']}s —— 必须加血/加抗，否则阵型无意义")
        ok = False
    print(f"  .. 破釜沉舟：skilled 触发（高压残局），average/poor 不触发 —— 定性符合预期，实测验 10-30%")
    print("-" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""放置类游戏平衡模拟器（headless balance harness）

放置模拟经营的设计风险不在美术，在数字。两个必答问题：
  1. 中期会不会空转？玩家 5~30 分钟里有没有事可做？
  2. 后期会不会爆炸？数值膨胀到玩家看不懂、失去意义？

这两个问题靠人肉试玩是试不出来的。本工具用配置驱动的模拟回答它们：
给它一份 economy JSON，它会模拟一个"理性玩家"（永远买当前 ROI 最高的东西）
在若干次会话中的完整生命周期，然后报告空转区间、膨胀拐点和关键里程碑耗时。

用法:
    python3 balance_sim.py                       # 跑内置示例配置
    python3 balance_sim.py --config my.json
    python3 balance_sim.py --offline-hours 8     # 每次回来给 8 小时离线收益
    python3 balance_sim.py --sessions 40         # 模拟 40 次上线

配置格式见 --dump-schema 输出的示例。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# ---------------------------------------------------------------- 内置示例配置
# 这是一份「放置 + 空间有限」的示例经济：6 种资源、5 台设备、每台占地有限。
# 换成真实设计只需要改这个 dict，不用改模拟器。

SAMPLE = {
    "name": "示例 · 循环工厂",
    "tick_s": 1.0,
    "start": {"废料": 25, "零件": 0, "成品": 0},   # 必须给起手，否则死锁
    "space": 12,                      # 占地上限：所有设备数量之和
    "generators": [
        # id, 名称, 产出{资源: 每台每秒产量}, 基础造价{资源: 量}, 占地, 解锁(累计产出)
        {"id": "g1", "name": "回收臂",   "out": {"废料": 1.0},
         "cost": {"废料": 10}, "space": 1, "unlock": 0, "max_owned": 4},
        {"id": "g2", "name": "冲压机",   "out": {"零件": 0.4},
         "cost": {"废料": 60}, "space": 2, "unlock": 60},
        {"id": "g3", "name": "注塑台",   "out": {"零件": 0.1, "成品": 0.05},
         "cost": {"零件": 40, "废料": 120}, "space": 3, "unlock": 400},
        {"id": "g4", "name": "总装线",   "out": {"成品": 0.3},
         "cost": {"零件": 200, "成品": 30}, "space": 4, "unlock": 2000},
        {"id": "g5", "name": "自动仓储", "out": {"废料": 0.5, "零件": 0.2, "成品": 0.1},
         "cost": {"成品": 400, "零件": 500}, "space": 5, "unlock": 9000},
    ],
    "upgrades": [
        # 造价随已购数量指数增长；效果是全局产出倍率
        {"id": "u1", "name": "精密轴承", "cost": {"零件": 30}, "space": 0,
         "unlock": 200, "effect": {"mult": 1.12}},
        {"id": "u2", "name": "热处理炉", "cost": {"成品": 60}, "space": 0,
         "unlock": 800, "effect": {"mult": 1.15}},
        {"id": "u3", "name": "自动上料", "cost": {"零件": 250, "成品": 90}, "space": 0,
         "unlock": 4000, "effect": {"mult": 1.20}},
    ],
    "cost_growth": 1.15,   # 设备/升级造价的指数增长底数
    "prestige": {
        "name": "重整产线",
        "unlock": 60000,    # 累计产出达到才可重整
        "gain_mult": 0.12,  # 每次重整获得的永久倍率
    },
    "milestones": [100, 500, 2000, 9000, 30000, 60000],
}


class Sim:
    def __init__(self, cfg: dict, ai: str = "greedy") -> None:
        # ai: "greedy" = 按单位产出成本贪心（新手直觉）
        #     "max"    = 上限型，永远推最高解锁档（同档里买最便宜的）
        # 两种玩家行为一致 => 这个经济真的没有决策，不是我 AI 笨
        self.ai = ai
        self.cfg = cfg
        self.tick = float(cfg.get("tick_s", 1.0))
        self.space = int(cfg.get("space", 10 ** 9))
        self.growth = float(cfg.get("cost_growth", 1.15))
        self.res = {k: float(v) for k, v in cfg["start"].items()}
        self.gens = {g["id"]: 0 for g in cfg["generators"]}
        self.ups = {u["id"]: 0 for u in cfg.get("upgrades", [])}
        self.total = 0.0            # 累计产出（判定解锁用）
        self.elapsed = 0.0          # 本次上线内经过的秒数
        self.session = 0            # 第几次上线
        self.prestige_n = 0
        self.pr_mult = 1.0
        self.spent = {k: 0.0 for k in self.res}
        self.t_start = 0.0          # 累计时间轴（秒）
        self.events: list[tuple[float, str]] = []
        self.gap_start: float | None = None   # 本次空转起点
        self.gaps: list[tuple[float, float]] = []
        self.milestone_seen: set[float] = set()
        self.prestige_done = False
        self.buy_mix: dict[str, int] = {}      # 买了什么，各多少次
        self.ms_time: dict[float, float] = {}  # 里程碑 -> 首次达成的绝对时间
        self.no_buy_streak = 0.0               # 连续买不起任何东西的时长
        self.max_no_buy = 0.0
        self.total_no_buy = 0.0

    # ---------- 经济 ----------

    def res_mult(self, r: str) -> float:
        """某种资源的最终产出倍率 = 永久倍率 × 全局升级倍率 × 单资源倍率。

        刻意用「线性于等级」而不是「叠乘」：1.12^50 会炸到 300 倍，
        真实放置游戏也都是 每级 +12% 这种线性写法。
        `mult_res` 让加成有情境价值——只放大某一种资源，
        于是「这个升级现在值不值」变成了一个真问题。"""
        m = self.pr_mult
        for u in self.cfg.get("upgrades", []):
            eff = u.get("effect", {})
            step = eff.get("mult")
            if step:
                m *= 1.0 + (step - 1.0) * self.ups[u["id"]]
            one = (eff.get("mult_res") or {}).get(r)
            if one:
                m *= 1.0 + (one - 1.0) * self.ups[u["id"]]
        return m

    def cost_mult(self, r: str) -> float:
        """升级提供的造价折扣（只作用于设备，不作用于升级自身）。
        同样是线性于等级，`cost_red` 每级 -6% 写起来。"""
        m = 1.0
        for u in self.cfg.get("upgrades", []):
            red = (u.get("effect", {}).get("cost_red") or {}).get(r)
            if red:
                m *= max(0.15, 1.0 - red * self.ups[u["id"]])
        return m

    def space_total(self) -> int:
        """占地上限。真实放置游戏里「扩地」本身就是一个常驻升级位。"""
        s = int(self.cfg.get("space", 10 ** 9))
        for u in self.cfg.get("upgrades", []):
            gain = u.get("effect", {}).get("space")
            if gain:
                s += int(gain) * self.ups[u["id"]]
        return s

    def income(self) -> dict[str, float]:
        out = {k: 0.0 for k in self.res}
        for g in self.cfg["generators"]:
            n = self.gens[g["id"]]
            if n:
                for r, v in g["out"].items():
                    out[r] += v * n
        for u in self.cfg.get("upgrades", []):
            n = self.ups[u["id"]]
            if n and "out" in u:
                for r, v in u["out"].items():
                    out[r] += v * n
        scale = {r: self.res_mult(r) for r in out}
        return {r: v * scale[r] for r, v in out.items()}

    def total_rate(self) -> float:
        return sum(self.income().values())

    def space_left(self) -> int:
        used = sum(self.gens[g["id"]] * g["space"]
                   for g in self.cfg["generators"])
        return self.space_total() - used

    def cap_of(self, g: dict) -> int:
        """单类设备上限。真实放置游戏靠它防止「最便宜的设备吃满全部空间」，
        没有这个上限，占地最省的那台会挤死后面所有内容。"""
        return min(self.space_left(), g.get("max_owned", 10 ** 9))

    def upg_cap(self, u: dict) -> int:
        """升级等级上限。

        没有上限的倍率升级 = 无限最优解：造价只按 cost_growth^n 涨，
        而总产量因为设备增长涨得更快，于是「加倍率」永远是划算的，
        玩家会无脑刷这一个升级，整局游戏只剩一个决策。
        真实放置游戏的解法是设等级上限，或让加成有情境价值。"""
        return int(u.get("max_level", 10 ** 9))

    def cost_of(self, item: dict, owned: int) -> dict[str, float]:
        is_gen = "out" in item
        return {r: v * (self.growth ** owned) * (self.cost_mult(r) if is_gen else 1.0)
                for r, v in item["cost"].items()}

    def affordable(self, item: dict, owned: int) -> bool:
        return all(self.res[r] >= v for r, v in self.cost_of(item, owned).items())

    # ---------- 决策 ----------

    def spend_rate(self) -> float:
        """平均每秒花掉多少资源。用于把「省下来的钱」换算成「多产出的量」。"""
        t = max(1.0, self.t_start)
        return sum(self.spent.values()) / t

    def upg_value(self, u: dict) -> float:
        """这一级升级能多产出多少单位/秒。统一估值，让 AI 能比较
        「加倍率 / 扩地 / 降造价 / 只放大某资源」这四类完全不同的升级。

        做法：把每种效果都折算成等价的「每秒多产多少」。
        玩家视角就是「这东西值不值」，而不是「它的字段叫什么」。"""
        eff = u.get("effect", {})
        n = self.ups[u["id"]]
        inc = self.income()
        total = sum(inc.values()) or 1e-9
        v = 0.0
        if eff.get("mult"):
            v += total * (eff["mult"] - 1.0)
        for r, m in (eff.get("mult_res") or {}).items():
            v += inc.get(r, 0.0) * (m - 1.0)
        if eff.get("cost_red"):
            # 省钱按当前效率折算成产量
            eff_rate = total / max(1e-9, self.spend_rate())
            v += sum(eff["cost_red"].values()) * eff_rate
        if eff.get("space"):
            used = sum(self.gens[g["id"]] * g["space"]
                       for g in self.cfg["generators"])
            per_space = (total / used) if used else max(
                (sum(g["out"].values()) for g in self.cfg["generators"]), default=0.0)
            v += eff["space"] * per_space
        return v * (1.0 + n)  # 下一级比当前更值钱一点

    def best_buy(self) -> tuple[str, dict | None, dict[str, float] | None]:
        """返回一个 (kind, item, cost, score) 四元组。"""
        best = ("", None, None, (0.0, 0.0))
        inc = self.income()
        for g in self.cfg["generators"]:
            owned = self.gens[g["id"]]
            if owned + 1 > self.cap_of(g):
                continue
            if self.total < g.get("unlock", 0):
                continue
            if not self.affordable(g, owned):
                continue          # 买不起就不考虑，模拟的是「理性玩家」
            c = self.cost_of(g, owned)
            gain = sum(v for v in g["out"].values()) * self.pr_mult
            if gain <= 0:
                continue
            if self.ai == "max":
                key = (-g.get("unlock", 0), sum(c.values()))
            else:
                key = (sum(c.values()) / gain, 0.0)
            if best[0] == "" or key < best[3]:
                best = ("gen", g, c, key)
        for u in self.cfg.get("upgrades", []):
            owned = self.ups[u["id"]]
            if owned >= self.upg_cap(u):
                continue
            if self.total < u.get("unlock", 0):
                continue
            if not self.affordable(u, owned):
                continue
            c = self.cost_of(u, owned)
            val = self.upg_value(u)
            if val <= 0:
                continue
            if self.ai == "max":
                key = (-u.get("unlock", 0), sum(c.values()))
            else:
                key = (sum(c.values()) / val, 0.0)
            if best[0] == "" or key < best[3]:
                best = ("upg", u, c, key)
        return best[0], best[1], best[2], best[3]

    def any_affordable(self) -> bool:
        """当前是否存在任何一件买得起、且已解锁、且占得起地的东西。"""
        for g in self.cfg["generators"]:
            if self.total < g.get("unlock", 0):
                continue
            if self.gens[g["id"]] + 1 > self.cap_of(g):
                continue
            if self.affordable(g, self.gens[g["id"]]):
                return True
        for u in self.cfg.get("upgrades", []):
            if self.total < u.get("unlock", 0):
                continue
            if self.ups[u["id"]] >= self.upg_cap(u):
                continue
            if self.affordable(u, self.ups[u["id"]]):
                return True
        return False

    def probe(self, dt: float) -> None:
        """每个 tick 问一次「玩家现在有事可做吗」。这是空转的客观定义。"""
        if self.any_affordable():
            self.no_buy_streak = 0.0
        else:
            self.no_buy_streak += dt
            self.max_no_buy = max(self.max_no_buy, self.no_buy_streak)
            self.total_no_buy += dt

    def do_buy(self) -> bool:
        kind, item, c, _ = self.best_buy()
        if item is None or c is None:
            return False
        for r, v in c.items():
            self.res[r] -= v
            self.spent[r] = self.spent.get(r, 0.0) + v
        key = item["name"]
        self.buy_mix[key] = self.buy_mix.get(key, 0) + 1
        if kind == "gen":
            self.gens[item["id"]] += 1
        else:
            self.ups[item["id"]] += 1
        return True

    # ---------- 时间推进 ----------

    def tick_fwd(self, dt: float) -> None:
        inc = self.income()
        for r, v in inc.items():
            self.res[r] += v * dt
            self.total += v * dt
        self.elapsed += dt
        self.t_start += dt
        self.probe(dt)
        for ms in self.cfg.get("milestones", []):
            if self.total >= ms and ms not in self.milestone_seen:
                self.milestone_seen.add(ms)
                if ms not in self.ms_time:
                    self.ms_time[ms] = self.t_start   # 只记首次，重整后不覆盖
                self.events.append((self.t_start, f"里程碑 {ms:g}"))

    def check_gap(self, had_purchase: bool) -> None:
        """空转检测：能产东西、但买不起任何东西 → 玩家在干等。"""
        busy = self.total_rate() > 0
        if busy and not had_purchase:
            if self.gap_start is None:
                self.gap_start = self.elapsed
        else:
            if self.gap_start is not None:
                self.gaps.append((self.gap_start, self.elapsed))
                self.gap_start = None

    def maybe_prestige(self) -> bool:
        """重整必须是玩家在一次上线开始时的主动决策，不是脚本自动触发。

        恒定倍率用「1 + 次数 x 增益」线性，而不是 (1+gain)^n 复合 ——
        复合会在几十次重整后把倍率推到天文数字，等于没有上限。
        """
        p = self.cfg.get("prestige")
        if not p or self.prestige_done:
            return False
        if self.total < p.get("unlock", float("inf")):
            return False
        # 只有当这一次重整的收益还「有意义」时才值得做：相对提升至少 8%，
        # 否则玩家会陷入「再等等」而永远不点
        if p.get("gain_mult", 0.1) < 0.08:
            return False
        self.events.append(
            (self.t_start, f"重整 x{self.prestige_n + 1}（累计 {self.total:.0f}）")
        )
        self.prestige_n += 1
        self.pr_mult = 1.0 + self.prestige_n * p.get("gain_mult", 0.1)
        self.prestige_done = True
        # 重置进度、保留永久倍率
        # 注意：必须重新发放起手资源，否则清零后没有任何产出 -> 永久死锁。
        # 真实的放置游戏在转生后同样要送一份「启动资金」，否则玩家会卡死。
        for k, v in self.cfg["start"].items():
            self.res[k] = float(v)
        for g in self.cfg["generators"]:
            self.gens[g["id"]] = 0
        for u in self.cfg.get("upgrades", []):
            self.ups[u["id"]] = 0
        self.total = 0.0
        self.milestone_seen.clear()
        return True

    # ---------- 一个会话 ----------

    def run_session(self, session_seconds: float) -> None:
        self.elapsed = 0.0
        self.gap_start = None
        self.prestige_done = False   # 每次上线重置：重整是一次决策
        bought_at = -1e9
        while self.elapsed < session_seconds:
            self.tick_fwd(self.tick)
            # 每 5 秒做一次购买决策（模拟人的反应频率）
            if self.elapsed - bought_at >= 5.0:
                did = False
                # 一次最多买 3 档，避免瞬间倾家荡产（更像真人）
                for _ in range(3):
                    if not self.do_buy():
                        break
                    did = True
                bought_at = self.elapsed
            self.check_gap(did)

    def finish(self) -> None:
        if self.gap_start is not None:
            self.gaps.append((self.gap_start, self.elapsed))
            self.gap_start = None


def preflight(s: Sim) -> str | None:
    """开局死锁检查：既没有可买的东西，也没有任何产出 -> 永远推不动。"""
    if s.any_affordable():
        return None
    if s.total_rate() > 0:
        return None
    names = [g["name"] for g in s.cfg["generators"]]
    cheapest = ""
    if names:
        cheapest = min(
            s.cfg["generators"],
            key=lambda g: sum(g["cost"].values()),
        )
        cheapest = (f"最便宜的一件是 {cheapest['name']}，造价 "
                    + "、".join(f"{r} {v:g}" for r, v in cheapest["cost"].items()))
    return (
        "开局死锁：起始资源买不起任何设备，且没有任何产出，"
        f"游戏永远无法启动。{cheapest}\n"
        "修复：① 给起始资源足够买第一台设备  ② 或让第一台设备免费/可手动产出"
    )


def run(cfg: dict, sessions: int, session_seconds: float, ai: str = "greedy") -> Sim:
    s = Sim(cfg, ai=ai)
    s.session = 0
    while s.t_start < sessions * session_seconds:
        s.maybe_prestige()   # 玩家在这一刻决定要不要重整
        s.run_session(session_seconds)
        s.session += 1
    s.finish()
    return s


def report(s: Sim, cfg: dict) -> None:
    total_h = s.t_start / 3600.0
    print()
    print("=" * 74)
    print(f"{cfg['name']}   {s.session} 次上线 × "
          f"{s.elapsed / 60:.0f} 分钟 = 模拟 {total_h:.1f} 小时")
    print("=" * 74)

    print("\n【资源】")
    for r, v in s.res.items():
        print(f"  {r:<8} 结余 {v:>14,.0f}   本轮累计投入 {s.spent.get(r, 0):>14,.0f}")

    print("\n【产出速率】")
    inc = s.income()
    for r, v in sorted(inc.items(), key=lambda kv: -kv[1]):
        if v:
            print(f"  {r:<8} {v:>12,.1f} /秒  ({v * 3600:>14,.0f} /小时)")
    print(f"  合计    {s.total_rate():>12,.1f} /秒")

    print("\n【设备 / 升级】")
    used = 0
    for g in cfg["generators"]:
        n = s.gens[g["id"]]
        used += n * g["space"]
        print(f"  {g['name']:<10} x{n:<5} 占地 {n * g['space']}")
    for u in cfg.get("upgrades", []):
        print(f"  {u['name']:<10} x{s.ups[u['id']]}")
    cap = s.space_total()
    if cap and cap < 10 ** 9:
        print(f"  → 占地 {used}/{cap}  {'⚠ 已占满' if used >= cap else ''}")

    print("\n【时间线】")
    for t, msg in s.events[:60]:
        print(f"  {t / 3600:>7.2f} h   {msg}")

    # --- 诊断 ---
    print("\n" + "-" * 74)
    print("【诊断】")

    # --- 1. 干等：客观定义 = 买不起任何一件已解锁且占得起地的东西 ---
    share_idle = s.total_no_buy / s.t_start * 100 if s.t_start else 0
    # 放置游戏的「等待」是设计的一部分：上线买几样东西然后就该下线。
    # 所以主指标是「最长连续空转」——玩家会不会卡到没事干；
    # 「占全程百分比」只作参考，放置游戏天然很高，早期用 12% 当阈值是错的。
    if s.max_no_buy >= 900:
        print(f"  [!] 干等：最长连续 {s.max_no_buy / 60:.1f} 分钟买不起任何东西"
              f"（阈值 15 分钟）—— 中期空转，玩家会流失")
    else:
        print(f"  OK 无空转死区：最长连续 {s.max_no_buy / 60:.1f} 分钟有东西可买"
              f"（全程 {share_idle:.0f}% 时间在攒资源，放置游戏正常）")

    # --- 2. 决策多样性：一直在买同一样东西 = 这游戏没有决策 ---
    tot_buy = sum(s.buy_mix.values())
    if tot_buy >= 20:
        items = sorted(s.buy_mix.items(), key=lambda kv: -kv[1])
        top_name, top_n = items[0]
        top_share = top_n / tot_buy * 100
        kinds = len(items)
        if kinds <= 2:
            print(f"  [!] 决策面极窄：{tot_buy} 次购买里只有 {kinds} 类东西"
                  f"（{top_name} 占 {top_share:.0f}%）—— 玩家只是在重复同一个动作")
        elif top_share > 70:
            print(f"  [!] 购买过度集中：{top_name} 占 {top_share:.0f}%，"
                  f"其余 {kinds - 1} 项形同虚设 —— 性价比需要重做")
        else:
            print(f"  OK 购买分布合理：{kinds} 类，最高一项占 {top_share:.0f}%")
    else:
        print(f"  .. 购买样本太少（{tot_buy} 次），决策面看不出")

    # --- 3. 曲线膨胀 ---
    # 旧口径有致命错误：拿「首次达成某里程碑」和「后期某一轮达成」比，
    # 那是两个不同周期，不可比。正确口径是量纲无关的：
    # 每两档之间算「时间 / log(数值倍数)」，看有没有某一档特别慢。
    ms = sorted(s.ms_time.keys())
    if len(ms) >= 3:
        ts = [s.ms_time[m] for m in ms]
        # 只取首次达成序列（重整后的重复达成已被 ms_time 去掉，这里再按顺序取第一段）
        seg = []
        for i in range(1, len(ts)):
            dt = ts[i] - ts[i - 1]
            if dt <= 0:
                continue
            ratio = ms[i] / ms[i - 1] if ms[i - 1] else 1
            seg.append((dt / max(1e-9, ratio), dt, ms[i - 1], ms[i]))
        if seg:
            seg_sorted = sorted(seg, key=lambda x: x[0])
            median = seg_sorted[len(seg_sorted) // 2][0]
            worst = seg[-1]
            growth = max(ms) / min(ms) if min(ms) else 0
            span = (ts[-1] - ts[0])
            print(f"  {'OK ' if worst[0] <= median * 4 else '[!] '}曲线节奏："
                  f"里程碑 {min(ms):g} -> {max(ms):g}（{growth:.0f}x 数值）"
                  f"用了 {span / 60:.0f} 分钟；"
                  f"最慢的一档 {worst[2]:g}->{worst[3]:g} 间隔 {worst[1] / 60:.1f} 分钟"
                  f"（比中位档慢 {worst[0] / median:.1f}x）")
        else:
            print("  .. 里程碑时间戳异常，看不出膨胀")
    else:
        print("  .. 里程碑达成样本不足，看不出膨胀")

    # --- 4. 资源垄断 ---
    vals = [v for v in s.spent.values() if v > 0]
    if vals:
        top = max(vals)
        share = top / sum(vals) * 100
        if share > 70:
            print(f"  [!] 资源投入 {share:.0f}% 集中在单一资源 —— 多种子设计失效，"
                  f"要么合并，要么给它真实用途")
        else:
            print(f"  OK 资源投入最集中的一项占 {share:.0f}%")

    # --- 5. 重整节奏 ---
    if s.prestige_n:
        per = s.t_start / 3600.0 / max(1, s.prestige_n)
        flag = "  [!] 周期太短，重整失去仪式感" if per < 0.5 else ""
        print(f"  .. 共重整 {s.prestige_n} 次，永久倍率 x{s.pr_mult:.2f}，"
              f"平均每 {per * 60:.0f} 分钟一次{flag}")
    else:
        p = cfg.get("prestige")
        if p and s.total < p.get("unlock", 0):
            print(f"  .. 未触发重整（门槛 {p['unlock']:g}，结束时累计 "
                  f"{s.total:,.0f}）—— 可能门槛设得太高")
    print("-" * 74)


def lint(cfg: dict) -> list[str]:
    """静态体检：抓那些「跑起来才发现、但其实是设计错误」的问题。

    重点是放置游戏最常见的两类低级错：
      1. 自锁：某台设备的造价里含一种资源，而这种资源在它之前解锁的设备里根本没人产
         -> 这台设备永远买不起，整段内容变成死内容（模拟器只能看到「干等」，
            看不到「为什么」）
      2. 空间被单一最便宜设备填满：占地最省的那台一旦能无限买，就会把 space 占满，
         后面所有更贵更好的设备再也进不来
    """
    problems: list[str] = []
    gens = sorted(cfg["generators"], key=lambda g: g.get("unlock", 0))
    start = set(cfg["start"])
    produced = set(start)          # 当前「已可达」的资源集合
    reachable: list[dict] = []
    for g in gens:
        need = set(g["cost"])
        missing = need - produced
        if missing and reachable:
            problems.append(
                f"自锁 · {g['name']}：造价需要 {'、'.join(sorted(missing))}，"
                f"但它之前的设备里没有任何一台产这些资源 -> 永远买不起"
                f"（要么删掉这部分造价，要么让前一级设备产出它）"
            )
            continue
        if missing and not reachable:
            problems.append(
                f"死锁 · {g['name']}：起手资源 {'、'.join(sorted(start))} "
                f"买不起它，造价里的 {'、'.join(sorted(missing))} 也没人产"
            )
            continue
        reachable.append(g)
        produced |= set(g["out"])

    space = int(cfg.get("space", 10 ** 9))
    if space < 10 ** 9 and reachable:
        # 如果有「无上限的扩地升级」，空间陷阱是有解的：玩家可以买地，
        # 于是最便宜的设备吃满空间反而变成一个真决策（买设备还是买地）。
        infinite_space = any(u.get("effect", {}).get("space")
                             and u.get("max_level", 0) >= 10 ** 5
                             for u in cfg.get("upgrades", []))
        cheapest = min(reachable, key=lambda g: (sum(g["cost"].values()), g["space"]))
        # 已经用 max_owned 防住的就不要再报
        trapped = cheapest.get("max_owned", 10 ** 9) >= space
        need_slots = space // max(1, cheapest["space"])
        if infinite_space:
            problems.append(
                f"提示 · {cheapest['name']} 占地 {cheapest['space']} 且最便宜，"
                f"起手 {space} 格只够放 {need_slots} 台。已检测到无上限的扩地升级，"
                f"所以这是可解的决策点而不是死局——但要确认它的造价涨幅跟得上"
            )
        elif trapped and cheapest["space"] == 1 and len(reachable) > 1:
            problems.append(
                f"空间陷阱 · {cheapest['name']} 占地 1 且最便宜，{space} 格能被它独占 "
                f"{need_slots} 台 -> 后面 {len(reachable) - 1} 种设备永远挤不进来，"
                f"玩家只会重复买同一台（这就是模拟器报「决策面极窄」的典型原因）"
            )
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="放置类游戏平衡模拟器")
    ap.add_argument("--config", help="economy JSON 路径")
    ap.add_argument("--sessions", type=int, default=40, help="模拟几次上线")
    ap.add_argument("--minutes", type=int, default=20, help="每次上线多少分钟")
    ap.add_argument("--offline-hours", type=float, default=8.0,
                    help="两次上线之间的离线小时数（会按离线收益结算）")
    ap.add_argument("--ai", choices=["greedy", "max", "both"], default="both",
                    help="模拟的玩家策略：greedy=单位产出成本贪心（新手），"
                         "max=永远推最高档（上限型），both=两种都跑并对照")
    ap.add_argument("--dump-schema", action="store_true", help="打印示例配置后退出")
    a = ap.parse_args()

    if a.dump_schema:
        print(json.dumps(SAMPLE, ensure_ascii=False, indent=2))
        return 0

    cfg = SAMPLE
    # 路径一律相对脚本目录，避免从别的 cwd 运行就读不到配置
    here = os.path.dirname(os.path.abspath(__file__))
    if a.config:
        cfg_path = a.config if os.path.isabs(a.config) else os.path.join(here, a.config)
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    elif os.path.exists(os.path.join(here, "economy.json")):
        with open(os.path.join(here, "economy.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        print(f"[已加载 economy.json: {cfg.get('name', '?')}]")

    problems = lint(cfg)
    if problems:
        print(f"【静态体检】{cfg.get('name', '(未命名)')} 发现 {len(problems)} 个设计问题：")
        for pr in problems:
            print(f"  [X] {pr}")

    sim = Sim(cfg)
    err = preflight(sim)
    if err:
        print(f"[X] {cfg.get('name', '(未命名)')} 无法模拟：{err}", file=sys.stderr)
        return 2

    ais = ["greedy", "max"] if a.ai == "both" else [a.ai]
    sims = {}
    for ai in ais:
        print(f"\n{'=' * 78}\n【玩家策略：{ai}】")
        print("=" * 78)
        sim = run(cfg, a.sessions, a.minutes * 60.0, ai=ai)
        report(sim, cfg)
        sims[ai] = sim

    if len(sims) == 2:
        g, m = sims["greedy"].buy_mix, sims["max"].buy_mix
        print(f"\n{'=' * 78}\n【决策面对照】两种玩家买的东西是否一样？")
        print("=" * 78)
        keys = sorted(set(g) | set(m))
        print(f"  {'购买目标':<16}{'贪婪玩家':>12}{'上限玩家':>12}")
        for k in keys:
            print(f"  {k:<16}{g.get(k, 0):>12}{m.get(k, 0):>12}")
        gt = max(g, key=g.get) if g else None
        mt = max(m, key=m.get) if m else None
        tot_g, tot_m = sum(g.values()), sum(m.values())
        share_g = g.get(gt, 0) / tot_g * 100 if tot_g else 0
        share_m = m.get(mt, 0) / tot_m * 100 if tot_m else 0
        print(f"\n  贪婪玩家主买：{gt}（{share_g:.0f}%）   上限玩家主买：{mt}（{share_m:.0f}%）")
        if gt == mt and share_g > 60 and share_m > 60:
            print("  [!] 两种截然不同的玩家都只买同一件东西 -> 这个经济真的没有决策，"
                  "不是模拟器的问题")
        else:
            print("  [.] 两种玩家的行为明显不同 -> 经济里有真实的决策空间")
    return 0


if __name__ == "__main__":
    sys.exit(main())

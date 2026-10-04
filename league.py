#!/usr/bin/env python3
"""联赛积分榜工具：改判级联重算 + 黄牌停赛 + 并列排名（纯标准库，单文件）

用法：
    python3 league.py --demo        # 运行内置示例（可直接验证全部功能）
    python3 league.py < input.txt   # 从标准输入读取指令流

输入协议（行文本，# 开头为注释，字段空白分隔）：
    TEAM   <球队名>
    PLAYER <球员名> <球队名>
    MATCH  <场次> <主队> <客队> <主分> <客分> [主队进球球员...]
           进球球员写作 名字 或 名字+Y（该球员本场同时吃到一张黄牌）
    FIX    <场次> CONFIRM <新主分> <新客分>   # 确认改判（也接受中文“确认”）
    FIX    <场次> REVOKE                      # 撤销改判，回退原比分（也接受“撤销”）

自定规则：
    * 黄牌累积阈值 YELLOW_LIMIT=2，达到即停赛，停赛对后续所有场次生效（级联标记）。
    * 停赛球员的进球无效：从有效比分中扣除并写入报告；未注册/不属于主队的进球同样无效。
    * 每场最多一次改判：重复改判、撤销无改判的场次、改判不存在的场次、负分均报错。
    * 改判只改比分；停赛无效进球的扣减对改判后的比分依然生效。
    * 排序键：积分 > 净胜球 > 相互战绩积分（同分同净胜球小循环）> 进球数 > 队名。
      全部键相同则并列排名（如 1, 2, 2, 4）。
    * 状态跨流延续：先执行赛果流，再执行改判流，改判在既有状态上级联重算。
"""

import sys
import unicodedata
from dataclasses import dataclass

YELLOW_LIMIT = 2  # 累积黄牌停赛阈值


@dataclass
class Match:
    mid: str
    home: str
    away: str
    raw_home: int
    raw_away: int
    invalid_home: int = 0          # 主队无效进球数（停赛/未注册/非本队）
    corrected: tuple = None        # 改判后比分 (主, 客)，None 表示未改判

    def effective(self):
        h, a = self.corrected if self.corrected is not None else (self.raw_home, self.raw_away)
        return max(0, h - self.invalid_home), a


class League:
    def __init__(self):
        self.teams = []
        self.team_set = set()
        self.players = {}          # 球员 -> 球队
        self.matches = {}          # 场次 -> Match
        self.order = []            # 场次的时间顺序（赛果流顺序）
        self.errors = []           # 错误/事件报告
        self.yellows = {}          # 球员 -> 累计黄牌
        self.suspended = set()     # 停赛球员

    def err(self, msg):
        self.errors.append(msg)

    # ---- 定义流 ----
    def add_team(self, name):
        if name in self.team_set:
            self.err(f"球队重复定义，已忽略: {name}")
            return
        self.team_set.add(name)
        self.teams.append(name)

    def add_player(self, name, team):
        if team not in self.team_set:
            self.err(f"球员 {name} 的球队不存在: {team}")
            return
        if name in self.players:
            self.err(f"球员重复定义，已忽略: {name}")
            return
        self.players[name] = team

    # ---- 赛果流 ----
    def add_match(self, mid, home, away, hs, as_, scorers):
        if mid in self.matches:
            self.err(f"场次重复，已忽略: {mid}")
            return
        for t in (home, away):
            if t not in self.team_set:
                self.err(f"场次 {mid}: 球队不存在: {t}，该场已忽略")
                return
        if hs < 0 or as_ < 0:
            self.err(f"场次 {mid}: 比分非法（负分），该场已忽略")
            return
        m = Match(mid, home, away, hs, as_)
        new_bans = []
        for tok in scorers:
            yellow = tok.endswith("+Y")
            name = tok[:-2] if yellow else tok
            if name not in self.players:
                self.err(f"场次 {mid}: 进球球员未注册: {name}，进球无效")
                m.invalid_home += 1
                continue
            if self.players[name] != home:
                self.err(f"场次 {mid}: {name} 不属于主队 {home}，进球无效")
                m.invalid_home += 1
                continue
            if name in self.suspended:
                self.err(f"场次 {mid}: 停赛球员 {name} 进球无效")
                m.invalid_home += 1
                continue
            if yellow:
                self.yellows[name] = self.yellows.get(name, 0) + 1
                if self.yellows[name] >= YELLOW_LIMIT and name not in self.suspended:
                    new_bans.append(name)
        if m.invalid_home > hs:
            self.err(f"场次 {mid}: 无效进球数超过主队比分，有效比分按 0 计")
        self.matches[mid] = m
        self.order.append(mid)
        for name in new_bans:  # 停赛从下一场开始生效（级联标记）
            self.suspended.add(name)
            self.err(f"球员 {name} 累计 {self.yellows[name]} 张黄牌，停赛（后续场次进球无效）")

    # ---- 改判流 ----
    def fix(self, mid, action, hs=None, as_=None):
        m = self.matches.get(mid)
        if m is None:
            self.err(f"改判失败: 场次 {mid} 不存在")
            return
        if action == "CONFIRM":
            if m.corrected is not None:
                self.err(f"改判失败: 场次 {mid} 重复改判")
                return
            if hs < 0 or as_ < 0:
                self.err(f"改判失败: 场次 {mid} 比分非法（负分）")
                return
            m.corrected = (hs, as_)
        elif action == "REVOKE":
            if m.corrected is None:
                self.err(f"撤销失败: 场次 {mid} 无改判可撤销")
                return
            m.corrected = None
        else:
            self.err(f"未知改判动作: {action}")

    # ---- 积分榜 ----
    def table(self):
        st = {t: dict(P=0, W=0, D=0, L=0, GF=0, GA=0, PTS=0) for t in self.teams}
        for mid in self.order:
            m = self.matches[mid]
            h, a = m.effective()
            sh, sa = st[m.home], st[m.away]
            for s, gf, ga in ((sh, h, a), (sa, a, h)):
                s["P"] += 1
                s["GF"] += gf
                s["GA"] += ga
            if h > a:
                sh["W"] += 1; sa["L"] += 1; sh["PTS"] += 3
            elif h < a:
                sa["W"] += 1; sh["L"] += 1; sa["PTS"] += 3
            else:
                sh["D"] += 1; sa["D"] += 1; sh["PTS"] += 1; sa["PTS"] += 1
        return st

    def ranking(self):
        st = self.table()
        gd = lambda t: st[t]["GF"] - st[t]["GA"]
        pre = sorted(self.teams, key=lambda t: (-st[t]["PTS"], -gd(t), t))
        rows = []  # (team, h2h_points)
        i = 0
        while i < len(pre):
            j = i
            while j < len(pre) and (st[pre[j]]["PTS"], gd(pre[j])) == (st[pre[i]]["PTS"], gd(pre[i])):
                j += 1
            grp = pre[i:j]
            h2h = {t: 0 for t in grp}
            if len(grp) > 1:  # 同分同净胜球小循环：相互战绩积分
                for mid in self.order:
                    m = self.matches[mid]
                    if m.home in h2h and m.away in h2h:
                        h, a = m.effective()
                        if h > a:
                            h2h[m.home] += 3
                        elif h < a:
                            h2h[m.away] += 3
                        else:
                            h2h[m.home] += 1; h2h[m.away] += 1
            grp.sort(key=lambda t: (-h2h[t], -st[t]["GF"], t))
            rows.extend((t, h2h[t]) for t in grp)
            i = j
        out, prev_key, prev_rank = [], None, 0
        for idx, (t, h2hp) in enumerate(rows, 1):
            key = (st[t]["PTS"], gd(t), h2hp, st[t]["GF"])
            rank = prev_rank if key == prev_key else idx  # 全键相同 -> 并列
            out.append((rank, t, st[t]))
            prev_key, prev_rank = key, rank
        return out


def _pad(s, width):
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)
    return s + " " * max(0, width - w)


def render(league):
    lines = [_pad("排名", 6) + _pad("球队", 10) + " 赛  胜  平  负   进   失  净胜  积分"]
    for rank, t, s in league.ranking():
        gd = s["GF"] - s["GA"]
        lines.append(
            _pad(str(rank), 6) + _pad(t, 10)
            + f"{s['P']:>3}{s['W']:>4}{s['D']:>4}{s['L']:>4}"
            + f"{s['GF']:>5}{s['GA']:>5}{gd:>5}{s['PTS']:>5}"
        )
    return "\n".join(lines)


def render_errors(league):
    if not league.errors:
        return "（无）"
    return "\n".join(f"{i}. {e}" for i, e in enumerate(league.errors, 1))


def run_lines(league, lines):
    for ln, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        p = line.split()
        cmd = p[0].upper()
        try:
            if cmd == "TEAM":
                league.add_team(p[1])
            elif cmd == "PLAYER":
                league.add_player(p[1], p[2])
            elif cmd == "MATCH":
                league.add_match(p[1], p[2], p[3], int(p[4]), int(p[5]), p[6:])
            elif cmd == "FIX":
                action = p[2].upper()
                if action in ("CONFIRM", "确认"):
                    league.fix(p[1], "CONFIRM", int(p[3]), int(p[4]))
                elif action in ("REVOKE", "撤销"):
                    league.fix(p[1], "REVOKE")
                else:
                    league.err(f"第{ln}行: 未知改判动作 {p[2]}")
            else:
                league.err(f"第{ln}行: 未知指令 {p[0]}")
        except (IndexError, ValueError):
            league.err(f"第{ln}行: 格式错误: {line}")


def demo():
    results = """
    TEAM 青龙
    TEAM 白虎
    TEAM 朱雀
    TEAM 玄武
    PLAYER 李雷 青龙
    PLAYER 韩梅 青龙
    PLAYER 王强 白虎
    PLAYER 赵刚 白虎
    PLAYER 孙丽 朱雀
    PLAYER 周杰 朱雀
    PLAYER 吴迪 玄武
    PLAYER 郑钱 玄武
    MATCH M1 青龙 白虎 2 1 李雷+Y 韩梅
    MATCH M2 青龙 朱雀 2 1 李雷+Y 韩梅
    MATCH M3 青龙 玄武 2 0 李雷 韩梅
    MATCH M4 白虎 朱雀 1 1 王强
    MATCH M5 玄武 白虎 2 2 吴迪 郑钱
    """
    fixes = """
    FIX M1 CONFIRM 3 1
    FIX M1 CONFIRM 2 0
    FIX M9 CONFIRM 1 0
    FIX M2 CONFIRM 0 -1
    FIX M5 确认 2 1
    FIX M1 撤销
    FIX M1 REVOKE
    """
    lg = League()
    run_lines(lg, results.splitlines())
    print("== 赛果流结束后积分榜（改判前）==")
    print(render(lg))
    run_lines(lg, fixes.splitlines())  # 状态跨流延续，改判级联重算
    print("\n== 改判流结束后积分榜（改判后，级联重算）==")
    print(render(lg))
    print("\n== 错误/事件报告 ==")
    print(render_errors(lg))


def main():
    if "--demo" in sys.argv or sys.stdin.isatty():
        demo()
        return
    lg = League()
    run_lines(lg, sys.stdin.read().splitlines())
    print("== 积分榜 ==")
    print(render(lg))
    print("\n== 错误/事件报告 ==")
    print(render_errors(lg))


if __name__ == "__main__":
    main()

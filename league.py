#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
league.py — 联赛积分榜工具：改判级联重算 / 黄牌停赛 / 并列排名（纯标准库，单文件）

输入为逐行指令流（支持 # 注释与空行），行顺序即时间顺序，状态跨流延续：

  TEAM   <球队名>
  PLAYER <球员名> <球队名>
  MATCH  <场次> <主队> <客队> <主分> <客分> <主队进球球员列表>
         （进球列表用英文逗号分隔，无进球写 - ）
  CARD   <场次> <球员名>                     黄牌（累积满 2 张起停赛后续场次）
  FIX    <场次> confirm <新主分> <新客分>     确认改判（级联重算全榜）
  FIX    <场次> revoke                        撤销比赛（积分回退并级联重排）

规则（自定，可调下方常量）：
  胜 3 分 / 平 1 分 / 负 0 分；黄牌累积满 2 张停赛，停赛球员后续进球无效并报告；
  排序键：积分 > 净胜球 > 相互战绩积分 > 相互战绩净胜球 > 总进球 > 队名；
  上述键完全相同则并列排名（1224 式）。

用法：
  python3 league.py 数据文件
  cat 数据.txt | python3 league.py
  python3 league.py --demo        运行内置示例，可直接验证输出
"""

import sys
import unicodedata

WIN_POINTS = 3
DRAW_POINTS = 1
YELLOW_CARD_SUSPENSION_THRESHOLD = 2  # 黄牌停赛累积满额


class League:
    def __init__(self):
        self.teams = {}          # 队名 -> None（保序）
        self.players = {}        # 球员名 -> 队名
        self.matches = {}        # 场次 -> dict
        self.yellow = {}         # 球员名 -> 累计黄牌
        self.suspended = set()   # 停赛球员
        self.errors = []         # 错误报告
        self.notices = []        # 停赛 / 无效进球 / 改判轨迹

    # ---------- 指令处理 ----------

    def feed(self, lineno, line):
        line = line.split('#', 1)[0].strip()
        if not line:
            return
        parts = line.split()
        cmd, args = parts[0].upper(), parts[1:]
        handler = getattr(self, 'cmd_' + cmd, None)
        if handler is None:
            self.errors.append('第%d行: 未知指令 %s' % (lineno, parts[0]))
            return
        try:
            handler(lineno, args)
        except ValueError:
            self.errors.append('第%d行: 数字参数非法: %s' % (lineno, line))

    def cmd_TEAM(self, lineno, args):
        (name,) = self._arity(lineno, args, 1, 'TEAM <球队名>')
        if name in self.teams:
            self.errors.append('第%d行: 球队重复定义: %s' % (lineno, name))
            return
        self.teams[name] = None

    def cmd_PLAYER(self, lineno, args):
        name, team = self._arity(lineno, args, 2, 'PLAYER <球员名> <球队名>')
        if team not in self.teams:
            self.errors.append('第%d行: 球员 %s 所属球队不存在: %s' % (lineno, name, team))
            return
        self.players[name] = team

    def cmd_MATCH(self, lineno, args):
        mid, home, away, hs, as_, scorers = self._arity(
            lineno, args, 6, 'MATCH <场次> <主队> <客队> <主分> <客分> <进球列表>')
        hs, as_ = int(hs), int(as_)
        if mid in self.matches:
            self.errors.append('第%d行: 场次重复定义: %s' % (lineno, mid))
            return
        for t in (home, away):
            if t not in self.teams:
                self.errors.append('第%d行: 场次 %s 引用不存在的球队: %s' % (lineno, mid, t))
                return
        if home == away:
            self.errors.append('第%d行: 场次 %s 主客队相同: %s' % (lineno, mid, home))
            return
        if hs < 0 or as_ < 0:
            self.errors.append('第%d行: 场次 %s 比分非法（负分）: %d:%d' % (lineno, mid, hs, as_))
            return
        scorer_list = [] if scorers == '-' else scorers.split(',')
        valid_goals = hs
        if len(scorer_list) != hs:
            self.notices.append('第%d行: 场次 %s 主分 %d 与进球球员数 %d 不一致，以主分为准'
                                % (lineno, mid, hs, len(scorer_list)))
        for p in scorer_list:
            if p not in self.players:
                self.errors.append('第%d行: 场次 %s 进球球员未注册: %s' % (lineno, mid, p))
            elif self.players[p] != home:
                self.errors.append('第%d行: 场次 %s 进球球员 %s 不属于主队 %s'
                                   % (lineno, mid, p, home))
            if p in self.suspended:
                valid_goals -= 1
                self.notices.append('第%d行: 场次 %s 停赛球员 %s 进球无效，%s 有效比分主分 %d'
                                    % (lineno, mid, p, home, max(valid_goals, 0)))
        self.matches[mid] = dict(home=home, away=away, hs=max(valid_goals, 0), as_=as_,
                                 status='active', fixed=False)

    def cmd_CARD(self, lineno, args):
        mid, player = self._arity(lineno, args, 2, 'CARD <场次> <球员名>')
        if mid not in self.matches:
            self.errors.append('第%d行: 黄牌引用不存在的场次: %s' % (lineno, mid))
            return
        if player not in self.players:
            self.errors.append('第%d行: 黄牌引用未注册球员: %s' % (lineno, player))
            return
        self.yellow[player] = self.yellow.get(player, 0) + 1
        n = self.yellow[player]
        if n >= YELLOW_CARD_SUSPENSION_THRESHOLD and player not in self.suspended:
            self.suspended.add(player)
            self.notices.append('第%d行: 球员 %s 累积 %d 张黄牌，停赛后续场次'
                                % (lineno, player, n))

    def cmd_FIX(self, lineno, args):
        if len(args) < 2:
            self.errors.append('第%d行: 参数不足: FIX <场次> confirm|revoke ...' % lineno)
            return
        mid, action = args[0], args[1].lower()
        if mid not in self.matches:
            self.errors.append('第%d行: 改判引用不存在的场次: %s' % (lineno, mid))
            return
        m = self.matches[mid]
        if m['fixed']:
            self.errors.append('第%d行: 场次 %s 已被改判过，拒绝重复改判' % (lineno, mid))
            return
        if action == 'confirm':
            if len(args) != 4:
                self.errors.append('第%d行: 参数不足: FIX <场次> confirm <新主分> <新客分>' % lineno)
                return
            hs, as_ = int(args[2]), int(args[3])
            if hs < 0 or as_ < 0:
                self.errors.append('第%d行: 改判分数非法（负分）: 场次 %s %d:%d'
                                   % (lineno, mid, hs, as_))
                return
            m['hs'], m['as_'], m['fixed'] = hs, as_, True
            self.notices.append('第%d行: 场次 %s 改判确认为 %d:%d，积分榜级联重算'
                                % (lineno, mid, hs, as_))
        elif action == 'revoke':
            m['status'], m['fixed'] = 'revoked', True
            self.notices.append('第%d行: 场次 %s 已撤销，积分回退并级联重排' % (lineno, mid))
        else:
            self.errors.append('第%d行: 未知改判动作: %s（应为 confirm/revoke）' % (lineno, action))

    def _arity(self, lineno, args, n, usage):
        if len(args) != n:
            self.errors.append('第%d行: 参数个数错误，用法: %s' % (lineno, usage))
            raise _SkipLine
        return args

    # ---------- 积分榜计算（每次全量级联重算） ----------

    def _table(self):
        st = {t: dict(P=0, W=0, D=0, L=0, GF=0, GA=0, Pts=0) for t in self.teams}
        for m in self.matches.values():
            if m['status'] != 'active':
                continue
            h, a = st[m['home']], st[m['away']]
            h['P'] += 1; a['P'] += 1
            h['GF'] += m['hs']; h['GA'] += m['as_']
            a['GF'] += m['as_']; a['GA'] += m['hs']
            if m['hs'] > m['as_']:
                h['W'] += 1; a['L'] += 1; h['Pts'] += WIN_POINTS
            elif m['hs'] < m['as_']:
                a['W'] += 1; h['L'] += 1; a['Pts'] += WIN_POINTS
            else:
                h['D'] += 1; a['D'] += 1
                h['Pts'] += DRAW_POINTS; a['Pts'] += DRAW_POINTS
        return st

    def _head_to_head(self, group):
        """相互战绩小联赛：组内 -> [积分, 净胜球]"""
        mini = {t: [0, 0] for t in group}
        for m in self.matches.values():
            if m['status'] != 'active':
                continue
            if m['home'] in mini and m['away'] in mini:
                d = m['hs'] - m['as_']
                mini[m['home']][1] += d
                mini[m['away']][1] -= d
                if d > 0:
                    mini[m['home']][0] += WIN_POINTS
                elif d < 0:
                    mini[m['away']][0] += WIN_POINTS
                else:
                    mini[m['home']][0] += DRAW_POINTS
                    mini[m['away']][0] += DRAW_POINTS
        return mini

    def standings(self):
        """返回 (table, [(rank, team), ...])；完全同键者并列排名。"""
        st = self._table()
        gd = {t: st[t]['GF'] - st[t]['GA'] for t in st}
        order = sorted(st, key=lambda t: (-st[t]['Pts'], -gd[t], -st[t]['GF'], t))
        rows = []  # (team, 并列判定键)
        i = 0
        while i < len(order):
            j = i
            while (j + 1 < len(order)
                   and st[order[j + 1]]['Pts'] == st[order[i]]['Pts']
                   and gd[order[j + 1]] == gd[order[i]]):
                j += 1
            group = order[i:j + 1]
            if len(group) > 1:
                mini = self._head_to_head(group)
                group.sort(key=lambda t: (-mini[t][0], -mini[t][1], -st[t]['GF'], t))
                rows += [(t, (st[t]['Pts'], gd[t], mini[t][0], mini[t][1], st[t]['GF']))
                         for t in group]
            else:
                rows += [(group[0], (st[group[0]]['Pts'], gd[group[0]], st[group[0]]['GF']))]
            i = j + 1
        ranked, last_key, last_rank = [], None, 0
        for pos, (t, key) in enumerate(rows, 1):
            if key != last_key:
                last_key, last_rank = key, pos
            ranked.append((last_rank, t))
        return st, ranked

    # ---------- 输出 ----------

    def report(self):
        st, ranked = self.standings()
        out = ['========== 积分榜 ==========']
        header = ['排名', '球队', '场次', '胜', '平', '负', '进球', '失球', '净胜球', '积分']
        lines = []
        for rank, t in ranked:
            s = st[t]
            lines.append([str(rank), t, str(s['P']), str(s['W']), str(s['D']), str(s['L']),
                          str(s['GF']), str(s['GA']), '%+d' % (s['GF'] - s['GA']), str(s['Pts'])])
        widths = [max(_w(header[c]), *(_w(r[c]) for r in lines)) for c in range(len(header))]
        out.append('  '.join(_pad(h, widths[i]) for i, h in enumerate(header)))
        for r in lines:
            out.append('  '.join(_pad(c, widths[i]) for i, c in enumerate(r)))
        out.append('')
        out.append('========== 停赛 / 无效进球 / 改判轨迹 ==========')
        out += self.notices or ['（无）']
        out.append('')
        out.append('========== 错误报告 ==========')
        out += self.errors or ['（无）']
        return '\n'.join(out)


class _SkipLine(Exception):
    pass


def _w(s):
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in s)


def _pad(s, width):
    return s + ' ' * max(0, width - _w(s))


DEMO = """\
# ===== 球队定义 =====
TEAM 雄鹰
TEAM 猛虎
TEAM 蛟龙
TEAM 雪豹
# ===== 球员定义 =====
PLAYER 张伟 雄鹰
PLAYER 李强 雄鹰
PLAYER 王磊 猛虎
PLAYER 赵鹏 蛟龙
PLAYER 陈晨 雪豹
# ===== 赛果流 =====
MATCH 1 雄鹰 猛虎 1 0 张伟
CARD 1 张伟
MATCH 2 蛟龙 雪豹 0 0 -
CARD 2 张伟
# 张伟累积 2 黄 -> 停赛；场次 3 其进球无效（雄鹰 2:0 -> 有效 1:0）
MATCH 3 雄鹰 蛟龙 2 0 张伟,李强
MATCH 4 猛虎 雪豹 0 1 -
MATCH 5 雪豹 雄鹰 0 3 -
# ===== 改判流 =====
FIX 4 confirm 0 0
FIX 5 revoke
FIX 99 confirm 1 0
FIX 1 confirm -1 0
FIX 4 revoke
"""


def run(lines):
    league = League()
    for lineno, line in enumerate(lines, 1):
        try:
            league.feed(lineno, line)
        except _SkipLine:
            pass
    return league.report()


def main(argv):
    if len(argv) > 1 and argv[1] == '--demo':
        print('---------- 示例输入 ----------')
        print(DEMO)
        print(run(DEMO.splitlines()))
    elif len(argv) > 1:
        with open(argv[1], encoding='utf-8') as f:
            print(run(f.read().splitlines()))
    else:
        print(run(sys.stdin.read().splitlines()))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))

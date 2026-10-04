#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""影视版权授权状态机与冲突检测工具（纯 Python 标准库，单文件）。

用法：
    python3 license_tool.py [输入文件]      # 省略输入文件时从标准输入读取

输入格式（每行一条指令，字段以空白分隔，# 开头为注释，空行忽略）：

    REGION  <地区名> <全国|地区>                        定义地区及其是否全国
    LICENSE <剧名> <平台> <地区> <开始> <结束> <状态>    授权事件
    CHECK   <剧名> <平台> <地区>                        冲突检查

    日期格式为 YYYY-MM-DD；状态 ∈ {意向, 生效, 到期, 解约}。
    三类指令可以任意混排，处理严格按行序进行，状态跨流延续。

规则：
  * 授权以 (剧名, 平台, 地区) 为唯一标识，状态机为
        意向 -> 生效 -> 到期 | 解约
    非法跳转（如 意向->到期、到期->生效、重复意向等）被拦截并报告。
  * 新授权首次出现必须为「意向」，其窗口在首次出现时确定。
  * 冲突判定：同剧同平台、授权窗口重叠，且地区范围重叠
    （同一地区，或任一方为全国授权）。冲突会被报告并定位到具体授权与重叠区间。
  * 优先级（自定义）：先「生效」者优先。后生效且冲突的授权进入「挂起」。
  * 授权「到期」即失效；「解约」后其窗口自动释放，被挂起的授权
    在冲突解除时自动恢复生效（每次状态变化都会全量重查）。
  * 开始期晚于结束期、引用不存在的地区或剧名、重复冲突检查均会被报告。

输出：冲突检查结果、授权状态清单、状态变迁提示、错误报告。

------------------------------ 输入示例 ------------------------------
    REGION 全国 全国
    REGION 华东 地区
    REGION 华南 地区

    LICENSE 剧A 平台X 全国 2026-06-01 2026-09-30 意向
    LICENSE 剧A 平台X 全国 2026-06-01 2026-09-30 生效
    LICENSE 剧A 平台X 华东 2026-01-01 2026-12-31 意向
    LICENSE 剧A 平台X 华东 2026-01-01 2026-12-31 生效
    LICENSE 剧B 平台Y 华南 2026-03-01 2026-05-31 意向
    LICENSE 剧B 平台Y 华南 2026-03-01 2026-05-31 生效
    LICENSE 剧B 平台Y 华南 2026-03-01 2026-05-31 到期
    LICENSE 剧A 平台X 全国 2026-06-01 2026-09-30 解约

    CHECK 剧A 平台X 华东
    CHECK 剧A 平台X 华东
    CHECK 剧C 平台X 华东

    LICENSE 剧B 平台Y 华南 2026-03-01 2026-05-31 生效
    LICENSE 剧D 平台Z 华北 2026-01-01 2026-06-30 意向
    LICENSE 剧E 平台Z 华东 2026-12-31 2026-01-01 意向

------------------------------ 输出示例 ------------------------------
    ====== 冲突检查结果 ======
    行 14 | CHECK 剧A/平台X/华东 -> 相关授权 2 条：
        剧A/平台X/全国 2026-06-01~2026-09-30 [解约]
        剧A/平台X/华东 2026-01-01~2026-12-31 [生效]
        当前无生效冲突
    ====== 授权状态清单 ======
    剧A | 平台X | 全国 | 2026-06-01 ~ 2026-09-30 | 解约
    剧A | 平台X | 华东 | 2026-01-01 ~ 2026-12-31 | 生效
    剧B | 平台Y | 华南 | 2026-03-01 ~ 2026-05-31 | 到期
    ====== 状态变迁提示 ======
    行 8  | 授权 剧A/平台X/华东 ... 与全国授权冲突，挂起
    行 12 | 授权 剧A/平台X/华东 ... 冲突解除，自动恢复生效
    ====== 错误报告 ======
    行 15 | 重复冲突检查：剧A/平台X/华东
    行 16 | 引用不存在的剧名：剧C
    行 18 | 非法状态跳转：剧B/平台Y/华南 到期 -> 生效
    行 19 | 引用不存在的地区：华北
    行 20 | 授权区间非法：开始期 2026-12-31 晚于结束期 2026-01-01
（以上为示意，实际输出以运行结果为准）
"""

import sys
from datetime import date

INTENT, EFFECTIVE, EXPIRED, TERMINATED = "意向", "生效", "到期", "解约"
STATUSES = (INTENT, EFFECTIVE, EXPIRED, TERMINATED)

ALLOWED_TRANSITIONS = {
    INTENT: {EFFECTIVE},
    EFFECTIVE: {EXPIRED, TERMINATED},
    EXPIRED: set(),
    TERMINATED: set(),
}

NATIONAL_TRUE = {"全国", "是"}
NATIONAL_FALSE = {"地区", "否"}


class License:
    __slots__ = ("drama", "platform", "region", "start", "end",
                 "status", "seq", "suspended", "line")

    def __init__(self, drama, platform, region, start, end, line):
        self.drama = drama
        self.platform = platform
        self.region = region
        self.start = start
        self.end = end
        self.status = INTENT
        self.seq = 0          # 生效顺序号，越小优先级越高
        self.suspended = False
        self.line = line      # 首次定义所在行

    @property
    def key(self):
        return (self.drama, self.platform, self.region)

    def label(self):
        return "%s/%s/%s %s~%s（定义于行 %d）" % (
            self.drama, self.platform, self.region,
            self.start, self.end, self.line)

    def status_text(self):
        return self.status + ("（挂起）" if self.suspended else "")


class Engine:
    def __init__(self):
        self.regions = {}          # 地区名 -> 是否全国
        self.licenses = {}         # key -> License
        self.order = []            # 授权创建顺序
        self.errors = []           # (行号, 消息)
        self.infos = []            # (行号, 消息)
        self.check_results = []    # (行号, 剧, 平台, 地区, 结果文本)
        self.seen_checks = set()
        self.reported_pairs = set()
        self.seq_counter = 0
        self.line_no = 0

    # ---------- 基础工具 ----------

    def err(self, msg):
        self.errors.append((self.line_no, msg))

    def info(self, msg):
        self.infos.append((self.line_no, msg))

    def parse_date(self, text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            self.err("日期格式非法：%r（应为 YYYY-MM-DD）" % text)
            return None

    def scope_overlap(self, region_a, region_b):
        """地区范围是否重叠：同名地区，或任一方为全国。"""
        return (region_a == region_b
                or self.regions.get(region_a, False)
                or self.regions.get(region_b, False))

    def conflict(self, a, b):
        return (a.drama == b.drama
                and a.platform == b.platform
                and a.start <= b.end and b.start <= a.end
                and self.scope_overlap(a.region, b.region))

    # ---------- 冲突重查 ----------

    def recompute(self):
        """全量重查：按生效顺序（优先级）重放，判定挂起/恢复并报告新冲突。"""
        effective = sorted((lic for lic in self.licenses.values()
                            if lic.status == EFFECTIVE),
                           key=lambda lic: lic.seq)
        active = []
        for lic in effective:
            rivals = [a for a in active if self.conflict(lic, a)]
            if rivals:
                if not lic.suspended:
                    self.info("授权 %s 与更高优先级授权冲突，挂起" % lic.label())
                lic.suspended = True
                for a in rivals:
                    pair = frozenset((lic.key, a.key))
                    if pair not in self.reported_pairs:
                        self.reported_pairs.add(pair)
                        lo, hi = max(lic.start, a.start), min(lic.end, a.end)
                        self.err("授权冲突：%s 与 %s 同平台窗口重叠"
                                 "（重叠区间 %s~%s），按生效先后优先级后者挂起"
                                 % (a.label(), lic.label(), lo, hi))
            else:
                if lic.suspended:
                    self.info("授权 %s 冲突解除，自动恢复生效" % lic.label())
                lic.suspended = False
                active.append(lic)

    # ---------- 指令处理 ----------

    def cmd_region(self, name, flag):
        if name in self.regions:
            self.err("地区重复定义：%s" % name)
            return
        if flag in NATIONAL_TRUE:
            self.regions[name] = True
        elif flag in NATIONAL_FALSE:
            self.regions[name] = False
        else:
            self.err("地区 %s 的「是否全国」字段非法：%r（应为 全国/地区）"
                     % (name, flag))

    def cmd_license(self, drama, platform, region, start_s, end_s, status):
        if region not in self.regions:
            self.err("引用不存在的地区：%s" % region)
            return
        if status not in STATUSES:
            self.err("未知授权状态：%r（应为 意向/生效/到期/解约）" % status)
            return
        start = self.parse_date(start_s)
        end = self.parse_date(end_s)
        if start is None or end is None:
            return
        if start > end:
            self.err("授权区间非法：开始期 %s 晚于结束期 %s" % (start, end))
            return

        key = (drama, platform, region)
        lic = self.licenses.get(key)
        if lic is None:
            if status != INTENT:
                self.err("非法状态跳转：新授权 %s/%s/%s 必须先为「意向」，"
                         "不能直接进入「%s」" % (drama, platform, region, status))
                return
            lic = License(drama, platform, region, start, end, self.line_no)
            self.licenses[key] = lic
            self.order.append(key)
            return

        if status not in ALLOWED_TRANSITIONS[lic.status]:
            self.err("非法状态跳转：%s/%s/%s %s -> %s"
                     % (drama, platform, region, lic.status, status))
            return

        lic.status = status
        if status == EFFECTIVE:
            self.seq_counter += 1
            lic.seq = self.seq_counter
        elif status == EXPIRED:
            lic.suspended = False
            self.info("授权 %s 到期失效" % lic.label())
        elif status == TERMINATED:
            lic.suspended = False
            self.info("授权 %s 解约，窗口释放" % lic.label())
        self.recompute()

    def cmd_check(self, drama, platform, region):
        ok = True
        if region not in self.regions:
            self.err("引用不存在的地区：%s" % region)
            ok = False
        if not any(lic.drama == drama for lic in self.licenses.values()):
            self.err("引用不存在的剧名：%s" % drama)
            ok = False
        triple = (drama, platform, region)
        if triple in self.seen_checks:
            self.err("重复冲突检查：%s/%s/%s" % triple)
            ok = False
        if not ok:
            return
        self.seen_checks.add(triple)

        matched = [lic for lic in self.licenses.values()
                   if lic.drama == drama and lic.platform == platform
                   and self.scope_overlap(lic.region, region)]
        if not matched:
            self.check_results.append(
                (self.line_no, drama, platform, region, "无相关授权"))
            return

        lines = ["相关授权 %d 条：" % len(matched)]
        for lic in sorted(matched, key=lambda l: (l.region, l.start)):
            lines.append("    %s/%s/%s %s~%s [%s]"
                         % (lic.drama, lic.platform, lic.region,
                            lic.start, lic.end, lic.status_text()))
        live = [lic for lic in matched if lic.status == EFFECTIVE]
        pairs = []
        for i in range(len(live)):
            for j in range(i + 1, len(live)):
                if self.conflict(live[i], live[j]):
                    pairs.append((live[i], live[j]))
        if pairs:
            for a, b in pairs:
                lo, hi = max(a.start, b.start), min(a.end, b.end)
                lines.append("    冲突：%s/%s 与 %s/%s 窗口重叠 %s~%s"
                             % (a.platform, a.region, b.platform, b.region,
                                lo, hi))
        else:
            lines.append("    当前无生效冲突")
        self.check_results.append(
            (self.line_no, drama, platform, region, "\n".join(lines)))

    # ---------- 主流程 ----------

    def run(self, lines):
        for self.line_no, raw in enumerate(lines, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            cmd, args = parts[0], parts[1:]
            if cmd == "REGION" and len(args) == 2:
                self.cmd_region(*args)
            elif cmd == "LICENSE" and len(args) == 6:
                self.cmd_license(*args)
            elif cmd == "CHECK" and len(args) == 3:
                self.cmd_check(*args)
            elif cmd in ("REGION", "LICENSE", "CHECK"):
                self.err("指令 %s 字段数量错误（收到 %d 个参数）"
                         % (cmd, len(args)))
            else:
                self.err("无法识别的指令：%s" % cmd)

    def report(self, out):
        w = out.write
        w("====== 冲突检查结果 ======\n")
        if self.check_results:
            for line_no, drama, platform, region, text in self.check_results:
                w("行 %-3d | CHECK %s/%s/%s -> %s\n"
                  % (line_no, drama, platform, region, text))
        else:
            w("（无冲突检查）\n")

        w("====== 授权状态清单 ======\n")
        if self.order:
            for key in self.order:
                lic = self.licenses[key]
                w("%s | %s | %s | %s ~ %s | %s\n"
                  % (lic.drama, lic.platform, lic.region,
                     lic.start, lic.end, lic.status_text()))
        else:
            w("（无授权记录）\n")

        w("====== 状态变迁提示 ======\n")
        if self.infos:
            for line_no, msg in self.infos:
                w("行 %-3d | %s\n" % (line_no, msg))
        else:
            w("（无）\n")

        w("====== 错误报告 ======\n")
        if self.errors:
            for line_no, msg in self.errors:
                w("行 %-3d | %s\n" % (line_no, msg))
        else:
            w("（无错误）\n")


def main(argv):
    if len(argv) > 2:
        sys.stderr.write("用法：python3 license_tool.py [输入文件]\n")
        return 2
    if len(argv) == 2:
        with open(argv[1], encoding="utf-8") as f:
            lines = f.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()
    engine = Engine()
    engine.run(lines)
    engine.report(sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

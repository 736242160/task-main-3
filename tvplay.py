#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tvplay.py — 电视台节目单播出监测与应急垫片级联工具（纯 Python 标准库，单文件）

输入文件分三个区段（# 起为注释，空行忽略）：

  [programs]  节目定义，每行：节目名称 时长（秒，或 mm:ss / hh:mm:ss）
  [schedule]  节目单流，每行：顺序号 节目名称 频道
  [events]    事件流（播出流与中断流按时间先后合并，跨流状态延续）：
                播出 频道 开始|结束 顺序号
                中断 频道 故障|恢复

级联规则（自定，确定性）：
  R1 故障发生在播出中：当前节目悬停，立即切入应急垫片（--filler 秒），
     后续节目级联顺延一个垫片时长。
  R2 中断期间收到"播出开始"：报错 E_FAULT，该节目不进播出链路，
     改播垫片（再顺延一个垫片时长），节目进入级联顺延队列。
  R3 恢复：被悬停节目自动接续播出（从头重播），级联队列中的节目
     按顺序号依次排在其后；队列节目后续收到自己的"开始"事件时正常播出。
  R4 播出顺序以"该频道节目单中尚未播出的最小顺序号"为期望，
     不符则报 E_ORDER 并定位（事件行号 + 节目单位置）。
  R5 播出引用不存在的节目单条目报 E_REF；同一顺序号重复播出报 E_DUP。
  R6 结束无在播、结束顺序号与在播不符、重复故障、未故障恢复等报 E_STATE。

退出码：0 无错误；1 有播出错误；2 输入文件解析失败。
"""
import argparse
import sys
from dataclasses import dataclass

E_REF = "E_REF"      # 播出引用不存在的节目单条目
E_DUP = "E_DUP"      # 同一顺序号重复播出
E_ORDER = "E_ORDER"  # 播出顺序与节目单不符
E_FAULT = "E_FAULT"  # 中断期间播出
E_STATE = "E_STATE"  # 状态机错误
E_PROG = "E_PROG"    # 节目单引用未定义节目


def parse_duration(text):
    """时长：秒数 或 mm:ss 或 hh:mm:ss。"""
    try:
        nums = [int(p) for p in text.split(":")]
    except ValueError:
        raise ValueError("非法时长 %r" % text)
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    raise ValueError("非法时长 %r" % text)


def fmt_clock(seconds, day_start):
    t = (day_start + seconds) % 86400
    return "%02d:%02d:%02d" % (t // 3600, (t % 3600) // 60, t % 60)


@dataclass
class ScheduleEntry:
    order: int
    program: str
    channel: str
    line: int


@dataclass
class Event:
    kind: str     # 'play' | 'intr'
    channel: str
    action: str   # 开始 / 结束 / 故障 / 恢复
    order: object # 播出事件为 int，中断事件为 None
    line: int


def load(path):
    programs, schedule, events = {}, [], []
    section = None
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip().lower()
                if section not in ("programs", "schedule", "events"):
                    raise ValueError("第%d行：未知区段 [%s]" % (lineno, section))
                continue
            if section is None:
                raise ValueError("第%d行：内容必须位于某个区段之内" % lineno)
            parts = line.split()
            if section == "programs":
                if len(parts) != 2:
                    raise ValueError("第%d行：节目定义格式应为：名称 时长" % lineno)
                programs[parts[0]] = parse_duration(parts[1])
            elif section == "schedule":
                if len(parts) != 3:
                    raise ValueError("第%d行：节目单格式应为：顺序号 节目 频道" % lineno)
                schedule.append(ScheduleEntry(int(parts[0]), parts[1], parts[2], lineno))
            else:  # events
                if parts[0] == "播出":
                    if len(parts) != 4 or parts[2] not in ("开始", "结束"):
                        raise ValueError("第%d行：播出事件格式应为：播出 频道 开始|结束 顺序号" % lineno)
                    events.append(Event("play", parts[1], parts[2], int(parts[3]), lineno))
                elif parts[0] == "中断":
                    if len(parts) != 3 or parts[2] not in ("故障", "恢复"):
                        raise ValueError("第%d行：中断事件格式应为：中断 频道 故障|恢复" % lineno)
                    events.append(Event("intr", parts[1], parts[2], None, lineno))
                else:
                    raise ValueError("第%d行：未知事件类型 %r" % (lineno, parts[0]))
    return programs, schedule, events


class ChannelState:
    def __init__(self, name):
        self.name = name
        self.mode = "IDLE"      # IDLE / ON_AIR / FAULT
        self.current = None     # 在播顺序号
        self.suspended = None   # 被故障悬停的顺序号
        self.pending = []       # 级联顺延队列（顺序号）
        self.aired = set()      # 已开始播出过的顺序号
        self.delay = 0          # 累计顺延秒数
        self.clock = 0          # 频道虚拟播出时钟（秒）


class Simulator:
    def __init__(self, programs, schedule, events, filler, day_start):
        self.programs = programs
        self.events = events
        self.filler = filler
        self.day_start = day_start
        self.log = []
        self.errors = []
        self.channels = {}
        # 节目单索引
        self.sched_by_ch = {}
        self.sched_index = {}
        for e in schedule:
            self.sched_by_ch.setdefault(e.channel, []).append(e)
            self.sched_index[(e.channel, e.order)] = e
        for lst in self.sched_by_ch.values():
            lst.sort(key=lambda e: e.order)
        # 装载期检查：节目单引用未定义节目
        for e in schedule:
            if e.program not in programs:
                self.error(e.line, e.channel, E_PROG,
                           "节目单 #%d 引用未定义节目《%s》（节目单第%d行）"
                           % (e.order, e.program, e.line))

    # ---- 输出辅助 ----
    def error(self, line, channel, code, msg):
        self.errors.append((line, channel, code, msg))
        self.log.append("L%-4d [%s] %s !! %s %s"
                        % (line, code, channel, code, msg))

    def info(self, line, channel, msg):
        self.log.append("L%-4d [%s] %s %s"
                        % (line, fmt_clock(self.ch(channel).clock, self.day_start),
                           channel, msg))

    def ch(self, name):
        if name not in self.channels:
            self.channels[name] = ChannelState(name)
        return self.channels[name]

    def expected_order(self, channel):
        for e in self.sched_by_ch.get(channel, []):
            if e.order not in self.ch(channel).aired:
                return e
        return None

    def sched_pos(self, channel, order):
        lst = self.sched_by_ch.get(channel, [])
        for i, e in enumerate(lst, 1):
            if e.order == order:
                return i, len(lst)
        return None, len(lst)

    # ---- 事件处理 ----
    def run(self):
        for ev in self.events:
            if ev.kind == "play" and ev.action == "开始":
                self.on_start(ev)
            elif ev.kind == "play":
                self.on_end(ev)
            elif ev.action == "故障":
                self.on_fault(ev)
            else:
                self.on_recover(ev)
        return self.errors

    def on_start(self, ev):
        st = self.ch(ev.channel)
        entry = self.sched_index.get((ev.channel, ev.order))
        if entry is None:
            self.error(ev.line, ev.channel, E_REF,
                       "播出顺序号#%d 引用不存在的节目单条目（频道 %s 节目单中无此顺序号）"
                       % (ev.order, ev.channel))
            return
        if ev.order in st.aired:
            self.error(ev.line, ev.channel, E_DUP,
                       "顺序号#%d《%s》重复播出：该顺序号此前已开始播出过"
                       % (ev.order, entry.program))
            return
        exp = self.expected_order(ev.channel)
        if exp is not None and exp.order != ev.order:
            pos, total = self.sched_pos(ev.channel, ev.order)
            epos, _ = self.sched_pos(ev.channel, exp.order)
            self.error(ev.line, ev.channel, E_ORDER,
                       "播出顺序与节目单不符：期望 #%d《%s》（节目单第%d/%d位），"
                       "实际 #%d《%s》（第%s/%d位）"
                       % (exp.order, exp.program, epos, total,
                          ev.order, entry.program, pos, total))
        if st.mode == "FAULT":
            st.pending.append(ev.order)
            st.delay += self.filler
            st.clock += self.filler
            self.error(ev.line, ev.channel, E_FAULT,
                       "中断期间播出：频道故障中，顺序号#%d《%s》改播应急垫片(%ds)，"
                       "节目进入级联顺延队列[%s]，累计顺延 %ds"
                       % (ev.order, entry.program, self.filler,
                          ",".join("#%d" % o for o in st.pending), st.delay))
            return
        if st.mode == "ON_AIR":
            cur = self.sched_index[(ev.channel, st.current)]
            self.error(ev.line, ev.channel, E_STATE,
                       "顺序号#%d《%s》未结束即开始 #%d，强制切出"
                       % (st.current, cur.program, ev.order))
            st.clock += self.programs.get(cur.program, 0)
        st.mode = "ON_AIR"
        st.current = ev.order
        st.aired.add(ev.order)
        if ev.order in st.pending:
            st.pending.remove(ev.order)
        self.info(ev.line, ev.channel,
                  "开始播出 #%d《%s》（时长 %ds）"
                  % (ev.order, entry.program, self.programs.get(entry.program, 0)))

    def on_end(self, ev):
        st = self.ch(ev.channel)
        if st.mode == "FAULT":
            self.error(ev.line, ev.channel, E_FAULT,
                       "中断期间收到结束：频道故障中，顺序号#%d 的结束无效" % ev.order)
            return
        if st.current is None:
            self.error(ev.line, ev.channel, E_STATE,
                       "结束顺序号#%d 但当前无在播节目" % ev.order)
            return
        entry = self.sched_index.get((ev.channel, st.current))
        if st.current != ev.order:
            self.error(ev.line, ev.channel, E_STATE,
                       "结束顺序号#%d 与在播顺序号#%d 不符，强制结束在播节目"
                       % (ev.order, st.current))
        dur = self.programs.get(entry.program, 0) if entry else 0
        self.info(ev.line, ev.channel,
                  "结束播出 #%d《%s》" % (st.current, entry.program if entry else "?"))
        st.clock += dur
        st.current = None
        st.mode = "IDLE"

    def on_fault(self, ev):
        st = self.ch(ev.channel)
        if st.mode == "FAULT":
            self.error(ev.line, ev.channel, E_STATE, "重复故障：频道已处于故障状态")
            return
        st.mode = "FAULT"
        if st.current is not None:
            entry = self.sched_index.get((ev.channel, st.current))
            st.suspended = st.current
            st.current = None
            st.delay += self.filler
            st.clock += self.filler
            self.info(ev.line, ev.channel,
                      "故障中断：在播 #%d《%s》悬停，切入应急垫片(%ds)，"
                      "后续节目级联顺延，累计顺延 %ds"
                      % (st.suspended, entry.program if entry else "?",
                         self.filler, st.delay))
        else:
            self.info(ev.line, ev.channel, "故障：频道空闲，垫片待命")

    def on_recover(self, ev):
        st = self.ch(ev.channel)
        if st.mode != "FAULT":
            self.error(ev.line, ev.channel, E_STATE, "未故障却收到恢复")
            return
        st.mode = "IDLE"
        if st.suspended is not None:
            order = st.suspended
            st.suspended = None
            entry = self.sched_index.get((ev.channel, order))
            st.mode = "ON_AIR"
            st.current = order
            st.aired.add(order)
            queue = "，级联队列[%s]依次顺延排后" % ",".join("#%d" % o for o in st.pending) \
                    if st.pending else ""
            self.info(ev.line, ev.channel,
                      "恢复：接续播出 #%d《%s》（从头重播）%s，累计顺延 %ds"
                      % (order, entry.program if entry else "?", queue, st.delay))
        else:
            self.info(ev.line, ev.channel, "恢复：频道就绪")

    # ---- 报告 ----
    def report(self):
        out = []
        out.append("========== 播出状态日志 ==========")
        out.extend(self.log)
        out.append("")
        out.append("========== 频道最终状态 ==========")
        mode_cn = {"IDLE": "空闲", "ON_AIR": "播出中", "FAULT": "故障"}
        for name in sorted(self.channels):
            st = self.channels[name]
            cur = "#%s" % st.current if st.current is not None else "-"
            pend = "[%s]" % ",".join("#%d" % o for o in st.pending) if st.pending else "[]"
            aired = "[%s]" % ",".join("#%d" % o for o in sorted(st.aired))
            out.append("%s: 状态=%s 在播=%s 已播=%s 级联队列=%s 累计顺延=%ds 播出时钟=%s"
                       % (name, mode_cn[st.mode], cur, aired, pend, st.delay,
                          fmt_clock(st.clock, self.day_start)))
            # 级联顺延计划
            t = st.clock
            if st.current is not None:
                e = self.sched_index.get((name, st.current))
                t += self.programs.get(e.program, 0) if e else 0
            for o in st.pending:
                e = self.sched_index.get((name, o))
                dur = self.programs.get(e.program, 0) if e else 0
                out.append("    顺延计划: #%d《%s》预计 %s 播出"
                           % (o, e.program if e else "?", fmt_clock(t, self.day_start)))
                t += dur
        out.append("")
        out.append("========== 错误清单 ==========")
        if not self.errors:
            out.append("无错误。")
        else:
            out.append("共 %d 个错误：" % len(self.errors))
            for i, (line, chn, code, msg) in enumerate(self.errors, 1):
                out.append("[%d] 第%d行 %s %s：%s" % (i, line, chn, code, msg))
        return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="电视台节目单播出监测与应急垫片级联工具（纯标准库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("input", help="输入文件（含 [programs]/[schedule]/[events] 三区段）")
    ap.add_argument("--filler", type=int, default=300,
                    help="应急垫片时长（秒），默认 300")
    ap.add_argument("--day-start", default="00:00:00",
                    help="播出时钟起点 HH:MM:SS，默认 00:00:00")
    args = ap.parse_args(argv)
    try:
        day_start = parse_duration(args.day_start)
        programs, schedule, events = load(args.input)
    except (ValueError, OSError) as exc:
        print("输入解析失败：%s" % exc, file=sys.stderr)
        return 2
    sim = Simulator(programs, schedule, events, args.filler, day_start)
    sim.run()
    print(sim.report())
    return 1 if sim.errors else 0


if __name__ == "__main__":
    sys.exit(main())

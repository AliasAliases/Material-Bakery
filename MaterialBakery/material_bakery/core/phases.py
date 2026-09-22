# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   阶段计时：运行期间每一个阶段都在日志里留一条**带耗时**的行
#
#   为什么需要（用户实测）:
#       他 8/8 烘完之后 Blender 整整 729 秒没有响应，内存还是平的，最后只能强杀。
#       而我们那份日志里，那 729 秒是**一片空白** —— 只有开场一行和结尾一行，
#       中间是黑箱，于是只能靠猜（猜了四个原因，没有一个能被证实）。
#
#   现在每个阶段自己报时:
#       [wizard +12.34s | +0.05s] commit: start
#       [wizard +18.90s | +6.56s] commit: done
#       左边是"距本轮开始"，右边是"距上一条"。谁慢一眼就能看出来，
#       下次再卡住，现场自己就留在磁盘上那份日志里了。
#
#   ⚠ 这是**纯日志**设施：不碰场景、不写 IDProperty、没有返回值。
#     唯一的模块级状态是"当前这一轮"，只为算相对时间用。两轮烘焙同时跑会互相
#     覆盖 —— 但两轮烘焙本来就不允许同时存在（ui/session.py 会拒绝）。
#
#   ⚠ 日志的落点是可以换的（set_sink）：GUI 里 `compat.log` 只打到 Blender 的
#     控制台，用户看不到，所以 Session 会把 sink 换成自己的 add_log，
#     这样阶段行会一起进会话日志和落盘文件 —— 那份文件才是事后能查的东西。
# ------------------------------------------------------------------------------------

import time
from contextlib import contextmanager

from .. import compat


class PhaseClock:
    """一个阶段的秒表。每写一行都带上"距本轮开始"和"距上一条"两个差值。"""

    def __init__(self, label="run"):
        self.label = label or "run"
        self.origin = time.time()
        self.last = self.origin
        self.started = {}          # 未结束的阶段名 -> 开始时刻
        self.rows = []             # [(阶段名, 秒数)] —— 测试要能直接断言
        self.lines = []            # 写出去的原文，方便测试/报告复用

    # -- 时间 -------------------------------------------------------------------

    def since_start(self):
        return time.time() - self.origin

    def since_last(self):
        return time.time() - self.last

    # -- 写日志 -----------------------------------------------------------------

    def mark(self, message):
        """写一条普通阶段行"""
        now = time.time()
        elapsed = now - self.origin
        delta = now - self.last
        self.last = now
        line = "[{} +{:.2f}s | +{:.2f}s] {}".format(self.label, elapsed, delta, message)
        self.lines.append(line)
        _write(line)
        return line

    def begin(self, name):
        """开始一个阶段（重复 begin 会以最后一次为准）"""
        self.started[name] = time.time()
        self.mark("{}: start".format(name))

    def end(self, name, extra=""):
        """结束一个阶段，并记下它花了多久。返回秒数。"""
        started = self.started.pop(name, None)
        seconds = (time.time() - started) if started is not None else 0.0
        self.rows.append((name, seconds))
        tail = " {}".format(extra) if extra else ""
        self.mark("{}: done in {:.2f}s{}".format(name, seconds, tail))
        return seconds

    @contextmanager
    def phase(self, name, extra=""):
        """with clock.phase("commit"): ... —— 异常也照样记耗时"""
        self.begin(name)
        try:
            yield self
        finally:
            self.end(name, extra)

    def total(self, name):
        """某个阶段累计花了多少秒（同名阶段会累加）"""
        return sum(seconds for row_name, seconds in self.rows if row_name == name)

    def slowest(self, limit=5):
        return sorted(self.rows, key=lambda row: -row[1])[:limit]

    def summary(self):
        if not self.rows:
            return "no phase recorded yet"
        rows = ", ".join("{} {:.2f}s".format(name, seconds)
                         for name, seconds in self.slowest(limit=4))
        return "slowest: {}".format(rows)


# ------------------------------------------------------------------------------------
#   落点

_sink = None


def set_sink(sink):
    """换掉日志落点：callable(text)。None 表示回到 compat.log（控制台）。"""
    global _sink
    _sink = sink


def _write(line):
    sink = _sink
    if sink is not None:
        try:
            sink(line)
            return
        except Exception:                     # 日志绝不允许把烘焙搞挂
            pass
    compat.log(line)


# ------------------------------------------------------------------------------------
#   当前这一轮

_current = None
_ambient = None


def _ambient_clock():
    """没有活动运行时的秒表 —— 让事务之类单独被调用时也有相对时间可读"""
    global _ambient
    if _ambient is None:
        _ambient = PhaseClock("idle")
    return _ambient


def current():
    return _current


def start_run(label="run"):
    """开一轮。返回这一轮的时钟。"""
    global _current
    _current = PhaseClock(label)
    _current.mark("run started")
    return _current


def end_run(note=""):
    """收一轮。返回这一轮的时钟（可能为 None）。"""
    global _current
    clock = _current
    _current = None
    if clock is not None:
        clock.mark("run finished{}".format(" — " + note if note else ""))
    return clock


def clock():
    return _current if _current is not None else _ambient_clock()


def mark(message):
    return clock().mark(message)


def begin(name):
    return clock().begin(name)


def end(name, extra=""):
    return clock().end(name, extra)


def phase(name, extra=""):
    return clock().phase(name, extra)

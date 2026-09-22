# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   运行报告：纯 Python 对象，烘完才落盘
#
#   ⚠ 绝不要在烘焙任务运行期间把报告写进 IDProperty。
#   旧引擎的崩溃（IDP_CopyProperty_ex use-after-free）就是这么来的：
#   派发 bake 之后再往场景写字符串 IDProperty，撞上 bake job 线程对 depsgraph
#   的 COW 拷贝。报告只在 job 完全结束后才序列化。
# ------------------------------------------------------------------------------------

import json
import os
import time


STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_CANCELLED = "cancelled"


class TaskResult:
    __slots__ = ("group", "type_key", "type_label", "size_x", "size_y", "image_name",
                 "filepath", "status", "error", "seconds", "objects", "surgery")

    def __init__(self, group="", type_key="", type_label="", size_x=0, size_y=0,
                 image_name="", filepath="", status=STATUS_PENDING, error="",
                 seconds=0.0, objects=()):
        self.group = group
        self.type_key = type_key
        self.type_label = type_label
        # ⚠ 两条边：非正方形贴图从 2026-09 起是常态。size_y 为 0 时按正方形处理。
        self.size_x = int(size_x or 0)
        self.size_y = int(size_y or 0) or self.size_x
        self.image_name = image_name
        self.filepath = filepath
        self.status = status
        self.error = error
        self.seconds = seconds
        self.objects = list(objects)
        self.surgery = []          # 节点手术做了什么（每个材质一句）

    @property
    def size(self):
        """兼容旧调用点与旧报告：正方形尺寸取长边。

        ⚠ 只用于显示/排序。判断"是不是非正方形"请看 size_x != size_y。
        """
        return max(self.size_x, self.size_y)

    @property
    def size_label(self):
        from ..core import naming
        return naming.size_label(self.size_x, self.size_y)

    def to_dict(self):
        return {
            "group": self.group,
            "type": self.type_key,
            "label": self.type_label,
            "size": self.size,
            "size_x": self.size_x,
            "size_y": self.size_y,
            "image": self.image_name,
            "filepath": self.filepath,
            "status": self.status,
            "error": self.error,
            "seconds": round(self.seconds, 3),
            "objects": list(self.objects),
            "surgery": list(self.surgery),
        }

    @classmethod
    def from_dict(cls, data):
        # ⚠ 旧报告只有一条 `size`（正方形时代的）。先读 size_x/size_y，
        #   没有就退回按正方形处理 —— 老的报告 JSON 照样能读回来。
        legacy = data.get("size", 0)
        result = cls(
            group=data.get("group", ""),
            type_key=data.get("type", ""),
            type_label=data.get("label", ""),
            size_x=data.get("size_x", legacy),
            size_y=data.get("size_y", legacy),
            image_name=data.get("image", ""),
            filepath=data.get("filepath", ""),
            status=data.get("status", STATUS_PENDING),
            error=data.get("error", ""),
            seconds=data.get("seconds", 0.0),
            objects=data.get("objects", ()),
        )
        result.surgery = list(data.get("surgery", []))
        return result

    def __repr__(self):
        return "<TaskResult {} {} {}>".format(self.group, self.type_key, self.status)


class RunReport:
    """一次烘焙运行的完整结果

    report.to_dict() 可以直接存进预设/场景属性，也可以拿来做像素级比对的基线。
    """

    FORMAT = 1

    def __init__(self, label=""):
        self.label = label
        self.started = 0.0
        self.finished = 0.0
        self.results = []
        self.warnings = []
        self.skipped = []          # [(组名, 原因), ...]
        self.conflicts = []
        self.exported = []         # [(文件路径, 结果数), ...]
        self.cancelled = False
        self.fatal = ""
        self.planned = 0           # 计划里的任务总数（取消时 results 会比它少）

    # -- 记账 -------------------------------------------------------------------

    def add(self, result):
        self.results.append(result)
        return result

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def skip(self, group, reason):
        self.skipped.append((group, reason))

    def start(self):
        self.started = time.time()

    def stop(self):
        self.finished = time.time()

    @property
    def seconds(self):
        if not self.started:
            return 0.0
        end = self.finished or time.time()
        return end - self.started

    # -- 统计 -------------------------------------------------------------------

    def count(self, status):
        return sum(1 for r in self.results if r.status == status)

    @property
    def total(self):
        return len(self.results)

    @property
    def done(self):
        return self.count(STATUS_DONE)

    @property
    def failed(self):
        return self.count(STATUS_FAILED)

    @property
    def ok(self):
        return self.failed == 0 and not self.fatal and not self.cancelled

    def failed_results(self):
        return [r for r in self.results if r.status == STATUS_FAILED]

    def group_names(self):
        names = []
        for result in self.results:
            if result.group not in names:
                names.append(result.group)
        return names

    # -- 展示 -------------------------------------------------------------------

    def summary(self):
        """一行结论，给 UI 和日志用"""
        if self.fatal:
            return "Failed: {}".format(self.fatal)
        planned = self.planned or self.total
        if self.cancelled:
            return "Cancelled — {}/{} maps baked".format(self.done, planned)
        text = "{} of {} maps baked in {:.1f}s".format(self.done, planned, self.seconds)
        if self.failed:
            text += " ({} failed)".format(self.failed)
        return text

    def details(self, limit=20):
        """多行明细，失败项排前面"""
        lines = [self.summary()]
        for result in self.failed_results()[:limit]:
            lines.append("  FAILED  {} {} — {}".format(
                result.group, result.type_label, result.error or "unknown error"))
        for group, reason in self.skipped[:limit]:
            lines.append("  SKIPPED {} — {}".format(group, reason))
        for message in self.warnings[:limit]:
            lines.append("  NOTE    {}".format(message))
        return lines

    # -- 序列化 -----------------------------------------------------------------

    def to_dict(self):
        return {
            "format": self.FORMAT,
            "label": self.label,
            "started": self.started,
            "finished": self.finished,
            "cancelled": self.cancelled,
            "fatal": self.fatal,
            "planned": self.planned,
            "results": [r.to_dict() for r in self.results],
            "warnings": list(self.warnings),
            "skipped": [list(pair) for pair in self.skipped],
            "conflicts": list(self.conflicts),
            "exported": [list(pair) for pair in self.exported],
        }

    def to_json(self):
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data):
        report = cls(label=data.get("label", ""))
        report.started = data.get("started", 0.0)
        report.finished = data.get("finished", 0.0)
        report.cancelled = data.get("cancelled", False)
        report.fatal = data.get("fatal", "")
        report.planned = data.get("planned", 0)
        report.results = [TaskResult.from_dict(d) for d in data.get("results", [])]
        report.warnings = list(data.get("warnings", []))
        report.skipped = [tuple(p) for p in data.get("skipped", [])]
        report.conflicts = list(data.get("conflicts", []))
        report.exported = [tuple(p) for p in data.get("exported", [])]
        return report

    @classmethod
    def from_json(cls, text):
        return cls.from_dict(json.loads(text))

    # -- 落盘（第 4 页可以存一份，也可以拿它当"基线"做对照） --------------------

    def save_to(self, path):
        """存成 JSON。原子写：先写临时文件再替换，中途断电不会留下半个文件。"""
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(self.to_json())
        os.replace(temporary, path)
        return path

    @classmethod
    def load_from(cls, path):
        with open(path, "r", encoding="utf-8") as handle:
            return cls.from_json(handle.read())

    def __repr__(self):
        return "<RunReport {}>".format(self.summary())

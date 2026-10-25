# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   逐像素对照
#
#   这是唯一能证明"重写没改坏行为"的手段：同一个场景，原版 Auto Bake 和新版各烘一遍，
#   把导出的 PNG 逐像素比。
#
#   比对规则（重要）：
#       - 尺寸不同 -> 直接判为差异（不是"缩放后差不多"，那就是不一样）
#       - 每像素每通道差超过 tolerance 才算差异像素
#       - 统计最大差、平均差、差异像素比例
#       - 差异像素比例低于阈值（默认 0.5%）就认为"实质一致" ——
#         烘焙本身有采样噪声，一两个像素的抖动不是回归
#
#   ⚠ 读图不走 image.pixels[:]（60 万像素的 list 复制很慢），
#     用 foreach_get 直接读到 buffer。
# ------------------------------------------------------------------------------------

import os

import bpy

from .. import compat


DEFAULT_TOLERANCE = 1.0 / 255.0      # 一个色阶
DEFAULT_MAX_DIFF_RATIO = 0.005       # 0.5% 的像素不同就算不一致


class ImageDiff:
    __slots__ = ("name", "path_a", "path_b", "size_a", "size_b", "pixels",
                 "max_delta", "mean_delta", "diff_ratio", "error")

    def __init__(self, name, path_a="", path_b=""):
        self.name = name
        self.path_a = path_a
        self.path_b = path_b
        self.size_a = (0, 0)
        self.size_b = (0, 0)
        self.pixels = 0
        self.max_delta = 0.0
        self.mean_delta = 0.0
        self.diff_ratio = 0.0
        self.error = ""

    @property
    def same_size(self):
        return self.size_a == self.size_b

    def matches(self, tolerance=DEFAULT_TOLERANCE,
                max_diff_ratio=DEFAULT_MAX_DIFF_RATIO):
        if self.error or not self.same_size:
            return False
        if self.max_delta <= tolerance:
            return True
        return self.diff_ratio <= max_diff_ratio

    def summary(self):
        if self.error:
            return "{}: ERROR {}".format(self.name, self.error)
        if not self.same_size:
            return "{}: size {} vs {}".format(self.name, self.size_a, self.size_b)
        return "{}: max {:.4f}, mean {:.5f}, {:.2%} of pixels differ".format(
            self.name, self.max_delta, self.mean_delta, self.diff_ratio)

    def to_dict(self):
        return {
            "name": self.name, "path_a": self.path_a, "path_b": self.path_b,
            "size_a": list(self.size_a), "size_b": list(self.size_b),
            "max_delta": self.max_delta, "mean_delta": self.mean_delta,
            "diff_ratio": self.diff_ratio, "error": self.error,
        }


def _read_pixels(image):
    """把图像像素读进一个 list（大图也不慢）

    ⚠ 读之前强制把色彩空间设成 Non-Color。
       PNG 文件里不存色彩空间信息，重新加载时 Blender 一律按 sRGB 认定 ——
       float/16 位的数据贴图（法线、粗糙度）就会被 sRGB 解码一次，
      0.37 读出来变成 0.1128。那是**读取方式**造成的差异，不是烘焙结果的差异。
      对照要做的是"两个文件里存的数字一样不一样"，所以按原始数值读。
    """
    try:
        image.colorspace_settings.name = "Non-Color"
    except (TypeError, RuntimeError):
        pass
    width, height = image.size
    buffer = [0.0] * (width * height * 4)
    image.pixels.foreach_get(buffer)
    return buffer


def compare_image_files(name, path_a, path_b, tolerance=DEFAULT_TOLERANCE):
    """比对两个图片文件"""
    diff = ImageDiff(name, path_a, path_b)
    for path, attr in ((path_a, "size_a"), (path_b, "size_b")):
        if not path or not os.path.isfile(path):
            diff.error = "missing file: {}".format(path or "(none)")
            return diff

    image_a = image_b = None
    try:
        image_a = bpy.data.images.load(path_a, check_existing=False)
        image_b = bpy.data.images.load(path_b, check_existing=False)
        diff.size_a = tuple(image_a.size)
        diff.size_b = tuple(image_b.size)
        if not diff.same_size:
            return diff

        pixels_a = _read_pixels(image_a)
        pixels_b = _read_pixels(image_b)
        diff.pixels = len(pixels_a) // 4

        total = 0.0
        worst = 0.0
        differing = 0
        for index in range(0, len(pixels_a), 4):
            pixel_delta = 0.0
            for channel in range(3):            # alpha 不参与比对
                delta = abs(pixels_a[index + channel] - pixels_b[index + channel])
                if delta > pixel_delta:
                    pixel_delta = delta
            total += pixel_delta
            if pixel_delta > worst:
                worst = pixel_delta
            if pixel_delta > tolerance:
                differing += 1

        diff.max_delta = worst
        diff.mean_delta = (total / diff.pixels) if diff.pixels else 0.0
        diff.diff_ratio = (differing / diff.pixels) if diff.pixels else 0.0
    except (RuntimeError, OSError) as exc:
        diff.error = str(exc).strip()
    finally:
        for image in (image_a, image_b):
            if image is not None:
                try:
                    bpy.data.images.remove(image)
                except (RuntimeError, ReferenceError):
                    pass
    return diff


class CompareResult:
    def __init__(self, label=""):
        self.label = label
        self.diffs = []
        self.only_in_a = []
        self.only_in_b = []
        self.error = ""

    def add(self, diff):
        self.diffs.append(diff)

    @property
    def compared(self):
        return len([d for d in self.diffs if not d.error and d.same_size])

    def mismatches(self, tolerance=DEFAULT_TOLERANCE,
                   max_diff_ratio=DEFAULT_MAX_DIFF_RATIO):
        return [d for d in self.diffs
                if not d.matches(tolerance, max_diff_ratio)]

    @property
    def ok(self):
        return (not self.error and not self.only_in_a and not self.only_in_b
                and not self.mismatches())

    def summary(self):
        if self.error:
            return "Compare failed: {}".format(self.error)
        text = "{} compared, {} differ".format(self.compared, len(self.mismatches()))
        if self.only_in_a or self.only_in_b:
            text += " ({} only in reference, {} only in new)".format(
                len(self.only_in_a), len(self.only_in_b))
        return text

    def details(self, limit=12):
        lines = [self.summary()]
        for name in self.only_in_a[:limit]:
            lines.append("  REFERENCE ONLY  {}".format(name))
        for name in self.only_in_b[:limit]:
            lines.append("  NEW ONLY        {}".format(name))
        for diff in self.mismatches()[:limit]:
            lines.append("  DIFFERS         {}".format(diff.summary()))
        return lines

    def to_dict(self):
        return {"label": self.label, "error": self.error,
                "only_in_a": list(self.only_in_a), "only_in_b": list(self.only_in_b),
                "diffs": [d.to_dict() for d in self.diffs]}


def _index_by_name(paths):
    """{图片名（不含扩展名）: 路径}"""
    index = {}
    for path in paths:
        stem = os.path.splitext(os.path.basename(path))[0]
        index[stem] = path
    return index


def compare_folders(label, folder_a, folder_b, tolerance=DEFAULT_TOLERANCE,
                    pattern=".png", recursive=True):
    """比对两个目录里的同名图片

    folder_a 是基线（原版烘的），folder_b 是新版烘的。
    """
    result = CompareResult(label)
    if not os.path.isdir(folder_a) or not os.path.isdir(folder_b):
        result.error = "one of the folders does not exist"
        return result

    def collect(root):
        found = []
        if recursive:
            for current, _dirs, files in os.walk(root):
                for name in files:
                    if name.lower().endswith(pattern):
                        found.append(os.path.join(current, name))
        else:
            for name in os.listdir(root):
                path = os.path.join(root, name)
                if os.path.isfile(path) and name.lower().endswith(pattern):
                    found.append(path)
        return found

    index_a = _index_by_name(collect(folder_a))
    index_b = _index_by_name(collect(folder_b))

    for name in sorted(index_a):
        if name not in index_b:
            result.only_in_a.append(name)
    for name in sorted(index_b):
        if name not in index_a:
            result.only_in_b.append(name)
    for name in sorted(set(index_a) & set(index_b)):
        result.add(compare_image_files(name, index_a[name], index_b[name], tolerance))
    compat.log("对照 {}: {}".format(label, result.summary()))
    return result


def report_to_folders(report_a, report_b):
    """用两份报告里的导出路径做对照（不用扫目录）"""
    result = CompareResult("reports")
    by_name_b = {r.image_name: r.filepath for r in report_b.results if r.filepath}
    for entry in report_a.results:
        if not entry.filepath:
            continue
        other = by_name_b.get(entry.image_name)
        if not other:
            result.only_in_a.append(entry.image_name)
            continue
        result.add(compare_image_files(entry.image_name, entry.filepath, other))
    names_a = {r.image_name for r in report_a.results if r.filepath}
    for entry in report_b.results:
        if entry.filepath and entry.image_name not in names_a:
            result.only_in_b.append(entry.image_name)
    return result

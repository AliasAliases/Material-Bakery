# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   贴图导出（第 4 页的延迟导出）
#
#   为什么不用 save_render()：save_render 会过一遍色彩管理（view transform），
#   非颜色的数据贴图（法线 / 粗糙度）会被改数值。这里直接 filepath_raw + save()，
#   写的是一点不改的原始数据。
# ------------------------------------------------------------------------------------

import os
import shutil

import bpy

from .. import compat
from ..core import naming

# ------------------------------------------------------------------------------------
#   导出清单（实施轮 1.5）
#
#   ⚠ 存在的理由是一个**真崩溃**（用户真机上撞到的）：
#     `ui/ops.py:_require_report()` 在"从磁盘扫回来的贴图"这种状态下也放行
#     （设计上"扫完就是烘完的界面"），**而那种状态里 `report` 是 None**；
#     导出代码却在第一行就摸 `report.results` →
#         AttributeError: 'NoneType' object has no attribute 'results'
#   所以导出的输入从此是**一张清单**：真烘过就用 `report.results`，
#   救援 / 重启后就用 `settings.textures`（持久列表，扫目录与"认出已有材质"都会填它）。
#   `report` 只用来补信息，为 None 时不再炸。
#
#   ⚠ alpha 相关的旧机制（1.4 的勾选框、1.5 的按钮、1.5b 的 `_NoAlpha` 后缀）**全部删掉**：
#     base_color 那张图在**烘完写盘之前**就已经带 alpha 了（`core/alpha_merge.py`），
#     所以导出这一步什么都不用做 —— 见 DESIGN §39–§42。
# ------------------------------------------------------------------------------------

class ExportOutcome:
    __slots__ = ("name", "path", "ok", "message", "notes")

    def __init__(self, name, path, ok, message="", notes=None):
        self.name = name
        self.path = path
        self.ok = ok
        self.message = message
        # [(级别, 说明), ...] —— 合并做了什么 / 为什么没做。
        # ⚠ 一定要有人把它写进日志：勾了却没合并是最典型的"静默失败"。
        self.notes = list(notes or ())

    def __repr__(self):
        return "<{} {} {}>".format("OK" if self.ok else "FAIL", self.name, self.path)


def target_path(image_name, file_format, directory):
    return os.path.join(directory,
                        "{}.{}".format(image_name,
                                       compat.file_extension(file_format)))


def export_one(image, directory, file_format='PNG', name=None, overwrite=True):
    """把一张图写到磁盘。返回 ExportOutcome。"""
    if image is None:
        return ExportOutcome(name or "?", "", False, "no image")
    clean_name = naming.sanitize_filename(name or image.name)
    if not directory:
        return ExportOutcome(clean_name, "", False, "no output folder")

    path = target_path(clean_name, file_format, directory)
    if os.path.exists(path) and not overwrite:
        return ExportOutcome(clean_name, path, False, "file exists (overwrite is off)")

    if not os.path.isdir(directory):
        try:
            os.makedirs(directory)
        except OSError as exc:
            return ExportOutcome(clean_name, path, False, str(exc))

    # ⚠ 大图写盘之后 Blender 会释放像素缓冲区（has_data 变 False），
    #   这时再 save() 只会得到一句看不懂的 "does not have any image data"。
    #   默认不 pack 之后（2026-09 起）这种情况很常见 —— 而图其实就在磁盘上，
    #   所以**直接拷贝文件**就行，不需要像素。
    if not image.has_data and image.packed_file is None:
        source = bpy.path.abspath(image.filepath) if image.filepath else ""
        same = source and os.path.normcase(source) == os.path.normcase(path)
        if source and os.path.isfile(source):
            if same:
                # 本来就在目标位置上，而且文件确实在 —— 什么都不用做
                return ExportOutcome(clean_name, path, True)
            try:
                shutil.copyfile(source, path)
                return ExportOutcome(clean_name, path, True)
            except OSError as exc:
                return ExportOutcome(clean_name, path, False, str(exc))
        # ⚠ 注意这里**不能**因为"路径相同"就当作成功：路径可能早就设过，
        #   但文件并不在（第一版就是这么错报成功的 —— 测试里 3 张图报"导出成功"
        #   结果磁盘上只有 1 张）。
        return ExportOutcome(
            clean_name, path, False,
            "pixel data is not in memory and there is no file to copy — set an output "
            "folder, or turn on 'Pack Textures Into .blend', and bake again")

    try:
        image.filepath_raw = path
        image.file_format = file_format
        image.save()
    except (OSError, RuntimeError) as exc:
        return ExportOutcome(clean_name, path, False, str(exc).strip())
    return ExportOutcome(clean_name, path, True)


# ------------------------------------------------------------------------------------
#   导出清单（实施轮 1.5）
#
#   ⚠ 存在的理由是一个**真崩溃**（用户真机上撞到的）：
#     `ui/ops.py:_require_report()` 在"从磁盘扫回来的贴图"这种状态下也放行
#     （设计上"扫完就是烘完的界面"），**而那种状态里 `report` 是 None**；
#     导出代码却在第一行就摸 `report.results` →
#         AttributeError: 'NoneType' object has no attribute 'results'
#   所以导出的输入从此是**一张清单**：真烘过就用 `report.results`，
#   救援 / 重启后就用 `settings.textures`（持久列表，扫目录与"认出已有材质"都会填它）。
#   `report` 只用来补信息，为 None 时不再炸。
# ------------------------------------------------------------------------------------

class ExportItem:
    """导出清单的一项 —— 纯数据，测试可以直接造（不碰任何 bpy 对象）"""

    __slots__ = ("image_name", "group", "type_key")

    def __init__(self, image_name, group="", type_key=""):
        self.image_name = image_name
        self.group = group
        self.type_key = type_key

    def __repr__(self):
        return "<ExportItem {} {}>".format(self.group or "-", self.image_name)


def manifest_from_report(report):
    """真烘过：清单 = 这一次运行的**成功**结果"""
    if report is None:
        return []
    return [ExportItem(result.image_name, result.group, result.type_key)
            for result in report.results if result.status == "done"]


def manifest_from_settings(settings):
    """救援 / 重启后：贴图列表就是清单

    ⚠ 这份列表是**持久**的（挂在场景的属性组上、随 .blend 保存），
      `scan_output()`（扫目录）与 `detect_existing()`（认出文件里已有的烘焙材质）
      都会填它 —— 所以重开文件之后它仍然是"用户眼前的那份清单"。
    """
    items = []
    for row in getattr(settings, "textures", ()) or ():
        name = getattr(row, "image_name", "")
        if name:
            items.append(ExportItem(name, row.group, row.type_key))
    return items


def build_manifest(report, settings=None):
    """清单来源二选一：① 真烘过 → `report.results`；② 否则 → 贴图列表

    ⚠ 不能反过来把 `settings.textures` 当第一来源：第 4 页每画一次都会调
      `detect_existing()`，而它**会清空并重填**这份列表（还没有交付材质时就是空的）——
      报告才是"这一轮烘了什么"的权威记录。
    """
    if report is not None:
        return manifest_from_report(report)
    return manifest_from_settings(settings)


def manifest_names(manifest):
    """清单里的图名（要判断"有没有东西可导出"时用）"""
    return [entry.image_name for entry in manifest or ()]


# ------------------------------------------------------------------------------------
#   导出
# ------------------------------------------------------------------------------------

def export_report(manifest, directory, file_format='PNG', group_subfolders=True,
                  only=None, overwrite=True):
    """把清单里的贴图全部导出（**每张各导各的**，导出这一步不做任何加工）。

    manifest : `[ExportItem]`（见 `build_manifest`）—— ⚠ **不是** report：
               救援状态下根本没有 report，这正是 1.5 修的那个崩溃
    directory: 根目录
    group_subfolders: 每个集合一个子文件夹
    only     : 只导出这些 image_name（第 4 页逐张勾选）；None 表示全部

    ⚠ 实施轮 1.6：合并已经**不在导出这一步**了 —— base_color 那张图在烘完写盘之前
      就已经把同组 alpha 的灰度并进 A（见 `core/alpha_merge.py`），所以导出只是把
      内存里那张原样写到磁盘，文件名就是 `image_name`（**不带任何后缀**）。
    """
    outcomes = []
    for entry in manifest or ():
        if only is not None and entry.image_name not in only:
            continue
        image = bpy.data.images.get(entry.image_name)
        if image is None:
            outcomes.append(ExportOutcome(entry.image_name, "", False,
                                          "image datablock is gone"))
            continue
        folder = directory
        if group_subfolders and entry.group:
            folder = os.path.join(directory, naming.sanitize_filename(entry.group))
        outcomes.append(export_one(image, folder, file_format,
                                   name=entry.image_name, overwrite=overwrite))
    return outcomes


def exported_names(manifest, only=None):
    """这次导出**真的会尝试**写哪些图名（用来判断"清单与勾选有没有交集"）"""
    return [entry.image_name for entry in manifest or ()
            if only is None or entry.image_name in only]


def clear_directory(directory, extensions=("png", "jpg", "jpeg", "tif", "tiff", "exr",
                                           "bmp", "tga", "webp")):
    """清掉输出目录里上一次的贴图（只删我们认识的图片扩展名）"""
    removed = 0
    if not directory or not os.path.isdir(directory):
        return removed
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if os.path.isdir(path):
            continue
        if name.rsplit(".", 1)[-1].lower() in extensions:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    return removed


def folder_size(directory):
    total = 0
    if not directory or not os.path.isdir(directory):
        return 0
    for root, _dirs, files in os.walk(directory):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def human_size(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "{:.1f} {}".format(size, unit) if unit != "B" else "{} B".format(size)
        size /= 1024.0
    return "{} B".format(size)

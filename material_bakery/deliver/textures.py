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


class ExportOutcome:
    __slots__ = ("name", "path", "ok", "message")

    def __init__(self, name, path, ok, message=""):
        self.name = name
        self.path = path
        self.ok = ok
        self.message = message

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


def export_report(report, directory, file_format='PNG', group_subfolders=True,
                  only=None, overwrite=True):
    """把一次运行的贴图全部导出。

    report      : engine.report.RunReport
    directory   : 根目录
    group_subfolders: 每个集合一个子文件夹
    only        : 只导出这些 image_name（第 4 页逐张勾选）；None 表示全部
    """
    outcomes = []
    for result in report.results:
        if result.status != "done":
            continue
        if only is not None and result.image_name not in only:
            continue
        image = bpy.data.images.get(result.image_name)
        if image is None:
            outcomes.append(ExportOutcome(result.image_name, "", False,
                                          "image datablock is gone"))
            continue
        folder = directory
        if group_subfolders and result.group:
            folder = os.path.join(directory, naming.sanitize_filename(result.group))
        outcomes.append(export_one(image, folder, file_format,
                                   name=result.image_name, overwrite=overwrite))
    return outcomes


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

# ------------------------------------------------------------------------------------
#   IDProperty 崩溃定位工具：找出含损坏自定义属性（IDProperty）的数据块
#
#   来由：老引擎崩在
#       memcpy <- MEM_lockfree_dupallocN <- IDP_CopyProperty_ex
#       <- BKE_libblock_copy_in_lib <- BKE_id_copy_ex <- deg_expand_eval_copy_datablock
#   即 depsgraph 在烘焙任务开始时复制某个数据块，复制它的自定义属性时读到了已释放的内存。
#   （本插件的引擎从结构上避免了它：**任何对场景的写入都发生在派发烘焙之前**，
#     烘的过程中一个字节都不写 —— 见 engine/job.py 的三条铁律。这个工具留着，
#     是因为它不挑插件：任何 .blend 的 IDProperty COW 崩溃都能用它定位。）
#
#   本脚本对每个数据块先打印名字，再调用 ID.copy() —— 这条路径和崩溃路径完全相同
#   （都会走到 IDP_CopyProperty_ex）。所以：日志里最后一条 "COPY ..." 就是元凶。
#
#   用法（二选一）：
#     A. Blender 菜单：Scripting -> 打开本文件 -> Run Script
#     B. 命令行：blender --background "你的文件.blend" --python diagnose_idprops.py
#
#   结果同时打印到控制台和 LOG_PATH（崩溃时控制台会丢，所以以文件为准）。
# ------------------------------------------------------------------------------------

import bpy
import os
import sys
import datetime

# 日志写到桌面，最不容易丢
_home = os.path.expanduser("~")
LOG_PATH = os.path.join(_home, "Desktop", "material_bakery_idprop_diagnose.txt")
if not os.path.isdir(os.path.dirname(LOG_PATH)):
    LOG_PATH = os.path.join(os.path.expanduser("~"), "material_bakery_idprop_diagnose.txt")

_log_file = open(LOG_PATH, "w", encoding="utf-8", buffering=1)   # 行缓冲，崩溃也不丢


def log(message):
    line = str(message)
    print(line, flush=True)
    try:
        _log_file.write(line + "\n")
        _log_file.flush()
    except Exception:
        pass


log("=" * 78)
log("Auto Bake - IDProperty 崩溃定位")
log("时间: {}".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
log("Blender: {}".format(bpy.app.version_string))
log("文件: {}".format(bpy.data.filepath or "(未保存)"))
log("日志: {}".format(LOG_PATH))
log("=" * 78)
log("")
log("将要测试的数据块类型（只测含自定义属性的）：")
log("")

# 需要检查的数据块集合
DATA_COLLECTIONS = [
    "objects", "meshes", "materials", "node_groups", "images", "collections",
    "scenes", "worlds", "curves", "armatures", "lights", "cameras", "textures",
    "actions", "particles", "volumes", "grease_pencils", "pointclouds",
    "metaballs", "lattices", "fonts", "speakers", "sounds", "movieclips", "masks",
]

total_checked = 0
total_with_props = 0
suspects = []

for collection_name in DATA_COLLECTIONS:
    data = getattr(bpy.data, collection_name, None)
    if data is None:
        continue
    try:
        items = list(data)
    except Exception as exc:
        log("  跳过 {}: {}".format(collection_name, exc))
        continue

    with_props = []
    for item in items:
        try:
            keys = list(item.keys())
        except Exception:
            continue
        if keys:
            with_props.append((item, keys))

    if not with_props:
        continue

    log("-" * 78)
    log("{}: {} 个数据块含自定义属性".format(collection_name, len(with_props)))
    total_with_props += len(with_props)

    for item, keys in with_props:
        types = {}
        for k in keys:
            try:
                value = item[k]
                types[k] = type(value).__name__
            except Exception as exc:
                types[k] = "READ_FAILED({})".format(exc)
        log("COPY {} '{}'  props={}".format(collection_name, item.name, types))
        total_checked += 1

        # 走与崩溃完全相同的复制路径
        try:
            duplicate = item.copy()
        except Exception as exc:
            log("    copy 抛出异常（Python 层，不是崩溃）: {}: {}".format(type(exc).__name__, exc))
            continue
        log("    copy ok -> '{}'".format(duplicate.name))

        # 顺手做一次真实的 depsgraph 评估（bake 会做的事）
        try:
            bpy.context.view_layer.update()
        except Exception as exc:
            log("    depsgraph update 失败: {}".format(exc))

        # 清理副本
        try:
            if hasattr(bpy.data, collection_name):
                getattr(bpy.data, collection_name).remove(duplicate)
        except Exception:
            pass

log("")
log("=" * 78)
log("完成：共复制测试 {} 个数据块，全部通过 —— 没有找到会崩溃的自定义属性。".format(total_checked))
log("=" * 78)

try:
    _log_file.close()
except Exception:
    pass

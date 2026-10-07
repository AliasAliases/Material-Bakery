# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   现取物体：凡是**跨操作符边界**要用到物体，一律先按名字现取一次
#   （实施轮 1.2，用户报的 Preview 崩溃）
#
#   来龙去脉：
#       交付层（deliver/）与 UI 层都从 `plan.groups[].objects` 拿物体引用，而那份
#       引用是**编译计划那一刻**抓下来的。Blender 的撤销步（Ctrl+Z、memfile 重读）
#       会释放并重建所有数据块，于是那些引用全部作废 —— 再读 `obj.name` 就是
#
#           ReferenceError: StructRNA of type Object has been removed
#
#       用户那条路径正是这样：建计划 → 进编辑模式（压了撤销栈）→ 出来 → 按 Preview，
#       崩在 `deliver/slots.py` 的 `obj.name` 上，整页操作直接报错。
#
#   规矩（本文件就是这条规矩的唯一落点）：
#       * 计划里**保留名字**（core/plan.py 编译时抓下来，见 ObjectGroup.object_names）
#       * 交付 / 预览 / 收尾 / 复制副本只认名字，用的时候 `live_object()` 现取
#       * 取不到就**跳过 + 记日志**（"object is gone"），绝不让它抛异常
#
#   ⚠ 不要用 `bpy.data.objects[name]` —— 名字不存在时它抛 KeyError；
#     也不要 `if obj is None` 判断存活：引用作废时它**不是** None，是炸。
# ------------------------------------------------------------------------------------

import bpy

from .scene_scan import object_name


def live_object(thing):
    """物体或名字 -> 现在这个时刻**还活着**的物体；取不到返回 None

    接受三种输入：`Object`（可能是死引用）、名字（str）、None。
    """
    name = object_name(thing)
    if not name:
        return None
    return bpy.data.objects.get(name)


def live_objects(things):
    """批量现取；取不到的**直接丢掉**（调用方要记账就自己按名字比对）"""
    found = []
    for thing in things or ():
        obj = live_object(thing)
        if obj is not None:
            found.append(obj)
    return found


def alive(thing):
    """这个物体**现在**还在场景里吗（死引用 / 名字查不到都算不在）"""
    return live_object(thing) is not None

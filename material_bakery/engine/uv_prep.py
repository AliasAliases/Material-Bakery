# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   Prepare UV —— 「帮我展 UV」这一步
#
#   规则（按用户要求）:
#       开关默认关闭 —— 绝不擅自给模型重展 UV
#       开关打开时，**只给没有 UV 的物体**展，已经有 UV 的一律不碰
#       重叠只在预检里报警，永远不阻止烘焙
# ------------------------------------------------------------------------------------

import bpy

from .. import compat
from . import uv_overlap


def objects_missing_uv(objects):
    return [o for o in objects if o.type == 'MESH' and o.data and not o.data.uv_layers]


def ensure_uvs(context, objects, angle_limit=1.15, island_margin=0.02, uv_name="UVMap"):
    """给缺 UV 的物体展 UV。返回被展过的物体名列表。

    已经有 UV 的物体直接跳过 —— 用户明确要求不要动已展好的模型。
    """
    targets = objects_missing_uv(objects)
    if not targets:
        return []

    unwrapped = []
    for obj in targets:
        if obj.name not in bpy.data.objects:
            continue
        try:
            _unwrap_one(context, obj, angle_limit, island_margin, uv_name)
            unwrapped.append(obj.name)
        except Exception as exc:
            compat.log("展 UV 失败 {}: {}".format(obj.name, exc))
    return unwrapped


def _unwrap_one(context, obj, angle_limit, island_margin, uv_name):
    view_layer = context.view_layer
    for other in view_layer.objects:
        other.select_set(False)
    obj.select_set(True)
    view_layer.objects.active = obj

    before = obj.mode
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        bpy.ops.mesh.select_all(action='SELECT')
        compat.call_operator(
            bpy.ops.uv.smart_project,
            angle_limit=angle_limit,
            island_margin=island_margin,
            correct_aspect=True,
            scale_to_bounds=False,
        )
    finally:
        bpy.ops.object.mode_set(mode=before if before != 'EDIT' else 'OBJECT')

    if obj.data.uv_layers:
        obj.data.uv_layers[0].name = uv_name


def report_overlap(objects, resolution=uv_overlap.DEFAULT_RESOLUTION):
    """预检：谁的重叠面积偏大。只用于提示，不参与决策。"""
    findings = []
    for obj in objects:
        stats = uv_overlap.scan_object(obj, resolution=resolution)
        if stats is None:
            continue
        if stats.overlap_ratio > 0.001:
            findings.append(stats)
    findings.sort(key=lambda s: s.overlap_ratio, reverse=True)
    return findings


# ------------------------------------------------------------------------------------
#   UDIM：临时平移 UV
#
#   烘一张瓦片时，把这组物体的 UV 整体平移 -(列, 行)，让**目标瓦片**落到 0..1，
#   其它瓦片的 UV 就跑到 0..1 之外 —— Blender 的烘焙只写 0..1 范围内的像素，
#   于是这一张图里只有目标瓦片的内容。烘完精确还原。
#
#   为什么不用 TILED 图像：实测在 --background 下，纯内存造出来的 tiled 图
#   烘焙一定报 "Uninitialized image"；用磁盘文件加载出来的 tiled 图能烘，
#   但逐瓦片读回像素会触发从占位文件重新加载，导出的全是黑图。
#   平铺图像这条路在本工程里是 100% 验证过的（对照组烘焙出纯红），所以走它。
# ------------------------------------------------------------------------------------

class UVSnapshot:
    """UV 快照。还原是**精确**的（存的是原始浮点值，不是反向平移）。"""

    __slots__ = ("entries",)

    def __init__(self):
        self.entries = []          # [(物体名, 层名, [u0, v0, u1, v1, ...]), ...]

    def __len__(self):
        return len(self.entries)


def render_layer_name(obj):
    """这个物体当前用于渲染的 UV 层的名字"""
    from . import uv_pack
    layer = uv_pack.render_uv_layer(obj)
    return layer.name if layer is not None else ""


def snapshot_uvs(objects):
    """把一组物体的渲染 UV 层记下来"""
    snapshot = UVSnapshot()
    for obj in objects:
        if obj is None or obj.type != 'MESH' or obj.data is None:
            continue
        name = render_layer_name(obj)
        if not name:
            continue
        layer = obj.data.uv_layers.get(name)
        if layer is None:
            continue
        flat = []
        for item in layer.data:
            flat.append(item.uv[0])
            flat.append(item.uv[1])
        snapshot.entries.append((obj.name, name, flat))
    return snapshot


def shift_uvs(objects, du, dv):
    """把渲染 UV 层整体平移 (du, dv)"""
    moved = 0
    for obj in objects:
        if obj is None or obj.type != 'MESH' or obj.data is None:
            continue
        layer = obj.data.uv_layers.get(render_layer_name(obj))
        if layer is None:
            continue
        for item in layer.data:
            item.uv = (item.uv[0] + du, item.uv[1] + dv)
        moved += 1
    return moved


def restore_uvs(snapshot):
    """把 UV 精确还原成快照里的样子"""
    if snapshot is None:
        return 0
    import bpy

    restored = 0
    for obj_name, layer_name, flat in snapshot.entries:
        obj = bpy.data.objects.get(obj_name)
        if obj is None or obj.data is None:
            continue
        layer = obj.data.uv_layers.get(layer_name)
        if layer is None or len(layer.data) * 2 != len(flat):
            continue                        # 网格被改过，放弃还原而不是写错
        for index, item in enumerate(layer.data):
            item.uv = (flat[index * 2], flat[index * 2 + 1])
        restored += 1
    return restored

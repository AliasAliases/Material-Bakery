# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
#  ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   UV 层工具（**不做打包**）
#
#   ⚠ 这里以前是"共享 UV 打包"：把同一集合里多个物体的 UV 摆进互不重叠的格子、
#     新建一层 MBAKERY_UV 当渲染层。用户 2026-09 明确要求**删掉这条路**：
#
#       他要的是"一个集合一张贴图，用物体现在的 UV，一个字节都不动"。
#       他的 UV 是故意重叠的（镜像件共用同一块纹理，省贴图空间），
#       打包既治不了这种重叠，又平白多出一层、还浪费大量空间
#       （等格子布局：每个物体固定拿 1/(列×行) 的地盘，不管它实际占多少）。
#
#   代价（如实记录在设计文档里）：如果重叠的两块区域需要的内容不同，
#   后烘的会覆盖先烘的 —— 这是"不动 UV 共用一张图"的固有代价，由用户自己把握。
#
#   现在 `MBAKERY_UV` 只有一个来源：用户在第 3 页把"没 UV 的物体"选择展到新层
#   （见 ui/properties.py 的 unwrap_target）。
#
#   这个模块只剩"层"相关的工具函数：查渲染层、切渲染层、还原渲染层。
# ------------------------------------------------------------------------------------

import bpy

from .. import compat


# 保留这个名字：老文件里那一层、以及"展到新层"选项都可能用它
PACK_LAYER_NAME = "MBAKERY_UV"
PROP_RENDER_UV = "mbakery_render_uv"


# ------------------------------------------------------------------------------------
#   层工具

def render_uv_layer(obj):
    """当前用于渲染的 UV 层（active_render 优先，否则 active）"""
    mesh = obj.data if obj is not None else None
    if mesh is None or not mesh.uv_layers:
        return None
    for layer in mesh.uv_layers:
        if layer.active_render:
            return layer
    return mesh.uv_layers.active or mesh.uv_layers[0]


def original_uv_layer_name(obj):
    """用户自己那层 UV 的名字（排除我们建的层）"""
    mesh = obj.data if obj is not None else None
    if mesh is None or not mesh.uv_layers:
        return ""
    stored = obj.get(PROP_RENDER_UV)
    if stored and stored != PACK_LAYER_NAME and mesh.uv_layers.get(stored):
        return stored
    for layer in mesh.uv_layers:
        if layer.name != PACK_LAYER_NAME:
            return layer.name
    return ""


def set_render_uv_layer(obj, layer_name):
    """把某一层设为 active_render + active。成功返回 True。"""
    mesh = obj.data if obj is not None else None
    if mesh is None or not layer_name:
        return False
    index = mesh.uv_layers.find(layer_name)
    if index < 0:
        return False
    for i, layer in enumerate(mesh.uv_layers):
        layer.active_render = (i == index)
    mesh.uv_layers.active_index = index
    return True


def uv_bounds(layer):
    """一层的 UV 包围盒 (min_u, min_v, max_u, max_v)；空层返回 None"""
    if layer is None or not len(layer.data):
        return None
    us = [item.uv[0] for item in layer.data]
    vs = [item.uv[1] for item in layer.data]
    return min(us), min(vs), max(us), max(vs)


def restore_render_uv_layers(objects):
    """把渲染用的 UV 层切回用户原来那层"""
    restored = 0
    for obj in objects:
        if obj.type != 'MESH' or obj.data is None:
            continue
        previous = original_uv_layer_name(obj)
        if previous and set_render_uv_layer(obj, previous):
            restored += 1
    return restored


def describe_layers(obj):
    """这个物体上有哪些 UV 层（写进 manifest，救援时有用）"""
    mesh = obj.data if obj is not None else None
    if mesh is None or not mesh.uv_layers:
        return ""
    render = render_uv_layer(obj)
    return render.name if render is not None else ""

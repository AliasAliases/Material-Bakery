# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   最终材质：把烘出来的贴图接回一个能用的材质
#
#   两种风格：
#       SIMPLE     —— 只有图像节点，不接 Principled。给"我自己接"的人用。
#       PRINCIPLED —— 每张图接到 Principled 对应输入口；法线过 Normal Map 节点。
#
#   接口口名不写死在这里 —— 用 core.bake_types 里每个类型的 `sockets` 元组，
#   那是全局唯一一份 socket 名表（含 3.x 别名），这里只负责按顺序找第一个存在的。
# ------------------------------------------------------------------------------------

import bpy

from .. import compat
from ..core import bake_types
from ..core.materials import MARKER_FINAL, MaterialStore


STYLE_SIMPLE = 'SIMPLE'
STYLE_PRINCIPLED = 'PRINCIPLED'

NORMAL_MAP_NODE = 'ShaderNodeNormalMap'


class LinkReport:
    """哪张贴图接上了、哪张没接上，为什么"""

    def __init__(self, material_name):
        self.material_name = material_name
        self.linked = []        # (type_key, socket 或节点说明)
        self.skipped = []       # (type_key, 原因)

    def ok(self, type_key, detail):
        self.linked.append((type_key, detail))

    def skip(self, type_key, reason):
        self.skipped.append((type_key, reason))

    def summary(self):
        text = "{} linked".format(len(self.linked))
        if self.skipped:
            text += ", {} skipped".format(len(self.skipped))
        return text


def find_input(node, names):
    """按顺序找第一个存在的输入口。返回 (名字, 输入) 或 (None, None)。"""
    for name in names:
        socket = node.inputs.get(name)
        if socket is not None:
            return name, socket
    return None, None


def build_final_material(store, name, images, style=STYLE_PRINCIPLED,
                         uv_layer="", image_nodes=True):
    """按 {type_key: image} 建一个最终材质。

    ⚠ **不再加 UVMap 节点**（用户拍板，也是他自己发现的问题）：
      以前这里会插一个 `UVMap` 节点、把 uv_map 写成打包层的名字
      （`MBAKERY_UV`）。而那一层是**按物体**存在的 —— 打包只对"2 个以上物体"
      的组做，被 uv_pack 跳过的物体根本没有这一层。于是材质硬引用一个不存在的层时，
      Blender 会悄悄退回别的 UV 层，物体表面就采样到贴图的错误区域（用户撞的就是这面墙）。
      现在让贴图节点**不接 Vector**：Blender 默认用物体当前的 active render UV 层，
      而打包层已经被设成 active_render（见 engine/uv_pack.py:set_render_uv_layer），
      所以每个物体会自动用对的那一层，不可能错位。

    uv_layer 参数保留只为兼容调用点，**不再产生任何节点**。
    """
    store = store or MaterialStore()
    material = store.acquire(name, MARKER_FINAL, rebuild=False)
    tree = material.node_tree
    if tree is None:
        material.use_nodes = True
        tree = material.node_tree
    tree.nodes.clear()

    report = LinkReport(material.name)

    output = tree.nodes.new('ShaderNodeOutputMaterial')
    output.location = (420, 0)

    shader = None
    if style == STYLE_PRINCIPLED:
        shader = tree.nodes.new('ShaderNodeBsdfPrincipled')
        shader.location = (60, 0)
        tree.links.new(shader.outputs[0], output.inputs[0])

    row = 0
    for type_key, image in sorted(images.items()):
        if image is None:
            continue
        bake_type = bake_types.get(type_key)
        if bake_type is None:
            report.skip(type_key, "unknown bake type")
            continue

        node = tree.nodes.new('ShaderNodeTexImage')
        node.image = image
        node.label = bake_type.label
        node.location = (-600, 300 - row * 260)
        row += 1

        if style == STYLE_SIMPLE or not image_nodes:
            report.ok(type_key, "image node only")
            continue

        if bake_type.key == 'shader.normal':
            normal_map = tree.nodes.new(NORMAL_MAP_NODE)
            normal_map.location = (-320, node.location[1])
            tree.links.new(node.outputs["Color"], normal_map.inputs["Color"])
            name_found, socket = find_input(shader, bake_type.sockets or ("Normal",))
            if socket is None:
                report.skip(type_key, "Principled has no Normal input")
            else:
                tree.links.new(normal_map.outputs["Normal"], socket)
                report.ok(type_key, "{} (via Normal Map)".format(name_found))
            continue

        if bake_type.channel.is_data and 'Normal' in (bake_type.sockets or ()):
            # 切线 / 涂层法线这类向量图：没有对应输入口时不硬接
            pass

        name_found, socket = find_input(shader, bake_type.sockets or ())
        if socket is None:
            report.skip(type_key, "no matching Principled input")
            continue
        tree.links.new(node.outputs["Color"], socket)
        report.ok(type_key, name_found)

    material.use_fake_user = True
    return material, report


def default_name(group_name):
    return group_name


def build_for_groups(store, images_by_group, style=STYLE_PRINCIPLED, uv_layer="",
                     name_func=None):
    """每个分组一个材质。返回 {组名: (材质, LinkReport)}"""
    store = store or MaterialStore()
    name_func = name_func or default_name
    result = {}
    for group_name, images in images_by_group.items():
        name = name_func(group_name)
        material, report = build_final_material(store, name, images, style=style,
                                                uv_layer=uv_layer)
        result[group_name] = (material, report)
        compat.log("材质 {}: {}".format(material.name, report.summary()))
    return result

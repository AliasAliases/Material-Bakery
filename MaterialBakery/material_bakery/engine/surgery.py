# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   节点手术
#
#   Blender 的 EMIT 烘焙是「把物体表面的自发光烘出来」。所以想烘 Principled 的
#   Base Color，就必须临时把 Base Color 那股信号接到一个 Emission 上、再接到
#   Material Output —— 这就是"节点手术"。
#
#   原则：
#       - 只在**烘焙期间**改造节点树，烘完立刻完全还原（SceneTransaction 管）
#       - 原始连线信息在 apply() 时记下来，revert() 按记录恢复，不猜
#       - 每种 BakeKind 一个 handler；认不出的类型返回 False，绝不瞎接
#
#   ⚠ 原版的坑：它把 float 插口的默认值直接塞进 Emission 的 Color（RGBA）插口，
#     类型不匹配会抛 TypeError；还有 `channel_packing_node.inputs[0]` 写死下标，
#     4.x 上完全接错。这里都按名字找插口，并且把 float 正确展开成 RGBA。
# ------------------------------------------------------------------------------------

import bpy

from ..core import bake_types


# 通道打包默认取哪三个通道（ORM 是业界最常见的排法）
DEFAULT_CHANNEL_SOURCES = ("shader.roughness", "shader.metallic", "misc.ao")


def _to_rgba(value):
    """把 float / int / 3 元组 / 4 元组统一成 RGBA。

    原版就是漏了这一步，把 0.5 直接赋给 Color 插口 -> TypeError。
    """
    if isinstance(value, (int, float)):
        return (float(value),) * 3 + (1.0,)
    values = list(value)
    if len(values) == 1:
        return (float(values[0]),) * 3 + (1.0,)
    if len(values) == 2:
        return (float(values[0]), float(values[1]), 0.0, 1.0)
    if len(values) == 3:
        return (float(values[0]), float(values[1]), float(values[2]), 1.0)
    return tuple(float(v) for v in values[:4])


def find_input(node, names):
    """按顺序找第一个存在的输入插口"""
    if node is None:
        return None, None
    for name in names:
        socket = node.inputs.get(name)
        if socket is not None:
            return name, socket
    return None, None


def find_output(node, names):
    """按顺序找第一个存在的输出插口

    ⚠ 全工程都不许写 outputs[0] —— 插口顺序随版本变，
      原版就是靠下标取插口才接错线的。
    """
    if node is None:
        return None, None
    for name in names:
        socket = node.outputs.get(name)
        if socket is not None:
            return name, socket
    return None, None


def supports_node(node_type):
    """这个 Blender 版本有没有这个节点类型

    ⚠ 别用 hasattr(bpy.ops...) 那种判法 —— ops 命名空间是懒加载的，恒为真。
       bpy.types 是真实注册表，getattr 拿不到就是没有。
       （实测 4.5 里 ShaderNodeBrightnessContrast 已经不存在了。）
    """
    return getattr(bpy.types, node_type, None) is not None


def find_shader_node(tree, preferred=None):
    """找到材质里那个"主着色器"节点。

    优先用正接着 Material Output 的那个；如果那里接的已经不是着色器
    （比如上一次手术没还原干净，接着的是 Emission），就退回找第一个 BSDF。
    这样手术是幂等的 —— 绝不会把 Emission 当成着色器去问它要 Roughness。
    """
    if preferred is not None and preferred.type.startswith('BSDF_'):
        return preferred
    for node in tree.nodes:
        if node.type == 'BSDF_PRINCIPLED':
            return node
    for node in tree.nodes:
        if node.type.startswith('BSDF_'):
            return node
    return preferred


def surface_target(tree):
    """找到 Material Output 以及当前接到它 Surface 上的那个输出插口

    返回 (output_node, from_socket 或 None)
    """
    output = None
    for node in tree.nodes:
        if node.type == 'OUTPUT_MATERIAL' and getattr(node, "is_active_output", True):
            output = node
            break
    if output is None:
        for node in tree.nodes:
            if node.type == 'OUTPUT_MATERIAL':
                output = node
                break
    if output is None:
        return None, None
    socket = output.inputs.get("Surface")
    if socket is None or not socket.links:
        return output, None
    return output, socket.links[0].from_socket


def shader_source(material):
    """材质里那个"主着色器"节点（接着 Material Output 的那个）"""
    tree = material.node_tree
    if tree is None:
        return None, None
    output, from_socket = surface_target(tree)
    if from_socket is None:
        return output, None
    return output, from_socket.node


# ------------------------------------------------------------------------------------
#   一次手术的记录

class SurgeryRecord:
    __slots__ = ("material_name", "tree_name", "output_name", "original_from",
                 "created", "detail")

    def __init__(self, material_name, tree_name, output_name, original_from,
                 created, detail):
        self.material_name = material_name
        self.tree_name = tree_name
        self.output_name = output_name
        self.original_from = original_from      # (node_name, socket_name) 或 None
        self.created = created                  # [node_name, ...]
        self.detail = detail                    # 给报告用的一句话

    def to_dict(self):
        return {"material": self.material_name, "detail": self.detail,
                "created": list(self.created), "restored": self.original_from}


class NodeSurgery:
    """一次烘焙任务期间对一组材质做的手术，可以整体还原。

    用法（CyclesBackend 里就是这么用的）:
        surgery = NodeSurgery()
        surgery.apply_materials(materials, bake_type, options)
        ... bake ...
        surgery.revert_all()
    """

    def __init__(self):
        self.records = []
        self.skipped = []          # [(材质名, 原因)]

    def __len__(self):
        return len(self.records)

    # ------------------------------------------------------------------ 施加

    def apply_materials(self, materials, bake_type, options=None):
        """对一批材质施加手术。返回成功的数量。"""
        count = 0
        for material in materials:
            if material is None or material.node_tree is None:
                self.skipped.append((getattr(material, "name", "?"), "no node tree"))
                continue
            if self.apply(material, bake_type, options) is not None:
                count += 1
        return count

    def apply(self, material, bake_type, options=None):
        """改造一个材质。返回 SurgeryRecord，或 None（无法处理，已记进 skipped）。

        ⚠ 成功的记录会**自己**登记进 self.records —— 调用方忘了登记的话
          revert_all() 就还原不了，材质里会留下 Emission 脚手架。
        """
        tree = material.node_tree
        output, original_from = surface_target(tree)
        if output is None:
            self.skipped.append((material.name, "no Material Output node"))
            return None

        handler = HANDLERS.get(bake_type.kind)
        if handler is None:
            self.skipped.append((material.name, "no handler for {}".format(bake_type.kind)))
            return None

        created = []
        try:
            detail = handler(self, tree, bake_type, options or {}, created)
        except SurgeryError as exc:
            self._remove(tree, created)
            self.skipped.append((material.name, str(exc)))
            return None

        if detail is None:
            self._remove(tree, created)
            self.skipped.append((material.name, "handler produced nothing"))
            return None

        original = None
        if original_from is not None:
            original = (original_from.node.name, original_from.name)
        record = SurgeryRecord(material.name, tree.name, output.name, original,
                               [node.name for node in created], detail)
        self.records.append(record)
        return record

    # ------------------------------------------------------------------ 还原

    def revert_all(self):
        """把所有材质恢复成手术前的样子"""
        for record in reversed(self.records):
            self.revert(record)
        self.records = []

    def revert(self, record):
        material = bpy.data.materials.get(record.material_name)
        if material is None or material.node_tree is None:
            return False
        tree = material.node_tree
        output = tree.nodes.get(record.output_name)
        if output is not None:
            socket = output.inputs.get("Surface")
            if socket is not None:
                for link in list(socket.links):
                    tree.links.remove(link)
                if record.original_from is not None:
                    node_name, socket_name = record.original_from
                    source_node = tree.nodes.get(node_name)
                    from_socket = source_node.outputs.get(socket_name) if source_node else None
                    if from_socket is not None:
                        tree.links.new(from_socket, socket)
        nodes = [tree.nodes.get(name) for name in record.created]
        for node in nodes:
            if node is not None:
                tree.nodes.remove(node)
        return True

    # ------------------------------------------------------------------ 工具

    def _remove(self, tree, nodes):
        for node in nodes:
            if node is not None and tree.nodes.get(node.name) is not None:
                tree.nodes.remove(node)

    def new(self, tree, created, node_type, location, **kwargs):
        """建节点并登记（新建的节点全部由手术负责删掉）"""
        node = tree.nodes.new(node_type)
        node.location = location
        for key, value in kwargs.items():
            setattr(node, key, value)
        created.append(node)
        return node

    def summary(self):
        return {
            "operated": [(r.material_name, r.detail) for r in self.records],
            "skipped": list(self.skipped),
        }


class SurgeryError(Exception):
    """手术做不下去（缺插口、没有可用的源等等），交给调用方记进报告"""


# ------------------------------------------------------------------------------------
#   材质上的信号取值

def socket_source(socket):
    """插口的信号来源：('link', from_socket) 或 ('value', 默认值)"""
    if socket is None:
        return "value", 0.0
    if socket.links:
        return "link", socket.links[0].from_socket
    return "value", socket.default_value


def feed_emission(tree, source, emission, input_name="Color"):
    """把一路信号接到 Emission 的输入上。

    link  -> 直接连（float 输出连到 Color 插口时 Blender 会当灰度处理）
    value -> 按 RGBA 规范化后写默认值（**这一步就是原版 TypeError 的来源**）
    """
    kind, payload = source
    socket = emission.inputs.get(input_name)
    if socket is None:
        raise SurgeryError("Emission has no {} input".format(input_name))
    if kind == "link":
        tree.links.new(payload, socket)
        return payload.node.name if payload.node else "linked"
    socket.default_value = _to_rgba(payload)
    return "value {:.3f}".format(socket.default_value[0])


# ------------------------------------------------------------------------------------
#   各个 kind 的 handler
#
#   每个 handler 返回一句"做了什么"的说明字符串；返回 None 表示放弃（会被还原）。

def resolve_shader(tree):
    """Material Output 后面那个着色器（找不到就抛错）"""
    _output, from_socket = surface_target(tree)
    if from_socket is None:
        raise SurgeryError("material has no shader connected to Material Output")
    shader = find_shader_node(tree, from_socket.node)
    if shader is None:
        raise SurgeryError("material has no shader node")
    return shader


def handle_shader_socket(surgery, tree, bake_type, options, created):
    """把一个 Principled 输入插口接到 Emission —— 27 种 shader.* 都走这里"""
    output, _from_socket = surface_target(tree)
    shader = resolve_shader(tree)

    name, socket = find_input(shader, bake_type.sockets)
    if socket is None:
        raise SurgeryError("shader has no input named {}".format(
            " / ".join(bake_type.sockets) or "(none)"))

    emission = surgery.new(tree, created, 'ShaderNodeEmission', (-400, 200))
    feed_emission(tree, socket_source(socket), emission)
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return "{} <- {}".format(name, shader.name)


def handle_channel_pack(surgery, tree, bake_type, options, created):
    """三个灰度通道合成一张 RGB —— 经典的 ORM 贴图

    ⚠ 原版这里写死了 inputs[0]，4.x 上接的是完全不相干的插口。
       这里按名字找，并且每个通道都先过 Separate Color 只取 R ——
       因为用户选的可能是张彩色贴图，直接连会让三个通道互相串色。
    """
    output, _from_socket = surface_target(tree)
    shader = resolve_shader(tree)

    sources = (
        options.get("channel_r") or DEFAULT_CHANNEL_SOURCES[0],
        options.get("channel_g") or DEFAULT_CHANNEL_SOURCES[1],
        options.get("channel_b") or DEFAULT_CHANNEL_SOURCES[2],
    )

    combine = surgery.new(tree, created, 'ShaderNodeCombineColor', (-400, 200),
                          mode='RGB')
    emission = surgery.new(tree, created, 'ShaderNodeEmission', (-160, 200))
    tree.links.new(combine.outputs["Color"], emission.inputs["Color"])
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])

    used = []
    for index, type_key in enumerate(sources):
        bake_type_source = bake_types.get(type_key)
        target = combine.inputs[index]
        if bake_type_source is None:
            target.default_value = 0.0
            used.append("{}?".format(type_key))
            continue

        # AO 不是 Principled 的输入口 —— 它是自己挂节点算出来的。
        # ORM 贴图的 B 通道恰恰最常用 AO，所以这里单独接一下。
        if bake_type_source.kind is bake_types.BakeKind.AO_NODE:
            ao = surgery.new(tree, created, 'ShaderNodeAmbientOcclusion',
                             (-620, 340 - index * 180))
            tree.links.new(ao.outputs["Color"], target)
            used.append("AO node")
            continue

        name, socket = find_input(shader, bake_type_source.sockets)
        if socket is None:
            target.default_value = 0.0
            used.append("{} missing".format(type_key))
            continue
        kind, payload = socket_source(socket)
        if kind == "link":
            separate = surgery.new(tree, created, 'ShaderNodeSeparateColor',
                                   (-620, 340 - index * 180), mode='RGB')
            tree.links.new(payload, separate.inputs["Color"])
            tree.links.new(separate.outputs["Red"], target)
        else:
            # 常量也要退化成灰度：float 0.5 -> 0.5，颜色 (1,0,0) -> 0.299R+...
            if isinstance(payload, (int, float)):
                target.default_value = float(payload)
            else:
                values = list(payload)
                target.default_value = (0.2126 * values[0] + 0.7152 * values[1]
                                        + 0.0722 * values[2])
        used.append(name)
    return "R/G/B <- " + ", ".join(used)


def handle_ao_node(surgery, tree, bake_type, options, created):
    # AO 只依赖几何，不依赖材质里有没有着色器 —— 只有 Output 是必须的
    output, _from_socket = surface_target(tree)
    if output is None:
        raise SurgeryError("no Material Output node")

    ao = surgery.new(tree, created, 'ShaderNodeAmbientOcclusion', (-400, 200))
    for key, attr in (("ao_distance", "distance"), ("ao_inside", "inside"),
                      ("ao_only_local", "only_local"), ("ao_sample", "samples")):
        if key in options and hasattr(ao, attr):
            setattr(ao, attr, options[key])

    emission = surgery.new(tree, created, 'ShaderNodeEmission', (-160, 200))
    tree.links.new(ao.outputs["Color"], emission.inputs["Color"])
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return "Ambient Occlusion node"


def handle_pointiness(surgery, tree, bake_type, options, created):
    output, _from_socket = surface_target(tree)
    if output is None:
        raise SurgeryError("no Material Output node")

    geometry = surgery.new(tree, created, 'ShaderNodeNewGeometry', (-800, 200))
    contrast_value = float(options.get("pointiness_contrast", 1.0))
    bright_value = float(options.get("pointiness_brightness", 0.0))

    signal = geometry.outputs["Pointiness"]
    detail = "Geometry.Pointiness"
    if supports_node('ShaderNodeBrightnessContrast'):
        contrast = surgery.new(tree, created, 'ShaderNodeBrightnessContrast', (-580, 200))
        tree.links.new(signal, contrast.inputs["Color"])
        contrast.inputs["Contrast"].default_value = contrast_value
        contrast.inputs["Bright"].default_value = bright_value
        signal = contrast.outputs["Color"]
        detail += " -> Brightness/Contrast"
    else:
        # 4.x 里没有 Brightness/Contrast 节点了，用 Math(Multiply Add) 顶上：
        # value * contrast + brightness，语义基本一致
        math_node = surgery.new(tree, created, 'ShaderNodeMath', (-580, 200),
                                operation='MULTIPLY_ADD')
        # socket-index: Math 节点的三个输入都叫 Value（Value/Value_001/Value_002），
        # 没有可区分的名字，只能按位次取。这是全工程唯一的例外，
        # tests/mb_test_surgery.py 的静态审计要求这种写法必须带 socket-index 说明。
        tree.links.new(signal, math_node.inputs[0])
        math_node.inputs[1].default_value = contrast_value
        math_node.inputs[2].default_value = bright_value
        signal = math_node.outputs["Value"]
        detail += " -> Math(Multiply Add; this Blender has no Brightness/Contrast node)"

    emission = surgery.new(tree, created, 'ShaderNodeEmission', (-320, 200))
    tree.links.new(signal, emission.inputs["Color"])
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return detail


def handle_uv_map(surgery, tree, bake_type, options, created):
    output, _from_socket = surface_target(tree)
    if output is None:
        raise SurgeryError("no Material Output node")

    uv_node = surgery.new(tree, created, 'ShaderNodeUVMap', (-400, 200))
    uv_name = options.get("uv_map") or ""
    if uv_name:
        uv_node.uv_map = uv_name

    emission = surgery.new(tree, created, 'ShaderNodeEmission', (-160, 200))
    tree.links.new(uv_node.outputs["UV"], emission.inputs["Color"])
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return "UVMap '{}'".format(uv_name or "(active render layer)")


def handle_color_attribute(surgery, tree, bake_type, options, created):
    output, _from_socket = surface_target(tree)
    if output is None:
        raise SurgeryError("no Material Output node")

    attribute = options.get("attribute_name") or ""
    node_type = 'ShaderNodeVertexColor' if supports_node('ShaderNodeVertexColor') \
        else 'ShaderNodeAttribute'
    node = surgery.new(tree, created, node_type, (-400, 200))
    if hasattr(node, "layer_name"):
        node.layer_name = attribute
    elif hasattr(node, "attribute_name"):
        node.attribute_name = attribute

    emission = surgery.new(tree, created, 'ShaderNodeEmission', (-160, 200))
    source_name, source = find_output(node, ("Color", "Vertex Color", "Attribute"))
    if source is None:
        raise SurgeryError("color attribute node has no Color output")
    tree.links.new(source, emission.inputs["Color"])
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return "Color Attribute '{}'".format(attribute or "(default)")


def handle_displacement(surgery, tree, bake_type, options, created):
    """从 Material Output 的 Displacement 插口取（不是从 shader 的输入口）"""
    output, from_socket = surface_target(tree)
    if output is None:
        raise SurgeryError("no Material Output node")
    displacement = output.inputs.get("Displacement")
    if displacement is None:
        raise SurgeryError("this Blender has no Displacement output socket")

    emission = surgery.new(tree, created, 'ShaderNodeEmission', (-400, 200))
    feed_emission(tree, socket_source(displacement), emission)
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return "Material Output.Displacement"


def handle_no_surgery(surgery, tree, bake_type, options, created):
    """标准 bake pass / Multires：不需要改节点树"""
    return "no node changes (standard pass)"


HANDLERS = {
    bake_types.BakeKind.SHADER_SOCKET: handle_shader_socket,
    bake_types.BakeKind.CHANNEL_PACK: handle_channel_pack,
    bake_types.BakeKind.AO_NODE: handle_ao_node,
    bake_types.BakeKind.POINTINESS: handle_pointiness,
    bake_types.BakeKind.UV_MAP: handle_uv_map,
    bake_types.BakeKind.COLOR_ATTR: handle_color_attribute,
    bake_types.BakeKind.DISPLACEMENT: handle_displacement,
    bake_types.BakeKind.STANDARD_PASS: handle_no_surgery,
}


def needs_surgery(bake_type):
    """这个类型到底要不要动节点树"""
    return bake_type.kind is not bake_types.BakeKind.STANDARD_PASS


def collect_materials(objects):
    """目标物体用到的所有材质（去重，按名字排序保证顺序稳定）"""
    materials = {}
    for obj in objects:
        if obj.type != 'MESH' or obj.data is None:
            continue
        for slot in obj.material_slots:
            if slot.material is not None:
                materials[slot.material.name] = slot.material
    return [materials[name] for name in sorted(materials)]

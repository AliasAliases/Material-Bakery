# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   UV 层：采样层 / 写入层（Re-bake 工作流）
#
#   需求 A（用户已经展好新 UV，想把旧贴图烘到新 UV 上）落到引擎里就是两件事：
#
#       ① **采样层**：把材质里每张 Image Texture 的坐标输入钉到指定的一层。
#          贴图 Vector 空 -> 插一个 `ShaderNodeUVMap`；
#          已经在 UVMap 上 -> 改它的 `uv_map`；
#          接的是 `Mapping` -> 钉在 **Mapping 的 Vector 输入之前**
#          （R2-4 实测：钉在 Mapping 之后，采样层是对的，但**丢掉了它的缩放**）
#       ② **写入层**：烘之前临时把那一层设成 `active_render` ——
#          只改这一个标志位，**一个 UV 坐标都不动**；烘完 / 取消都逐项还原。
#
#   两边都照 `engine/surgery.py` 的模式写：apply 时记下"我改了什么"，revert 时
#   **逐项还回**，绝不"重新算一遍原来应该是什么"。
#
#   ⚠ 这个模块**不做业务判断** —— 谁该被钉、缺层的物体怎么办，由调用方（后端 /
#     面板 / 清单）决定。它只负责"照做 + 精确还原"。
# ------------------------------------------------------------------------------------

import bpy

from ..core import bake_types, naming
from . import surgery

# 下拉里的哨兵项。⚠ 它是**枚举项**，不是空串 —— 标识符为空的枚举项会被 Blender
# 当成不可选的分隔符丢掉（见 ui/properties.py 里 COLLECTION_NONE 的说明）。
# 标签就叫 `None`（用户 2026-09-26 的原话："就不能直接写 None 吗？"）：
#   采样侧 None = 不钉采样层；写入侧 None = 不换写入层（两者都 = 今天的行为）
NONE_UV = "__none__"

# 旧哨兵（实施轮 1.1 的 `Same as the write layer` / `Each object's render layer`）。
# 下拉里**已经没有它们了**，但旧 .blend / 旧预设里可能还存着这两个字符串，
# 读到必须当 `None` 处理 —— Blender 对动态枚举里的未知标识符不报错，只会静默失效，
# 于是用户会看到"我明明选过，怎么什么都不对"。
LEGACY_SENTINELS = ("__same__", "__auto__")

# 我们自己的烘焙目标节点（core/transaction.install_bake_target）带的标签。
# ⚠ 钉它没有任何作用（往哪写由 active_render 决定），只会平白多一个节点。
BAKE_TARGET_LABEL = "MBakery"


# ------------------------------------------------------------------------------------
#   两个下拉 -> 两个具体层名

def is_none(value):
    """这一侧是不是"什么都不动"（空串 / None / 旧哨兵都算）"""
    return (not value) or value == NONE_UV or value in LEGACY_SENTINELS


def resolve_choices(from_value, to_value):
    """UI 的两个选择 -> `(采样层名, 写入层名)`；空串 = 这一项**什么都不改**

    - `Write to` 选 `None`    -> 写入层 = 空串 = 不动 `active_render`
    - `Sample from` 选 `None` -> 采样层 = 空串 = **一个采样钉都不注入**
      （材质里每张贴图按各物体当时的渲染层采样；写入层换完之后那就是写入层）
    """
    write = "" if is_none(to_value) else to_value
    sample = "" if is_none(from_value) else from_value
    return sample, write


# ------------------------------------------------------------------------------------
#   层的查询

def mesh_of(obj):
    if obj is None or getattr(obj, "type", "") != 'MESH':
        return None
    return getattr(obj, "data", None)


def object_has_layer(obj, layer_name):
    mesh = mesh_of(obj)
    if mesh is None or not layer_name:
        return False
    return mesh.uv_layers.get(layer_name) is not None


def render_layer_name(obj):
    """物体当前用于渲染的那层 UV（active_render 优先，退回 active / 第一层）

    ⚠ 跟 engine/uv_pack.render_uv_layer 是同一件事，这里独立实现是为了不让
      "UV Sets" 这条流程依赖打包时代的模块（那条路已经被用户明确删掉了）。
    """
    mesh = mesh_of(obj)
    if mesh is None or not mesh.uv_layers:
        return ""
    for layer in mesh.uv_layers:
        if layer.active_render:
            return layer.name
    active = mesh.uv_layers.active
    if active is not None:
        return active.name
    return mesh.uv_layers[0].name


def layer_names(objects):
    """这批物体上出现过的全部 UV 层名（排序去重）—— 两个下拉列表就用它"""
    names = set()
    for obj in objects or ():
        mesh = mesh_of(obj)
        if mesh is None:
            continue
        for layer in mesh.uv_layers:
            names.add(layer.name)
    return sorted(names)


def layer_stats(objects, sample="", write=""):
    """采样层 / 写入层在这批物体上的覆盖情况

    返回 dict（面板上的统计行、清单、测试都用它）：
        objects         物体数
        with_sample     有采样层的物体数（sample 为空时 = 全部）
        with_write      有写入层的物体数（write 为空时 = 全部）
        missing_sample  [物体名, ...]
        missing_write   [物体名, ...]
        layers          出现过的全部层名
        needs_swap      写入层与当前渲染层不同的物体数（= 真的要改标志位的）
        same            True 表示"采样层 == 写入层"（两侧都是同一层 -> 什么都不变）
        nothing         True 表示两侧都没选（`None` / `None`）-> 一个字节都不动
        no_pin          True 表示采样侧没选（-> 不注入采样钉）
    """
    objects = [obj for obj in (objects or ()) if mesh_of(obj) is not None]
    missing_sample = []
    missing_write = []
    needs_swap = 0
    for obj in objects:
        if sample and not object_has_layer(obj, sample):
            missing_sample.append(obj.name)
        if write and not object_has_layer(obj, write):
            missing_write.append(obj.name)
        elif write and render_layer_name(obj) != write:
            needs_swap += 1
    same = bool(sample) and sample == write
    nothing = not sample and not write
    return {
        "objects": len(objects),
        "with_sample": len(objects) - len(missing_sample),
        "with_write": len(objects) - len(missing_write),
        "missing_sample": missing_sample,
        "missing_write": missing_write,
        "layers": layer_names(objects),
        "needs_swap": needs_swap,
        "same": same,
        "nothing": nothing,
        "no_pin": not sample,
        "sample": sample,
        "write": write,
    }


# ------------------------------------------------------------------------------------
#   写入层：临时换 active_render，逐项还原

class RenderLayerSwap:
    """烘之前把**写入层**临时设成 active_render，收尾时逐项还回

    ⚠ 只碰 `active_render` / `active_index` 两个标志位，UV 坐标一个字节都不动
      ——"绝不改用户 UV"那条铁律不破。
    ⚠ 值一样就**一个字都不写**：UV 层上的赋值同样会触发依赖图更新，
      这是本工程反复强调过的规矩（见 core/transaction._restore_write）。
    ⚠ 换过哪些物体、原来在哪一层全部记在 `_saved` 里，还原**不靠猜**：
      早先那套 `uv_pack.restore_render_uv_layers` 是按"第一个不是 MBAKERY_UV 的层"
      推断的，那不是精确还原。
    """

    __slots__ = ("_saved", "swapped", "missing")

    def __init__(self):
        self._saved = []          # [(物体名, 原渲染层名, 原 active_index)]
        self.swapped = 0
        self.missing = []         # 没有这一层的物体名

    def apply(self, objects, layer_name):
        """把这一层设成渲染层。返回真正被换过的物体数（没有则 0）"""
        self.revert_all()
        if not layer_name:
            return 0
        count = 0
        for obj in objects or ():
            mesh = mesh_of(obj)
            if mesh is None:
                continue
            index = mesh.uv_layers.find(layer_name)
            if index < 0:
                self.missing.append(obj.name)
                continue
            # ⚠ `active_render` 在**层**上，不在集合上：`mesh.uv_layers.active_render`
            #   会直接 AttributeError（"attribute active_render not found"）。
            current = ""
            for layer in mesh.uv_layers:
                if layer.active_render:
                    current = layer.name
                    break
            self._saved.append((obj.name, current, mesh.uv_layers.active_index))
            changed = False
            for i, layer in enumerate(mesh.uv_layers):
                wanted = (i == index)
                if bool(layer.active_render) != wanted:
                    layer.active_render = wanted
                    changed = True
            if mesh.uv_layers.active_index != index:
                mesh.uv_layers.active_index = index
                changed = True
            if changed:
                count += 1
        self.swapped = count
        return count

    def revert_all(self):
        """逐项还原成原来的 active_render / active_index"""
        restored = 0
        for name, layer_name, active_index in reversed(self._saved):
            mesh = mesh_of(bpy.data.objects.get(name))
            if mesh is None:
                continue
            if layer_name:
                index = mesh.uv_layers.find(layer_name)
                if index >= 0:
                    for i, layer in enumerate(mesh.uv_layers):
                        wanted = (i == index)
                        if bool(layer.active_render) != wanted:
                            layer.active_render = wanted
                    restored += 1
            if 0 <= active_index < len(mesh.uv_layers):
                if mesh.uv_layers.active_index != active_index:
                    mesh.uv_layers.active_index = active_index
        self._saved = []
        self.swapped = 0
        self.missing = []
        return restored

    def summary(self):
        return {"swapped": self.swapped, "missing": list(self.missing)}


# ------------------------------------------------------------------------------------
#   采样层：把每张 Image Texture 钉到指定层

class PinRecord:
    """一个材质上做的钉定（还原所需的全部信息）"""

    __slots__ = ("material_name", "created", "links", "uv_maps", "untouched", "detail")

    def __init__(self, material_name):
        self.material_name = material_name
        self.created = []         # [节点名, ...] 新插的 UVMap 节点
        self.links = []           # [(来源节点, 来源插口, 目标节点, 目标插口), ...]
        self.uv_maps = []         # [(节点名, 原来的 uv_map 值), ...]
        self.untouched = []       # [(节点名, 节点类型), ...] 坐标来自别的节点，没碰
        self.detail = ""

    def to_dict(self):
        return {"material": self.material_name, "detail": self.detail,
                "created": list(self.created),
                "uv_maps": [list(item) for item in self.uv_maps],
                "untouched": [list(item) for item in self.untouched]}


class UVLayerPin:
    """把材质里每张 Image Texture 的**采样 UV 层**钉住，可精确还原

    用法（engine/backend.py 里就是这么用的）::

        pin = UVLayerPin()
        pin.apply_objects(shading_objects, "UV1")
        ... bake ...
        pin.revert_all()

    记录三类东西，还原时逐项还回：新建的节点、新建的连线、被改过的 `uv_map`。
    """

    MAX_DEPTH = 4             # Mapping 链的深度上限（正常就一两层）

    def __init__(self):
        self.records = []              # [PinRecord, ...]
        self.skipped = []              # [(材质名, 原因)]
        self.untouched = []            # [(材质名, 节点名, 节点类型)]
        self.missing_layers = []       # [(物体名, 层名)] 该物体没有这一层

    def __len__(self):
        return len(self.records)

    @property
    def nodes_created(self):
        return sum(len(record.created) for record in self.records)

    # ------------------------------------------------------------------ 施加

    def apply_objects(self, objects, layer_name):
        """对这批物体用到的材质施加钉子。返回被钉过的材质数。

        ⚠ 层是**每物体**的、钉子是**每材质**的：同一个材质被多个物体共用时
          只能钉一次。所以规则是"这个材质的使用者里**至少有一个**带着这一层才钉"：
            * 一个都没有 -> **不注入**（它们按自己当前的渲染层烘，也就是等于今天），
              材质名记进 `skipped`；
            * 有的有、有的没有 -> 钉，缺层的物体点名记进 `missing_layers`
              （它们会退回按自己当前的渲染层采样）。
          静默不处理是最糟的：用户看到"某一层没生效"却查不出为什么。
        """
        if not layer_name:
            return 0
        owners = {}
        for obj in objects or ():
            mesh = mesh_of(obj)
            if mesh is None:
                continue
            if not object_has_layer(obj, layer_name):
                self.missing_layers.append((obj.name, layer_name))
            for slot in obj.material_slots:
                if slot.material is not None:
                    owners.setdefault(slot.material.name, []).append(obj)

        applied = 0
        for material in surgery.collect_materials(objects or ()):
            group = owners.get(material.name) or []
            if group and not any(object_has_layer(obj, layer_name) for obj in group):
                self.skipped.append((material.name, "no object using it has UV layer "
                                                    "'{}'".format(layer_name)))
                continue
            if self.apply_material(material, layer_name) is not None:
                applied += 1
        return applied

    def apply_material(self, material, layer_name):
        """钉一个材质。返回 PinRecord，或 None（这个材质里没有可钉的贴图）"""
        tree = getattr(material, "node_tree", None)
        if tree is None:
            self.skipped.append((getattr(material, "name", "?"), "no node tree"))
            return None

        record = PinRecord(material.name)
        seen_mappings = set()
        pinned = 0
        climbed = []                    # [(贴图节点名, 真正被钉的那个节点名)]
        for node in tree.nodes:
            if node.type != 'TEX_IMAGE':
                continue
            if (node.label or "").startswith(BAKE_TARGET_LABEL):
                continue                # 我们自己的烘焙目标节点，钉它没有意义
            socket = node.inputs.get("Vector")
            if socket is None:
                continue
            before = len(record.created) + len(record.uv_maps)
            if self._pin_socket(tree, socket, layer_name, record, seen_mappings):
                pinned += 1
                if len(record.created) + len(record.uv_maps) == before:
                    climbed.append(node.name)

        if not pinned:
            self.untouched.extend((material.name, name, kind)
                                  for name, kind in record.untouched)
            return None

        record.detail = "sample UV '{}' pinned on {} image node(s)".format(
            layer_name, pinned)
        if climbed:
            record.detail += " ({} through a Mapping node)".format(len(climbed))
        self.records.append(record)
        self.untouched.extend((material.name, name, kind)
                              for name, kind in record.untouched)
        return record

    def _pin_socket(self, tree, socket, layer_name, record, seen_mappings, depth=0):
        """把一个坐标输入钉到 layer_name。返回 True 表示这里已经钉好了"""
        if socket is None or depth > self.MAX_DEPTH:
            return False
        link = socket.links[0] if socket.links else None
        node = link.from_node if link is not None else None

        if node is None:
            # 空输入（默认 UV）-> 插一个 UVMap 节点，接在坐标链的**起点**
            uv_node = tree.nodes.new('ShaderNodeUVMap')
            uv_node.uv_map = layer_name
            try:
                uv_node.location = (socket.node.location.x - 220,
                                    socket.node.location.y)
            except AttributeError:              # 理论上不会有，但别为此崩
                pass
            record.created.append(uv_node.name)
            tree.links.new(uv_node.outputs["UV"], socket)
            record.links.append((uv_node.name, "UV", socket.node.name, socket.name))
            return True

        if node.type == 'UVMAP':
            if node.uv_map != layer_name:
                record.uv_maps.append((node.name, node.uv_map))
                node.uv_map = layer_name
            return True

        if node.type == 'MAPPING':
            # ⚠ 必须在 Mapping **之前**：R2-4 实测，钉在 Mapping 的 Vector 上会
            #   把缩放整条丢掉（读 0.204 而不是 0.412）。
            if node.name in seen_mappings:
                return False                    # 同一个 Mapping 被好几张贴图共用
            seen_mappings.add(node.name)
            return self._pin_socket(tree, node.inputs.get("Vector"), layer_name,
                                    record, seen_mappings, depth + 1)

        # 其它坐标节点（Texture Coordinate / Attribute / 另一个节点树……）：
        # **不动**，但要说出来 —— 静默不处理比处理错更难查。
        record.untouched.append((node.name, node.type))
        return False

    # ------------------------------------------------------------------ 还原

    def revert_all(self):
        """把所有材质还原成钉之前的样子（逐项还回，不猜）"""
        for record in reversed(self.records):
            material = bpy.data.materials.get(record.material_name)
            tree = getattr(material, "node_tree", None)
            if tree is None:
                continue
            for node_name, old_value in record.uv_maps:
                node = tree.nodes.get(node_name)
                if node is not None and node.uv_map != old_value:
                    node.uv_map = old_value
            for from_node, from_socket, to_node, to_socket in record.links:
                source = tree.nodes.get(from_node)
                target = tree.nodes.get(to_node)
                if source is None or target is None:
                    continue
                out_socket = source.outputs.get(from_socket)
                in_socket = target.inputs.get(to_socket)
                if out_socket is None or in_socket is None:
                    continue
                for link in list(in_socket.links):
                    if link.from_socket is out_socket:
                        tree.links.remove(link)
            for node_name in record.created:
                node = tree.nodes.get(node_name)
                if node is not None:
                    tree.nodes.remove(node)
        self.records = []
        self.untouched = []
        self.missing_layers = []

    # ------------------------------------------------------------------ 报告

    def describe(self, layer_name):
        """几行给报告 / 任务明细看的话（没有异常就不啰嗦）"""
        rows = []
        if self.records:
            rows.append("UV: sample layer '{}' pinned on {} material(s), "
                        "{} UVMap node(s) added".format(
                            layer_name, len(self.records), self.nodes_created))
        for material_name, node_name, node_type in self.untouched[:3]:
            rows.append("UV: {} '{}' in {} was left alone (only UVMap / Mapping "
                        "inputs are pinned)".format(node_type, node_name, material_name))
        for obj_name, layer in self.missing_layers[:3]:
            rows.append("UV: {} has no UV layer '{}' — it samples its own layer"
                        .format(obj_name, layer))
        return rows

    def summary(self):
        return {"pinned": [(r.material_name, r.detail) for r in self.records],
                "skipped": list(self.skipped),
                "untouched": list(self.untouched),
                "missing_layers": list(self.missing_layers)}


# ------------------------------------------------------------------------------------
#   源贴图的尺寸（Re-bake 的 Maps 默认读它）

MAX_UPSTREAM_DEPTH = 6


def _upstream_image(socket):
    """从某个输入插口往上游找第一张 Image Texture 节点（含穿过 Normal Map 之类）"""
    seen = set()
    queue = [(socket, 0)]
    while queue:
        current, level = queue.pop(0)
        if level > MAX_UPSTREAM_DEPTH:
            continue
        for link in getattr(current, "links", ()):
            node = link.from_node
            if node is None or node.name in seen:
                continue
            seen.add(node.name)
            if node.type == 'TEX_IMAGE' and node.image is not None:
                return node
            for item in node.inputs:
                queue.append((item, level + 1))
    return None


def _type_token(type_key):
    bake_type = bake_types.get(type_key)
    if bake_type is None:
        return ""
    return naming.type_token(bake_type.label).lower()


def _image_size(image):
    if image is None:
        return None
    try:
        width, height = int(image.size[0]), int(image.size[1])
    except (AttributeError, IndexError, TypeError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _image_label(image):
    name = getattr(image, "name", "") or ""
    if not name:
        return ""
    if "." not in name:
        name += ".png"                      # 数据块名通常没带扩展名
    return name


def source_size_for(objects, type_key):
    """这个通道的源贴图尺寸 -> `(宽, 高, 图片名)`，找不到返回 None

    两条路，按可靠程度排：
      1. **顺着材质里喂给这个通道的那条链往上游找**第一张 Image Texture
         （着色器插口 -> [Normal Map / Separate Color] -> Image Texture）
      2. 文件名里带这个类型的 token（`Chair_BaseColor.png`）—— 用户从别处
         import 回来的贴图通常就是这个命名
    两条都找不到就返回 None：**不给数字**比给一个来路不明的数字好，
    面板照旧显示默认分辨率。
    """
    best = None
    objects = [obj for obj in (objects or ()) if mesh_of(obj) is not None]

    for material in surgery.collect_materials(objects):
        tree = getattr(material, "node_tree", None)
        if tree is None:
            continue
        shader = surgery.find_shader_node(tree)
        if shader is not None:
            bake_type = bake_types.get(type_key)
            if bake_type is not None:
                _name, socket = surgery.find_input(shader, bake_type.sockets)
                node = _upstream_image(socket) if socket is not None else None
                if node is not None:
                    size = _image_size(node.image)
                    if size is not None:
                        return size[0], size[1], _image_label(node.image)

        token = _type_token(type_key)
        if not token:
            continue
        for node in tree.nodes:
            if node.type != 'TEX_IMAGE' or node.image is None:
                continue
            if (node.label or "").startswith(BAKE_TARGET_LABEL):
                continue
            haystack = "{} {}".format(node.image.name or "",
                                      node.image.filepath or "").lower()
            if token not in haystack:
                continue
            size = _image_size(node.image)
            if size is None:
                continue
            if best is None or size[0] * size[1] > best[0] * best[1]:
                best = (size[0], size[1], _image_label(node.image))
    return best

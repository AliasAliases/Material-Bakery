# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   材质策略 —— 全工程唯一一处创建/复用材质的地方
#
#   原版在**两处**各自 bpy.data.materials.new()：
#       AUTOBAKE_OT_BakeStart.modal  （Final Material）
#       obj_bake_ready               （给无材质物体补一个）
#   同名时 materials.new 得到 "Cube.001"，随后的名字互换把旧的 "Cube" 挤成 "Cube.001"，
#   于是每烘一次就多一个重复材质，而且永远不回收。
#
#   MaterialStore 从结构上让那个 bug 无法复现：
#       同名 + 带本插件标记 -> 原地清空重建（复用同一个数据块）
#       同名 + 是用户的     -> 新建 + 名字互换，绝不动用户的材质
#       没有同名            -> 新建
# ------------------------------------------------------------------------------------

import array

import bpy


# 标记键。挂在材质上，用来区分"这个材质是插件建的"和"用户自己的"。
# 注意与集合引擎用的 autobake_collection_material 区分开。
MARKER_FINAL = "mbakery_final_material"
MARKER_HELPER = "mbakery_helper_material"


class MaterialStore:
    """材质仓库。

    用法：
        store = MaterialStore()
        mat = store.acquire("Body", MARKER_FINAL)
        mat = store.acquire("Sphere", MARKER_HELPER, simple=True)
    """

    def __init__(self):
        self.created = []      # 本次会话新建的材质
        self.reused = []       # 本次会话原地复用的材质
        self.displaced = []    # 被名字互换挤走的用户材质

    # ---------------------------------------------------------------- 主要入口

    def acquire(self, name, marker=MARKER_FINAL, simple=False, rebuild=True):
        """取得一个由本插件拥有的材质。

        name    : 材质名
        marker  : 标记键
        simple  : True 时只清空节点树（给"给无材质物体补一个"用，不需要 Output+Principled）
        rebuild : False 时不重建节点，只保证存在（调用方自己搭节点）

        返回材质。同名材质若是用户自己的，保持原行为新建并做名字互换。
        """
        existing = bpy.data.materials.get(name)

        if existing is not None and existing.get(marker):
            material = existing
            material.use_nodes = True
            if rebuild:
                self._rebuild_tree(material, simple)
            self.reused.append(material)
            return material

        material = bpy.data.materials.new(name=name)
        if material.name != name:
            self._displace(material, name)
        material.use_nodes = True
        if rebuild:
            self._rebuild_tree(material, simple)
        material[marker] = True
        self.created.append(material)
        return material

    def owns(self, material, marker=None):
        """这个材质是不是本插件建的"""
        if material is None:
            return False
        if marker is None:
            return bool(material.get(MARKER_FINAL) or material.get(MARKER_HELPER))
        return bool(material.get(marker))

    # ---------------------------------------------------------------- 内部

    def _displace(self, new_material, wanted_name):
        """名字互换：让新材质拿到想要的名字，把撞名的旧材质改成临时名

        完全沿用原版的策略 —— 不删除用户的材质，只是改名。
        """
        temporary = str(new_material.name)
        new_material.name = ''
        displaced = bpy.data.materials.get(wanted_name)
        if displaced is not None:
            displaced.name = temporary
            self.displaced.append(displaced)
        new_material.name = wanted_name

    def _rebuild_tree(self, material, simple):
        """清空节点树，建出与原版默认一致的结构

        simple=False: Material Output + Principled BSDF，并连线
                      （后续 ab_final_shader 之类的逻辑依赖这个结构去找 BSDF_PRINCIPLED）
        simple=True : 只留一个空的节点树
        """
        tree = material.node_tree
        if tree is None:
            material.use_nodes = True
            tree = material.node_tree
        tree.nodes.clear()
        if simple:
            return

        output = tree.nodes.new('ShaderNodeOutputMaterial')
        output.location = (300, 0)
        shader = tree.nodes.new('ShaderNodeBsdfPrincipled')
        shader.location = (0, 0)
        tree.links.new(shader.outputs[0], output.inputs[0])

    # ---------------------------------------------------------------- 报告

    def summary(self):
        return {
            "created": [m.name for m in self.created],
            "reused": [m.name for m in self.reused],
            "displaced": [m.name for m in self.displaced],
        }


# ------------------------------------------------------------------------------------
#   材质槽交付：追加一个烘焙槽 → 分配面 → 收尾
#
#   ⚠ 一条实测得出的关键事实（Blender 4.5 验证）：
#       **外观由 polygon.material_index -> 槽 决定，active_material_index 不影响渲染。**
#       实测：全部面 index=0 时把 active 设成 1，评估后所有面用的仍是槽 0 的材质。
#
#   所以"追加一个烘焙材质槽、把 active 指过去"**不会改变外观**。用户要的流程是：
#       烘焙完  -> 物体上多一个槽（原来的槽一个不动）
#       预览    -> 把面**分配**到烘焙槽（就是材质属性里那个 Assign 干的事）
#       收尾    -> 删掉除烘焙槽以外的槽，面索引归 0，导出就只有一个材质
#
#   ⚠ 历史教训（用户撞过的那面南墙）：以前是"把物体收拢成一个槽放烘焙材质"，
#     并且材质里还挂了一个引用 `MBAKERY_UV` 的 UVMap 节点 —— 只要物体没有那一层，
#     材质就悄悄退回到别的 UV，表面就花了。现在：
#       * 原槽一个都不删（收尾除外，而且是用户主动点的）
#       * **烘焙材质里不加 UVMap 节点**，让它自动用物体当前的渲染 UV 层
#         （打包层已经被设成 active_render，见 engine/uv_pack.py）
#
#   还原：原槽名 + 原逐面索引一直存在物体上，所以**收尾之后照样能还原** ——
#   备份不在"槽"里，在物体的自定义属性里。
# ------------------------------------------------------------------------------------

PROP_ORIG_SLOTS = "mbakery_orig_slots"          # 原槽材质名，按顺序
PROP_ORIG_ACTIVE = "mbakery_orig_active"        # 原 active_material_index
PROP_ORIG_INDICES = "mbakery_orig_indices"      # 面索引的 RLE（仅多槽时）
PROP_ORIG_POLYCOUNT = "mbakery_orig_polycount"  # 存索引时的面数，用于校验网格未被改动
PROP_BAKED_APPLIED = "mbakery_baked_applied"    # 已追加烘焙槽的标记（= 烘焙材质名）
PROP_BAKED_NAME = "mbakery_baked_material"      # 烘焙材质名，切回原材质后依然记得
PROP_BAKED_SLOT = "mbakery_baked_slot"          # 烘焙槽的下标
PROP_PREVIEWING = "mbakery_previewing"          # 现在是不是在预览烘焙材质


# ------------------------------------------------------------------------------------
#   逐面材质索引：读 / 写
#
#   ⚠ 这里原来全是 `[poly.material_index for poly in mesh.polygons]` 和
#     `for poly, value in zip(...)`。真机测过（tools/probe_run_prefs.py）：
#       160000 个面     Python 循环读 0.037s      foreach_get 0.015s
#     —— 也就是说这个循环**不是**那次 729 秒卡死的原因（一个物体几十毫秒，
#     全场景也就几秒）。换成 foreach_get 是因为它白拿，而且交付/还原正好落在
#     用户等待的那个窗口里；但它救不了任何大问题，别再往它身上安功劳。
#   顺带把最常见的"单材质物体"走一条捷径：所有面都在槽 0 时**一个字节都不存**。
# ------------------------------------------------------------------------------------

def _index_buffer(count):
    """count 个 0 的 C int 数组 —— array('i', bytes) 走 frombytes，没有 Python 循环"""
    return array.array("i", bytes(4 * count))


def read_face_indices(obj):
    """物体的逐面材质索引。返回 array('i')（拿不到就退回 Python 列表）。"""
    mesh = getattr(obj, "data", None)
    polygons = getattr(mesh, "polygons", None)
    if polygons is None:
        return []
    count = len(polygons)
    if not count:
        return []
    try:
        buffer = _index_buffer(count)
        polygons.foreach_get("material_index", buffer)
        return buffer
    except (TypeError, AttributeError, RuntimeError, ValueError):
        # 老版本 / 被限制的 context：退回逐面读，慢但一定对
        return [poly.material_index for poly in polygons]


def write_face_indices(obj, values):
    """把逐面材质索引写回去。values 为空或长度不对时全部写 0。"""
    polygons = getattr(getattr(obj, "data", None), "polygons", None)
    if polygons is None:
        return False
    count = len(polygons)
    if not count:
        return False
    values = list(values) if values is None else values
    if len(values) != count:
        values = _index_buffer(count)
    try:
        polygons.foreach_set("material_index", values)
        return True
    except (TypeError, AttributeError, RuntimeError, ValueError):
        for poly, value in zip(polygons, values):
            poly.material_index = int(value)
        return True


def fill_face_indices(obj, index):
    """所有面都分配到同一个槽"""
    polygons = getattr(getattr(obj, "data", None), "polygons", None)
    if polygons is None:
        return False
    count = len(polygons)
    if not count:
        return False
    index = max(0, int(index))
    try:
        polygons.foreach_set("material_index", array.array("i", [index]) * count)
        return True
    except (TypeError, AttributeError, RuntimeError, ValueError):
        for poly in polygons:
            poly.material_index = index
        return True


def uniform_index(indices):
    """所有面都在同一个槽上吗？是就返回那个槽号，否则 None。

    ⚠ `array.count()` / `list.count()` 是 C 实现 —— 16 万个面一次比较 5 毫秒，
      比 Python 里 `all(...)` 或建 RLE 快一个数量级。单材质物体（最常见的
      情况）因此完全不用存逐面索引：存个空串就够了。
    """
    if not indices:
        return 0
    first = indices[0]
    try:
        return first if indices.count(first) == len(indices) else None
    except (AttributeError, TypeError, ValueError):
        return first if all(value == first for value in indices) else None


def compress_indices(indices):
    """面索引序列 -> RLE 字符串：[0,0,1,1,1] -> "0:2,1:3" """
    if not indices:
        return ""
    parts = []
    current = indices[0]
    count = 1
    for value in indices[1:]:
        if value == current:
            count += 1
        else:
            parts.append("{}:{}".format(current, count))
            current = value
            count = 1
    parts.append("{}:{}".format(current, count))
    return ",".join(parts)


def expand_indices(text):
    """RLE 字符串 -> 面索引序列"""
    if not text:
        return []
    indices = []
    for chunk in text.split(","):
        if ":" not in chunk:
            continue
        key, _, count = chunk.partition(":")
        try:
            indices.extend([int(key)] * int(count))
        except ValueError:
            return []
    return indices


def has_baked_applied(obj):
    """这个物体上有没有我们追加的烘焙槽"""
    return obj is not None and PROP_BAKED_APPLIED in obj


def can_add_baked_slot(obj, material=None):
    """能不能追加烘焙槽。返回 (ok, reason)

    ⚠ 传了 material 时会额外检查"槽里是不是已经有它" —— 共享网格（obj.copy()）
      的两个物体共用一个 mesh，材质槽挂在 mesh 上：给 A 追加之后 B 的槽里也有它，
      但 B 物体自己没被标记过。只看标记的话会给 B 再追加一次（同一个材质占两个槽）。
    """
    if obj is None or obj.data is None:
        return False, "no mesh data"
    if has_baked_applied(obj):
        return False, "already has the baked slot"
    if material is not None and any(slot.material == material
                                    for slot in obj.material_slots):
        return False, "this mesh already has that material in a slot"
    return True, ""


def baked_slot_index(obj):
    """烘焙槽的下标。找不到或对不上时返回 -1。"""
    if obj is None or not has_baked_applied(obj):
        return -1
    material = baked_material_for(obj)
    if material is None:
        return -1
    stored = int(obj.get(PROP_BAKED_SLOT, -1))
    if 0 <= stored < len(obj.material_slots) and \
            obj.material_slots[stored].material == material:
        return stored
    for index, slot in enumerate(obj.material_slots):
        if slot.material == material:
            return index
    return -1


def add_baked_slot(obj, material, fake_user_originals=True):
    """在物体**末尾追加**一个烘焙材质槽，原槽与逐面索引一个都不动。

    这是用户要的流程的第一步：烘完物体上就是两个材质，
    "使用分配材质就能预览成果"（分配由 assign_baked 做）。

    返回 (ok, reason)
    """
    ok, reason = can_add_baked_slot(obj, material)
    if not ok:
        return False, reason
    if material is None:
        return False, "material is None"

    slots = obj.material_slots
    # 备份一次就够；已经有备份（比如收尾之后又追加回来）就不覆盖
    if PROP_ORIG_SLOTS not in obj:
        indices = read_face_indices(obj)
        uniform = uniform_index(indices)
        # 空串 = "所有面都在槽 0"，也就是单材质物体的常态（见 uniform_index）
        if uniform == 0:
            compressed = ""
        elif uniform is not None and indices:
            compressed = "{}:{}".format(int(uniform), len(indices))
        else:
            compressed = compress_indices(indices)
        obj[PROP_ORIG_SLOTS] = [slot.material.name if slot.material else ""
                                for slot in slots]
        obj[PROP_ORIG_ACTIVE] = obj.active_material_index
        obj[PROP_ORIG_INDICES] = compressed
        obj[PROP_ORIG_POLYCOUNT] = len(obj.data.polygons)

    if fake_user_originals:
        for slot in slots:
            if slot.material is not None:
                slot.material.use_fake_user = True
    # 烘焙材质本身也要保命：导出/保存之后它得还在
    material.use_fake_user = True

    obj.data.materials.append(material)
    index = len(obj.material_slots) - 1
    obj[PROP_BAKED_APPLIED] = material.name
    obj[PROP_BAKED_NAME] = material.name
    obj[PROP_BAKED_SLOT] = index
    obj[PROP_PREVIEWING] = False
    # ⚠ 只把 active 指过去**不会改变外观**（外观由逐面索引决定，实测过），
    #   所以这里不动任何面的索引 —— 物体看起来还是原来的样子，
    #   用户点"分配"（assign_baked）才会变成烘焙材质。
    return True, "slot {}".format(index)


def _restore_recorded_indices(obj, compressed, polycount):
    """把备份里的逐面索引写回去。返回 (写回去了吗, 说明)

    ⚠ 三条分支都要清楚：
      * 空串 + 面数对得上 = 备份时**所有面都在槽 0**（单材质物体的常态）；
      * RLE + 面数对得上 = 精确还原；
      * 面数对不上（网格被改过）= 放弃索引，全部回槽 0，并且**说出来** ——
        悄悄按旧索引写回去会让网格显示成一团乱七八糟的材质。
    """
    polygons = obj.data.polygons
    count = len(polygons)
    indices = expand_indices(compressed)
    recorded = int(polycount)
    if not count:
        return True, "empty mesh"
    if recorded != count:
        fill_face_indices(obj, 0)
        return True, ("face count changed ({} -> {}) — fell back to the first "
                      "original slot".format(recorded, count))
    if not indices:
        fill_face_indices(obj, 0)
        return True, "{} face(s) back on slot 0".format(count)
    if len(indices) != count:
        fill_face_indices(obj, 0)
        return True, ("recorded indices do not match the mesh ({} vs {}) — fell back "
                      "to the first original slot".format(len(indices), count))
    last = max(len(obj.material_slots) - 1, 0)
    if max(indices) > last:
        indices = [min(value, last) for value in indices]
    write_face_indices(obj, indices)
    return True, "restored {} face index(es)".format(count)


def assign_baked(obj):
    """把所有面分配到烘焙槽 —— 就是材质属性里那个 Assign 按钮干的事。"""
    index = baked_slot_index(obj)
    if index < 0:
        return False, "no baked slot"
    # 已经在烘焙槽上就一个字节都不写（重复点 Preview 是常事）
    if uniform_index(read_face_indices(obj)) != index:
        fill_face_indices(obj, index)
    obj.active_material_index = index
    obj[PROP_PREVIEWING] = True
    return True, "{} face(s) -> slot {}".format(len(obj.data.polygons), index)


def assign_original(obj):
    """把面分配回原来的槽（用存下来的逐面索引）。返回 (ok, reason)"""
    if not has_baked_applied(obj):
        return False, "no baked slot"
    has_record = PROP_ORIG_POLYCOUNT in obj
    compressed = obj.get(PROP_ORIG_INDICES, "")
    polycount = int(obj.get(PROP_ORIG_POLYCOUNT, 0))
    original_slots = list(obj.get(PROP_ORIG_SLOTS, []))
    if has_record:
        _ok, reason = _restore_recorded_indices(obj, compressed, polycount)
    else:
        # 没有备份（旧版本留下的槽）：只能全回第一个槽，说清楚
        fill_face_indices(obj, 0)
        reason = ("no per-face backup on this object — fell back to the first "
                  "original slot ({} slot(s) were recorded)".format(len(original_slots)))
    obj.active_material_index = int(obj.get(PROP_ORIG_ACTIVE, 0))
    obj[PROP_PREVIEWING] = False
    return True, reason


def finalize_for_export(obj):
    """收尾：删掉除烘焙槽以外的所有槽，面索引归 0 —— 导出时只有一个材质。

    ⚠ 备份**不删**：原槽名与逐面索引存在物体的自定义属性里，
      所以 restore_original_materials() 在收尾之后照样能把原材质槽重建回来。
    """
    index = baked_slot_index(obj)
    if index < 0:
        return False, "no baked slot"
    material = obj.material_slots[index].material
    for slot_index in reversed(range(len(obj.material_slots))):
        obj.data.materials.pop(index=slot_index)
    obj.data.materials.append(material)
    fill_face_indices(obj, 0)
    obj.active_material_index = 0
    obj[PROP_BAKED_SLOT] = 0
    obj[PROP_PREVIEWING] = True
    return True, "kept only the baked material"


def apply_baked_material(obj, material, fake_user_originals=True):
    """兼容旧名字：等价于"追加烘焙槽 + 分配面"

    ⚠ 新代码请直接用 add_baked_slot()（只追加、不改外观）+ assign_baked()（预览）。
    """
    ok, reason = add_baked_slot(obj, material, fake_user_originals)
    if not ok:
        return False, reason
    assign_baked(obj)
    return True, reason


def restore_original_materials(obj):
    """恢复原材质槽与逐面材质索引。返回 (ok, reason)

    ⚠ 三种情况都要能还原：
      1. 只是追加了烘焙槽（面还没分配）—— 去掉那一槽即可；
      2. 正在预览烘焙材质 —— 去掉那一槽 + 把面索引还原；
      3. **已经收尾**（只剩烘焙槽）—— 用备份把原槽全部重建回来。
         备份存在物体的自定义属性里，所以收尾是可逆的。
    """
    if not has_baked_applied(obj):
        return False, "no baked slot"

    names = list(obj.get(PROP_ORIG_SLOTS, []))
    active = int(obj.get(PROP_ORIG_ACTIVE, 0))
    compressed = obj.get(PROP_ORIG_INDICES, "")
    polycount = obj.get(PROP_ORIG_POLYCOUNT, None)

    for index in reversed(range(len(obj.material_slots))):
        obj.data.materials.pop(index=index)

    restored = 0
    for name in names:
        material = bpy.data.materials.get(name) if name else None
        if name and material is None:
            continue                       # 数据块被删了，跳过而不是报错
        obj.data.materials.append(material)
        restored += 1

    # 恢复逐面索引。网格若被改过（面数变了）就放弃索引，避免写错
    if polycount is not None:
        _restore_recorded_indices(obj, compressed, polycount)

    if obj.material_slots:
        obj.active_material_index = min(active, len(obj.material_slots) - 1)
    else:
        obj.active_material_index = 0

    del obj[PROP_BAKED_APPLIED]
    for key in (PROP_ORIG_SLOTS, PROP_ORIG_ACTIVE, PROP_ORIG_INDICES, PROP_ORIG_POLYCOUNT):
        if key in obj:
            del obj[key]
    return True, "restored {} slot(s)".format(restored)


def applied_material_name(obj):
    return obj.get(PROP_BAKED_APPLIED, "") if obj is not None else ""


def baked_material_for(obj):
    """这个物体**应该**用哪个烘焙材质 —— 切回原材质之后依然记得。

    对比切换必须用它，不能用 applied_material_name（那个在切回原材质后被清掉了）。
    """
    if obj is None:
        return None
    name = obj.get(PROP_BAKED_NAME, "") or obj.get(PROP_BAKED_APPLIED, "")
    return bpy.data.materials.get(name) if name else None


def baked_material_name(obj):
    return obj.get(PROP_BAKED_NAME, "") if obj is not None else ""


def visible_material_names(obj):
    """物体**实际显示**的材质名（去重）。

    这是"烘焙材质到底有没有生效"的唯一可信判据 —— 别再看 active_material_index。
    """
    if obj is None or obj.data is None:
        return []
    indices = read_face_indices(obj)
    if not indices:
        return []
    # 单材质物体先走捷径：只有一个槽时不必逐面看
    uniform = uniform_index(indices)
    if uniform is not None and 0 <= uniform < len(obj.material_slots):
        slot = obj.material_slots[uniform]
        if slot.material is not None:
            return [slot.material.name]
    names = []
    for index in sorted(set(indices)):
        if index < len(obj.material_slots):
            slot = obj.material_slots[index]
            if slot.material is not None and slot.material.name not in names:
                names.append(slot.material.name)
    return names

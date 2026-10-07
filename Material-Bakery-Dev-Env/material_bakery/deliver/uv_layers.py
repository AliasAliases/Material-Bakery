# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   UV 层的成品化与预览切换（实施轮 1.1）
#
#   导出器是按**层顺序**给 TEXCOORD_0 / TEXCOORD_1 … 的，所以"只改名、不减少层数"
#   一点用都没有（用户 2026-09-26 的反馈 3）。收尾与"创建最终物体"都要真的把
#   旧层删掉、保留层改名 `UVMap`、设成 active + active_render。
#
#   ⚠ 三件事必须同时成立，否则会把用户的东西弄坏：
#     ① **保留哪一层由"计划里的写入层（`write_uv` = To 层）"决定**，
#        **不许**拿当前的 `active_render` 猜 —— 烘完 `RenderLayerSwap` 已经把渲染层
#        精确还回 `From` 了，猜就会把**新层删掉、旧层留下**，正好反了。
#        没有 `write_uv`（= Bake 流程 / 从磁盘救援那次）时才退回"它当前的渲染层"。
#     ② 删之前**扫这个物体的材质**：用户自己的 `UVMap` / `Attribute` 节点引用了
#        将要被删的层名就不删那层，点名记进 `blocked`（我们注入的采样钉层节点烘完
#        已经还原，正常情况下扫不到）。共享材质时这条守卫照样要跑 ——
#        "创建最终物体"改的是副本，但材质是共享的。
#     ③ **顺序不能反**：`UVMap` 这个名字可能正被一个"将被删掉"的层占着 ——
#        必须先删完再改名，否则 Blender 会给出 `UVMap.001`。
#
#   ⚠ 这一步**不可逆**（层是真的没了）。所以界面文案必须是明确的破坏性警告，
#     测试里也要明写"UV 层回不来"，不许再断言 UV 可还原。
#
#   另外这里还管**预览时的渲染层切换**（用户的反馈 1："UV Maps 栏里 UV 没切过去"）：
#   交付材质故意不接 `UVMap` 节点（见 deliver/materials.py 的说明），它按物体**当前的**
#   `active_render` 采样；所以预览烘焙材质要把渲染层切到写入层，切回原材质要还回去。
# ------------------------------------------------------------------------------------

import bpy

from ..core import live
from ..core.scene_scan import object_name
from ..engine import uv_sets

LAYER_NAME = "UVMap"

# 预览切换用的"原值备份"（挂在物体上，跟材质槽备份同一套做法）
PROP_ORIG_RENDER_UV = "mbakery_orig_render_uv"
PROP_ORIG_UV_INDEX = "mbakery_orig_uv_index"

BACKUP_PROPS = (PROP_ORIG_RENDER_UV, PROP_ORIG_UV_INDEX)


class LayerOutcome:
    """一个物体的 UV 层成品化结果"""

    __slots__ = ("object_name", "ok", "reason", "kept", "renamed", "deleted",
                 "blocked", "fell_back")

    def __init__(self, object_name, ok, reason="", kept="", renamed=False,
                 deleted=(), blocked=(), fell_back=False):
        self.object_name = object_name
        self.ok = ok
        self.reason = reason
        self.kept = kept
        self.renamed = renamed
        self.deleted = list(deleted)
        self.blocked = list(blocked)          # [(层名, [材质名, ...]), ...] 守卫生效
        self.fell_back = fell_back            # True = 没用计划里的层，退回当前渲染层

    def __repr__(self):
        return "<LayerOutcome {} {} kept={} deleted={} blocked={}>".format(
            self.object_name, "OK" if self.ok else "SKIP", self.kept,
            self.deleted, [name for name, _m in self.blocked])


# ------------------------------------------------------------------------------------
#   查询

def keep_layer_for(obj, preferred=""):
    """保留哪一层 -> `(层名, 是否退回了当前的渲染层)`

    `preferred` 是计划里的写入层（`write_uv`）。空 / 这个物体上没这一层时退回
    "它当前的渲染层" —— 那是 Bake 流程与救援路径的语义。
    """
    if preferred and uv_sets.object_has_layer(obj, preferred):
        return preferred, False
    return uv_sets.render_layer_name(obj), bool(preferred)


def referenced_layers(obj):
    """这个物体的材质里，**用户自己的**节点引用了哪些 UV 层名

    返回 `{层名: [材质名, ...]}`。只认两种节点：
        `ShaderNodeUVMap.uv_map`         —— 显式钉了某一层
        `ShaderNodeAttribute.attribute_name` —— 用属性名取那一层
    ⚠ 空串表示"跟随当前的渲染层"，那**不是**一个具体层名，不算引用。
    """
    found = {}
    for slot in obj.material_slots:
        material = slot.material
        tree = getattr(material, "node_tree", None)
        if tree is None:
            continue
        for node in tree.nodes:
            names = []
            if node.type == 'UVMAP':
                names.append(getattr(node, "uv_map", "") or "")
            elif node.type == 'ATTRIBUTE':
                names.append(getattr(node, "attribute_name", "") or "")
            for name in names:
                if not name:
                    continue
                found.setdefault(name, [])
                if material.name not in found[name]:
                    found[name].append(material.name)
    return found


# ------------------------------------------------------------------------------------
#   成品化：真删旧层 + 改名 UVMap + 设 active/active_render

def finalize_layers(obj, keep_name="", guard=True):
    """把一个物体的 UV 层成品化。返回 LayerOutcome

    步骤严格照任务书 §2.3 的 1–4：定保留层 -> 守卫 -> 删其余 -> 改名 -> 设标志位。
    **材质槽的收尾不在这里**（那是 deliver/slots.py 的事，任务书 §2.3-5）。

    ⚠ `obj` 可以是物体也可以是**名字**：这里现取一次（实施轮 1.2）。取不到就返回
      一条 `object is gone`，绝不抛 `ReferenceError` —— 调用方（收尾 / 建最终物体）
      都是跨操作符边界的，手里的引用随时可能已经作废。
    """
    label = object_name(obj) or "?"
    obj = live.live_object(obj)
    if obj is None:
        return LayerOutcome(label, False, "object is gone")
    if obj.type != 'MESH' or obj.data is None:
        return LayerOutcome(obj.name, False, "not a mesh")
    mesh = obj.data
    if not mesh.uv_layers:
        return LayerOutcome(obj.name, False, "no UV layer at all")

    keep, fell_back = keep_layer_for(obj, keep_name)
    if not keep:
        return LayerOutcome(obj.name, False, "could not decide which layer to keep")

    blocked = []
    if guard:
        references = referenced_layers(obj)
        for name in [layer.name for layer in mesh.uv_layers]:
            if name == keep:
                continue
            owners = references.get(name)
            if owners:
                blocked.append((name, owners))

    blocked_names = {name for name, _owners in blocked}
    deleted = []
    for name in [layer.name for layer in mesh.uv_layers]:
        if name == keep or name in blocked_names:
            continue
        layer = mesh.uv_layers.get(name)          # 每删一个都重新取，别留旧引用
        if layer is None:
            continue
        mesh.uv_layers.remove(layer)
        deleted.append(name)

    kept = mesh.uv_layers.get(keep)
    if kept is None:
        return LayerOutcome(obj.name, False, "the kept layer disappeared", keep,
                            deleted=deleted, blocked=blocked, fell_back=fell_back)

    renamed = False
    if kept.name != LAYER_NAME:
        if mesh.uv_layers.get(LAYER_NAME) is None:
            kept.name = LAYER_NAME
            kept = mesh.uv_layers.get(LAYER_NAME)
            renamed = True
        else:
            # 名字被一个**守卫生效**的层占着 —— 不改名好过悄悄变成 UVMap.001
            renamed = False

    index = mesh.uv_layers.find(kept.name)
    if index >= 0:
        mesh.uv_layers.active_index = index
        for i, layer in enumerate(mesh.uv_layers):
            wanted = (i == index)
            if bool(layer.active_render) != wanted:
                layer.active_render = wanted

    reason = "kept '{}'".format(kept.name)
    if deleted:
        reason += ", deleted {}".format(", ".join(deleted))
    if blocked:
        reason += ", kept {} (referenced by material)".format(
            ", ".join(name for name, _o in blocked))
    if fell_back:
        reason += " (no '{}' on this object — used its own render layer)".format(
            keep_name)
    return LayerOutcome(obj.name, True, reason, kept.name, renamed, deleted,
                        blocked, fell_back)


def finalize_layers_for(objects, keep_name="", guard=True):
    """批量成品化，返回 [LayerOutcome, ...]（顺序与入参一致）"""
    return [finalize_layers(obj, keep_name, guard) for obj in objects or ()]


# ------------------------------------------------------------------------------------
#   预览：临时切渲染层（记住原值，能准确还回去）

def switch_render_layer(obj, layer_name):
    """把渲染层指到 `layer_name`，**第一次**切之前记下原值。返回 (ok, reason)"""
    obj = live.live_object(obj)
    if obj is None:
        return False, "object is gone"
    if layer_name == "":
        return False, "no layer to switch to"
    mesh = uv_sets.mesh_of(obj)
    if mesh is None:
        return False, "not a mesh"
    if not uv_sets.object_has_layer(obj, layer_name):
        return False, "this object has no UV layer '{}'".format(layer_name)

    if PROP_ORIG_RENDER_UV not in obj:
        # 只在**第一次**切之前记 —— 来回切几次也不会把记录覆盖成临时值
        obj[PROP_ORIG_RENDER_UV] = uv_sets.render_layer_name(obj)
        obj[PROP_ORIG_UV_INDEX] = int(mesh.uv_layers.active_index)

    index = mesh.uv_layers.find(layer_name)
    if index < 0:
        return False, "this object has no UV layer '{}'".format(layer_name)
    for i, layer in enumerate(mesh.uv_layers):
        wanted = (i == index)
        if bool(layer.active_render) != wanted:
            layer.active_render = wanted
    if mesh.uv_layers.active_index != index:
        mesh.uv_layers.active_index = index
    return True, "rendering from '{}'".format(layer_name)


def restore_render_layer(obj):
    """把渲染层还回记下的原值（并清掉记录）。返回 (ok, reason)"""
    obj = live.live_object(obj)
    if obj is None:
        return False, "object is gone"
    if PROP_ORIG_RENDER_UV not in obj:
        return False, "nothing was recorded"
    name = obj.get(PROP_ORIG_RENDER_UV, "")
    index = int(obj.get(PROP_ORIG_UV_INDEX, 0))
    mesh = uv_sets.mesh_of(obj)
    ok = False
    reason = "the recorded layer is gone"
    if mesh is not None and name and mesh.uv_layers.get(name) is not None:
        found = mesh.uv_layers.find(name)
        for i, layer in enumerate(mesh.uv_layers):
            wanted = (i == found)
            if bool(layer.active_render) != wanted:
                layer.active_render = wanted
        if 0 <= index < len(mesh.uv_layers):
            mesh.uv_layers.active_index = index
        ok, reason = True, "rendering from '{}' again".format(name)
    forget_render_backup(obj)
    return ok, reason


def forget_render_backup(obj):
    """丢掉渲染层的备份记录（成品化 / 移除烘焙槽之后用）"""
    obj = live.live_object(obj)
    if obj is None:
        return False
    removed = False
    for key in BACKUP_PROPS:
        if key in obj:
            del obj[key]
            removed = True
    return removed

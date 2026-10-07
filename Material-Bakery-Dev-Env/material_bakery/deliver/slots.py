# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   材质槽交付（用户 2026-09 定稿的流程）
#
#     烘焙完 -> 物体上追加一个烘焙槽（原槽一个不动）
#     预览   -> 把面分配到烘焙槽（= 材质属性里的 Assign），随时切回原样
#     收尾   -> 删掉除烘焙槽以外的槽，面索引归 0，导出只有一个材质
#
#   ⚠ 以前是"把物体收拢成一个槽"，那需要动原槽、还要靠 RLE 还原；
#     现在是**非破坏性**的：原槽一直在，收尾也是用户主动点的，而且照样能还原。
#
#   规则（用户确认过）：只对「本次运行里所有烘焙项都成功」的物体生效。
#   有一个类型失败就说明这张图不可信，不能拿它盖上去。
#
#   ⚠ 这一层**只认名字**（实施轮 1.2）：计划里的物体引用活不过一次撤销步，
#     用的时候一律按名字现取（`core/live.py`），取不到就跳过 + 记原因。
#     用户那条 Preview 崩溃（`StructRNA of type Object has been removed`）就出在这里。
# ------------------------------------------------------------------------------------

from ..core import live
from ..core import materials as materials_mod
from ..core.scene_scan import object_name


class SlotOutcome:
    __slots__ = ("object_name", "ok", "reason")

    def __init__(self, object_name, ok, reason=""):
        self.object_name = object_name
        self.ok = ok
        self.reason = reason

    def __repr__(self):
        return "<{} {} {}>".format(self.object_name, "OK" if self.ok else "SKIP",
                                   self.reason)


def eligible_names(plan):
    """本次运行里所有烘焙项都成功的物体**名字**（交付的前提条件）"""
    return [name for group in plan.groups for name in group.names()
            if plan.object_all_succeeded(name)]


def group_map(plan):
    """{物体名: 组名} —— 交付时要按组找材质"""
    mapping = {}
    for group in plan.groups:
        for name in group.names():
            mapping[name] = group.name
    return mapping


def _names_for(plan, objects, exclude):
    """要处理哪些物体 -> `(名字列表, 明确被排除的名字集合)`

    `objects=None` = 用计划里"本次全部成功"的那些。给进来的 `objects` 可以是
    物体也可以是名字，这里统一**只取名字**（引用随时可能已经作废）。
    """
    if objects is None:
        names = eligible_names(plan)
    else:
        names = [name for name in (object_name(item) for item in objects) if name]
    skipped = set(exclude or ())
    return [name for name in names if name not in skipped], skipped


def _materials_for(plan, materials_by_group, objects=None, exclude=()):
    """挑出 (物体, 材质) 配对，跳过不适用的

    ⚠ `objects` 里的物体在这一刻**现取**：取不到（被删了 / 撤销步作废了）就写一条
      "object is gone" 的结果，绝不抛异常 —— 用户按 Preview 时该看到一句日志，
      而不是一整页红色报错。
    """
    wanted, _skipped = _names_for(plan, objects, exclude)
    mapping = group_map(plan)
    pairs = []
    outcomes = []
    for name in wanted:
        obj = live.live_object(name)
        if obj is None:
            outcomes.append(SlotOutcome(name, False, "object is gone"))
            continue
        if name not in mapping:
            outcomes.append(SlotOutcome(name, False, "not part of this run"))
            continue
        entry = materials_by_group.get(mapping[name])
        if entry is None:
            outcomes.append(SlotOutcome(name, False, "no material for this group"))
            continue
        material = entry[0] if isinstance(entry, tuple) else entry
        if material is None:
            outcomes.append(SlotOutcome(name, False, "material is None"))
            continue
        pairs.append((obj, material))
    return pairs, outcomes


def add_baked_slots(plan, materials_by_group, objects=None, fake_user=True,
                    exclude=()):
    """给每个物体**追加**一个烘焙材质槽（不动原槽、不改外观）。

    返回 [SlotOutcome, ...]。想看到效果要点 assign_baked()。
    """
    pairs, outcomes = _materials_for(plan, materials_by_group, objects, exclude)
    for obj, material in pairs:
        ok, reason = materials_mod.add_baked_slot(obj, material,
                                                  fake_user_originals=fake_user)
        outcomes.append(SlotOutcome(obj.name, ok, reason))
    return outcomes


def assign_objects(plan, objects=None, baked=True, exclude=()):
    """预览：把面分配到烘焙槽（baked=True）或还原成原槽（baked=False）。

    返回 [SlotOutcome, ...]
    """
    wanted, skipped = _names_for(plan, objects, exclude)
    outcomes = []
    for name in wanted:
        obj = live.live_object(name)
        if obj is None:
            outcomes.append(SlotOutcome(name, False, "object is gone"))
            continue
        if name in skipped:
            outcomes.append(SlotOutcome(name, False, "not part of the bake"))
            continue
        if not materials_mod.has_baked_applied(obj):
            outcomes.append(SlotOutcome(name, False, "no baked slot yet"))
            continue
        if baked:
            ok, reason = materials_mod.assign_baked(obj)
        else:
            ok, reason = materials_mod.assign_original(obj)
        outcomes.append(SlotOutcome(name, ok, reason))
    return outcomes


def finalize_objects(plan, objects=None, exclude=()):
    """收尾：只留烘焙槽，导出就只有一个材质。返回 [SlotOutcome, ...]"""
    wanted, _skipped = _names_for(plan, objects, exclude)
    outcomes = []
    for name in wanted:
        obj = live.live_object(name)
        if obj is None:
            outcomes.append(SlotOutcome(name, False, "object is gone"))
            continue
        if not materials_mod.has_baked_applied(obj):
            continue
        ok, reason = materials_mod.finalize_for_export(obj)
        outcomes.append(SlotOutcome(name, ok, reason))
    return outcomes


def apply_group_materials(plan, materials_by_group, objects=None, fake_user=True,
                          restore_first=True, exclude=()):
    """**兼容旧名字**：追加烘焙槽（不分配面）。

    用户定稿的流程是"追加槽 → 手动分配预览 → 收尾"，所以这里不再收拢槽、
    也不再改外观。
    """
    return add_baked_slots(plan, materials_by_group, objects=objects,
                           fake_user=fake_user, exclude=exclude)


def restore_objects(objects=None):
    """恢复成原材质槽与逐面索引。objects=None 表示全场景扫一遍。

    ⚠ 还要把**渲染层**还回去（实施轮 1.1）：预览时我们把 `active_render` 切到了
      写入层（见 deliver/uv_layers.py），只还材质槽的话，原材质会按新层采样 ——
      正是用户报的那类"采样错位"。备份挂在物体上，所以这一步精确可逆。
    """
    from . import uv_layers

    # ⚠ 入参可能是名字也可能是物体（会话 / 操作符递过来的形状不统一），
    #   一律**现取**：以前写成 `obj.name not in _scene_objects()`，
    #   而那个函数返回的是物体列表 —— 字符串永远不在里面，于是每个物体都被
    #   当成"已经不存在"跳过，恢复静默失败（Info: Restored 0 object(s)）。
    if objects is None:
        objects = _scene_objects()
    outcomes = []
    for item in objects:
        name = object_name(item)
        obj = live.live_object(item)
        if obj is None:
            outcomes.append(SlotOutcome(name or "?", False, "object is gone"))
            continue
        ok, reason = materials_mod.restore_original_materials(obj)
        if ok:
            layer_ok, layer_reason = uv_layers.restore_render_layer(obj)
            if layer_ok:
                reason += "; {}".format(layer_reason)
        outcomes.append(SlotOutcome(obj.name, ok, reason))
    return outcomes


def applied_names(objects=None):
    """已经挂上烘焙槽的物体**名字**（交付 / 预览 / 对比都用它）"""
    if objects is None:
        objects = _scene_objects()
    names = []
    for item in objects:
        name = object_name(item)
        if not name:
            continue
        obj = live.live_object(name)
        if obj is None:
            continue
        if materials_mod.has_baked_applied(obj):
            names.append(name)
    return names


def _scene_objects():
    import bpy
    return list(bpy.data.objects)


# 兼容旧名字（外部/宏可能还在用）
applied_objects = applied_names


def compare_objects(objects=None):
    """能参与"原材质 / 烘焙材质"对比切换的物体"""
    return applied_names(objects)

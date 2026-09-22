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
# ------------------------------------------------------------------------------------

from ..core import materials as materials_mod


class SlotOutcome:
    __slots__ = ("object_name", "ok", "reason")

    def __init__(self, object_name, ok, reason=""):
        self.object_name = object_name
        self.ok = ok
        self.reason = reason

    def __repr__(self):
        return "<{} {} {}>".format(self.object_name, "OK" if self.ok else "SKIP",
                                   self.reason)


def eligible_objects(plan):
    """本次运行里所有烘焙项都成功的物体"""
    return [obj for group in plan.groups for obj in group.objects
            if plan.object_all_succeeded(obj)]


def group_map(plan):
    """{物体名: 组名} —— 交付时要按组找材质"""
    mapping = {}
    for group in plan.groups:
        for obj in group.objects:
            mapping[obj.name] = group.name
    return mapping


def _materials_for(plan, materials_by_group, objects=None, exclude=()):
    """挑出 (物体, 材质) 配对，跳过不适用的"""
    wanted = objects if objects is not None else eligible_objects(plan)
    mapping = group_map(plan)
    skipped = set(exclude or ())
    pairs = []
    outcomes = []
    for obj in wanted:
        if obj.name in skipped:
            outcomes.append(SlotOutcome(
                obj.name, False,
                "skipped: this object was not packed into the shared texture "
                "(its UVs would sample the wrong part)"))
            continue
        if obj.name not in mapping:
            outcomes.append(SlotOutcome(obj.name, False, "not part of this run"))
            continue
        entry = materials_by_group.get(mapping[obj.name])
        if entry is None:
            outcomes.append(SlotOutcome(obj.name, False, "no material for this group"))
            continue
        material = entry[0] if isinstance(entry, tuple) else entry
        if material is None:
            outcomes.append(SlotOutcome(obj.name, False, "material is None"))
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
    wanted = objects if objects is not None else eligible_objects(plan)
    skipped = set(exclude or ())
    outcomes = []
    for obj in wanted:
        if obj.name in skipped:
            outcomes.append(SlotOutcome(obj.name, False, "not part of the bake"))
            continue
        if not materials_mod.has_baked_applied(obj):
            outcomes.append(SlotOutcome(obj.name, False, "no baked slot yet"))
            continue
        if baked:
            ok, reason = materials_mod.assign_baked(obj)
        else:
            ok, reason = materials_mod.assign_original(obj)
        outcomes.append(SlotOutcome(obj.name, ok, reason))
    return outcomes


def finalize_objects(plan, objects=None, exclude=()):
    """收尾：只留烘焙槽，导出就只有一个材质。返回 [SlotOutcome, ...]"""
    wanted = objects if objects is not None else eligible_objects(plan)
    skipped = set(exclude or ())
    outcomes = []
    for obj in wanted:
        if obj.name in skipped:
            continue
        if not materials_mod.has_baked_applied(obj):
            continue
        ok, reason = materials_mod.finalize_for_export(obj)
        outcomes.append(SlotOutcome(obj.name, ok, reason))
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
    """恢复成原材质槽与逐面索引。objects=None 表示全场景扫一遍。"""
    if objects is None:
        objects = _scene_objects()
    # ⚠ 这里要比的是**名字集合**。以前写成 `obj.name not in _scene_objects()`，
    #   而那个函数返回的是物体列表 —— 字符串永远不在里面，于是每个物体都被
    #   当成"已经不存在"跳过，恢复静默失败（Info: Restored 0 object(s)）。
    names = {obj.name for obj in _scene_objects()}
    outcomes = []
    for obj in objects:
        if obj is None or obj.name not in names:
            outcomes.append(SlotOutcome(getattr(obj, "name", "?"), False, "object is gone"))
            continue
        ok, reason = materials_mod.restore_original_materials(obj)
        outcomes.append(SlotOutcome(obj.name, ok, reason))
    return outcomes


def applied_objects(objects=None):
    return [obj for obj in (objects if objects is not None else _scene_objects())
            if materials_mod.has_baked_applied(obj)]


def _scene_objects():
    import bpy
    return list(bpy.data.objects)


def compare_objects(objects=None):
    """能参与"原材质 / 烘焙材质"对比切换的物体"""
    return applied_objects(objects)

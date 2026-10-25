"""被排除的集合（Excluded collection）—— 回归测试

用户 2026-09-25 的实测（"我现在每次烘焙完成的时候都会卡住"那份 log）:
他的道具文件里几乎每个集合都被勾掉了排除（视图层里只剩 2 个物体），
而 `scene_scan.scan()` 走的是 `collection.objects`，完全不看视图层 ——
于是插件照常派发，Blender 每一张都回一句：

    RuntimeError: Error: Object 'ShuiMa.phy' can't be selected because
    it is not in View Layer 'View Layer'!

96 张图里 92 张这么废掉，而日志里只有一串看不懂的英文报错。

这一套盯两件事：
    ① 排除集合里的物体**不进计划**，理由写清楚（带行动指令：去 Outliner 勾回来）；
    ② 剩下的计划**必须是能在视图层里选中的物体** —— 那才是烘焙操作符认的东西。

⚠ 这条断言是"真的去选一下"，不是复读计划里的字段：
   `select_set` 抛不抛异常才是 Blender 的判据本身。
"""
import os
import sys

import bpy

# 工作区 = 本文件所在目录的上一级。
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WS not in sys.path:
    sys.path.insert(0, WS)

import material_bakery
from material_bakery import compat
from material_bakery.core import plan as pl
from material_bakery.core import scene_scan as ss
from material_bakery.ui import ops as uops

compat.set_silent(True)

FAIL = []


def check(label, ok, extra=""):
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", label,
                               ("  " + str(extra)) if extra else ""))
    if not ok:
        FAIL.append(label)


bpy.ops.wm.read_factory_settings(use_empty=True)
material_bakery.register()

scene = bpy.context.scene
view_layer = bpy.context.view_layer


def quad(name, collection, color=(0.9, 0.1, 0.1, 1.0)):
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
    mesh.update()
    layer = mesh.uv_layers.new(name="UVMap")
    for i, uv in enumerate(((0.02, 0.02), (0.98, 0.02), (0.98, 0.98), (0.02, 0.98))):
        layer.data[i].uv = uv
    material = bpy.data.materials.new(name + "Mat")
    material.use_nodes = True
    for node in material.node_tree.nodes:
        if node.type == 'BSDF_PRINCIPLED':
            node.inputs["Base Color"].default_value = color
    mesh.materials.append(material)
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    return obj


# ------------------------------------------------------------------ 场景
print("=== 搭场景：一个看得见的集合 + 一个被排除的集合 ===")
visible = bpy.data.collections.new("Visible")
scene.collection.children.link(visible)
quad("VisibleCube", visible)

excluded = bpy.data.collections.new("Excluded")
scene.collection.children.link(excluded)
quad("ExcludedCube", excluded)

# 直接在场景主集合里的物体：好用的"当前选中物"，用来证明 set() 真的会炸
loose = quad("LooseCube", scene.collection)

layer_collection = view_layer.layer_collection.children["Excluded"]
layer_collection.exclude = True
bpy.context.view_layer.update()

names_in_layer = {o.name for o in view_layer.objects}
check("被排除的集合里的物体**不在**视图层里",
      "ExcludedCube" not in names_in_layer, sorted(names_in_layer))
check("看得见的物体在视图层里", "VisibleCube" in names_in_layer, sorted(names_in_layer))

# 先把"这个场景真的能复现那句报错"钉死。没有这一步，下面的修复测试
# 有可能只是因为场景搭错了才通过 —— 那种绿是假的。
bpy.ops.object.select_all(action='DESELECT')
loose.select_set(True)
view_layer.objects.active = loose
try:
    bpy.data.objects["ExcludedCube"].select_set(True)
    reproduced = False
except RuntimeError as exc:
    reproduced = "not in View Layer" in str(exc)
check("确认 Blender 的原话就是这个（复现条件成立）", reproduced)

# ------------------------------------------------------------------ 修复
print("\n=== ① 排除集合里的物体不进计划 ===")
groups, conflicts = ss.scan(scene, view_layer)
by_name = {g.name: g for g in groups}

check("扫描结果里有那个被排除的集合", "Excluded" in by_name, sorted(by_name))
excluded_group = by_name["Excluded"]
check("被排除的物体没有进 objects",
      [o.name for o in excluded_group.objects] == [],
      [o.name for o in excluded_group.objects])
check("被排除的原因写在 excluded 里（用了统一的常量）",
      ("ExcludedCube", ss.NO_VIEW_LAYER) in excluded_group.excluded,
      excluded_group.excluded)
check("整组没东西可烘 → 标成 skipped 且原因是「视图层里没有」",
      excluded_group.skipped and "excluded collections" in excluded_group.reason,
      (excluded_group.skipped, excluded_group.reason))
check("看得见的集合照常出物体",
      [o.name for o in by_name["Visible"].objects] == ["VisibleCube"],
      [o.name for o in by_name["Visible"].objects])
check("物体没有在多个组里重复出现（选了排除集合就不该再算进主集合的组）",
      [name for name, where in conflicts] == [], conflicts)

print("\n=== ② 计划里剩下的目标都能在视图层里真的选中 ===")
settings = pl.BakeSettings(
    target_mode=ss.MODE_COLLECTIONS,
    maps=pl.map_requests_from_pairs([("shader.base_color", 32)]),
)
plan = pl.build_plan(bpy.context, settings)
targets = [obj for task in plan.tasks for obj in task.targets]
check("计划里有任务（看得见的那个集合）", len(plan.tasks) > 0, len(plan.tasks))
check("没有一个目标是被排除集合里的物体",
      all(obj.name != "ExcludedCube" for obj in targets),
      [obj.name for obj in targets])
check("`Excluded` 进了 skipped 名单，用户能看见为什么",
      any(name == "Excluded" for name, _reason in plan.skipped),
      list(plan.skipped))

bpy.ops.object.select_all(action='DESELECT')
unselectable = []
for obj in targets:
    try:
        obj.select_set(True)
    except RuntimeError:
        unselectable.append(obj.name)
check("每个目标都真的能被 select_set 选中（这才是烘焙的判据）",
      unselectable == [], unselectable)

bpy.ops.object.select_all(action='DESELECT')
loose.select_set(True)
view_layer.objects.active = loose

print("\n=== ③ 一整组都被排除时，用户看得到 actionable 的提示 ===")
# `Excluded` 整组被跳过，所以它以 skipped 的形式出现，而不是 warnings。
# 这里盯的是"理由里说得出是排除"，用户才知道要去 Outliner 打勾。
skipped_reasons = dict(plan.skipped)
check("skipped 理由指向排除集合（不是含糊的 No visible mesh）",
      "excluded collections" in skipped_reasons.get("Excluded", ""),
      skipped_reasons.get("Excluded"))

print("\n=== ④ 部分被排除的组：警告要点名，并告诉用户怎么办 ===")
# 真实形状是"同一个集合里，有的物体能选、有的不能选"：把一个物体直接挂进
# `Excluded`（那个集合是排除状态），另外挂一个能选的进去。
# 注意 `scan` 只看**直接**挂在集合下的物体，所以两个都必须直接挂。
mixed = bpy.data.collections.new("Mixed")
scene.collection.children.link(mixed)
quad("MixedGood", mixed)
quad("MixedBadCube", mixed)
view_layer.layer_collection.children["Mixed"].exclude = True
bpy.context.view_layer.update()

groups, _conflicts = ss.scan(scene, view_layer)
mixed_group = {g.name: g for g in groups}["Mixed"]
check("整组都被排除 → objects 是空的，excluded 里点名两个",
      [o.name for o in mixed_group.objects] == []
      and {name for name, _reason in mixed_group.excluded}
      == {"MixedGood", "MixedBadCube"},
      ([o.name for o in mixed_group.objects], mixed_group.excluded))
check("skipped 理由说得出是排除",
      "excluded collections" in mixed_group.reason, mixed_group.reason)

# 再把 Mixed 放开、只排除它的一个**子集合** —— 这才是嵌套排除，
# 也就是用户文件里 Barricade 那种形状。
view_layer.layer_collection.children["Mixed"].exclude = False
nested = bpy.data.collections.new("MixedNested")
mixed.children.link(nested)
quad("NestedCube", nested)
view_layer.layer_collection.children["Mixed"].children["MixedNested"].exclude = True
bpy.context.view_layer.update()

groups, _conflicts = ss.scan(scene, view_layer)
mixed_group = {g.name: g for g in groups}["Mixed"]
check("父集合可见、子集合被排除：子集合里的物体不算进来",
      [o.name for o in mixed_group.objects] == ["MixedGood", "MixedBadCube"]
      and "NestedCube" not in [o.name for o in mixed_group.objects],
      [o.name for o in mixed_group.objects])
nested_group = {g.name: g for g in groups}["MixedNested"]
check("被排除的嵌套子集合自己也被点名",
      (nested_group.skipped, [o.name for o in nested_group.objects]) == (True, []),
      (nested_group.skipped, [o.name for o in nested_group.objects]))

plan2 = pl.build_plan(bpy.context, settings)
targets2 = [obj.name for task in plan2.tasks for obj in task.targets]
check("嵌套排除集合里的物体没有进计划",
      "NestedCube" not in targets2, targets2)
check("能选的物体照常在计划里（Mixed 重新勾上之后）",
      "MixedGood" in targets2 and "MixedBadCube" in targets2, targets2)

print("\n=== 结果 ===")
if FAIL:
    print("FAILED {} check(s):".format(len(FAIL)))
    for name in FAIL:
        print("  -", name)
else:
    print("ALL CHECKS PASSED")

# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   场景扫描：集合 / 物体分组、冲突检测、目标解析
#
#   三种目标模式（用户要求"单独烘焙"的两种已包含在内）：
#       COLLECTIONS  所有命名集合（弹窗里逐集合勾选）
#       SINGLE       只烘一个集合          <- "单独烘某个集合"
#       SELECTED     只烘选中物体          <- "只烘选中物体"
# ------------------------------------------------------------------------------------

from dataclasses import dataclass, field


MODE_COLLECTIONS = "collections"
MODE_SINGLE = "single"
MODE_SELECTED = "selected"

MODES = (MODE_COLLECTIONS, MODE_SINGLE, MODE_SELECTED)


@dataclass
class ObjectGroup:
    """一组要烘进同一张贴图的物体"""
    name: str                                   # 集合名；SELECTED 模式下是物体名
    objects: list = field(default_factory=list)
    collection: object = None
    skipped: bool = False
    reason: str = ""
    excluded: list = field(default_factory=list)   # [(物体名, 原因)]

    def __len__(self):
        return len(self.objects)


def is_bakeable(obj):
    """可见的网格物体。hide_get() 是视口 H 键隐藏；不看 view layer 的集合排除"""
    if obj.type != 'MESH':
        return False
    if obj.hide_viewport or obj.hide_render or obj.hide_get():
        return False
    return True


def is_bakeable_source(obj):
    """能不能当**采样源**（投影烘焙里的高模）。

    ⚠ 跟 is_bakeable 的区别只有一点，但很关键：**不管隐不隐藏**。
      高模在正常的工作流里就是藏起来的（挡住视口、拖慢显示），
      如果拿 is_bakeable 去筛源，就会得出"没有源可用"的结论 ——
      而目标那一边照样烘，只是烘出来的是它自己的材质，不报任何错。
    """
    return obj is not None and obj.type == 'MESH' and obj.data is not None


def iter_collections(collection, depth=0):
    yield collection, depth
    for child in collection.children:
        yield from iter_collections(child, depth + 1)


# ⚠ 集合被排除（Exclude）时，它里面的物体**不在视图层里**，而烘焙操作符只认
#   视图层的选中物体 —— `select_set` 会直接抛
#       RuntimeError: Object 'X' can't be selected because it is not in View Layer
#   一个都选不中时 Blender 转头去烘**当时选中的别的物体**，于是失败得更隐蔽。
#
#   用户 2026-09-25 的实测：他的道具文件里几乎每个集合都是排除状态（视图层里只剩
#   2 个物体），而 `scan()` 走的是 collection.objects，完全不看视图层 ——
#   结果 96 张图里 **92 张全废**，每一张都是同一句 "can't be selected"。
#
#   所以：视图层里没有的物体，既不进计划也不要派发，理由写在组的 excluded 里 ——
#   像"No visible mesh"那样在处理之前就说清楚，而不是烘到一半才一个个报错。
NO_VIEW_LAYER = "not in this view layer"


def objects_in_view_layer(view_layer):
    """这个视图层里**真的能被选中**的物体名集合。

    ⚠ 判据是 layer collection 树上的 `exclude`，**不是** `view_layer.objects`。
      两个都试过，`view_layer.objects` 是错的：那份集合要等依赖图同步之后才更新，
      而计划编译往往就发生在"刚建完集合、还没同步"的那一刻 ——
      于是新建出来的物体全被判成"不在视图层里"，测试里七个套件因此一起变红
      （2026-09-25 实测）。`exclude` 是用户自己在 Outliner 里勾的那个勾，读它永远最新。

    ⚠ 递归整棵树，而不是只看自己那一层：一个集合自己没被排除，但**父级**被排除了，
      它的物体同样选不中 —— 只看自己会漏掉嵌套集合里的大批物体。
      用户那份道具文件正是这个形状：Barricade 可见，它下面 18 个子集合全被排除。

    ⚠ 用"只要出现一次被排除就算被排除"（`exclude` 取或），**不能**直接
      `states[collection] = exclude`：同一个 collection 在一棵 layer collection
      树里**可以出现多次**（同一个集合挂在多个父级下），后写的那次会把前面的
      排除状态冲掉 —— 实测就是这样，修好之后计划里还有 96 个任务。
    """
    excluded_names = set()
    seen = set()
    root = getattr(view_layer, "layer_collection", None)
    stack = [root] if root is not None else []
    while stack:
        layer_collection = stack.pop()
        collection = layer_collection.collection
        seen.add(collection)
        if bool(layer_collection.exclude):
            excluded_names.add(collection.name)
        stack.extend(layer_collection.children)

    scene = getattr(view_layer, "id_data", None)
    master = getattr(scene, "collection", None)
    names = set()
    for collection in seen:
        # 主集合永远不是"被排除的集合"
        if collection is not master and collection.name in excluded_names:
            continue
        for obj in collection.objects:
            names.add(obj.name)
    return names


def scan(scene, view_layer=None):
    """扫描整个场景。

    返回 (groups, conflicts)
      groups    : [ObjectGroup, ...]，含被标记跳过的
      conflicts : [(物体名, [集合名, ...]), ...] 只统计非主集合的直接链接

    view_layer: 用来判断物体能不能被烘焙选中（见 NO_VIEW_LAYER）。
                不给就**不做视图层过滤**，只按可见性筛 ——
                "没有视图层"和"视图层里没有"是两件事，不能混为一谈。

    ⚠ 不要再写 `scene.view_layers.active` 当兜底：Blender 的 view_layers 集合上
      **没有** `active` 属性（实测 AttributeError），于是兜底永远拿到 None、
      于是所有物体都被判成"不在视图层里" —— 三个套件因此变红，而且失败信息
      只说"主集合里的物体没分到组"，看不出真正的原因。UI 那边走
      `resolve_targets(context, ...)`，视图层是显式传进来的。
    """
    master = scene.collection
    in_layer = objects_in_view_layer(view_layer) if view_layer is not None else None
    groups = []
    by_collection = {}

    for collection, _depth in iter_collections(master):
        if collection is master:
            continue
        group = ObjectGroup(name=collection.name, collection=collection)
        for obj in collection.objects:
            if not is_bakeable(obj):
                group.excluded.append(
                    (obj.name, "not a mesh" if obj.type != 'MESH' else "hidden"))
            elif in_layer is not None and obj.name not in in_layer:
                group.excluded.append((obj.name, NO_VIEW_LAYER))
            else:
                group.objects.append(obj)
        if not collection.objects:
            group.skipped = True
            group.reason = "No direct objects" if collection.children else "Empty collection"
        elif not group.objects:
            group.skipped = True
            # 排除状态**要说出来**：用户看到"No visible mesh"会去检查物体是不是被
            # H 藏了，而真正的原因是他把整个集合的勾去掉了。
            group.reason = ("No visible mesh in this view layer — its objects are in "
                            "excluded collections") if group.excluded else "No visible mesh"
        groups.append(group)
        by_collection[collection] = group

    # 直接挂在场景主集合下的物体：没有集合可用，就一个物体一组，组名 = 物体名
    # （命名规则「有集合用集合名，没有集合用物体名」就落在这里）
    for obj in master.objects:
        if not is_bakeable(obj):
            continue
        if in_layer is not None and obj.name not in in_layer:
            continue
        group = ObjectGroup(name=obj.name, collection=master)
        group.objects.append(obj)
        groups.append(group)


    # 冲突 = 同一个物体出现在不止一个分组里（跨集合共用，或者既在主集合又在子集合）
    conflicts = []
    membership = {}
    for group in groups:
        for obj in group.objects:
            membership.setdefault(obj.name, []).append(group.name)
    for obj_name, group_names in membership.items():
        if len(group_names) > 1:
            conflicts.append((obj_name, group_names))

    return groups, conflicts


def scan_selected(context):
    """SELECTED 模式：每个选中物体自成一组（组名 = 物体名）

    组名就是贴图名前缀的来源 —— 没有集合时自然回落到物体名。

    ⚠ 选中列表本来就来自视图层，所以这里的过滤通常一个都不会命中；
      留着是因为代价为零，而"计划里出现一个选不中的物体"整批烘焙都会失败
      （见 NO_VIEW_LAYER）—— 多一道闸门比多一次事故便宜。
    """
    view_layer = getattr(context, "view_layer", None)
    in_layer = objects_in_view_layer(view_layer) if view_layer is not None else None
    groups = []
    for obj in context.selected_objects:
        if not is_bakeable(obj):
            continue
        if in_layer is not None and obj.name not in in_layer:
            continue
        group = ObjectGroup(name=obj.name)
        group.objects.append(obj)
        groups.append(group)
    groups.sort(key=lambda g: g.name)
    return groups


def resolve_targets(context, mode, single_collection=None, gate_lookup=None):
    """按模式解析出要烘的分组。

    gate_lookup: callable(group_name) -> bool，供弹窗的逐集合勾选使用；
                 None 表示全部启用。
    返回 (groups, conflicts)
    """
    scene = context.scene

    if mode == MODE_SELECTED:
        return scan_selected(context), []

    # ⚠ 传 context 的视图层，不是 scene.view_layers.active：烘焙操作符认的是
    #   **当前窗口**那个视图层，两者在多视图层的文件里可以不是同一个。
    groups, conflicts = scan(scene, getattr(context, "view_layer", None))

    if mode == MODE_SINGLE:
        wanted = single_collection if single_collection is not None else ""
        selected = [g for g in groups if g.name == wanted]
    else:
        selected = list(groups)

    result = []
    for group in selected:
        if group.skipped:
            result.append(group)          # 保留以便报告"为什么跳过"
            continue
        if gate_lookup is not None and not gate_lookup(group.name):
            continue
        result.append(group)
    return result, conflicts


def object_collection_name(obj, scene):
    """物体所属集合名（排除场景主集合）；没有则空串"""
    if obj is None or scene is None:
        return ""
    master = scene.collection
    for collection in obj.users_collection:
        if collection is not master:
            return collection.name
    return ""


def missing_uv_objects(objects):
    return [o for o in objects if o.data is not None and not o.data.uv_layers]


def usable_materials(obj):
    """这个物体身上**真正能烘**的材质（槽位存在且材质不为空）"""
    if obj is None or obj.type != 'MESH':
        return []
    return [slot.material for slot in obj.material_slots if slot.material is not None]


def objects_without_material(objects):
    """没有任何可用材质的物体

    ⚠ 这类物体必须**在计划阶段就剔掉**，否则 Blender 会用一句天书报错：
        RuntimeError: No active image found, add a material or bake to an external file
      实测（tools/probe_no_active_image.py）：这句话只在"被选中的物体里有物体
      一个材质都没有"时出现，而且它是**整批失败** —— 一个没材质的螺丝钉就能让
      整个集合的 4 张贴图全废，错误里还不告诉你是哪个物体。
      所以这里点名记账，让用户在点烘焙之前就在日志里看到。
    """
    return [o for o in objects if not usable_materials(o)]


def drop_objects_without_material(group):
    """把没有材质的物体从组里剔掉，并记账。返回被剔掉的名字列表。"""
    missing = objects_without_material(group.objects)
    if not missing:
        return []
    group.excluded.extend((o.name, "no material") for o in missing)
    group.objects = [o for o in group.objects if usable_materials(o)]
    return [o.name for o in missing]


def drop_objects_without_uv(group):
    """把缺 UV 的物体从组里剔掉，并记账。返回被剔掉的数量。

    没有 UV 烘不出有意义的结果，所以只能跳过 —— 除非打开了 Prepare UV。
    """
    missing = missing_uv_objects(group.objects)
    if not missing:
        return 0
    group.excluded.extend((o.name, "no UV") for o in missing)
    group.objects = [o for o in group.objects if o.data is not None and o.data.uv_layers]
    return len(missing)

# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   向导 operator
#
#   严格顺序：只能上一步 / 下一步，不能跳页。每一步都重新校验。
#   operator 只做「改设置」和「调引擎」，业务判断一律在 core / engine。
# ------------------------------------------------------------------------------------

import os

import bpy

from .. import compat
from ..core import bake_types, naming
from ..core import scene_scan
from ..core import transaction as transaction_mod
from ..engine import backend as backend_mod
from . import panels, properties, session

# 模态 operator 的 timer 间隔。烘焙本身跑在 Blender 的 job 线程里，
# 主线程每拍只做"准备一个任务 / 派发一个任务 / 收一张图"，很轻。
# 0.1s 足够让进度条动起来，也不会白烧 CPU。
BAKE_TICK = 0.1


def _settings(context):
    return context.scene.mbakery


def _log(context, level, message):
    session.Session.get().add_log(level, message)


def _error(operator, message):
    """报一个红字 —— **同时**写进我们自己的日志和落盘文件

    ⚠ 用户的原话："哪个 log 是 blender 里的红色出错 log，我刚把 blender 关了，
      应该是没了"。他看到的红字从来没进过我们的日志（`self.report({'ERROR'})`
      只打到界面上），所以关掉 Blender 就真没了、我也无从查起。
      现在所有 ERROR 都同时落盘 —— 下次红字出现，`Open Session Log` 里必然有一份。

    ⚠ 为什么不覆盖 `bpy.types.Operator.report`（那样一次就够，还能覆盖以后的代码）：
      **实测不生效** —— 在 Python 子类里定义 report，Blender 执行 operator 时
      直接走 C 的那个，我们的函数根本不被调用（探针里打了 print，一次都没出现）。
      所以只能在每个调用点显式调用这里。
    """
    operator.report({'ERROR'}, message)
    session.Session.get().add_log("ERROR", message)


def _warn(operator, message):
    """黄字同样留档（"改了没反应"这类问题往往先以警告出现）"""
    operator.report({'WARNING'}, message)
    session.Session.get().add_log("WARN", message)


def _redraw():
    session.Session.tag_redraw()


# ------------------------------------------------------------------------------------
#   导航

def _step_enabled(context, settings, page):
    """这一步在当前情况下要不要出现在流程里"""
    return True


def _next_page(context, settings, index, direction):
    """算出真正该去的那一页（跳过被禁用的步骤）

    目前没有条件步骤，但写成通用的循环 —— 以后再加"条件步骤"不用改结构。
    ⚠ 步骤表是**按工作流**取的：Bake 五页 / Re-bake 五页（Source & Target ›
      UV Sets › Settings › Bake › Result），两条流程共用同一个 `page` 字段。
    ⚠ 一定要防止死循环：如果所有中间页都被跳过，要能落到终点。
    """
    order = properties.step_order(settings)
    target = index + direction
    while 0 < target < len(order) - 1 and not _step_enabled(context, settings,
                                                            order[target]):
        target += direction
    return max(0, min(target, len(order) - 1))


def _first_page(context, settings):
    """这条流程的第一步 —— "开始新的一轮"要回到它，而不是永远回到 Targets"""
    return properties.step_order(settings)[0]


class MBAKERY_OT_Next(bpy.types.Operator):
    bl_idname = "mbakery.next"
    bl_label = "Next"
    bl_description = "Go to the next step"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        settings = _settings(context)
        order = properties.step_order(settings)
        index = settings.page_index()
        if index >= len(order) - 1:
            return {'CANCELLED'}

        # 第一步（两条流程各自的第一步）与设置页是两道闸门
        if order[index] in (properties.PAGE_TARGETS, properties.PAGE_REBAKE_SOURCE):
            problems = validate_targets(settings)
            if problems:
                _error(self, problems[0])
                return {'CANCELLED'}

        if order[index] == properties.PAGE_SETTINGS:
            problems = [m for level, m in checklist(context, settings) if level == 'ERROR']
            if problems:
                _error(self, problems[0])
                return {'CANCELLED'}

        target = _next_page(context, settings, index, 1)
        if target == index:
            return {'CANCELLED'}
        settings.page = order[target]
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_Back(bpy.types.Operator):
    bl_idname = "mbakery.back"
    bl_label = "Back"
    bl_description = "Go back one step"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        settings = _settings(context)
        index = settings.page_index()
        if index <= 0:
            return {'CANCELLED'}
        if session.Session.get().is_running:
            _warn(self, "Cancel the running bake first")
            return {'CANCELLED'}
        target = _next_page(context, settings, index, -1)
        if target == index:
            return {'CANCELLED'}
        settings.page = properties.step_order(settings)[target]
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_StartOver(bpy.types.Operator):
    bl_idname = "mbakery.start_over"
    bl_label = "Start Over"
    bl_description = "Forget the last run and go back to step 1"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        settings = _settings(context)
        if session.Session.get().is_running:
            _warn(self, "Cancel the running bake first")
            return {'CANCELLED'}
        settings.page = _first_page(context, settings)
        session.Session.get().reset()
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_GotoPage(bpy.types.Operator):
    """只在严格顺序允许的范围内跳（回看已完成的步骤）"""
    bl_idname = "mbakery.goto_page"
    bl_label = "Go to Step"
    bl_options = {'INTERNAL'}

    page: bpy.props.StringProperty()

    def execute(self, context):
        settings = _settings(context)
        order = properties.step_order(settings)
        if self.page not in order:
            return {'CANCELLED'}
        current = settings.page_index()
        target = order.index(self.page)
        if target > current:
            return {'CANCELLED'}          # 不能往前跳，只能往回看
        settings.page = self.page
        _redraw()
        return {'FINISHED'}


# ------------------------------------------------------------------------------------
#   第 1 页：扫描目标

class MBAKERY_OT_RefreshGroups(bpy.types.Operator):
    bl_idname = "mbakery.refresh_groups"
    bl_label = "Scan Scene"
    bl_description = "Rescan collections and objects"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        settings = _settings(context)
        # ⚠ 只对 Bake 流程做重叠测量：Re-bake 恰恰是"两套 UV 重叠/错开"的场景，
        #   量它的跨层重叠没有意义（而且 prepare_uv 在那条流程里是关的）。
        conflicts = session.refresh_groups(context, settings,
                                           measure_overlap=not settings.prepare_uv
                                           and not properties.is_rebake(settings))
        if not properties.active_maps(settings):
            add_default_maps(settings)
            if properties.is_rebake(settings):
                # Re-bake 的默认分辨率**读源贴图**，不是拍脑袋 2048
                # （读不到就留着默认值 —— 面板上那一行会说明来源）
                changed = read_source_sizes(context, settings)
                if changed:
                    _log(context, "INFO", "Re-bake: {} default size(s) taken from the "
                                          "source textures".format(changed))
        for obj_name, group_names in conflicts:
            _log(context, "CONFLICT",
                 "{} belongs to {}".format(obj_name, " + ".join(group_names)))
        self.report({'INFO'}, "Found {} collection(s)".format(
            len(properties.active_groups(settings))))
        _redraw()
        return {'FINISHED'}


def add_default_maps(settings):
    """新会话给一套最常见的贴图，省得用户从零加（加进**当前工作流**那份列表）"""
    for key in ("shader.base_color", "shader.roughness", "shader.normal"):
        add_map(settings, key, properties.DEFAULT_SIZE, properties.DEFAULT_SIZE,
                dedupe=True)
    properties.set_active_map_index(settings, 0)


def add_map(settings, type_key, size_x, size_y=None, dedupe=False):
    """加一项。

    size_y 省略时按正方形处理（`add_map(s, key, 2048)` 仍然有效）。

    dedupe=False（Add 按钮）—— 直接复制一行，允许出现 (类型,宽,高) 相同的两行，
                              用户自己再改。要合并请用 Dedupe。
    dedupe=True （自动填默认列表）—— 已经有的就不重复加。

    ⚠ 加进的是**当前工作流**那份列表（Bake / Re-bake 各一份，甲方定的）。
    """
    if bake_types.get(type_key) is None:
        return None
    size_x = int(size_x or 0)
    size_y = int(size_y or 0) or size_x
    maps = properties.active_maps(settings)
    if dedupe:
        for item in maps:
            if (item.type_key == type_key and item.size_x == size_x
                    and (item.size_y or item.size_x) == size_y):
                return item
    item = maps.add()
    item.type_key = type_key
    item.size_x = size_x
    item.size_y = size_y
    item.enabled = True
    properties.set_active_map_index(settings, len(maps) - 1)
    return item


def read_source_sizes(context, settings):
    """把 Re-bake 的默认分辨率换成**源贴图的尺寸**。返回改了几行

    ⚠ 读不到就**留着原值**，不猜也不清零 —— 面板上那一行会写清楚"这个数字是从
      哪张图读来的"，读不到就什么都没有，用户自己填。
    """
    from ..engine import uv_sets

    maps = properties.active_maps(settings)
    objects = session.rebake_objects(context, settings)
    if not maps or not objects:
        return 0
    changed = 0
    for item in maps:
        found = uv_sets.source_size_for(objects, item.type_key)
        if found is None:
            item.size_source = ""
            continue
        width, height, name = found
        item.size_source = name
        if (item.size_x, item.size_y) != (width, height):
            item.size_x, item.size_y = width, height
            changed += 1
    return changed


def dedupe_maps(settings):
    """删掉 (类型,宽,高) 重复的行，保留第一条。返回删掉的数量。"""
    maps = properties.active_maps(settings)
    seen = set()
    duplicates = []
    for index, item in enumerate(maps):
        key = (item.type_key, item.size_x, item.size_y or item.size_x)
        if key in seen:
            duplicates.append(index)
        else:
            seen.add(key)
    for index in reversed(duplicates):
        maps.remove(index)
    if properties.active_map_index(settings) >= len(maps):
        properties.set_active_map_index(settings, max(0, len(maps) - 1))
    return len(duplicates)


class MBAKERY_OT_AddMap(bpy.types.Operator):
    bl_idname = "mbakery.add_map"
    bl_label = "Add Map"
    bl_description = "Add a bake item"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        settings = _settings(context)
        maps = properties.active_maps(settings)
        index = properties.active_map_index(settings)
        current = maps[index] if maps and 0 <= index < len(maps) else None
        item = add_map(settings,
                       current.type_key if current else "shader.base_color",
                       current.size_x if current else properties.DEFAULT_SIZE,
                       current.size_y if current else properties.DEFAULT_SIZE)
        if item is None:
            _error(self, "Unknown map type")
            return {'CANCELLED'}
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_RemoveMap(bpy.types.Operator):
    bl_idname = "mbakery.remove_map"
    bl_label = "Remove"
    bl_description = "Remove the selected bake item"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        settings = _settings(context)
        maps = properties.active_maps(settings)
        index = properties.active_map_index(settings)
        if not maps or not (0 <= index < len(maps)):
            return {'CANCELLED'}
        maps.remove(index)
        properties.set_active_map_index(
            settings, max(0, min(index, len(maps) - 1)))
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_MoveMap(bpy.types.Operator):
    bl_idname = "mbakery.move_map"
    bl_label = "Move"
    bl_description = "Move the selected bake item up or down"
    bl_options = {'INTERNAL'}

    direction: bpy.props.EnumProperty(items=(('UP', "Up", ""), ('DOWN', "Down", "")),
                                      default='UP')

    def execute(self, context):
        settings = _settings(context)
        maps = properties.active_maps(settings)
        index = properties.active_map_index(settings)
        target = index - 1 if self.direction == 'UP' else index + 1
        if not (0 <= index < len(maps)) or not (0 <= target < len(maps)):
            return {'CANCELLED'}
        maps.move(index, target)
        properties.set_active_map_index(settings, target)
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_DedupeMaps(bpy.types.Operator):
    bl_idname = "mbakery.dedupe_maps"
    bl_label = "Dedupe"
    bl_description = "Remove repeated type + size combinations, keeping the first"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        settings = _settings(context)
        removed = dedupe_maps(settings)
        self.report({'INFO'}, "Removed {} duplicate(s)".format(removed))
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_ClearMaps(bpy.types.Operator):
    bl_idname = "mbakery.clear_maps"
    bl_label = "Clear All"
    bl_description = "Remove every bake item"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        settings = _settings(context)
        properties.active_maps(settings).clear()
        properties.set_active_map_index(settings, 0)
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_ToggleAllGroups(bpy.types.Operator):
    bl_idname = "mbakery.toggle_all_groups"
    bl_label = "Toggle All"
    bl_description = "Enable or disable every collection at once"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        settings = _settings(context)
        groups = properties.active_groups(settings)
        enable = not all(item.enabled for item in groups) if groups else True
        for item in groups:
            item.enabled = enable
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_ReadUvSizes(bpy.types.Operator):
    """Re-bake 的 Maps：分辨率默认**读源贴图的尺寸**

    为什么要有这一条：重烘的源就是用户自己那批贴图，它们的尺寸才是这张图
    该有的尺寸（用户原话里的意思："分辨率默认读源贴图尺寸"）。写死 2048
    会把一张 1024 的贴图烘成 2048 —— 白烧四倍时间，还得用户自己回来改。
    """
    bl_idname = "mbakery.read_uv_sizes"
    bl_label = "Read Sizes From Textures"
    bl_description = ("Use the size of the existing texture each map is baked from")
    bl_options = {'INTERNAL'}

    def execute(self, context):
        settings = _settings(context)
        if not properties.active_maps(settings):
            _warn(self, "Add a bake item first")
            return {'CANCELLED'}
        objects = session.rebake_objects(context, settings)
        if not objects:
            _warn(self, "No target object with a UV map found")
            return {'CANCELLED'}
        changed = read_source_sizes(context, settings)
        sample, _write = session.rebake_uv_choices(settings)
        layer_note = " (sampling '{}')".format(sample) if sample else ""
        with_source = [item for item in properties.active_maps(settings)
                       if item.size_source]
        _log(context, "INFO", "Re-bake: map sizes read from the source textures{}: "
                              "{} of {} line(s) found one, {} changed".format(
                                  layer_note, len(with_source),
                                  len(properties.active_maps(settings)), changed))
        self.report({'INFO'}, "{} map size(s) changed{}".format(changed, layer_note))
        _redraw()
        return {'FINISHED'}


# ------------------------------------------------------------------------------------
#   校验

def validate_targets(settings):
    """当前工作流第一步的放行条件：至少 1 个目标、至少 1 个烘焙项"""
    problems = []
    mode = properties.active_target_mode(settings)
    maps = properties.active_maps(settings)
    if mode == scene_scan.MODE_SINGLE and not properties.active_selected_collection(settings):
        problems.append("Pick a collection to bake")
    if mode == scene_scan.MODE_COLLECTIONS and properties.active_groups(settings):
        if not any(item.enabled for item in properties.active_groups(settings)):
            problems.append("Enable at least one collection")
    if mode == scene_scan.MODE_SELECTED:
        selected = [o for o in bpy.context.selected_objects if scene_scan.is_bakeable(o)]
        if not selected:
            problems.append("Select at least one mesh object in the viewport")
    if not maps:
        problems.append("Add at least one bake item")
    elif not any(item.enabled for item in maps):
        problems.append("Enable at least one bake item")
    return problems


def checklist(context, settings):
    """设置页的红绿清单。返回 [(级别, 说明), ...]，级别是 'ERROR' / 'WARN' / 'OK'。

    ⚠ 两条工作流的清单**不是同一份**：Bake 那份问的是准备 UV / 投影 / 采样，
      Re-bake 那份问的是"两层 UV 齐不齐、采样层和写入层是不是同一层"。
      共享的几条（输出目录 / 有东西可烘 / 命名模板）走同一批小函数。
    """
    if properties.is_rebake(settings):
        return rebake_checklist(context, settings)
    return bake_checklist(context, settings)


def bake_checklist(context, settings):
    rows = []

    path = bpy.path.abspath(settings.output_dir) if settings.output_dir else ""
    if not path:
        rows.append(('WARN', "No output folder — textures only live in memory and are lost "
                             "when you close the file (or turn on 'Pack Textures Into "
                             ".blend')"))
    elif not os.path.isdir(path):
        try:
            os.makedirs(path)
        except OSError:
            rows.append(('ERROR', "Output folder cannot be created: {}".format(path)))
        else:
            rows.append(('OK', "Output folder created: {}".format(path)))
    else:
        rows.append(('OK', "Output folder exists"))

    bakeable = [o for o in context.scene.objects if scene_scan.is_bakeable(o)]
    if not bakeable:
        rows.append(('ERROR', "No bakeable mesh in the scene"))
    else:
        rows.append(('OK', "{} bakeable object(s)".format(len(bakeable))))

    if not settings.maps:
        rows.append(('ERROR', "The bake list is empty"))
    else:
        enabled = [i for i in settings.maps if i.enabled]
        if not enabled:
            rows.append(('ERROR', "Every bake item is disabled"))
        else:
            rows.append(('OK', "{} enabled bake item(s)".format(len(enabled))))

    if not settings.template:
        rows.append(('ERROR', "The name template is empty"))
    else:
        ok, message = naming.validate_template(settings.template)
        # ⚠ 校验通过时 validate_template 返回的空串会被画成一个**没有文字的绿钩**
        #   （用户报的就是这个："Checklist 下面有个意义不明的钩，后面没有文字解释"）。
        #   空文案必须在源头就补上，不能让 UI 去猜。
        rows.append(('OK' if ok else 'ERROR',
                     message or "Name template is valid"))

    missing = scene_scan.missing_uv_objects(bakeable)
    if missing and not settings.prepare_uv:
        rows.append(('WARN', "{} object(s) have no UV map and will be skipped — "
                             "turn on Unwrap With SmartUV to unwrap them".format(len(missing))))
    elif missing:
        rows.append(('OK', "{} object(s) will be unwrapped by Unwrap With SmartUV".format(
            len(missing))))

    # 没有材质的物体：Blender 会**拒绝整批**（"No active image found"），
    # 而且不说是哪个物体。这里点名，放在点烘焙之前就能看到的地方。
    # ⚠ 这是警告不是门禁：用户明确要求先不改"哪些物体参与烘焙"，
    #   所以不拦他，只把后果说清楚。
    naked = scene_scan.objects_without_material(bakeable)
    if naked:
        rows.append(('WARN', "{} object(s) have no material and Blender will refuse the "
                             "whole collection: {}".format(
                                 len(naked), ", ".join(o.name for o in naked[:4]))))

    if settings.use_selected_to_active:
        rows.extend(projection_rows(context, settings))

    rows.extend(sample_rows(context, settings))
    rows.extend(filename_rows(settings))
    return rows


def _shared_setup_rows(context, settings):
    """两条流程**共享**的几条前置检查：输出目录 / 有东西可烘 / 有启用的烘焙项 / 命名

    抽出来是因为 Re-bake 的清单要用同一批话 —— 而不是各写一套（那种重复
    过一阵子必然长歪：一边改了措辞，另一边还在说旧话）。
    """
    rows = []

    path = bpy.path.abspath(settings.output_dir) if settings.output_dir else ""
    if not path:
        rows.append(('WARN', "No output folder — textures only live in memory and are lost "
                             "when you close the file (or turn on 'Pack Textures Into "
                             ".blend')"))
    elif not os.path.isdir(path):
        try:
            os.makedirs(path)
        except OSError:
            rows.append(('ERROR', "Output folder cannot be created: {}".format(path)))
        else:
            rows.append(('OK', "Output folder created: {}".format(path)))
    else:
        rows.append(('OK', "Output folder exists"))

    bakeable = [o for o in context.scene.objects if scene_scan.is_bakeable(o)]
    if not bakeable:
        rows.append(('ERROR', "No bakeable mesh in the scene"))
    else:
        rows.append(('OK', "{} bakeable object(s)".format(len(bakeable))))

    maps = properties.active_maps(settings)
    if not maps:
        rows.append(('ERROR', "The bake list is empty"))
    else:
        enabled = [i for i in maps if i.enabled]
        if not enabled:
            rows.append(('ERROR', "Every bake item is disabled"))
        else:
            rows.append(('OK', "{} enabled bake item(s)".format(len(enabled))))

    if not settings.template:
        rows.append(('ERROR', "The name template is empty"))
    else:
        ok, message = naming.validate_template(settings.template)
        # ⚠ 校验通过时 validate_template 返回的空串会被画成一个**没有文字的绿钩**
        #   （用户报的就是这个："Checklist 下面有个意义不明的钩，后面没有文字解释"）。
        #   空文案必须在源头就补上，不能让 UI 去猜。
        rows.append(('OK' if ok else 'ERROR',
                     message or "Name template is valid"))
    return rows


def rebake_checklist(context, settings):
    """Re-bake 流程的清单：**这条流程自己**的前置条件

    共享的几条走 `_shared_setup_rows`；这里多出来的是这条流程真正的关键点：
        * 两层 UV 齐不齐（缺 From 层的物体**点名**，不静默）
        * 采样层 == 写入层 -> INFO（那等于今天，不是警告）
        * 目标里没有材质的物体（Blender 会整批拒绝，只在日志里体现）
    """
    rows = _shared_setup_rows(context, settings)

    objects = session.rebake_objects(context, settings)
    stats = session.rebake_uv_stats(context, settings)
    sample = stats["sample"]
    write = stats["write"]

    if not objects:
        rows.append(('ERROR', "No target object — check the collection / selection"))
        return rows

    name = lambda value: value if value else "None"                   # noqa: E731
    if stats["nothing"]:
        # ⚠ 这一条是 **INFO 不是 WARN**（甲方定的）：两侧都是 `None` 就是今天的行为，
        #   不是"配错了"，不该让用户以为哪里出了问题。
        rows.append(('INFO', "Both UV sets are None — nothing is injected and no flag "
                             "is changed, exactly like the Bake workflow"))
    elif stats["same"]:
        rows.append(('INFO', "Sampling and writing the same layer ('{}') — nothing is "
                             "changed".format(sample)))
    elif stats["no_pin"]:
        rows.append(('OK', "Sample side is None — the textures are not pinned; writing "
                           "'{}'".format(name(write))))
    else:
        rows.append(('OK', "Sampling '{}', writing '{}'".format(
            name(sample), name(write))))

    if stats["missing_sample"]:
        rows.append(('WARN', "{} object(s) have no UV layer '{}' and will sample their "
                             "own layer: {}".format(
                                 len(stats["missing_sample"]), sample,
                                 ", ".join(stats["missing_sample"][:4]))))
    if stats["missing_write"]:
        rows.append(('WARN', "{} object(s) have no UV layer '{}' — their render layer "
                             "is left alone: {}".format(
                                 len(stats["missing_write"]), write,
                                 ", ".join(stats["missing_write"][:4]))))
    if stats["with_sample"] and not stats["missing_sample"]:
        rows.append(('OK', "All {} object(s) have the sample layer '{}'".format(
            stats["with_sample"], sample)))
    if stats["layers"]:
        rows.append(('OK', "UV layers in this selection: {}".format(
            ", ".join(stats["layers"]))))
    if stats["needs_swap"]:
        rows.append(('OK', "{} object(s) will temporarily render from '{}' while "
                           "baking, then go back".format(stats["needs_swap"], write)))

    naked = scene_scan.objects_without_material(objects)
    if naked:
        rows.append(('WARN', "{} object(s) have no material and Blender will refuse the "
                             "whole collection: {}".format(
                                 len(naked), ", ".join(o.name for o in naked[:4]))))

    rows.extend(sample_rows(context, settings))
    rows.extend(filename_rows(settings))
    return rows


def filename_rows(settings):
    """分辨率相关的提醒：会不会撞文件名。

    ⚠ 为什么必须有这一条：`{size}` 的规则是用户 2026-09 定的 ——
      "两边都是 1024 的倍数就取**大的那条**写 k"。于是 2048x4096 和
      1024x4096 都会渲染成 `4k`。同一个集合里若放了**同一类型**的这两档
      （比如两个 BaseColor，一个 2048x4096、一个 1024x4096），它们会写出
      **同一个文件名**，后烘的覆盖先烘的 —— 这不是猜测，是规则的直接推论。
      规则是他要的，所以这里只警告、不改规则；也**不拦人**（撞名有时是无害的：
      两档内容一样，只是想换个分辨率再烘一次）。
    """
    rows = []
    enabled = [item for item in properties.active_maps(settings) if item.enabled]
    if not enabled:
        return rows
    seen = {}
    collisions = []
    for item in enabled:
        token = naming.size_token(item.size_x, item.size_y)
        key = (item.type_key, token)
        if key in seen:
            collisions.append((seen[key], item))
        else:
            seen[key] = item
    if collisions:
        first, second = collisions[0]
        rows.append(('WARN', "{} bake item(s) would write the same file name — "
                             "{} at {} and {} at {} both get '-{}'. The later one "
                             "overwrites the earlier one".format(
                                 len(collisions), first.bake_type.label,
                                 naming.size_label(first.size_x, first.size_y),
                                 second.bake_type.label,
                                 naming.size_label(second.size_x, second.size_y),
                                 naming.size_token(first.size_x, first.size_y))))
    non_square = [item for item in enabled
                  if (item.size_y or item.size_x) != item.size_x]
    if non_square:
        rows.append(('OK', "{} non-square map(s): {}".format(
            len(non_square),
            ", ".join(naming.size_label(i.size_x, i.size_y)
                      for i in non_square[:3]))))
    return rows


def sample_rows(context, settings):
    """烘焙质量 / 采样 / Hide Unrelated 的提醒。

    来由是用户 2026-09 的两句话：
        "采样默认我接受"          —— 两组采样分开：确定值通道 1 个就够
        "AO 还是需要更高的采样"   —— 所以 AO / 阴影那组默认提到 Medium (64)
    这两件事都必须在**点烘焙之前**看得见：烘焙一轮是几十分钟的事，
    等看到结果才知道设错了，代价太大。
    """
    from ..core import bake_types

    rows = []
    enabled = [item for item in properties.active_maps(settings) if item.enabled]
    if not enabled:
        return rows
    try:
        channel_samples = session.resolve_samples(context, settings)
        sampled_samples = session.resolve_samples(context, settings, sampled=True)
    except Exception as exc:                  # 取值失败不该让整页 Checklist 空掉
        return [('WARN', "Could not read the sample settings: {}".format(exc))]

    sampled_items = [item for item in enabled if _needs_sampling(item.type_key)]
    sampled_labels = []
    for item in sampled_items:
        label = bake_types.get(item.type_key).label
        if label not in sampled_labels:
            sampled_labels.append(label)
    deterministic = len(enabled) - len(sampled_items)
    if deterministic and channel_samples > 8:
        rows.append(('WARN', "Bake Quality is {} samples for {} channel map(s). Those "
                             "channels are deterministic — every sample produces the "
                             "same pixels, so the extra ones are pure wait time "
                             "(1 sample is enough)".format(channel_samples, deterministic)))
    if sampled_labels:
        listed = ", ".join(sampled_labels[:3])
        if sampled_samples < 64:
            rows.append(('WARN', "{} map(s) need real sampling ({}) but are set to {} "
                                 "samples — AO and shadows want 64 or more".format(
                                     len(sampled_items), listed, sampled_samples)))
        else:
            rows.append(('OK', "{} sampled map(s) ({}) at {} samples".format(
                len(sampled_items), listed, sampled_samples)))

    # ⚠ AO 有**两条完全不同的路**，采样旋钮也完全不同 —— 用户说「AO 还是需要更高的
    #   采样」时必须让他知道该拧哪一个：
    #     * `misc.ao`（AO 节点 + EMIT 烘）—— 画质由**节点自己的 Samples（光线数）**
    #       决定，跟烘焙采样数基本无关；
    #     * `standard.ao`（Cycles 原生 AO pass）—— 画质由烘焙采样数决定（见上面那条）。
    ao_rays = [item.ao_samples for item in enabled if _kind_is(item.type_key,
                                                              bake_types.BakeKind.AO_NODE)]
    if ao_rays:
        rays = min(ao_rays)
        if rays < 16:
            rows.append(('WARN', "The Ambient Occlusion node is set to {} rays — that "
                                 "is where its quality comes from (not the bake "
                                 "samples). Raise its Samples box".format(rays)))
        else:
            rows.append(('OK', "The Ambient Occlusion node traces {} rays per pixel "
                               "(its own Samples box)".format(rays)))

    # Hide Unrelated 的后果必须点名到通道：藏起来的几何不再遮挡、不再投影，
    # 受影响的是采样类通道和 AO 节点，而确定值通道一点都不受影响。
    if getattr(settings, "hide_unrelated", False):
        rows.append(('OK', "Meshes outside this run stay hidden while baking — less "
                           "geometry for Cycles to build"))
        affected = list(sampled_labels)
        if ao_rays and "Ambient Occlusion" not in affected:
            affected.append("Ambient Occlusion")
        if affected:
            rows.append(('WARN', "Hide Unrelated While Baking is on: hidden meshes stop "
                                 "casting shadows and occlusion, so {} will not include "
                                 "them".format(", ".join(affected[:3]))))
    return rows


def _needs_sampling(type_key):
    from ..core import bake_types
    bake_type = bake_types.get(type_key)
    return bool(bake_type is not None and bake_type.needs_sampling)


def _kind_is(type_key, kind):
    from ..core import bake_types
    bake_type = bake_types.get(type_key)
    return bool(bake_type is not None and bake_type.kind is kind)


def projection_rows(context, settings):
    """投影烘焙的前置检查

    ⚠ 这里有一条实测得出的硬规则：**只有 Cage Extrusion 才能让光线打到高模**。
      只设 Ray Distance（max_ray_distance）是不够的 —— 实测烘出来是一张纯黑图，
      既不报错也没有任何提示（见 tests/mb_test_s2a.py 里的探针对照）。
      所以两者都为 0 时必须警告，否则用户会对着黑图猜半天。
    """
    from ..engine import providers

    rows = []
    if not settings.cage_extrusion and not settings.max_ray_distance:
        rows.append(('WARN', "Bake From High-Poly is on but Cage Extrusion and Ray "
                             "Distance are both 0 — the rays will not reach the source"))
    sources, reason = panels.count_sources(context, settings)
    if reason:
        rows.append(('ERROR', reason))
    elif sources == 0:
        rows.append(('ERROR', "Bake From High-Poly is on but no source object was found"))
    else:
        rows.append(('OK', "{} source object(s) will be sampled".format(sources)))
    if not settings.cage_extrusion:
        rows.append(('WARN', "Cage Extrusion is 0 — projection usually needs it"))

    return rows


def prepare_needed(context, settings):
    """占位：后面接重叠检测（重叠只警告，不阻止）"""
    return False


# ------------------------------------------------------------------------------------
#   第 3 页：烘焙

class MBAKERY_OT_StartBake(bpy.types.Operator):
    bl_idname = "mbakery.start_bake"
    bl_label = "Bake"
    bl_description = "Start baking"
    bl_options = {'INTERNAL'}

    skip_validation: bpy.props.BoolProperty(default=False)
    confirm: bpy.props.BoolProperty(
        name="Yes, start baking", default=True)

    _timer = None

    def invoke(self, context, event):
        """先弹一个确认框，把"要烘多少东西、存到哪"再摆一遍。

        这是最后一处能拦住误操作的地方 —— 96 张贴图烘起来是要花时间的。
        """
        if self.skip_validation:
            return self.execute(context)
        settings = _settings(context)
        active = session.Session.get()
        if active.is_running:
            _warn(self, "A bake is already running")
            return {'CANCELLED'}
        problems = validate_targets(settings)
        if not problems:
            problems = [m for level, m in checklist(context, settings) if level == 'ERROR']
        if problems:
            _error(self, problems[0])
            return {'CANCELLED'}
        self._summary = bake_summary(context, settings)
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        layout = self.layout
        for line, icon in getattr(self, "_summary", []):
            layout.label(text=line, icon=icon)
        layout.separator()
        layout.prop(self, "confirm")

    def execute(self, context):
        settings = _settings(context)
        active = session.Session.get()
        if active.is_running:
            _warn(self, "A bake is already running")
            return {'CANCELLED'}

        if not self.skip_validation:
            problems = validate_targets(settings)
            if not problems:
                problems = [m for level, m in checklist(context, settings) if level == 'ERROR']
            if problems:
                _error(self, problems[0])
                return {'CANCELLED'}
            if not self.confirm:
                self.report({'INFO'}, "Cancelled")
                return {'CANCELLED'}

        if not settings.output_dir and not settings.pack_into_blend:
            _log(context, "WARN", "No output folder and 'Pack Textures Into .blend' is off — "
                                  "the baked textures will only live in memory")

        if not active.start(context, settings):
            _error(self, active.error or "Could not start")
            return {'CANCELLED'}
        settings.page = properties.PAGE_BAKE

        # ⚠ 用**模态 operator** 驱动，不是 bpy.app.timers。
        #   这是跟原版 Auto Bake 学的一条：模态 operator 是 Blender 里做
        #   "长任务 + 进度 + 取消"的正统做法 ——
        #       ESC 原生就能取消（modal 直接收到按键）
        #       事件投递有保障，不受别的模态弹窗影响
        #       状态栏会显示任务在跑，和 Blender 其它长操作一致
        #   引擎一点没变，模态只是外面那层壳（BakeJob.step() 照旧）。
        # ⚠ 判断"有没有事件循环"要用 bpy.app.background，不能只看 context.window ——
        #   实测 --background 下 context.window 依然不是 None，
        #   于是模态被注册了却永远不会触发，烘焙就卡着不动。
        window = getattr(context, "window", None)
        if window is None or bpy.app.background:
            # 背景模式 / 测试：没有事件循环，让调用方自己 run_blocking 驱动
            _redraw()
            return {'FINISHED'}

        self._timer = context.window_manager.event_timer_add(BAKE_TICK, window=window)
        context.window_manager.modal_handler_add(self)
        _redraw()
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        active = session.Session.get()

        if event.type in {'ESC', 'RIGHTMOUSE'}:
            active.cancel()
            active.step_once()
            self._stop_timer(context)
            active.add_log("WARN", "Cancelled with Esc")
            _redraw()
            return {'CANCELLED'}

        if event.type != 'TIMER':
            # ⚠ 这里**必须**是 PASS_THROUGH，不能是 RUNNING_MODAL。
            #   返回 RUNNING_MODAL 等于把鼠标键盘全吞了：用户烘焙期间切不到
            #   Blender 窗口、面板点不动、连 Stop 按钮都按不到 —— 他的原话是
            #   "我在烘焙期间是切不到 blender 窗口的……发不了 log 也 stop 不了"。
            #   PASS_THROUGH 让事件继续交给 Blender 正常处理：窗口能切、按钮
            #   能点、页面能翻，而 TIMER 照旧推进烘焙、ESC 照旧取消。
            return {'PASS_THROUGH'}

        if active.job is None:
            self._stop_timer(context)
            return {'FINISHED'}

        active.step_once()
        session.Session.tag_redraw()

        if active.job.is_finished:
            self._stop_timer(context)
            _redraw()
            return {'FINISHED'}
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        """Blender 自己取消这个模态（比如切文件）时也要收干净"""
        self._stop_timer(context)

    def _stop_timer(self, context):
        if self._timer is not None:
            try:
                context.window_manager.event_timer_remove(self._timer)
            except (RuntimeError, ValueError, AttributeError):
                pass
            self._timer = None


def bake_summary(context, settings):
    """确认弹窗里那几行：要烘什么、多少、存哪（两条工作流共用这一个弹窗）"""
    rows = []
    mode = properties.active_target_mode(settings)
    if mode == scene_scan.MODE_SELECTED:
        targets = "{} selected object(s)".format(
            len([o for o in context.selected_objects if scene_scan.is_bakeable(o)]))
    elif mode == scene_scan.MODE_SINGLE:
        targets = "collection '{}'".format(
            properties.active_selected_collection(settings) or "?")
    else:
        enabled = [item for item in properties.active_groups(settings) if item.enabled]
        targets = "{} collection(s), {} object(s)".format(
            len(enabled), sum(item.object_count for item in enabled))

    maps = [item for item in properties.active_maps(settings) if item.enabled]
    biggest = max(maps, key=lambda i: i.size_x * (i.size_y or i.size_x), default=None)
    rows.append(("Targets: {}".format(targets), 'OBJECT_DATAMODE'))
    rows.append(("Maps: {} item(s), up to {}".format(
        len(maps), naming.size_label(biggest.size_x, biggest.size_y) if biggest else "?"),
        'TEXTURE'))
    rows.append(("Estimated: {} image(s)".format(
        _estimate_images(context, settings, len(maps))), 'IMAGE_DATA'))
    rows.append(("Output: {}".format(
        bpy.path.abspath(settings.output_dir) if settings.output_dir
        else "(none — stays in the .blend)"), 'FILE_FOLDER'))
    if properties.is_rebake(settings):
        sample, write = session.rebake_uv_choices(settings)
        stats = session.rebake_uv_stats(context, settings)
        rows.append(("Re-bake: sample '{}' -> write '{}'".format(
            sample or "None", write or "None"), 'UV'))
        if stats["nothing"]:
            rows.append(("Both UV sets are None — nothing is injected and no flag "
                         "changes", 'INFO'))
    if settings.antialias == 'DOWNSCALE':
        rows.append(("Supersampling x{} — about {}x slower".format(
            settings.aa_scale, settings.aa_scale ** 2), 'INFO'))
    return rows


def _estimate_images(context, settings, map_count):
    """大概会出多少张图 —— 只做粗略估算，给用户一个量级"""
    mode = properties.active_target_mode(settings)
    if mode == scene_scan.MODE_SELECTED:
        groups = 1
    elif mode == scene_scan.MODE_SINGLE:
        groups = 1 if properties.active_selected_collection(settings) else 0
    else:
        groups = len([item for item in properties.active_groups(settings) if item.enabled])
    return groups * map_count


class MBAKERY_OT_PauseBake(bpy.types.Operator):
    bl_idname = "mbakery.pause_bake"
    bl_label = "Pause"
    bl_description = "Stop after the map that is currently baking"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        active = session.Session.get()
        if not active.is_running or active.job is None:
            return {'CANCELLED'}
        active.job.pause()
        active.add_log("INFO", "Paused")
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_ResumeBake(bpy.types.Operator):
    bl_idname = "mbakery.resume_bake"
    bl_label = "Resume"
    bl_description = "Continue baking"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        active = session.Session.get()
        if active.job is None:
            return {'CANCELLED'}
        active.job.resume()
        active.add_log("INFO", "Resumed")
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_Rebake(bpy.types.Operator):
    """用上一次的设置重跑，跳过第 1、2 页 —— 但校验照跑"""
    bl_idname = "mbakery.rebake"
    bl_label = "Re-bake with these settings"
    bl_description = "Run the same bake again without going through the wizard"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        return bpy.ops.mbakery.start_bake(skip_validation=False)


class MBAKERY_OT_CancelBake(bpy.types.Operator):
    bl_idname = "mbakery.cancel_bake"
    bl_label = "Cancel"
    bl_description = "Stop the running bake and put everything back"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        active = session.Session.get()
        if not active.is_running:
            return {'CANCELLED'}
        active.cancel()
        active.step_once()
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_RunToFinish(bpy.types.Operator):
    """背景 / 测试用：没有事件循环时一口气跑完"""
    bl_idname = "mbakery.run_to_finish"
    bl_label = "Run To Finish"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        active = session.Session.get()
        if active.job is None:
            return {'CANCELLED'}
        active.run_blocking()
        _redraw()
        return {'FINISHED'}


# ------------------------------------------------------------------------------------
#   第 4 页

# ------------------------------------------------------------------------------------
#   第 4 页：交付
#
#   全部是**烘焙之后**的独立动作 —— 不满意就直接不点，不用重跑。

def _require_report(self):
    """交付动作的前置条件。

    ⚠ 两种"有东西可交付"的合法状态：
      1. 这次会话真烘过（job + report 都在）；
      2. 从磁盘扫回来的贴图（用户强杀之后的情况）——
         以前这里只认第一种，于是"图都在硬盘上"却什么都点不动。
    ⚠ 状态 2 里 **`report` 是 None**：凡是会碰 `report.xxx` 的代码在状态 2 都会炸
      （实施轮 1.5 修的就是这个 —— 导出走了 `report.results`，用户真机上
       `AttributeError: 'NoneType' object has no attribute 'results'`）。
      所以**导出**那三个入口用的是 `_require_exportable()`：它们只要求"清单非空"，
      清单可以从报告来、也可以从贴图列表来（见 `deliver/textures.build_manifest`）。
    """
    active = session.Session.get()
    if active.imported_by_group:
        return active
    if active.job is None or active.report is None:
        _error(self, "Nothing has been baked yet — or scan the output "
                               "folder on the last page")
        return None
    return active


def _require_exportable(self, settings):
    """**导出**三个入口的前置条件：清单里有东西就行。

    ⚠ 与 `_require_report()` 的差别只有这一条，但它是实的：导出**不要求 report 存在**。
      三种状态都能导：① 这次真烘过（report）② 扫回来的贴图 ③ 重开文件后
      贴图列表还在（它挂在场景上，随 .blend 保存）。
      没有清单就明确报错 —— **不许**再出现 `AttributeError`。
    """
    from ..deliver import textures as textures_mod
    active = session.Session.get()
    if textures_mod.build_manifest(active.report, settings):
        return active
    _error(self, "Nothing to export — bake or scan the output folder first")
    return None


class MBAKERY_OT_RefreshTextures(bpy.types.Operator):
    bl_idname = "mbakery.refresh_textures"
    bl_label = "Refresh List"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        active = _require_report(self)
        if active is None:
            return {'CANCELLED'}
        count = active.refresh_textures(_settings(context))
        self.report({'INFO'}, "{} texture(s)".format(count))
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_ExportTextures(bpy.types.Operator):
    bl_idname = "mbakery.export_textures"
    bl_label = "Export Textures"
    bl_description = "Write the baked images to the output folder"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        from ..deliver import textures as textures_mod
        settings = _settings(context)
        active = _require_exportable(self, settings)
        if active is None:
            return {'CANCELLED'}
        directory = bpy.path.abspath(settings.output_dir) if settings.output_dir else ""
        if not directory:
            _error(self, "Set an output folder on the Settings page first")
            return {'CANCELLED'}

        if not settings.textures:
            active.refresh_textures(settings)
        only = active.enabled_texture_names(settings) or None
        # ⚠ 输入是**清单**不是报告：救援 / 重开文件的状态里没有报告（见 _require_exportable）
        manifest = textures_mod.build_manifest(active.report, settings)
        # ⚠ 实施轮 1.6：清单与逐张勾选**没有交集**时必须点名报错 ——
        #   以前会打印 "Exported 0 of 0 texture(s)" 然后什么都不发生（静默无操作，
        #   用户以为按钮坏了）。这种错配通常来自"烘完又在同一会话里扫了目录"：
        #   清单取本次报告、而勾选来自扫描列表。
        planned = textures_mod.exported_names(manifest, only)
        if manifest and not planned:
            _error(self, "Nothing selected to export — the texture list has {} entr(ies) but "
                         "none matches what is on screen. Press 'Refresh List' on this page "
                         "and try again".format(len(manifest)))
            return {'CANCELLED'}
        outcomes = textures_mod.export_report(
            manifest, directory, settings.file_format,
            group_subfolders=settings.use_subfolders, only=only,
            overwrite=settings.overwrite)
        active.export_outcomes = outcomes
        failed = [o for o in outcomes if not o.ok]
        for outcome in outcomes:
            if outcome.ok:
                active.add_log("EXPORT", outcome.path)
            else:
                active.add_log("FAIL", "{}: {}".format(outcome.name, outcome.message))
        # 把路径写回列表，第 4 页好显示
        by_name = {o.name: o for o in outcomes}
        for item in settings.textures:
            outcome = by_name.get(item.image_name)
            if outcome is not None and outcome.ok:
                item.exported_path = outcome.path
        self.report({'INFO'}, "Exported {} of {} texture(s)".format(
            len(outcomes) - len(failed), len(outcomes)))
        _redraw()
        return {'FINISHED' if not failed else 'CANCELLED'}


class MBAKERY_OT_ExportOneTexture(bpy.types.Operator):
    bl_idname = "mbakery.export_one_texture"
    bl_label = "Export"
    bl_options = {'INTERNAL'}

    index: bpy.props.IntProperty(default=-1)

    def execute(self, context):
        from ..deliver import textures as textures_mod
        settings = _settings(context)
        active = _require_exportable(self, settings)
        if active is None:
            return {'CANCELLED'}
        index = self.index if self.index >= 0 else settings.texture_index
        if not (0 <= index < len(settings.textures)):
            return {'CANCELLED'}
        item = settings.textures[index]
        directory = bpy.path.abspath(settings.output_dir) if settings.output_dir else ""
        if settings.use_subfolders and item.group:
            directory = os.path.join(directory, naming.sanitize_filename(item.group))
        image = bpy.data.images.get(item.image_name)
        # 逐张导出**永远给原图**（1.5 起合并只走"Export Base Color With Alpha"按钮）
        outcome = textures_mod.export_one(image, directory, settings.file_format,
                                          name=item.image_name,
                                          overwrite=settings.overwrite)
        if outcome.ok:
            item.exported_path = outcome.path
            active.add_log("EXPORT", outcome.path)
        else:
            active.add_log("FAIL", "{}: {}".format(item.image_name, outcome.message))
            _error(self, outcome.message)
        _redraw()
        return {'FINISHED' if outcome.ok else 'CANCELLED'}


class MBAKERY_OT_ToggleTextureGroup(bpy.types.Operator):
    bl_idname = "mbakery.toggle_texture_group"
    bl_label = "Show/Hide This Collection's Textures"
    bl_options = {'INTERNAL'}

    group: bpy.props.StringProperty(default="")

    def execute(self, context):
        settings = _settings(context)
        name = self.group or ""
        settings.texture_expanded_group = "" if settings.texture_expanded_group == name else name
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_ExpandTextures(bpy.types.Operator):
    bl_idname = "mbakery.expand_textures"
    bl_label = "Expand All / Collapse All"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        settings = _settings(context)
        settings.texture_expanded_group = "" if settings.texture_expanded_group == '*' else '*'
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_SelectAllTextures(bpy.types.Operator):
    bl_idname = "mbakery.select_all_textures"
    bl_label = "Select All"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        settings = _settings(context)
        enable = not all(item.enabled for item in settings.textures) if settings.textures else True
        for item in settings.textures:
            item.enabled = enable
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_BuildMaterials(bpy.types.Operator):
    bl_idname = "mbakery.build_materials"
    bl_label = "Build Materials"
    bl_description = "Create one material per collection from the baked textures"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        active = _require_report(self)
        if active is None:
            return {'CANCELLED'}
        settings = _settings(context)
        built, _reports = active.build_materials(settings)
        if not built:
            _error(self, "Nothing to build from — bake something, or scan "
                                   "the output folder for textures already on disk")
            return {'CANCELLED'}
        self.report({'INFO'}, "Built {} material(s)".format(built))
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_ApplyBakedMaterial(bpy.types.Operator):
    bl_idname = "mbakery.apply_baked_material"
    bl_label = "Add Baked Material Slot"
    bl_description = ("Append the baked material as an extra slot on each object. The "
                      "original slots are left untouched — assign the faces to preview it")
    bl_options = {'INTERNAL'}

    def execute(self, context):
        active = _require_report(self)
        if active is None:
            return {'CANCELLED'}
        added = active.apply_materials(_settings(context))
        if not added:
            _error(self, "Nothing to add a slot to — see the Log for why "
                                   "(already added, or nothing was baked)")
            return {'CANCELLED'}
        self.report({'INFO'}, "Baked slot added to {} object(s)".format(added))
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_PreviewBaked(bpy.types.Operator):
    """预览：把面分配到烘焙槽 / 切回原槽

    用户的原话："物体两个材质，只需使用分配材质就能预览成果" ——
    这个按钮就是那个"分配"。

    ⚠ 实施轮 1.1：Re-bake 下还要**连渲染层一起切**（烘焙材质用写入层、原材质用
      采样层），否则烘焙贴图会按旧层采样，而且"物体数据 → UV Maps"栏看起来
      "UV 没切过去"（用户报的）。
    """
    bl_idname = "mbakery.preview_baked"
    bl_label = "Preview"
    bl_description = ("Assign every face to the baked slot (or back to the original "
                      "slots). Same as the Assign button in the material properties. "
                      "In a Re-bake it also points the render UV layer at the baked "
                      "layer (and back)")
    bl_options = {'INTERNAL'}

    baked: bpy.props.BoolProperty(default=True)

    def execute(self, context):
        active = _require_report(self)
        if active is None:
            return {'CANCELLED'}
        changed = active.preview_materials(_settings(context), baked=self.baked)
        if not changed:
            _error(self, "No object has a baked slot yet — press Add Slot "
                                   "first (see the Log)")
            _redraw()
            return {'CANCELLED'}
        self.report({'INFO'}, "{} object(s) now show the {} material".format(
            changed, "baked" if self.baked else "original"))
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_FinalizeForExport(bpy.types.Operator):
    bl_idname = "mbakery.finalize_for_export"
    bl_label = "Finalize For Export"
    bl_description = ("DESTRUCTIVE: keep only the baked material slot and only the baked "
                      "UV layer (renamed 'UVMap') on every object, so the export carries "
                      "one material and one UV set. The extra UV layers are deleted for "
                      "good; Restore Original Slots only brings the material slots back. "
                      "Use Create Final Object to keep the original untouched")
    bl_options = {'INTERNAL'}

    def execute(self, context):
        active = _require_report(self)
        if active is None:
            return {'CANCELLED'}
        done = active.finalize_materials(_settings(context))
        if not done:
            _error(self, "Nothing to finalize — check the Log")
            return {'CANCELLED'}
        self.report({'INFO'}, "Finalized {} object(s) — one material slot, ready to "
                              "export".format(done))
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_RestoreOriginalMaterial(bpy.types.Operator):
    bl_idname = "mbakery.restore_original_material"
    bl_label = "Restore Original Slots"
    bl_description = "Put the original material slots and per-face indices back"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        from ..deliver import slots as slots_mod
        active = session.Session.get()
        outcomes = slots_mod.restore_objects(None)
        restored = [o for o in outcomes if o.ok]
        for outcome in outcomes:
            if outcome.ok:
                active.add_log("SLOT", "{} restored".format(outcome.object_name))
        _settings(context).compare_baked = False
        self.report({'INFO'}, "Restored {} object(s)".format(len(restored)))
        _redraw()
        return {'FINISHED' if restored else 'CANCELLED'}


class MBAKERY_OT_CompareToggle(bpy.types.Operator):
    """原材质 / 烘焙材质一键切换（**兼容旧入口，第 5 页不再画它**）

    实施轮 1.1 起第 5 页只留 `Preview Baked` / `Show Original` 两个按钮 ——
    它们做的事跟这里**完全一样**（同一个 `preview_materials`），所以 Compare 框被删掉。
    这个 operator 先留着：旧的快捷键/宏/别的面板可能还引用它，删掉会直接报错。
    """
    bl_idname = "mbakery.compare_toggle"
    bl_label = "Compare"
    bl_description = "Switch between the original and the baked material"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        """等价于"预览烘焙材质 / 切回原材质"（含 Re-bake 的渲染层切换）

        ⚠ 渲染层由 `preview_materials` 统一管（`_switch_preview_layers`）：
          Re-bake 下它会切到写入层 / 还回采样层，Bake 流程一个字都不动。
          这里不再自己碰 UV 层 —— 那条路以前会顺手切 `MBAKERY_UV`，而材质本来就
          不该引用那个层名（见 deliver/materials.py 的说明）。
        """
        active = _require_report(self)
        if active is None:
            return {'CANCELLED'}
        settings = _settings(context)
        show_baked = not settings.compare_baked
        changed = active.preview_materials(settings, baked=show_baked)
        if not changed:
            _error(self, "No object has a baked material slot yet")
            return {'CANCELLED'}
        self.report({'INFO'}, "Showing the {} material".format(
            "baked" if show_baked else "original"))
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_CreateFinalObjects(bpy.types.Operator):
    bl_idname = "mbakery.create_final_objects"
    bl_label = "Create Final Objects"
    bl_description = ("Duplicate the baked objects (copying their meshes) into a new "
                      "collection, and make each copy export-ready: a single UV layer "
                      "named 'UVMap' holding the baked layer. The originals are not "
                      "touched at all")
    bl_options = {'INTERNAL'}

    def execute(self, context):
        from ..deliver import objects as objects_mod
        from ..deliver import uv_layers
        active = _require_report(self)
        if active is None:
            return {'CANCELLED'}
        # ⚠ 只认**名字**：计划里的物体引用活不过一次撤销步（实施轮 1.2，用户那条
        #   Preview 崩溃就是这个机制）。`job` 还可能整个不在（从磁盘扫回来的那次），
        #   那时用救援计划 —— 以前这里直接 `active.job.plan`，救援路径下会 AttributeError。
        plan = active.job.plan if active.job is not None else active.import_plan()
        names = plan.object_names()
        if not names:
            _error(self, "Nothing to duplicate")
            return {'CANCELLED'}
        collection = objects_mod.default_collection(context.scene)
        created, skipped = objects_mod.create_final_objects(names, collection)
        active.created_objects = created
        for obj in created:
            active.add_log("OBJECT", "created {}".format(obj.name))
        for name, reason in skipped:
            active.add_log("SKIP", "{}: {}".format(name, reason))

        # 副本也**成品化**：只留写入层、改名 UVMap、设 active/active_render。
        # ⚠ 原物体一个字节都不动（副本的网格是复制出来的），但**材质是共享的** ——
        #   所以"材质引用层名"那条守卫在副本路径上照样要跑，否则删副本的层会把
        #   原物体用的材质一起弄坏。
        _sample_uv, write_uv = active.bake_layers()
        for outcome in uv_layers.finalize_layers_for(created, write_uv):
            if outcome.ok:
                active.add_log("FINALIZE", "{}: {}".format(outcome.object_name,
                                                           outcome.reason))
                for name, owners in outcome.blocked:
                    active.add_log("WARN", "'{}' layer {} is referenced by material {} "
                                           "— kept it".format(outcome.object_name, name,
                                                              ", ".join(owners)))
            else:
                active.add_log("SKIP", "{} UV layers: {}".format(outcome.object_name,
                                                                 outcome.reason))

        self.report({'INFO'}, "Created {} object(s) in '{}'".format(
            len(created), collection.name))
        _redraw()
        return {'FINISHED' if created else 'CANCELLED'}


class MBAKERY_OT_OpenOutput(bpy.types.Operator):
    bl_idname = "mbakery.open_output"
    bl_label = "Open Output Folder"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        settings = _settings(context)
        path = bpy.path.abspath(settings.output_dir) if settings.output_dir else ""
        if not path or not os.path.isdir(path):
            _error(self, "Output folder does not exist")
            return {'CANCELLED'}
        bpy.ops.wm.path_open(filepath=path)
        return {'FINISHED'}


CLASSES = (
    MBAKERY_OT_Next,
    MBAKERY_OT_Back,
    MBAKERY_OT_StartOver,
    MBAKERY_OT_GotoPage,
    MBAKERY_OT_RefreshGroups,
    MBAKERY_OT_AddMap,
    MBAKERY_OT_RemoveMap,
    MBAKERY_OT_MoveMap,
    MBAKERY_OT_DedupeMaps,
    MBAKERY_OT_ClearMaps,
    MBAKERY_OT_ToggleAllGroups,
    MBAKERY_OT_ReadUvSizes,
    MBAKERY_OT_StartBake,
    MBAKERY_OT_Rebake,
    MBAKERY_OT_PauseBake,
    MBAKERY_OT_ResumeBake,
    MBAKERY_OT_CancelBake,
    MBAKERY_OT_RunToFinish,
    MBAKERY_OT_RefreshTextures,
    MBAKERY_OT_ExportTextures,
    MBAKERY_OT_ExportOneTexture,
    MBAKERY_OT_SelectAllTextures,
    MBAKERY_OT_ToggleTextureGroup,
    MBAKERY_OT_ExpandTextures,
    MBAKERY_OT_BuildMaterials,
    MBAKERY_OT_ApplyBakedMaterial,
    MBAKERY_OT_PreviewBaked,
    MBAKERY_OT_FinalizeForExport,
    MBAKERY_OT_RestoreOriginalMaterial,
    MBAKERY_OT_CompareToggle,
    MBAKERY_OT_CreateFinalObjects,
    MBAKERY_OT_OpenOutput,
)

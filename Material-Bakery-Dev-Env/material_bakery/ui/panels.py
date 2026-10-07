# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   N 面板
#
#   严格顺序：顶部步骤指示 + 底部导航，只能上一步 / 下一步。
#   运行期间除取消外全部禁用。
#
#   分层规则：draw() 只读状态、只画控件，不做任何业务判断 ——
#   校验在 ops.checklist / ops.validate_targets，状态在 ui.session。
# ------------------------------------------------------------------------------------

import bpy

from ..core import bake_types, naming, scene_scan
from . import ops, properties, session


CATEGORY = "Material Bakery"

STEP_DOTS = ("\u25cf", "\u25cb")          # ● / ○

_ICON_CACHE = {}


def valid_icons():
    """这个 Blender 版本全部合法的 icon 名（从真 RNA 拿，只算一次）"""
    if "names" not in _ICON_CACHE:
        names = set()
        for function_name in ("label", "operator", "prop", "prop_enum",
                              "prop_search", "separator", "menu", "menu_pie"):
            try:
                parameters = (bpy.types.UILayout.bl_rna.functions[function_name]
                              .parameters)
            except (KeyError, AttributeError):
                continue
            prop = parameters.get("icon")
            if prop is None:
                continue
            names.update(item.identifier for item in prop.enum_items)
        _ICON_CACHE["names"] = names
        _ICON_CACHE["warned"] = set()
    return _ICON_CACHE["names"]


def safe_icon(name, fallback='NONE'):
    """一个 icon 名，保证是合法的。

    ⚠ 为什么需要这个：Blender 对 icon 参数是**严格**的 —— 传一个不存在的名字
      会抛 TypeError，而 draw() 一抛异常，这一页剩下的控件全都不画了。
      实测被咬过一次（'WARNING'）、又被咬过一次（'UV_ISLANDS'，还是位置参数传的，
      静态扫描第一版没扫到）。所以凡是**经过我们自己代码**的 icon 名都过一道闸：
      不合法就退成 fallback 并在控制台点一次名，绝不让它把界面打挂。
    """
    if not name or name in valid_icons():
        return name or fallback
    warned = _ICON_CACHE.setdefault("warned", set())
    if name not in warned:
        warned.add(name)
        print("[Material Bakery] 这个 Blender 版本没有 icon {!r}，已退化为 {!r}。"
              "请把源码里的名字改掉。".format(name, fallback))
    return fallback


def _active():
    return session.Session.get()


def _draw_draw_error(layout, exc):
    """draw() 出错时在面板里说出来，而不是静默少画一堆控件

    用户看到的是"按钮不见了"，光看界面根本猜不到是代码抛异常；
    而 Blender 只把 traceback 打到控制台。这里直接在面板上摊开，
    连带把 traceback 也打一份到控制台方便排查。
    """
    import traceback
    box = layout.box()
    box.label(text="Panel error — click for details in the console", icon='ERROR')
    box.label(text="{}: {}".format(type(exc).__name__, exc)[:120])
    print("[Material Bakery] 面板绘制失败:")
    traceback.print_exc()


def _box(layout, title, icon='NONE'):
    box = layout.box()
    row = box.row()
    # 所有分区标题都走这里 —— 在这一个点把 icon 名过闸，
    # 以后再有写错的 icon 名也只是少个图标，不会把整页打挂。
    row.label(text=title, icon=safe_icon(icon))
    return box


def tag_redraw_safe():
    """给 timer 用的重绘标记（timer 里不该直接碰 session 的复杂逻辑）"""
    from . import session
    session.Session.tag_redraw()


def _draw_texture_groups(layout, settings):
    """按集合分组的贴图列表（默认折叠）

    每行给出的信息是"用户真正要判断的东西"：哪个集合、几张、多大、成功没有；
    具体某一张要看细节时再展开。这样 96 张贴图只占 3~8 行。
    """
    groups = []
    for index, item in enumerate(settings.textures):
        entry = groups[-1] if groups and groups[-1][0] == item.group else None
        if entry is None:
            entry = (item.group, [], )
            groups.append(entry)
        entry[1].append((index, item))

    expanded = settings.texture_expanded_group
    for group_name, entries in groups:
        open_group = expanded == '*' or expanded == group_name
        sizes = sorted({(item.size_x, item.size_y or item.size_x)
                         for _index, item in entries})
        done = sum(1 for _i, item in entries if item.status == 'done')
        failed = sum(1 for _i, item in entries if item.status == 'failed')
        row = layout.row(align=True)
        op = row.operator("mbakery.toggle_texture_group", text="",
                          icon='TRIA_DOWN' if open_group else 'TRIA_RIGHT',
                          emboss=False)
        op.group = group_name
        row.label(text="{}".format(group_name or "(no collection)"))
        row.label(text="{} maps".format(len(entries)))
        row.label(text="/".join(naming.size_label(x, y) for x, y in sizes))
        icon = 'CHECKMARK' if done == len(entries) else (
            'CANCEL' if failed else 'BLANK1')
        row.label(text="", icon=icon)

        if not open_group:
            continue
        for index, item in entries:
            sub = layout.row(align=True)
            sub.label(text="")
            sub.prop(item, "enabled", text="")
            sub.label(text=item.type_label)
            sub.label(text=naming.size_label(item.size_x, item.size_y))
            icon = 'CHECKMARK' if item.exported_path else (
                'CANCEL' if item.status == 'failed' else 'BLANK1')
            sub.label(text="", icon=icon)
            export = sub.operator("mbakery.export_one_texture", text="", icon='FILE_TICK')
            export.index = index


def _draw_queue_window(layout, tasks, context=None, before=2, after=3):
    """只画"当前任务附近"的几行 —— 队列再长也不占地方

    128 张的规模下，把队列全铺出来既没用又卡（用户："死长，我都互动不了"）。
    真正要看的信息是"现在烘到哪、上面几张成没成、下面还有多少"。
    """
    total = len(tasks)
    if not total:
        layout.label(text="No tasks", icon='INFO')
        return
    current = 0
    if context is not None:
        for index, item in enumerate(tasks):
            if item is context:
                current = index
                break
    else:
        for index, item in enumerate(tasks):
            if item.status in ("baking", "pending"):
                current = index
                break
    start = max(0, current - before)
    end = min(total, current + after + 1)
    if start > 0:
        layout.label(text="... {} earlier".format(start), icon='BLANK1')
    for index in range(start, end):
        item = tasks[index]
        icon = {'done': 'CHECKMARK', 'failed': 'CANCEL', 'baking': 'RENDER_STILL',
                'skipped': 'INFO', 'pending': 'BLANK1'}.get(item.status, 'BLANK1')
        label = "{} {}  {}".format(item.group_name, item.bake_type.label,
                                   naming.size_label(item.size_x, item.size_y))
        if index == current and item.status == "baking":
            label += "   <- now"
        layout.label(text=label, icon=icon)
    if end < total:
        layout.label(text="... {} more".format(total - end), icon='BLANK1')


def _draw_log_box(box, active, limit=8):
    """日志框：优先显示本次会话的日志；没有就读磁盘上那份

    ⚠ 用户的原话："我的 log 没了"（他指的是 Blender 里那行**红色报错**，
      关掉 Blender 就没了）。所以两件事一起做：
        * 所有 ERROR 现在都会写进会话日志 → 同时落盘（见 ui/ops.py 的 _error）
        * 面板在没有会话日志时**把磁盘上那份显示出来**，标清是"上次运行"
      这样即使重开了文件，现场还在。
    """
    if active.log:
        for entry in active.log[-limit:]:
            icon = {'ERROR': 'ERROR', 'FAIL': 'CANCEL', 'WARN': 'INFO',
                    'CONFLICT': 'ERROR', 'SKIP': 'INFO',
                    'IMPORT': 'IMPORT', 'FINALIZE': 'EXPORT',
                    'PHASE': 'TIME',
                    'PREVIEW': 'HIDE_OFF'}.get(entry.level, 'BLANK1')
            box.label(text="{} {}".format(entry.stamp(), entry.message), icon=icon)
    else:
        tail = active.tail_log_file(limit)
        if tail:
            box.label(text="This session has no log — showing the last run:", icon='INFO')
            for line in tail:
                box.label(text=line[-120:])
        else:
            box.label(text="No log yet", icon='INFO')
    box.operator("mbakery.open_log_file", icon='FILE_FOLDER')


def count_sources(context, settings):
    """投影烘焙时源有几个 —— 第 2 页要显示，不然用户根本不知道选没选对

    ⚠ 必须用 active_provider()，不能写死 ProjectionProvider：
      写死的话第三方 provider 模式下这里仍然按 ProjectionProvider 数，
      数出来是"所有其它网格"（2 个），而实际会用 1 个 —— 显示和实际不一致。
    """
    from ..engine import providers
    from ..core import scene_scan as scan_mod
    try:
        bake_settings = session.settings_to_bake_settings(settings)
    except Exception:
        return 0, ""
    mode = bake_settings.source_mode
    if mode == 'PATTERN' and not bake_settings.source_pattern.strip():
        return 0, "Name pattern is empty"
    if mode == 'SELECTED':
        candidates = [o for o in context.selected_objects if scan_mod.is_bakeable_source(o)]
        if not candidates:
            return 0, "Nothing is selected in the viewport"
    provider = providers.active_provider(bake_settings)
    return len(provider.resolve_sources(
        scan_mod.ObjectGroup(name="?"), bake_settings, context)), ""


class MBAKERY_PT_Wizard(bpy.types.Panel):
    bl_idname = "MBAKERY_PT_wizard"
    bl_label = "Material Bakery"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = CATEGORY

    def draw(self, context):
        layout = self.layout
        settings = getattr(context.scene, "mbakery", None)
        if settings is None:
            layout.label(text="Material Bakery is not registered", icon='ERROR')
            return

        active = _active()
        if not active.is_running:
            # ⚠ 只在**没在跑**的时候校正页面：运行期间 properties.py 里的任何属性
            #   都不许写（旧引擎的崩溃就是派发之后写 IDProperty 引起的，
            #   见 ui/properties.py 顶部的说明）。
            settings.sync_page()
        # 第一级：这一轮是哪一类的活。下面的步骤条、页面内容都跟着它走。
        self.draw_workflow(layout, settings, active)
        self.draw_steps(layout, settings, active)
        # 版本 + 打包时间：一眼看出"你装的是哪一版"。
        # ⚠ 起因很实际：用户装了 zip 但 Blender 用的还是内存里的旧模块，
        #   我们为此查了一整轮"为什么修复没生效"。有了这行，一句话就能确认。
        try:
            from .. import __build__
            from .. import bl_info
            layout.label(text="v{} · build {}".format(
                ".".join(str(v) for v in bl_info.get("version", (0, 0, 0))), __build__),
                icon='INFO')
        except Exception:
            pass

        # ⚠ 页面主体包在 try 里，导航栏**永远**要画出来。
        #   为什么：draw() 里任何一处抛异常（icon 名写错、某个属性在这个
        #   Blender 版本上不存在、某个数据块被删了……），Blender 只会把错误
        #   打到控制台，**这一页剩下的控件全都不会画** —— 用户看到的就是
        #   "下一步按钮不见了"，而所有后台测试照样全绿（它们不调用真的 draw）。
        #   实测就是这么被咬的：有一页用了不存在的 icon 'WARNING'，
        #   于是那一页的 Back/Next 整个消失。
        try:
            if active.is_running:
                self.draw_bake(layout, context, settings)
            else:
                page = settings.page
                if page == properties.PAGE_TARGETS:
                    self.draw_targets(layout, context, settings)
                elif page == properties.PAGE_SETTINGS:
                    self.draw_settings(layout, context, settings)
                elif page == properties.PAGE_BAKE:
                    self.draw_bake(layout, context, settings)
                elif page == properties.PAGE_REBAKE_SOURCE:
                    self.draw_rebake_source(layout, context, settings)
                elif page == properties.PAGE_REBAKE_UVS:
                    self.draw_rebake_uvs(layout, context, settings)
                else:
                    self.draw_result(layout, context, settings)
        except Exception as exc:
            _draw_draw_error(layout, exc)

        self.draw_footer(layout, context, settings)

    # ------------------------------------------------------------------ 顶部

    def draw_workflow(self, layout, settings, active):
        """第一级：先选"哪一类的活"，下面才是这条流程自己的步骤条

        ⚠ 列表来自 ui/properties.WORKFLOWS —— **数据驱动**：以后加第三个工作流
          只需要往那张表和 WORKFLOW_STEPS 各加一行，这里一个字都不用改。
          而且绝不画空的/灰色的占位 tab（用户明确否掉了那个做法）。
        ⚠ 运行期间禁用：跑到一半换流程，页面和正在跑的 job 就对不上了。
        """
        row = layout.row()
        row.enabled = not active.is_running
        row.prop(settings, "workflow", expand=True)

    def draw_steps(self, layout, settings, active):
        steps = properties.steps_for(settings)
        index = settings.page_index()
        row = layout.row(align=True)
        for i, _step in enumerate(steps):
            row.label(text=STEP_DOTS[0 if i <= index else 1])
        label = steps[index][1] if 0 <= index < len(steps) else settings.page
        layout.label(text="Step {} of {} — {}".format(
            index + 1, len(steps), label), icon='INFO')

    # ------------------------------------------------------------------ 第 1 页

    def draw_targets(self, layout, context, settings):
        box = _box(layout, "Targets", 'OBJECT_DATAMODE')
        box.prop(settings, "target_mode", text="")
        if settings.target_mode == scene_scan.MODE_SINGLE:
            box.prop(settings, "single_collection", text="")
        if settings.target_mode == scene_scan.MODE_SELECTED:
            count = len([o for o in context.selected_objects if scene_scan.is_bakeable(o)])
            box.label(text="{} selected mesh object(s)".format(count),
                      icon='RESTRICT_SELECT_OFF')
        box.operator("mbakery.refresh_groups", icon='FILE_REFRESH')

        if settings.target_mode != scene_scan.MODE_SELECTED and settings.groups:
            box.operator("mbakery.toggle_all_groups", icon='CHECKBOX_DEHLT')
            column = box.column(align=True)
            for item in settings.groups:
                row = column.row(align=True)
                row.prop(item, "enabled", text="")
                row.label(text=item.name)
                detail = "{} obj".format(item.object_count)
                if item.missing_uv:
                    detail += ", {} no UV".format(item.missing_uv)
                if item.overlap_ratio > 0.001:
                    detail += ", overlap {:.0%}".format(item.overlap_ratio)
                row.label(text=detail)
                if item.has_conflict:
                    row.label(text="", icon='ERROR')

        map_box = _box(layout, "Maps", 'TEXTURE')
        maps = properties.active_maps(settings)
        map_index = properties.active_map_index(settings)
        if not maps:
            map_box.label(text="No bake item yet — press Add", icon='INFO')
        for index, item in enumerate(maps):
            row = map_box.row(align=True)
            row.prop(item, "enabled", text="")
            row.prop(item, "type_key", text="")
            # ⚠ 分辨率是**两个框**（先宽后高），不再是"一个正方形尺寸"。
            #   用户 2026-09 的原话："凭什么只能烘正方形的贴图"。
            #   原来这行尾巴上还有个"▸ options below"的标签，把它去掉了：
            #   专属参数就在下面那个 Type Options 框里，箭头+文字纯属占地方。
            row.prop(item, "size_x", text="W")
            row.prop(item, "size_y", text="H")

        row = map_box.row(align=True)
        row.operator("mbakery.add_map", text="", icon='ADD')
        row.operator("mbakery.remove_map", text="", icon='REMOVE')
        row.operator("mbakery.move_map", text="", icon='TRIA_UP').direction = 'UP'
        row.operator("mbakery.move_map", text="", icon='TRIA_DOWN').direction = 'DOWN'
        row.operator("mbakery.dedupe_maps", text="", icon='DUPLICATE')
        row.operator("mbakery.clear_maps", text="", icon='TRASH')

        current = maps[map_index] if maps and 0 <= map_index < len(maps) else None
        if current is not None:
            bake_type = current.bake_type
            options = _box(layout, "Type Options", 'PREFERENCES')
            if bake_type is None:
                options.label(text="Unknown type", icon='ERROR')
            else:
                options.label(text="{} — {} — {}".format(
                    bake_type.label, bake_type.group,
                    "Color" if not bake_type.channel.is_data else "Data (Non-Color)"))
                self.draw_type_options(options, current, bake_type)

        preview_box = _box(layout, "Name Preview", 'SORTALPHA')
        # ⚠ 带视图层：预览里不能出现"选都选不中"的集合，否则用户看到的命名
        #   与实际会烘的那批对不上（见 core/scene_scan.NO_VIEW_LAYER）。
        groups, _conflicts = scene_scan.scan(context.scene, context.view_layer)
        shown = 0
        for group in groups:
            if not group.objects or shown >= 5:
                continue
            object_name = group.objects[0].name
            prefix = naming.select_prefix(settings.prefix, group.name, object_name,
                                          not settings.prefix)
            for item in settings.maps[:2]:
                if not item.enabled:
                    continue
                label = bake_types.get(item.type_key)
                if label is None:
                    continue
                name = naming.render_name(settings.template, {
                    "prefix": prefix,
                    "type": naming.type_token(label.label),
                    "bridge": settings.bridge,
                    "size": naming.size_token(item.size_x, item.size_y),
                    "suffix": settings.suffix,
                })
                preview_box.label(text=name)
                shown += 1
                break
        if not shown:
            preview_box.label(text="Press Scan Scene to preview names")

    # ------------------------------------------------------------------ 类型专属参数

    def draw_type_options(self, layout, item, bake_type):
        """按类型只画它真正用得到的参数

        ⚠ 这些值必须**真的传到引擎**（见 MBAKERY_MapItem.options）。
          只画控件不接线的话，用户改了通道打包的 R/G/B 却毫无效果 ——
          比不显示这个控件更糟。
        """
        kind = bake_type.kind
        kinds = bake_types.BakeKind

        if kind is kinds.CHANNEL_PACK:
            column = layout.column(align=True)
            column.label(text="Channel sources (packed into R / G / B):")
            column.prop(item, "channel_r", text="R")
            column.prop(item, "channel_g", text="G")
            column.prop(item, "channel_b", text="B")
            if len({item.channel_r, item.channel_g, item.channel_b}) < 3:
                column.label(text="Two channels use the same source", icon='INFO')
            missing = [c for c in (item.channel_r, item.channel_g, item.channel_b)
                       if c and bake_types.get(c) is None]
            if missing:
                column.label(text="Unknown source in this preset", icon='ERROR')
        elif kind is kinds.AO_NODE:
            layout.prop(item, "ao_distance")
            layout.prop(item, "ao_samples")
            layout.prop(item, "ao_only_local")
            layout.label(text="Distance 0 uses the AO node default", icon='INFO')
        elif kind is kinds.POINTINESS:
            layout.prop(item, "pointiness_contrast")
            layout.prop(item, "pointiness_brightness")
        elif kind is kinds.UV_MAP:
            layout.prop(item, "uv_map", text="")
            layout.label(text="Empty = the active render UV layer", icon='INFO')
        elif kind is kinds.COLOR_ATTR:
            layout.prop(item, "attribute_name", text="")
            layout.label(text="Empty = the default color attribute", icon='INFO')
        elif bake_type.bake_pass == "NORMAL":
            layout.prop(item, "normal_space", text="")
            layout.label(text="Tangent is what game engines and glTF expect", icon='INFO')
        else:
            layout.label(text="This type has no options")
        layout.label(text="Bake pass: {}".format(bake_type.bake_pass))


    # ------------------------------------------------------------------ Re-bake 第 1 页

    def draw_rebake_source(self, layout, context, settings):
        """Re-bake 的目标与 Maps

        目标选择**沿用同一套扫描逻辑**（集合 / 单物体 / 选中），但跟 Bake 那套
        **各记一份**（甲方定的）—— 所以这里读的是 `rebake_*` 那几个属性，
        控件和措辞跟第一页一致，用户不用重新学一遍。
        """
        box = _box(layout, "Targets", 'OBJECT_DATAMODE')
        box.prop(settings, "rebake_target_mode", text="")
        if settings.rebake_target_mode == scene_scan.MODE_SINGLE:
            box.prop(settings, "rebake_single_collection", text="")
        if settings.rebake_target_mode == scene_scan.MODE_SELECTED:
            count = len([o for o in context.selected_objects if scene_scan.is_bakeable(o)])
            box.label(text="{} selected mesh object(s)".format(count),
                      icon='RESTRICT_SELECT_OFF')
        box.operator("mbakery.refresh_groups", icon='FILE_REFRESH')

        rows = {row["name"]: row for row in session.rebake_group_rows(context, settings)}
        if settings.rebake_target_mode != scene_scan.MODE_SELECTED and settings.rebake_groups:
            box.operator("mbakery.toggle_all_groups", icon='CHECKBOX_DEHLT')
            column = box.column(align=True)
            for item in settings.rebake_groups:
                info = rows.get(item.name)
                row = column.row(align=True)
                row.prop(item, "enabled", text="")
                row.label(text=item.name)
                detail = "{} obj".format(item.object_count)
                if info is None:
                    pass
                elif info["reason"]:
                    detail += ", {}".format(info["reason"])
                else:
                    detail += ", {} UV layer{}".format(
                        info["layers"], "" if info["layers"] == 1 else "s")
                    if info["missing"]:
                        detail += ", {} missing a layer".format(len(info["missing"]))
                row.label(text=detail)
                if (info is not None and info["missing"]) or item.has_conflict:
                    row.label(text="", icon='ERROR')

        # "全组是不是都具备这两层"——**一行结论**，别让用户自己去数
        stats = session.rebake_uv_stats(context, settings)
        missing = sorted(set(stats["missing_sample"]) | set(stats["missing_write"]))
        if not stats["objects"]:
            box.label(text="No target object in this selection", icon='ERROR')
        elif missing:
            box.label(text="{} of {} object(s) have both layers · {} missing".format(
                stats["objects"] - len(missing), stats["objects"], len(missing)),
                icon='ERROR')
            for name in missing[:3]:
                box.label(text="  {} — see the UV Sets page".format(name), icon='BLANK1')
        else:
            box.label(text="{} of {} object(s) have both layers · 0 missing".format(
                stats["objects"], stats["objects"]), icon='CHECKMARK')

        map_box = _box(layout, "Maps", 'TEXTURE')
        maps = properties.active_maps(settings)
        map_index = properties.active_map_index(settings)
        if not maps:
            map_box.label(text="No bake item yet — press Add", icon='INFO')
        for item in maps:
            row = map_box.row(align=True)
            row.prop(item, "enabled", text="")
            row.prop(item, "type_key", text="")
            row.prop(item, "size_x", text="W")
            row.prop(item, "size_y", text="H")
            # "分辨率是谁给的"必须写在脸上（甲方要求：显示来源）——
            # 否则用户看到一个数字，不知道是默认值还是从哪张图读来的。
            if item.size_source:
                row.label(text="", icon='FILE_IMAGE')
                row.label(text=item.size_source)

        row = map_box.row(align=True)
        row.operator("mbakery.add_map", text="", icon='ADD')
        row.operator("mbakery.remove_map", text="", icon='REMOVE')
        row.operator("mbakery.move_map", text="", icon='TRIA_UP').direction = 'UP'
        row.operator("mbakery.move_map", text="", icon='TRIA_DOWN').direction = 'DOWN'
        row.operator("mbakery.dedupe_maps", text="", icon='DUPLICATE')
        row.operator("mbakery.clear_maps", text="", icon='TRASH')
        # 重烘的源就是用户自己那批贴图 —— 尺寸该 **读它们的**，不是拍脑袋 2048
        map_box.operator("mbakery.read_uv_sizes", icon='FILE_REFRESH')
        read = [item for item in maps if item.size_source]
        if read:
            first = read[0]
            map_box.label(text="{}x{}  (from {})".format(
                first.size_x, first.size_y, first.size_source), icon='FILE_IMAGE')
            if len(read) > 1:
                map_box.label(text="... and {} more item(s) took their size from a "
                                   "source texture".format(len(read) - 1), icon='BLANK1')

        current = maps[map_index] if maps and 0 <= map_index < len(maps) else None
        if current is not None:
            bake_type = current.bake_type
            options = _box(layout, "Type Options", 'PREFERENCES')
            if bake_type is None:
                options.label(text="Unknown type", icon='ERROR')
            else:
                options.label(text="{} — {} — {}".format(
                    bake_type.label, bake_type.group,
                    "Color" if not bake_type.channel.is_data else "Data (Non-Color)"))
                self.draw_type_options(options, current, bake_type)

    # ------------------------------------------------------------------ Re-bake 第 2 页

    def draw_rebake_uvs(self, layout, context, settings):
        """采样层 / 写入层：这条流程唯一多出来的输入"""
        stats = session.rebake_uv_stats(context, settings)
        box = _box(layout, "UV Sets", 'UV')
        # 标签照实施轮 1.1 的裁定改清楚（用户原话："两个下拉不明不白"）：
        #   旧的（源）= 现在这些贴图在哪一层；新的（目标）= 烘到哪一层去
        box.prop(settings, "rebake_from_uv", text="Sample from (old UV)")
        box.prop(settings, "rebake_to_uv", text="Write to (new UV)")

        missing = sorted(set(stats["missing_sample"]) | set(stats["missing_write"]))
        if not stats["objects"]:
            box.label(text="No target object in this selection", icon='ERROR')
        elif missing:
            box.label(text="{} of {} object(s) have both layers · {} missing".format(
                stats["objects"] - len(missing), stats["objects"], len(missing)),
                icon='ERROR')
            for name in stats["missing_sample"][:3]:
                box.label(text="{} has no '{}'".format(name, stats["sample"]), icon='BLANK1')
            for name in stats["missing_write"][:3]:
                if name not in stats["missing_sample"]:
                    box.label(text="{} has no '{}'".format(name, stats["write"]),
                              icon='BLANK1')
        else:
            box.label(text="{} of {} object(s) have both layers · 0 missing".format(
                stats["objects"], stats["objects"]), icon='CHECKMARK')

        if stats["layers"]:
            box.label(text="Layers in this selection: {}".format(
                ", ".join(stats["layers"])), icon='INFO')

        note = _box(layout, "What This Run Does", 'INFO')
        # 这一行把**两个层名与物体计数**都写出来（甲方要求：别让用户猜）
        note.label(text="{} object(s) selected".format(stats["objects"]), icon='OBJECT_DATA')
        if stats["nothing"]:
            # 两侧都是 `None`：等于今天，什么都不注入、什么标志位都不动
            note.label(text="Both set to None — nothing is injected and no flag is "
                            "changed (exactly like the Bake workflow)", icon='CHECKMARK')
        else:
            # 实施轮 1.2：两侧各写各的（`None` 只影响它自己那一侧）
            if stats["no_pin"]:
                note.label(text="Sample from = None — the image textures are not pinned; "
                                "they follow each object's render layer", icon='INFO')
            else:
                note.label(text="Sampling '{}'".format(stats["sample"]), icon='UV')
                note.label(text="Each material's image textures get a UV Map node pinned "
                                "to the sample layer (before any Mapping node)", icon='INFO')
            if stats["write"]:
                note.label(text="The write layer becomes the render layer while baking, "
                                "and again when you preview the baked material — both "
                                "are put back afterwards", icon='INFO')
            else:
                note.label(text="Write to = None — the render layer is left alone",
                           icon='INFO')
        if stats["needs_swap"]:
            note.label(text="{} object(s) will temporarily render from '{}'".format(
                stats["needs_swap"], stats["write"]), icon='INFO')
        note.label(text="No UV coordinate is ever modified and no object is copied",
                   icon='CHECKMARK')

    # ------------------------------------------------------------------ 第 2 页 Remesh

    def draw_settings(self, layout, context, settings):
        preset = _box(layout, "Preset", 'PRESET')
        preset.prop(settings, "preset_name", text="")
        row = preset.row(align=True)
        row.operator("mbakery.load_preset", icon='IMPORT')
        row.operator("mbakery.save_preset", icon='EXPORT')
        row.operator("mbakery.delete_preset", text="", icon='TRASH')
        preset.operator("mbakery.open_presets_folder", icon='FILE_FOLDER')

        naming_box = _box(layout, "Naming", 'SORTALPHA')
        # 方案下拉列表：用户要"列表"，不要每次手写占位符。
        # 选一个方案 = 一次把 template / bridge / suffix 三件套都设好。
        naming_box.prop(settings, "template_preset", text="")
        naming_box.prop(settings, "template", text="")
        ok, message = naming.validate_template(settings.template)
        # ⚠ 校验通过时 message 是空串，直接画出来就是一个**没有文字的绿钩**
        #   （用户报的"意义不明的钩，后面没有文字解释"就是这个，
        #    另一个同类在 ops.checklist 里，两处都要补文案）。
        naming_box.label(text=message or "Name template is valid",
                         icon='CHECKMARK' if ok else 'ERROR')
        if settings.template:
            # ⚠ 预览用的是**当前烘焙列表里第一项的真实分辨率**，不再是写死的 2048。
            #   用户 2026-09："这里贴图的分辨率也得重做了" —— 改了分辨率却看到
            #   预览还写着 2k，那就等于预览在骗人。列表为空时退回 2048²。
            sample_x = sample_y = 2048
            for item in properties.active_maps(settings):
                if item.enabled:
                    sample_x, sample_y = item.size_x, item.size_y or item.size_x
                    break
            sample = naming.render_name(settings.template, {
                "prefix": "Body", "type": "BaseColor", "bridge": settings.bridge,
                "size": naming.size_token(sample_x, sample_y), "suffix": settings.suffix})
            naming_box.label(text="e.g. " + sample, icon='FILE_IMAGE')
        row = naming_box.row(align=True)
        row.prop(settings, "prefix", text="Prefix")
        row = naming_box.row(align=True)
        row.prop(settings, "bridge", text="Bridge")
        row.prop(settings, "suffix", text="Suffix")

        out = _box(layout, "Output", 'FILE_FOLDER')
        out.prop(settings, "output_dir", text="")
        out.prop(settings, "file_format", text="")
        row = out.row(align=True)
        row.prop(settings, "use_subfolders")
        row.prop(settings, "overwrite")
        out.prop(settings, "fake_user")
        out.prop(settings, "pack_into_blend")

        quality = _box(layout, "Quality", 'SETTINGS')
        # 采样分两组 + 每组一个自定义输入框（用户要求）。
        # ⚠ 输入框一直是**可编辑**的：改数字就把那一组切到 Custom，
        #   不做成灰的 —— "改了没反应"是这个项目反复踩过的坑。
        quality.prop(settings, "samples_mode")
        row = quality.row(align=True)
        row.prop(settings, "samples_custom")
        row.label(text="samples for channel maps")
        quality.prop(settings, "sampled_maps_mode")
        row = quality.row(align=True)
        row.prop(settings, "sampled_maps_custom")
        row.label(text="samples for AO / shadow / light passes")
        quality.label(text="Effective: {} for channel maps, {} for sampled maps "
                           "(scene value is restored afterwards)".format(
                               session.resolve_samples(context, settings),
                               session.resolve_samples(context, settings, sampled=True)),
                      icon='INFO')
        quality.prop(settings, "bake_method")
        if settings.bake_method == 'NATIVE':
            quality.label(text="Uses Cycles' own passes. Channels without one "
                               "(Metallic, Alpha, ...) fall back to Emission", icon='INFO')
        else:
            # ⚠ 默认模式下的这个例外必须写在脸上（2026-09-26 实测）：手术会把表面换成
            #   没有 Normal 输入的 Emission，Bump / Normal Map 链随之脱离求值 ——
            #   那是用户报的"重烘结果唯独少了法线"。法线通道现在一律走原生 NORMAL pass。
            quality.label(text="Normal is always baked with Cycles' own NORMAL pass — "
                               "node surgery would drop the bump / normal map chain",
                          icon='INFO')
        # ⚠ 这个钩原来只有控件没有任何文字（用户："意义不明的钩，后面没有文字解释"）。
        #   边距和"自适应边距"是一对，放在同一行并且都写清楚。
        row = quality.row(align=True)
        row.prop(settings, "margin")
        row.prop(settings, "adaptive_margin")
        if settings.adaptive_margin:
            quality.label(text="Margin scales with the texture size (up to 64 px)",
                          icon='INFO')
        row = quality.row(align=True)
        row.prop(settings, "antialias", text="")
        if settings.antialias != 'OFF':
            row.prop(settings, "aa_scale", text="x")
        if settings.antialias == 'DOWNSCALE':
            quality.label(text="Bakes at {}x then shrinks — slower but cleaner".format(
                settings.aa_scale), icon='INFO')
        # ⚠ 这个开关必须带着后果一起出现。藏起来的物体会从 AO / 阴影 / 光路
        #   通道里消失 —— 那是"结果变了但界面什么都没说"的典型来源。
        #   具体哪几个通道受影响，由 Checklist 的 sample_rows 点名。
        quality.prop(settings, "hide_unrelated")
        if settings.hide_unrelated:
            quality.label(text="Only this run's meshes stay in the render — lights and "
                               "cameras are never hidden", icon='INFO')

        tiles = _box(layout, "UDIM", 'IMAGE_ZDEPTH')
        tiles.prop(settings, "udim")
        if settings.udim:
            from ..core import udim as udim_mod
            groups, _conflicts = scene_scan.scan(context.scene, context.view_layer)
            found = 0
            for group in groups:
                group_tiles = udim_mod.group_tiles(group.objects)
                if len(group_tiles) > 1:
                    tiles.label(text="{}: {} tiles ({}-{})".format(
                        group.name, len(group_tiles), group_tiles[0], group_tiles[-1]),
                        icon='CHECKMARK')
                    found += 1
            if not found:
                tiles.label(text="No object spans more than one tile", icon='INFO')

        if properties.is_rebake(settings):
            # Re-bake 这条流程**没有** SmartUV 那一块、也没有 Selected to Active：
            # 它烘的是**同一批物体**的另一层 UV，谁都不展、也不从别处投影。
            # 把这两块藏起来比画成灰的清楚（灰的会让人以为"这里还能开"）。
            uv_note = _box(layout, "UV Layers", 'UV')
            uv_note.label(text="Re-bake uses the two layers picked on the UV Sets page — "
                               "nothing is unwrapped and no UV coordinate is touched",
                          icon='CHECKMARK')
        else:
            uv = _box(layout, "UV", 'UV')
            uv.prop(settings, "prepare_uv")
            if settings.prepare_uv:
                uv.prop(settings, "unwrap_target")
                uv.label(text="Only objects WITHOUT a UV map are unwrapped — everything else "
                              "is left alone", icon='INFO')
            else:
                missing = scene_scan.missing_uv_objects(
                    [o for o in context.scene.objects if scene_scan.is_bakeable(o)])
                if missing:
                    uv.label(text="{} object(s) have no UV and will be skipped".format(
                        len(missing)), icon='INFO')
                else:
                    uv.label(text="Every bakeable object has a UV map", icon='CHECKMARK')
            # ⚠ 这里原来写的是"每集合共用一张贴图、UV 会被打包" —— 2026-09 之后
            #   **不再打包**：一个集合一张贴图，但用的是各物体**现在的** UV，
            #   一个字节都不动。重叠是用户自己的安排（镜像件共用一块纹理），我们不碰。
            uv.label(text="Baking uses each object's current UVs — nothing is repacked",
                     icon='CHECKMARK')
            overlap_note = [item for item in settings.groups if item.overlap_ratio > 0.001]
            if overlap_note:
                uv.label(text="{} collection(s) have overlapping UVs — that is fine here, "
                              "but overlapping faces share the same texels".format(
                                  len(overlap_note)), icon='INFO')

            project = _box(layout, "Selected to Active", 'MOD_UVPROJECT')
            project.prop(settings, "use_selected_to_active")
            if settings.use_selected_to_active:
                project.label(text="Targets stay as they are; detail is projected from the "
                                   "sources onto them", icon='INFO')
                project.prop(settings, "source_mode", text="")
                if settings.source_mode == 'PATTERN':
                    project.prop(settings, "source_pattern", text="")
                row = project.row(align=True)
                row.prop(settings, "max_ray_distance")
                row.prop(settings, "cage_extrusion")
                project.prop(settings, "unhide_sources")
                sources, reason = count_sources(context, settings)
                if reason:
                    project.label(text=reason, icon='ERROR')
                else:
                    project.label(text="{} source object(s) will be sampled".format(sources),
                                  icon='CHECKMARK')

        check = _box(layout, "Checklist", 'CHECKMARK')
        rows = ops.checklist(context, settings)
        problems = 0
        for level, message in rows:
            icon = {'OK': 'CHECKMARK', 'WARN': 'INFO', 'ERROR': 'ERROR'}.get(level, 'INFO')
            if level == 'ERROR':
                problems += 1
            check.label(text=message, icon=icon)
        if problems:
            check.label(text="Fix {} problem(s) before baking".format(problems), icon='ERROR')

    # ------------------------------------------------------------------ 第 3 页

    def draw_bake(self, layout, context, settings):
        active = _active()
        box = _box(layout, "Progress", 'TIME')

        if active.job is None:
            box.label(text="Nothing baked yet in this session", icon='INFO')
            if settings.output_dir:
                box.label(text="Output: " + bpy.path.abspath(settings.output_dir))
            box.operator("mbakery.start_bake", icon='RENDER_STILL')
            return

        done, total, fraction = active.progress()
        job = active.job
        box.progress(factor=fraction, text="{}/{}  ({:.0%})".format(done, total, fraction))
        # 状态行要写清楚**现在在干什么**：准备阶段和烘焙阶段分得开，
        # 否则进度条动了但用户不知道在忙什么，就像卡住了。
        box.label(text=job.status_line(),
                  icon='RENDER_STILL' if active.is_running else 'CHECKMARK')
        if getattr(job, "paused", False):
            box.label(text="Paused", icon='PAUSE')

        eta = active.eta()
        if active.is_running and eta is not None:
            box.label(text="About {:.0f}s remaining".format(eta), icon='TIME')
        if active.is_running:
            # 运行期间全局撤销和自动保存是**停掉**的（见 core/guard.py）：
            # 用户的文件打包后 6 GB，自动保存会在烘焙刚结束时补写一份，
            # 表现就是"烘完了界面卡住十几分钟"。这件事必须写在脸上，
            # 否则他会以为插件把他的 Ctrl+Z 弄坏了。
            guard = getattr(active, "guard", None)
            if guard is not None and guard.engaged:
                box.label(text="Global undo and auto-save are paused while baking — "
                               "they come back when the run ends", icon='INFO')
        if not active.is_running and active.report is not None:
            box.label(text=active.report.summary(),
                      icon='CHECKMARK' if active.report.ok else 'ERROR')

        # ⚠ 暂停 / 停止要**显眼**：单独一行、加高。
        #   之前它们混在底下一排小图标里；而烘焙一旦卡住界面就完全点不到，
        #   用户的实际感受就是"这插件没有暂停和停止功能"。
        controls = layout.row(align=True)
        controls.scale_y = 1.5
        if active.is_running:
            if getattr(job, "paused", False):
                controls.operator("mbakery.resume_bake", text="Resume", icon='PLAY')
            else:
                controls.operator("mbakery.pause_bake", text="Pause", icon='PAUSE')
            controls.operator("mbakery.cancel_bake", text="Stop", icon='CANCEL')
        else:
            if active.report is not None and not active.report.ok:
                controls.operator("mbakery.rebake", text="Bake Again",
                                  icon='FILE_REFRESH')
            else:
                controls.operator("mbakery.start_bake", text="Bake", icon='RENDER_STILL')
        if job is not None and not active.is_running:
            controls.operator("mbakery.run_to_finish", text="", icon='FF')

        # ⚠ 队列改成**紧凑**的。原来是"最多画 60 行"，128 张的规模下就是个
        #   滚不完的列表（用户："这个烘焙列表死长，我都互动不了"），而且每 0.1 秒
        #   重画一次 60 行本身就是负担。
        #   现在只给：一行统计 + 当前任务前后的少量上下文 + "还有 N 条"。
        queue = _box(layout, "Queue", 'LINENUMBERS_ON')
        tasks = job.plan.tasks
        counts = {'done': 0, 'failed': 0, 'skipped': 0, 'pending': 0, 'baking': 0}
        for item in tasks:
            counts[item.status] = counts.get(item.status, 0) + 1
        queue.label(text="{} done · {} failed · {} left".format(
            counts['done'], counts['failed'],
            counts['pending'] + counts['baking']), icon='LINENUMBERS_ON')
        _draw_queue_window(queue, tasks, context=job.current_task())

        # 日志框：没有会话日志时会显示磁盘上那份（用户"log 没了"的补救）
        _draw_log_box(_box(layout, "Log", 'CONSOLE'), active)


    # ------------------------------------------------------------------ 第 4 页

    def draw_result(self, layout, context, settings):
        active = _active()
        # ⚠ 先认"文件里已经烘过的材质"：用户上次烘完保存过、或者只是没关文件，
        #   就应该能直接做交付动作，不必先扫目录（他的原话："要是用户之前烘焙过了，
        #   有材质了就能直接进行烘焙后的操作"）。
        active.detect_existing(settings)
        report = active.report
        # ⚠ 三个条件缺一不可。少了 `imported_by_group` 会出这个 bug（用户实测）：
        #   扫完目录之后——没有报告、材质还没建（要再点 Build Materials）——
        #   条件仍然成立，于是页面又画成"救援入口"并 **直接 return**：
        #   128 张贴图一张不显示、UV 判断不显示、**连 Build Materials 按钮都没有**，
        #   看起来就像"扫了没用"。其实数据全在内存里躺着。
        if report is None and not active.materials_by_group \
                and not active.imported_by_group:
            box = _box(layout, "Summary", 'CHECKMARK')
            # ⚠ 这里原来直接 `return` —— 于是"这次会话没有报告"就等于整页作废，
            #   连按钮都不画。用户那次强杀之后正是这个局面：21.7 分钟的图都在硬盘上，
            #   第 5 页却什么都做不了。现在给一条**从磁盘救回来**的路。
            self._draw_rescue(box, layout, context, settings, active)
            return

        # ⚠ 每个 box 单独兜异常。
        #   用户按 Ctrl+Z 撤销"Remove Slot"之后，会话里握着的材质引用变成了
        #   已释放的 bpy_struct（撤销会删掉后建的材质数据块），
        #   draw() 一碰它就抛 —— 而整页是一个大 try，于是**下面所有 box
        #   （创建最终物体、报告）全都不画了**。他报的就是"面板下面的东西都没了"。
        #   现在一个 box 坏掉只坏它自己，并且把异常写进界面和日志。
        self._safe_box(layout, "Summary", 'CHECKMARK',
                       lambda box: self._draw_summary(box, active, report))
        # ⚠ 这里原来有一个 "Compare" 框（`compare_baked` 勾 + `mbakery.compare_toggle`）。
        #   实施轮 1.1 删掉：它跟下面 Materials 盒里的 `Preview Baked` /
        #   `Show Original` **完全是同一件事**（同一个 preview_materials），
        #   用户的原话就是"跟 Preview Baked / Show Original 功能一样"。
        #   `compare_baked` 属性与 `compare_toggle` 这个 operator **保留**
        #   （前者由 preview_materials / restore 维护，后者可能有旧入口引用），
        #   只是不再有独立控件。
        self._safe_box(layout, "Textures", 'TEXTURE',
                       lambda box: self._draw_textures(box, settings))
        self._safe_box(layout, "Materials", 'MATERIAL',
                       lambda box: self._draw_materials(box, active, settings))
        self._safe_box(layout, "Objects", 'OUTLINER_OB_MESH',
                       lambda box: self._draw_objects(box, active))
        self._safe_box(layout, "Report", 'TEXT',
                       lambda box: box.operator("mbakery.save_report", icon='FILE_TICK'))
        self._safe_box(layout, "Log", 'CONSOLE',
                       lambda box: _draw_log_box(box, active))

    def _safe_box(self, layout, title, icon, draw):
        """画一个 box；这个 box 内部出错不影响后面的 box

        这是给"撤销把数据块弄没了"这类情况兜底的：宁可这一小块显示一行红字，
        也不能让整页剩下的控件全部消失（用户报过）。

        ⚠ 兜底代码自己也要绝对安全：第一版这里直接调 `compat.log(...)`，
          而 panels.py 当时**没有 import compat** —— 于是处理异常的那段自己抛
          NameError 逃出去，把整页剩下的 box 又带崩了（跟要修的问题一模一样）。
        ⚠ 异常还要**写进会话日志**（进而落盘）。用户报过"第 5 页的按钮没了"，
          而日志里什么都没有 —— 因为面板异常当时只进控制台。
          现在打开 `Open Session Log` 就能看到是哪一块、什么异常。
        """
        box = None
        try:
            box = _box(layout, title, icon)
            draw(box)
        except Exception as exc:
            detail = "{}: {}".format(type(exc).__name__, exc)
            try:
                from .. import compat
                compat.log("{} box 画失败: {}".format(title, detail))
            except Exception:
                pass
            try:
                session.Session.get().add_log(
                    "ERROR", "'{}' panel section failed to draw — {} "
                             "(the rest of the page still works)".format(title, detail))
            except Exception:
                pass
            try:
                box = box if box is not None else _box(layout, title, icon)
                box.label(text="{} could not be drawn: {}".format(title, detail[:60]),
                          icon='ERROR')
            except Exception:
                pass

    def _draw_summary(self, box, active, report):
        if report is None:
            # 没有这次会话的报告，但文件里有已烘的材质 —— 说清楚，别让人以为坏了
            box.label(text="No report from this session", icon='INFO')
            box.label(text="Found {} baked material(s) already in this file".format(
                len(active.valid_materials_by_group())), icon='CHECKMARK')
            box.label(text="{} object(s) carry a baked slot".format(
                len(active.objects_with_baked_slot())), icon='INFO')
            return
        row = box.row(align=True)
        row.label(text="OK {}".format(report.done), icon='CHECKMARK')
        row.label(text="Failed {}".format(report.failed),
                  icon='CANCEL' if report.failed else 'BLANK1')
        row.label(text="Skipped {}".format(len(report.skipped)))
        row.label(text="{:.1f}s".format(report.seconds))
        for result in report.failed_results()[:5]:
            box.label(text="{} {}: {}".format(result.group, result.type_label, result.error),
                      icon='ERROR')
        if active.imported_by_group:
            self._draw_import_banner(box, active)

    def _draw_textures(self, box, settings):
        if settings.textures:
            # ⚠ 以前是**一张贴图一行**：96 张贴图 = 96 行 + 每行 4 个控件，
            #   面板被淹掉（用户："太长太杂了"）。
            #   现在按集合分组：默认只看得到"集合名 + 几张 + 多大 + 状态"，
            #   点箭头才展开这一组的贴图。展开状态记在 texture_expanded_group。
            _draw_texture_groups(box, settings)
            row = box.row(align=True)
            row.operator("mbakery.export_textures", icon='FILE_TICK')
            row.operator("mbakery.expand_textures", text="",
                          icon='TRIA_DOWN' if settings.texture_expanded_group != '*'
                          else 'TRIA_UP')
            row.operator("mbakery.select_all_textures", text="", icon='CHECKBOX_DEHLT')
            row.operator("mbakery.refresh_textures", text="", icon='FILE_REFRESH')
            # ⚠ 实施轮 1.6：这里不再有任何"带/不带 alpha"的控件 —— 用户定的规矩是
            #   "**默认的 basecolor 就是带 alpha**，别分家了"（原话见 core/alpha_merge.py）。
            #   1.4 的勾选框与 1.5 的 `Export Base Color With Alpha` 按钮**都删掉了**。
        else:
            box.operator("mbakery.refresh_textures", icon='FILE_REFRESH')
        # ⚠ 扫目录**常驻**：不管有没有报告、有没有材质，都应该能重新扫一次。
        #   以前它只出现在"什么都没有"的救援入口里 —— 一旦文件里认出了材质，
        #   这条路就整个消失了（用户："是一定要有过烘焙记录才能扫描吗？？？"）。
        row = box.row(align=True)
        row.operator("mbakery.scan_output", text="Scan Output Folder", icon='IMPORT')
        row.operator("mbakery.choose_texture_folder", text="", icon='FILE_FOLDER')
        if settings.output_dir:
            box.operator("mbakery.open_output", icon='FILE_FOLDER')

    def _draw_materials(self, box, active, settings):
        """交付三步：加槽 → 分配面预览 → 收尾导出

        ⚠ 这里以前还画了两次 `material_style` 下拉（用户："两个一样的下拉选单，
          都是 bsdf，这是写多了吗" —— 确实是重复的，而且那个选项本身也该去掉：
          `TexImage Only` 出来的材质没有着色器接到输出，用户还得自己接，
          属于把半成品丢给用户）。
          现在**只有一个方案**：Principled BSDF，不再给假的二选一。
        ⚠ 材质**列表**单独兜异常：它只是"信息"，而下面的按钮才是**操作**。
          用户报过"扫完目录之后按钮全没了" —— 列表里任何一处意外都不许
          把 Add Slot / Preview / 收尾这些操作连坐掉。
        """
        try:
            valid = active.valid_materials_by_group()
            for group_name, (material, link_report) in valid.items():
                box.label(text="{} -> {}".format(group_name, material.name),
                          icon='CHECKMARK')
                # link_report 可能是 None：**认出来的**（文件里本来就有的）材质没有
                # 本次运行的接线报告 —— 直接 .skipped 会 AttributeError。
                if link_report is None:
                    box.label(text="  (from a previous run — no link report)", icon='BLANK1')
                    continue
                for type_key, reason in link_report.skipped[:3]:
                    box.label(text="  {} not wired: {}".format(type_key, reason),
                              icon='INFO')
        except Exception as exc:
            from .. import compat
            detail = "{}: {}".format(type(exc).__name__, exc)
            compat.log("材质列表画失败: {}".format(detail))
            try:
                session.Session.get().add_log(
                    "ERROR", "the Materials list could not be drawn — {} "
                             "(the buttons below still work)".format(detail))
            except Exception:
                pass
            box.label(text="material list unavailable: {}".format(detail)[:70], icon='ERROR')
            valid = {}

        if not valid:
            box.operator("mbakery.build_materials", text="Build Materials", icon='MATERIAL')

        with_slot = active.objects_with_baked_slot()
        box.label(text="{} object(s) have the baked slot".format(len(with_slot)),
                  icon='CHECKMARK' if with_slot else 'INFO')
        # 有材质但还没有槽时，直接把两个按钮都摆上（用户的流程是加槽 → 预览）
        row = box.row(align=True)
        row.operator("mbakery.apply_baked_material", text="Add Slot", icon='ADD')
        row.operator("mbakery.preview_baked", text="Preview Baked",
                     icon='HIDE_OFF').baked = True
        row.operator("mbakery.preview_baked", text="Show Original",
                     icon='LOOP_BACK').baked = False
        box.label(text="Add Slot appends the baked material; Preview assigns the faces "
                       "to it (Add Slot alone does not change the look)", icon='INFO')
        # ⚠ 实施轮 1.2（回应 1.1 报告 §5-3）：预览**只是临时**地把渲染 UV 层指过去。
        #   交付材质故意不接 `UVMap` 节点（钉层名会让"材质硬引用一个按物体存在的层"
        #   那个老坑回来 —— 甲方裁定维持），所以"新 UV 永久生效"只有成品化这一条路。
        #   只在 Re-bake 里说这句话：Bake 流程两个层名都是空串，切层根本不会发生。
        if properties.is_rebake(settings):
            box.label(text="Preview is temporary — it only points the render UV layer at "
                           "the baked layer. To make the new UV permanent, use Keep Only "
                           "Baked or Create Final Object", icon='INFO')
        box.separator()
        # 收尾和"移除"是**相反**的两件事，标签必须说清，不然很容易以为是同一个：
        #   收尾   = 删掉**旧**槽 + **旧 UV 层**，只留烘焙材质与写入层 → 导出干净
        #   移除   = 删掉**烘焙**槽，把原材质槽与渲染层还回来（撤销我这步操作）
        # ⚠ 实施轮 1.1 起收尾**不可逆**（UV 层是真的删掉了）—— 文案必须是明确的
        #   破坏性警告，不能再写"reversible"（用户 2026-09-26 的反馈 3）。
        box.operator("mbakery.finalize_for_export",
                     text="Keep Only Baked (Export Ready)", icon='EXPORT')
        box.label(text="Finalize is DESTRUCTIVE: original slots AND extra UV layers are "
                       "deleted, the baked layer is renamed \"UVMap\". Use Create Final "
                       "Object to keep the original intact.", icon='ERROR')
        box.operator("mbakery.restore_original_material",
                     text="Remove Baked Slot (Undo)", icon='X')
        box.label(text="Remove deletes the BAKED slot and brings the original slots and "
                       "render layer back — it does not bring deleted UV layers back",
                  icon='INFO')

    def _draw_objects(self, box, active):
        box.operator("mbakery.create_final_objects", icon='DUPLICATE')
        box.label(text="Copies are made export-ready (one UV layer named 'UVMap', the "
                       "baked one) — the originals keep all their UV layers", icon='INFO')
        created = active.valid_created_objects()
        if created:
            box.label(text="{} object(s): {}".format(
                len(created), ", ".join(o.name for o in created[:3])), icon='CHECKMARK')

    def _draw_import_banner(self, layout, active):
        """告诉用户"这些贴图是从磁盘扫来的"，并且 UV 对不对得上"""
        box = _box(layout, "Imported From Disk", 'IMPORT')
        total = sum(len(images) for images in active.imported_by_group.values())
        box.label(text="{} texture(s) in {} collection(s)".format(
            total, len(active.imported_by_group)), icon='CHECKMARK')
        if active.import_source:
            box.label(text=active.import_source)
        state, detail = active.uv_state()
        if state == "matched":
            box.label(text="These textures used the meshes' own UVs — all good",
                      icon='CHECKMARK')
        elif state == "unknown":
            box.label(text="UV check: {}".format(detail), icon='INFO')
        else:
            # 旧版（打包布局）烘出来的贴图。插件现在已经不打包 UV 了，
            # 所以没法让它们对上 —— 只能重烘一次。
            box.label(text="These textures came from an OLDER packed bake", icon='ERROR')
            box.label(text=detail)
            box.label(text="Bake again to get textures that use your own UVs", icon='INFO')
        if active.import_unmatched:
            box.label(text="{} file(s) not recognised (renamed?)".format(
                len(active.import_unmatched)), icon='INFO')
            for path, reason in active.import_unmatched[:3]:
                box.label(text="  {}: {}".format(path, reason), icon='BLANK1')
        row = box.row(align=True)
        row.operator("mbakery.scan_output", text="Rescan", icon='FILE_REFRESH')
        row.operator("mbakery.choose_texture_folder", text="", icon='FILE_FOLDER')
        row.operator("mbakery.clear_imported", text="", icon='TRASH')

    def _draw_rescue(self, box, layout, context, settings, active):
        """没有报告时画的"救援"入口"""
        box.label(text="Nothing baked in this session", icon='INFO')
        box.label(text="If textures are already on disk, scan for them:", icon='INFO')
        row = box.row(align=True)
        row.operator("mbakery.scan_output", text="Scan Output Folder", icon='IMPORT')
        row.operator("mbakery.choose_texture_folder", text="", icon='FILE_FOLDER')
        if settings.output_dir:
            box.label(text=bpy.path.abspath(settings.output_dir))
        else:
            box.label(text="No output folder set — use the folder button", icon='INFO')
        box.operator("mbakery.open_log_file", text="Open Last Run Log", icon='CONSOLE')
        layout.label(text="Or bake something first.", icon='INFO')

    # ------------------------------------------------------------------ 底部

    def draw_footer(self, layout, context, settings):
        steps = properties.steps_for(settings)
        index = settings.page_index()
        active = _active()
        row = layout.row(align=True)
        row.enabled = not active.is_running
        row.operator("mbakery.back", text="Back", icon='TRIA_LEFT')
        if index < len(steps) - 1:
            next_label = "Next: {}".format(steps[index + 1][1])
            row.operator("mbakery.next", text=next_label, icon='TRIA_RIGHT')
        else:
            row.operator("mbakery.start_over", icon='FILE_REFRESH')


CLASSES = (MBAKERY_PT_Wizard,)

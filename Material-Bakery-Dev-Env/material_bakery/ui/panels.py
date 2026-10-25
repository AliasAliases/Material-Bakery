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

import textwrap

import bpy

from ..core import naming, scene_scan
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
    box.label(text=_wrap("{}: {}".format(type(exc).__name__, exc)))
    print("[Material Bakery] 面板绘制失败:")
    traceback.print_exc()


def _box(layout, title, icon='NONE'):
    box = layout.box()
    row = box.row()
    # 所有分区标题都走这里 —— 在这一个点把 icon 名过闸，
    # 以后再有写错的 icon 名也只是少个图标，不会把整页打挂。
    row.label(text=title, icon=safe_icon(icon))
    return box


def _wrap(text, width=64):
    """把一句说明 / 警告 / 错误折成多行（Blender 的 label 认 `\\n`）

    ⚠ 用户的原话："你的提示、警告全都被省略了，能不能自动换行之类的"。
      `textwrap.fill` 只折行、不丢字 —— 把 `\\n` 去掉拼回去等于原文，
      所以面板上不会再出现 `…` / `...` 那种截断。
    ⚠ 不能写成一个超长单行：Blender 会把它裁掉，用户照样看不全。
    ⚠ 这个 helper 必须留在本文件**前面**：strip_qr.py 的 cut() 从被剥离的
      那一页提示函数一直切到 `def _draw_texture_groups`（公开变体没有那一页），
      写在那一段里会被一起切掉，面板当场 NameError。
    """
    return textwrap.fill(str(text), width=width)


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


# ------------------------------------------------------------------------------------
#   卡26 · 常驻 info 行（卡29 起折叠区整体删除：解释一律常驻、一律换行）
#
#   ⚠ 这一段必须放在 `_draw_texture_groups` **之后**：
#     strip_qr.py 的 cut() 从被剥离的那个页面提示函数一直切到
#     `def _draw_texture_groups` —— helper 写在前面会被一起切掉，面板当场 NameError。
# ------------------------------------------------------------------------------------

def _info(layout, text, icon='INFO'):
    """**唯一**的常驻 info 行入口（测试靠它数每个盒子的 info 行数）"""
    layout.label(text=_wrap(text), icon=icon)


def _hint(layout, text):
    """一行解释 —— 无条件画（卡29：不再有"点开才画"），长句按 `_wrap` 折行"""
    layout.label(text=_wrap(text), icon='INFO')


def _notes(layout, hints):
    """一束解释行的**唯一**入口：每句一行，每行都折行

    ⚠ 以前这些句子收在 ⓘ Details 里，收起时一个像素都不占 —— 用户的原话是
      "加这么多▲details干啥 …… 这些Details都没写解释啊"。现在没有折叠区了，
      调用点把句子交到这里，就等于常驻。
    """
    for text in hints:
        _hint(layout, text)


def _single_group_summary(groups, collection_name):
    """Single 模式那一行摘要：`"{N} object(s) · {M} material(s)"`

    ⚠ 卡29 1.1：Single 模式下下拉已经指定了集合，再画一遍逐组勾选列表就是
      把同一个信息说两次。没选集合 / 选中的集合已经不在场景里时**说人话** ——
      画 "0 object(s)" 会让人以为"这个集合是空的"，而不是"你还没选"。
    """
    if not collection_name:
        return "Pick a collection"
    for item in groups:
        if item.name == collection_name:
            return "{} object(s) · {} material(s)".format(
                item.object_count, item.material_count)
    return "Nothing to bake in this collection"


def _draw_udim(box, context, settings):
    """UDIM 开关 + 跨瓦片清单（卡29 1.8：不再单独占一个盒子，并进 UV 框）

    ⚠ 没有跨 tile 的物体时**不说废话**（不画 "No object spans more than one tile"）。
    """
    box.prop(settings, "udim")
    if not settings.udim:
        return
    from ..core import udim as udim_mod
    groups, _conflicts = scene_scan.scan(context.scene, context.view_layer)
    for group in groups:
        group_tiles = udim_mod.group_tiles(group.objects)
        if len(group_tiles) > 1:
            box.label(text="{}: {} tiles ({}-{})".format(
                group.name, len(group_tiles), group_tiles[0], group_tiles[-1]),
                icon='CHECKMARK')


def _name_sample(settings):
    """Settings › Naming 那一行样例 —— **每次 draw 现算**，不缓存、不写属性

    ⚠ 用的是"当前设置 + 当前 Maps 的真实值"：模板 / 连接符 / 后缀 / 前缀 /
      第一个启用项的类型与分辨率，任何一个变了，下一帧就跟着变 ——
      所以不存在"预览陈旧"。
    """
    item = None
    for entry in properties.active_maps(settings):
        if entry.enabled:
            item = entry
            break
    if item is None:
        type_token = "BaseColor"
        size_token = naming.size_token(properties.DEFAULT_SIZE, properties.DEFAULT_SIZE)
    else:
        bake_type = item.bake_type
        type_token = naming.type_token(bake_type.label) if bake_type else "BaseColor"
        size_token = naming.size_token(item.size_x, item.size_y or item.size_x)
    groups = [entry.name for entry in properties.active_groups(settings)
              if entry.enabled]
    prefix = naming.select_prefix(settings.prefix, groups[0] if groups else "",
                                  "Body", not settings.prefix)
    return naming.render_name(settings.template, {
        "prefix": prefix,
        "type": type_token,
        "bridge": settings.bridge,
        "size": size_token,
        "suffix": settings.suffix,
    })


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


def _draw_log_box(box, active, settings, limit=8):
    """日志框：优先显示本次会话的日志；没有就读磁盘上那份

    ⚠ 用户的原话："我的 log 没了"（他指的是 Blender 里那行**红色报错**，
      关掉 Blender 就没了）。所以两件事一起做：
        * 所有 ERROR 现在都会写进会话日志 → 同时落盘（见 ui/ops.py 的 _error）
        * 面板在没有会话日志时**把磁盘上那份显示出来**，标清是"上次运行"
      这样即使重开了文件，现场还在。

    ⚠ 卡26：默认收起，只占一行 "Show Log"。
      自动展开 = 正在跑 **或** 这次会话有错误（取消不算错误，见
      Session.has_errors）。auto 时不画切换 —— 运行中写 Scene 属性是禁区；
      auto 之外是用户自己的状态：手动展开后成功结束也保持展开，反过来也一样，
      不擅自反复打开。
    ⚠ draw **永不写** log_expanded。
    """
    manual = bool(getattr(settings, "log_expanded", False))
    auto = active.is_running or active.has_errors()
    if not (manual or auto):
        row = box.row(align=True)
        row.operator("mbakery.toggle_log", text="Show Log", icon='TRIA_RIGHT',
                     emboss=False)
        return
    if not auto:
        row = box.row(align=True)
        row.operator("mbakery.toggle_log", text="Hide Log", icon='TRIA_DOWN',
                     emboss=False)
    if active.log:
        for entry in active.log[-limit:]:
            icon = {'ERROR': 'ERROR', 'FAIL': 'CANCEL', 'WARN': 'INFO',
                    'CONFLICT': 'ERROR', 'SKIP': 'INFO',
                    'IMPORT': 'IMPORT', 'FINALIZE': 'EXPORT',
                    'PHASE': 'TIME',
                    'PREVIEW': 'HIDE_OFF'}.get(entry.level, 'BLANK1')
            # 卡29 1.7：log 里那些长报错（路径 / 异常原文）现在折行显示，
            # 不再被 Blender 从右边裁掉 —— 用户的原话："提示、警告全都被省略了"。
            box.label(text=_wrap("{} {}".format(entry.stamp(), entry.message)), icon=icon)
    else:
        tail = active.tail_log_file(limit)
        if tail:
            box.label(text="This session has no log — showing the last run:", icon='INFO')
            for line in tail:
                # 卡29 1.7：日志行也折行，不再按字符数截断（长路径会被折断，
                # 但一个字都不丢）。
                box.label(text=_wrap(line))
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
        box = _box(layout, "Materials" if properties.is_per_material(settings) else "Targets", 'OBJECT_DATAMODE')
        box.prop(settings, "target_mode", text="")
        single = settings.target_mode == scene_scan.MODE_SINGLE
        if single:
            box.prop(settings, "single_collection", text="")
        if settings.target_mode == scene_scan.MODE_SELECTED:
            count = len([o for o in context.selected_objects if scene_scan.is_bakeable(o)])
            box.label(text="{} selected mesh object(s)".format(count),
                      icon='RESTRICT_SELECT_OFF')
        box.operator("mbakery.refresh_groups", icon='FILE_REFRESH')

        # 卡29 1.1：Single 模式下**只画**下拉 + 一行摘要。
        #   下拉已经指名了那一个集合，再画一遍逐组勾选列表就是把同一件事说两次
        #   （用户："很多非必要信息都塞在我脸上"）。Collections 模式照旧画列表。
        if single:
            box.label(text=_single_group_summary(settings.groups,
                                                 settings.selected_collection()),
                      icon='INFO')
        elif settings.target_mode == scene_scan.MODE_COLLECTIONS and settings.groups:
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
        if not maps:
            map_box.label(text="No bake item yet — press Add", icon='INFO')
        for index, item in enumerate(maps):
            row = map_box.row(align=True)
            row.prop(item, "enabled", text="")
            row.prop(item, "type_key", text="")
            # ⚠ 分辨率是**两个框**（先宽后高），不再是"一个正方形尺寸"。
            #   用户 2026-09 的原话："凭什么只能烘正方形的贴图"。
            row.prop(item, "size_x", text="W")
            row.prop(item, "size_y", text="H")

        # 卡26：六个常驻图标收成 `+` / `−` / `▾`（▾ 是 ops.MBAKERY_MT_Maps）。
        # 原有操作一个都不丢：加 / 删在明面上，移动 / 去重 / 清空进菜单。
        row = map_box.row(align=True)
        row.operator("mbakery.add_map", text="", icon='ADD')
        row.operator("mbakery.remove_map", text="", icon='REMOVE')
        row.menu("MBAKERY_MT_maps", text="", icon='COLLAPSEMENU')

    # ------------------------------------------------------------------ Re-bake 第 1 页

    def draw_rebake_source(self, layout, context, settings):
        """Re-bake 的目标与 Maps

        目标选择**沿用同一套扫描逻辑**（集合 / 单物体 / 选中），但跟 Bake 那套
        **各记一份**（甲方定的）—— 所以这里读的是 `rebake_*` 那几个属性，
        控件和措辞跟第一页一致，用户不用重新学一遍。
        """
        box = _box(layout, "Materials" if properties.is_per_material(settings) else "Targets", 'OBJECT_DATAMODE')
        box.prop(settings, "rebake_target_mode", text="")
        single = settings.rebake_target_mode == scene_scan.MODE_SINGLE
        if single:
            box.prop(settings, "rebake_single_collection", text="")
        if settings.rebake_target_mode == scene_scan.MODE_SELECTED:
            count = len([o for o in context.selected_objects if scene_scan.is_bakeable(o)])
            box.label(text="{} selected mesh object(s)".format(count),
                      icon='RESTRICT_SELECT_OFF')
        box.operator("mbakery.refresh_groups", icon='FILE_REFRESH')

        # 卡29 1.1：跟 Bake 那一页同一个处理 —— Single 只留下拉 + 一行摘要。
        if single:
            box.label(text=_single_group_summary(settings.rebake_groups,
                                                 settings.selected_rebake_collection()),
                      icon='INFO')
        elif settings.rebake_target_mode == scene_scan.MODE_COLLECTIONS and settings.rebake_groups:
            rows = {row["name"]: row
                    for row in session.rebake_group_rows(context, settings)}
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

        # "全组是不是都具备这两层"——**只在缺的时候说**（卡26：成功分支删掉，
        # 全齐的情况下上面每行已经写了"N UV layer(s)"，不需要再夸一遍）。
        stats = session.rebake_uv_stats(context, settings)
        missing = sorted(set(stats["missing_sample"]) | set(stats["missing_write"]))
        if not stats["objects"]:
            box.label(text="No target object in this selection", icon='ERROR')
        elif missing:
            box.label(text=_wrap("{} of {} object(s) have both layers · {} missing".format(
                stats["objects"] - len(missing), stats["objects"], len(missing))),
                icon='ERROR')
            for name in missing[:3]:
                box.label(text="  {} — see the UV Sets page".format(name), icon='BLANK1')

        map_box = _box(layout, "Maps", 'TEXTURE')
        maps = properties.active_maps(settings)
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

        # 卡26：同上 —— `+` / `−` / `▾`；重烘多出来的 Read Sizes 在菜单里。
        row = map_box.row(align=True)
        row.operator("mbakery.add_map", text="", icon='ADD')
        row.operator("mbakery.remove_map", text="", icon='REMOVE')
        row.menu("MBAKERY_MT_maps", text="", icon='COLLAPSEMENU')
        # 重烘的源就是用户自己那批贴图 —— 尺寸该 **读它们的**，不是拍脑袋 2048
        # （这一行是"已经读了哪张"的读数，不是开关；开关在 ▾ 菜单里）
        read = [item for item in maps if item.size_source]
        if read:
            first = read[0]
            map_box.label(text="{}x{}  (from {})".format(
                first.size_x, first.size_y, first.size_source), icon='FILE_IMAGE')
            if len(read) > 1:
                map_box.label(text="... and {} more item(s) took their size from a "
                                   "source texture".format(len(read) - 1), icon='BLANK1')

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
            box.label(text=_wrap("{} of {} object(s) have both layers · {} missing".format(
                stats["objects"] - len(missing), stats["objects"], len(missing))),
                icon='ERROR')
            for name in stats["missing_sample"][:3]:
                box.label(text=_wrap("{} has no '{}'".format(name, stats["sample"])),
                          icon='BLANK1')
            for name in stats["missing_write"][:3]:
                if name not in stats["missing_sample"]:
                    box.label(text=_wrap("{} has no '{}'".format(name, stats["write"])),
                              icon='BLANK1')
        # 卡26：全齐时不再画一行 "N of N · 0 missing"（成功文案直接删掉）。

        if stats["layers"]:
            box.label(text=_wrap("Layers in this selection: {}".format(
                ", ".join(stats["layers"]))), icon='INFO')

        note = _box(layout, "What This Run Does", 'INFO')
        # 这一行把**物体计数**写出来（甲方要求：别让用户猜）——这是数据行，常驻。
        note.label(text="{} object(s) selected".format(stats["objects"]), icon='OBJECT_DATA')
        # 卡29 1.4：下面这些解释原来收在 details_uv 里，现在**常驻**（每句一行，走 _wrap）。
        # ⚠ 这里面每一句都是**解释**，不是数据；"No UV coordinate is ever modified"
        #   那条成功文案按清单直接删掉（不是移进来）。
        hints = []
        if stats["nothing"]:
            # 两侧都是 `None`：等于今天，什么都不注入、什么标志位都不动
            hints.append("Both set to None — nothing is injected and no flag is "
                         "changed (exactly like the Bake workflow)")
        else:
            # 实施轮 1.2：两侧各写各的（`None` 只影响它自己那一侧）
            if stats["no_pin"]:
                hints.append("Sample from = None — the image textures are not pinned; "
                             "they follow each object's render layer")
            else:
                hints.append("Sampling '{}'".format(stats["sample"]))
                hints.append("Each material's image textures get a UV Map node pinned "
                             "to the sample layer (before any Mapping node)")
            if stats["write"]:
                hints.append("The write layer becomes the render layer while baking, "
                             "and again when you preview the baked material — both "
                             "are put back afterwards")
            else:
                hints.append("Write to = None — the render layer is left alone")
        if stats["needs_swap"]:
            hints.append("{} object(s) will temporarily render from '{}'".format(
                stats["needs_swap"], stats["write"]))
        _notes(note, hints)

    # ------------------------------------------------------------------ 第 2 页 Settings

    def draw_settings(self, layout, context, settings):
        if properties.is_per_material(settings):
            # 卡25：布局与 Tile Size 只属于 Per Material。Packed 时这里只剩一个下拉，
            # Tiling 时才展开 Tile Size 与实时 px/m 读数。
            layout_box = _box(layout, "Per Material Layout", 'UV')
            layout_box.prop(settings, "per_material_layout", expand=True)
            # 卡29 1.9：枚举下面**常驻**一句人话解释（以前只有枚举标签，用户不知道
            # 两种布局到底差在哪）。每句都走 _wrap，不截断。
            if settings.per_material_layout == 'PACKED':
                _hint(layout_box, "Each material's UVs are unwrapped and packed into 0-1. "
                                  "One image per material, no repetition.")
            else:
                _hint(layout_box, "One image repeats every Tile Size metres. UVs are written "
                                  "in world space, so the pattern continues across the surface.")
            if settings.per_material_layout == 'TILING':
                from ..core import plan as plan_mod

                layout_box.prop(settings, "tile_size")
                tile = float(settings.tile_size or 0.0) or 1.0
                maps = properties.active_maps(settings)
                active_index = properties.active_map_index(settings)
                chosen = None
                if 0 <= active_index < len(maps) and maps[active_index].enabled:
                    chosen = maps[active_index]
                if chosen is None:
                    chosen = next((item for item in maps if item.enabled), None)
                if chosen is None:
                    layout_box.label(text="px/m —  (no enabled map)", icon='INFO')
                else:
                    size_x, size_y = chosen.size_x, chosen.size_y or chosen.size_x
                    px_x, px_y = plan_mod.pixels_per_meter(size_x, size_y, tile)
                    if size_x == size_y:
                        text = "{:.0f} px/m  ({} px over {:.3g} m)".format(
                            px_x, size_x, tile)
                    else:
                        text = "X {:.0f} px/m · Y {:.0f} px/m  ({} × {} px over {:.3g} m)".format(
                            px_x, px_y, size_x, size_y, tile)
                    layout_box.label(text=text, icon='IMAGE_ZDEPTH')
                # 限制说明是 ERROR 级、必须常驻（卡26 第 7 条：ERROR 行不进 Details）。
                layout_box.label(
                    text=_wrap("Baked on a temporary 1 m x 1 m plane. Materials driven by "
                               "object UVs or geometry may not match the original surface."),
                    icon='ERROR')
                # 卡29 1.4：这句原来收在 ⓘ Details 里，现在常驻。
                _hint(layout_box, "Tile Size changes the repeat period of the delivered "
                                  "material; it does not re-bake.")

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
        # 卡26：**健康状态不显示**（"Name template is valid" 那类绿字只在失败时有用）。
        # 这一盒常驻**一行**：合法 -> 实时样例；非法 -> 错误原文。
        # ⚠ 样例每次 draw 现算（模板 / 连接符 / 后缀 / 前缀 / 第一个启用项的
        #   真实类型与分辨率），不缓存、不写任何属性 -> 不会陈旧。
        # ⚠ 原第 1 页那个 5 行的 Name Preview 盒删掉了：同一事实不该重复显示。
        if ok:
            _info(naming_box, "e.g. " + _name_sample(settings), 'FILE_IMAGE')
        else:
            naming_box.label(text=_wrap(message or "The name template is not valid"),
                             icon='ERROR')
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
        quality.label(text=_wrap("Effective: {} for channel maps, {} for sampled maps "
                                 "(scene value is restored afterwards)".format(
                                     session.resolve_samples(context, settings),
                                     session.resolve_samples(context, settings, sampled=True))),
                      icon='INFO')
        # 卡29 1.4：Quality 的解释性句子原来收在 details_quality 里，现在**常驻**
        #   （每句一行、走 _wrap）。第 7 条要求常驻的那句 "Normal is always … NORMAL pass"
        #   与 hide_unrelated 的后果行也仍然在明面上。
        quality.prop(settings, "bake_method")
        if settings.bake_method == 'NATIVE':
            _hint(quality, "Uses Cycles' own passes. Channels without one "
                           "(Metallic, Alpha, ...) fall back to Emission")
        else:
            # ⚠ 默认模式下的这个例外必须写在脸上（2026-09-26 实测）：手术会把表面换成
            #   没有 Normal 输入的 Emission，Bump / Normal Map 链随之脱离求值 ——
            #   那是用户报的"重烘结果唯独少了法线"。法线通道现在一律走原生 NORMAL pass。
            _hint(quality, "Normal is always baked with Cycles' own NORMAL pass — "
                           "node surgery would drop the bump / normal map chain")
        # ⚠ 这个钩原来只有控件没有任何文字（用户："意义不明的钩，后面没有文字解释"）。
        #   边距和"自适应边距"是一对，放在同一行并且都写清楚。
        row = quality.row(align=True)
        row.prop(settings, "margin")
        row.prop(settings, "adaptive_margin")
        if settings.adaptive_margin:
            _hint(quality, "Margin scales with the texture size (up to 64 px)")
        # 卡29 1.3：抗锯齿必须自己报名 —— 以前那行只写了个 "Off"，
        #   用户原话："关于抗锯齿的选项都没明确标注，看着就是个 off，谁知道它是 anti-aliasing"。
        row = quality.row(align=True)
        row.prop(settings, "antialias", text="Anti-Aliasing")
        if settings.antialias != 'OFF':
            row.prop(settings, "aa_scale", text="Scale")
        if settings.antialias == 'DOWNSCALE':
            _hint(quality, "Bakes at {}x then shrinks — slower but cleaner".format(
                settings.aa_scale))
        # ⚠ 这个开关必须带着后果一起出现。藏起来的物体会从 AO / 阴影 / 光路
        #   通道里消失 —— 那是"结果变了但界面什么都没说"的典型来源。
        #   具体哪几个通道受影响，由 Checklist 的 sample_rows 点名。
        quality.prop(settings, "hide_unrelated")
        if settings.hide_unrelated:
            _info(quality, "Only this run's meshes stay in the render — lights and "
                           "cameras are never hidden")

        if properties.is_rebake(settings):
            # Re-bake 这条流程**没有** SmartUV 那一块、也没有 Selected to Active：
            # 它烘的是**同一批物体**的另一层 UV，谁都不展、也不从别处投影。
            # 把这两块藏起来比画成灰的清楚（灰的会让人以为"这里还能开"）。
            uv_note = _box(layout, "UV Layers", 'UV')
            # 卡29 1.4：这句原来收在 ⓘ Details 里，现在常驻（走 _wrap）。
            _hint(uv_note, "Re-bake uses the two layers picked on the UV Sets page — "
                           "nothing is unwrapped and no UV coordinate is touched")
            # 卡29 1.8：UDIM 不再单独占一个盒子 —— 并进本框（见下）。
            uv_note.separator()
            _draw_udim(uv_note, context, settings)
        else:
            uv = _box(layout, "UV", 'UV')
            uv_hints = []
            uv.prop(settings, "prepare_uv")
            if settings.prepare_uv:
                uv.prop(settings, "unwrap_target")
                uv_hints.append("Only objects WITHOUT a UV map are unwrapped — everything "
                                "else is left alone")
            else:
                missing = scene_scan.missing_uv_objects(
                    [o for o in context.scene.objects if scene_scan.is_bakeable(o)])
                if missing:
                    uv.label(text=_wrap("{} object(s) have no UV and will be skipped".format(
                        len(missing))), icon='INFO')
                # 卡26：全都有 UV 时不再画 "Every bakeable object has a UV map"。
            # ⚠ 这里原来写的是"每集合共用一张贴图、UV 会被打包" —— 2026-09 之后
            #   **不再打包**：一个集合一张贴图，但用的是各物体**现在的** UV，
            #   一个字节都不动。重叠是用户自己的安排（镜像件共用一块纹理），我们不碰。
            #   卡26：这条成功文案（"nothing is repacked"）直接删掉；
            #   重叠那句是解释，卡29 起常驻。
            overlap_note = [item for item in settings.groups if item.overlap_ratio > 0.001]
            if overlap_note:
                uv_hints.append("{} collection(s) have overlapping UVs — that is fine here, "
                                "but overlapping faces share the same texels".format(
                                    len(overlap_note)))
            _notes(uv, uv_hints)
            # 卡29 1.8：UDIM 开关与跨瓦片清单并进 UV 框（Prepare UV 之后、separator 之后）。
            uv.separator()
            _draw_udim(uv, context, settings)

            project = _box(layout, "Selected to Active", 'MOD_UVPROJECT')
            project.prop(settings, "use_selected_to_active")
            if settings.use_selected_to_active:
                project.prop(settings, "source_mode", text="")
                if settings.source_mode == 'PATTERN':
                    project.prop(settings, "source_pattern", text="")
                row = project.row(align=True)
                row.prop(settings, "max_ray_distance")
                row.prop(settings, "cage_extrusion")
                project.prop(settings, "unhide_sources")
                sources, reason = count_sources(context, settings)
                if reason:
                    project.label(text=_wrap(reason), icon='ERROR')
                # 卡26：成功 source 计数（"N source object(s) will be sampled"）删掉。
                # 卡29 1.4：这句解释原来收在 ⓘ Details 里，现在常驻。
                _hint(project, "Targets stay as they are; detail is projected from the "
                               "sources onto them")

        check = _box(layout, "Checklist", 'CHECKMARK')
        rows = ops.checklist(context, settings)
        # 卡26：只画 WARN / ERROR（健康状态不占地方）。数据层 ops.checklist()
        # **一个字节都没改** —— 那些 OK 行仍然照常算出来，只是界面不再画。
        # 卡29 1.7：每一行都折行，不再截断（行数由 check.label 的 \n 决定）。
        shown = 0
        for level, message in rows:
            if level not in ('WARN', 'ERROR'):
                continue
            check.label(text=_wrap(message),
                        icon='ERROR' if level == 'ERROR' else 'INFO')
            shown += 1
        if not shown:
            _info(check, "All checks passed", 'CHECKMARK')

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
        # 卡29 1.4：运行期间"全局撤销 / 自动保存被暂停"这段解释原来收在
        # details_bake 里，现在只要此刻真的暂停了就**常驻**画出来。
        if active.is_running:
            # 运行期间全局撤销和自动保存是**停掉**的（见 core/guard.py）：
            # 用户的文件打包后 6 GB，自动保存会在烘焙刚结束时补写一份，
            # 表现就是"烘完了界面卡住十几分钟"。这件事必须写在脸上，
            # 否则他会以为插件把他的 Ctrl+Z 弄坏了。
            guard = getattr(active, "guard", None)
            if guard is not None and guard.engaged:
                _hint(box, "Global undo and auto-save are paused while baking — "
                           "they come back when the run ends")
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
        _draw_log_box(_box(layout, "Log", 'CONSOLE'), active, settings)


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
                       lambda box: self._draw_summary(box, active, report, settings))
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
                       lambda box: self._draw_objects(box, active, settings))
        self._safe_box(layout, "Report", 'TEXT',
                       lambda box: box.operator("mbakery.save_report", icon='FILE_TICK'))
        self._safe_box(layout, "Log", 'CONSOLE',
                       lambda box: _draw_log_box(box, active, settings))

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
                box.label(text=_wrap("{} could not be drawn: {}".format(title, detail)),
                          icon='ERROR')
            except Exception:
                pass

    def _draw_summary(self, box, active, report, settings):
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
        # 卡29 1.7：失败条目**折行**显示，不再截断（`[:5]` 只是条数上限，不动）。
        for result in report.failed_results()[:5]:
            box.label(text=_wrap("{} {}: {}".format(
                result.group, result.type_label, result.error)), icon='ERROR')
        if active.imported_by_group:
            self._draw_import_banner(box, active, settings)

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
        ⚠ 材质**列表**单独兜异常：它只是"信息"，而**操作**（Add Slot / Preview /
          收尾）任何一处意外都不许把它连坐掉。
          用户报过"扫完目录之后按钮全没了" —— 所以卡29 起顺序固定为
          **先按钮、后列表**：列表哪怕整段炸了，页面上该点的东西也早就在了。
        ⚠ 列表默认**收起**（卡29 1.2）：96 个材质铺出来就是第二屏；
          收起时只占最后那一行 `N material(s) in M object(s)` + 三角形。
        """
        valid = {}
        try:
            valid = active.valid_materials_by_group()
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
            box.label(text=_wrap("material list unavailable: {}".format(detail)),
                      icon='ERROR')
            valid = {}

        # ① 只有"一个有效材质都没有"时才给 Build Materials
        if not valid:
            box.operator("mbakery.build_materials", text="Build Materials", icon='MATERIAL')

        # ② 三个交付按钮（卡26：槽计数的成功行删掉；有材质但还没有槽时也照样摆上）
        row = box.row(align=True)
        row.operator("mbakery.apply_baked_material", text="Add Slot", icon='ADD')
        row.operator("mbakery.preview_baked", text="Preview Baked",
                     icon='HIDE_OFF').baked = True
        row.operator("mbakery.preview_baked", text="Show Original",
                     icon='LOOP_BACK').baked = False
        # 卡29 1.4：这两句原来收在 ⓘ Details 里，现在紧跟它们说明的按钮常驻。
        _hint(box, "Add Slot appends the baked material; Preview assigns the faces "
                   "to it (Add Slot alone does not change the look)")
        # ⚠ 实施轮 1.2（回应 1.1 报告 §5-3）：预览**只是临时**地把渲染 UV 层指过去。
        #   交付材质故意不接 `UVMap` 节点（钉层名会让"材质硬引用一个按物体存在的层"
        #   那个老坑回来 —— 甲方裁定维持），所以"新 UV 永久生效"只有成品化这一条路。
        #   只在 Re-bake 里说这句话：Bake 流程两个层名都是空串，切层根本不会发生。
        if properties.is_rebake(settings):
            _hint(box, "Preview is temporary — it only points the render UV layer "
                       "at the baked layer. To make the new UV permanent, use Keep "
                       "Only Baked or Create Final Object")
        box.separator()
        # ③ 收尾和"移除"是**相反**的两件事，标签必须说清，不然很容易以为是同一个：
        #   收尾   = 删掉**旧**槽 + **旧 UV 层**，只留烘焙材质与写入层 → 导出干净
        #   移除   = 删掉**烘焙**槽，把原材质槽与渲染层还回来（撤销我这步操作）
        # ⚠ 实施轮 1.1 起收尾**不可逆**（UV 层是真的删掉了）—— 文案必须是明确的
        #   破坏性警告，不能再写"reversible"（用户 2026-09-26 的反馈 3）。
        box.operator("mbakery.finalize_for_export",
                     text="Keep Only Baked (Export Ready)", icon='EXPORT')
        box.label(text=_wrap("Finalize is DESTRUCTIVE: original slots AND extra UV layers "
                             "are deleted, the baked layer is renamed \"UVMap\". Use Create "
                             "Final Object to keep the original intact."), icon='ERROR')
        # ④ 移除 + 它的说明
        box.operator("mbakery.restore_original_material",
                     text="Remove Baked Slot (Undo)", icon='X')
        box.label(text=_wrap("Remove deletes the BAKED slot and brings the original slots "
                             "and render layer back — it does not bring deleted UV layers "
                             "back"), icon='INFO')

        # ⑤ 最后才是汇总行 + 三角形（列表永远不挡在操作前面）
        expanded = bool(getattr(settings, "materials_expanded", False))
        row = box.row(align=True)
        row.operator("mbakery.toggle_materials", text="",
                     icon='TRIA_DOWN' if expanded else 'TRIA_RIGHT', emboss=False)
        row.label(text="{} material(s) in {} object(s)".format(
            len(valid), len(active.objects_with_baked_slot())), icon='MATERIAL')
        if not expanded:
            return

        # ⑥ 展开时才逐条画；这一段继续单独兜异常（撤销会把 bpy_struct 带走）
        try:
            for group_name, (material, link_report) in valid.items():
                box.label(text="{} -> {}".format(group_name, material.name),
                          icon='CHECKMARK')
                # link_report 可能是 None：**认出来的**（文件里本来就有的）材质没有
                # 本次运行的接线报告 —— 直接 .skipped 会 AttributeError。
                if link_report is None:
                    box.label(text="  (from a previous run — no link report)", icon='BLANK1')
                    continue
                _notes(box, ["{} not wired: {}".format(type_key, reason)
                             for type_key, reason in link_report.skipped[:3]])
        except Exception as exc:
            from .. import compat
            detail = "{}: {}".format(type(exc).__name__, exc)
            compat.log("材质列表画失败: {}".format(detail))
            try:
                session.Session.get().add_log(
                    "ERROR", "the Materials list could not be drawn — {} "
                             "(the buttons above still work)".format(detail))
            except Exception:
                pass
            box.label(text=_wrap("material list unavailable: {}".format(detail)),
                      icon='ERROR')

    def _draw_objects(self, box, active, settings):
        box.operator("mbakery.create_final_objects", icon='DUPLICATE')
        created = active.valid_created_objects()
        if created:
            box.label(text="{} object(s): {}".format(
                len(created), ", ".join(o.name for o in created[:3])), icon='CHECKMARK')
        # 卡29 1.4：这句解释原来收在 ⓘ Details 里，现在常驻。
        _hint(box, "Copies are made export-ready (one UV layer named 'UVMap', the baked "
                   "one) — the originals keep all their UV layers")

    def _draw_import_banner(self, layout, active, settings):
        """告诉用户"这些贴图是从磁盘扫来的"，并且 UV 对不对得上"""
        box = _box(layout, "Imported From Disk", 'IMPORT')
        total = sum(len(images) for images in active.imported_by_group.values())
        box.label(text="{} texture(s) in {} collection(s)".format(
            total, len(active.imported_by_group)), icon='CHECKMARK')
        if active.import_source:
            box.label(text=active.import_source)
        # 卡26：成功分支（"all good"）直接删；unknown / 旧打包的说明进 details_imported。
        hints = []
        state, detail = active.uv_state()
        if state == "matched":
            pass
        elif state == "unknown":
            hints.append("UV check: {}".format(detail))
        else:
            # 旧版（打包布局）烘出来的贴图。插件现在已经不打包 UV 了，
            # 所以没法让它们对上 —— 只能重烘一次。
            box.label(text="These textures came from an OLDER packed bake", icon='ERROR')
            if detail:
                hints.append(detail)
            hints.append("Bake again to get textures that use your own UVs")
        if active.import_unmatched:
            box.label(text="{} file(s) not recognised (renamed?)".format(
                len(active.import_unmatched)), icon='INFO')
            for path, reason in active.import_unmatched[:3]:
                box.label(text=_wrap("{}: {}".format(path, reason)), icon='BLANK1')
        row = box.row(align=True)
        row.operator("mbakery.scan_output", text="Rescan", icon='FILE_REFRESH')
        row.operator("mbakery.choose_texture_folder", text="", icon='FILE_FOLDER')
        row.operator("mbakery.clear_imported", text="", icon='TRASH')
        # 卡29 1.4：原来收在 ⓘ Details 里的说明，现在常驻。
        _notes(box, hints)

    def _draw_rescue(self, box, layout, context, settings, active):
        """没有报告时画的"救援"入口"""
        box.label(text="Nothing baked in this session", icon='INFO')
        row = box.row(align=True)
        row.operator("mbakery.scan_output", text="Scan Output Folder", icon='IMPORT')
        row.operator("mbakery.choose_texture_folder", text="", icon='FILE_FOLDER')
        if settings.output_dir:
            box.label(text=bpy.path.abspath(settings.output_dir))
        else:
            box.label(text="No output folder set — use the folder button", icon='INFO')
        box.operator("mbakery.open_log_file", text="Open Last Run Log", icon='CONSOLE')
        # 卡29 1.4：这句原来收在 ⓘ Details 里，现在常驻；"Or bake something first."
        # 与上一句重复，早已删掉。
        _hint(box, "If textures are already on disk, scan for them:")

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

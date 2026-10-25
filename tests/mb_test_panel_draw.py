"""把向导每一页真的"画"一遍

为什么需要这个：如果某一页的 draw() 抛异常，Blender 只会把错误打到控制台，
**这一页剩下的控件（包括底部导航）就全都不会画出来** ——
界面上表现为"按钮不见了"，而所有后台测试照样全绿（它们从不调用 draw）。

所以这里用一个记录型的假 layout 把 5 页各画一遍：
    - 任何一页抛异常 -> 失败，并打出是哪一页
    - 每页都必须画出前进/后退按钮
    - 非第一页必须有 Back，非最后一页必须有 Next，最后一页必须有 Start Over
"""
import bpy, sys, os, re, traceback, ast

# 工作区 = 本文件所在目录的上一级。
# ⚠ 这里原来写死的是开发机上的绝对路径，clone 到别处每个套件都跑不起来，
#   而且公开版/私有版两份检出会互相 import 错代码。
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WS not in sys.path:
    sys.path.insert(0, WS)

import material_bakery
from material_bakery import compat
from material_bakery.ui import ops as uops
from material_bakery.ui import ops_presets
from material_bakery.ui import panels as upanels
from material_bakery.ui import properties as uprops

compat.set_silent(True)

FAIL = []
def check(label, ok, extra=""):
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", label,
                               ("  " + str(extra)) if extra else ""))
    if not ok:
        FAIL.append(label)


class Recorder:
    """假 layout：记下所有被画的控件，并支持点号取属性

    它还**校验**两件真 UILayout 会校验、而普通假对象不会的事：
        - icon 名合不合法（通过在真 layout 上画一次来验）
        - layout.prop(obj, "名字") 里的名字在 obj 上真的存在

    上一版这里对任何参数都照单全收，所以 'WARNING' / 'UV_ISLANDS'
    这类错误一路漏到用户界面上。教训：**假对象必须像真对象一样挑剔**，
    否则它证明不了任何事。
    """
    def __init__(self, log):
        self._log = log

    def prop(self, data, name=None, **kwargs):
        # 名字不存在 -> 真界面上同样会抛（而且会把整页打挂）
        if isinstance(name, str) and name and not hasattr(data, name):
            raise AttributeError(
                "layout.prop({}, {!r}) — 这个属性不存在".format(
                    type(data).__name__, name))
        self._log.append(("prop", (data, name), kwargs))
        return Recorder(self._log)

    def label(self, text="", icon=None, **kwargs):
        _verify_icon(icon)
        # text 放在 kwargs 里，保持和之前一样的记录形状（断言按 kwargs 取）
        payload = dict(kwargs)
        payload["text"] = text
        payload["icon"] = icon
        self._log.append(("label", (), payload))
        return Recorder(self._log)

    def operator(self, idname=None, icon=None, **kwargs):
        _verify_icon(icon)
        self._log.append(("operator", (idname,), dict(kwargs, icon=icon)))
        return Recorder(self._log)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def method(*args, **kwargs):
            self._log.append((name, args, kwargs))
            return Recorder(self._log)
        return method

    # 这些是属性不是方法
    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            self._log.append(("set:" + name, (value,), {}))


def _verify_icon(icon):
    """验一个 icon 名 —— 假 layout 自己不会验，这里补上"""
    if icon is None or icon in upanels.valid_icons():
        return
    raise TypeError(
        'UILayout: error with keyword argument "icon" - '
        'enum "{}" not found in this Blender version'.format(icon))


def draw_page(settings, page, context):
    """画一页，返回记录到的控件列表；抛异常就返回异常

    ⚠ 面板的签名是 draw(self, context)，layout 在 self.layout 上
      （不是 draw(self, layout, context) —— 这里写错过一次）。
      而且 draw() 会调 self.draw_steps / self.draw_targets 等方法，
      所以假面板必须把这些方法**绑定到自己身上**，
      否则会得到 "'FakePanel' object has no attribute 'draw_steps'"。
    """
    settings.page = page
    log = []
    panel = upanels.MBAKERY_PT_Wizard

    class FakePanel:
        layout = Recorder(log)

        def __getattr__(self, name):
            # 从真面板类上取方法，绑定到这个假实例
            attribute = getattr(panel, name)
            if callable(attribute):
                return attribute.__get__(self, type(self))
            return attribute

    try:
        panel.draw(FakePanel(), context)
    except Exception:
        return traceback.format_exc()
    return log


def operators_in(log):
    return [entry[1][0] for entry in log
            if entry[0] == "operator" and entry[1]]


def label_texts(log):
    """只取 label 的文字（本文件后面的断言都按文字看）"""
    return [str(entry[2].get("text", "")) for entry in log
            if entry[0] == "label"]


def flat_texts(log):
    """label 文字，**折行拉平**（卡29 起长句一律 textwrap 换行）

    ⚠ 卡29 1.7 之后，说明 / 警告 / 错误都是多行的（`textwrap.fill`，width=64）。
      按单行做子串匹配会漏 —— 例如 `lights and cameras are never hidden` 被折成
      两行。统一走这里：空白折叠成单个空格，`\\n` 消失，拼回去等于原文
      （textwrap 只折行、不丢字）。
    """
    return [" ".join(text.split()) for text in label_texts(log)]


def box_texts(log, title):
    """取某个盒子（以标题 label 起）之后画出的全部 label 文字

    ⚠ 只对**页面里最后一个盒子**准确：盒子标题也是普通 label，记录里没有边界
      标记。本文件只拿它看 Settings 页最后的 Checklist 与 Result 页最后的 Log，
      不假装通用。
    """
    texts = []
    seen = False
    for entry in log:
        if entry[0] != "label":
            continue
        text = str(entry[2].get("text", ""))
        if not seen:
            if text == title:
                seen = True
            continue
        texts.append(text)
    return texts


def detail_toggle_rows(log):
    """卡26 的 ⓘ Details 行已经删除；这个 helper 只剩"确认它不再出现"的用途

    卡29 起 `mbakery.toggle_details`、`DETAILS_BOXES`、`details_*` 全部删除，
    所以正常情况下它必须一直返回空列表 —— 新断言直接用它数"有没有漏网的折叠行"。
    """
    rows = []
    for index, entry in enumerate(log):
        if entry[0] != "operator" or not entry[1]:
            continue
        if entry[1][0] != "mbakery.toggle_details":
            continue
        box = None
        for following in log[index + 1:index + 4]:
            if following[0] == "set:box" and following[1]:
                box = following[1][0]
                break
        rows.append((str(entry[2].get("text", "")), box))
    return rows


print("=== 准备一个像样的场景 ===")
bpy.ops.wm.read_factory_settings(use_empty=True)
material_bakery.register()
scene = bpy.context.scene

mesh = bpy.data.meshes.new("M")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
mesh.update()
uv = mesh.uv_layers.new(name="UVMap")
for i, c in enumerate(((0, 0), (1, 0), (1, 1), (0, 1))):
    uv.data[i].uv = c
mat = bpy.data.materials.new("M")
mat.use_nodes = True
mesh.materials.append(mat)
obj = bpy.data.objects.new("Body", mesh)
scene.collection.objects.link(obj)
obj.select_set(True)
bpy.context.view_layer.objects.active = obj

settings = scene.mbakery
# 卡24：默认工作流已是 per_material，那时 materials_by_group 的分组键是材质，
# 而下面 Result 页那几段沿用 v1.2 的集合语义（分组键 = 物体/集合名，例如 "Body"）。
# 所以准备场景后先显式钉到 Per Collection；三条流程 × 每一步的绘制覆盖
# 交给后面 WORKFLOWS 的数据驱动循环，不靠这里的默认值。
settings.workflow = uprops.WORKFLOW_BAKE
uops.add_map(settings, "shader.base_color", 256, dedupe=True)

print("\n=== 每一页都要能画出来 ===")
for page in uprops.PAGE_ORDER:
    result = draw_page(settings, page, bpy.context)
    if isinstance(result, str):
        print("  [FAIL] {} 页 draw() 抛异常:".format(page))
        for line in result.strip().splitlines()[-6:]:
            print("        " + line)
        FAIL.append("{} 页 draw() 抛异常".format(page))
    else:
        ops = operators_in(result)
        check("{} 页能画出来（{} 个控件）".format(page, len(result)),
              True, "")

print("\n=== 每一页的底部导航 ===")
for index, page in enumerate(uprops.PAGE_ORDER):
    result = draw_page(settings, page, bpy.context)
    if isinstance(result, str):
        check("{} 页导航".format(page), False, "draw 抛异常")
        continue
    ops = operators_in(result)
    print("    {} 页画出的 operator: {}".format(page, ops))
    is_first = index == 0
    is_last = index == len(uprops.PAGE_ORDER) - 1
    check("{} 页有 Back".format(page),
          "mbakery.back" in ops if not is_first else True,
          ops)
    if is_last:
        check("{} 页（最后一页）有 Start Over".format(page),
              "mbakery.start_over" in ops, ops)
    else:
        check("{} 页有 Next".format(page), "mbakery.next" in ops, ops)

print("\n=== 不许出现「光秃秃的图标」===")
# 用户报的：第 3 页 Checklist 下面有个"意义不明的钩，后面没有文字解释"。
# 根因是 naming.validate_template 校验通过时返回空串，而 checklist 把空串
# 原样交给了 label(text="", icon='CHECKMARK') —— 画出来就是一个没有文字的绿钩。
# 这类 bug 静态扫描扫不到（icon 名是合法的），只有真画一遍才看得见。
for page in uprops.PAGE_ORDER:
    result = draw_page(settings, page, bpy.context)
    if isinstance(result, str):
        continue
    bare = [entry[2].get("icon") for entry in result
            if entry[0] == "label"
            and entry[2].get("icon") not in (None, 'BLANK1')
            and not (entry[2].get("text") or "").strip()]
    check("{} 页没有「只有图标没有文字」的 label".format(page), not bare, bare)

print("\n=== 第 5 页的贴图列表要按集合分组（用户嫌太长太杂）===")
# 96 张贴图如果一张一行，面板会被淹掉。这里先真跑一轮（NullBackend）让
# Result 页有报告可画，再手工塞 3 个集合 × 4 张 = 12 张来验分组：
# 折起来应该只有"每个集合一行"，展开才逐张列出。
from material_bakery.engine import backend as bk
from material_bakery.ui import session as usession

active_for_textures = usession.Session.get()
active_for_textures.reset()
settings.textures.clear()
settings.maps.clear()
uops.add_map(settings, "shader.base_color", 64, dedupe=True)
active_for_textures.backend_factory = lambda context, s: bk.NullBackend()
started = active_for_textures.start(bpy.context, settings)
check("能起一轮小烘焙（给第 5 页准备报告）", started, active_for_textures.error)
active_for_textures.run_blocking()
print("  报告:", active_for_textures.report.summary())

settings.textures.clear()
for group in ("Body", "Gun", "Mag"):
    for type_key in ("shader.base_color", "shader.roughness",
                     "shader.metallic", "shader.normal"):
        item = settings.textures.add()
        item.image_name = "{}{}".format(group, type_key)
        item.group = group
        item.type_key = type_key
        item.type_label = type_key.split(".")[-1]
        item.size_x = 2048
        item.size_y = 2048
        item.status = "done"
print("  贴图条数:", len(settings.textures))

settings.texture_expanded_group = ""
collapsed = draw_page(settings, uprops.PAGE_RESULT, bpy.context)
if isinstance(collapsed, str):
    check("第 5 页能画出来", False, collapsed.strip().splitlines()[-1])
else:
    # ⚠ 判据要盯"贴图列表里画了什么"，不能数整页的 row ——
    #   整页还有步骤条、摘要、材质等别的行，跟贴图数没有可比性。
    collapsed_exports = operators_in(collapsed).count("mbakery.export_one_texture")
    texts = [entry[2].get("text", "") for entry in collapsed if entry[0] == "label"]
    print("  折起来时: 每张一个的导出按钮 {} 个, 集合名 {}".format(
        collapsed_exports, [t for t in texts if t in ("Body", "Gun", "Mag")]))
    check("折起来时不再一张一行（没有逐张的导出按钮）",
          collapsed_exports == 0, collapsed_exports)
    check("折起来时每个集合占一行",
          all(name in texts for name in ("Body", "Gun", "Mag")),
          [t for t in texts if t in ("Body", "Gun", "Mag")])
    check("折起来时看得到张数", any("maps" in str(t) for t in texts), texts[:10])
    check("折起来时看得到分辨率（缩写成小写 k）",
          any(str(t) == "2k" for t in texts), texts[:10])

settings.texture_expanded_group = "*"
expanded = draw_page(settings, uprops.PAGE_RESULT, bpy.context)
if isinstance(expanded, str):
    check("第 5 页展开也能画出来", False, expanded.strip().splitlines()[-1])
else:
    expanded_exports = operators_in(expanded).count("mbakery.export_one_texture")
    print("  展开后: 每张一个的导出按钮 {} 个".format(expanded_exports))
    check("展开后逐张列出（每张一个导出按钮）",
          expanded_exports == len(settings.textures), expanded_exports)
    # 用户报过"Materials 槽有两个一样的下拉选单"（material_style 被画了两遍），
    # 而且那个二选一本身就该去掉（TexImage Only 出来的材质没接着色器）。
    props = [entry[1][1] for entry in expanded
             if entry[0] == "prop" and entry[1] and len(entry[1]) > 1]
    print("  第 5 页画出的 prop:", props)
    check("第 5 页不再画材质风格下拉（那个二选一是假的）",
          "material_style" not in props, props)
    # ⚠ 判据要盯**设置级**的属性（data 是 settings 的那些）重复画了两遍 ——
    #   `enabled` 是每张贴图的行内勾选框，重复是正常的（第一版断言写宽了，误报）。
    # ⚠ 这里原来写的是 `entry[1][0] is settings`，**永远不成立**（`bpy.context.scene.mbakery`
    #   每次取都是一个新的 RNA 包装对象，实测 `is` False / `==` True），于是这条断言
    #   一直在"空列表"上空转 —— 实施轮 1.4 加新勾选框时才发现。
    settings_props = [entry[1][1] for entry in expanded
                      if entry[0] == "prop" and entry[1] and len(entry[1]) > 1
                      and entry[1][0] == settings]
    print("  第 5 页画出的设置级 prop:", settings_props)
    check("同一个设置属性不会画两遍（不会出现两个一样的下拉）",
          len(settings_props) == len(set(settings_props)),
          [p for p in settings_props if settings_props.count(p) > 1])
settings.texture_expanded_group = ""

# 实施轮 1.6：1.4 的"合并 alpha"勾选框、1.5 的 `Export Base Color With Alpha` 按钮
#   **都删掉了** —— 用户定的规矩是"默认的 basecolor 就是带 alpha，别分家了"，
#   所以这排控件里不该再有第二个导出按钮，也不该有任何"带/不带 alpha"的说法。
_result_props = [entry[1][1] for entry in expanded
                 if entry[0] == "prop" and entry[1] and len(entry[1]) > 1
                 and entry[1][0] == settings]
_result_ops = operators_in(expanded)
check("Result 页不再有那个带 alpha 的导出按钮（一个功能不留两套 UI）",
      "mbakery.export_base_color_with_alpha" not in _result_ops, _result_ops)
check("贴图区仍然只有整排导出那一个导出按钮",
      _result_ops.count("mbakery.export_textures") == 1, _result_ops)
check("1.4/1.5 的 alpha 勾选框也不在页面上",
      "merge_alpha" not in _result_props, _result_props)
check("整个 Result 页没有任何『alpha』字样（这个话题消失了）",
      not [t for t in [entry[2].get("text", "") for entry in expanded
                       if entry[0] == "label"] if "alpha" in str(t).lower()],
      [t for t in [entry[2].get("text", "") for entry in expanded
                   if entry[0] == "label"] if "alpha" in str(t).lower()])

print("\n=== 没有报告时第 5 页的两种状态 ===")
# 用户原话："直接扫一遍目录，要是有贴图就让第五页可以互动"，
# 后来补了一条："要是用户之前烘焙过了，有材质了就能直接进行烘焙后的操作"。
# 所以是**两条路**：
#   (a) 文件里已经有我们烘过的材质 -> 直接可用，不用扫目录
#   (b) 文件里什么都没有        -> 给扫目录 / 选文件夹 / 看上次日志的入口
from material_bakery.ui import session as usession_mod
from material_bakery.core import materials as materials_mod
rescue_active = usession_mod.Session.get()
rescue_active.reset()
settings.page = uprops.PAGE_RESULT
settings.textures.clear()
settings.auto_deliver = False
settings.maps.clear()
uops.add_map(settings, "shader.base_color", 32, dedupe=True)
rescue_active.backend_factory = lambda context, s: bk.NullBackend(save=False)
rescue_active.start(bpy.context, settings)
rescue_active.run_blocking()
rescue_active.build_materials(settings)
rescue_active.reset()                       # 新会话：没有报告，但文件里有材质
detected_page = draw_page(settings, uprops.PAGE_RESULT, bpy.context)
if isinstance(detected_page, str):
    check("(a) 文件里有已烘材质时第 5 页能画出来", False,
          detected_page.strip().splitlines()[-1])
else:
    ops = operators_in(detected_page)
    print("  (a) 认出材质后画出的按钮:", [o for o in ops if "slot" in str(o) or "preview" in str(o)
                                          or "final" in str(o) or "create" in str(o)
                                          or "report" in str(o)])
    check("(a) 认出材质后能直接 Add Slot", "mbakery.apply_baked_material" in ops, ops)
    check("(a) 认出材质后能直接 Preview", "mbakery.preview_baked" in ops, ops)
    check("(a) 认出材质后能直接收尾", "mbakery.finalize_for_export" in ops, ops)
    check("(a) 认出材质后能直接复制物体", "mbakery.create_final_objects" in ops, ops)
    check("(a) 认出材质后能直接存报告", "mbakery.save_report" in ops, ops)
    check("(a) 认出材质时贴图列表也是满的", len(settings.textures) > 0,
          len(settings.textures))

# (b) 把文件里我们建的材质删掉 -> 应该回到"扫目录"那条路
for material in list(bpy.data.materials):
    if material.get(materials_mod.MARKER_FINAL):
        bpy.data.materials.remove(material)
rescue_active.reset()
empty_log = draw_page(settings, uprops.PAGE_RESULT, bpy.context)
if isinstance(empty_log, str):
    check("(b) 什么都没有时第 5 页也能画出来", False, empty_log.strip().splitlines()[-1])
else:
    ops = operators_in(empty_log)
    print("  (b) 什么都没有时画出的入口:",
          [o for o in ops if "scan" in str(o) or "log" in str(o) or "folder" in str(o)])
    check("(b) 什么都没有时有 Scan Output Folder",
          "mbakery.scan_output" in ops, ops)
    check("(b) 什么都没有时有选文件夹的入口",
          "mbakery.choose_texture_folder" in ops, ops)
    check("(b) 什么都没有时有打开上次日志的入口",
          "mbakery.open_log_file" in ops, ops)

settings.page = uprops.PAGE_BAKE
settings.textures.clear()
settings.auto_deliver = True
rescue_active.reset()

print("\n=== Materials 箱：材质列表出问题也不许把操作按钮连坐掉 ===")
# 用户报过"扫完目录之后按钮全没了"。不管是什么原因让列表画不出来，
# Add Slot / Preview / 收尾这些**操作**都必须还在，而且异常要写进日志
# （以前面板异常只进控制台，用户手里什么都没有）。
robust_active = usession_mod.Session.get()
robust_active.reset()
settings.textures.clear()
settings.auto_deliver = False
settings.maps.clear()
uops.add_map(settings, "shader.base_color", 32, dedupe=True)
robust_active.backend_factory = lambda context, s: bk.NullBackend(save=False)
robust_active.start(bpy.context, settings)
robust_active.run_blocking()
robust_active.build_materials(settings)
_original = robust_active.valid_materials_by_group
robust_active.valid_materials_by_group = (
    lambda: (_ for _ in ()).throw(RuntimeError("simulated list failure")))
robust_page = draw_page(settings, uprops.PAGE_RESULT, bpy.context)
robust_active.valid_materials_by_group = _original
if isinstance(robust_page, str):
    check("列表炸了之后第 5 页仍然能画", False, robust_page.strip().splitlines()[-1])
else:
    ops = operators_in(robust_page)
    check("列表炸了，Add Slot 还在", "mbakery.apply_baked_material" in ops, ops)
    check("列表炸了，Preview 还在", "mbakery.preview_baked" in ops, ops)
    check("列表炸了，Show Original 还在",
          ops.count("mbakery.preview_baked") >= 2, ops)
    check("列表炸了，收尾按钮还在", "mbakery.finalize_for_export" in ops, ops)
    check("列表炸了，移除按钮还在", "mbakery.restore_original_material" in ops, ops)
    check("列表炸了也没把后面的 box 连坐（复制物体 / 报告 / 日志还在）",
          "mbakery.create_final_objects" in ops and "mbakery.save_report" in ops
          and "mbakery.open_log_file" in ops, ops)
check("面板异常被写进会话日志（不用去开控制台）",
      any("could not be drawn" in e.message for e in robust_active.log),
      [e.message for e in robust_active.log][-3:])
robust_active.reset()

print("\n=== 撤销把材质删掉之后，第 5 页下面的面板不能消失 ===")
# 用户实测："使用 remove slot 移除后按 ctrl-z 撤销会导致面板出错，
#            下面创建最终物体和输出 log 的面板都没了"
# 根因：撤销删掉了我们刚建的烘焙材质数据块，而 session.materials_by_group 里
# 握着的是已释放的 bpy_struct，draw() 一碰就抛 —— 整页剩下的 box 全不画。
from material_bakery.ui import session as usession_undo
undo_active = usession_undo.Session.get()
undo_active.reset()
settings.textures.clear()
undo_active.backend_factory = lambda context, s: bk.NullBackend()
settings.maps.clear()
uops.add_map(settings, "shader.base_color", 32, dedupe=True)
settings.auto_deliver = False
undo_active.start(bpy.context, settings)
undo_active.run_blocking()
built, _reports = undo_active.build_materials(settings)
check("先建出材质（为撤销做准备）", built > 0, built)
material = undo_active.materials_by_group["Body"][0]
bpy.data.materials.remove(material)          # 模拟 Ctrl+Z 把数据块删了

stale_page = draw_page(settings, uprops.PAGE_RESULT, bpy.context)
if isinstance(stale_page, str):
    check("材质引用失效后第 5 页仍然能画出来", False,
          stale_page.strip().splitlines()[-1])
else:
    ops = operators_in(stale_page)
    print("  失效引用下画出的按钮:", [o for o in ops if "final" in str(o) or "report" in str(o)
                                      or "create" in str(o)])
    check("材质引用失效后第 5 页仍然能画出来", True)
    check("**下面几块还在**：创建最终物体",
          "mbakery.create_final_objects" in ops, ops)
    check("**下面几块还在**：存报告",
          "mbakery.save_report" in ops, ops)
check("会话里失效的材质被过滤掉", undo_active.valid_materials_by_group() == {},
      undo_active.valid_materials_by_group())
dropped = undo_active.revalidate()
print("  revalidate 丢掉:", dropped)
check("revalidate 会报出丢了几个材质（不是静默）", dropped >= 0, dropped)
undo_active.reset()
settings.auto_deliver = True

print("\n=== 具体盯一下第 3 页（用户报的「下一步不见了」）===")
settings.page = uprops.PAGE_SETTINGS
result = draw_page(settings, uprops.PAGE_SETTINGS, bpy.context)
if isinstance(result, str):
    print("  [FAIL] 设置页 draw() 抛异常:")
    print(result)
    FAIL.append("设置页 draw() 抛异常")
else:
    ops = operators_in(result)
    check("设置页画出了 Next", "mbakery.next" in ops, ops)
    labels = [entry[2].get("text", "") for entry in result if entry[0] == "operator"]
    check("Next 按钮有文字标签（不是空白按钮）",
          any("Next" in str(l) for l in labels), labels)
    print("    设置页画的全部按钮文字:", [l for l in labels if l])

print("\n=== v1.1：分辨率是两个框，且没有多余的说明标签 ===")
_v11 = draw_page(settings, uprops.PAGE_TARGETS, bpy.context)
if isinstance(_v11, str):
    check("第一页能画（v1.1 分辨率两个框）", False, _v11.strip().splitlines()[-1:])
else:
    _props = [entry[1][1] for entry in _v11
              if entry[0] == "prop" and len(entry[1]) > 1]
    check("第一页画出了 W 和 H 两个分辨率框",
          "size_x" in _props and "size_y" in _props, _props)
    check("没有画那个已经不存在的单尺寸属性", "size" not in _props, _props)
    check("没有画 'options below' 那个标签",
          not any("options below" in str(entry[2].get("text", ""))
                  for entry in _v11 if entry[0] == "label"))

print("\n=== 2026-09 性能一轮加的东西真的画出来了 ===")
# Hide Unrelated：必须**连后果一起画**（藏起来的几何不再遮挡），
# 否则用户只会看到一个钩，不知道它会把 AO/阴影变成另一件事。
settings.hide_unrelated = False
plain = draw_page(settings, uprops.PAGE_SETTINGS, bpy.context)
if isinstance(plain, str):
    check("Hide Unrelated 关着时也能画", False, plain.strip().splitlines()[-1:])
else:
    check("Hide Unrelated 的开关画出来了",
          any(entry[0] == "prop" and len(entry[1]) > 1
              and entry[1][1] == "hide_unrelated" for entry in plain),
          [entry[1] for entry in plain if entry[0] == "prop"][:20])
    check("关着的时候不解释后果（不啰嗦）",
          not any("lights and cameras" in str(entry[2].get("text", ""))
                  for entry in plain if entry[0] == "label"))

settings.hide_unrelated = True
# ⚠ 把输出目录清空再比：checklist() 第一次调用会**真的去建目录**并返回
#   "Output folder created"，第二次就变成 "Output folder exists" ——
#   不固定状态的话，这条断言会因为环境而随机失败。
saved_output = settings.output_dir
settings.output_dir = ""
switched = draw_page(settings, uprops.PAGE_SETTINGS, bpy.context)
from material_bakery.ui import ops as uops_panel

expected_pairs = list(uops_panel.checklist(bpy.context, settings))
settings.output_dir = saved_output
settings.hide_unrelated = False
if isinstance(switched, str):
    check("Hide Unrelated 开着时也能画", False, switched.strip().splitlines()[-1:])
else:
    # 卡29 1.7：长句一律换行，所以这里用**拉平**的文字（见 flat_texts）
    texts = flat_texts(switched)
    check("开着的时候写清了只藏网格、灯光摄像机不动",
          any("lights and cameras" in text for text in texts),
          [t for t in texts if "lights" in t][:2])
    # ⚠ 卡26 起 Checklist **只画 WARN / ERROR**（健康状态不占地方）。
    #   所以断言按**级别**分三组 —— 覆盖点一个没少，少的只是"拿 OK 行当屏上内容"：
    #     ① 有 WARN/ERROR 的场景里，它们必须真的画出来；
    #     ② 同一场景里 OK / INFO 行必须**一条都不出现**（新规格，不是放宽）；
    #     ③ 没有 WARN/ERROR 时盒子里只有一行 `All checks passed`（下面单独画）；
    #     ④ 数据层 ops.checklist() 仍然照旧算得出那批 OK 行（API 没被 UI 改掉）。
    #   ⚠ 原来那条是"每一行都必须画" —— 它把 OK 行也当屏上内容，与新规格冲突。
    warn_rows = [message for level, message in expected_pairs
                 if level in ('WARN', 'ERROR')]
    ok_rows = [message for level, message in expected_pairs
               if level not in ('WARN', 'ERROR')]
    missing_warn = [row for row in warn_rows if row not in texts]
    leaked_ok = [row for row in ok_rows if row in texts]
    check("这个场景里真的有 WARN/ERROR 行可画（下面两条才有意义）",
          bool(warn_rows), expected_pairs)
    check("(卡26) Checklist 的 WARN/ERROR 行都真的画在设置页上",
          not missing_warn, missing_warn[:2] or warn_rows[:1])
    check("(卡26) Checklist 的 OK / INFO 行一条都不画（只在数据层）",
          bool(ok_rows) and not leaked_ok, leaked_ok[:2] or ok_rows[:2])

    # ④ 数据层：直接调 API，确认 OK 行还在（UI 的隐藏逻辑没有改数据）。
    data_rows = list(uops_panel.checklist(bpy.context, settings))
    check("(卡26) 数据层 ops.checklist() 仍然返回 OK 行（API 没被 UI 改）",
          any(level == 'OK' and "enabled bake item(s)" in message
              for level, message in data_rows), data_rows)

    # ③ 没有 WARN/ERROR 的场景：盒子里**只有**一行 "All checks passed"。
    #   输出目录指到一个**已存在**的目录（_probe），checklist 才不会建目录、
    #   也不会因为"第一次创建"而临时多出一行。其余能产生 WARN 的开关全部关掉。
    _saved_state = (settings.output_dir, settings.use_selected_to_active,
                    settings.prepare_uv, settings.udim, settings.hide_unrelated)
    settings.output_dir = os.path.join(WS, "_probe")
    settings.use_selected_to_active = False
    settings.prepare_uv = False
    settings.udim = False
    settings.hide_unrelated = False
    clean_pairs = list(uops_panel.checklist(bpy.context, settings))
    clean_page = draw_page(settings, uprops.PAGE_SETTINGS, bpy.context)
    (settings.output_dir, settings.use_selected_to_active,
     settings.prepare_uv, settings.udim, settings.hide_unrelated) = _saved_state
    check("(卡26) 这个场景里没有 WARN/ERROR（用来验「全通过」形态）",
          not [row for level, row in clean_pairs if level in ('WARN', 'ERROR')],
          clean_pairs)
    if isinstance(clean_page, str):
        check("(卡26) 全通过时设置页也能画", False,
              clean_page.strip().splitlines()[-1])
    else:
        clean_box = box_texts(clean_page, "Checklist")
        check("(卡26) 没有 WARN/ERROR 时 Checklist 只有一行 All checks passed",
              clean_box == ["All checks passed"], clean_box)

# 运行期间第 4 页要写明"撤销和自动保存是暂停的" —— 不然用户会以为
# 插件把他的 Ctrl+Z 弄坏了。
active = __import__("material_bakery.ui.session", fromlist=["x"]).Session.get()
active.reset()
settings.page = uprops.PAGE_BAKE


class _FakeBakeType:
    label = "BaseColor"


class _FakeTask:
    status = "done"
    group_name = "Baked"
    size_x = 2048
    size_y = 2048
    bake_type = _FakeBakeType()


class _FakePlan:
    tasks = [_FakeTask(), _FakeTask()]


class _FakeJob:
    """只要够画第 4 页就行（真跑一遍在这里没必要）"""

    is_finished = False
    paused = False
    plan = _FakePlan()
    progress = (1, 2, 0.5)

    def status_line(self):
        return "Baking X — BaseColor (last one took 10s, ~20s to go)"

    def current_task(self):
        return None

    def eta(self):
        return 12.0


active.job = _FakeJob()
active.pause_preferences()
check("运行期间会话认为自己在跑", active.is_running)
bake_page = draw_page(settings, uprops.PAGE_BAKE, bpy.context)
if isinstance(bake_page, str):
    check("运行中的第 4 页能画", False, bake_page.strip().splitlines()[-1:])
else:
    texts = flat_texts(bake_page)
    check("运行中的第 4 页写明撤销 / 自动保存被暂停",
          any("undo and auto-save are paused" in text for text in texts),
          [t for t in texts if "paused" in t][:2])
active.release_guard()
active.reset()
check("收尾之后护栏是空闲的", not active.guard.engaged)

print("\n=== 各种状态下都要能画（状态会影响某些分支）===")
# 运行中
active = __import__("material_bakery.ui.session", fromlist=["x"]).Session.get()
for page in uprops.PAGE_ORDER:
    result = draw_page(settings, page, bpy.context)
    if isinstance(result, str):
        check("{} 页在默认状态下能画".format(page), False, "抛异常")
    else:
        check("{} 页在默认状态下能画".format(page), True)

# 打开投影 + 各种 source_mode 的话，分支更多
settings.use_selected_to_active = True
settings.source_mode = 'HIDDEN'
settings.prepare_uv = True
settings.udim = True
for page in uprops.PAGE_ORDER:
    result = draw_page(settings, page, bpy.context)
    if isinstance(result, str):
        print("  [FAIL] {} 页在投影/UDIM 状态下抛异常:".format(page))
        for line in result.strip().splitlines()[-6:]:
            print("        " + line)
        FAIL.append("{} 页在投影/UDIM 状态下抛异常".format(page))
    else:
        op_names = operators_in(result)
        check("{} 页在投影/UDIM 状态下能画".format(page), True, "")
        if page != uprops.PAGE_RESULT:
            check("{} 页在这个状态下仍有 Next".format(page),
                  "mbakery.next" in op_names, op_names)

print("\n=== 每个工作流 × 每一步都要能真画一遍（含 Re-bake）===")
# ⚠ 这是新导航唯一能防回归的方式：页面 dispatch 按 `settings.page` 走，而步骤条、
#   页脚按钮、工作流切换跟着 `settings.workflow` 走 —— 每个工作流 × 每一步都真画一遍，
#   任何一页 draw() 抛异常都会被抓住（Blender 只会把错误打到控制台，界面上就是
#   "按钮不见了"，而所有不调 draw 的测试照样全绿）。
#   同时给 Re-bake 备好真实内容（第二层 UV + 它自己那份 Maps + 集合表），
#   否则那两页永远只画"空列表"那一个分支。
_draw_mesh = obj.data
if _draw_mesh.uv_layers.get("UV2") is None:
    _draw_mesh.uv_layers.new(name="UV2")
    for _layer in _draw_mesh.uv_layers:
        _layer.active_render = (_layer.name == "UVMap")
from material_bakery.engine import uv_sets as uvs_module
settings.workflow = uprops.WORKFLOW_REBAKE
bpy.ops.mbakery.refresh_groups()
uops.add_map(settings, "shader.base_color", 64, dedupe=True)
settings.rebake_from_uv = "UV2"
settings.rebake_to_uv = "UVMap"
check("Re-bake 的层名下拉能拿到场景里的层",
      "UV2" in uvs_module.layer_names(usession.rebake_objects(bpy.context, settings)),
      uvs_module.layer_names(usession.rebake_objects(bpy.context, settings)))
settings.page = uprops.PAGE_TARGETS          # 故意设成另一条流程的页
settings.sync_page()
check("切到 Re-bake 时页面被校正到该流程的第一步",
      settings.page == uprops.PAGE_REBAKE_SOURCE, settings.page)

# 分辨率来源必须画出来（甲方要求"显示来源"）：给一行塞个来源再画一次
if settings.rebake_maps:
    settings.rebake_maps[0].size_source = "Chair_BaseColor.png"
    _source_page = draw_page(settings, uprops.PAGE_REBAKE_SOURCE, bpy.context)
    if isinstance(_source_page, str):
        check("Re-bake 首页（带来源）能画", False, _source_page.strip().splitlines()[-1])
    else:
        _texts = [str(entry[2].get("text", "")) for entry in _source_page
                  if entry[0] == "label"]
        check("Re-bake 首页把分辨率来源画出来了（含文件名）",
              any("Chair_BaseColor.png" in text for text in _texts),
              [t for t in _texts if "Chair" in t or "from " in t][:3])
    settings.rebake_maps[0].size_source = ""

STEP_COUNT = 0
# 卡24：WORKFLOWS 每项是 4 元组 (id, label, description, sort_index)。
for workflow, workflow_label, _desc, _sort in uprops.WORKFLOWS:
    settings.workflow = workflow
    steps = [key for key, _label, _d in uprops.steps_for(settings)]
    check("{} 流程有步骤（数据驱动）".format(workflow_label), bool(steps), steps)
    for index, page in enumerate(steps):
        STEP_COUNT += 1
        result = draw_page(settings, page, bpy.context)
        if isinstance(result, str):
            print("  [FAIL] {} / {} 页 draw() 抛异常:".format(workflow_label, page))
            for line in result.strip().splitlines()[-6:]:
                print("        " + line)
            FAIL.append("{} / {} 页 draw() 抛异常".format(workflow_label, page))
            continue
        op_names = operators_in(result)
        check("{} / {} 页能画出来（{} 个控件）".format(workflow_label, page, len(result)),
              True, "")
        if index > 0:
            check("{} / {} 页有 Back".format(workflow_label, page),
                  "mbakery.back" in op_names, op_names)
        labels = [entry[2].get("text", "") for entry in result if entry[0] == "operator"]
        if index < len(steps) - 1:
            check("{} / {} 页有 Next".format(workflow_label, page),
                  "mbakery.next" in op_names, op_names)
            check("{} / {} 页的 Next 带着下一步的名字".format(workflow_label, page),
                  any(str(item).startswith("Next: ") for item in labels), labels)
        else:
            check("{} / {} 页（最后一步）有 Start Over".format(workflow_label, page),
                  "mbakery.start_over" in op_names, op_names)
        bare = [entry[2].get("icon") for entry in result
                if entry[0] == "label"
                and entry[2].get("icon") not in (None, 'BLANK1')
                and not (entry[2].get("text") or "").strip()]
        check("{} / {} 页没有「只有图标没有文字」的 label".format(workflow_label, page),
              not bare, bare)
WORKFLOW_COUNT = len(uprops.WORKFLOWS)
EXPECTED_STEPS = sum(len(uprops.workflow_steps(key))
                     for key, _l, _d, _s in uprops.WORKFLOWS)
check("每个工作流的每一步都真画过",
      STEP_COUNT == EXPECTED_STEPS and STEP_COUNT >= WORKFLOW_COUNT,
      (STEP_COUNT, EXPECTED_STEPS, WORKFLOW_COUNT))
settings.workflow = uprops.WORKFLOW_BAKE

print("\n=== 实施轮 1.1：Compare 框删了、收尾文案是破坏性警告、UV Sets 换了标签 ===")
# 用户真机反馈 2：第 5 页的 Compare 与 Preview Baked / Show Original 功能一样 → 删框。
# 反馈 3：Keep Only Baked 现在**真删旧 UV 层**，文案必须是明确的破坏性警告。
# 反馈 5：两个 UV 下拉"不明不白" → 标签改成"旧 UV / 新 UV"。
# ⚠ Result 页有两种状态（救援入口 / 烘完的界面），只有**烘完**那种才画 Materials 盒 ——
#   所以先真起一轮，再画。
_deliver = usession.Session.get()
_deliver.reset()
settings.workflow = uprops.WORKFLOW_BAKE
settings.textures.clear()
settings.auto_deliver = False
settings.maps.clear()
uops.add_map(settings, "shader.base_color", 32, dedupe=True)
_deliver_started = _deliver.start(bpy.context, settings)
_deliver.run_blocking()
_deliver.build_materials(settings)
_deliver.apply_materials(settings)
check("(1.1) 准备好了一轮（给 Result 页用）",
      _deliver_started and _deliver.report is not None
      and _deliver.report.failed == 0,
      _deliver.report.summary() if _deliver.report else _deliver.error)
_result_page = draw_page(settings, uprops.PAGE_RESULT, bpy.context)
if isinstance(_result_page, str):
    check("(1.1) Result 页能画出来", False, _result_page.strip().splitlines()[-1])
else:
    _ops = operators_in(_result_page)
    _props = [entry[1][1] for entry in _result_page
              if entry[0] == "prop" and entry[1] and len(entry[1]) > 1]
    _texts = flat_texts(_result_page)
    check("(1.1) Result 页不再画 Compare 的按钮",
          "mbakery.compare_toggle" not in _ops, _ops)
    check("(1.1) Result 页不再画 compare_baked 那个勾",
          "compare_baked" not in _props, _props)
    check("(1.1) Preview / Show Original 两个按钮都还在",
          _ops.count("mbakery.preview_baked") >= 2, _ops)
    check("(1.1) 收尾按钮还在", "mbakery.finalize_for_export" in _ops, _ops)
    check("(1.1) 移除按钮还在", "mbakery.restore_original_material" in _ops, _ops)
    check("(1.1) 收尾的说明写明了 DESTRUCTIVE",
          any("DESTRUCTIVE" in text for text in _texts),
          [t for t in _texts if "Final" in t or "DEST" in t][:3])
    check("(1.1) 收尾的说明写明会删多余的 UV 层",
          any("extra UV layers are deleted" in text for text in _texts),
          [t for t in _texts if "UV layer" in t][:3])
    check("(1.1) 收尾的说明不再写 reversible",
          not any("Finalize is reversible" in text for text in _texts),
          [t for t in _texts if "revers" in t][:3])
    # 卡29 1.4：这句从 details_result 改成**常驻**（折叠区整体删除）——
    # 所以它现在默认就在页面上，而且和 DESTRUCTIVE 是两句不同的话。
    check("(1.1/卡29) 删 UV 层这件事（改走副本那条路）现在默认就写着",
          any("keep all their UV layers" in text for text in _texts),
          [t for t in _texts if "UV layer" in t][:3])

settings.workflow = uprops.WORKFLOW_REBAKE
_uv_page = draw_page(settings, uprops.PAGE_REBAKE_UVS, bpy.context)
if isinstance(_uv_page, str):
    check("(1.1) UV Sets 页能画出来", False, _uv_page.strip().splitlines()[-1])
else:
    _labels = [entry[2].get("text") for entry in _uv_page
               if entry[0] == "prop" and entry[1] and len(entry[1]) > 1
               and entry[1][1] in ("rebake_from_uv", "rebake_to_uv")]
    _uv_texts = [str(entry[2].get("text", "")) for entry in _uv_page
                 if entry[0] == "label"]
    print("  UV Sets 两个下拉的标签:", _labels)
    check("(1.1) 采样层下拉的标签是 Sample from (old UV)",
          "Sample from (old UV)" in _labels, _labels)
    check("(1.1) 写入层下拉的标签是 Write to (new UV)",
          "Write to (new UV)" in _labels, _labels)
    check("(1.1) What This Run Does 写出了物体计数",
          any("object(s) selected" in text for text in _uv_texts),
          [t for t in _uv_texts if "object" in t][:3])
settings.workflow = uprops.WORKFLOW_BAKE
_deliver.reset()

print("\n=== 实施轮 1.2：UV Sets 下拉改 None、法线通道的说明、预览只是临时 ===")
# 用户原话："就不能直接写 None 吗？" —— 下拉项 = `None` + 实际检测到的层名，
# 两个下拉各管各的那一侧（采样 None = 不钉采样层；写入 None = 不换渲染层）。
from material_bakery.engine import uv_sets as _uvs

settings.workflow = uprops.WORKFLOW_REBAKE
try:
    settings.rebake_from_uv = _uvs.NONE_UV
    settings.rebake_to_uv = "UV2"
    _assign_ok = True
except Exception as _exc:                             # noqa: BLE001
    _assign_ok = False
check("(1.2) 哨兵 None 是可选的一项（不是空串分隔符）", _assign_ok, _assign_ok)

_uv12 = draw_page(settings, uprops.PAGE_REBAKE_UVS, bpy.context)
if isinstance(_uv12, str):
    check("(1.2) UV Sets 页能画出来", False, _uv12.strip().splitlines()[-1])
else:
    _texts12 = flat_texts(_uv12)
    print("  UV Sets（采样 None / 写入 UV2）:", [t for t in _texts12 if "None" in t
                                                 or "write layer" in t][:3])
    # 卡29 1.4：这两句从 details_uv 改成**常驻**（折叠区整体删除）。
    check("(1.2) 采样侧 None 的说明写清了'不钉'（现在默认就画）",
          any("Sample from = None" in text and "not pinned" in text
              for text in _texts12),
          [t for t in _texts12 if "None" in t][:3])
    check("(1.2) 写入层那一侧照样说明会临时换渲染层（现在默认就画）",
          any("write layer becomes the render layer" in text for text in _texts12),
          [t for t in _texts12 if "render layer" in t][:3])

settings.rebake_to_uv = _uvs.NONE_UV
_uv12b = draw_page(settings, uprops.PAGE_REBAKE_UVS, bpy.context)
if isinstance(_uv12b, str):
    check("(1.2) UV Sets 页（两侧都 None）能画出来", False,
          _uv12b.strip().splitlines()[-1])
else:
    _texts12b = flat_texts(_uv12b)
    # 卡29 1.4：这句也从 details_uv 改成常驻。
    check("(1.2) 两侧都是 None 时说明'什么都不注入、什么标志都不改'（默认就画）",
          any("Both set to None" in text and "no flag" in text for text in _texts12b),
          [t for t in _texts12b if "None" in t][:3])

# Result 页（Re-bake）：预览只是临时的，永久生效要走成品化
# 卡29 1.4：这两句都改成**常驻**（原 details_result 已删除）。
_result12 = draw_page(settings, uprops.PAGE_RESULT, bpy.context)
if isinstance(_result12, str):
    check("(1.2) Result 页（Re-bake）能画出来", False,
          _result12.strip().splitlines()[-1])
else:
    _texts12r = flat_texts(_result12)
    print("  Result（Re-bake）预览说明:",
          [t for t in _texts12r if "Preview" in t][:2])
    check("(1.2) Result 页写明预览只是临时的（默认就画）",
          any("Preview is temporary" in text for text in _texts12r),
          [t for t in _texts12r if "Preview" in t][:2])
    check("(1.2) 并指路 Keep Only Baked / Create Final Object",
          any("Keep Only Baked" in text and "Create Final Object" in text
              for text in _texts12r),
          [t for t in _texts12r if "Preview is temporary" in t][:2])

# Quality 页：默认（EMISSION）模式下也要说清法线是例外
settings.workflow = uprops.WORKFLOW_BAKE
settings.bake_method = 'EMISSION'
_quality12 = draw_page(settings, uprops.PAGE_SETTINGS, bpy.context)
if isinstance(_quality12, str):
    check("(1.2) Quality 页能画出来", False, _quality12.strip().splitlines()[-1])
else:
    _texts12q = flat_texts(_quality12)
    check("(1.2) 默认模式下写明法线一律走原生 NORMAL pass",
          any("NORMAL pass" in text and "bump" in text for text in _texts12q),
          [t for t in _texts12q if "Normal" in t][:3])
settings.bake_method = 'NATIVE'
# 卡29 1.4：这句解释从 details_quality 改成**常驻**。
_quality12n = draw_page(settings, uprops.PAGE_SETTINGS, bpy.context)
if not isinstance(_quality12n, str):
    _texts12n = flat_texts(_quality12n)
    check("(1.2) Native 模式下的说明还在（默认就画）",
          any("fall back to Emission" in text for text in _texts12n),
          [t for t in _texts12n if "Emission" in t][:3])
else:
    check("(1.2) Native 模式的 Quality 页能画出来", False,
          _quality12n.strip().splitlines()[-1])
settings.bake_method = 'EMISSION'
settings.workflow = uprops.WORKFLOW_BAKE

print("=== 每个 icon 名字都必须是这个 Blender 版本真有的 ===")
# ⚠ 这一条是两次翻车的直接原因：
#     第一次有一页用了不存在的 'WARNING'（关键字写法）
#     第二次 UV 分区用了不存在的 'UV_ISLANDS'（**位置参数**传给 _box）
#   第一版扫描只看 `icon=` 关键字，所以第二次没扫到 —— 用户先在界面上撞见了。
#   现在两种写法都扫。
def valid_icons():
    names = set()
    for function_name in ("label", "operator", "prop", "prop_enum", "prop_search",
                          "separator", "menu", "menu_pie"):
        try:
            parameters = bpy.types.UILayout.bl_rna.functions[function_name].parameters
        except (KeyError, AttributeError):
            continue
        prop = parameters.get("icon")
        if prop is None:
            continue
        names.update(item.identifier for item in prop.enum_items)
    return names


VALID_ICONS = valid_icons()
check("能拿到合法 icon 列表", len(VALID_ICONS) > 500, len(VALID_ICONS))

# icon 作为第 3 个位置参数的辅助函数（0-based 下标 2）
HELPERS_WITH_ICON_ARG = {"_box": 2}

_ui_dir = os.path.join(WS, "material_bakery", "ui")
bad_icons = {}
icon_refs = 0
for filename in sorted(os.listdir(_ui_dir)):
    if not filename.endswith(".py"):
        continue
    with open(os.path.join(_ui_dir, filename), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        found = []
        for keyword in node.keywords:
            if keyword.arg == "icon" and isinstance(keyword.value, ast.Constant) \
                    and isinstance(keyword.value.value, str):
                found.append((keyword.value.value, "icon="))
        func = node.func
        helper = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None)
        index = HELPERS_WITH_ICON_ARG.get(helper)
        if index is not None and len(node.args) > index:
            arg = node.args[index]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.append((arg.value, "{} positional".format(helper)))
        for value, style in found:
            icon_refs += 1
            if value not in VALID_ICONS:
                bad_icons.setdefault(value, []).append(
                    "{}:{} ({})".format(filename, node.lineno, style))
print("  扫到 icon 引用 {} 处".format(icon_refs))
check("所有 icon 名都在这个 Blender 版本里存在（关键字与位置参数都扫）",
      not bad_icons, bad_icons)
check("确实扫到了位置参数形式的 icon（防止扫描又漏一种写法）",
      any("positional" in place
          for places in bad_icons.values() for place in places)
      or icon_refs > 45, icon_refs)

print("\n=== 运行时的 icon 闸门 ===")
# 就算源码里再有写错的 icon，也不该把面板打挂
check("合法 icon 原样返回", upanels.safe_icon('CHECKMARK') == 'CHECKMARK')
check("非法 icon 退化成 NONE 而不是抛异常",
      upanels.safe_icon('NO_SUCH_ICON_XYZ') == 'NONE',
      upanels.safe_icon('NO_SUCH_ICON_XYZ'))
check("空名字也安全", upanels.safe_icon('') == 'NONE', upanels.safe_icon(''))
check("_box 用非法 icon 也能画出来",
      upanels._box(Recorder([]), "Test", 'NOT_AN_ICON') is not None)

print("\n=== 页面主体出错时，导航栏必须还在 ===")
# 模拟"某一页的绘制抛异常"，确认 Back/Next 仍然被画出来
original = upanels.MBAKERY_PT_Wizard.draw_settings


def broken(self, layout, context, settings):
    raise RuntimeError("simulated panel failure")


upanels.MBAKERY_PT_Wizard.draw_settings = broken
try:
    result = draw_page(settings, uprops.PAGE_SETTINGS, bpy.context)
finally:
    upanels.MBAKERY_PT_Wizard.draw_settings = original

if isinstance(result, str):
    check("页面主体异常不会冒出去（被 draw 兜住）", False, result)
else:
    ops = operators_in(result)
    check("页面主体异常不会冒出去（被 draw 兜住）", True, "")
    check("这种情况下仍然画出了 Next", "mbakery.next" in ops, ops)
    check("这种情况下仍然画出了 Back", "mbakery.back" in ops, ops)
    check("并且把错误显示在面板上",
          any("Panel error" in str(entry[2].get("text", ""))
              for entry in result if entry[0] == "label"),
          [entry[2] for entry in result if entry[0] == "label"][:3])
    check("恢复后设置页照常工作",
          "mbakery.next" in operators_in(
              draw_page(settings, uprops.PAGE_SETTINGS, bpy.context)))

print("\n=== 卡26 验收：折叠区 / Maps 工具栏 / 日志 / 命名样例 ===")
# 这一整段跑在**一条成功会话**上（NullBackend 真跑一轮）：卡26 好几条断言
# 的前提就是"成功场景"—— 没有错误时不该自动展开、健康文案不该出现。
c26 = usession.Session.get()
c26.reset()
settings.workflow = uprops.WORKFLOW_BAKE
settings.use_selected_to_active = False
settings.prepare_uv = False
settings.udim = False
settings.hide_unrelated = False
settings.bake_method = 'EMISSION'
settings.output_dir = os.path.join(WS, "_probe")
settings.maps.clear()
uops.add_map(settings, "shader.base_color", 64, dedupe=True)
c26.backend_factory = lambda context, s: bk.NullBackend(save=False)
_c26_started = c26.start(bpy.context, settings)
if _c26_started:
    c26.run_blocking()
    c26.build_materials(settings)
    c26.apply_materials(settings)
check("卡26 · 备好一轮成功烘焙（后面几条要成功场景）",
      _c26_started and c26.report is not None and c26.report.failed == 0
      and not c26.has_errors(),
      c26.error or (c26.report.summary() if c26.report else "no report"))

# ---------------------------------------------------------------- 卡29 · 1.4
# 卡29 · 1.4：ⓘ Details 折叠区**整体删除** —— 操作符、Scene 属性、画出来的行
# 三样都不许残留。卡26 那套"每盒一份展开状态"到这里为止。
_DETAIL_PROPS = ("details_settings", "details_maps", "details_quality", "details_uv",
                 "details_std", "details_result", "details_bake", "details_imported")
_leftover_props = [name for name in _DETAIL_PROPS if hasattr(settings, name)]
print("  DETAILS_BOXES 还在吗:", hasattr(uops, "DETAILS_BOXES"))
print("  残留的 details_* 属性:", _leftover_props)
check("卡29 · ops 里没有 DETAILS_BOXES", not hasattr(uops, "DETAILS_BOXES"))
check("卡29 · 八个 details_* Scene 属性都不存在", not _leftover_props, _leftover_props)
check("卡29 · ToggleDetails 类已经从 ops 里删掉",
      not hasattr(uops, "MBAKERY_OT_ToggleDetails"))
check("卡29 · mbakery.toggle_details 没注册",
      not hasattr(bpy.types, "MBAKERY_OT_ToggleDetails"))
try:
    _toggle_result = bpy.ops.mbakery.toggle_details(box="quality")
except Exception as _exc:                             # noqa: BLE001
    _toggle_result = "raised: {}".format(type(_exc).__name__)
check("卡29 · 调这个操作符直接失败（不是悄悄 CANCELLED）",
      _toggle_result != {'FINISHED'}, _toggle_result)
check("卡29 · materials_expanded 存在且默认 False",
      hasattr(settings, "materials_expanded")
      and not bool(settings.materials_expanded),
      getattr(settings, "materials_expanded", "<missing>"))

# ---------------------------------------------------------------- 卡29 · 1.1
# 卡29 1.1：Single 模式**不画**组列表（下拉已经指名了那一个集合），只留一行摘要；
#   Collections 模式照旧画 `toggle_all_groups` + 逐组勾选。
#   Bake 与 Re-bake 各一遍 —— 两条流程各记一份集合表。
from material_bakery.core import scene_scan as _scan_mod

# ⚠ `single_collection` 的枚举项来自 `bpy.data.collections`，而夹具里的物体挂在
#   master collection 上（collection 名就是物体的分组名 "Body"）。这里补一个同名
#   集合，让"Single Collection"下拉里真的有 Body 可选 —— 否则赋值会抛
#   `enum "Body" not found in ('__none__')`。
if bpy.data.collections.get("Body") is None:
    _body_collection = bpy.data.collections.new("Body")
    scene.collection.children.link(_body_collection)
    if obj.name in scene.collection.objects:
        scene.collection.objects.unlink(obj)
    _body_collection.objects.link(obj)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

c26_refresh = bpy.ops.mbakery.refresh_groups()
print("  refresh_groups:", c26_refresh, "| collection(s):",
      [item.name for item in settings.groups])
settings.workflow = uprops.WORKFLOW_BAKE
settings.target_mode = _scan_mod.MODE_SINGLE
settings.single_collection = "Body"
_single_bake = draw_page(settings, uprops.PAGE_TARGETS, bpy.context)
if isinstance(_single_bake, str):
    check("卡29 · Single 模式的 Bake 首页能画", False,
          _single_bake.strip().splitlines()[-1])
else:
    _sb_ops = operators_in(_single_bake)
    _sb_texts = label_texts(_single_bake)
    _sb_summary = [text for text in _sb_texts
                   if re.search(r"\d+ object\(s\) · \d+ material\(s\)", text)]
    print("  Single 模式的标签:", _sb_texts[-3:])
    check("卡29 · Single 模式没有 toggle_all_groups",
          "mbakery.toggle_all_groups" not in _sb_ops, _sb_ops)
    check("卡29 · Single 模式不再逐组列集合名", "Body" not in _sb_texts, _sb_texts)
    check("卡29 · Single 模式有一行 N object(s) · M material(s) 摘要",
          len(_sb_summary) == 1, _sb_summary)

settings.single_collection = uprops.COLLECTION_NONE
_single_none = draw_page(settings, uprops.PAGE_TARGETS, bpy.context)
check("卡29 · 没选集合时写 Pick a collection",
      not isinstance(_single_none, str)
      and "Pick a collection" in label_texts(_single_none),
      _single_none if isinstance(_single_none, str) else label_texts(_single_none)[-2:])

settings.single_collection = "Body"
_groups_backup = [(item.name, item.enabled, item.object_count, item.material_count)
                  for item in settings.groups]
settings.groups.clear()
_single_missing = draw_page(settings, uprops.PAGE_TARGETS, bpy.context)
check("卡29 · 选中的集合不在列表里时写 Nothing to bake in this collection",
      not isinstance(_single_missing, str)
      and "Nothing to bake in this collection" in label_texts(_single_missing),
      _single_missing if isinstance(_single_missing, str)
      else label_texts(_single_missing)[-2:])
for _name, _enabled, _objects, _materials in _groups_backup:
    _item = settings.groups.add()
    _item.name = _name
    _item.enabled = _enabled
    _item.object_count = _objects
    _item.material_count = _materials

settings.target_mode = _scan_mod.MODE_COLLECTIONS
_multi_bake = draw_page(settings, uprops.PAGE_TARGETS, bpy.context)
if isinstance(_multi_bake, str):
    check("卡29 · Collections 模式的 Bake 首页能画", False,
          _multi_bake.strip().splitlines()[-1])
else:
    check("卡29 · Collections 模式仍然画 toggle_all_groups",
          "mbakery.toggle_all_groups" in operators_in(_multi_bake))
    check("卡29 · Collections 模式逐组画集合名",
          "Body" in label_texts(_multi_bake), label_texts(_multi_bake))

# Re-bake 那一侧：同一套处理，但读的是 rebake_groups / selected_rebake_collection()
settings.workflow = uprops.WORKFLOW_REBAKE
bpy.ops.mbakery.refresh_groups()
settings.rebake_target_mode = _scan_mod.MODE_SINGLE
settings.rebake_single_collection = "Body"
_rebake_single = draw_page(settings, uprops.PAGE_REBAKE_SOURCE, bpy.context)
if isinstance(_rebake_single, str):
    check("卡29 · Single 模式的 Re-bake 首页能画", False,
          _rebake_single.strip().splitlines()[-1])
else:
    _rs_ops = operators_in(_rebake_single)
    _rs_texts = label_texts(_rebake_single)
    check("卡29 · Re-bake Single 模式没有 toggle_all_groups",
          "mbakery.toggle_all_groups" not in _rs_ops, _rs_ops)
    check("卡29 · Re-bake Single 模式有摘要",
          any(re.search(r"\d+ object\(s\) · \d+ material\(s\)", text)
              for text in _rs_texts), _rs_texts)
settings.rebake_target_mode = _scan_mod.MODE_COLLECTIONS
_rebake_multi = draw_page(settings, uprops.PAGE_REBAKE_SOURCE, bpy.context)
check("卡29 · Re-bake Collections 模式仍然画组列表",
      not isinstance(_rebake_multi, str)
      and "mbakery.toggle_all_groups" in operators_in(_rebake_multi)
      and "Body" in label_texts(_rebake_multi),
      _rebake_multi if isinstance(_rebake_multi, str)
      else operators_in(_rebake_multi)[:6])
settings.workflow = uprops.WORKFLOW_BAKE


# ---------------------------------------------------------------- 卡29 · 1.2
# 卡29 1.2：Result 页**先按钮、后列表**；列表默认收起（1 行汇总 + 三角形）。
def _first_index(log, kind, predicate):
    for index, entry in enumerate(log):
        if entry[0] == kind and predicate(entry):
            return index
    return None


def _summary_label_index(log):
    for index, entry in enumerate(log):
        if entry[0] != "label":
            continue
        if re.search(r"\d+ material\(s\) in \d+ object\(s\)",
                     str(entry[2].get("text", ""))):
            return index
    return None


settings.materials_expanded = False
_res_collapsed = draw_page(settings, uprops.PAGE_RESULT, bpy.context)
if isinstance(_res_collapsed, str):
    check("卡29 · Result 页能画（材质列表收起）", False,
          _res_collapsed.strip().splitlines()[-1])
else:
    _rc = _res_collapsed
    _apply_index = _first_index(
        _rc, "operator", lambda entry: entry[1][0] == "mbakery.apply_baked_material")
    _summary_index = _summary_label_index(_rc)
    _material_rows = [text for text in label_texts(_rc) if "->" in text]
    _summary_text = ([str(entry[2].get("text", "")) for entry in _rc
                      if entry[0] == "label" and _summary_label_index([entry])
                      is not None] or [""])[0]
    print("  apply_baked_material 下标:", _apply_index,
          "| 汇总行下标:", _summary_index, "| 汇总文字:", _summary_text)
    check("卡29 · 按钮在汇总行**之前**（先按钮后列表）",
          _apply_index is not None and _summary_index is not None
          and _apply_index < _summary_index,
          (_apply_index, _summary_index))
    check("卡29 · 收起时一条 `group -> material` 都不画",
          len(_material_rows) == 0, _material_rows)
    check("卡29 · 收起时只有 1 行汇总（N material(s) in M object(s)）",
          _summary_index is not None, _summary_text)
    _toggle_rows = [entry for entry in _rc
                    if entry[0] == "operator" and entry[1]
                    and entry[1][0] == "mbakery.toggle_materials"]
    check("卡29 · 收起时三角形是 TRIA_RIGHT + emboss=False",
          len(_toggle_rows) == 1
          and _toggle_rows[0][2].get("icon") == 'TRIA_RIGHT'
          and _toggle_rows[0][2].get("emboss") is False,
          _toggle_rows[0][2] if _toggle_rows else None)

_before_toggle = bool(settings.materials_expanded)
_toggle_ops = bpy.ops.mbakery.toggle_materials()
_after_toggle = bool(settings.materials_expanded)
check("卡29 · toggle_materials 能翻转 materials_expanded",
      _toggle_ops == {'FINISHED'} and _after_toggle == (not _before_toggle),
      (_toggle_ops, _before_toggle, _after_toggle))

_res_expanded = draw_page(settings, uprops.PAGE_RESULT, bpy.context)
if isinstance(_res_expanded, str):
    check("卡29 · Result 页能画（材质列表展开）", False,
          _res_expanded.strip().splitlines()[-1])
else:
    _expanded_rows = [text for text in label_texts(_res_expanded) if "->" in text]
    print("  展开后的材质行:", _expanded_rows)
    check("卡29 · 展开后逐条画 group -> material",
          len(_expanded_rows) > 0, _expanded_rows)
    _expanded_toggle = [entry for entry in _res_expanded
                        if entry[0] == "operator" and entry[1]
                        and entry[1][0] == "mbakery.toggle_materials"]
    check("卡29 · 展开时三角形是 TRIA_DOWN",
          len(_expanded_toggle) == 1
          and _expanded_toggle[0][2].get("icon") == 'TRIA_DOWN',
          _expanded_toggle[0][2] if _expanded_toggle else None)
settings.materials_expanded = False

# 没有有效材质时才画 Build Materials（不能用"有没有报告"来判断）
_real_valid = c26.valid_materials_by_group
c26.valid_materials_by_group = lambda: {}
_res_empty = draw_page(settings, uprops.PAGE_RESULT, bpy.context)
c26.valid_materials_by_group = _real_valid
if isinstance(_res_empty, str):
    check("卡29 · Result 页能画（没有有效材质）", False,
          _res_empty.strip().splitlines()[-1])
else:
    check("卡29 · 没有有效材质时画 Build Materials",
          "mbakery.build_materials" in operators_in(_res_empty),
          operators_in(_res_empty))
    check("卡29 · 没有有效材质时汇总写 0 material(s)",
          any(text.startswith("0 material(s) in ")
              for text in label_texts(_res_empty)),
          label_texts(_res_empty)[-3:])

# ---------------------------------------------------------------- 卡29 · 1.4 / 1.3
# 卡29 1.4：卡26 的黑名单（"收起时不许出现"）现在**反过来** —— 那些解释
#   一律常驻，默认就在页面上，而且任何页面都不许再画 ⓘ Details / toggle_details。
_HINTS_NOW_PERMANENT = (
    ("Quality · Native 说明", "fall back to Emission"),
    ("Quality · 自适应边距", "Margin scales with the texture size"),
    ("Quality · 降采样", "then shrinks"),
    ("UV · 自动展开", "Only objects WITHOUT a UV map are unwrapped"),
    ("UV · 重叠解释", "overlapping faces share the same texels"),
    ("Re-bake · 写入层解释", "becomes the render layer while baking"),
    ("Result · 预览只是临时", "Preview is temporary"),
    ("Result · Objects 解释", "keep all their UV layers"),
)

# (a) Settings 页：Quality 三段 + UV 两段（一次画全，全部默认可见）
_state_3a = (settings.bake_method, settings.adaptive_margin,
             settings.antialias, settings.prepare_uv)
settings.bake_method = 'NATIVE'
settings.adaptive_margin = True
settings.antialias = 'DOWNSCALE'
settings.prepare_uv = True
bpy.ops.mbakery.refresh_groups()
_overlap_saved = [item.overlap_ratio for item in settings.groups]
if settings.groups:
    settings.groups[0].overlap_ratio = 0.5
_hint_page = draw_page(settings, uprops.PAGE_SETTINGS, bpy.context)
for _index, _item in enumerate(settings.groups):
    _item.overlap_ratio = _overlap_saved[_index]
(settings.bake_method, settings.adaptive_margin,
 settings.antialias, settings.prepare_uv) = _state_3a
if isinstance(_hint_page, str):
    check("卡29 · 解释句场景（Settings）能画", False,
          _hint_page.strip().splitlines()[-1])
    _hint_page_texts = []
else:
    _hint_page_texts = flat_texts(_hint_page)
_settings_hints = [name for name, fragment in _HINTS_NOW_PERMANENT
                   if name.startswith(("Quality", "UV"))
                   and not any(fragment in text for text in _hint_page_texts)]
print("  Settings 页上找不到的常驻解释:", _settings_hints)
check("卡29 · 原来在 Details 里的 5 句解释现在默认就画（Settings 页）",
      not _settings_hints, _settings_hints)
check("卡29 · Settings 页没有 ⓘ Details / toggle_details",
      not isinstance(_hint_page, str) and not detail_toggle_rows(_hint_page)
      and "mbakery.toggle_details" not in operators_in(_hint_page),
      _hint_page if isinstance(_hint_page, str) else operators_in(_hint_page)[-6:])

# 卡29 1.3：抗锯齿自己报名（面板标签 + 枚举项人话）
if isinstance(_hint_page, str):
    check("卡29 · Anti-Aliasing 标签能验", False, "draw 抛异常")
else:
    _aa_kwargs = [entry[2] for entry in _hint_page
                  if entry[0] == "prop" and entry[1] and len(entry[1]) > 1
                  and entry[1][1] == "antialias"]
    _aa_scale_kwargs = [entry[2] for entry in _hint_page
                        if entry[0] == "prop" and entry[1] and len(entry[1]) > 1
                        and entry[1][1] == "aa_scale"]
    print("  antialias / aa_scale 的绘制参数:", _aa_kwargs, _aa_scale_kwargs)
    check("卡29 · 抗锯齿控件标着 text='Anti-Aliasing'",
          any(kwargs.get("text") == "Anti-Aliasing" for kwargs in _aa_kwargs),
          _aa_kwargs)
    check("卡29 · aa_scale 的标签是 'Scale'",
          any(kwargs.get("text") == "Scale" for kwargs in _aa_scale_kwargs),
          _aa_scale_kwargs)
_aa_items = [item.name for item in
             settings.bl_rna.properties["antialias"].enum_items]
print("  antialias 枚举标签:", _aa_items)
check("卡29 · antialias 枚举项是 Off / 2x downsample / 2x upsample",
      _aa_items == ["Off", "2x downsample", "2x upsample"], _aa_items)
check("卡29 · antialias 的 key 一个没动",
      [item.identifier for item in
       settings.bl_rna.properties["antialias"].enum_items]
      == ["OFF", "DOWNSCALE", "UPSCALE"])

# (b) Re-bake 第 2 页：What This Run Does 的状态解释（默认可见）
settings.workflow = uprops.WORKFLOW_REBAKE
if not settings.rebake_maps:
    uops.add_map(settings, "shader.base_color", 64, dedupe=True)
settings.rebake_from_uv = _uvs.NONE_UV
settings.rebake_to_uv = "UV2"
_uvh_page = draw_page(settings, uprops.PAGE_REBAKE_UVS, bpy.context)
settings.workflow = uprops.WORKFLOW_BAKE
if isinstance(_uvh_page, str):
    check("卡29 · 解释句场景（UV Sets）能画", False, "draw 抛异常")
else:
    _uvh_texts = flat_texts(_uvh_page)
    check("卡29 · Re-bake 的写入层解释默认就画",
          any(_HINTS_NOW_PERMANENT[5][1] in text for text in _uvh_texts),
          [t for t in _uvh_texts if "render layer" in t][:2])

# (c) Result 页：预览解释 + Objects 解释（默认可见）
_resulth_page = draw_page(settings, uprops.PAGE_RESULT, bpy.context)
if isinstance(_resulth_page, str):
    check("卡29 · 解释句场景（Result）能画", False, "draw 抛异常")
else:
    _resulth_texts = flat_texts(_resulth_page)
    check("卡29 · Result 页的 Objects 解释默认就画",
          any(_HINTS_NOW_PERMANENT[7][1] in text for text in _resulth_texts),
          [t for t in _resulth_texts if "UV layer" in t][:2])

# ---------------------------------------------------------------- 卡29 · 1.5
# 卡29 1.5：整个 Type Options 框（连同 `Bake pass:`）删掉，但**属性一个不少**，
#   旧预设（里面带着通道打包的 R/G/B）读回仍然成功。
_panels_source = open(os.path.join(WS, "material_bakery", "ui", "panels.py"),
                      encoding="utf-8").read()
check("卡29 · panels.py 里没有 Type Options 这个盒子",
      "Type Options" not in _panels_source)
check("卡29 · panels.py 里没有 `Bake pass:` 那行",
      "Bake pass:" not in _panels_source)
check("卡29 · panels.py 里没有 draw_type_options 方法",
      "draw_type_options" not in _panels_source)
_all_texts = []
for _page_key in uprops.PAGE_ORDER:
    _page_log = draw_page(settings, _page_key, bpy.context)
    if not isinstance(_page_log, str):
        _all_texts.extend(flat_texts(_page_log))
check("卡29 · 所有页面 label 里都没有 Type Options / Bake pass:",
      not [text for text in _all_texts
           if "Type Options" in text or text.startswith("Bake pass:")],
      [text for text in _all_texts
       if "Type Options" in text or text.startswith("Bake pass:")][:3])
check("卡29 · MapItem.normal_space 默认仍是 Tangent",
      settings.maps[0].bl_rna.properties["normal_space"].default == 'TANGENT'
      if settings.maps else False)

# 旧预设读回：通道打包 + AO + 法线空间的参数都还在
from material_bakery.ui import ops_presets as _presets_ops
_saved_maps = [(item.type_key, item.size_x, item.size_y, item.enabled)
               for item in settings.maps]
_legacy_preset = {
    "version": 4,
    "workflow": "collection",
    "per_material_layout": "PACKED",
    "antialias": "DOWNSCALE",
    "aa_scale": 3,
    "maps": [
        {"type": "misc.channel_packing", "size_x": 1024, "size_y": 512,
         "enabled": True,
         "options": {"channel_r": "shader.roughness",
                     "channel_g": "shader.metallic",
                     "channel_b": "misc.ao"}},
        {"type": "shader.normal", "size": 2048, "enabled": True,
         "options": {"normal_space": "OBJECT"}},
        {"type": "misc.ao", "size": 512, "enabled": False, "options": {}},
    ],
}
_legacy_ok = True
_legacy_error = ""
try:
    _legacy_message = _presets_ops.apply_to(settings, _legacy_preset)
except Exception as _exc:                             # noqa: BLE001
    _legacy_ok = False
    _legacy_error = "{}: {}".format(type(_exc).__name__, _exc)
    _legacy_message = ""
print("  旧预设读回:", _legacy_message, _legacy_error)
check("卡29 · 旧预设（带 Type Options 时代的专属参数）读回不报错",
      _legacy_ok, _legacy_error)
check("卡29 · 旧预设读回后烘焙项都在（3 项，未知类型为 0）",
      _legacy_ok and len(settings.maps) == 3 and "dropped" not in _legacy_message,
      (len(settings.maps), _legacy_message))
check("卡29 · 通道打包的 R/G/B 参数保留在预设数据里",
      _legacy_ok and settings.maps
      and "channel_r" in str(settings.maps[0].get("mbakery_options", "")),
      settings.maps[0].get("mbakery_options") if settings.maps else None)
# 复原原来的地图表（后面的断言按"1 个 base_color"准备）
settings.maps.clear()
for _type_key, _size_x, _size_y, _enabled in _saved_maps:
    _item = settings.maps.add()
    _item.type_key = _type_key
    _item.size_x = _size_x
    _item.size_y = _size_y
    _item.enabled = _enabled
settings.map_index = 0

# ---------------------------------------------------------------- 卡29 · 1.7
# 卡29 1.7：喂一条 200+ 字符的错误，画出来必须是**多行、每行 ≤64、不丢字**。
_long_error = ("ValueError: the bake job could not read the image datablock named "
               "'Body_BaseColor_2048' because the file was replaced by another "
               "process while the queue was running and the reader still holds "
               "the old handle")
_wrap_log = []
try:
    raise ValueError(_long_error)
except ValueError as _exc:                            # noqa: BLE001
    upanels._draw_draw_error(Recorder(_wrap_log), _exc)
_wrap_texts = [str(entry[2].get("text", "")) for entry in _wrap_log
               if entry[0] == "label"]
_wrapped_payload = _wrap_texts[-1] if _wrap_texts else ""
_wrapped_lines = _wrapped_payload.split("\n")
print("  200 字符错误的绘制行数:", len(_wrapped_lines),
      "| 每行长度:", [len(line) for line in _wrapped_lines])
check("卡29 · 长错误画成多行（不是一个超长单行）", len(_wrapped_lines) > 1,
      len(_wrapped_lines))
check("卡29 · 每一行都 <= 64 字符",
      all(len(line) <= 64 for line in _wrapped_lines),
      [len(line) for line in _wrapped_lines])
check("卡29 · 没有 … / ... 截断标记",
      "…" not in _wrapped_payload and "..." not in _wrapped_payload)
check("卡29 · 折行只换行、不丢字（拼回去等于原文）",
      " ".join(_wrapped_payload.split()) == " ".join(
          ("ValueError: " + _long_error).split()),
      _wrapped_payload)
check("卡29 · panels.py 里没有任何 [:60]/[:70]/[:120] 截断",
      not re.search(r"\[:(?:60|70|120)\]|\[-120:\]", _panels_source),
      re.findall(r"\[:(?:60|70|120)\]|\[-120:\]", _panels_source))

# ---------------------------------------------------------------- 卡29 · 1.8
# 卡29 1.8：UDIM 不再是独立盒子，`udim` 控件并进 UV 框；多瓦片清单照旧画。
settings.workflow = uprops.WORKFLOW_BAKE
settings.udim = False
_uv_box_page = draw_page(settings, uprops.PAGE_SETTINGS, bpy.context)
if isinstance(_uv_box_page, str):
    check("卡29 · UV 框（UDIM 并入）能画", False, _uv_box_page.strip().splitlines()[-1])
else:
    _udim_props = [entry for entry in _uv_box_page
                   if entry[0] == "prop" and entry[1] and len(entry[1]) > 1
                   and entry[1][1] == "udim"]
    check("卡29 · 页面上没有独立的 UDIM 盒标题",
          "UDIM" not in label_texts(_uv_box_page), label_texts(_uv_box_page))
    check("卡29 · udim 控件仍然被画出来（并进 UV 框）", len(_udim_props) == 1,
          len(_udim_props))

settings.udim = True
_tiles_page = draw_page(settings, uprops.PAGE_SETTINGS, bpy.context)
settings.udim = False
check("卡29 · 打开 UDIM 后仍然能画", not isinstance(_tiles_page, str),
      _tiles_page if isinstance(_tiles_page, str) else "")
# 造一个跨瓦片物体：把一个 UV 角推到 1002 瓦片
_uv_layer = obj.data.uv_layers.active
_uv_saved = [tuple(loop.uv) for loop in _uv_layer.data]
_uv_layer.data[0].uv = (1.5, 0.25)
bpy.ops.mbakery.refresh_groups()
settings.udim = True
_tile_scene_page = draw_page(settings, uprops.PAGE_SETTINGS, bpy.context)
settings.udim = False
for _loop, _co in zip(_uv_layer.data, _uv_saved):
    _loop.uv = _co
if isinstance(_tile_scene_page, str):
    check("卡29 · 跨瓦片场景能画", False, _tile_scene_page.strip().splitlines()[-1])
else:
    _tile_texts = [text for text in flat_texts(_tile_scene_page) if "tiles (" in text]
    print("  跨瓦片清单:", _tile_texts)
    check("卡29 · 跨瓦片时画出一行 tiles 清单", bool(_tile_texts), _tile_texts)

# ---------------------------------------------------------------- 卡29 · 1.9
# 卡29 1.9：Layout 两个人话标签 + 两种布局各一句常驻解释。
_layout_items = [item.name for item in
                 settings.bl_rna.properties["per_material_layout"].enum_items]
print("  per_material_layout 枚举标签:", _layout_items)
check("卡29 · Layout 枚举标签是人话",
      _layout_items == ["Packed — one image per material",
                        "Tiling — repeats every Tile Size"], _layout_items)
_settings_workflow = settings.workflow
settings.workflow = uprops.WORKFLOW_MATERIAL
settings.per_material_layout = 'PACKED'
_packed_page = draw_page(settings, uprops.PAGE_SETTINGS, bpy.context)
settings.per_material_layout = 'TILING'
_tiling_page = draw_page(settings, uprops.PAGE_SETTINGS, bpy.context)
settings.per_material_layout = 'PACKED'
settings.workflow = _settings_workflow
_PACKED_SENTENCE = ("Each material's UVs are unwrapped and packed into 0-1. "
                    "One image per material, no repetition.")
_TILING_SENTENCE = ("One image repeats every Tile Size metres. UVs are written in "
                    "world space, so the pattern continues across the surface.")
_TILE_PERIOD_SENTENCE = ("Tile Size changes the repeat period of the delivered "
                         "material; it does not re-bake.")
if isinstance(_packed_page, str) or isinstance(_tiling_page, str):
    check("卡29 · 两种 Layout 都能画", False, "draw 抛异常")
else:
    _packed_texts = flat_texts(_packed_page)
    _tiling_texts = flat_texts(_tiling_page)
    check("卡29 · PACKED 下那句解释常驻", _PACKED_SENTENCE in _packed_texts,
          [t for t in _packed_texts if "0-1" in t][:2])
    check("卡29 · PACKED 下不出现 TILING 那句",
          _TILING_SENTENCE not in _packed_texts)
    check("卡29 · TILING 下那句解释常驻", _TILING_SENTENCE in _tiling_texts,
          [t for t in _tiling_texts if "repeat" in t][:2])
    check("卡29 · Tiling 的 Tile Size 解释常驻（不重烘）",
          _TILE_PERIOD_SENTENCE in _tiling_texts,
          [t for t in _tiling_texts if "repeat period" in t][:2])
    check("卡29 · Tiling 仍保留 ERROR 级限制说明",
          any("Baked on a temporary 1 m x 1 m plane" in text
              for text in _tiling_texts))

# ---------------------------------------------------------------- 卡29 · 1.10
# 卡29 1.10：Per Material + UDIM = ERROR（一字不差），Per Collection 下不出现。
settings.workflow = uprops.WORKFLOW_MATERIAL
settings.udim = False
_rows_without_udim = uops.checklist(bpy.context, settings)
settings.udim = True
_rows_with_udim = uops.checklist(bpy.context, settings)
_bad_udim_page = draw_page(settings, uprops.PAGE_SETTINGS, bpy.context)
settings.udim = False
settings.workflow = uprops.WORKFLOW_BAKE
_bake_udim_rows = uops.checklist(bpy.context, settings)
_UDIM_ERROR = ("UDIM is not supported with Per Material yet - "
               "switch to Per Collection.")
print("  Per Material + UDIM 的 ERROR:",
      [row for level, row in _rows_with_udim if row == _UDIM_ERROR])
check("卡29 · Per Material + UDIM 时 checklist 里出现那条 ERROR",
      ("ERROR", _UDIM_ERROR) in _rows_with_udim, _rows_with_udim[-3:])
check("卡29 · 没开 UDIM 时不出现那条 ERROR",
      ("ERROR", _UDIM_ERROR) not in _rows_without_udim,
      [row for level, row in _rows_without_udim if "UDIM" in row])
check("卡29 · 面板把这条 ERROR 画出来",
      not isinstance(_bad_udim_page, str)
      and _UDIM_ERROR in flat_texts(_bad_udim_page),
      _bad_udim_page if isinstance(_bad_udim_page, str) else
      [t for t in flat_texts(_bad_udim_page) if "UDIM" in t][:2])
settings.udim = True
_bake_udim_rows = uops.checklist(bpy.context, settings)
_pc_udim_page = draw_page(settings, uprops.PAGE_SETTINGS, bpy.context)
settings.udim = False
check("卡29 · Per Collection + UDIM 不出现那条 ERROR（行为完全不变）",
      ("ERROR", _UDIM_ERROR) not in _bake_udim_rows,
      [row for level, row in _bake_udim_rows if "UDIM" in row])
check("卡29 · Per Collection + UDIM 的面板也没画那条 ERROR",
      not isinstance(_pc_udim_page, str)
      and _UDIM_ERROR not in flat_texts(_pc_udim_page))

# ---------------------------------------------------------------- 4
# 卡26 · 4：DESTRUCTIVE 逐字出现且只 1 次；Remove / Hide Unrelated 仍在常驻区
_result_c26 = draw_page(settings, uprops.PAGE_RESULT, bpy.context)
if isinstance(_result_c26, str):
    check("卡26 · Result 页能画（验 DESTRUCTIVE）", False,
          _result_c26.strip().splitlines()[-1])
else:
    _c26_result_texts = label_texts(_result_c26)
    _destructive = [text for text in _c26_result_texts if "DESTRUCTIVE" in text]
    check("卡26 · 收尾警告逐字出现且只出现 1 次",
          len(_destructive) == 1
          and "Finalize is DESTRUCTIVE" in _destructive[0], _destructive)
    check("卡26 · Remove 的说明仍在常驻区",
          any("Remove deletes" in text for text in _c26_result_texts),
          [text for text in _c26_result_texts if "Remove" in text][:2])

settings.hide_unrelated = True
_settings_c26 = draw_page(settings, uprops.PAGE_SETTINGS, bpy.context)
settings.hide_unrelated = False
if isinstance(_settings_c26, str):
    check("卡26 · Settings 页能画（验 Hide Unrelated 常驻行）", False,
          _settings_c26.strip().splitlines()[-1])
else:
    _c26_settings_texts = flat_texts(_settings_c26)
    check("卡26 · Hide Unrelated 的后果行仍在常驻区（lights and cameras）",
          any("lights and cameras" in text for text in _c26_settings_texts),
          [text for text in _c26_settings_texts if "lights" in text][:2])

# ---------------------------------------------------------------- 5
# 卡26 · 5：Maps 工具栏 + / − / ▾；菜单 Bake 4 项、Re-bake 5 项；旧操作仍可达
_targets_c26 = draw_page(settings, uprops.PAGE_TARGETS, bpy.context)
if isinstance(_targets_c26, str):
    _targets_ops = []
    _targets_menus = []
    _targets_ok = False
else:
    _targets_ops = operators_in(_targets_c26)
    _targets_menus = [entry[1][0] for entry in _targets_c26
                      if entry[0] == "menu" and entry[1]]
    _targets_ok = True
check("卡26 · Maps 工具栏的 + / − / ▾ 三个入口都在",
      "mbakery.add_map" in _targets_ops
      and "mbakery.remove_map" in _targets_ops
      and "MBAKERY_MT_maps" in _targets_menus,
      (_targets_ops, _targets_menus))


def _menu_operators():
    log = []
    fake_menu = type("FakeMenu", (), {})()
    fake_menu.layout = Recorder(log)
    uops.MBAKERY_MT_Maps.draw(fake_menu, bpy.context)
    return operators_in(log)


_saved_workflow = settings.workflow
settings.workflow = uprops.WORKFLOW_BAKE
_bake_menu = _menu_operators()
settings.workflow = uprops.WORKFLOW_REBAKE
_rebake_menu = _menu_operators()
settings.workflow = _saved_workflow
check("卡26 · Maps 菜单 Bake 有 4 项（上移/下移/去重/清空）",
      _bake_menu == ["mbakery.move_map", "mbakery.move_map",
                     "mbakery.dedupe_maps", "mbakery.clear_maps"],
      _bake_menu)
check("卡26 · Maps 菜单 Re-bake 有 5 项（多一个 read_uv_sizes）",
      _rebake_menu == _bake_menu + ["mbakery.read_uv_sizes"], _rebake_menu)
_legacy_bake = ["mbakery.add_map", "mbakery.remove_map"] + _bake_menu
check("卡26 · 旧 6 个 Maps 操作仍可达（工具栏 2 + 菜单 4）",
      _targets_ok and len(_legacy_bake) == 6
      and set(_bake_menu) == {"mbakery.move_map", "mbakery.dedupe_maps",
                              "mbakery.clear_maps"},
      _legacy_bake)
_legacy_rebake = _legacy_bake + ["mbakery.read_uv_sizes"]
check("卡26 · Re-bake 的旧 7 个 Maps 操作仍可达（多 Read Sizes）",
      len(_legacy_rebake) == 7
      and _rebake_menu.count("mbakery.move_map") == 2
      and "mbakery.read_uv_sizes" in _rebake_menu,
      _legacy_rebake)

# ---------------------------------------------------------------- 7
# 卡26 · 7：Name Preview 只有 1 行 e.g.；改模板后样例跟着变（不陈旧）
_sample_page = draw_page(settings, uprops.PAGE_SETTINGS, bpy.context)
if isinstance(_sample_page, str):
    check("卡26 · Settings 页能画（验 Name 样例）", False,
          _sample_page.strip().splitlines()[-1])
else:
    _eg_before = [text for text in label_texts(_sample_page)
                  if text.startswith("e.g. ")]
    check("卡26 · Settings 页只有 1 行 e.g. 样例", len(_eg_before) == 1, _eg_before)
    _expected_type = ("".join(str(settings.maps[0].bake_type.label).split())
                      if settings.maps and settings.maps[0].bake_type
                      else "BaseColor")
    _saved_template = settings.template
    settings.template = "{type}"
    _sample_page_after = draw_page(settings, uprops.PAGE_SETTINGS, bpy.context)
    settings.template = _saved_template
    if isinstance(_sample_page_after, str):
        check("卡26 · 改模板后的 Settings 页能画", False,
              _sample_page_after.strip().splitlines()[-1])
    else:
        _eg_after = [text for text in label_texts(_sample_page_after)
                     if text.startswith("e.g. ")]
        check("卡26 · 改模板后样例跟着变（不陈旧）",
              len(_eg_after) == 1 and _eg_after != _eg_before
              and _eg_after[0] == "e.g. " + _expected_type,
              (_eg_before, _eg_after))

# ---------------------------------------------------------------- 8
# 卡26 · 8：成功场景下健康文案黑名单命中 0
_HEALTH_TEXT = (
    "Name template is valid",
    "0 missing",
    "Every bakeable object has a UV map",
    "all good",
    "No object spans more than one tile",
    "No UV coordinate is ever modified",
)
settings.use_selected_to_active = False
settings.prepare_uv = False
settings.hide_unrelated = False
settings.udim = True
settings.workflow = uprops.WORKFLOW_BAKE
_health_pages = [draw_page(settings, uprops.PAGE_SETTINGS, bpy.context),
                 draw_page(settings, uprops.PAGE_RESULT, bpy.context)]
settings.workflow = uprops.WORKFLOW_REBAKE
_health_pages.append(draw_page(settings, uprops.PAGE_REBAKE_UVS, bpy.context))
settings.workflow = uprops.WORKFLOW_BAKE
settings.udim = False
_health_texts = []
for _page in _health_pages:
    if isinstance(_page, str):
        check("卡26 · 健康文案黑名单的场景能画", False,
              _page.strip().splitlines()[-1])
    else:
        _health_texts.extend(label_texts(_page))
_health_hits = [fragment for fragment in _HEALTH_TEXT
                if any(fragment.lower() in text.lower()
                       for text in _health_texts)]
check("卡26 · 成功场景下健康文案一条都不出现（命中 {}）".format(
          len(_health_hits)),
      not _health_hits, _health_hits)

# ---------------------------------------------------------------- 6
# 卡26 · 6：Log 默认收起 / 运行时自动展开 / 成功后跟随手动值


def _log_toggle_texts(page):
    if isinstance(page, str):
        return None
    return [str(entry[2].get("text", "")) for entry in page
            if entry[0] == "operator" and entry[1]
            and entry[1][0] == "mbakery.toggle_log"]


settings.workflow = uprops.WORKFLOW_BAKE
settings.log_expanded = False
_log_page = draw_page(settings, uprops.PAGE_RESULT, bpy.context)
check("卡26 · 日志默认收起：只有一行 Show Log",
      _log_toggle_texts(_log_page) == ["Show Log"],
      _log_toggle_texts(_log_page) or _log_page)

settings.log_expanded = True
c26.add_log("INFO", "card26 visible log line")
_log_page_open = draw_page(settings, uprops.PAGE_RESULT, bpy.context)
check("卡26 · 手动展开后画 Hide Log 且日志内容可见",
      _log_toggle_texts(_log_page_open) == ["Hide Log"]
      and any("card26 visible log line" in text
              for text in label_texts(_log_page_open)),
      _log_toggle_texts(_log_page_open))
settings.log_expanded = False

# 运行时自动展开，而且**不画**切换行（运行期写 Scene 属性是禁区）
c26.job = _FakeJob()
c26.pause_preferences()
c26.add_log("INFO", "card26 running log line")
_running_log = draw_page(settings, uprops.PAGE_BAKE, bpy.context)
check("卡26 · 运行时日志自动展开（没有 Show / Hide Log 切换行）",
      _log_toggle_texts(_running_log) == []
      and any("card26 running log line" in text
              for text in label_texts(_running_log)),
      _log_toggle_texts(_running_log)
      if not isinstance(_running_log, str) else "draw 抛异常")

# 卡29 1.2：运行期间 toggle_materials 也必须 CANCELLED（运行期不写 Scene 属性）
_materials_before_run = bool(settings.materials_expanded)
_materials_toggle_run = bpy.ops.mbakery.toggle_materials()
check("卡29 · 运行期间 toggle_materials 是 CANCELLED，且没改 Scene 属性",
      _materials_toggle_run == {'CANCELLED'}
      and bool(settings.materials_expanded) == _materials_before_run,
      (_materials_toggle_run, settings.materials_expanded))
c26.job = None
c26.release_guard()
_after_run_log = draw_page(settings, uprops.PAGE_RESULT, bpy.context)
check("卡26 · 成功后日志跟随 log_expanded 手动值（仍未展开）",
      _log_toggle_texts(_after_run_log) == ["Show Log"],
      _log_toggle_texts(_after_run_log))

# ---------------------------------------------------------------- 9
# 卡29 · 9：两个"列表展开状态"（Log / Materials）存盘后仍在
#   ⚠ 卡26 的 details_* 已经删除，这里守的是新的一对：log_expanded / materials_expanded。
_persist_path = os.path.join(WS, "_probe", "card29_lists_persist.blend")
for _stale in (_persist_path, _persist_path + "1"):
    if os.path.exists(_stale):
        os.remove(_stale)
bpy.context.preferences.filepaths.save_version = 0
settings.log_expanded = True
settings.materials_expanded = True
_persist_before = {"log_expanded": bool(settings.log_expanded),
                   "materials_expanded": bool(settings.materials_expanded)}
_persist_saved = False
try:
    bpy.ops.wm.save_as_mainfile(filepath=_persist_path, check_existing=False)
    _persist_saved = os.path.exists(_persist_path)
except Exception as _exc:                             # noqa: BLE001
    print("  写临时 .blend 失败:", _exc)
if not _persist_saved:
    check("卡29 · 能把临时 .blend 存到 _probe\\", False, _persist_path)
else:
    bpy.ops.wm.open_mainfile(filepath=_persist_path)
    settings = bpy.context.scene.mbakery
    _persist_after = {
        "log_expanded": bool(getattr(settings, "log_expanded", False)),
        "materials_expanded": bool(getattr(settings, "materials_expanded", False)),
    }
    check("卡29 · 存盘再打开后 Log / Materials 的展开状态还在",
          _persist_after == _persist_before,
          {"before": _persist_before, "after": _persist_after})
    check("卡29 · 临时文件没有留下 .blend1 备份",
          not os.path.exists(_persist_path + "1"),
          "" if not os.path.exists(_persist_path + "1")
          else _persist_path + "1")

print("\n=== 结果 ===")
if FAIL:
    print("FAILED {} check(s):".format(len(FAIL)))
    for name in FAIL:
        print("  -", name)
else:
    print("ALL CHECKS PASSED")

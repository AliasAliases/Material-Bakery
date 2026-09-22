"""把向导每一页真的"画"一遍

为什么需要这个：如果某一页的 draw() 抛异常，Blender 只会把错误打到控制台，
**这一页剩下的控件（包括底部导航）就全都不会画出来** ——
界面上表现为"按钮不见了"，而所有后台测试照样全绿（它们从不调用 draw）。

所以这里用一个记录型的假 layout 把 5 页各画一遍：
    - 任何一页抛异常 -> 失败，并打出是哪一页
    - 每页都必须画出前进/后退按钮
    - 非第一页必须有 Back，非最后一页必须有 Next，最后一页必须有 Start Over
"""
import bpy, sys, os, traceback, ast

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
        item.size = 2048
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
    settings_props = [entry[1][1] for entry in expanded
                      if entry[0] == "prop" and entry[1] and len(entry[1]) > 1
                      and entry[1][0] is settings]
    print("  第 5 页画出的设置级 prop:", settings_props)
    check("同一个设置属性不会画两遍（不会出现两个一样的下拉）",
          len(settings_props) == len(set(settings_props)),
          [p for p in settings_props if settings_props.count(p) > 1])
settings.texture_expanded_group = ""

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

expected_rows = [message for _level, message in
                 uops_panel.checklist(bpy.context, settings)]
settings.output_dir = saved_output
settings.hide_unrelated = False
if isinstance(switched, str):
    check("Hide Unrelated 开着时也能画", False, switched.strip().splitlines()[-1:])
else:
    texts = [str(entry[2].get("text", "")) for entry in switched if entry[0] == "label"]
    check("开着的时候写清了只藏网格、灯光摄像机不动",
          any("lights and cameras" in text for text in texts),
          [t for t in texts if "lights" in t][:2])
    # ⚠ 这条要盯的是"第 2 页真的把 Checklist 的每一行都画出来了"，
    #   不是"页面上有含 sample 字样的文字"（那种弱断言会因为别的静态标签通过）。
    missing_rows = [row for row in expected_rows if row not in texts]
    check("Checklist 算出来的每一行都真的画在设置页上",
          expected_rows and not missing_rows, missing_rows[:2] or expected_rows[:1])

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
    size = 2048
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
    texts = [str(entry[2].get("text", "")) for entry in bake_page if entry[0] == "label"]
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

print("\n=== 结果 ===")
if FAIL:
    print("FAILED {} check(s):".format(len(FAIL)))
    for name in FAIL:
        print("  -", name)
else:
    print("ALL CHECKS PASSED")

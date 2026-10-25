"""审计：向导每一页里有哪些"没有文字"的控件。

起因是用户说"第三页 Checklist 下面有个意义不明的钩，后面没有文字解释"。
与其猜是哪一个，不如把每一页都画一遍、把所有 text="" 的 prop 列出来。

用法: blender --background --factory-startup --python tools/audit_panel_labels.py
"""
import bpy, sys, os, traceback

# 工作区 = 本文件所在目录的上一级。
# ⚠ 这里原来写死的是开发机上的绝对路径，clone 到别处每个套件都跑不起来，
#   而且公开版/私有版两份检出会互相 import 错代码。
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WS not in sys.path:
    sys.path.insert(0, WS)

import material_bakery
from material_bakery import compat
from material_bakery.ui import panels as upanels
from material_bakery.ui import properties as uprops

compat.set_silent(True)


class Recorder:
    def __init__(self, log):
        self._log = log

    def prop(self, data, name=None, **kwargs):
        self._log.append(("prop", (data, name), kwargs))
        return Recorder(self._log)

    def label(self, text="", icon=None, **kwargs):
        self._log.append(("label", (), dict(kwargs, text=text, icon=icon)))
        return Recorder(self._log)

    def operator(self, idname=None, icon=None, **kwargs):
        self._log.append(("operator", (idname,), dict(kwargs, icon=icon)))
        return Recorder(self._log)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def method(*args, **kwargs):
            self._log.append((name, args, kwargs))
            return Recorder(self._log)
        return method


bpy.ops.wm.read_factory_settings(use_empty=True)
material_bakery.register()
scene = bpy.context.scene
mesh = bpy.data.meshes.new("M")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
mesh.update()
mesh.uv_layers.new(name="UVMap")
material = bpy.data.materials.new("M")
material.use_nodes = True
mesh.materials.append(material)
obj = bpy.data.objects.new("Body", mesh)
scene.collection.objects.link(obj)
obj.select_set(True)
bpy.context.view_layer.objects.active = obj

settings = scene.mbakery
panels = upanels.MBAKERY_PT_Wizard
print("=" * 74)
print("没有文字的控件审计")
print("=" * 74)
for page_id, page_label, _desc in uprops.PAGES:
    settings.page = page_id
    log = []
    recorder = Recorder(log)

    class FakePanel:
        layout = recorder

        def __getattr__(self, name):
            attribute = getattr(panels, name)
            return attribute.__get__(self, type(self)) if callable(attribute) else attribute

    try:
        panels.draw(FakePanel(), bpy.context)
    except Exception:
        print("\n[{}] draw 抛异常:\n{}".format(page_label, traceback.format_exc()))
        continue

    silent = []
    icon_no_text = []
    for kind, args, kwargs in log:
        if kind == "prop":
            data, name = args
            if kwargs.get("text", None) == "":
                silent.append((type(data).__name__, name,
                               type(getattr(data, name, None)).__name__))
        elif kind == "label":
            # ⚠ 这一类才是用户实际报的那个 bug：一个光秃秃的图标 + 空文字。
            #   "意义不明的钩，后面没有文字解释" —— 就是文本为空的 label。
            icon = kwargs.get("icon")
            text = kwargs.get("text") or ""
            if icon and not text and icon != 'BLANK1':
                icon_no_text.append(icon)
    print("\n[{}] 一共 {} 个控件，其中没文字的 {} 个:".format(
        page_label, sum(1 for e in log if e[0] == "prop"), len(silent)))
    for owner, name, value_type in silent:
        print("    - {:24s} {}  ({})".format(name, owner, value_type))
    if icon_no_text:
        print("    ⚠ 只有图标没有文字的 label: {}".format(
            ", ".join(sorted(set(icon_no_text)))))
    else:
        print("    ok 没有'光秃秃的图标'label")

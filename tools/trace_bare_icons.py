"""找出"只有图标没有文字"的 label 到底是哪一行画的。

用户报第 3 页有个"意义不明的钩"。测试能判定"有"，但要说清"是哪一行"，
得把 draw() 的记录按顺序打出来，看它前后是什么控件。
"""
import bpy, sys, os

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
        self._log.append(("prop", (getattr(data, "name", type(data).__name__), name), kwargs))
        return Recorder(self._log)

    def label(self, text="", icon=None, **kwargs):
        self._log.append(("label", (text,), dict(kwargs, icon=icon)))
        return Recorder(self._log)

    def operator(self, idname=None, icon=None, **kwargs):
        self._log.append(("operator", (idname,), kwargs))
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
settings = scene.mbakery

panels = upanels.MBAKERY_PT_Wizard
for page in ("targets", "settings"):
    settings.page = page
    log = []
    recorder = Recorder(log)

    class FakePanel:
        layout = recorder

        def __getattr__(self, name):
            attribute = getattr(panels, name)
            return attribute.__get__(self, type(self)) if callable(attribute) else attribute

    panels.draw(FakePanel(), bpy.context)
    print("=" * 70)
    print("page:", page)
    print("=" * 70)
    for index, (kind, args, kwargs) in enumerate(log):
        if kind == "label":
            # ⚠ text 是**位置参数**，不在 kwargs 里 —— 上一版这里读 kwargs，
            #   于是每个 label 都显示成 text=None，把工具自己搞成了满屏误报。
            text = args[0] if args else ""
            icon = kwargs.get("icon")
        flag = ""
        if kind == "label" and icon not in (None, 'BLANK1') and not (text or "").strip():
            flag = "   <<<<<< 光秃秃的图标"
        if kind == "label":
            print("{:3d} label   text={!r:34s} icon={!r}{}".format(index, text, icon, flag))
        elif kind == "prop":
            print("{:3d} prop    {} . {}".format(index, args[0], args[1]))
        else:
            print("{:3d} {}   {}".format(index, kind, args))

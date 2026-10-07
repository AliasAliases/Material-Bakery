"""验证"装好的那份"能真的被 Blender 装上并跑起来。

跟其它测试不一样：这里**不用 sys.path 注入**，而是走 Blender 正规的
`addon_enable` 路径 —— 也就是用户实际会经历的流程。

⚠ 插件目录必须在 Blender **启动之前**就放好：addon 搜索路径是启动时扫描的，
  运行中新建的目录不会被认出来（第一版就是这么失败的：
  `No module named 'material_bakery'`）。
  所以复制这一步在 shell 里做 —— 见 tests/README.md 的说明。

隔离配置目录，不会碰你真实的 Blender 设置。
"""
import bpy, sys, os, glob

# 工作区 = 本文件所在目录的上一级。
# ⚠ 这里原来写死的是开发机上的绝对路径，clone 到别处每个套件都跑不起来，
#   而且公开版/私有版两份检出会互相 import 错代码。
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(WS, "_probe", "install")
target = os.path.join(PROBE, "scr", "addons", "material_bakery")

FAIL = []
def check(label, ok, extra=""):
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", label,
                               ("  " + str(extra)) if extra else ""))
    if not ok:
        FAIL.append(label)

print("=== 检查已放好的插件目录 ===")
print("  ", target)
if not os.path.isdir(target):
    print("  插件目录不存在 —— 这一步要在启动 Blender 之前由 shell 完成，")
    print("  见 tests/README.md 里 mb_test_install.py 的运行方式。")
    sys.exit(1)

check("打包目录里有 __init__.py", os.path.isfile(os.path.join(target, "__init__.py")))
check("打包目录名是合法模块名（没有点、没有空格）",
      "material_bakery".isidentifier(), "material_bakery")
check("没有残留 __pycache__",
      not glob.glob(os.path.join(target, "**", "__pycache__"), recursive=True))
check("没有残留 .pyc",
      not glob.glob(os.path.join(target, "**", "*.pyc"), recursive=True))
check("子包齐全（core / engine / deliver / ui）",
      all(os.path.isdir(os.path.join(target, part))
          for part in ("core", "engine", "deliver", "ui")),
      [d for d in os.listdir(target) if os.path.isdir(os.path.join(target, d))])

try:
    result = bpy.ops.preferences.addon_enable(module="material_bakery")
    check("addon_enable 成功", result == {'FINISHED'}, result)
except Exception as exc:
    check("addon_enable 成功", False, "{}: {}".format(type(exc).__name__, exc))

check("启用后出现在已启用列表里",
      "material_bakery" in bpy.context.preferences.addons.keys(),
      list(bpy.context.preferences.addons.keys())[-3:])

import material_bakery as loaded
check("加载到的是打包的那份",
      os.path.normcase(os.path.dirname(loaded.__file__)) == os.path.normcase(target),
      (os.path.dirname(loaded.__file__), target))
# ⚠ 不要写死版本号：每发一版都会 +1，写死了就是假失败
#   （同类错误已出现四次：operator 数量、.py 文件数、版本号 ×2）。
_version = getattr(loaded, "bl_info", {}).get("version", (0, 0, 0))
check("版本号是合法的三段元组", isinstance(_version, tuple) and len(_version) == 3,
      _version)
check("装好的包有打包时间戳（用来确认装的是哪一版）",
      bool(getattr(loaded, "__build__", "")), getattr(loaded, "__build__", ""))
check("Scene 上有向导设置", hasattr(bpy.context.scene, "mbakery"))
check("Scene 上的版本标记跟 bl_info 一致",
      getattr(bpy.context.scene, "mbakery_version", "")
      == ".".join(str(v) for v in _version),
      (getattr(bpy.context.scene, "mbakery_version", None), _version))

# ⚠ 查 bpy.types 里的面板要用 **bl_idname**（MBAKERY_PT_wizard），
#   不是 Python 类名。查错了会得出"面板没注册"的假结论。
panel_cls = getattr(bpy.types, "MBAKERY_PT_wizard", None)
check("N 面板真的注册上了", panel_cls is not None)
check("面板在 3D 视图侧栏",
      panel_cls is not None and panel_cls.bl_space_type == 'VIEW_3D'
      and panel_cls.bl_region_type == 'UI'
      and panel_cls.bl_category == "Material Bakery",
      (getattr(panel_cls, "bl_space_type", None),
       getattr(panel_cls, "bl_region_type", None),
       getattr(panel_cls, "bl_category", None)))
# ⚠ 不要在这里写死数量。原来写的是 32，加完 Remesh 页那三个 operator
#   之后就成了 35 —— 断言过期，看起来像插件坏了。
#   改成"拿打包目录里声明的 bl_idname 跟真正注册上的对一遍"，
#   这样以后加 operator 不用再改这个测试。
import re
declared = set()
for path in glob.glob(os.path.join(target, "ui", "ops*.py")):
    with open(path, encoding="utf-8") as handle:
        declared |= set(re.findall(r'bl_idname\s*=\s*"mbakery\.([A-Za-z0-9_]+)"',
                                   handle.read()))
registered = set(n for n in dir(bpy.ops.mbakery) if not n.startswith("_"))
print("  声明 {} 个 / 注册 {} 个".format(len(declared), len(registered)))
check("operator 数量与声明一致（{} 个）".format(len(declared)),
      declared == registered,
      (sorted(declared - registered), sorted(registered - declared)))

print("\n=== 装好之后能不能真的烘 ===")
from material_bakery.core import plan as pl
from material_bakery.core import transaction as txmod
from material_bakery.engine import backend as bk
from material_bakery.engine import job as jb

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.samples = 1
scene.render.bake.margin = 2

mesh = bpy.data.meshes.new("M")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
mesh.update()
mesh.uv_layers.new(name="UVMap")
material = bpy.data.materials.new("Test")
material.use_nodes = True
for node in material.node_tree.nodes:
    if node.type == 'BSDF_PRINCIPLED':
        node.inputs["Base Color"].default_value = (0.0, 1.0, 0.0, 1.0)
mesh.materials.append(material)
obj = bpy.data.objects.new("Quad", mesh)
scene.collection.objects.link(obj)

settings = pl.BakeSettings(
    maps=pl.map_requests_from_pairs([("shader.base_color", 32)]),
    file_format='PNG')
plan = pl.build_plan(bpy.context, settings)
job = jb.BakeJob(bpy.context, plan, settings,
                 backend=bk.CyclesBackend(txmod.SceneTransaction(bpy.context),
                                          margin=2, samples=1))
job.run_to_completion()
center = [round(v, 3) for v in list(plan.tasks[0].image.pixels[4 * (32 * 16 + 16):][:4])]
print("  烘出来的像素:", center)
check("装好的版本能真机烘焙", job.report.failed == 0 and job.report.done == 1,
      job.report.summary())
check("像素正确（绿色）", center[1] > 0.9 and center[0] < 0.1, center)

print("\n=== 卸载 ===")
try:
    bpy.ops.preferences.addon_disable(module="material_bakery")
    check("能正常禁用", "material_bakery" not in bpy.context.preferences.addons.keys())
except Exception as exc:
    check("能正常禁用", False, "{}: {}".format(type(exc).__name__, exc))

print("\n=== 结果 ===")
if FAIL:
    print("FAILED {} check(s):".format(len(FAIL)))
    for name in FAIL:
        print("  -", name)
else:
    print("ALL CHECKS PASSED")

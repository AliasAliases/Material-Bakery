"""Material Bakery — 跨版本冒烟测试

在 3.6 / 4.5 / 5.2 上跑同一套最核心的路径，看哪些功能在哪些版本可用。
刻意做得**宽容**：某一项不支持就记下来，不当成失败 ——
目的是得出一张"哪个版本支持什么"的事实表，而不是假装全都一样。

用法：
    blender --background --factory-startup --python tests/mb_smoke.py
"""
import bpy, sys, os, shutil, tempfile, traceback

# 工作区 = 本文件所在目录的上一级。
# ⚠ 这里原来写死的是开发机上的绝对路径，clone 到别处每个套件都跑不起来，
#   而且公开版/私有版两份检出会互相 import 错代码。
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WS not in sys.path:
    sys.path.insert(0, WS)

OUT = os.path.join(tempfile.gettempdir(), "mb_smoke")
if os.path.isdir(OUT):
    shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT, exist_ok=True)

FAIL = []
NOTES = []


def check(label, ok, extra=""):
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", label,
                               ("  " + str(extra)) if extra else ""))
    if not ok:
        FAIL.append(label)


def note(message):
    NOTES.append(message)
    print("  [note] {}".format(message))


print("=" * 70)
print("Material Bakery 跨版本冒烟   Blender {}".format(bpy.app.version_string))
print("=" * 70)

# ------------------------------------------------------------------ 注册
print("\n--- 注册 ---")
import material_bakery
from material_bakery import compat
from material_bakery.core import bake_types as bt
from material_bakery.core import plan as pl
from material_bakery.core import transaction as txmod
from material_bakery.engine import backend as bk
from material_bakery.engine import job as jb
from material_bakery.engine import surgery as sg

compat.set_silent(True)
try:
    material_bakery.register()
    check("插件能注册", hasattr(bpy.context.scene, "mbakery"))
except Exception as exc:
    check("插件能注册", False, "{}: {}".format(type(exc).__name__, exc))
    traceback.print_exc()
    print("\n注册就失败了，后面的检查没有意义")
    sys.exit(1)

check("版本判定可用（IS_4X={}）".format(compat.IS_4X), isinstance(compat.IS_4X, bool))
check("类型表是 44 种", len(bt.REGISTRY) == 44, len(bt.REGISTRY))

# 能用哪些节点，是跨版本差异的主要来源
for node_type in ("ShaderNodeEmission", "ShaderNodeTexImage", "ShaderNodeOutputMaterial",
                  "ShaderNodeBsdfPrincipled", "ShaderNodeAmbientOcclusion",
                  "ShaderNodeNewGeometry", "ShaderNodeUVMap", "ShaderNodeVertexColor",
                  "ShaderNodeAttribute", "ShaderNodeSeparateColor",
                  "ShaderNodeCombineColor", "ShaderNodeSeparateRGB",
                  "ShaderNodeCombineRGB", "ShaderNodeNormalMap", "ShaderNodeMath"):
    available = getattr(bpy.types, node_type, None) is not None
    print("    {:<32} {}".format(node_type, "有" if available else "没有"))
    if not available:
        note("这个版本没有 {}".format(node_type))

# ------------------------------------------------------------------ 场景
print("\n--- 场景 ---")
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
if hasattr(scene, "cycles"):
    scene.cycles.samples = 1
    if hasattr(scene.cycles, "use_denoising"):
        scene.cycles.use_denoising = False
scene.render.bake.margin = 2

mesh = bpy.data.meshes.new("QuadMesh")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
mesh.update()
mesh.uv_layers.new(name="UVMap")
material = bpy.data.materials.new("Tester")
material.use_nodes = True
shader = None
for node in material.node_tree.nodes:
    if node.type == 'BSDF_PRINCIPLED':
        shader = node
check("材质里有 Principled BSDF", shader is not None)
if shader is not None:
    shader.inputs["Base Color"].default_value = (1.0, 0.0, 0.0, 1.0)
    if "Roughness" in shader.inputs:
        shader.inputs["Roughness"].default_value = 0.25
mesh.materials.append(material)
obj = bpy.data.objects.new("Quad", mesh)
scene.collection.objects.link(obj)

# 4.x 的 socket 名在 3.6 上不存在，探测器能报出来是好事
for socket_name in ("Base Color", "Roughness", "Metallic", "Normal", "Coat Weight",
                    "Transmission Weight", "Emission Color", "Specular IOR Level"):
    if shader is not None:
        present = shader.inputs.get(socket_name) is not None
        print("    Principled.{}: {}".format(socket_name, "有" if present else "没有"))

# ------------------------------------------------------------------ 各类型真机烘焙
print("\n--- 真机烘焙（每种 32px，Cycles） ---")

PROBES = [
    ("shader.base_color", "节点手术：Base Color -> EMIT", True),
    ("shader.roughness", "节点手术：float 插口 -> EMIT", True),
    ("standard.normal", "标准 pass：NORMAL", True),
    ("standard.combined", "标准 pass：COMBINED", True),
    ("misc.uv", "节点手术：UVMap", False),
    ("misc.color_attribute", "节点手术：Color Attribute", False),
    ("misc.ao", "节点手术：Ambient Occlusion", False),
    ("misc.pointiness", "节点手术：Pointiness", False),
    ("misc.channel_packing", "节点手术：通道打包", False),
]

results = {}
for type_key, description, required in PROBES:
    bake_type = bt.get(type_key)
    if bake_type is None:
        probe_key = {"misc.uv": "standard.uv"}.get(type_key, type_key)
        bake_type = bt.get(probe_key)
    if bake_type is None:
        note("{} 这个版本的类型表里没有".format(type_key))
        continue
    try:
        settings = pl.BakeSettings(
            maps=pl.map_requests_from_pairs([(bake_type.key, 32)]),
            pack_into_blend=False, fake_user=False)
        plan = pl.build_plan(bpy.context, settings)
        job = jb.BakeJob(bpy.context, plan, settings,
                         backend=bk.CyclesBackend(txmod.SceneTransaction(bpy.context),
                                                  margin=2, samples=1))
        job.run_to_completion()
        ok = job.report.done == 1 and job.report.failed == 0
        results[bake_type.key] = ok
        extra = ""
        if not ok:
            extra = [r.error for r in job.report.failed_results()][:1]
        check("{}  {}".format(bake_type.key, description), ok, extra)
    except Exception as exc:
        results[bake_type.key] = False
        check("{}  {}".format(bake_type.key, description), False,
              "{}: {}".format(type(exc).__name__, exc))

# 核心那几项必须过
for required_key in ("shader.base_color", "shader.roughness", "standard.normal",
                     "standard.combined"):
    check("核心类型 {} 可用".format(required_key), results.get(required_key) is True)

# ------------------------------------------------------------------ 像素正确性
print("\n--- 像素正确性 ---")
settings = pl.BakeSettings(maps=pl.map_requests_from_pairs([("shader.base_color", 32)]),
                           pack_into_blend=False, fake_user=False)
plan = pl.build_plan(bpy.context, settings)
job = jb.BakeJob(bpy.context, plan, settings,
                 backend=bk.CyclesBackend(txmod.SceneTransaction(bpy.context),
                                          margin=2, samples=1))
job.run_to_completion()
image = plan.tasks[0].image
center = [round(v, 3) for v in list(image.pixels[4 * (32 * 16 + 16):][:4])]
print("    Base Color 中心像素:", center)
check("Base Color 烘出纯红", center[0] > 0.9 and center[1] < 0.1, center)

# ------------------------------------------------------------------ 导出/打包
print("\n--- 导出与图像存活 ---")
settings = pl.BakeSettings(maps=pl.map_requests_from_pairs([("shader.base_color", 32)]),
                           file_format='PNG')
plan = pl.build_plan(bpy.context, settings)
job = jb.BakeJob(bpy.context, plan, settings,
                 backend=bk.CyclesBackend(txmod.SceneTransaction(bpy.context),
                                          margin=2, samples=1,
                                          save_directory=OUT, save_files=True))
job.run_to_completion()
import glob
files = glob.glob(os.path.join(OUT, "**", "*.png"), recursive=True)
check("导出写盘成功", len(files) == 1, files)
if files:
    check("文件不是空壳", os.path.getsize(files[0]) > 100, os.path.getsize(files[0]))
    # 再写一次：能重复导出说明 pack 起了作用（见 design 文档 §11 的那个坑）
    try:
        image = plan.tasks[0].image
        image.save()
        check("同一张图能重复导出（pack 生效）", True)
    except Exception as exc:
        check("同一张图能重复导出（pack 生效）", False, str(exc).strip())

# ------------------------------------------------------------------ UI
print("\n--- UI ---")
try:
    from material_bakery.ui import panels, properties, ops as ui_ops
    check("panel 类存在", bool(panels.MBAKERY_PT_Wizard.bl_idname))
    check("向导设置可用", bpy.context.scene.mbakery is not None)
    result = ui_ops.validate_targets(bpy.context.scene.mbakery)
    check("校验函数能跑", isinstance(result, list), result)
except Exception as exc:
    check("UI 能加载", False, "{}: {}".format(type(exc).__name__, exc))

try:
    material_bakery.unregister()
    check("能干净注销", not hasattr(bpy.context.scene, "mbakery"))
    material_bakery.register()
    check("能重新注册", hasattr(bpy.context.scene, "mbakery"))
except Exception as exc:
    check("注销/重注册", False, "{}: {}".format(type(exc).__name__, exc))

# ------------------------------------------------------------------ 汇总
print("\n" + "=" * 70)
print("结论：Blender {}".format(bpy.app.version_string))
print("=" * 70)
usable = sorted(k for k, v in results.items() if v)
broken = sorted(k for k, v in results.items() if not v)
print("  可用类型 ({}): {}".format(len(usable), ", ".join(usable) or "无"))
print("  不可用   ({}): {}".format(len(broken), ", ".join(broken) or "无"))
for message in NOTES:
    print("  注意: {}".format(message))

if FAIL:
    print("\nFAILED {} check(s):".format(len(FAIL)))
    for name in FAIL:
        print("  -", name)
else:
    print("\nALL CHECKS PASSED")

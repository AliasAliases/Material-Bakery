"""量一量：烘焙开始前那段同步工作到底有多重。

⚠ 这个脚本只量**耗时**，内存那部分是按尺寸算的**名义值**。
   `resource` 模块在 Windows 上不存在，所以下面的"峰值内存"永远是 None。
   要看真实内存请跑 tools/measure_bake_memory.py —— 那边用
   K32GetProcessMemoryInfo 拿真读数，而且结论不一样：
   bpy.data.images.new() 是惰性的，建 96 张 2048² 图只涨 0.02 GB。

用户报告"烘焙过程卡住、界面没法操作"。怀疑对象是这两处**同步**做完全部工作的地方：
    1. session.start() -> BakeJob.start() 里的 UV 准备 + 共享 UV 打包
    2. 一次性把所有任务的图像都建出来（96 张 2048² 的耗时）

这个脚本只做测量，不改任何东西。
"""
import bpy, sys, os, time, shutil, tempfile

# 工作区 = 本文件所在目录的上一级。
# ⚠ 这里原来写死的是开发机上的绝对路径，clone 到别处每个套件都跑不起来，
#   而且公开版/私有版两份检出会互相 import 错代码。
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WS not in sys.path:
    sys.path.insert(0, WS)

import material_bakery
from material_bakery import compat
from material_bakery.core import plan as pl
from material_bakery.core import transaction as txmod
from material_bakery.engine import backend as bk
from material_bakery.engine import job as jb
from material_bakery.ui import ops as uops
from material_bakery.ui import session as usession

compat.set_silent(True)


def memory_mb():
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except ImportError:
        return None


print("=" * 72)
print("烘焙启动成本测量    Blender {}".format(bpy.app.version_string))
print("=" * 72)

# ------------------------------------------------------------------ 建一个"像用户的场景"
GROUPS = 32
OBJECTS_PER_GROUP = 4
SIZE = 2048
MAPS = 3

print("\n场景规模: {} 个集合 x {} 个物体，{} 张贴图/集合 @ {}px".format(
    GROUPS, OBJECTS_PER_GROUP, MAPS, SIZE))
print("          合计 {} 个烘焙任务".format(GROUPS * MAPS))

bpy.ops.wm.read_factory_settings(use_empty=True)
material_bakery.register()
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.samples = 1

t0 = time.time()
for g in range(GROUPS):
    collection = bpy.data.collections.new("Body{:02d}".format(g))
    scene.collection.children.link(collection)
    for i in range(OBJECTS_PER_GROUP):
        name = "Body{:02d}_Part{}".format(g, i)
        mesh = bpy.data.meshes.new(name + "Mesh")
        mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
                         [], [(0, 1, 2, 3)])
        mesh.update()
        layer = mesh.uv_layers.new(name="UVMap")
        for k, uv in enumerate(((0, 0), (1, 0), (1, 1), (0, 1))):
            layer.data[k].uv = uv
        mat = bpy.data.materials.new(name + "Mat")
        mat.use_nodes = True
        mesh.materials.append(mat)
        obj = bpy.data.objects.new(name, mesh)
        collection.objects.link(obj)
print("  建场景用了 {:.2f}s".format(time.time() - t0))

settings = scene.mbakery
settings.maps.clear()
for key in ("shader.base_color", "shader.roughness", "shader.normal"):
    uops.add_map(settings, key, SIZE, dedupe=True)
settings.target_mode = 'collections'
settings.prepare_uv = False          # 先不动 UV，单独测

bake_settings = usession.settings_to_bake_settings(settings)
plan = pl.build_plan(bpy.context, bake_settings)
print("  计划任务数:", len(plan.tasks))
print("  分组数:", len(plan.groups))

# ------------------------------------------------------------------ 1) 共享 UV 打包
print("\n--- 1) 共享 UV 打包（现在在 start() 里同步做完）---")
from material_bakery.engine import uv_pack
t0 = time.time()
packed_total = 0
for group in plan.groups:
    if len(group.objects) < 2:
        continue
    result = uv_pack.pack_shared_uv(group.objects)
    packed_total += len(result.packed)
elapsed = time.time() - t0
print("  打包 {} 个物体用了 {:.2f}s".format(packed_total, elapsed))
print("  -> 这在点下开始烘焙的那一瞬间同步发生，期间界面完全不响应")

# ------------------------------------------------------------------ 2) 一次性建图像
print("\n--- 2) 一次性建出所有图像（现在在 _step_prepare 里一口气做完）---")
from material_bakery.engine.backend import ensure_image

t0 = time.time()
total_bytes = 0
for index, task in enumerate(plan.tasks):
    image = ensure_image(task.image_name, task.size, task.bake_type.channel,
                         file_format='PNG', reuse=False)
    task.image = image
    per_image = task.size * task.size * 4 * (4 if image.is_float else 1)
    total_bytes += per_image
    if index in (0, 9, 49) or index == len(plan.tasks) - 1:
        print("    第 {:>3} 张: {:.2f}s 累计".format(index + 1, time.time() - t0))
elapsed = time.time() - t0
print("  建 {} 张图用了 {:.2f}s".format(len(plan.tasks), elapsed))
print("  ⚠ 下面是按尺寸算的**名义**像素内存，不是实测：")
print("     名义值约 {:.1f} GB".format(total_bytes / 1024.0 ** 3))
print("     真实读数要跑 tools/measure_bake_memory.py（实测建图只涨 0.02 GB）")

peak = memory_mb()
if peak:
    print("  进程峰值内存: {:.1f} GB".format(peak / 1024.0))
else:
    print("  （Windows 上没有 resource 模块，进程峰值内存拿不到）")

print("\n" + "=" * 72)
print("结论")
print("=" * 72)
print("  点下开始烘焙后，界面要等 {:.1f}s（建图）才行".format(elapsed))
print("  而且中途没有任何进度可看、按钮也点不动 —— 因为全在一个函数里同步跑")
print("  ⚠ 这个脚本只证明耗时；内存结论见 tools/measure_bake_memory.py")

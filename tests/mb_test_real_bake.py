"""Material Bakery — 阶段 2 真实烘焙测试（Cycles，无头）

对 test.blend 里的物体跑一次真正的 Cycles 烘焙，验证：
    - CyclesBackend 真的能出图（不是空图 / 全黑 / 全零）
    - 图像写盘、报告、引擎与烘焙设置还原
    - 材质里的临时烘焙节点被清干净
    - 重跑复用同一批图像，不产生 .001

只用 standard.* 类型 —— 它们是 Blender 自带 bake pass，不需要节点手术，
正好单独验证「烘焙链路」本身。shader.* 的节点手术在阶段 6 单独测。
"""
import bpy, sys, os, shutil, glob

# 工作区 = 本文件所在目录的上一级。
# ⚠ 这里原来写死的是开发机上的绝对路径，clone 到别处每个套件都跑不起来，
#   而且公开版/私有版两份检出会互相 import 错代码。
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WS)

import material_bakery
from material_bakery import compat
from material_bakery.core import scene_scan as ss
from material_bakery.core import plan as pl
from material_bakery.core import transaction as txmod
from material_bakery.engine import backend as bk
from material_bakery.engine import job as jb
from material_bakery.engine import report as rp

compat.set_silent(False)

FAIL = []
def check(label, ok, extra=""):
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", label, ("  " + str(extra)) if extra else ""))
    if not ok:
        FAIL.append(label)

material_bakery.register()

BLEND = os.path.join(WS, "test.blend")
OUT = os.path.join(WS, "_probe", "real_bake")
if os.path.isdir(OUT):
    shutil.rmtree(OUT)
os.makedirs(OUT)

print("=== 加载 test.blend ===")
bpy.ops.wm.open_mainfile(filepath=BLEND)
scene = bpy.context.scene
meshes = [o for o in scene.objects if o.type == 'MESH']
print("  网格物体:", [o.name for o in meshes])
print("  材质:", [m.name for m in bpy.data.materials])
check("场景里有 3 个网格", len(meshes) == 3, len(meshes))

groups, conflicts = ss.scan(scene)
print("  分组:", [(g.name, [o.name for o in g.objects]) for g in groups])
print("  冲突:", conflicts)
check("每个网格都能找到归属分组",
      len([o for g in groups for o in g.objects]) == len(meshes))
check("有材质的物体才能烘（测试素材都带材质）",
      all(o.material_slots for o in meshes))

# ------------------------------------------------------------------ 计划
settings = pl.BakeSettings(
    target_mode=ss.MODE_COLLECTIONS,
    maps=pl.map_requests_from_pairs([
        ("standard.normal", 128),
        ("standard.roughness", 128),
        ("standard.combined", 128),
    ]),
)
plan = pl.build_plan(bpy.context, settings)
print("  任务数:", len(plan.tasks))
for task in plan.tasks:
    print("    -", task.image_name, "->", [o.name for o in task.targets])
check("每个分组 3 张贴图", len(plan.tasks) == 3 * len(plan.groups),
      (len(plan.tasks), len(plan.groups)))
check("贴图名唯一", len({t.image_name for t in plan.tasks}) == len(plan.tasks))

# ------------------------------------------------------------------ 烘焙
original_engine = scene.render.engine
original_margin = scene.render.bake.margin
original_cycles_samples = scene.cycles.samples
node_counts = {m.name: len(m.node_tree.nodes) for m in bpy.data.materials if m.use_nodes}
print("  原始渲染引擎:", original_engine, " margin:", original_margin,
      " 采样:", original_cycles_samples)

tx = txmod.SceneTransaction(bpy.context)
backend = bk.CyclesBackend(tx, margin=4, samples=8, save_directory=OUT, save_files=True)
job = jb.BakeJob(bpy.context, plan, settings, backend=backend, transaction=tx)
job.run_to_completion()

print("\n=== 结果 ===")
for line in job.report.details():
    print("  " + line)
check("烘焙状态为 DONE", job.state == jb.STATE_DONE, job.state)
check("全部任务成功", job.report.failed == 0 and job.report.done == len(plan.tasks),
      (job.report.done, job.report.failed, len(plan.tasks)))

files = glob.glob(os.path.join(OUT, "**", "*.png"), recursive=True)
print("  输出文件:", [os.path.basename(f) for f in files])
check("每个任务都写了文件", len(files) == len(plan.tasks), len(files))
# 回归：pack() 会把 float 图像的 file_format 改成 OPEN_EXR，
# 早期实现据此决定扩展名，于是选了 PNG 却写出 .exr。
check("输出扩展名就是设置里选的 PNG",
      all(f.endswith(".png") for f in files),
      [os.path.basename(f) for f in files])
check("没有多出 .exr 文件",
      not glob.glob(os.path.join(OUT, "**", "*.exr"), recursive=True),
      glob.glob(os.path.join(OUT, "**", "*.exr"), recursive=True))
check("文件不是空壳",
      all(os.path.getsize(f) > 200 for f in files),
      [(os.path.basename(f), os.path.getsize(f)) for f in files])


# ------------------------------------------------------------------ 图像内容真的是烘出来的
def image_stats(image):
    """返回 (最小, 最大, 不同值的数量抽样)"""
    width, height = image.size
    step = max(1, (width * height) // 4096)
    flat = []
    pixels = list(image.pixels)
    for i in range(0, width * height, step):
        flat.extend(pixels[i * 4:i * 4 + 4])
    return min(flat), max(flat), len(set(round(v, 4) for v in flat))


for task in plan.tasks:
    image = task.image
    check("任务 {} 有图像".format(task.image_name), image is not None)
    low, high, distinct = image_stats(image)
    print("    {} -> size={} min={:.4f} max={:.4f} distinct={}".format(
        task.image_name, image.size[:], low, high, distinct))
    check("  {} 不是全零空图".format(task.bake_type.label), high > 0.001,
          (low, high))
    if task.bake_type.key == "standard.normal":
        check("  Normal 图有颜色变化（真的烘出了法线）", distinct > 1, distinct)
    if task.bake_type.key == "standard.combined":
        check("  Combined 图有内容", high > 0.01, high)

# ------------------------------------------------------------------ 还原
check("渲染引擎已还原", scene.render.engine == original_engine, scene.render.engine)
check("烘焙 margin 已还原", scene.render.bake.margin == original_margin,
      scene.render.bake.margin)
check("Cycles 采样已还原", scene.cycles.samples == original_cycles_samples,
      scene.cycles.samples)
now_counts = {m.name: len(m.node_tree.nodes) for m in bpy.data.materials if m.use_nodes}
check("材质节点数没变（临时烘焙节点已清掉）", now_counts == node_counts,
      (node_counts, now_counts))
check("烘焙目标节点没留在材质里",
      all("MBakery Bake" not in [n.label for n in m.node_tree.nodes]
          for m in bpy.data.materials if m.use_nodes))

# ------------------------------------------------------------------ 重跑
before_images = len(bpy.data.images)
plan2 = pl.build_plan(bpy.context, settings)
job2 = jb.BakeJob(bpy.context, plan2, settings,
                  backend=bk.CyclesBackend(txmod.SceneTransaction(bpy.context),
                                           margin=4, samples=8,
                                           save_directory=OUT, save_files=False))
job2.run_to_completion()
check("重跑同样成功", job2.report.done == len(plan2.tasks), job2.report.done)
check("重跑复用图像（没有 .001）",
      len(bpy.data.images) == before_images
      and not any(".00" in t.image.name for t in plan2.tasks),
      [t.image.name for t in plan2.tasks])
after_files = glob.glob(os.path.join(OUT, "**", "*.png"), recursive=True)
check("重跑覆盖同名文件而不是新增", len(after_files) == len(files), len(after_files))

print("\n=== 结果 ===")
if FAIL:
    print("FAILED {} check(s):".format(len(FAIL)))
    for name in FAIL:
        print("  -", name)
else:
    print("ALL CHECKS PASSED")

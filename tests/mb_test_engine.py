"""Material Bakery — 阶段 2 测试：场景扫描 / 计划编译 / 事务 / 引擎状态机
用 NullBackend，不需要 Cycles，也不写任何文件到工作区。
"""
import bpy, sys, os, shutil, tempfile

# 工作区 = 本文件所在目录的上一级。
# ⚠ 这里原来写死的是开发机上的绝对路径，clone 到别处每个套件都跑不起来，
#   而且公开版/私有版两份检出会互相 import 错代码。
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WS)

import material_bakery
from material_bakery import compat
from material_bakery.core import bake_types as bt
from material_bakery.core import naming as nm
from material_bakery.core import scene_scan as ss
from material_bakery.core import plan as pl
from material_bakery.core import transaction as txmod
from material_bakery.engine import backend as bk
from material_bakery.engine import job as jb
from material_bakery.engine import report as rp
from material_bakery.engine import uv_overlap as uo
from material_bakery.engine import uv_prep

compat.set_silent(True)

FAIL = []
def check(label, ok, extra=""):
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", label, ("  " + str(extra)) if extra else ""))
    if not ok:
        FAIL.append(label)

material_bakery.register()

OUT = os.path.join(tempfile.gettempdir(), "mb_engine_test")
if not os.path.isdir(OUT):
    os.makedirs(OUT)


# ------------------------------------------------------------------ 场景搭建
def wipe():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def make_mesh(name, size=1.0, uv=True):
    mesh = bpy.data.meshes.new(name + "Mesh")
    verts = [(-size, -size, -size), (size, -size, -size), (size, size, -size), (-size, size, -size),
             (-size, -size, size), (size, -size, size), (size, size, size), (-size, size, size)]
    faces = [(0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    if uv:
        mesh.uv_layers.new(name="UVMap")
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def give_material(obj, name):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    obj.data.materials.append(mat)
    return mat


print("=== 阶段 2a：场景扫描 ===")
wipe()
a = make_mesh("Alpha")
b = make_mesh("Beta")
c = make_mesh("Gamma", uv=False)
# ⚠ 这些物体必须**有材质**：没有材质的物体会在计划阶段被剔掉
#   （用户报过一次"一个没材质的螺丝钉让整个集合 4 张贴图全废"，
#     Blender 的原话是 No active image found, add a material or ...）。
#   假场景如果都是裸网格，测的就不是真实场景了。
give_material(a, "AlphaMat")
give_material(b, "BetaMat")
give_material(c, "GammaMat")

col = bpy.data.collections.new("Body")
bpy.context.scene.collection.children.link(col)
for obj in (a, b):
    bpy.context.scene.collection.objects.unlink(obj)
    col.objects.link(obj)

groups, _scan_conflicts = ss.scan(bpy.context.scene)
names = [g.name for g in groups]
print("  扫到的分组:", names)
check("集合被识别为分组", "Body" in names, names)
check("主集合里的物体归到自己的分组",
      any(g.name == "Gamma" for g in groups), names)

body = [g for g in groups if g.name == "Body"][0]
check("分组里物体齐全", sorted(o.name for o in body.objects) == ["Alpha", "Beta"],
      [o.name for o in body.objects])

check("可见网格被判为可烘（UV 是后续步骤的事）", ss.is_bakeable(c))
cam_data = bpy.data.cameras.new("CamData")
cam = bpy.data.objects.new("Cam", cam_data)
bpy.context.scene.collection.objects.link(cam)
check("非网格物体不可烘", not ss.is_bakeable(cam))
hidden = make_mesh("Hidden")
hidden.hide_viewport = True
check("隐藏物体不可烘", not ss.is_bakeable(hidden))
hidden.hide_viewport = False
missing = ss.missing_uv_objects([a, b, c])
check("缺 UV 检测准确", [o.name for o in missing] == ["Gamma"], [o.name for o in missing])

# 冲突：一个物体同时在两个集合里
d = make_mesh("Delta")
give_material(d, "DeltaMat")
col2 = bpy.data.collections.new("Arm")
bpy.context.scene.collection.children.link(col2)
col2.objects.link(d)
groups2, conflicts = ss.scan(bpy.context.scene)
print("  冲突:", conflicts)
check("跨集合物体被记为冲突", len(conflicts) == 1, conflicts)

# 选中模式
for obj in bpy.context.view_layer.objects:
    obj.select_set(False)
bpy.data.objects["Alpha"].select_set(True)
sel = ss.resolve_targets(bpy.context, ss.MODE_SELECTED)[0]
check("selected 模式只取选中的", [g.name for g in sel] == ["Alpha"], [g.name for g in sel])

# 单集合模式
one = ss.resolve_targets(bpy.context, ss.MODE_SINGLE, "Body")[0]
check("single 模式只取指定集合", [g.name for g in one] == ["Body"], [g.name for g in one])

check("物体集合名查询正确", ss.object_collection_name(a, bpy.context.scene) == "Body",
      ss.object_collection_name(a, bpy.context.scene))


print("\n=== 阶段 2b：计划编译 ===")
settings = pl.BakeSettings(
    target_mode=ss.MODE_COLLECTIONS,
    maps=pl.map_requests_from_pairs([("shader.base_color", 2048), ("shader.normal", 2048)]),
)
plan = pl.build_plan(bpy.context, settings)
print("  任务数:", len(plan.tasks))
print("  预览:", plan.name_preview[:4])
check("每个分组每个类型一个任务",
      all(t.group_name != "Body" or len([x for x in plan.tasks if x.group_name == "Body"]) == 2
          for t in plan.tasks))
check("Body 的任务名用集合名",
      any(t.image_name.startswith("Body") for t in plan.tasks),
      [t.image_name for t in plan.tasks])
check("缺 UV 的物体被剔除并警告", any("no UV map" in w for w in plan.warnings), plan.warnings)
check("Gamma 组被整组跳过", any(g == "Gamma" for g, _ in plan.skipped), plan.skipped)

body_tasks = [t for t in plan.tasks if t.group_name == "Body"]
sample = body_tasks[0].image_name
# 新的默认命名方案：`Body-BaseColor-2k`
# （原来是 "{prefix}{type}{bridge}{size}{suffix}" + suffix="px"，
#   拼出 "BodyBaseColor - 2048px" —— 用户嫌又长又挤，指定要 BaseColor-2k 这种写法）
check("命名是 前缀-类型-2k（不带空格、小写 k）",
      " - " not in sample and sample.endswith("k") and "px" not in sample
      and "-" in sample,
      sample)
check("sources 与 targets 都是独立列表",
      body_tasks[0].targets is not body_tasks[0].sources
      and [o.name for o in body_tasks[0].targets] == [o.name for o in body_tasks[0].sources])

# 手工前缀优先
settings2 = pl.BakeSettings(prefix="Mine",
                            maps=pl.map_requests_from_pairs([("shader.base_color", 1024)]))
plan2 = pl.build_plan(bpy.context, settings2)
check("手动前缀优先于集合名",
      all(t.image_name.startswith("Mine") for t in plan2.tasks),
      [t.image_name for t in plan2.tasks])

# 空前缀
settings3 = pl.BakeSettings(prefix='""',
                            maps=pl.map_requests_from_pairs([("shader.base_color", 1024)]))
plan3 = pl.build_plan(bpy.context, settings3)
check("空前缀不产生前缀",
      all(not t.image_name.startswith("Body") for t in plan3.tasks),
      [t.image_name for t in plan3.tasks])

# Prepare UV 打开时不剔除
settings4 = pl.BakeSettings(prepare_uv=True,
                            maps=pl.map_requests_from_pairs([("shader.base_color", 512)]))
plan4 = pl.build_plan(bpy.context, settings4)
check("Prepare UV 打开时缺 UV 物体保留",
      not any(g == "Gamma" for g, _ in plan4.skipped), plan4.skipped)

try:
    pl.map_requests_from_pairs([("no.such.type", 512)])
    check("未知类型抛错", False)
except KeyError:
    check("未知类型抛错", True)

check("重复 (类型,尺寸) 去重",
      len(pl.map_requests_from_pairs([("shader.base_color", 512),
                                      ("shader.base_color", 512)])) == 1)


print("\n=== 阶段 2c：事务还原 ===")
wipe()
obj = make_mesh("T")
mat = give_material(obj, "TMat")
scene = bpy.context.scene
original_engine = scene.render.engine
original_margin = scene.render.bake.margin
obj.select_set(True)
bpy.context.view_layer.objects.active = obj

tx = txmod.SceneTransaction(bpy.context)
tx.capture()
base_nodes = len(mat.node_tree.nodes)
tx.set_bake_setting("margin", 99)
tx.install_bake_target(mat, bpy.data.images.new("TmpImg", 8, 8))
check("烘焙目标节点已挂上", len(mat.node_tree.nodes) == base_nodes + 1,
      len(mat.node_tree.nodes))
tx.rollback()
check("回滚后节点被移除", len(mat.node_tree.nodes) == base_nodes,
      len(mat.node_tree.nodes))
check("回滚后烘焙设置还原", scene.render.bake.margin == original_margin,
      scene.render.bake.margin)
check("回滚后引擎还原", scene.render.engine == original_engine)

# 取消后同样还原
tx2 = txmod.SceneTransaction(bpy.context)
tx2.capture()
tx2.set_bake_setting("margin", 77)
tx2.rollback()
check("第二次事务同样还原", scene.render.bake.margin == original_margin)

# 复用同一材质时只挂一个节点
tx3 = txmod.SceneTransaction(bpy.context)
tx3.capture()
img1 = bpy.data.images.new("I1", 8, 8)
img2 = bpy.data.images.new("I2", 8, 8)
before = len(mat.node_tree.nodes)
tx3.install_bake_target(mat, img1)
tx3.install_bake_target(mat, img2)
after = len(mat.node_tree.nodes)
check("同一材质只挂一个烘焙节点", after == before + 1, after - before)
active = mat.node_tree.nodes.active
check("第二个任务换了图像而不是加节点", active.image is img2, active.image)
# 用户自己的图像节点不能被误删
user_node = mat.node_tree.nodes.new('ShaderNodeTexImage')
user_node.image = bpy.data.images.new("UserImg", 8, 8)
tx3.rollback()
check("用户自己的节点没被删", mat.node_tree.nodes.get(user_node.name) is not None)


print("\n=== 阶段 2d：UV 重叠预检 ===")
wipe()
flat = make_mesh("Flat")
# 六个面全部用默认 UV（Blender 新建 UV 层是全零），必然重叠
stats = uo.scan_object(flat, 64)
check("重叠能被检出", stats is not None and stats.overlap > 0, stats)
check("重叠比例在 0..1", 0.0 <= stats.overlap_ratio <= 1.0, stats.overlap_ratio)
check("重叠比例不是 NaN", stats.overlap_ratio == stats.overlap_ratio)
check("全零 UV 完全重叠", stats.overlap == stats.covered, (stats.overlap, stats.covered))

# 单个四边形，UV 正好铺满 0..1 —— 应该零重叠
quad_mesh = bpy.data.meshes.new("QuadMesh")
quad_mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
quad_mesh.update()
uvl = quad_mesh.uv_layers.new(name="UVMap")
for i, uv in enumerate(((0, 0), (1, 0), (1, 1), (0, 1))):
    uvl.data[i].uv = uv
quad = bpy.data.objects.new("Quad", quad_mesh)
bpy.context.scene.collection.objects.link(quad)
quad_stats = uo.scan_object(quad, 64)
print("  四边形:", quad_stats)
check("展开完整的 UV 零重叠", quad_stats.overlap == 0, quad_stats)
check("UV 铺满时覆盖率接近 1", quad_stats.covered_ratio > 0.95, quad_stats.covered_ratio)

# 没有 UV 的物体返回 None
bare = make_mesh("Bare", uv=False)
check("没有 UV 的物体返回 None", uo.scan_object(bare, 64) is None)
cam2 = bpy.data.objects.new("Cam2", bpy.data.cameras.new("Cam2Data"))
bpy.context.scene.collection.objects.link(cam2)
check("非网格返回 None", uo.scan_object(cam2, 64) is None)
allstats = uo.scan_objects([quad, flat], 64)
check("批量扫描返回两个结果", len(allstats) == 2, len(allstats))
check("worst 选出重叠最严重的",
      uo.worst([quad, flat], 64).object_name == "Flat", uo.worst([quad, flat], 64))
check("零重叠时 worst 返回 None", uo.worst([quad], 64) is None)

check("没有 UV 的物体被 ensure_uvs 认出",
      [o.name for o in uv_prep.objects_missing_uv([bare])] == ["Bare"])
check("有 UV 的物体不被算作缺 UV",
      uv_prep.objects_missing_uv([quad]) == [])


print("\n=== 阶段 2e：引擎状态机（NullBackend） ===")
wipe()
for name in ("One", "Two"):
    o = make_mesh(name)
    give_material(o, name + "Mat")

jobs = os.path.join(OUT, "jobs")
# ⚠ 每次跑之前先清空：这个目录是按名字累加的，命名规则一改，
#   上一轮留下的旧文件名还在里面，于是"每个任务都导出了文件"这种
#   数文件的断言会拿两次运行的结果一起算（这次就是这么挂的：
#   目录里同时有 `One - BaseColor - 256.png` 和 `One-BaseColor-256.png`）。
if os.path.isdir(jobs):
    shutil.rmtree(jobs, ignore_errors=True)
os.makedirs(jobs, exist_ok=True)
settings5 = pl.BakeSettings(
    maps=pl.map_requests_from_pairs([("shader.base_color", 256), ("shader.roughness", 256)]))
plan5 = pl.build_plan(bpy.context, settings5)
check("计划里有 4 个任务", len(plan5.tasks) == 4, len(plan5.tasks))

frames = []
def tick(state):
    frames.append(state)

backend = bk.NullBackend(save=True, directory=jobs)
tx5 = txmod.SceneTransaction(bpy.context)
job = jb.BakeJob(bpy.context, plan5, settings5, backend=backend, transaction=tx5)
job.start()
# ⚠ 现在**没有独立的"准备阶段"**：准备并进每个任务自己的步骤里
#   （建这一张图 → 立刻派发它），这是跟原版学的交错做法。
check("start 后直接进烘焙阶段", job.state == jb.STATE_BAKING, job.state)
check("start 之后一张图都还没建（准备工作跟着任务走）",
      job.prepare_index == 0, job.prepare_index)
steps = 0
while not job.is_finished and steps < 200:
    job.step()
    tick(job.state)
    steps += 1
print("  步数:", steps, " 状态:", job.state, " 报告:", job.report.summary())
check("跑完是 DONE", job.state == jb.STATE_DONE, job.state)
check("4 个任务全部完成", job.report.done == 4, job.report.done)
check("报告没有失败", job.report.failed == 0, job.report.failed)
check("is_finished 为真", job.is_finished)
# 进度把"准备"也算进总工作量（准备与烘焙交错）。4 个任务 -> 8
check("进度满格", job.progress[0] == 8 and job.progress[2] == 1.0, job.progress)
check("一步一个任务（4 个任务 + 收尾）", steps >= 5, steps)
check("进度是逐步涨的", len(set(frames)) > 1, set(frames))
check("准备跟着任务走：4 个任务准备了 4 次",
      job.prepare_index == 4, job.prepare_index)

images = [t.image.name for t in plan5.tasks]
print("  图像:", images)
check("每个任务都拿到了图像数据块", all(t.image is not None for t in plan5.tasks))
check("图像尺寸正确", all(t.image.size[0] == 256 for t in plan5.tasks))
check("数据贴图是 Non-Color",
      all(t.image.colorspace_settings.name == "Non-Color"
          for t in plan5.tasks if t.bake_type.channel.is_data))
check("颜色贴图是 sRGB",
      all(t.image.colorspace_settings.name == "sRGB"
          for t in plan5.tasks if not t.bake_type.channel.is_data))

import glob
saved = glob.glob(os.path.join(jobs, "**", "*.png"), recursive=True)
check("每个任务都导出了文件", len(saved) == 4, saved)
check("报告记录了文件路径", all(r.filepath for r in job.report.results))
check("报告耗时为正", job.report.seconds >= 0)
check("图像节点已从材质里清干净",
      all(len(m.node_tree.nodes) == 2 for m in bpy.data.materials),
      [len(m.node_tree.nodes) for m in bpy.data.materials])

# 序列化往返
text = job.report.to_json()
back = rp.RunReport.from_json(text)
check("报告 JSON 往返一致", len(back.results) == 4 and back.done == 4, back.summary())
check("报告摘要可读", "4 of 4" in back.summary(), back.summary())

# 同一批再跑一次：图像复用，不产生重复数据块
before_images = len(bpy.data.images)
plan6 = pl.build_plan(bpy.context, settings5)
job6 = jb.BakeJob(bpy.context, plan6, settings5, backend=bk.NullBackend(save=False))
job6.run_to_completion()
names6 = [t.image.name for t in plan6.tasks]
check("重跑复用同名图像", len(bpy.data.images) == before_images,
      "{} -> {}".format(before_images, len(bpy.data.images)))
check("重跑不产生 .001 后缀", not any(n.endswith(".001") for n in names6), names6)
check("重跑同样成功", job6.report.done == 4, job6.report.done)


print("\n=== 阶段 2f：失败与取消 ===")
plan7 = pl.build_plan(bpy.context, settings5)
failing = plan7.tasks[1].key
backend7 = bk.NullBackend(fail_on=[failing])
job7 = jb.BakeJob(bpy.context, plan7, settings5, backend=backend7)
job7.run_to_completion()
print("  报告:", job7.report.summary())
check("单个任务失败不终止整批", job7.state == jb.STATE_DONE, job7.state)
check("失败数正确", job7.report.failed == 1, job7.report.failed)
check("其余任务仍然完成", job7.report.done == 3, job7.report.done)
check("失败项带错误信息", all(r.error for r in job7.report.failed_results()),
      [r.error for r in job7.report.failed_results()])
check("失败项在明细里置顶", "FAILED" in job7.report.details()[1], job7.report.details()[:3])

# 取消：跑到一半叫停
scene = bpy.context.scene
original_margin = scene.render.bake.margin
plan8 = pl.build_plan(bpy.context, settings5)
tx8 = txmod.SceneTransaction(bpy.context)
job8 = jb.BakeJob(bpy.context, plan8, settings5, transaction=tx8)
job8.start()
# ⚠ 一步 = 准备**这一个**任务 + 立刻派发它（交错）。
#   关键是"一次 tick 只碰一个任务"：原来一个 for 循环把 96 张图全建出来
#   （实测 1.7s、4.5GB），界面会整个卡死、暂停和取消都点不到。
check("第一步就准备并派发了第一个任务",
      job8.step() == jb.STATE_BAKING, job8.state)
check("只准备了一个任务（不是一口气全建完）",
      job8.prepare_index == 1, job8.prepare_index)
job8.step()                                  # 收第一张 + 准备并派发第二张
check("第二步推进到第二个任务", job8.prepare_index == 2, job8.prepare_index)
tx8.set_bake_setting("margin", 99)           # 模拟后端对设置的临时改动
job8.cancel()
job8.step()
print("  取消后状态:", job8.state, " 报告:", job8.report.summary())
check("取消后状态为 CANCELLED", job8.state == jb.STATE_CANCELLED, job8.state)
check("取消后 is_finished 为真", job8.is_finished)
check("报告标记为已取消", job8.report.cancelled)
check("取消后烘焙设置还原", scene.render.bake.margin == original_margin,
      scene.render.bake.margin)
check("取消后没有残留图像节点",
      all(len(m.node_tree.nodes) == 2 for m in bpy.data.materials),
      [len(m.node_tree.nodes) for m in bpy.data.materials])
check("取消后不再推进", job8.step() == jb.STATE_CANCELLED)
check("取消的任务被记账", job8.report.count(rp.STATUS_CANCELLED) <= 1,
      job8.report.count(rp.STATUS_CANCELLED))

# 空计划
empty_settings = pl.BakeSettings(maps=())
plan9 = pl.build_plan(bpy.context, empty_settings)
job9 = jb.BakeJob(bpy.context, plan9, empty_settings)
job9.run_to_completion()
check("空计划直接完成", job9.state == jb.STATE_DONE, job9.state)
check("空计划报告为空", job9.report.total == 0)
check("空计划 is_empty", plan9.is_empty)

# ETA
check("ETA 在无样本时返回 None 或数字", job.eta() is None or job.eta() >= 0)


print("\n=== 阶段 2g：多集合一次任务量 ===")
wipe()
for i in range(4):
    coll = bpy.data.collections.new("Group{}".format(i))
    bpy.context.scene.collection.children.link(coll)
    for j in range(3):
        o = make_mesh("G{}_{}".format(i, j))
        give_material(o, "G{}_{}Mat".format(i, j))
        bpy.context.scene.collection.objects.unlink(o)
        coll.objects.link(o)

big = pl.BakeSettings(
    maps=pl.map_requests_from_pairs([("shader.base_color", 64), ("shader.normal", 64),
                                     ("shader.roughness", 64)]))
big_plan = pl.build_plan(bpy.context, big)
check("4 集合 × 3 贴图 = 12 个任务", len(big_plan.tasks) == 12, len(big_plan.tasks))
check("每集合一套贴图（名字唯一）",
      len({t.image_name for t in big_plan.tasks}) == 12,
      len({t.image_name for t in big_plan.tasks}))
check("每组只烘一张同类型贴图",
      all(len([t for t in big_plan.tasks
               if t.group_name == g.name and t.bake_type.key == "shader.base_color"]) == 1
          for g in big_plan.groups))

big_job = jb.BakeJob(bpy.context, big_plan, big, backend=bk.NullBackend())
big_job.run_to_completion()
check("12 个任务一次跑完", big_job.report.done == 12, big_job.report.done)
check("报告里 4 个集合", len(big_job.report.group_names()) == 4, big_job.report.group_names())


print("\n=== 结果 ===")
if FAIL:
    print("FAILED {} check(s):".format(len(FAIL)))
    for name in FAIL:
        print("  -", name)
else:
    print("ALL CHECKS PASSED")

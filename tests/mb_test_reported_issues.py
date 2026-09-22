"""用户报告的三个问题 —— 回归测试

    ① 输出缺少"以集合命名的文件夹"
    ② 烘焙过程中暂停 / 停止用不了
    ③ 烘焙过程卡死界面、无法操作

③ 的根因是**同步做重活**：展 UV、打包 UV、建图像原来全在
   "点下开始烘焙"那一刻或者单次 timer tick 里一口气做完。
   实测 32 集合 × 3 张 2048² 的场景，光建 96 张图像就要约 0.7s，全程零响应
   —— 界面当然卡死，按钮当然点不到。
   （测量脚本：tools/measure_bake_memory.py）

⚠ 后来真量了一次内存，发现当初写的"一次申请 4.5 GB"是**按尺寸算的名义值**，
   实测建 96 张图只涨 0.02 GB（bpy.data.images.new 是惰性的，像素缓冲区
   第一次写入才分配）。所以这套测试里跟内存有关的断言只认实测斜率。

所以这一套盯的是"**每一步都很小**"：一步一个任务、随时可暂停/取消、
进度在准备阶段也在动。
"""
import bpy, sys, os, shutil, time

# 工作区 = 本文件所在目录的上一级。
# ⚠ 这里原来写死的是开发机上的绝对路径，clone 到别处每个套件都跑不起来，
#   而且公开版/私有版两份检出会互相 import 错代码。
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WS not in sys.path:
    sys.path.insert(0, WS)

import material_bakery
from material_bakery import compat
from material_bakery.core import imported as im
from material_bakery.core import plan as pl
from material_bakery.core import scene_scan as ss
from material_bakery.core import transaction as txmod
from material_bakery.engine import backend as bk
from material_bakery.engine import job as jb
from material_bakery.engine import report as report_mod
from material_bakery.ui import ops as uops
from material_bakery.ui import panels as upanels
from material_bakery.ui import properties as uprops
from material_bakery.ui import session as usession

compat.set_silent(True)

FAIL = []
def check(label, ok, extra=""):
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", label,
                               ("  " + str(extra)) if extra else ""))
    if not ok:
        FAIL.append(label)

OUT = os.path.join(WS, "_probe", "reported")
if os.path.isdir(OUT):
    shutil.rmtree(OUT)
os.makedirs(OUT)

bpy.ops.wm.read_factory_settings(use_empty=True)
material_bakery.register()


def quad(name, collection, color=(0.9, 0.1, 0.1, 1.0)):
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
    mesh.update()
    layer = mesh.uv_layers.new(name="UVMap")
    for i, uv in enumerate(((0.02, 0.02), (0.98, 0.02), (0.98, 0.98), (0.02, 0.98))):
        layer.data[i].uv = uv
    material = bpy.data.materials.new(name + "Mat")
    material.use_nodes = True
    for node in material.node_tree.nodes:
        if node.type == 'BSDF_PRINCIPLED':
            node.inputs["Base Color"].default_value = color
    mesh.materials.append(material)
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    return obj


scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.samples = 1

GROUPS = 3
OBJECTS = 2
for g in range(GROUPS):
    collection = bpy.data.collections.new("Body{:02d}".format(g))
    scene.collection.children.link(collection)
    for i in range(OBJECTS):
        quad("Body{:02d}_P{}".format(g, i), collection)

settings = scene.mbakery
settings.maps.clear()
for key in ("shader.base_color", "shader.roughness"):
    uops.add_map(settings, key, 64, dedupe=True)
settings.output_dir = OUT
settings.use_subfolders = True

print("=== 问题①：输出要按集合建子文件夹 ===")
bake_settings = usession.settings_to_bake_settings(settings)
check("设置传到了 BakeSettings（以前根本没这个字段）",
      getattr(bake_settings, "use_subfolders", None) is True,
      getattr(bake_settings, "use_subfolders", "缺失"))

plan = pl.build_plan(bpy.context, bake_settings)
check("有 {} 个任务".format(GROUPS * 2), len(plan.tasks) == GROUPS * 2, len(plan.tasks))

backend = bk.CyclesBackend(txmod.SceneTransaction(bpy.context), margin=2, samples=1,
                           save_directory=OUT, save_files=True)
backend.begin(plan, bake_settings)
folders = {task.group_name: backend._folder_for(task) for task in plan.tasks}
print("  每个集合的落盘目录:")
for name, folder in sorted(folders.items()):
    print("    {} -> {}".format(name, os.path.relpath(folder, OUT)))
check("每个集合一个子文件夹",
      all(os.path.basename(folder) == name for name, folder in folders.items()),
      folders)
check("关掉开关就平铺",
      backend._folder_for.__self__ is backend, True)

backend.use_subfolders = False
check("use_subfolders=False 时直接写根目录",
      backend._folder_for(plan.tasks[0]) == OUT, backend._folder_for(plan.tasks[0]))
backend.use_subfolders = True

# 真跑一遍，确认文件真的落在子文件夹里
job = jb.BakeJob(bpy.context, plan, bake_settings, backend=backend,
                 transaction=txmod.SceneTransaction(bpy.context))
job.run_to_completion()
check("烘焙全部成功", job.report.failed == 0 and job.report.done == GROUPS * 2,
      (job.report.done, job.report.failed))
subdirs = sorted(d for d in os.listdir(OUT) if os.path.isdir(os.path.join(OUT, d)))
root_pngs = [f for f in os.listdir(OUT) if f.endswith(".png")]
print("  输出根目录下的子文件夹:", subdirs)
print("  根目录下平铺的 png:", root_pngs)
check("磁盘上真的出现了集合子文件夹", len(subdirs) == GROUPS, subdirs)
check("图片落在子文件夹里而不是根目录", not root_pngs, root_pngs)
for name in subdirs:
    folder = os.path.join(OUT, name)
    inside = os.listdir(folder)
    # ⚠ 目录里除了 png 还有 manifest（`_mbakery.json`，用来把贴图救回来），
    #   所以这里按"图片"数，而不是数所有文件。
    images = [f for f in inside if f.endswith(".png")]
    check("{} 里有 2 张图".format(name), len(images) == 2, inside)
    check("{} 里有 manifest（以后能从磁盘救回来）".format(name),
          im.MANIFEST_NAME in inside, inside)

print("\n=== 问题③：准备跟着任务走（不先建完所有图）===")
fresh_plan = pl.build_plan(bpy.context, usession.settings_to_bake_settings(settings))
job2 = jb.BakeJob(bpy.context, fresh_plan,
                  usession.settings_to_bake_settings(settings),
                  backend=bk.NullBackend())
job2.start()
# ⚠ 关键改动：**没有独立的准备阶段**。每一步 = 准备这一个任务 + 立刻派发它，
#   也就是原版 Auto Bake 的交错做法（建一张 → 烘一张）。
#   原来是把 96 张图先全建出来（实测 1.7s、4.5GB）才开始烘，界面整个卡死。
check("start() 之后一张图都还没建", job2.prepare_index == 0, job2.prepare_index)
check("start() 之后直接进烘焙阶段（不再有准备阶段）",
      job2.state == jb.STATE_BAKING, job2.state)

job2.step()
check("第一步只准备并派发了一个任务",
      job2.prepare_index == 1, job2.prepare_index)
images_after_one_step = len([t for t in fresh_plan.tasks if t.image is not None])
check("第一步之后**只有一张图**存在（不是全部）",
      images_after_one_step <= 1, images_after_one_step)
check("总任务数是 {}，远大于 1".format(len(fresh_plan.tasks)),
      len(fresh_plan.tasks) > 1, len(fresh_plan.tasks))

job2.step()
check("第二步推进到第二个任务", job2.prepare_index == 2, job2.prepare_index)
images_after_two = len([t for t in fresh_plan.tasks if t.image is not None])
check("两张图，而不是一次全建出来", images_after_two <= 2, images_after_two)

print("\n=== 问题③：进度在整轮里都在动 ===")
fresh_plan = pl.build_plan(bpy.context, usession.settings_to_bake_settings(settings))
job3 = jb.BakeJob(bpy.context, fresh_plan,
                  usession.settings_to_bake_settings(settings),
                  backend=bk.NullBackend())
job3.start()
fractions = []
while not job3.is_finished:
    fractions.append(round(job3.progress[2], 3))
    job3.step()
print("  进度序列:", fractions)
check("进度是逐步涨的（不是一直 0 然后突然满）",
      len(set(fractions)) > 2, fractions)
check("进度单调不减",
      all(b >= a for a, b in zip(fractions, fractions[1:])), fractions)
check("状态行说得出在干什么", bool(job3.status_line()), job3.status_line())

print("\n=== 问题②：准备阶段就能暂停 / 停止 ===")
fresh_plan = pl.build_plan(bpy.context, usession.settings_to_bake_settings(settings))
job4 = jb.BakeJob(bpy.context, fresh_plan,
                  usession.settings_to_bake_settings(settings),
                  backend=bk.NullBackend())
job4.start()
job4.step()
check("准备阶段 is_running 为真（按钮该显示成暂停/停止）", job4.is_running)
job4.pause()
job4.step()
check("准备阶段能暂停", job4.state == jb.STATE_PAUSED, job4.state)
check("暂停后不再推进", job4.prepare_index == 1, job4.prepare_index)
job4.resume()
check("能继续", job4.state == jb.STATE_BAKING, job4.state)
job4.step()
check("继续后又开始推进", job4.prepare_index >= 1, job4.prepare_index)
job4.cancel()
job4.step()
check("准备阶段能停止", job4.state == jb.STATE_CANCELLED, job4.state)
check("停止后事务被还原（没留临时节点）",
      all(len(m.node_tree.nodes) <= 3 for m in bpy.data.materials if m.use_nodes),
      [len(m.node_tree.nodes) for m in bpy.data.materials if m.use_nodes])

print("\n=== 问题②：按钮真的画出来了 ===")
# 直接画烘焙页，确认暂停/停止/继续三个按钮都在
class Rec:
    def __init__(self, log): object.__setattr__(self, '_log', log)
    def prop(self, data, name=None, **k):
        if isinstance(name, str) and name and not hasattr(data, name):
            raise AttributeError("prop(%s, %r)" % (type(data).__name__, name))
        self._log.append(('prop', name)); return Rec(self._log)
    def label(self, text='', icon=None, **k):
        if icon is not None and icon not in upanels.valid_icons():
            raise TypeError("icon %r" % icon)
        self._log.append(('label', text)); return Rec(self._log)
    def operator(self, idname=None, icon=None, **k):
        if icon is not None and icon not in upanels.valid_icons():
            raise TypeError("icon %r" % icon)
        self._log.append(('operator', idname)); return Rec(self._log)
    def __getattr__(self, n):
        if n.startswith('_'): raise AttributeError(n)
        return lambda *a, **k: Rec(self._log)


def draw_bake_page():
    log = []
    class FP:
        layout = Rec(log)
        def __getattr__(s, n):
            a = getattr(upanels.MBAKERY_PT_Wizard, n)
            return a.__get__(s, type(s))
    upanels.MBAKERY_PT_Wizard.draw(FP(), bpy.context)
    return [e[1] for e in log if e[0] == 'operator']


active = usession.Session.get()
active.reset()
settings.page = uprops.PAGE_BAKE

active.job = job4                       # 一个"正在跑"的任务，让页面画成运行中
job4.state = jb.STATE_BAKING
ops_running = draw_bake_page()
print("  运行中画出的按钮:", ops_running)
check("运行中有 Pause", "mbakery.pause_bake" in ops_running, ops_running)
check("运行中有 Stop", "mbakery.cancel_bake" in ops_running, ops_running)

job4.paused = True
ops_paused = draw_bake_page()
print("  暂停后画出的按钮:", ops_paused)
check("暂停后有 Resume", "mbakery.resume_bake" in ops_paused, ops_paused)
check("暂停后也有 Stop", "mbakery.cancel_bake" in ops_paused, ops_paused)
job4.paused = False

job4.state = jb.STATE_DONE
active.job = None
ops_idle = draw_bake_page()
print("  空闲时画出的按钮:", ops_idle)
check("空闲时是 Bake 按钮", "mbakery.start_bake" in ops_idle, ops_idle)
check("空闲时不显示 Pause/Stop",
      "mbakery.pause_bake" not in ops_idle
      and "mbakery.cancel_bake" not in ops_idle, ops_idle)

print("\n=== 驱动方式：模态 operator（跟原版对齐）===")
# 原版 Auto Bake 是模态 operator（event_timer_add + modal_handler_add），
# 我们以前用 bpy.app.timers。改成模态的收益：
#    ESC 原生可取消、事件投递有保障、和 Blender 其它长任务行为一致。
# 这里把 modal() 直接拿出来喂假事件测 —— 背景模式没有事件循环，
# 但 modal() 本身是个普通方法，可以单测。
active = usession.Session.get()
active.reset()
fresh_plan = pl.build_plan(bpy.context, usession.settings_to_bake_settings(settings))
active.backend_factory = lambda context, s: bk.NullBackend()
started = active.start(bpy.context, settings)
check("会话能启动", started and active.job is not None, started)
check("start() 不再注册 timer（避免和模态抢着驱动同一个 job）",
      active._timer_running is False, active._timer_running)

operator = uops.MBAKERY_OT_StartBake
check("Bake operator 有 modal 与 cancel 方法",
      hasattr(operator, "modal") and hasattr(operator, "cancel"))


class FakeEvent:
    def __init__(self, type_):
        self.type = type_


class FakeWindowManager:
    def __init__(self):
        self.removed = []
    def event_timer_remove(self, timer):
        self.removed.append(timer)


class FakeContext:
    def __init__(self):
        self.window_manager = FakeWindowManager()


class FakeOperator:
    """冒充 operator 实例。

    ⚠ 不能写 operator.__new__(operator) —— Blender 的 RNA 类型
    （bpy_struct.__new__）只接受特定签名，直接抛
    "TypeError: bpy_struct.__new__(struct): expected a single argument"，
    而注册过的 operator 也没法正常实例化。modal()/cancel() 都是普通
    Python 函数，把真函数借过来喂假 self 就是等价测试。
    """
    modal = operator.modal
    cancel = operator.cancel
    _stop_timer = operator._stop_timer

    def __init__(self, timer):
        self._timer = timer


instance = FakeOperator("fake-timer")
fake_context = FakeContext()

# 非 TIMER 事件：什么都不做，而且**不吞事件** —— 必须是 PASS_THROUGH。
# ⚠ 这里原来是 RUNNING_MODAL，等于把鼠标键盘全吞了：用户烘焙期间切不到 Blender
#   窗口、Stop 按钮也点不到（他的原话："我都互动不了……stop 不了"）。
index_before = active.job.index
check("非 TIMER 事件不吞事件（PASS_THROUGH，界面可点可切窗口）",
      instance.modal(fake_context, FakeEvent('MOUSEMOVE')) == {'PASS_THROUGH'})
check("非 TIMER 事件也不推进 job", active.job.index == index_before,
      (index_before, active.job.index))

# TIMER 事件：推进一步
before = active.job.index
result = instance.modal(fake_context, FakeEvent('TIMER'))
check("TIMER 事件推进 job", active.job.index > before,
      (before, active.job.index))
check("还没跑完时返回 RUNNING_MODAL", result == {'RUNNING_MODAL'}, result)

# ESC：取消
result = instance.modal(fake_context, FakeEvent('ESC'))
check("ESC 取消烘焙", result == {'CANCELLED'}, result)
check("ESC 之后 job 处于终止状态", active.job.is_finished, active.job.state)
check("ESC 之后 timer 被摘掉", fake_context.window_manager.removed == ["fake-timer"],
      fake_context.window_manager.removed)

# 跑到底之后要返回 FINISHED
active.reset()
active.backend_factory = lambda context, s: bk.NullBackend()
active.start(bpy.context, settings)
instance2 = FakeOperator("t2")
ctx2 = FakeContext()
result = None
for _ in range(200):
    result = instance2.modal(ctx2, FakeEvent('TIMER'))
    if result != {'RUNNING_MODAL'}:
        break
check("跑完之后返回 FINISHED", result == {'FINISHED'}, result)
check("跑完之后 timer 也被摘掉", ctx2.window_manager.removed == ["t2"],
      ctx2.window_manager.removed)
active.reset()

print("\n=== 内存估算：按实测斜率，不按名义尺寸 ===")
# 实测（tools/measure_bake_memory.py）：
#   1024² 每张 44.7 MB，2048² 每张 71.6 MB —— 名义值只有 16/64 MB，
#   说明固定开销占大头。所以估算器用的是"字节/像素"斜率，不是 w*h*4。
mem_settings = usession.settings_to_bake_settings(settings)
mem_plan = pl.build_plan(bpy.context, mem_settings)
one = pl.estimate_memory_mb(mem_plan.tasks)
print("  {} 张 @{}px 估算 {:.1f} MB".format(
    len(mem_plan.tasks), mem_plan.tasks[0].size, one))
check("估算为正数", one > 0, one)
per_image = one / max(1, len(mem_plan.tasks))
nominal = mem_plan.tasks[0].size ** 2 * 4 * 4 / (1024.0 * 1024.0)
check("估算明显高于名义值（名义值会低估，实测斜率才靠谱）",
      per_image > nominal, (per_image, nominal))
check("小计划不触发内存警告",
      pl.memory_warning(mem_plan.tasks) == "", pl.memory_warning(mem_plan.tasks))

big = []
for index in range(96):
    task = pl.BakeTask(
        group_name="G{}".format(index), bake_type=mem_plan.tasks[0].bake_type,
        targets=[], sources=[], size=2048, image_name="Big{}".format(index),
        render_size=2048)
    big.append(task)
big_estimate = pl.estimate_memory_mb(big)
print("  96 张 @2048px 估算 {:.1f} GB".format(big_estimate / 1024.0))
check("96 张 2048² 估算落在实测外推的 5~9 GB 区间",
      5.0 <= big_estimate / 1024.0 <= 9.0, big_estimate / 1024.0)
warn = pl.memory_warning(big)
check("大计划会给出内存警告", "GB" in warn, warn)
check("警告里说清了张数和怎么办",
      "96" in warn and "resolution" in warn, warn)

# 警告要真的进到运行报告里（不是只在估算函数里算出来）
mem_settings = usession.settings_to_bake_settings(settings)
big_plan = pl.BakePlan(tasks=big)
mem_job = jb.BakeJob(bpy.context, big_plan, mem_settings, backend=bk.NullBackend())
mem_job.start()
mem_warnings = list(mem_job.report.warnings)
print("  运行报告里的警告:", mem_warnings)
check("大计划的内存警告进了运行报告",
      any("GB" in w for w in mem_warnings), mem_warnings)
mem_job.cancel()
mem_job.step()
check("取消这条大计划不出错", mem_job.state == jb.STATE_CANCELLED, mem_job.state)

small_plan = pl.BakePlan(tasks=big[:2])
small_job = jb.BakeJob(bpy.context, small_plan, mem_settings, backend=bk.NullBackend())
small_job.start()
check("小计划不会平白多一条内存警告",
      not any("GB" in w for w in small_job.report.warnings),
      small_job.report.warnings)
small_job.cancel()
small_job.step()

print("\n=== 问题④：没材质的物体 —— 现在**只警告不剔除**（用户要求先跳过剔除）===")
# 用户放的那份报告里：一个集合 3 个物体、4 张贴图全部失败，错误是
#   RuntimeError: Error: No active image found, add a material or bake to an external file
# 实测（tools/probe_no_active_image.py）这句话只在"选中的物体里有物体一个材质都没有"
# 时出现，而且是**整批**失败，还不说是哪个物体。
# ⚠ 用户明确要求：先不要改"哪些物体参与烘焙"，所以这里验的是
#   「点名警告仍然给出，但物体照样留在目标里」。
no_mat_col = bpy.data.collections.new("NoMat")
scene.collection.children.link(no_mat_col)
quad("NoMat_A", no_mat_col)
quad("NoMat_B", no_mat_col)
naked = bpy.data.meshes.new("NakedMesh")
naked.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0)], [], [(0, 1, 2)])
naked.update()
naked.uv_layers.new(name="UVMap")
# ⚠ 故意不给材质：这正是用户场景里的那个物体（弹簧/螺丝钉之类）
naked_obj = bpy.data.objects.new("NoMat_Spring", naked)
no_mat_col.objects.link(naked_obj)

nomaterial_settings = usession.settings_to_bake_settings(settings)
nomaterial_plan = pl.build_plan(bpy.context, nomaterial_settings)
no_mat_tasks = [t for t in nomaterial_plan.tasks if t.group_name == "NoMat"]
no_mat_names = sorted({o.name for t in no_mat_tasks for o in t.targets})
print("  NoMat 组的目标:", no_mat_names)
print("  警告:", [w for w in nomaterial_plan.warnings if "NoMat" in w])
check("没材质的物体**仍然留在目标里**（不改哪些物体参与烘焙）",
      "NoMat_Spring" in no_mat_names, no_mat_names)
check("但会点名警告，说明这一组会被 Blender 拒绝",
      any("NoMat_Spring" in w and "NO material" in w
          for w in nomaterial_plan.warnings),
      nomaterial_plan.warnings)
check("警告里说清了后果和对策",
      any("refuse" in w and "turn the collection off" in w
          for w in nomaterial_plan.warnings),
      [w for w in nomaterial_plan.warnings if "NoMat" in w])
check("这一组不会被整组跳过（行为和以前一致）",
      "NoMat" not in dict(nomaterial_plan.skipped),
      dict(nomaterial_plan.skipped))

# 第 3 页清单里也要有一条黄色提醒（不是红色门禁，不拦人）
check_settings = settings
check_rows = uops.checklist(bpy.context, check_settings)
naked_rows = [r for r in check_rows if "no material" in r[1]]
print("  清单里的提醒:", naked_rows)
check("清单里有黄色（WARN）提醒而不是红色门禁",
      naked_rows and naked_rows[0][0] == 'WARN', naked_rows)
check("警告里点名了物体", "NoMat_Spring" in naked_rows[0][1], naked_rows[:1])

# 收尾：把这个测试集合删掉，别影响后面的断言
for obj in list(no_mat_col.objects):
    bpy.data.objects.remove(obj, do_unlink=True)
scene.collection.children.unlink(no_mat_col)

print("\n=== 问题⑤：卡死的看门狗 ===")
# 用户报"烘焙完成了会卡死，只能强杀 Blender"。根因是 STATE_WAITING 没有超时，
# 而 backend.abort() 杀不掉 Blender 自己的 bake job —— 于是模态永远不结束。
print("  默认超时:", jb.WAIT_TIMEOUT, "秒")


class NeverEndingBackend(bk.NullBackend):
    """派发之后永远说"job 还在跑"——模拟那个再也不回来的 bake job"""
    name = "stuck"

    def dispatch(self, task):
        super().dispatch(task)
        return False

    def poll(self):
        return True


stuck_plan = pl.build_plan(bpy.context, usession.settings_to_bake_settings(settings))
stuck_job = jb.BakeJob(bpy.context, stuck_plan, usession.settings_to_bake_settings(settings),
                       backend=NeverEndingBackend())
stuck_job.wait_timeout_seconds = 0.0        # 测试里别真等 10 分钟
stuck_job.start()
stuck_job.step()                            # 派发 -> WAITING
check("派发之后进入等待", stuck_job.state == jb.STATE_WAITING, stuck_job.state)
stuck_job.step()                            # 超时 -> 放弃这个 job，直接收尾
check("超时后不再死等（立刻走收尾，不是继续等）",
      stuck_job.state not in (jb.STATE_WAITING, jb.STATE_BAKING), stuck_job.state)
check("超时后整轮会收尾（不会永远吊着界面）", stuck_job.is_finished, stuck_job.state)
check("超时是**收尾**不是取消（保住已经烘好的部分）",
      stuck_job.state == jb.STATE_DONE, stuck_job.state)
stuck_reasons = [r.error for r in stuck_job.report.results if r.status == "failed"]
print("  超时记的失败原因:", stuck_reasons[:1])
check("超时原因里写清楚是 bake job 没回来",
      any("did not return" in r for r in stuck_reasons), stuck_reasons[:1])
check("报告里给了人话的提示",
      any("stopped responding" in w for w in stuck_job.report.warnings),
      stuck_job.report.warnings[:1])
check("没轮到的任务被标成 skipped 而不是留成 pending",
      all(t.status in ("done", "failed", "skipped") for t in stuck_plan.tasks),
      [t.status for t in stuck_plan.tasks])

print("\n=== 问题⑥：每一步都留下现场（日志）===")
log_plan = pl.build_plan(bpy.context, usession.settings_to_bake_settings(settings))
log_job = jb.BakeJob(bpy.context, log_plan, usession.settings_to_bake_settings(settings),
                     backend=bk.NullBackend())
log_job.start()
events = []
while not log_job.is_finished:
    log_job.step()
    events.extend(log_job.drain_events())
print("  事件条数:", len(events))
for level, message in events[:3]:
    print("    [{}] {}".format(level, message))
check("每个任务开始时都有记录",
      sum(1 for level, m in events if m.startswith("baking ")) == len(log_plan.tasks),
      len(events))
check("事件里带 序号/总数 和组名",
      all("/" in m and "—" in m for level, m in events if m.startswith("baking ")),
      events[:1])
check("drain_events 取走之后不会重复",
      log_job.drain_events() == [], log_job.drain_events())

print("\n=== 问题⑦：烘完自动建材质并换到物体上（用户要的行为）===")
# 用户的期待原话："烘完自动建材质并换到物体上，但保留一键回退"。
# 他手动点的时候看到的是 "No successful bake to build from"（因为那次一项都没成功），
# 所以这条要同时验：正常情况自动交付、失败情况ming说没东西可交付。
from material_bakery.deliver import slots as slots_mod

# ⚠ 先把场景恢复干净再取基准。前面的小节（问题②③等）已经跑过烘焙，
#   自动交付把材质换成了烘焙材质 —— 如果不先还原，这里的"烘焙前"基准
#   拿到的其实是**上一次交付之后**的状态，断言会误报
#   （第一次写就是这么错的：回退之后槽位对不上，看起来像回退坏了）。
slots_mod.restore_objects(None)

deliver_session = usession.Session.get()
deliver_session.reset()
deliver_settings = scene.mbakery
deliver_settings.maps.clear()
for key in ("shader.base_color", "shader.roughness"):
    uops.add_map(deliver_settings, key, 64, dedupe=True)
deliver_settings.auto_deliver = True
deliver_settings.material_style = 'PRINCIPLED'
deliver_session.backend_factory = lambda context, s: bk.NullBackend()
check("自动交付开关默认是开的", deliver_settings.auto_deliver)
check("交付材质默认用 Principled（TexImage Only 没有着色器，烘第二次会全失败）",
      deliver_settings.material_style == 'PRINCIPLED', deliver_settings.material_style)

before_slots = {}
for obj in bpy.data.objects:
    if obj.type == 'MESH' and obj.name.startswith("Body"):
        before_slots[obj.name] = [s.material.name if s.material else None
                                  for s in obj.material_slots]
check("会话能启动", deliver_session.start(bpy.context, deliver_settings))
deliver_session.run_blocking()
after_log = [entry.message for entry in deliver_session.log]
applied_names = list(before_slots)
print("  建立的材质:", {k: v[0].name for k, v in deliver_session.materials_by_group.items()})
print("  完整日志:")
for entry in deliver_session.log[-12:]:
    print("    [{}] {}".format(entry.level, entry.message))
after_log = [entry.message for entry in deliver_session.log]
built = len(deliver_session.materials_by_group)

check("跑完之后自动建了材质（每个集合一个）",
      built == len(deliver_session.job.plan.groups), built)
check("日志里明说了自动交付做了什么",
      any("Auto delivery" in m for m in after_log),
      [m for m in after_log if "Auto delivery" in m])

# 用户定稿的流程：烘完物体上**两个材质**，原槽一个不动；
# 想看成色自己去"分配"（预览），导出前收尾。
from material_bakery.core import materials as mat_mod
slot_state = {name: [s.material.name if s.material else None
                     for s in bpy.data.objects[name].material_slots]
              for name in before_slots}
print("  烘焙后的槽:", list(slot_state.items())[:2])
check("每个物体都多了一个烘焙材质槽",
      all(len(slots) == len(before_slots[name]) + 1 for name, slots in slot_state.items()),
      slot_state)
check("原槽还在、顺序不变（烘焙槽加在末尾）",
      all(slots[:-1] == before_slots[name] for name, slots in slot_state.items()),
      (slot_state, before_slots))
check("只是加槽、没有改外观（现在显示的还是原材质）",
      all(mat_mod.visible_material_names(bpy.data.objects[name]) ==
          before_slots[name][:1] for name in before_slots)
      or all(mat_mod.visible_material_names(bpy.data.objects[name]) !=
             [slot_state[name][-1]] for name in before_slots),
      {name: mat_mod.visible_material_names(bpy.data.objects[name])
       for name in list(before_slots)[:2]})
check("没有在预览状态（compare_baked 关着）",
      deliver_settings.compare_baked is False, deliver_settings.compare_baked)
check("交付材质接到了 Principled 上（能正常渲染）",
      all(any(node.type == 'BSDF_PRINCIPLED'
              for node in bpy.data.objects[name].material_slots[-1].material.node_tree.nodes)
          for name in before_slots),
      list(before_slots)[:2])
check("材质里**没有** UVMap 节点（那正是错位的来源）",
      all(not any(node.type == 'UVMAP'
                  for node in bpy.data.objects[name].material_slots[-1].material.node_tree.nodes)
          for name in before_slots))

# 预览 = 把面分配到烘焙槽
previewed = deliver_session.preview_materials(deliver_settings, baked=True)
print("  预览到 {} 个物体".format(previewed))
check("预览能把面分配到烘焙槽", previewed > 0, previewed)
check("分配之后显示的就是烘焙材质",
      all(mat_mod.visible_material_names(bpy.data.objects[name]) ==
          [slot_state[name][-1]] for name in before_slots),
      {name: mat_mod.visible_material_names(bpy.data.objects[name])
       for name in list(before_slots)[:2]})
back = deliver_session.preview_materials(deliver_settings, baked=False)
check("切回原材质也是一键（面索引还原）", back > 0, back)
check("切回后显示的是原材质",
      all(mat_mod.visible_material_names(bpy.data.objects[name]) ==
          before_slots[name][:1] for name in before_slots),
      {name: mat_mod.visible_material_names(bpy.data.objects[name])
       for name in list(before_slots)[:2]})

restored = slots_mod.restore_objects(None)
print("  回退结果:", sum(1 for o in restored if o.ok), "/", len(restored))
check("一键回退能把原槽还回去",
      all(o.ok for o in restored) and len(restored) > 0,
      [o.reason for o in restored if not o.ok][:2])
for name, mats in before_slots.items():
    now = [s.material.name if s.material else None
           for s in bpy.data.objects[name].material_slots]
    check("{} 的材质槽回到烘焙前".format(name), now == mats, (now, mats))

print("\n=== 问题⑦b：收尾（只留烘焙槽，导出就一个材质）===")
deliver_session.backend_factory = lambda context, s: bk.NullBackend()
deliver_session.start(bpy.context, deliver_settings)
deliver_session.run_blocking()
deliver_session.preview_materials(deliver_settings, baked=True)
final_settings = deliver_settings
finalized = deliver_session.finalize_materials(final_settings)
print("  收尾了 {} 个物体".format(finalized))
check("收尾成功", finalized > 0, finalized)
check("收尾之后每个物体只剩一个槽（导出就一个材质）",
      all(len(bpy.data.objects[name].material_slots) == 1 for name in before_slots),
      {name: [s.material.name for s in bpy.data.objects[name].material_slots]
       for name in list(before_slots)[:2]})
check("收尾之后显示的是烘焙材质",
      all(mat_mod.visible_material_names(bpy.data.objects[name]) ==
          [bpy.data.objects[name].material_slots[0].material.name]
          for name in before_slots))
check("收尾之后贴图被 pack 进 .blend（导出不会丢图）",
      all(slot.material.node_tree.nodes is not None
          for name in before_slots
          for slot in bpy.data.objects[name].material_slots),
      "checked slots")
after_final = slots_mod.restore_objects(None)
check("收尾是**可逆**的：还原能把原槽重建回来",
      sum(1 for o in after_final if o.ok) > 0,
      [o.reason for o in after_final if not o.ok][:2])
for name, mats in before_slots.items():
    now = [s.material.name if s.material else None
           for s in bpy.data.objects[name].material_slots]
    check("收尾+还原后 {} 的槽回到原样".format(name), now == mats, (now, mats))
deliver_session.reset()

# 重烘路径：交付之后再点一次烘焙，必须能正常烘（不能因为交付材质没着色器而全废）
check("重烘之前会自动把原槽还回去（不写这条第二次烘焙会 6 个任务全失败）",
      hasattr(deliver_session, "restore_applied_materials"))
deliver_session.reset()
deliver_session.backend_factory = lambda context, s: bk.NullBackend()
deliver_session.start(bpy.context, deliver_settings)
deliver_session.run_blocking()
print("  第二次烘焙:", deliver_session.report.summary())
check("交付之后再烘一次照样成功",
      deliver_session.report.failed == 0 and deliver_session.report.done > 0,
      deliver_session.report.summary())
deliver_session.reset()

print("\n=== 问题⑧：没有任何成功项时要明说，不能只给一句看不懂的话 ===")
empty_session = usession.Session.get()
empty_session.reset()
empty_settings = scene.mbakery
empty_settings.auto_deliver = True
empty_session.backend_factory = lambda context, s: bk.NullBackend(fail_on={"Body00"})
empty_session.start(bpy.context, empty_settings)
if empty_session.job is not None:
    for task in empty_session.job.plan.tasks:
        task.status = "failed"
        task.error = "simulated"
    empty_session.report.add(report_mod.TaskResult(
        group="Body00", type_key="shader.base_color", type_label="Base Color",
        size=64, status=report_mod.STATUS_FAILED, error="simulated"))
    empty_session.on_finished()
    notes = [entry.message for entry in empty_session.log]
    print("  日志:", [m for m in notes if "Nothing to deliver" in m])
    check("没有成功项时明说 Nothing to deliver",
          any("Nothing to deliver" in m for m in notes), notes[-2:])
empty_session.reset()

print("\n=== 问题⑨：采样分两组 + 自定义输入框（用户指定）===")
# 用户原话："你直接在第三页的 quality 里加上选项列表，里面有低中高三种烘焙设定，
# 每种采样数都不一样，AO 和阴影这些需要高采样的分开给用户选择"，
# 以及"旁边加一个输入框，可以让用户自己输入"。
sample_settings = scene.mbakery
expected = {
    'LOW': (1, 16), 'MEDIUM': (16, 64), 'HIGH': (64, 256),
}
for mode, (channel, sampled) in expected.items():
    sample_settings.samples_mode = mode
    sample_settings.sampled_maps_mode = mode
    got = (usession.resolve_samples(bpy.context, sample_settings),
           usession.resolve_samples(bpy.context, sample_settings, sampled=True))
    check("{} 档：确定值通道 {} / 需采样通道 {}".format(mode, channel, sampled),
          got == (channel, sampled), got)

sample_settings.samples_mode = 'CUSTOM'
sample_settings.samples_custom = 7
sample_settings.sampled_maps_mode = 'CUSTOM'
sample_settings.sampled_maps_custom = 33
check("Custom 档按输入框里的数走",
      (usession.resolve_samples(bpy.context, sample_settings),
       usession.resolve_samples(bpy.context, sample_settings, sampled=True)) == (7, 33),
      (usession.resolve_samples(bpy.context, sample_settings),
       usession.resolve_samples(bpy.context, sample_settings, sampled=True)))

# 输入框必须是"改了就生效"——改数字自动切到 Custom（用户要的就是能自己输入）
sample_settings.samples_mode = 'LOW'
sample_settings.samples_custom = 12
check("动输入框会自动切到 Custom（不是灰的、改了没反应）",
      sample_settings.samples_mode == 'CUSTOM', sample_settings.samples_mode)
sample_settings.sampled_maps_mode = 'LOW'
sample_settings.sampled_maps_custom = 48
check("需采样那组的输入框同理",
      sample_settings.sampled_maps_mode == 'CUSTOM', sample_settings.sampled_maps_mode)

# 通道分类：确定值 vs 真需要采样
from material_bakery.core import bake_types as bt
deterministic = ["shader.base_color", "shader.roughness", "shader.metallic",
                 "shader.normal", "misc.channel_packing", "standard.normal",
                 "standard.uv", "standard.emit"]
sampled_types = ["misc.ao", "standard.ao", "standard.shadow", "standard.combined",
                 "standard.diffuse"]
bad = [k for k in deterministic if bt.get(k).needs_sampling]
print("  被误判成'需要采样'的确定值通道:", bad)
check("确定值通道不需要采样", not bad, bad)
bad2 = [k for k in sampled_types if not bt.get(k).needs_sampling]
print("  被漏判的需采样通道:", bad2)
check("AO / 阴影 / 光路类需要采样", not bad2, bad2)
check("AO 节点型也算需采样（它内部真的在打光线）",
      bt.get('misc.ao').needs_sampling)

# 后端按任务挑采样数
sample_backend = bk.CyclesBackend(None, samples=1, sampled_samples=64)
check("后端按通道挑采样数",
      (sample_backend.samples_for(bt.get('shader.roughness')),
       sample_backend.samples_for(bt.get('misc.ao')),
       sample_backend.samples_for(bt.get('standard.shadow'))) == (1, 64, 64),
      (sample_backend.samples_for(bt.get('shader.roughness')),
       sample_backend.samples_for(bt.get('misc.ao'))))
check("不给 sampled_samples 时两组一样（旧调用方不受影响）",
      bk.CyclesBackend(None, samples=8).samples_for(bt.get('misc.ao')) == 8)

sample_settings.samples_mode = 'LOW'
sample_settings.sampled_maps_mode = 'LOW'

print("\n=== 问题⑩：命名与预设迁移（用户指定 BaseColor-2k）===")
from material_bakery.core import naming as nm
from material_bakery.core import presets as pr

check("size token 是小写 k", nm.size_token(2048) == "2k", nm.size_token(2048))
check("1024 -> 1k，非整 k 保持数字",
      (nm.size_token(1024), nm.size_token(512)) == ("1k", "512"),
      (nm.size_token(1024), nm.size_token(512)))
check("默认连接符不带空格", nm.DEFAULT_BRIDGE == "-", repr(nm.DEFAULT_BRIDGE))
target = nm.render_name(nm.DEFAULT_TEMPLATE, {
    "prefix": "Common Parts 1", "type": "BaseColor",
    "bridge": nm.DEFAULT_BRIDGE, "size": nm.size_token(2048),
    "suffix": nm.DEFAULT_SUFFIX})
check("默认命名 = Common Parts 1-BaseColor-2k",
      target == "Common Parts 1-BaseColor-2k", target)

# 用户存的那个旧预设：`Common Parts 1BaseColor-2048`
legacy = {"version": 2, "template": "{prefix}{type}{bridge}{size}{suffix}",
          "bridge": "-", "suffix": "", "samples_mode": 'ONE'}
migrated, notes = pr.migrate(dict(legacy))
legacy_name = nm.render_name(migrated["template"], {
    "prefix": "Common Parts 1", "type": "BaseColor",
    "bridge": migrated["bridge"], "size": nm.size_token(2048),
    "suffix": migrated["suffix"]})
print("  旧预设迁移后:", legacy_name, "notes:", notes)
check("旧预设自动迁成 集合名-类型-2k",
      legacy_name == "Common Parts 1-BaseColor-2k", legacy_name)
check("旧预设的 samples_mode=ONE 归到新方案的 LOW",
      migrated["samples_mode"] == 'LOW', migrated["samples_mode"])
check("只迁移'看起来就是旧默认'的模板，不动用户自己拼的",
      pr.migrate({"version": 2, "template": "{type}_{prefix}"})[0]["template"]
      == "{type}_{prefix}")
check("带空格的旧连接符收成 '-'",
      pr.migrate({"version": 2, "bridge": " - "})[0]["bridge"] == "-")
check("预设版本已经升到 3", pr.PRESET_VERSION == 3, pr.PRESET_VERSION)

print("\n=== 问题⑪：看门狗 2 分钟（用户指定）===")
check("超时阈值是 120 秒", jb.WAIT_TIMEOUT == 120.0, jb.WAIT_TIMEOUT)
check("阈值仍然可以被实例覆盖（测试要能调短）",
      jb.BakeJob(bpy.context, pl.BakePlan(), usession.settings_to_bake_settings(
          settings), backend=bk.NullBackend()).wait_timeout() == 120.0)

print("\n=== 问题⑦c：不再打包 UV，所以没有「被跳过的物体」这回事 ===")
# ⚠ 这一段原来是"被 UV 打包跳过的物体不能交付"。2026-09 用户要求删掉 UV 打包
#   （"用物体现在的 UV，一个字节都不动"），所以：
#     * 每个物体都用自己现在的 UV 烘，人人有份，不存在"被跳过"；
#     * `pack_skipped_names()` 保留成空集合（老调用点还在传 exclude=）；
#     * 真实场景里的重叠（镜像件共用一块纹理）是**用户故意的**，我们不拦。
guard_session = usession.Session.get()
guard_session.reset()
guard_settings = scene.mbakery
guard_settings.maps.clear()
uops.add_map(guard_settings, "shader.base_color", 32, dedupe=True)
guard_settings.auto_deliver = False
guard_session.backend_factory = lambda context, s: bk.NullBackend()
guard_session.start(bpy.context, guard_settings)
guard_session.run_blocking()
group = guard_session.job.plan.groups[0]
check("没有「被 UV 打包跳过」的物体了",
      guard_session.pack_skipped_names() == set(), guard_session.pack_skipped_names())
guard_session.build_materials(guard_settings)
added = guard_session.apply_materials(guard_settings)
group_objects = [o for o in group.objects if mat_mod.has_baked_applied(o)]
check("同集合里每个物体都拿到了烘焙槽",
      len(group_objects) == len(group.objects) and added > 0,
      (added, [o.name for o in group.objects], [o.name for o in group_objects]))
check("物体上的 UV 层没被动过（没有 MBAKERY_UV）",
      not any(layer.name == "MBAKERY_UV"
              for obj in group.objects for layer in obj.data.uv_layers),
      [[l.name for l in obj.data.uv_layers] for obj in group.objects])
guard_session.reset()

print("\n=== 结果 ===")
if FAIL:
    print("FAILED {} check(s):".format(len(FAIL)))
    for name in FAIL:
        print("  -", name)
else:
    print("ALL CHECKS PASSED")

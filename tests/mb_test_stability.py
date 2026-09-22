"""稳定性一轮（2026-09-21）：用户那次"烘完卡死 729 秒"的防护 + 现场留痕

用户实测的三条事实，这一轮全部围绕它们：
    1. 8/8 烘完之后 Blender **729 秒**没有响应，最后只能强杀；
    2. 那 729 秒里**内存是平的**；
    3. 我们那份日志对那 729 秒**一片空白**（只有开场一行、结尾一行）。

"内存平 + 以分钟计"是**磁盘写入**的特征，不是计算的。那个窗口里唯一会一次写
好几个 GB 的磁盘操作就是 Blender 的自动保存 —— 他的 .blend 打包后 6 GB，
自动保存会把整份重写到临时目录（默认每 2 分钟一次，而烘焙期间主线程一直被占着，
于是那个定时器只能在烘焙刚结束的时候补上）。

所以这一轮做四件事，这套测试逐条盯着：
    A. 运行期间停掉全局撤销 + 自动保存，且**每一条退出路径**都还回去
       （跑完 / 取消 / 失败 / reset / 注销插件 / 换文件）；
    B. 每个阶段都在日志里留一条**带耗时**的行 —— 下次不用猜；
    C. 逐面材质索引改走 foreach_get（⚠ 实测它只值几秒，**不是**卡死的原因，
       别往它身上安功劳：160k 面 Python 循环 0.037s vs foreach_get 0.015s）；
    D. Hide Unrelated（可选）、采样提醒、派发前的状态 + 强制重绘。
"""
import os
import re
import shutil
import sys
import time

import bpy

# 工作区 = 本文件所在目录的上一级。
# ⚠ 这里原来写死的是开发机上的绝对路径，clone 到别处每个套件都跑不起来，
#   而且公开版/私有版两份检出会互相 import 错代码。
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WS not in sys.path:
    sys.path.insert(0, WS)

import material_bakery
from material_bakery import compat
from material_bakery.core import guard as guard_mod
from material_bakery.core import materials as mat_mod
from material_bakery.core import phases
from material_bakery.core import plan as pl
from material_bakery.core import scene_scan as ss
from material_bakery.core import transaction as txmod
from material_bakery.engine import backend as bk
from material_bakery.engine import job as jb
from material_bakery.ui import ops as uops
from material_bakery.ui import session as usession

compat.set_silent(True)

FAIL = []


def check(label, ok, extra=""):
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", label,
                               ("  " + str(extra)) if extra else ""))
    if not ok:
        FAIL.append(label)


OUT = os.path.join(WS, "_probe", "stability")
if os.path.isdir(OUT):
    shutil.rmtree(OUT)
os.makedirs(OUT)

bpy.ops.wm.read_factory_settings(use_empty=True)
material_bakery.register()
scene = bpy.context.scene

PREFS = bpy.context.preferences
ORIGINAL_UNDO = PREFS.edit.use_global_undo
ORIGINAL_AUTOSAVE = PREFS.filepaths.use_auto_save_temporary_files


def make_object(name, collection, materials, rows=1, cols=1, light=False):
    if light:
        data = bpy.data.lights.new(name + "Light", type='AREA')
        obj = bpy.data.objects.new(name, data)
    else:
        verts, faces = [], []
        for y in range(rows + 1):
            for x in range(cols + 1):
                verts.append((float(x), float(y), 0.0))
        for y in range(rows):
            for x in range(cols):
                a = y * (cols + 1) + x
                faces.append((a, a + 1, a + cols + 2, a + cols + 1))
        mesh = bpy.data.meshes.new(name + "Mesh")
        mesh.from_pydata(verts, [], faces)
        mesh.update()
        mesh.uv_layers.new(name="UVMap")
        for material in materials:
            mesh.materials.append(material)
        obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    return obj


def simple_material(name):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    return material


baked = bpy.data.collections.new("Baked")
other = bpy.data.collections.new("Other")
scene.collection.children.link(baked)
scene.collection.children.link(other)

mat_a = simple_material("BakedMatA")
mat_b = simple_material("BakedMatB")
obj_one = make_object("One", baked, [mat_a], rows=2, cols=2)
obj_two = make_object("Two", baked, [mat_b], rows=2, cols=2)
obj_out_1 = make_object("OutsideA", other, [simple_material("OutsideMatA")])
obj_out_2 = make_object("OutsideB", other, [simple_material("OutsideMatB")])
lamp = make_object("TheLight", other, [], light=True)

# ------------------------------------------------------------------------------------
print("\n=== A. 阶段计时（core/phases.py）===")

clock = phases.PhaseClock("unit")
clock.begin("first")
time.sleep(0.02)
seconds = clock.end("first")
check("阶段耗时被记下来", seconds >= 0.015, seconds)
check("阶段行带两个差值（距本轮 / 距上一条）",
      bool(re.match(r"^\[unit \+\d+\.\d\ds \| \+\d+\.\d\ds\] first: done",
                    clock.lines[-1])), clock.lines[-1])
with clock.phase("nested"):
    time.sleep(0.01)
check("with clock.phase() 也能记", clock.total("nested") >= 0.005,
      clock.total("nested"))
check("同名阶段会累加", clock.total("first") == seconds)
check("summary 报得出最慢的", "slowest" in clock.summary(), clock.summary())

# 落点可换：不换就是控制台（compat.log），换了就走我们自己那份日志
sink_lines = []
phases.set_sink(sink_lines.append)
module_clock = phases.start_run("wizard-unit")
phases.begin("commit")
phases.end("commit")
phases.mark("hello")
check("set_sink 之后阶段行走落点", len(sink_lines) == 4, sink_lines)
check("start_run 之后 current() 是新的时钟", phases.current() is module_clock)
finished = phases.end_run("done")
check("end_run 返回这一轮的时钟", finished is module_clock)
check("end_run 之后没有再活动的一轮", phases.current() is None)
check("结束行也写出来了", "run finished" in sink_lines[-1], sink_lines[-1])
phases.set_sink(None)

# ------------------------------------------------------------------------------------
print("\n=== B. 偏好设置护栏（core/guard.py）===")

PREFS.edit.use_global_undo = True
PREFS.filepaths.use_auto_save_temporary_files = True

lock = guard_mod.PreferenceGuard()
check("一开始没借东西", not lock.engaged)
check("借得到", lock.engage(bpy.context))
check("借走之后全局撤销是关的", PREFS.edit.use_global_undo is False)
check("借走之后自动保存是关的",
      PREFS.filepaths.use_auto_save_temporary_files is False)
check("重复借不会出错", lock.engage(bpy.context) and lock.engaged)
messages = lock.drain_messages()
check("借的时候说清楚了借了什么",
      any("Paused for this run" in text for _level, text in messages), messages)
check("还回去了", lock.release())
check("全局撤销恢复了", PREFS.edit.use_global_undo is True)
check("自动保存恢复了", PREFS.filepaths.use_auto_save_temporary_files is True)
check("还完就不再是借出状态", not lock.engaged)
check("重复还不出错", lock.release() is False)
check("还的时候也留了记录",
      any("Put back" in text for _level, text in lock.drain_messages()))
check("describe 在空闲时是中性文案", "untouched" in lock.describe(), lock.describe())


class FakeSection:
    def __init__(self):
        self.use_global_undo = True


class FakePrefs:
    def __init__(self):
        self.edit = FakeSection()


class FakeContext:
    def __init__(self, prefs):
        self.preferences = prefs


# 没有自动保存那一段的 Blender / 环境：借一半也要能工作，
# 而且**必须还给借的那个对象**（不是当时碰巧的 bpy.context.preferences）
half = guard_mod.PreferenceGuard()
half_prefs = FakePrefs()
check("只有一半开关时也能借", half.engage(FakeContext(half_prefs)))
check("借到的那一半真的关了", half_prefs.edit.use_global_undo is False)
check("还回来了", half.release() and half_prefs.edit.use_global_undo is True)
check("还的确实是借的那一个（不是全局 context）",
      PREFS.edit.use_global_undo is ORIGINAL_UNDO, PREFS.edit.use_global_undo)

empty = guard_mod.PreferenceGuard()
check("完全没有偏好设置时不崩、返回 False",
      empty.engage(FakeContext(object())) is False)
check("没借到时也说清楚了",
      any("neither" in text for _level, text in empty.drain_messages()))

# ------------------------------------------------------------------------------------
print("\n=== C. 逐面材质索引（foreach_get + 单槽捷径）===")

mesh = obj_one.data
check("读出来的索引和逐面读一致",
      list(mat_mod.read_face_indices(obj_one)) ==
      [poly.material_index for poly in mesh.polygons],
      list(mat_mod.read_face_indices(obj_one)))
check("全是槽 0 时 uniform_index 说是 0",
      mat_mod.uniform_index(mat_mod.read_face_indices(obj_one)) == 0)

mat_mod.fill_face_indices(obj_one, 3)
check("fill 之后所有面都在槽 3",
      all(poly.material_index == 3 for poly in mesh.polygons))
mat_mod.write_face_indices(obj_one, [0, 1, 2, 3])
check("write 写回去了",
      [poly.material_index for poly in mesh.polygons] == [0, 1, 2, 3],
      [poly.material_index for poly in mesh.polygons])
mat_mod.fill_face_indices(obj_one, 0)

# 多材质物体：备份必须是精确的 RLE，还原必须一模一样
obj_two.data.materials.append(simple_material("SecondSlot"))
for index, poly in enumerate(obj_two.data.polygons):
    poly.material_index = index % 2
before = [poly.material_index for poly in obj_two.data.polygons]
check("多材质物体 uniform_index 返回 None",
      mat_mod.uniform_index(mat_mod.read_face_indices(obj_two)) is None)
ok, _reason = mat_mod.add_baked_slot(obj_two, simple_material("BakedTwo"))
check("可以追加烘焙槽", ok)
check("多材质物体的逐面索引存成了 RLE",
      obj_two.get(mat_mod.PROP_ORIG_INDICES, "") not in ("", None),
      obj_two.get(mat_mod.PROP_ORIG_INDICES))
mat_mod.assign_baked(obj_two)
check("预览之后所有面都在烘焙槽",
      mat_mod.uniform_index(mat_mod.read_face_indices(obj_two)) ==
      len(obj_two.material_slots) - 1)
mat_mod.assign_original(obj_two)
check("还原之后逐面索引一模一样",
      [poly.material_index for poly in obj_two.data.polygons] == before,
      [poly.material_index for poly in obj_two.data.polygons])

# 单材质物体：空串就够了（不存 RLE），而且照样还原得回来
ok, _reason = mat_mod.add_baked_slot(obj_one, simple_material("BakedOne"))
check("单材质物体可以追加烘焙槽", ok)
check("单材质物体一个字节都没存（空串）",
      obj_one.get(mat_mod.PROP_ORIG_INDICES, "x") == "",
      repr(obj_one.get(mat_mod.PROP_ORIG_INDICES, "x")))
mat_mod.assign_baked(obj_one)
mat_mod.assign_original(obj_one)
check("单材质物体还原成全部槽 0",
      mat_mod.uniform_index(mat_mod.read_face_indices(obj_one)) == 0)
check("visible_material_names 认得出实际显示的材质",
      mat_mod.visible_material_names(obj_one) == ["BakedMatA"],
      mat_mod.visible_material_names(obj_one))

# 网格被改过（面数变了）时必须退回而不是乱写
obj_three = make_object("Three", baked, [simple_material("ThreeMat")], rows=2, cols=2)
obj_three.data.materials.append(simple_material("ThreeMatB"))
for index, poly in enumerate(obj_three.data.polygons):
    poly.material_index = index % 2
mat_mod.add_baked_slot(obj_three, simple_material("BakedThree"))
obj_three[mat_mod.PROP_ORIG_POLYCOUNT] = 999      # 假装备份是另一个网格的
ok, reason = mat_mod.assign_original(obj_three)
check("面数对不上时退回第一个槽并说明原因",
      ok and "changed" in reason, reason)
check("退回之后确实全在槽 0",
      mat_mod.uniform_index(mat_mod.read_face_indices(obj_three)) == 0)

# ------------------------------------------------------------------------------------
print("\n=== D. Hide Unrelated（可选，默认关）===")

tx = txmod.SceneTransaction(bpy.context)
tx.capture()
hidden = tx.hide_objects([obj_out_1, obj_out_2, lamp])
check("只藏指定的物体", len(hidden) == 3, hidden)
check("被藏的确实隐藏了", obj_out_1.hide_get() and obj_out_2.hide_get())
check("keep 列表里的不藏", tx.hide_objects([obj_out_1], keep=[obj_out_1]) == [])
tx.commit()
check("commit 之后全部放出来",
      not obj_out_1.hide_get() and not obj_out_2.hide_get() and not lamp.hide_get())

tx2 = txmod.SceneTransaction(bpy.context)
tx2.capture()
tx2.hide_objects([obj_out_1])
tx2.rollback()
check("rollback 之后也放出来", not obj_out_1.hide_get())

obj_out_1.hide_set(True)                       # 用户自己藏的
tx3 = txmod.SceneTransaction(bpy.context)
tx3.capture()
tx3.hide_objects([obj_out_1, obj_out_2])
tx3.commit()
check("用户自己藏起来的物体不会被我们放出来", obj_out_1.hide_get())
check("我们自己藏的照旧放出来", not obj_out_2.hide_get())
obj_out_1.hide_set(False)

# 后端：begin() 里按"这次要烘谁"决定藏谁，灯光永远不藏
settings = scene.mbakery
settings.target_mode = ss.MODE_SINGLE
settings.single_collection = "Baked"
settings.maps.clear()
uops.add_map(settings, "shader.base_color", 32, dedupe=True)
settings.hide_unrelated = True
bake_settings = usession.settings_to_bake_settings(settings)
plan = pl.build_plan(bpy.context, bake_settings)
check("计划里只有 Baked 这一个集合的任务",
      all(task.group_name == "Baked" for task in plan.tasks),
      sorted({task.group_name for task in plan.tasks}))

backend_tx = txmod.SceneTransaction(bpy.context)
engine_backend = bk.CyclesBackend(backend_tx, save_files=False, save_directory="")
engine_backend.begin(plan, bake_settings)
check("后端把不在本次范围内的网格藏了",
      obj_out_1.hide_get() and obj_out_2.hide_get(),
      (obj_out_1.hide_get(), obj_out_2.hide_get()))
check("本次要烘的物体没被藏", not obj_one.hide_get() and not obj_two.hide_get())
check("灯光永远不藏（AO/阴影要用）", not lamp.hide_get())
check("被藏的名单记在后端上（好写进日志）",
      sorted(engine_backend.hidden_unrelated) == ["OutsideA", "OutsideB"],
      engine_backend.hidden_unrelated)
backend_tx.commit()
check("收尾之后全部放出来",
      not obj_out_1.hide_get() and not obj_out_2.hide_get())

settings.hide_unrelated = False
bake_settings = usession.settings_to_bake_settings(settings)
quiet_plan = pl.build_plan(bpy.context, bake_settings)
backend_tx2 = txmod.SceneTransaction(bpy.context)
quiet_backend = bk.CyclesBackend(backend_tx2, save_files=False, save_directory="")
quiet_backend.begin(quiet_plan, bake_settings)
check("关掉这个开关时什么都不藏",
      not obj_out_1.hide_get() and not obj_out_2.hide_get())
backend_tx2.commit()

# ------------------------------------------------------------------------------------
print("\n=== E. 采样 / AO 的提醒（第 2 页 Checklist）===")


def rows_for(settings):
    return uops.sample_rows(bpy.context, settings)


settings.samples_mode = 'LOW'
settings.sampled_maps_mode = 'LOW'
settings.maps.clear()
uops.add_map(settings, "shader.base_color", 32, dedupe=True)
labels = [text for _level, text in rows_for(settings)]
check("确定值通道 + 1 采样时不啰嗦（没有采样警告）",
      not any("deterministic" in text for text in labels), labels)

settings.samples_mode = 'HIGH'
labels = [text for _level, text in rows_for(settings)]
check("Bake Quality 调大时警告（用户要求）",
      any("deterministic" in text and "64" in text for text in labels), labels)

settings.samples_mode = 'LOW'
settings.maps.clear()
uops.add_map(settings, "standard.ao", 32, dedupe=True)
settings.sampled_maps_mode = 'MEDIUM'
labels = [(level, text) for level, text in rows_for(settings)]
check("AO + 默认 Medium(64) 是 OK 行",
      any(level == 'OK' and "sampled map" in text for level, text in labels), labels)
settings.sampled_maps_mode = 'LOW'
labels = [(level, text) for level, text in rows_for(settings)]
check("AO 采样偏低时警告（用户原话：AO 还是需要更高的采样）",
      any(level == 'WARN' and ("noisy" in text or "64 or more" in text)
          for level, text in labels), labels)

settings.maps.clear()
uops.add_map(settings, "misc.ao", 32, dedupe=True)
settings.hide_unrelated = True
labels = [(level, text) for level, text in rows_for(settings)]
check("AO 节点那条路要指出真正的旋钮是光线数",
      any(level == 'OK' and "rays per pixel" in text for level, text in labels), labels)
check("Hide Unrelated 开着时说明后果",
      any("stop casting shadows" in text for _level, text in labels), labels)
settings.maps[0].ao_samples = 4
labels = [(level, text) for level, text in rows_for(settings)]
check("AO 光线数偏低时警告",
      any(level == 'WARN' and "rays" in text for level, text in labels), labels)
settings.hide_unrelated = False

# ------------------------------------------------------------------------------------
print("\n=== F. 一次完整运行：护栏 / 阶段行 / 派发前重绘 ===")

active = usession.Session.get()
active.reset()
settings.target_mode = ss.MODE_SINGLE
settings.single_collection = "Baked"
settings.maps.clear()
uops.add_map(settings, "shader.base_color", 32, dedupe=True)
settings.auto_deliver = False
settings.output_dir = OUT

dispatched = []


def record_dispatch(task):
    dispatched.append(task.image_name)


# ⚠ 后端要**显式传**给 start()，不能用 session.backend_factory：
#   Session.start() 第一件事就是 self.reset()，而 reset() 会把 backend_factory
#   清成 None —— 于是"注入的后端"永远不生效，测试实际上在跑真的 Cycles
#   （这一轮就是被它咬到的：日志里冒出 3 次 "Baking map saved to internal image"）。
#   这是测试接缝的坑，不是产品行为；本轮先绕开，别顺手改掉它去影响别的套件。
started = active.start(bpy.context, settings,
                       backend=bk.NullBackend(save=True, directory=OUT))
check("运行起来了", started and active.job is not None)
check("用的确实是我们注入的后端（不是真 Cycles）",
      active.job.backend.name == "null", active.job.backend.name)
check("运行期间全局撤销被停掉（这就是那次 729 秒的防护）",
      PREFS.edit.use_global_undo is False, PREFS.edit.use_global_undo)
check("运行期间自动保存被停掉",
      PREFS.filepaths.use_auto_save_temporary_files is False)
active.job.on_before_dispatch = record_dispatch

active.run_blocking()
check("跑完了", active.job.is_finished and active.job.state == jb.STATE_DONE,
      active.job.state)
check("跑完之后全局撤销**还回来了**", PREFS.edit.use_global_undo is True)
check("跑完之后自动保存**还回来了**",
      PREFS.filepaths.use_auto_save_temporary_files is True)
check("每个任务派发前都调了一次回调",
      len(dispatched) == len(active.job.plan.tasks), len(dispatched))
check("没有留下借出的东西", not active.guard.engaged)

log_lines = [entry.message for entry in active.log]
check("日志里有阶段计时行",
      any("commit transaction: done in" in text for text in log_lines),
      [t for t in log_lines if "commit transaction" in t])
check("日志里有派发耗时（这就是卡在哪一张的答案）",
      any("dispatch " in text and "done in" in text for text in log_lines),
      [t for t in log_lines if "dispatch " in t][:2])
check("日志里写了本轮总耗时",
      any(text.startswith("Timing:") for text in log_lines),
      [t for t in log_lines if t.startswith("Timing:")])
check("日志里写了偏好设置被暂停 / 已还原",
      any("Paused for this run" in text for text in log_lines) and
      any("Put back" in text for text in log_lines),
      [t for t in log_lines if "Paused" in t or "Put back" in t])

# 落盘那份：用户唯一能事后发出来的东西
disk = os.path.join(usession.log_path())
with open(disk, encoding="utf-8", errors="replace") as handle:
    disk_text = handle.read()
check("阶段行进了落盘日志（不是只打控制台）",
      "commit transaction: done in" in disk_text and "Timing:" in disk_text)
check("落盘日志里有还回偏好的记录", "Put back" in disk_text)

# 状态行要给出"上一张多久 / 还剩多久"
check("状态行带剩余时间（有样本时）",
      active.job.eta_text() == "" or "to go" in active.job.eta_text(),
      active.job.eta_text())

# ------------------------------------------------------------------------------------
print("\n=== G. 取消 / reset 也会把偏好还回去 ===")

active.reset()
PREFS.edit.use_global_undo = True
PREFS.filepaths.use_auto_save_temporary_files = True
active.start(bpy.context, settings, backend=bk.NullBackend())
check("重新开始又借走了", PREFS.edit.use_global_undo is False)
active.cancel()
active.step_once()
check("取消之后也还回去了",
      active.job.is_finished and PREFS.edit.use_global_undo is True,
      (active.job.state, PREFS.edit.use_global_undo))

active.reset()
active.start(bpy.context, settings, backend=bk.NullBackend())
active.pause_preferences()
check("手动借走", PREFS.edit.use_global_undo is False)
active.reset()
check("reset() 会还回去", PREFS.edit.use_global_undo is True)

# 兜底：这一轮根本没走到收尾（模态被顶掉 / 注销）时，藏起来的物体也必须放出来
dangling_tx = txmod.SceneTransaction(bpy.context)
dangling_tx.capture()
dangling_tx.hide_objects([obj_out_2])
check("先藏一个", obj_out_2.hide_get())
active.transaction = dangling_tx
released = active.release_hidden_objects()
check("兜底会把藏起来的放出去（不然几百个物体永远藏着）",
      released == 1 and not obj_out_2.hide_get(), (released, obj_out_2.hide_get()))
dangling_tx.rollback()
active.transaction = None

active.reset()
active.start(bpy.context, settings, backend=bk.NullBackend())
check("借走准备注销", PREFS.edit.use_global_undo is False)
usession.Session.forget()
check("注销 / 换文件时也还回去了", PREFS.edit.use_global_undo is True)
check("Session.forget 之后旧实例的引用被丢掉", usession.Session._instance is None)

# 失败路径：后端直接抛异常
active = usession.Session.get()
active.reset()


class ExplodingBackend(bk.NullBackend):
    def dispatch(self, task):
        raise RuntimeError("boom")


active.start(bpy.context, settings, backend=ExplodingBackend())
active.run_blocking()
check("后端炸了也会收尾并还回偏好",
      active.job.is_finished and PREFS.edit.use_global_undo is True,
      (active.job.state, PREFS.edit.use_global_undo))

# ------------------------------------------------------------------------------------
print("\n=== H. 写文件的留痕（save_pre / save_post）===")

from material_bakery.ui import _save_handlers

handlers = dict(_save_handlers())
check("save_pre / save_post 都注册得上",
      "save_pre" in handlers and "save_post" in handlers, sorted(handlers))
registered = {name for name in ("save_pre", "save_post")
              if handlers.get(name) in getattr(bpy.app.handlers, name, [])}
check("register() 之后真的挂在 bpy.app.handlers 上", len(registered) == 2, registered)

sink_lines = []
phases.set_sink(sink_lines.append)
phases.start_run("save-unit")
save_path = os.path.join(OUT, "save_probe.blend")
bpy.ops.wm.save_as_mainfile(filepath=save_path)
phases.end_run()
phases.set_sink(None)
check("保存文件会在日志里留两行（开始 / 耗时）",
      sum(1 for line in sink_lines if "file write" in line) == 2,
      [line for line in sink_lines if "file write" in line])
check("留痕里有耗时数字",
      any(re.search(r"file write done in \d+\.\d\ds", line) for line in sink_lines),
      sink_lines)

# ------------------------------------------------------------------------------------
print("\n=== 收尾 ===")

PREFS.edit.use_global_undo = ORIGINAL_UNDO
PREFS.filepaths.use_auto_save_temporary_files = ORIGINAL_AUTOSAVE
usession.Session.forget()
phases.set_sink(None)

print("\n=== 结果 ===")
if FAIL:
    print("FAILED {} check(s):".format(len(FAIL)))
    for name in FAIL:
        print("  -", name)
else:
    print("ALL CHECKS PASSED")

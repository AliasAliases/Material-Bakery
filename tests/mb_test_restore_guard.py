"""收尾还原的三条规矩 —— 回归测试（用户 2026-09-25 的卡死）

现场：用户的 run 日志停在
    restore: engine -> RenderSettings.engine (was 'CYCLES', want 'CYCLES')
之后永远没有下文，Blender 无限期不响应，只能强杀。两次运行（07:03 / 07:09）
卡在**同一行**，同一段代码在 `--background` 里 0.0000 秒就跑完了。

所以这套盯三件事：
    ① 值一样 -> **一个字都不写**（那行是在把同一个值赋给自己，白付一次引擎重建）
    ② 值真的不一样 -> 排进推迟队列、由 timer 在 operator 退出后补上
    ③ **background 模式必须有同步排空**（那里 timer 不触发；只靠 timer 的话
       引擎就永远不还原，而 GUI 里根本不报错 —— 最难查的那种不一致）

⚠ 断言"没写"不能只看值（赋同一个值之后值当然还是一样的），所以这里用一个
   会数数的替身对象记录每一次 setattr，再配合 phases 的日志行。
"""
import os
import sys

import bpy

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WS not in sys.path:
    sys.path.insert(0, WS)

import material_bakery
from material_bakery import compat
from material_bakery.core import phases
from material_bakery.core import transaction as tx_mod
from material_bakery.engine import backend as bk

compat.set_silent(True)

FAIL = []


def check(label, ok, extra=""):
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", label,
                               ("  " + str(extra)) if extra else ""))
    if not ok:
        FAIL.append(label)


bpy.ops.wm.read_factory_settings(use_empty=True)
material_bakery.register()

context = bpy.context
scene = context.scene
bpy.ops.mesh.primitive_cube_add()
bpy.ops.object.light_add(type='SUN')

print("=== ① 值一样时必须一个字节都不写 ===")
tx = tx_mod.SceneTransaction(context)
tx.capture()
engine_now = scene.render.engine
tx._restore_write("engine", scene.render, "engine", engine_now)
check("赋值同一个引擎值之后，值当然没变（这条本身说明不了问题）",
      scene.render.engine == engine_now, scene.render.engine)
check("推迟队列是空的（同值不该排进队列）",
      tx_mod._PENDING == [], len(tx_mod._PENDING))

clock = phases.start_run("test")
tx_mod._PENDING.clear()
tx._restore_write("engine", scene.render, "engine", engine_now)
lines = " | ".join(clock.lines)
check("日志里明确写了 skipped（说明真的没动手，而不是写了没效果）",
      "skipped" in lines, lines[-160:])
phases.end_run()

print("\n=== ② 值不一样 -> 排进队列，不当场写 ===")
bake_margin_before = scene.render.bake.margin
tx2 = tx_mod.SceneTransaction(context)
tx2.capture()
scene.render.bake.margin = bake_margin_before + 7      # 模拟烘焙改过的值
tx2._restore_write("bake", scene.render.bake, "margin", bake_margin_before)
check("值确实还没被写回去（这就是'推迟'的意思）",
      scene.render.bake.margin == bake_margin_before + 7, scene.render.bake.margin)
check("它在推迟队列里", len(tx_mod._PENDING) == 1, len(tx_mod._PENDING))

print("\n=== ③ background 模式：commit 必须**同步**排空队列 ===")
check("确认这里就是 background（否则这套测试的前提不成立）",
      bool(bpy.app.background), bpy.app.background)
tx2.commit()
check("commit 之后 margin 已还原",
      scene.render.bake.margin == bake_margin_before, scene.render.bake.margin)
check("队列被排空了（没有留下没人执行的幽灵记录）",
      tx_mod._PENDING == [], len(tx_mod._PENDING))

print("\n=== ④ 真烘一次：引擎/采样必须还原（用户卡死那一行的真实路径）===")
from material_bakery.core import plan as pl                       # noqa: E402
from material_bakery.core import scene_scan as ss                 # noqa: E402
from material_bakery.engine import job as jb                      # noqa: E402

# ⚠ 采样数设成"跟默认不一样"，否则"已还原"这条断言是空转的：
#   烘焙把 samples 改成 1，还原回 1 跟没还原看起来一模一样。
scene.cycles.samples = 32
scene.cycles.use_denoising = True

engine_before = scene.render.engine
samples_before = scene.cycles.samples
denoise_before = scene.cycles.use_denoising
margin_before = scene.render.bake.margin

obj = next(o for o in bpy.data.objects if o.type == 'MESH')
material = bpy.data.materials.new("ProbeMat")
material.use_nodes = True
obj.data.materials.append(material)
if not obj.data.uv_layers:
    obj.data.uv_layers.new(name="UVMap")

# ⚠ 基线必须在**建完材质之后**取。第一版在创建材质之前算，拿到的是空字典，
#   于是"节点数没变"当然对不上 —— 那是测试的错，不是代码的错。
node_counts = {m.name: len(m.node_tree.nodes)
               for m in bpy.data.materials if m.use_nodes}

settings = pl.BakeSettings(
    target_mode=ss.MODE_COLLECTIONS,
    maps=pl.map_requests_from_pairs([("shader.base_color", 64)]),
    margin=4,
)
plan = pl.build_plan(context, settings)
check("有计划可烘", len(plan.tasks) > 0, len(plan.tasks))

tx3 = tx_mod.SceneTransaction(context, label="probe")
bakery = bk.CyclesBackend(tx3, margin=4, samples=1, sampled_samples=1,
                          save_files=False)
job = jb.BakeJob(context, plan, settings, backend=bakery, label="probe",
                 transaction=tx3)
clock = phases.start_run("probe")
job.run_to_completion()
check("烘焙跑完了（state=done）", job.state == jb.STATE_DONE, job.state)
# ⚠ "烘的过程中引擎真的是 CYCLES"只能从日志里看：收尾会把引擎还原回去，
#   所以烘完之后再去读 render.engine 是查不到的（第一版就是这么写歪的）。
check("烘焙过程中引擎真的被切到了 CYCLES（日志为证）",
      "CYCLES" in " | ".join(clock.lines),
      [line for line in clock.lines if "CYCLES" in line][:2])
phases.end_run()
check("引擎已还原", scene.render.engine == engine_before,
      (engine_before, scene.render.engine))
check("Cycles 采样已还原（烘的时候是 1，原来是 {}）".format(samples_before),
      scene.cycles.samples == samples_before,
      (samples_before, scene.cycles.samples))
check("降噪开关已还原", scene.cycles.use_denoising == denoise_before,
      (denoise_before, scene.cycles.use_denoising))
check("烘焙 margin 已还原", scene.render.bake.margin == margin_before,
      (margin_before, scene.render.bake.margin))
check("推迟队列是空的（没有留下没人执行的幽灵记录）",
      tx_mod._PENDING == [], len(tx_mod._PENDING))
now_counts = {m.name: len(m.node_tree.nodes)
              for m in bpy.data.materials if m.use_nodes}
check("材质节点数没变（临时烘焙节点已清掉）", now_counts == node_counts,
      (node_counts, now_counts))
check("烘焙目标节点没留在材质里",
      all("MBakery Bake" not in [n.label for n in m.node_tree.nodes]
          for m in bpy.data.materials if m.use_nodes),
      [(m.name, [n.label for n in m.node_tree.nodes])
       for m in bpy.data.materials if m.use_nodes])

print("\n=== 结果 ===")
if FAIL:
    print("FAILED {} check(s):".format(len(FAIL)))
    for name in FAIL:
        print("  -", name)
else:
    print("ALL CHECKS PASSED")

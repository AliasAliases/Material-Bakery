# Probe: run a REAL Cycles bake over Prop Creation.blend in the background.
#
# engine/job.py's _step_finish() -> tx.commit() is the exact code path whose
# "restore: engine + bake settings" phase never reported "done" in the user's
# run log.  The restored-writes probe ran clean in background, but it did not do
# a bake first -- no Cycles session, no baked images, no committed transaction.
# This one does everything a GUI run does except the event loop.
#
# Usage:
#   blender --background --factory-startup "<repo>\_samples\Prop Creation.blend" \
#       --python tools/probe_real_bake_prop.py
#
# ⚠ --factory-startup 是必须的：不加的话 Blender 会先加载 %APPDATA% 那份已安装插件，
#   同名模块遮蔽 sys.path，探针 import 到的就不是工作树里的代码（踩过）。
#
# Watch the tail: with the per-line instrumentation in core/transaction.py, the
# last "restore: ..." line in the output says exactly which write is slow/stuck.

import os
import sys
import time

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import material_bakery
from material_bakery.core import scene_scan as ss
from material_bakery.core import plan as pl


def say(message):
    print("[{:7.2f}] {}".format(time.time() - STARTED, message))
    sys.stdout.flush()


STARTED = time.time()

say("=" * 78)
say("real bake over the user's file")
say("file: {}".format(bpy.data.filepath))
say("blender {}".format(bpy.app.version_string))
say("background={}".format(bpy.app.background))
say("=" * 78)

material_bakery.register()

context = bpy.context
scene = context.scene
say("scene={!r} view_layer={!r} objects in view layer={}".format(
    scene.name, context.view_layer.name, len(context.view_layer.objects)))
say("engine={!r}".format(scene.render.engine))

# --- what the planner sees -------------------------------------------------
groups, conflicts = ss.scan(scene)
for group in groups:
    say("group {!r}: skipped={} reason={!r} objects={} excluded={}".format(
        group.name, group.skipped, group.reason,
        [o.name for o in group.objects], group.excluded))
say("conflicts: {}".format(conflicts))
say("objects in the active view layer: {}".format(
    [o.name for o in context.view_layer.objects]))

# --- the same plan shape as the user's run: a few groups x 4 maps -----------
settings = pl.BakeSettings(
    target_mode=ss.MODE_COLLECTIONS,
    maps=pl.map_requests_from_pairs([
        ("shader.base_color", 256),
        ("shader.metallic", 256),
        ("shader.roughness", 256),
        ("shader.normal", 256),
    ]),
    margin=4,
    hide_unrelated=True,
)
plan = pl.build_plan(context, settings)
say("tasks: {}".format(len(plan.tasks)))
for task in plan.tasks:
    say("  {} -> {}".format(task.image_name, [o.name for o in task.targets]))
say("plan warnings: {}".format(list(plan.warnings)))
say("plan skipped: {}".format(list(plan.skipped)))

if not plan.tasks:
    say("!! the planner produced no tasks -- nothing to bake, stopping here")
    say("probe done (no bake)")
    sys.exit(0)

# --- real bake, same objects as the GUI modal operator drives ---------------
from material_bakery.core import phases
from material_bakery.core import transaction as txmod
from material_bakery.engine import backend as bk
from material_bakery.engine import job as jb

phases.set_sink(say)
phases.start_run("probe")

tx = txmod.SceneTransaction(context, label="probe")
backend = bk.CyclesBackend(tx, margin=4, samples=1, sampled_samples=4,
                           save_files=False)
job = jb.BakeJob(context, plan, settings, backend=backend, label="probe",
                 transaction=tx)

say("--- baking (this is the part that freezes in the GUI) ---")
job.run_to_completion()
phases.end_run(job.report.summary())

say("--- result: state={} done={} failed={} skipped={} ---".format(
    job.state, job.report.done, job.report.failed, job.report.skipped))
for line in job.report.details():
    say("  " + line)
say("engine after restore: {!r}".format(scene.render.engine))
say("cycles.samples after restore: {}".format(getattr(scene.cycles, "samples", "?")))
say("probe finished, file NOT saved")

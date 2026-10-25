# Probe: reproduce the "freeze after every bake" IN THE GUI.
#
# What we know (2026-09-25):
#   * The run log's last line is always
#         [wizard +4.81s | +0.00s] restore: engine + bake settings: start
#     with no matching ": done" -- so the freeze is inside those ~20 writes in
#     core/transaction.py::_restore_scene.
#   * A real bake in --background does NOT freeze (4 maps, 0.8 s, clean finish).
#     So it needs the GUI: an event loop, viewports, a window.
#   * `core/transaction.py` now writes one line per write:
#         restore: engine -> RenderSettings.engine
#         restore: cycles -> CyclesRenderSettings.device
#     so the last "restore: ..." line in the output names the culprit exactly.
#
# This runs the same pipeline the wizard runs (ui/session.py Session -> job ->
# CyclesBackend -> tx.commit) but drives it with a timer instead of the modal
# operator, so progress lands on disk frame by frame.
#
# Usage (GUI, NOT --background, and NOT --factory-startup so Cycles exists):
#     blender "_samples\Prop Creation.blend"
#     Scripting workspace -> Open -> tools/probe_gui_bake.py -> Run Script
#
# Then read _probe/probe_gui_bake.txt. If Blender stops responding, press ESC.

import os
import sys
import time
import traceback

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

OUT_PATH = os.path.join(REPO, "_probe", "probe_gui_bake.txt")
STARTED = time.time()
_handle = None


def say(message, durable=False):
    global _handle
    line = "[{:8.2f}] {}".format(time.time() - STARTED, message)
    print(line)
    try:
        if _handle is None:
            _handle = open(OUT_PATH, "w", encoding="utf-8", buffering=1)
        _handle.write(line + "\n")
        _handle.flush()
        if durable:
            os.fsync(_handle.fileno())
    except (OSError, ValueError):
        _handle = None


say("=" * 78)
say("GUI bake probe -- this one is supposed to reproduce the freeze")
say("blender {}  background={}".format(bpy.app.version_string, bpy.app.background))
say("file: {}".format(bpy.data.filepath))
say("output: {}".format(OUT_PATH))
say("=" * 78)

if bpy.app.background:
    say("!! running in background: this will NOT reproduce the freeze, and the")
    say("!! timer will never fire (there is no event loop), so nothing else will")
    say("!! be printed. Open Blender WITH A WINDOW and run this from the")
    say("!! Scripting workspace instead.")

import material_bakery                                     # noqa: E402
from material_bakery.core import phases                    # noqa: E402
from material_bakery.ui import session as session_mod      # noqa: E402

say("addon loaded from: {}".format(material_bakery.__file__))

context = bpy.context
scene = context.scene

say("scene={!r} view_layer={!r}".format(scene.name, context.view_layer.name))
say("engine={!r}  cycles.samples={}".format(
    scene.render.engine, getattr(scene.cycles, "samples", "?")))
say("objects in the active view layer: {}".format(
    [o.name for o in context.view_layer.objects]))
say("window: {}  screen: {!r}".format(
    bool(context.window), getattr(context.window, "screen", None) is not None))

# --- build the settings the wizard actually uses: scene.mbakery -------------
#
# ⚠ Session.start() takes the *UI* settings object (ui/properties.py
#   MaterialBakerySettings) and compiles them through settings_to_bake_settings.
#   Handing it a core plan.BakeSettings dies with
#   "'BakeSettings' object has no attribute 'map_requests'".
material_bakery.register()          # idempotent; makes scene.mbakery exist
scene = context.scene
settings = getattr(scene, "mbakery", None)
if settings is None:
    say("!! scene.mbakery is missing even after register() -- cannot continue")
    sys.exit(0)

settings.maps.clear()
for key in ("shader.base_color", "shader.metallic", "shader.roughness",
            "shader.normal"):
    item = settings.maps.add()
    item.type_key = key
    item.size_x = 512
    item.size_y = 512
settings.auto_deliver = False
settings.pack_into_blend = True
settings.fake_user = True
if hasattr(settings, "hide_unrelated"):
    settings.hide_unrelated = True
say("settings: {} map(s), target_mode={!r}, hide_unrelated={!r}, auto_deliver={!r}".format(
    len(settings.maps), settings.target_mode,
    getattr(settings, "hide_unrelated", "n/a"), settings.auto_deliver))

phases.set_sink(say)
session = session_mod.Session.get()
started = session.start(context, settings)
say("session.start -> {}".format(started))
if not started:
    say("!! nothing to bake: {}".format(session.error))
    say("probe ended without baking")
    sys.exit(0)

say("plan: {} task(s), {} group(s)".format(
    len(session.job.plan.tasks), len(session.job.plan.groups)))
for task in session.job.plan.tasks:
    say("  {} -> {}".format(task.image_name, [o.name for o in task.targets]))
say("--- now driving the run with a timer, like the modal operator does ---",
    durable=True)

state = {"ticks": 0, "worst": 0.0, "last": time.time()}


def tick():
    """Same thing ui/session.Session.tick does, plus per-tick timing."""
    now = time.time()
    delta = now - state["last"]
    state["last"] = now
    if delta > state["worst"]:
        state["worst"] = delta
    state["ticks"] += 1
    if delta > 1.0:
        say("!! a single step took {:.2f}s (worst so far {:.2f}s)".format(
            delta, state["worst"]), durable=True)

    try:
        session.step_once()
    except Exception as exc:
        say("!! step_once raised: {}: {}".format(type(exc).__name__, exc))
        say(traceback.format_exc())
        return None

    if session.job is None or session.job.is_finished:
        say("--- run finished after {} tick(s), worst single step {:.2f}s ---".format(
            state["ticks"], state["worst"]))
        say("engine after restore: {!r}".format(scene.render.engine))
        say("cycles.samples after restore: {}".format(
            getattr(scene.cycles, "samples", "?")))
        say("report: {}".format(session.report.summary() if session.report else "none"))
        if session.report is not None:
            for line in session.report.details():
                say("  " + line)
        say("probe finished, nothing was saved")
        return None
    return 0.05


say("registering timer...", durable=True)
bpy.app.timers.register(tick, first_interval=0.05)
say("timer registered -- if the next line never appears again, that is the freeze",
    durable=True)

# Probe: which single assignment inside SceneTransaction._restore_scene hangs?
#
# The user's run log stops on exactly one line:
#     [wizard +4.81s | +0.00s] restore: engine + bake settings: start
# with no matching "...: done in Xs" after it.  That phase is transaction.py
# lines 231-252, about twenty property writes.  The previous round of guessing
# ("it must be the auto-save") cost the user 729 seconds of frozen Blender, so
# this probe does not guess: it writes every step to a flushed file, so whichever
# line is last in that file IS the one that hangs.
#
# Usage (GUI -- this REPRODUCES the freeze, and that is the point):
#     blender "C:\Users\Admin\Desktop\JNU Map\Prop Creation.blend"
#     Scripting workspace -> Open -> tools/probe_restore_freeze.py -> Run Script
#
# Then read  _probe\probe_restore_freeze.txt  (and press ESC if it hangs).
#
# Background equivalent (will NOT reproduce the freeze, only the timings):
#     blender --background "the.blend" --python tools/probe_restore_freeze.py

import os
import sys
import time
import traceback

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OUT_PATH = os.path.join(REPO, "_probe", "probe_restore_freeze.txt")
if REPO not in sys.path:
    sys.path.insert(0, REPO)

STARTED = time.time()
_handle = None
LINES = []


# --------------------------------------------------------------------------- io
def say(message, durable=False):
    """timestamp + relative time + the message. Flushed on every line.

    durable=True also fsyncs -- a hard freeze can leave the tail of the file in
    the OS cache, and the tail is the whole point of this probe.
    """
    global _handle
    line = "[{:8.2f}] {}".format(time.time() - STARTED, message)
    LINES.append(line)
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


def step(label, call, durable=True):
    """Run one property write, and say so both before and after it."""
    say("  BEGIN  {}".format(label), durable=durable)
    started = time.time()
    try:
        value = call()
    except Exception as exc:
        say("  RAISED {}: {}: {}".format(label, type(exc).__name__, exc), durable=durable)
        return None
    say("  OK     {} = {!r}  ({:.4f}s)".format(label, value, time.time() - started),
        durable=durable)
    return value


say("=" * 78)
say("restore-freeze probe")
say("blender {}  background={}".format(bpy.app.version_string, bpy.app.background))
say("file: {}".format(bpy.data.filepath))
say("output: {}".format(OUT_PATH))
say("=" * 78)

context = bpy.context
scene = context.scene
render = scene.render
say("active scene: {!r}  view layer: {!r}".format(scene.name, context.view_layer.name))

# ---------------------------------------------------------------- phase 1: view layer
say("")
say("--- PHASE 1: view layer membership (the '.phy' error) ---")
names = ("ShuiMa", "ShuiMa.phy", "3mShuiMa.phy")
for name in names:
    obj = bpy.data.objects.get(name)
    if obj is None:
        say("{!r}: NOT in this .blend".format(name))
        continue
    in_layer = any(o.name == name for o in context.view_layer.objects)
    collections = [c.name for c in obj.users_collection]
    layers_of_scene = []
    for vl in scene.view_layers:
        present = any(o.name == name for o in vl.objects)
        layers_of_scene.append("{}={}".format(vl.name, present))
    say("{!r}: type={} hidden={} in_active_view_layer={}".format(
        name, obj.type, obj.hide_get(), in_layer))
    say("        users_collection={}".format(collections))
    say("        per view layer: {}".format(", ".join(layers_of_scene)))
    try:
        member = scene.collection in obj.users_collection
        say("        directly in master collection: {}".format(member))
    except Exception as exc:
        say("        master collection check raised: {}".format(exc))
for vl in scene.view_layers:
    say("view layer {!r}: {} objects".format(vl.name, len(vl.objects)))
say("scene collections (recursive), with this view layer's exclude state:")


def layer_states(layer_collection, table):
    """{collection: layer_collection} for the whole layer tree."""
    table[layer_collection.collection] = layer_collection
    for child in layer_collection.children:
        layer_states(child, table)


table = {}
layer_states(context.view_layer.layer_collection, table)

stack = [(scene.collection, 0)]
while stack:
    collection, depth = stack.pop(0)
    layer_collection = table.get(collection)
    if collection is scene.collection:
        state = "master"
    elif layer_collection is None:
        state = "NOT in this view layer's tree"
    elif layer_collection.exclude:
        state = "EXCLUDED (bake cannot touch its objects)"
    elif layer_collection.hide_viewport:
        state = "hidden (eye off)"
    else:
        state = "visible"
    say("  {}{!r} [{}] {} direct object(s), {} child(ren)".format(
        "  " * depth, collection.name, state,
        len(collection.objects), len(collection.children)))
    for child in collection.children:
        stack.append((child, depth + 1))

# ---------------------------------------------------------------- phase 2: snapshot
say("")
say("--- PHASE 2: capture the exact snapshot core/transaction.py makes ---")
engine_before = render.engine
say("engine BEFORE = {!r}".format(engine_before))

try:
    from material_bakery.core import transaction as transaction_mod
    say("imported material_bakery.core.transaction OK")
except Exception as exc:
    say("IMPORT FAILED: {}: {}".format(type(exc).__name__, exc))
    traceback.print_exc()
    sys.exit(1)

tx = transaction_mod.SceneTransaction(context, label="probe")
say("capturing a real transaction (this is what the run does at start)...")
step("SceneTransaction.capture()", lambda: (tx.capture(), len(tx._snapshot))[1])
say("snapshot keys: {}".format(sorted(tx._snapshot.keys())))
say("snapshot engine  = {!r}".format(tx._snapshot.get("engine")))
say("snapshot bake    = {}".format(tx._snapshot.get("bake")))
say("snapshot bake_image_settings = {}".format(tx._snapshot.get("bake_image_settings")))
say("snapshot color_management    = {}".format(tx._snapshot.get("color_management")))
say("snapshot cycles  = {}".format(tx._snapshot.get("cycles")))

# ---------------------------------------------------------------- phase 2b: cycles
say("")
say("--- PHASE 2b: switch to CYCLES, like engine/backend.py begin() does ---")
say("The restore only matters because the bake left the engine on CYCLES.")
say("(Run this from the GUI, not with --factory-startup: factory startup does not")
say(" register the Cycles addon at all, and then 'CYCLES' is silently ignored.)")
engines_now = [item.identifier for item in
               render.bl_rna.properties["engine"].enum_items]
say("engines available before the switch: {}".format(engines_now))
if 'CYCLES' not in engines_now:
    say("!! CYCLES is NOT registered in this session -- the engine switch below")
    say("!! cannot be reproduced here. Re-run from a normal Blender GUI session.")
step("render.engine = 'CYCLES' (what the bake leaves behind)",
     lambda: (setattr(render, "engine", 'CYCLES'), render.engine)[1])
say("engine is now {!r} -- restoring from here is the real scenario".format(render.engine))

# ---------------------------------------------------------------- phase 3: replay
say("")
say("--- PHASE 3: replay _restore_scene's writes, one line at a time ---")
say("This is the exact block whose 'done' line never made it to the run log.")
say("Watch this file: the last BEGIN without a matching OK is the culprit.")
say("")

say("[3a] engine -> snapshot value (line 236)")
say("     engine entering this line: {!r}".format(render.engine))
step("render.engine = {!r}".format(tx._snapshot.get("engine", engine_before)),
     lambda: (setattr(render, "engine", tx._snapshot.get("engine", engine_before)),
              render.engine)[1])
say("checkpoint: engine is {!r}, still alive".format(render.engine))
if render.engine == 'CYCLES':
    say("!! the engine switch did not take effect (CYCLES is not registered)")
else:
    say("   the switch really happened: CYCLES -> {!r}".format(render.engine))

say("")
say("[3b] render.bake.* writes (lines 239-241)")
for name, value in tx._snapshot.get("bake", {}).items():
    if not hasattr(render.bake, name):
        say("  SKIP   render.bake.{} (not in this build)".format(name))
        continue
    step("render.bake.{} = {!r}".format(name, value),
         lambda n=name, v=value: (setattr(render.bake, n, v), getattr(render.bake, n))[1])

say("")
say("[3c] render.bake.image_settings.* writes (lines 242-245)")
bake_image = getattr(render.bake, "image_settings", None)
if bake_image is None:
    say("  SKIP   no render.bake.image_settings in this build")
else:
    for name, value in tx._snapshot.get("bake_image_settings", {}).items():
        step("image_settings.{} = {!r}".format(name, value),
             lambda n=name, v=value: (setattr(bake_image, n, v), getattr(bake_image, n))[1])

say("")
say("[3d] scene.view_settings.* writes (lines 246-247)")
for name, value in tx._snapshot.get("color_management", {}).items():
    step("view_settings.{} = {!r}".format(name, value),
         lambda n=name, v=value: (setattr(scene.view_settings, n, v),
                                  getattr(scene.view_settings, n))[1])

say("")
say("[3e] scene.cycles.* writes (lines 248-251)")
cycles = getattr(scene, "cycles", None)
if cycles is None:
    say("  SKIP   no scene.cycles in this build")
else:
    for name, value in tx._snapshot.get("cycles", {}).items():
        step("cycles.{} = {!r}".format(name, value),
             lambda n=name, v=value: (setattr(cycles, n, v), getattr(cycles, n))[1])

say("")
say("[3f] the rest of _restore_scene (selection, line 255)")
step("restore_selection()", lambda: (tx.restore_selection(), "done")[1])

# ---------------------------------------------------------------- phase 4: engine round trip
say("")
say("--- PHASE 4: engine round trip in isolation ---")
say("If PHASE 3 says the engine line is fine but PHASE 5 says a forced update")
say("hangs, the cost is in the depsgraph and shows up later. This times the")
say("switch itself, both directions, so 'EEVEE is slow to recompile' is a number")
say("instead of a theory.")
engines = [item.identifier for item in
           render.bl_rna.properties["engine"].enum_items]
say("engines available: {}".format(engines))
for target in [e for e in engines if e != 'CYCLES']:
    step("render.engine = {!r} (leave CYCLES)".format(target),
         lambda t=target: (setattr(render, "engine", t), render.engine)[1])
    step("render.engine back to 'CYCLES'",
         lambda: (setattr(render, "engine", 'CYCLES'), render.engine)[1])
step("render.engine back to the snapshot value {!r}".format(engine_before),
     lambda t=engine_before: (setattr(render, "engine", t), render.engine)[1])

# ---------------------------------------------------------------- phase 5: forced update
say("")
say("--- PHASE 5: does a forced depsgraph update / redraw hang? ---")
step("view_layer.update()", lambda: (context.view_layer.update(), "done")[1])
try:
    step("wm.redraw_timer(DRAW_WIN_SWAP)", lambda: bpy.ops.wm.redraw_timer(
        type='DRAW_WIN_SWAP', iterations=1).__str__())
except Exception as exc:
    say("  redraw_timer raised (fine in background): {}".format(exc))

# ---------------------------------------------------------------- report
say("")
say("--- probe finished ---")
say("total {:.2f}s".format(time.time() - STARTED))
say("file was NOT saved, so nothing in the .blend changed.")
if _handle is not None:
    _handle.close()

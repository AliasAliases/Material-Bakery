# Probe: preference names, save handlers, foreach_get cost, redraw_timer.
#
# Everything this optimization round relies on gets verified here FIRST -- the
# whole point of the round is "the user's Blender froze for 729 s", and a guess
# about property names would just add another invisible failure.
#
# Usage: blender --background --factory-startup --python tools/probe_run_prefs.py

import array
import os
import sys
import tempfile
import time

import bpy

OUT = []


def say(message):
    OUT.append(str(message))
    print(message)


say("blender {}".format(bpy.app.version_string))

# ---------------------------------------------------------------- preferences
prefs = bpy.context.preferences
say("")
say("--- preferences.edit ---")
for name in dir(prefs.edit):
    if "undo" in name.lower():
        try:
            say("  edit.{} = {!r}".format(name, getattr(prefs.edit, name)))
        except Exception as exc:
            say("  edit.{} <{}>".format(name, exc))

say("")
say("--- preferences.filepaths (save/auto) ---")
for name in dir(prefs.filepaths):
    if "save" in name.lower() or "auto" in name.lower():
        try:
            say("  filepaths.{} = {!r}".format(name, getattr(prefs.filepaths, name)))
        except Exception as exc:
            say("  filepaths.{} <{}>".format(name, exc))

say("")
say("--- writable? ---")
for path, attr, value in (("edit", "use_global_undo", False),
                          ("filepaths", "use_auto_save_temporary_files", False)):
    section = getattr(prefs, path)
    if not hasattr(section, attr):
        say("  {}.{}: MISSING".format(path, attr))
        continue
    before = getattr(section, attr)
    try:
        setattr(section, attr, value)
        after = getattr(section, attr)
        setattr(section, attr, before)
        say("  {}.{}: {} -> {} -> {} OK".format(path, attr, before, after,
                                                getattr(section, attr)))
    except Exception as exc:
        say("  {}.{}: NOT WRITABLE ({})".format(path, attr, exc))

# ---------------------------------------------------------------- save handlers
say("")
say("--- save handlers ---")
events = []


def _pre(*args, **kwargs):
    events.append(("pre", args, tuple(sorted(kwargs))))


def _post(*args, **kwargs):
    events.append(("post", args, tuple(sorted(kwargs))))


bpy.app.handlers.save_pre.append(_pre)
bpy.app.handlers.save_post.append(_post)
path = os.path.join(tempfile.gettempdir(), "mbakery_probe_save.blend")
try:
    bpy.ops.wm.save_as_mainfile(filepath=path)
except Exception as exc:
    say("  save failed: {}".format(exc))
for entry in events:
    say("  fired {} args={} kwargs={}".format(entry[0], entry[1], entry[2]))
say("  save_pre fires for a normal save: {}".format(bool(events)))
try:
    os.remove(path)
except OSError:
    pass

# ---------------------------------------------------------------- foreach_get
say("")
say("--- polygon material_index traversal ---")
bpy.ops.mesh.primitive_grid_add(x_subdivisions=400, y_subdivisions=400)
obj = bpy.context.active_object
mesh = obj.data
count = len(mesh.polygons)
say("  polygons: {}".format(count))

started = time.time()
values = [poly.material_index for poly in mesh.polygons]
loop_seconds = time.time() - started
say("  python loop read:  {:.3f}s".format(loop_seconds))

buffer = array.array("i", [0]) * count
started = time.time()
mesh.polygons.foreach_get("material_index", buffer)
get_seconds = time.time() - started
say("  foreach_get(into array('i')): {:.4f}s  values[0]={} len={}".format(
    get_seconds, buffer[0], len(buffer)))

started = time.time()
mesh.polygons.foreach_set("material_index", array.array("i", [3]) * count)
set_seconds = time.time() - started
say("  foreach_set(array('i')):      {:.4f}s  now {}".format(
    set_seconds, mesh.polygons[0].material_index))

say("  speedup read: x{:.0f}".format(loop_seconds / max(get_seconds, 1e-6)))

# single-slot shortcut: 1 slot means every index is 0 by definition
obj.data.materials.append(bpy.data.materials.new("probe"))
say("  slots: {}".format(len(obj.material_slots)))

# ---------------------------------------------------------------- redraw_timer
say("")
say("--- redraw_timer ---")
say("  bpy.ops.wm.redraw_timer exists: {}".format(hasattr(bpy.ops.wm, "redraw_timer")))
try:
    result = bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
    say("  background call returned: {}".format(result))
except Exception as exc:
    say("  background call raised: {}: {}".format(type(exc).__name__, exc))

# ---------------------------------------------------------------- report
say("")
say("--- probe done ---")
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "probe_run_prefs.txt"), "w", encoding="utf-8") as handle:
    handle.write("\n".join(OUT) + "\n")
sys.stdout.flush()

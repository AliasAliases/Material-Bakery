# Probe: does toggling use_global_undo throw away the undo history?
#
# The run guard pauses global undo while baking. Before claiming "the preference
# is restored", this checks what Blender itself does to the stack on the way
# through -- if the toggle wipes it, that has to be said out loud.
#
# Usage: blender --background --factory-startup --python tools/probe_undo_toggle.py

import bpy


def names():
    return sorted(o.name for o in bpy.data.objects)


print("blender", bpy.app.version_string)
print("start:", names())

bpy.ops.mesh.primitive_cube_add()
print("after cube 1:", names())
try:
    print("  undo_push 1:", bpy.ops.ed.undo_push(message="one"))
except Exception as exc:
    print("  undo_push 1 raised:", exc)

bpy.ops.mesh.primitive_cylinder_add()
print("after cylinder:", names())
try:
    print("  undo_push 2:", bpy.ops.ed.undo_push(message="two"))
except Exception as exc:
    print("  undo_push 2 raised:", exc)

print("undo before toggle:", getattr(bpy.ops.ed.undo, "poll", lambda: "?")())

prefs = bpy.context.preferences
print("use_global_undo was", prefs.edit.use_global_undo)
prefs.edit.use_global_undo = False
prefs.edit.use_global_undo = True
print("use_global_undo now", prefs.edit.use_global_undo)

try:
    result = bpy.ops.ed.undo()
    print("undo after toggle:", result)
except Exception as exc:
    print("undo after toggle raised: {}: {}".format(type(exc).__name__, exc))
print("after undo:", names())

# Also: what does the save preference actually gate? Show the value that decides
# whether a write happens at all.
print("auto_save_time:", prefs.filepaths.auto_save_time)
print("use_auto_save_temporary_files:", prefs.filepaths.use_auto_save_temporary_files)

# And confirm the guard class itself round-trips on this build.
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from material_bakery.core import guard as guard_mod

lock = guard_mod.PreferenceGuard()
print("engage:", lock.engage(bpy.context))
print("  describe:", lock.describe())
print("  undo:", prefs.edit.use_global_undo,
      " autosave:", prefs.filepaths.use_auto_save_temporary_files)
print("engage again:", lock.engage(bpy.context))
print("release:", lock.release())
print("  undo:", prefs.edit.use_global_undo,
      " autosave:", prefs.filepaths.use_auto_save_temporary_files)
print("release again:", lock.release())
for level, message in lock.drain_messages():
    print("  [{}] {}".format(level, message))
print("probe done")

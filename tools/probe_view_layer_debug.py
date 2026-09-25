# Debug: read the layer collection tree's exclude flags for this file.
#
# ⚠ This must be run with the WORKSPACE copy of the addon. Blender registers the
#   one in the user's addons folder at startup under the same module name, and
#   it then shadows sys.path entirely -- which is exactly how the first version
#   of this probe failed with "has no attribute objects_in_view_layer" while
#   looking like a code bug. --factory-startup avoids loading it.
import os
import sys

import bpy

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WS not in sys.path:
    sys.path.insert(0, WS)

from material_bakery.core import scene_scan as ss

print("module file:", ss.__file__)

context = bpy.context
view_layer = context.view_layer

print("context.view_layer:", view_layer.name)
print("view_layer.objects:", [o.name for o in view_layer.objects])
print("has objects_in_view_layer:", hasattr(ss, "objects_in_view_layer"))

names = ss.objects_in_view_layer(view_layer)
print("objects_in_view_layer count:", len(names))
print("  sorted:", sorted(names))


def walk(node, depth=0):
    print("{}{!r} exclude={} objects={}".format(
        "  " * depth, node.collection.name, node.exclude,
        [o.name for o in node.collection.objects]))
    for child in node.children:
        walk(child, depth + 1)


print("--- layer collection tree ---")
walk(view_layer.layer_collection)

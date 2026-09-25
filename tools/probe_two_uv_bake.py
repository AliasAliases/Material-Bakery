"""Probe: how does the texture actually get from UV1 onto a NEW UV2?

Two experiments in one file, both with real Cycles bakes and pixel checks.

A) SAME OBJECT, two UV layers. Material's Texture node is fed by an explicit
   UVMap node set to "UV1"; "UV2" is active_render (the write layer).
   Source image: R = u, G = v (the sampled colour encodes the coordinate used).
   -> If the result reads ~0.2/0.2 (the UV1 coord of that point) the sampling
      really used UV1 and one object CAN cross-bake.
   -> If it reads ~0.8/0.8 (the coordinate where it wrote) then sampling is
      pinned to the render layer and a single object cannot cross-bake.

B) TWO OBJECTS, the plugin's own projection path: a visible target carrying the
   new UV2 plus a hidden source carrying UV1 and the texture. Same-space
   surfaces, so this also answers "does the ray hit itself?".
   -> Reads ~0.2/0.2 in UV2 space => the two-object route works.

Usage:
    blender --background --factory-startup --python tools/probe_two_uv_bake.py
"""

import os
import sys

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OUT = os.path.join(REPO, "_probe")


def say(message):
    print("[two-uv] {}".format(message))
    sys.stdout.flush()


def make_plane(name, collection, uv_lo, uv_hi):
    """A unit plane whose UV layer sits in the [uv_lo, uv_hi] quarter."""
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)], [],
                     [(0, 1, 2, 3)])
    mesh.update()
    layer = mesh.uv_layers.new(name="UV")
    for i, uv in enumerate(((uv_lo, uv_lo), (uv_hi, uv_lo),
                            (uv_hi, uv_hi), (uv_lo, uv_hi))):
        layer.data[i].uv = uv
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    return obj


def gradient_image(name, size=64):
    """R = u, G = v -- a colour that encodes where it was sampled."""
    image = bpy.data.images.new(name, size, size, alpha=False)
    pixels = [0.0, 0.0, 0.0, 1.0] * (size * size)
    for y in range(size):
        for x in range(size):
            index = (y * size + x) * 4
            pixels[index:index + 3] = [(x + 0.5) / size, (y + 0.5) / size, 0.0]
    image.pixels.foreach_set(pixels)
    image.update()
    return image


def textured_material(name, image, uv_layer):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    tree = material.node_tree
    for node in list(tree.nodes):
        tree.nodes.remove(node)
    output = tree.nodes.new('ShaderNodeOutputMaterial')
    principled = tree.nodes.new('ShaderNodeBsdfPrincipled')
    texture = tree.nodes.new('ShaderNodeTexImage')
    texture.image = image
    uvm = tree.nodes.new('ShaderNodeUVMap')
    uvm.uv_map = uv_layer
    tree.links.new(uvm.outputs['UV'], texture.inputs['Vector'])
    tree.links.new(texture.outputs['Color'], principled.inputs['Base Color'])
    tree.links.new(principled.outputs['BSDF'], output.inputs['Surface'])
    return material


def sample(image, u, v):
    x = min(image.size[0] - 1, max(0, int(u * image.size[0])))
    y = min(image.size[1] - 1, max(0, int(v * image.size[1])))
    index = (y * image.size[0] + x) * 4
    return tuple(round(value, 3) for value in image.pixels[index:index + 3])


def fresh_image(name, size=64):
    image = bpy.data.images.new(name, size, size, alpha=False)
    image.pixels.foreach_set([0.0, 0.0, 0.0, 1.0] * (size * size))
    image.update()
    return image


def set_bake_target(obj, image):
    """Install the active image node the bake writes into."""
    tree = obj.data.materials[0].node_tree
    node = tree.nodes.new('ShaderNodeTexImage')
    node.image = image
    for other in tree.nodes:
        other.select = False
    node.select = True
    tree.nodes.active = node


def make_active(obj, also=()):
    """Select obj (+also) and make obj active -- headless bake needs both.

    ⚠ Forgetting this is how the first run reported
      "No valid selected objects" and looked like a plugin limitation.
    """
    bpy.ops.object.select_all(action='DESELECT')
    for candidate in (obj,) + tuple(also):
        candidate.select_set(True)
    bpy.context.view_layer.objects.active = obj
    context = bpy.context
    say("    active={!r} selected={} in_view_layer={} hide_get={} hide_render={}".format(
        context.view_layer.objects.active.name if context.view_layer.objects.active else None,
        sorted(o.name for o in context.selected_objects),
        obj.name in {o.name for o in context.view_layer.objects},
        obj.hide_get(), obj.hide_render))


def experiment_a(scene):
    say("--- A) same object, two UV layers (explicit UVMap node on UV1) ---")
    collection = bpy.data.collections.new("A")
    scene.collection.children.link(collection)
    obj = make_plane("Single", collection, 0.10, 0.30)      # UV1 quarter
    mesh = obj.data
    uv2 = mesh.uv_layers.new(name="UV2")
    for i, uv in enumerate(((0.70, 0.70), (0.90, 0.70), (0.90, 0.90), (0.70, 0.90))):
        uv2.data[i].uv = uv
    mesh.uv_layers["UV"].name = "UV1"
    mesh.materials.append(textured_material("MatA", gradient_image("SrcA"), "UV1"))
    for layer in mesh.uv_layers:
        layer.active_render = (layer.name == "UV2")
    mesh.uv_layers.active_index = mesh.uv_layers.find("UV2")
    say("A: layers = {}  active_render = {}".format(
        [layer.name for layer in mesh.uv_layers],
        [layer.name for layer in mesh.uv_layers if layer.active_render]))

    target = fresh_image("BakedA")
    set_bake_target(obj, target)
    make_active(obj)                        # <- the missing piece last time
    scene.render.bake.target = 'IMAGE_TEXTURES'
    scene.render.bake.use_selected_to_active = False
    scene.render.bake.use_clear = False     # <- do NOT wipe the canvas
    scene.render.bake.margin = 0
    bpy.ops.object.bake(type='EMIT')

    # sweep both quarters so we see WHERE content landed and WHAT it holds
    for label, (u, v) in (("UV1 quarter", (0.20, 0.20)),
                          ("UV2 quarter", (0.80, 0.80))):
        say("A: baked @ {} ({:.2f},{:.2f}) = {}".format(label, u, v, sample(target, u, v)))
    obj.hide_set(True)
    return sample(target, 0.80, 0.80)


def experiment_b(scene):
    say("--- B) two objects, plugin projection path (target UV2 <- source UV1) ---")
    collection = bpy.data.collections.new("B")
    scene.collection.children.link(collection)
    # source: carries the texture, its UV sits in the 0.1..0.3 quarter, hidden
    source = make_plane("Source", collection, 0.10, 0.30)
    source.data.materials.append(
        textured_material("MatSrc", gradient_image("SrcB"), "UV"))
    source.hide_set(True)
    # target: same space, its UV is the NEW layout (0.7..0.9), fully visible
    target_obj = make_plane("Target", collection, 0.70, 0.90)
    target_obj.data.materials.append(
        textured_material("MatTgt", gradient_image("SrcB"), "UV"))
    target_obj.hide_set(False)

    image = fresh_image("BakedB")
    set_bake_target(target_obj, image)
    make_active(target_obj, also=(source,))     # target active, source also selected
    scene.render.bake.target = 'IMAGE_TEXTURES'
    scene.render.bake.use_selected_to_active = True
    scene.render.bake.use_clear = False
    scene.render.bake.margin = 0
    try:
        bpy.ops.object.bake(type='EMIT', use_selected_to_active=True)
    except Exception as exc:
        say("B: bake raised {}: {}".format(type(exc).__name__, exc))
        return None
    for label, (u, v) in (("UV1 quarter", (0.20, 0.20)),
                          ("UV2 quarter", (0.80, 0.80))):
        say("B: baked @ {} ({:.2f},{:.2f}) = {}".format(label, u, v, sample(image, u, v)))
    say("B: content in the UV2 quarter at ~0.2 => the source's texture landed "
        "on the NEW layout")
    return sample(image, 0.80, 0.80)


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.engine = 'CYCLES'
    scene.cycles.samples = 1
    scene.cycles.device = 'CPU'

    a = experiment_a(scene)
    b = experiment_b(scene)

    say("SUMMARY  A(same object)={}  B(two objects)={}".format(a, b))

    for label, image in (("A", bpy.data.images.get("BakedA")),
                         ("B", bpy.data.images.get("BakedB"))):
        if image is None:
            continue
        path = os.path.join(OUT, "probe_two_uv_{}.png".format(label))
        image.filepath_raw = path
        image.file_format = 'PNG'
        try:
            image.save()
            say("saved {}".format(path))
        except Exception as exc:
            say("save {} failed: {}".format(label, exc))


if __name__ == "__main__":
    main()

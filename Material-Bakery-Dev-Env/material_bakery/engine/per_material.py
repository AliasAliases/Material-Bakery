# GPL v2+ — see LICENSE.txt
"""Per-object material groups, replaceable UV layout and slot delivery.

Card 25 can register another layout and set group.bake_source to a sample
surface. BakeTask remains the common engine protocol.
"""
import bpy

from .. import compat
from ..core import materials
from . import uv_sets

UV_NAME = "PerMaterial"
SCRATCH_TAG = "mbakery_per_material_scratch"
# Card 25: temporary 1 m sample surfaces live only for one task / one run.
SAMPLE_TAG = "mbakery_tiling_sample"
SCENE_SURFACES = "mbakery_tiling_surfaces"
# Card 28 (round 6): world Box tiling is written into a real UV layer instead of
# a TexCoord(Object=Empty) -> Mapping node network. Exporters drop those nodes,
# so the delivered material only sets `REPEAT` and relies on this layer.
TILING_UV_NAME = "Tiled"
TILING_UV_MARKER = "mbakery_tiling_uv"
TILING_TILE_SIZE = "mbakery_tiling_tile_size"
TILING_SCALE_LENGTH = "mbakery_tiling_scale_length"
TILING_ORIG_RENDER_UV = "mbakery_tiling_orig_render_uv"
TILING_ORIG_UV_INDEX = "mbakery_tiling_orig_uv_index"
TILING_OBJECT_MARKERS = (
    TILING_UV_MARKER, TILING_TILE_SIZE, TILING_SCALE_LENGTH,
    TILING_ORIG_RENDER_UV, TILING_ORIG_UV_INDEX,
)
LAYOUTS = {}


def register_layout(name, callback):
    LAYOUTS[name] = callback


def packed_layout(context, obj, slots):
    mesh = obj.data
    if mesh.users > 1:
        raise RuntimeError("Mesh data is shared. Make the mesh single-user before Per Material baking.")
    if obj.mode != 'OBJECT':
        raise RuntimeError("Object is in Edit Mode. Return to Object Mode before Per Material baking.")
    old = uv_sets.render_layer_name(obj)
    # Preserve every old layer, including the current active layer's coordinates.
    layer = mesh.uv_layers.get(UV_NAME)
    if layer is None:
        layer = mesh.uv_layers.new(name=UV_NAME)
    if old == UV_NAME:
        old = obj.get("mbakery_per_material_source_uv", "")
    obj["mbakery_per_material_source_uv"] = old
    pin = uv_sets.UVLayerPin()
    if old:
        for slot in obj.material_slots:
            if slot.material is not None:
                pin.apply_material(slot.material, old)
        # This is a permanent source-material pin, intentionally not reverted.
    saved = [poly.select for poly in mesh.polygons]
    saved_vertices = [vertex.select for vertex in mesh.vertices]
    saved_edges = [edge.select for edge in mesh.edges]
    tools = context.scene.tool_settings
    saved_sync = tools.use_uv_select_sync
    saved_mode = tuple(tools.mesh_select_mode)
    selected = list(context.selected_objects)
    active = context.view_layer.objects.active
    active_index = mesh.uv_layers.active_index
    try:
        for item in selected:
            item.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        mesh.uv_layers.active_index = mesh.uv_layers.find(UV_NAME)
        tools.use_uv_select_sync = True
        tools.mesh_select_mode = (False, False, True)
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='DESELECT')
        # Object-mode polygon flags alone leave the old vertex/edge selection
        # active when Blender enters Edit Mode. Select the exact BMesh faces.
        import bmesh
        edit_mesh = bmesh.from_edit_mesh(mesh)
        for face in edit_mesh.faces:
            face.select_set(face.material_index in slots)
        edit_mesh.select_flush_mode()
        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
        bpy.ops.uv.smart_project(island_margin=0.001)
        bpy.ops.uv.pack_islands(margin=0.001)
    finally:
        if obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        for vertex, value in zip(mesh.vertices, saved_vertices):
            vertex.select = value
        for edge, value in zip(mesh.edges, saved_edges):
            edge.select = value
        for poly, value in zip(mesh.polygons, saved):
            poly.select = value
        tools.use_uv_select_sync = saved_sync
        tools.mesh_select_mode = saved_mode
        obj.select_set(False)
        for item in selected:
            item.select_set(True)
        context.view_layer.objects.active = active
        mesh.uv_layers.active_index = active_index
        mesh.uv_layers[UV_NAME].active_render = True


register_layout("PACKED", packed_layout)


def tiling_layout(context, obj, slots):
    """TILING never touches the original object or its UVs.

    The bake runs on a temporary 1 m sample surface instead (see
    create_task_surface). Keeping the layout callback a no-op is what makes
    "old object / UV drift = 0" true by construction.
    """
    return None


register_layout("TILING", tiling_layout)


def prepare_group(context, group):
    if getattr(group, "uv_strategy", "") == "TILING":
        return
    layout = LAYOUTS[group.uv_strategy]
    for obj in group.objects:
        layout(context, obj, group.material_slots)


def _surface_size_bu(task):
    """Sample surface size in Blender units — 1 m / scene scale."""
    info = task.options.get("per_material") or {}
    scale = float(info.get("scene_scale_length", 1.0) or 0.0)
    if scale <= 0.0:
        scale = 1.0
    return 1.0 / scale


def _track_surface(scene, name):
    names = [item for item in (scene.get(SCENE_SURFACES) or []) if item != name]
    names.append(name)
    scene[SCENE_SURFACES] = names


def _untrack_surface(scene, name):
    if scene is None:
        return
    names = [item for item in (scene.get(SCENE_SURFACES) or []) if item != name]
    if names:
        scene[SCENE_SURFACES] = names
    elif SCENE_SURFACES in scene.keys():
        del scene[SCENE_SURFACES]


def create_task_surface(context, task):
    """Build the temporary 1 m x 1 m plane a Tiling task bakes into.

    World size is 1 m: Blender-unit size is 1.0 / scene.unit_settings.scale_length.
    The plane carries UV 0-1 and exactly one material (`task.options`), and it is
    linked to the scene collection so the bake operator can select it. The object
    is tagged with SAMPLE_TAG and tracked in a scene property so it belongs to
    this run and can be purged even after an exception.
    """
    info = task.options.get("per_material") or {}
    material_name = info.get("material", "")
    material = bpy.data.materials.get(material_name)
    if material is None:
        raise RuntimeError(
            "Tiling material '{}' no longer exists".format(material_name))

    size = _surface_size_bu(task)
    half = size * 0.5
    mesh = bpy.data.meshes.new(".MB_TilingSampleMesh")
    mesh.from_pydata(
        [(-half, -half, 0.0), (half, -half, 0.0),
         (half, half, 0.0), (-half, half, 0.0)],
        [], [(0, 1, 2, 3)])
    mesh.update()
    uv_layer = mesh.uv_layers.new(name="UVMap")
    corners = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    for loop in mesh.loops:
        uv_layer.data[loop.index].uv = corners[loop.vertex_index]
    mesh.materials.append(material)
    mesh.update()

    obj = bpy.data.objects.new(".MB_TilingSample_{}".format(material.name), mesh)
    obj.scale = (1.0, 1.0, 1.0)
    obj[SAMPLE_TAG] = True
    obj["mbakery_tiling_group"] = task.group_name
    obj["mbakery_tiling_material"] = material.name
    context.scene.collection.objects.link(obj)
    _track_surface(context.scene, obj.name)
    return obj


def remove_task_surface(obj):
    """Delete a sample surface (idempotent — callers may pass a dead reference)."""
    if obj is None:
        return
    try:
        name = obj.name
        mesh = obj.data
    except ReferenceError:
        return
    scene = bpy.context.scene if bpy.context is not None else None
    _untrack_surface(scene, name)
    try:
        obj.select_set(False)
    except (ReferenceError, RuntimeError):
        pass
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh is not None and mesh.users == 0:
        bpy.data.meshes.remove(mesh)
    # Remove the dead slot from the view layer immediately: the next task's
    # select_for_bake() iterates view_layer.objects and must not meet a None.
    try:
        bpy.context.view_layer.update()
    except (AttributeError, RuntimeError):
        pass


def purge_scratch_surfaces(scene):
    """Remove every tagged sample surface left in the file (success/cancel/fail)."""
    removed = 0
    for obj in list(bpy.data.objects):
        if obj.get(SAMPLE_TAG):
            remove_task_surface(obj)
            removed += 1
    if scene is not None and SCENE_SURFACES in scene.keys():
        del scene[SCENE_SURFACES]
    return removed


# ------------------------------------------------------------------------------------
#   Card 28 (round 6): the world Box lives in a real UV layer
#
#   Exporters only carry UV sets — glTF / FBX drop `TexCoord` and `Mapping` nodes.
#   The old delivery therefore collapsed to the mesh's own UVs as soon as it left
#   Blender ("直接被放大了"). So the world coordinate is now baked into a UV layer
#   named `Tiled`:
#
#       u, v = world_axis * scene_scale_length / tile_size
#
#   and every delivered image node only sets `extension = 'REPEAT'`.
#
#   ⚠ The glTF exporter numbers `TEXCOORD_0` by **layer order**, not by
#     `active_render` (io_scene_gltf2/blender/exp/primitive_extract.py reads
#     mesh.uv_layers[index]). `Tiled` therefore has to be `uv_layers[0]`; the old
#     layers are rebuilt behind it with their data preserved value-for-value.
# ------------------------------------------------------------------------------------

def _world_normal_matrix(obj):
    """Normal matrix (inverse transpose of the world rotation); safe on odd scales."""
    try:
        return obj.matrix_world.to_3x3().inverted_safe().transposed()
    except (AttributeError, ValueError):
        return obj.matrix_world.to_3x3()


def _face_axis(world_normal):
    """World face normal -> `(axis_u, axis_v)` vertex indices.

    Deterministic x -> y -> z tie break: the first axis whose absolute component
    is not smaller than the others wins.
    """
    nx, ny, nz = (abs(float(value)) for value in world_normal)
    if nx >= ny and nx >= nz:
        return 1, 2          # |nx| largest: u = world.y, v = world.z
    if ny >= nz:
        return 0, 2          # |ny| largest: u = world.x, v = world.z
    return 0, 1              # otherwise:    u = world.x, v = world.y


def _read_uvs(layer, count):
    data = [0.0] * (count * 2)
    layer.data.foreach_get("uv", data)
    return data


def _snapshot_old_layers(mesh, count, skip_name):
    """Every existing layer except `skip_name`, in order: `[(name, flat uv list)]`."""
    layers = []
    for layer in mesh.uv_layers:
        if layer.name == skip_name:
            continue
        layers.append((layer.name, _read_uvs(layer, count)))
    return layers


def _unique_layer_name(mesh, base):
    """A layer name that does not collide with an existing one (`base`, `base.001`, …)."""
    if mesh.uv_layers.get(base) is None:
        return base
    index = 1
    while True:
        candidate = "{}.{:03d}".format(base, index)
        if mesh.uv_layers.get(candidate) is None:
            return candidate
        index += 1


def _restore_render_layer(obj, mesh):
    """Put `active_render` / `active_index` back on the layer recorded before writing."""
    original = obj.get(TILING_ORIG_RENDER_UV, "")
    index = int(obj.get(TILING_ORIG_UV_INDEX, 0) or 0)
    if mesh is None or not len(mesh.uv_layers):
        return
    found = mesh.uv_layers.find(original) if original else -1
    if found < 0:
        found = 0
    for position, layer in enumerate(mesh.uv_layers):
        wanted = position == found
        if bool(layer.active_render) != wanted:
            layer.active_render = wanted
    mesh.uv_layers.active_index = index if 0 <= index < len(mesh.uv_layers) else found


def write_tiling_uv(obj, tile_size, scale_length):
    """Write the world Box mapping into a UV layer named `Tiled` (or a free name).

    Returns a dict with the readings the tests print (min/max per axis, counts).
    Old UV layers keep their exact values — only their order moves behind the
    tiling layer, which is `uv_layers[0]` and `active_render`.

    A user layer that is already called `Tiled` is never touched: the first write
    picks `Tiled.001` (or the next free name) and stores that actual name in the
    object marker. Every later call reads the name back from the marker.

    Raises (same wording as `packed_layout`) when the mesh is shared or the object
    is not in OBJECT mode: writing into a shared mesh would silently change every
    other user, and edit-mode data does not match the evaluated mesh.
    """
    if obj is None or getattr(obj, "type", "") != 'MESH' or obj.data is None:
        raise RuntimeError("Tiling needs a mesh object")
    mesh = obj.data
    if mesh.users > 1:
        raise RuntimeError("Mesh data is shared. Make the mesh single-user before Per Material baking.")
    if obj.mode != 'OBJECT':
        raise RuntimeError("Object is in Edit Mode. Return to Object Mode before Per Material baking.")

    tile = float(tile_size or 0.0)
    if tile <= 0.0:
        tile = 1.0
    length = float(scale_length or 0.0)
    if length <= 0.0:
        length = 1.0
    factor = length / tile

    # The marker stores the **actual** layer name, because a user layer called
    # `Tiled` forces us to `Tiled.001`. Never overwrite the user's layer.
    layer_name = obj.get(TILING_UV_MARKER, "") or ""
    if not layer_name or mesh.uv_layers.get(layer_name) is None:
        if obj.get(TILING_UV_MARKER) and layer_name:
            # Our own layer was deleted by hand; remember what NOT to touch first.
            layer_name = _unique_layer_name(mesh, layer_name)
        else:
            layer_name = _unique_layer_name(mesh, TILING_UV_NAME)

    # First write remembers where rendering used to point, so remove_tiling_uvs()
    # can put it back exactly. Recomputation never overwrites that record.
    if TILING_ORIG_RENDER_UV not in obj:
        previous = uv_sets.render_layer_name(obj)
        if previous == layer_name:
            previous = next((layer.name for layer in mesh.uv_layers
                             if layer.name != layer_name), "")
        obj[TILING_ORIG_RENDER_UV] = previous
        obj[TILING_ORIG_UV_INDEX] = int(mesh.uv_layers.active_index)

    count = len(mesh.loops)
    old_layers = _snapshot_old_layers(mesh, count, layer_name)
    normal_matrix = _world_normal_matrix(obj)
    matrix = obj.matrix_world
    positions = [matrix @ vertex.co for vertex in mesh.vertices]

    coords = [0.0] * (count * 2)
    axis_faces = {0: 0, 1: 0, 2: 0}
    for polygon in mesh.polygons:
        normal = normal_matrix @ polygon.normal
        axis_u, axis_v = _face_axis(normal)
        axis_faces[3 - axis_u - axis_v] += 1
        for loop_index in polygon.loop_indices:
            point = positions[mesh.loops[loop_index].vertex_index]
            coords[loop_index * 2] = float(point[axis_u]) * factor
            coords[loop_index * 2 + 1] = float(point[axis_v]) * factor

    u_values = coords[0::2]
    v_values = coords[1::2]

    # Rebuild the layer stack: tiling layer first, then the old layers in their
    # old order with their old data. Values are moved through foreach_get /
    # foreach_set, so the old layers are bit-identical after the reorder.
    while len(mesh.uv_layers):
        mesh.uv_layers.remove(mesh.uv_layers[0])
    tiled = mesh.uv_layers.new(name=layer_name)
    layer_name = tiled.name
    tiled.data.foreach_set("uv", coords)
    for name, data in old_layers:
        layer = mesh.uv_layers.new(name=name)
        layer.data.foreach_set("uv", data)
    mesh.uv_layers.active_index = 0
    for position, layer in enumerate(mesh.uv_layers):
        layer.active_render = position == 0
    mesh.update()

    obj[TILING_UV_MARKER] = layer_name
    obj[TILING_TILE_SIZE] = tile
    obj[TILING_SCALE_LENGTH] = length
    return {
        "uv_layer": layer_name,
        "layer_index": mesh.uv_layers.find(layer_name),
        "tile_size": tile,
        "scale_length": length,
        "factor": factor,
        "vertices": len(mesh.vertices),
        "loops": count,
        "faces": len(mesh.polygons),
        "x_faces": axis_faces[0],
        "y_faces": axis_faces[1],
        "z_faces": axis_faces[2],
        "u_min": min(u_values) if u_values else 0.0,
        "u_max": max(u_values) if u_values else 0.0,
        "v_min": min(v_values) if v_values else 0.0,
        "v_max": max(v_values) if v_values else 0.0,
    }


def update_tiling_uv(tile_size, scale_length=None):
    """Recompute every `Tiled` layer for the new Tile Size — no re-bake at all.

    Nothing here may raise: this function is called from a property update
    callback, and an exception there aborts the property assignment.
    Returns the number of objects that were really recomputed.
    """
    try:
        tile = float(tile_size or 0.0)
    except (TypeError, ValueError):
        tile = 0.0
    if tile <= 0.0:
        tile = 1.0

    if scale_length is None:
        length = 1.0
        try:
            length = float(bpy.context.scene.unit_settings.scale_length or 0.0)
        except (AttributeError, TypeError, ValueError):
            length = 1.0
    else:
        try:
            length = float(scale_length)
        except (TypeError, ValueError):
            length = 1.0
    if length <= 0.0:
        length = 1.0

    updated = 0
    for obj in list(bpy.data.objects):
        try:
            if not obj.get(TILING_UV_MARKER):
                continue
            write_tiling_uv(obj, tile, length)
            updated += 1
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError) as exc:
            compat.log("Could not recompute the '{}' layer on '{}': {}".format(
                TILING_UV_NAME, getattr(obj, "name", "?"), exc))

    # Delivery materials carry the values as metadata only (no Mapping node now).
    try:
        from ..deliver import materials as deliver_materials
        for material in bpy.data.materials:
            try:
                if not material.get(deliver_materials.TILING_MARKER):
                    continue
                material[deliver_materials.TILING_TILE_SIZE] = tile
                material[deliver_materials.TILING_SCALE_LENGTH] = length
            except (AttributeError, ReferenceError):
                continue
    except Exception as exc:                      # noqa: BLE001 - metadata only
        compat.log("Could not update the Tiling material metadata: {}".format(exc))
    return updated


def remove_tiling_uvs(objects=None):
    """Delete the tiling UV layer and restore the original render layer.

    `objects=None` scans the whole file. Only objects carrying TILING_UV_MARKER
    are touched; the layer name comes from that marker, so a user layer that
    happens to be called `Tiled` is never removed. Idempotent, never raises at
    the caller. Returns how many objects were cleaned.
    """
    if objects is None:
        targets = list(bpy.data.objects)
    else:
        targets = [bpy.data.objects.get(item) if isinstance(item, str) else item
                   for item in objects]
    cleaned = 0
    for obj in targets:
        if obj is None:
            continue
        try:
            if not obj.get(TILING_UV_MARKER):
                continue
        except (AttributeError, ReferenceError):
            continue
        layer_name = obj.get(TILING_UV_MARKER, "") or TILING_UV_NAME
        try:
            mesh = getattr(obj, "data", None)
            if mesh is not None and mesh.uv_layers.get(layer_name) is not None:
                mesh.uv_layers.remove(mesh.uv_layers[layer_name])
            _restore_render_layer(obj, mesh)
        except (AttributeError, ReferenceError, RuntimeError) as exc:
            compat.log("Could not remove the '{}' layer from '{}': {}".format(
                layer_name, getattr(obj, "name", "?"), exc))
        finally:
            for key in TILING_OBJECT_MARKERS:
                try:
                    if key in obj:
                        del obj[key]
                except (AttributeError, ReferenceError):
                    continue
        cleaned += 1
    return cleaned


class MaterialTargets:
    """Install a throwaway target on other materials; release it after each bake."""
    def __init__(self):
        self.image = None
        self.nodes = []
        self.active = []

    def install(self, tx, targets, task):
        self.clear()
        wanted = task.options["per_material"]["material"]
        self.image = bpy.data.images.new(".MB_PerMaterialDiscard", width=4, height=4)
        self.image[SCRATCH_TAG] = True
        seen = set()
        for obj in targets:
            for slot in obj.material_slots:
                mat = slot.material
                if mat is None or mat.name in seen:
                    continue
                seen.add(mat.name)
                if mat.name == wanted:
                    tx.install_bake_target(mat, task.image)
                    continue
                if not mat.use_nodes:
                    mat.use_nodes = True
                tree = mat.node_tree
                self.active.append((mat.name, tree.nodes.active.name if tree.nodes.active else ""))
                node = tree.nodes.new('ShaderNodeTexImage')
                node.label = "MBakery PerMaterial discard"
                node.image = self.image
                node[SCRATCH_TAG] = True
                tree.nodes.active = node
                self.nodes.append((mat.name, node.name))

    def clear(self):
        for mat_name, node_name in self.nodes:
            mat = bpy.data.materials.get(mat_name)
            if mat is not None and mat.node_tree:
                node = mat.node_tree.nodes.get(node_name)
                if node is not None:
                    mat.node_tree.nodes.remove(node)
        for mat_name, node_name in self.active:
            mat = bpy.data.materials.get(mat_name)
            if mat is not None and mat.node_tree:
                mat.node_tree.nodes.active = mat.node_tree.nodes.get(node_name)
        self.nodes = []
        self.active = []
        if self.image is not None:
            bpy.data.images.remove(self.image)
            self.image = None


def _tiling_values(tiling):
    """`(tile_size, scale_length)` from the delivery payload, both normalised."""
    info = tiling or {}
    tile = float(info.get("tile_size", 1.0) or 0.0)
    if tile <= 0.0:
        tile = 1.0
    length = float(info.get("scale_length", 1.0) or 0.0)
    if length <= 0.0:
        length = 1.0
    return tile, length


def apply_materials(plan, by_group, tiling=None):
    """Replace corresponding slots and keep the existing restore record intact.

    With `tiling={"tile_size": ..., "scale_length": ...}` the world Box is first
    written into each object's `Tiled` UV layer, and every material that was on
    the object is pinned to its old render layer (the existing permanent pin —
    that is what keeps the original material looking right while the baked
    material follows `active_render`).

    A shared mesh or an edit-mode object aborts the whole call **before** a
    single UV value is written.
    """
    tile, length = _tiling_values(tiling)
    if tiling is not None:
        from ..deliver import materials as deliver_materials
        for name in plan.eligible_names():
            obj = bpy.data.objects.get(name)
            mesh = getattr(obj, "data", None) if obj is not None else None
            if obj is None or mesh is None:
                continue
            if mesh.users > 1:
                raise RuntimeError("Mesh data is shared. Make the mesh single-user before Per Material baking.")
            if obj.mode != 'OBJECT':
                raise RuntimeError("Object is in Edit Mode. Return to Object Mode before Per Material baking.")

    changed = set()
    backed_up = set()
    # A material can be used by several objects / groups; pin it exactly once,
    # to the oldest recorded render layer (they all come from the same snapshot).
    pinned_materials = set()
    for group in plan.groups:
        entry = by_group.get(group.name)
        if entry is None:
            continue
        baked = entry[0] if isinstance(entry, tuple) else entry
        for name in group.names():
            obj = bpy.data.objects.get(name)
            if obj is None or not plan.object_all_succeeded(name):
                continue
            if tiling is not None:
                # Write the UV layer exactly once per object. `write_tiling_uv`
                # recorded the original render layer on the first write; always
                # read it from there instead of the live `active_render` (which
                # is already `Tiled` after the first group).
                if not obj.get(TILING_UV_MARKER):
                    write_tiling_uv(obj, tile, length)
                old = obj.get(TILING_ORIG_RENDER_UV, "") or ""
                if old:
                    # Permanent pin (same as the Packed layout): the original
                    # material keeps sampling the layer it looked at before.
                    for slot in obj.material_slots:
                        material = slot.material
                        if material is None or material.name in pinned_materials:
                            continue
                        try:
                            if material.get(deliver_materials.TILING_MARKER):
                                continue
                        except (AttributeError, ReferenceError):
                            pass
                        uv_sets.UVLayerPin().apply_material(material, old)
                        pinned_materials.add(material.name)
            if name not in backed_up:
                # Use the project's canonical backup, then remove its extra slot.
                ok, reason = materials.add_baked_slot(obj, baked)
                if not ok:
                    raise RuntimeError(reason)
                obj.data.materials.pop(index=len(obj.data.materials) - 1)
                backed_up.add(name)
            mapping = dict(obj.get("mbakery_per_material_slots", {}))
            for index in group.material_slots:
                mapping[str(index)] = baked.name
                obj.material_slots[index].material = baked
            obj["mbakery_per_material_slots"] = mapping
            changed.add(name)
    return len(changed)


def preview_materials(plan, baked=True):
    changed = 0
    for name in plan.eligible_names():
        obj = bpy.data.objects.get(name)
        if obj is None:
            continue
        mapping = obj.get("mbakery_per_material_slots", {})
        originals = list(obj.get(materials.PROP_ORIG_SLOTS, []))
        if not mapping or not originals:
            continue
        for key, baked_name in mapping.items():
            index = int(key)
            wanted = baked_name if baked else originals[index]
            material = bpy.data.materials.get(wanted)
            if index < len(obj.material_slots) and material is not None:
                obj.material_slots[index].material = material
        obj[materials.PROP_PREVIEWING] = baked
        changed += 1
    return changed


def _set_tiling_render_layer(obj):
    """Make the recorded tiling layer the render layer again (finalize path)."""
    mesh = getattr(obj, "data", None)
    if mesh is None:
        return False
    layer_name = obj.get(TILING_UV_MARKER, "") or ""
    index = mesh.uv_layers.find(layer_name) if layer_name else -1
    if index < 0:
        return False
    mesh.uv_layers.active_index = index
    for position, layer in enumerate(mesh.uv_layers):
        wanted = position == index
        if bool(layer.active_render) != wanted:
            layer.active_render = wanted
    return True


def finalize_materials(plan, settings):
    # Per Material keeps the original slot indices: merging them would destroy
    # the face-to-texture correspondence. Preserve the canonical restore record.
    preview_materials(plan, baked=True)
    from ..deliver import uv_layers
    # TILING bakes on a throwaway sample surface, so the original object's UV
    # layers must stay exactly as they were: no finalize, no layer deletion.
    # `Tiled` is re-armed as the render layer so exporting right after finalize
    # still writes it into TEXCOORD_0.
    tiling = str(getattr(settings, "per_material_layout", "PACKED") or "PACKED").upper() \
        == "TILING"
    done = 0
    for name in plan.eligible_names():
        obj = bpy.data.objects.get(name)
        if obj is None or not obj.get("mbakery_per_material_slots"):
            continue
        if not tiling:
            uv_layers.finalize_layers_for([name], UV_NAME)
        else:
            _set_tiling_render_layer(obj)
        for slot in obj.material_slots:
            material = slot.material
            if material is None:
                continue
            material.use_fake_user = True
            for node in material.node_tree.nodes if material.node_tree else ():
                image = getattr(node, "image", None)
                if image is None:
                    continue
                image.use_fake_user = True
                if getattr(settings, "pack_into_blend", False) and image.packed_file is None:
                    image.pack()
        done += 1
    return done

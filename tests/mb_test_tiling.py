"""Card 28 (round 6): Tiling delivery writes a real world-Box UV layer.

The delivered material no longer contains TexCoord / Mapping nodes; the world
coordinate is written into a UV layer that the glTF / FBX exporters carry. This
suite checks the UV maths, the layer order, the material shape, the exported
GLB TEXCOORD_0, Tile Size recomputation without re-baking, apply/restore, and
the four exit paths.
"""
import hashlib
import json
import math
import os
import struct
import sys
import tempfile
import time
import shutil

import bpy
import numpy as np

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WS)
import material_bakery
from material_bakery.core import plan, scene_scan
from material_bakery.core import presets as presets_mod
from material_bakery.core import transaction
from material_bakery.core import materials as core_materials
from material_bakery.core.materials import MaterialStore
from material_bakery.engine import backend, job, per_material
from material_bakery.deliver import materials as deliver_materials
from material_bakery.deliver import slots as slots_mod
from material_bakery.deliver import uv_layers
from material_bakery.ui import ops as uops
from material_bakery.ui import ops_presets, properties, session

failed = []


def check(label, value, detail=''):
    print('[{}] {} {}'.format('PASS' if value else 'FAIL', label, detail), flush=True)
    if not value:
        failed.append(label)


# ------------------------------------------------------------------------------------
#   Generic helpers
# ------------------------------------------------------------------------------------

def tagged_surfaces():
    return [obj for obj in bpy.data.objects if obj.get(per_material.SAMPLE_TAG)]


def layer_data(obj, name):
    layer = obj.data.uv_layers.get(name)
    if layer is None:
        return None
    return np.array([tuple(item.uv) for item in layer.data], dtype=float)


def uv_snapshot(obj):
    return {layer.name: np.array([tuple(item.uv) for item in layer.data], dtype=float)
            for layer in obj.data.uv_layers}


def uv_drift(before, obj):
    """(max value diff of layers that existed before, missing names, new names)"""
    after = uv_snapshot(obj)
    missing = sorted(set(before) - set(after))
    extra = sorted(set(after) - set(before))
    drift = 0.0
    for name, values in before.items():
        current = after.get(name)
        if current is None:
            return float('inf'), missing, extra
        if current.shape != values.shape:
            return float('inf'), missing, extra
        if values.size:
            drift = max(drift, float(np.max(np.abs(current - values))))
    return drift, missing, extra


def select_only(obj):
    bpy.ops.object.select_all(action='DESELECT')
    if obj is not None:
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj


def clear_scene():
    if bpy.context.object is not None and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    for material in list(bpy.data.materials):
        bpy.data.materials.remove(material)
    for mesh in list(bpy.data.meshes):
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)


def make_cube(name, size=2.0):
    """An axis-aligned cube spanning 0..size with a distinctive old UV layer."""
    mesh = bpy.data.meshes.new(name + 'Mesh')
    s = float(size)
    mesh.from_pydata(
        [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0),
         (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)],
        [], [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
             (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)])
    mesh.update()
    layer = mesh.uv_layers.new(name='OriginalUV')
    for loop in mesh.loops:
        index = loop.index
        layer.data[index].uv = ((index % 7) * 0.13, ((index * 3) % 11) * 0.07)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def make_quad(name, x0, x1, y0, y1, z=0.0):
    mesh = bpy.data.meshes.new(name + 'Mesh')
    mesh.from_pydata([(x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)],
                     [], [(0, 1, 2, 3)])
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def make_plane(name, size=1.0):
    half = size / 2.0
    return make_quad(name, -half, half, -half, half)


def make_delivery_object(name):
    mesh = bpy.data.meshes.new(name + 'Mesh')
    mesh.from_pydata([(0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
                      (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)], [], [(0, 1, 2, 3)])
    mesh.update()
    mesh.uv_layers.new(name='UVMap')
    original = bpy.data.materials.new(name + 'Original')
    original.use_nodes = True
    mesh.materials.append(original)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def make_scene():
    """Two quads, two materials: a Per Material plan makes two groups."""
    clear_scene()
    mesh = bpy.data.meshes.new('TilingSourceMesh')
    mesh.from_pydata(
        [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
         (2, 0, 0), (3, 0, 0), (3, 1, 0), (2, 1, 0)],
        [], [(0, 1, 2, 3), (4, 5, 6, 7)])
    mesh.update()
    mesh.uv_layers.new(name='OriginalUV')
    layer = mesh.uv_layers['OriginalUV']
    corners = ((0.05, 0.1), (0.9, 0.1), (0.9, 0.8), (0.05, 0.8))
    for loop in mesh.loops:
        layer.data[loop.index].uv = corners[loop.vertex_index % 4]
    colors = ((0.8, 0.2, 0.1, 1.0), (0.1, 0.3, 0.8, 1.0))
    for index, color in enumerate(colors):
        material = bpy.data.materials.new('TilingMat{}'.format(index))
        material.use_nodes = True
        bsdf = material.node_tree.nodes.get('Principled BSDF')
        bsdf.inputs['Base Color'].default_value = color
        mesh.materials.append(material)
    mesh.polygons[0].material_index = 0
    mesh.polygons[1].material_index = 1

    obj = bpy.data.objects.new('TilingSource', mesh)
    bpy.context.scene.collection.objects.link(obj)
    select_only(obj)
    return obj


def make_settings(layout, scale_length=1.0, tile_size=1.0):
    return plan.BakeSettings(
        target_mode=scene_scan.MODE_SELECTED,
        maps=(plan.MapRequest('shader.base_color', 64),),
        workflow='per_material',
        per_material_layout=layout,
        tile_size=tile_size,
        scene_scale_length=scale_length,
        margin=0,
        use_subfolders=False,
        pack_into_blend=False,
        fake_user=True,
        file_format='PNG',
    )


def tiling_payload(tile=1.0, scale=1.0):
    return {"tile_size": tile, "scale_length": scale}


def tiling_materials():
    result = []
    for material in bpy.data.materials:
        try:
            if material.get(deliver_materials.TILING_MARKER):
                result.append(material)
        except (AttributeError, ReferenceError):
            continue
    return result


def clear_tiling_materials():
    for material in tiling_materials():
        bpy.data.materials.remove(material)


def image_signature(image):
    values = np.array(image.pixels[:], dtype=float)
    return hashlib.sha1(values.tobytes()).hexdigest(), int(values.size)


def file_sha1(path):
    digest = hashlib.sha1()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(65536), b''):
            digest.update(block)
    return digest.hexdigest()


def face_uv_bounds(obj, layer_name):
    """Per face: (dominant world axis, u_min, u_max, v_min, v_max)."""
    mesh = obj.data
    layer = mesh.uv_layers.get(layer_name)
    normal_matrix = obj.matrix_world.to_3x3().inverted_safe().transposed()
    rows = []
    for polygon in mesh.polygons:
        values = np.array([tuple(layer.data[index].uv)
                           for index in polygon.loop_indices])
        normal = normal_matrix @ polygon.normal
        nx, ny, nz = abs(normal.x), abs(normal.y), abs(normal.z)
        if nx >= ny and nx >= nz:
            axis = 'x'
        elif ny >= nz:
            axis = 'y'
        else:
            axis = 'z'
        rows.append((axis, float(values[:, 0].min()), float(values[:, 0].max()),
                     float(values[:, 1].min()), float(values[:, 1].max())))
    return rows


def expected_tiling_uv(obj, tile, scale):
    """The exact formula from the task card, loop order."""
    factor = scale / tile
    mesh = obj.data
    normal_matrix = obj.matrix_world.to_3x3().inverted_safe().transposed()
    positions = [obj.matrix_world @ vertex.co for vertex in mesh.vertices]
    values = []
    for polygon in mesh.polygons:
        normal = normal_matrix @ polygon.normal
        nx, ny, nz = abs(normal.x), abs(normal.y), abs(normal.z)
        if nx >= ny and nx >= nz:
            axis_u, axis_v = 1, 2
        elif ny >= nz:
            axis_u, axis_v = 0, 2
        else:
            axis_u, axis_v = 0, 1
        for index in polygon.loop_indices:
            point = positions[mesh.loops[index].vertex_index]
            values.append((point[axis_u] * factor, point[axis_v] * factor))
    return np.array(values)


def loop_uv(obj, layer_name):
    layer = obj.data.uv_layers[layer_name]
    return np.array([tuple(item.uv) for item in layer.data], dtype=float)


def linear_fit_residual(obj, layer_name):
    """Worst |UV - affine(world)| over every face and both UV channels."""
    mesh = obj.data
    layer = mesh.uv_layers[layer_name]
    positions = [obj.matrix_world @ vertex.co for vertex in mesh.vertices]
    worst = 0.0
    for polygon in mesh.polygons:
        design = []
        us = []
        vs = []
        for index in polygon.loop_indices:
            point = positions[mesh.loops[index].vertex_index]
            design.append([point.x, point.y, point.z, 1.0])
            uv = layer.data[index].uv
            us.append(uv[0])
            vs.append(uv[1])
        design = np.array(design)
        for target in (np.array(us), np.array(vs)):
            coeff, *_ = np.linalg.lstsq(design, target, rcond=None)
            worst = max(worst, float(np.max(np.abs(design @ coeff - target))))
    return worst


# ------------------------------------------------------------------------------------
#   Probe image / renders
# ------------------------------------------------------------------------------------

PROBE_SIZE = 8
PROBE_PROFILE = (0.1, 0.3, 0.6, 0.9, 0.9, 0.6, 0.3, 0.1)
RENDER_SIZE = 32


def make_probe_image(name='MB_TilingProbe'):
    image = bpy.data.images.get(name)
    if image is not None:
        return image
    image = bpy.data.images.new(name, PROBE_SIZE, PROBE_SIZE, alpha=False)
    image.colorspace_settings.name = 'Non-Color'
    pixels = []
    for row in range(PROBE_SIZE):
        for col in range(PROBE_SIZE):
            value = 0.5 * (PROBE_PROFILE[col] + PROBE_PROFILE[row])
            pixels.extend((value, value, value, 1.0))
    image.pixels = pixels
    return image


def setup_probe_render():
    scene = bpy.context.scene
    scene.render.engine = 'CYCLES'
    scene.cycles.device = 'CPU'
    scene.cycles.samples = 1
    scene.cycles.use_denoising = False
    scene.cycles.use_adaptive_sampling = False
    scene.cycles.max_bounces = 0
    scene.cycles.filter_width = 0.01
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.render.dither_intensity = 0.0
    scene.view_settings.view_transform = 'Standard'
    scene.view_settings.look = 'None'
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0


_RENDER_COUNTER = [0]


def render_top(plane, size=RENDER_SIZE, center=(0.0, 0.0), span=1.0):
    """Render a top-down orthographic view and return linear pixels (size, size, 4)."""
    setup_probe_render()
    scene = bpy.context.scene
    scene.render.resolution_x = size
    scene.render.resolution_y = size
    camera = bpy.data.objects.get('MB_R6Camera')
    if camera is None:
        data = bpy.data.cameras.new('MB_R6Camera')
        data.type = 'ORTHO'
        data.clip_start = 0.01
        data.clip_end = 100.0
        camera = bpy.data.objects.new('MB_R6Camera', data)
        scene.collection.objects.link(camera)
    camera.data.ortho_scale = span
    camera.location = (center[0], center[1], 5.0)
    camera.rotation_euler = (0.0, 0.0, 0.0)
    scene.camera = camera
    saved = {obj.name: obj.hide_render for obj in bpy.data.objects}
    for obj in bpy.data.objects:
        obj.hide_render = obj is not plane and obj is not camera
    plane.hide_render = False
    _RENDER_COUNTER[0] += 1
    path = os.path.join(tempfile.gettempdir(),
                        'mb_r6_render_{}.exr'.format(_RENDER_COUNTER[0]))
    scene.render.filepath = path
    old_format = scene.render.image_settings.file_format
    old_depth = scene.render.image_settings.color_depth
    scene.render.image_settings.file_format = 'OPEN_EXR'
    scene.render.image_settings.color_depth = '32'
    bpy.ops.render.render(write_still=True)
    scene.render.image_settings.file_format = old_format
    scene.render.image_settings.color_depth = old_depth
    image = bpy.data.images.load(path, check_existing=False)
    image.colorspace_settings.name = 'Non-Color'
    values = np.array(image.pixels[:], dtype=float)
    pixels = values.reshape(size, size, 4)
    bpy.data.images.remove(image)
    for name, hidden in saved.items():
        obj = bpy.data.objects.get(name)
        if obj is not None:
            obj.hide_render = hidden
    return pixels


def emissionize(material):
    tree = material.node_tree
    output = next((node for node in tree.nodes if node.type == 'OUTPUT_MATERIAL'), None)
    image_node = next((node for node in tree.nodes if node.type == 'TEX_IMAGE'), None)
    principled = next((node for node in tree.nodes if node.type == 'BSDF_PRINCIPLED'), None)
    emission = tree.nodes.new('ShaderNodeEmission')
    emission.location = (200, -200)
    emission.inputs['Strength'].default_value = 1.0
    if image_node is not None:
        tree.links.new(image_node.outputs['Color'], emission.inputs['Color'])
    if output is not None:
        tree.links.new(emission.outputs['Emission'], output.inputs['Surface'])
    if principled is not None:
        tree.nodes.remove(principled)
    return material


def make_gradient_material(name, emission=False):
    """A smooth procedural gradient on the plane's Generated coordinates."""
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new('ShaderNodeOutputMaterial')
    texcoord = tree.nodes.new('ShaderNodeTexCoord')
    separate = tree.nodes.new('ShaderNodeSeparateXYZ')
    add = tree.nodes.new('ShaderNodeMath')
    add.operation = 'ADD'
    half = tree.nodes.new('ShaderNodeMath')
    half.operation = 'MULTIPLY'
    half.inputs[1].default_value = 0.5
    ramp = tree.nodes.new('ShaderNodeValToRGB')
    ramp.color_ramp.elements[0].position = 0.0
    ramp.color_ramp.elements[0].color = (0.05, 0.1, 0.5, 1.0)
    ramp.color_ramp.elements[1].position = 1.0
    ramp.color_ramp.elements[1].color = (0.9, 0.4, 0.05, 1.0)
    tree.links.new(texcoord.outputs['Generated'], separate.inputs[0])
    tree.links.new(separate.outputs['X'], add.inputs[0])
    tree.links.new(separate.outputs['Y'], add.inputs[1])
    tree.links.new(add.outputs[0], half.inputs[0])
    tree.links.new(half.outputs[0], ramp.inputs['Fac'])
    if emission:
        emitter = tree.nodes.new('ShaderNodeEmission')
        emitter.inputs['Strength'].default_value = 1.0
        tree.links.new(ramp.outputs['Color'], emitter.inputs['Color'])
        tree.links.new(emitter.outputs['Emission'], output.inputs['Surface'])
    else:
        bsdf = tree.nodes.new('ShaderNodeBsdfPrincipled')
        tree.links.new(ramp.outputs['Color'], bsdf.inputs['Base Color'])
        tree.links.new(bsdf.outputs[0], output.inputs['Surface'])
    return material


# ------------------------------------------------------------------------------------
#   GLB reading (pure Python: JSON chunk + BIN chunk)
# ------------------------------------------------------------------------------------

GLB_JSON = 0x4E4F534A
GLB_BIN = 0x004E4942


def read_glb(path):
    with open(path, 'rb') as handle:
        raw = handle.read()
    magic, version, total = struct.unpack_from('<III', raw, 0)
    if magic != 0x46546C67:
        raise ValueError('not a GLB file: {!r}'.format(magic))
    offset = 12
    chunks = {}
    while offset < total:
        length, kind = struct.unpack_from('<II', raw, offset)
        offset += 8
        if kind == GLB_JSON:
            chunks[GLB_JSON] = raw[offset:offset + length]
        elif kind == GLB_BIN:
            chunks[GLB_BIN] = raw[offset:offset + length]
        offset += length
    return json.loads(chunks[GLB_JSON].decode('utf-8').rstrip('\x00 ')), chunks.get(GLB_BIN, b'')


def glb_accessor(gltf, binary, index):
    accessor = gltf['accessors'][index]
    view = gltf['bufferViews'][accessor['bufferView']]
    components = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4}[accessor['type']]
    if accessor['componentType'] != 5126:
        raise ValueError('expected float accessor, got {}'.format(
            accessor['componentType']))
    stride = view.get('byteStride') or components * 4
    start = view.get('byteOffset', 0) + accessor.get('byteOffset', 0)
    rows = []
    for item in range(accessor['count']):
        rows.append(struct.unpack_from('<' + 'f' * components, binary,
                                       start + item * stride))
    return rows


# ------------------------------------------------------------------------------------
#   Tests
# ------------------------------------------------------------------------------------

material_bakery.register()
try:
    # ================================================================ 1. pure maths / plan
    print('--- 1. pure functions, plan and UI settings ---', flush=True)
    expected_px = {0.5: 4096.0, 1.0: 2048.0, 2.0: 1024.0}
    for tile, expected in expected_px.items():
        px_x, px_y = plan.pixels_per_meter(2048, 2048, tile)
        print('pixels_per_meter(2048,2048,{}) -> {} / {}'.format(tile, px_x, px_y),
              flush=True)
        check('px/m tile {}'.format(tile),
              abs(px_x - expected) < 1e-9 and abs(px_y - expected) < 1e-9,
              str((px_x, px_y)))
    expected_scale = {0.5: 2.0, 1.0: 1.0, 2.0: 0.5}
    for tile, expected in expected_scale.items():
        value = plan.tiling_map_scale(1.0, tile)
        print('tiling_map_scale(1,{}) -> {}'.format(tile, value), flush=True)
        check('map scale tile {}'.format(tile), abs(value - expected) < 1e-9, str(value))
    tiny = plan.tiling_map_scale(0.01, 1.0)
    print('tiling_map_scale(0.01,1) -> {}'.format(tiny), flush=True)
    check('map scale scene 0.01', abs(tiny - 0.01) < 1e-12, str(tiny))

    obj = make_scene()
    original_uvs = uv_snapshot(obj)
    tiling_settings = make_settings('TILING')
    tiling_plan = plan.build_plan(bpy.context, tiling_settings)
    groups = len(tiling_plan.groups)
    strategies = [group.uv_strategy for group in tiling_plan.groups]
    task_layouts = [task.options['per_material']['layout'] for task in tiling_plan.tasks]
    print('TILING groups={} uv_strategy={} task_layouts={} tasks={}'.format(
        groups, strategies, task_layouts, len(tiling_plan.tasks)), flush=True)
    check('TILING group count', groups == 2, str(groups))
    check('TILING every group', all(item == 'TILING' for item in strategies),
          str(strategies))
    check('TILING every task layout', all(item == 'TILING' for item in task_layouts),
          str(task_layouts))
    check('TILING option tile/scale',
          all(abs(task.options['per_material']['tile_size'] - 1.0) < 1e-9
              and abs(task.options['per_material']['scene_scale_length'] - 1.0) < 1e-9
              for task in tiling_plan.tasks), 'tile=1 scale=1')

    packed_plan = plan.build_plan(bpy.context, make_settings('PACKED'))
    print('PACKED groups={} tasks={}'.format(len(packed_plan.groups),
                                             len(packed_plan.tasks)), flush=True)
    check('PACKED group count unchanged', len(packed_plan.groups) == groups,
          str(len(packed_plan.groups)))
    unknown_plan = plan.build_plan(bpy.context, make_settings('SIDEWAYS'))
    unknown_warning = any('Unknown Per Material layout' in text
                          for text in unknown_plan.warnings)
    print('unknown layout warnings={} groups={}'.format(
        unknown_plan.warnings, [g.uv_strategy for g in unknown_plan.groups]), flush=True)
    check('unknown layout falls back to PACKED',
          unknown_warning and all(g.uv_strategy == 'PACKED'
                                  for g in unknown_plan.groups),
          str(unknown_plan.warnings))

    ui_settings = bpy.context.scene.mbakery
    check('layout / tile_size properties exist',
          hasattr(ui_settings, 'per_material_layout')
          and hasattr(ui_settings, 'tile_size'), 'per_material_layout/tile_size')
    ui_settings.per_material_layout = 'TILING'
    ui_settings.tile_size = 2.0
    bpy.context.scene.unit_settings.scale_length = 0.5
    converted = session.settings_to_bake_settings(ui_settings)
    print('settings_to_bake_settings layout={} tile={} scene_scale={}'.format(
        converted.per_material_layout, converted.tile_size,
        converted.scene_scale_length), flush=True)
    check('UI settings carry layout/tile/scale',
          converted.per_material_layout == 'TILING'
          and abs(converted.tile_size - 2.0) < 1e-9
          and abs(converted.scene_scale_length - 0.5) < 1e-9,
          str((converted.per_material_layout, converted.tile_size,
               converted.scene_scale_length)))
    bpy.context.scene.unit_settings.scale_length = 1.0
    migrated, notes = presets_mod.migrate(
        {'version': presets_mod.PRESET_VERSION, 'workflow': 'collection', 'maps': []})
    print('preset migrated layout={} notes={}'.format(
        migrated.get('per_material_layout'), notes), flush=True)
    check('old preset defaults to PACKED',
          migrated.get('per_material_layout') == 'PACKED',
          str(migrated.get('per_material_layout')))
    captured = ops_presets.capture(ui_settings)
    check('preset captures layout but not tile size',
          captured.get('per_material_layout') == 'TILING'
          and 'tile_size' not in captured, str(sorted(captured)))
    ui_settings.per_material_layout = 'PACKED'
    ui_settings.tile_size = 1.0

    # ================================================================ 2. sample surface
    print('--- 2. temporary 1 m sample surface ---', flush=True)
    task = tiling_plan.tasks[0]
    identity_before = (list(task.target_names), list(task.source_names))
    surface = per_material.create_task_surface(bpy.context, task)
    bpy.context.view_layer.update()
    surface_uv = layer_data(surface, 'UVMap')
    print('surface name={} dimensions.x={} BU*scale={} uv_min={} uv_max={}'.format(
        surface.name, surface.dimensions.x,
        surface.dimensions.x * bpy.context.scene.unit_settings.scale_length,
        surface_uv.min(axis=0), surface_uv.max(axis=0)), flush=True)
    check('surface world size 1 m',
          abs(surface.dimensions.x
              * bpy.context.scene.unit_settings.scale_length - 1.0) < 1e-6,
          str(surface.dimensions.x))
    check('surface UV 0-1',
          np.allclose(surface_uv.min(axis=0), (0.0, 0.0))
          and np.allclose(surface_uv.max(axis=0), (1.0, 1.0)),
          str((surface_uv.min(axis=0), surface_uv.max(axis=0))))
    check('surface tagged', bool(surface.get(per_material.SAMPLE_TAG)), surface.name)
    check('surface linked to scene',
          surface.name in bpy.context.scene.collection.objects, surface.name)
    check('surface one material', len(surface.material_slots) == 1
          and surface.material_slots[0].material.name == 'TilingMat0',
          str([slot.material.name if slot.material else None
               for slot in surface.material_slots]))
    check('surface identity untouched',
          identity_before == (list(task.target_names), list(task.source_names)),
          str(identity_before))
    per_material.remove_task_surface(surface)
    check('surface removed after use', len(tagged_surfaces()) == 0,
          str(len(tagged_surfaces())))

    bpy.context.scene.unit_settings.scale_length = 0.01
    tiny_plan = plan.build_plan(bpy.context, make_settings('TILING', scale_length=0.01))
    tiny_surface = per_material.create_task_surface(bpy.context, tiny_plan.tasks[0])
    bpy.context.view_layer.update()
    tiny_uv = layer_data(tiny_surface, 'UVMap')
    print('surface scale=0.01 dimensions.x={} BU world={} m uv={}'.format(
        tiny_surface.dimensions.x,
        tiny_surface.dimensions.x * bpy.context.scene.unit_settings.scale_length,
        (tiny_uv.min(axis=0), tiny_uv.max(axis=0))), flush=True)
    check('surface world size 1 m at scale 0.01',
          abs(tiny_surface.dimensions.x
              * bpy.context.scene.unit_settings.scale_length - 1.0) < 1e-6,
          str(tiny_surface.dimensions.x))
    check('surface UV 0-1 at scale 0.01',
          np.allclose(tiny_uv.min(axis=0), (0.0, 0.0))
          and np.allclose(tiny_uv.max(axis=0), (1.0, 1.0)),
          str((tiny_uv.min(axis=0), tiny_uv.max(axis=0))))
    per_material.remove_task_surface(tiny_surface)
    bpy.context.scene.unit_settings.scale_length = 1.0
    check('surface scale 0.01 removed', len(tagged_surfaces()) == 0,
          str(len(tagged_surfaces())))

    # ================================================================ 3. write_tiling_uv
    print('--- 3. world Box UV layer (2 m cube, Tile Size 2) ---', flush=True)
    clear_scene()
    cube = make_cube('MB_R6Cube2m', 2.0)
    select_only(cube)
    old_layers = uv_snapshot(cube)
    info = per_material.write_tiling_uv(cube, 2.0, 1.0)
    tiled_name = info['uv_layer']
    tiled = cube.data.uv_layers[0]
    bounds = face_uv_bounds(cube, tiled_name)
    print('write tile=2: name={} index={} faces={} axes(x,y,z)=({},{},{}) '
          'u=[{:.6f},{:.6f}] v=[{:.6f},{:.6f}] active_render={}'.format(
              tiled_name, info['layer_index'], info['faces'], info['x_faces'],
              info['y_faces'], info['z_faces'], info['u_min'], info['u_max'],
              info['v_min'], info['v_max'], bool(tiled.active_render)), flush=True)
    for axis, umin, umax, vmin, vmax in bounds:
        print('  face axis={} u=[{:.6f},{:.6f}] v=[{:.6f},{:.6f}]'.format(
            axis, umin, umax, vmin, vmax), flush=True)
    check('cube has 6 faces', info['faces'] == 6, str(info['faces']))
    check('cube axis count x=y=z=2',
          (info['x_faces'], info['y_faces'], info['z_faces']) == (2, 2, 2),
          str((info['x_faces'], info['y_faces'], info['z_faces'])))
    check('Tile Size 2 -> u covers 0..1',
          abs(info['u_min']) < 1e-9 and abs(info['u_max'] - 1.0) < 1e-9,
          '[{:.9f},{:.9f}]'.format(info['u_min'], info['u_max']))
    check('Tile Size 2 -> v covers 0..1',
          abs(info['v_min']) < 1e-9 and abs(info['v_max'] - 1.0) < 1e-9,
          '[{:.9f},{:.9f}]'.format(info['v_min'], info['v_max']))
    check('every face covers 0..1',
          all(abs(umin) < 1e-9 and abs(umax - 1.0) < 1e-9
              and abs(vmin) < 1e-9 and abs(vmax - 1.0) < 1e-9
              for _axis, umin, umax, vmin, vmax in bounds), str(bounds))
    residual = linear_fit_residual(cube, tiled_name)
    print('linear fit residual (world -> uv) max diff {:.6f}'.format(residual),
          flush=True)
    check('UV is linear in world space', residual < 1e-6, '{:.9f}'.format(residual))
    check('Tiled is uv_layers[0]', tiled.name == tiled_name
          and cube.data.uv_layers[0].name == tiled_name, tiled.name)
    check('Tiled is active_render', bool(tiled.active_render), str(tiled.active_render))
    drift, missing, extra = uv_drift(old_layers, cube)
    print('old layers: before={} after={} missing={} extra={} max diff {:.6f}'.format(
        sorted(old_layers), sorted(uv_snapshot(cube)), missing, extra, drift), flush=True)
    check('old layer count unchanged (+Tiled)', not missing
          and sorted(extra) == [tiled_name], str((missing, extra)))
    check('old layer values unchanged', drift == 0.0, '{:.9f}'.format(drift))
    check('object markers written',
          cube.get(per_material.TILING_UV_MARKER) == tiled_name
          and abs(cube.get(per_material.TILING_TILE_SIZE) - 2.0) < 1e-9
          and abs(cube.get(per_material.TILING_SCALE_LENGTH) - 1.0) < 1e-9,
          str({key: cube.get(key) for key in per_material.TILING_OBJECT_MARKERS}))
    check('original render layer recorded',
          cube.get(per_material.TILING_ORIG_RENDER_UV) == 'OriginalUV',
          repr(cube.get(per_material.TILING_ORIG_RENDER_UV)))
    check('original active_index recorded',
          int(cube.get(per_material.TILING_ORIG_UV_INDEX)) == 0,
          repr(cube.get(per_material.TILING_ORIG_UV_INDEX)))

    print('--- 4. Tile Size 1 -> UV 0..2 and recomputation ---', flush=True)
    info1 = per_material.write_tiling_uv(cube, 1.0, 1.0)
    bounds1 = face_uv_bounds(cube, info1['uv_layer'])
    print('write tile=1: u=[{:.6f},{:.6f}] v=[{:.6f},{:.6f}]'.format(
        info1['u_min'], info1['u_max'], info1['v_min'], info1['v_max']), flush=True)
    for axis, umin, umax, vmin, vmax in bounds1:
        print('  face axis={} u=[{:.6f},{:.6f}] v=[{:.6f},{:.6f}]'.format(
            axis, umin, umax, vmin, vmax), flush=True)
    check('Tile Size 1 -> u covers 0..2',
          abs(info1['u_min']) < 1e-9 and abs(info1['u_max'] - 2.0) < 1e-9,
          '[{:.9f},{:.9f}]'.format(info1['u_min'], info1['u_max']))
    check('Tile Size 1 -> v covers 0..2',
          abs(info1['v_min']) < 1e-9 and abs(info1['v_max'] - 2.0) < 1e-9,
          '[{:.9f},{:.9f}]'.format(info1['v_min'], info1['v_max']))
    residual1 = linear_fit_residual(cube, info1['uv_layer'])
    print('tile=1 linear fit residual max diff {:.6f}'.format(residual1), flush=True)
    check('tile=1 UV still linear', residual1 < 1e-6, '{:.9f}'.format(residual1))
    drift1, missing1, extra1 = uv_drift(old_layers, cube)
    check('recompute leaves the old layer untouched', drift1 == 0.0,
          '{:.9f}'.format(drift1))
    updated = per_material.update_tiling_uv(2.0, 1.0)
    actual = loop_uv(cube, info['uv_layer'])
    expected = expected_tiling_uv(cube, 2.0, 1.0)
    max_diff = float(np.max(np.abs(actual - expected))) if actual.size else 0.0
    print('update_tiling_uv(2.0) recomputed={} uv_vs_formula max diff {:.6f}'.format(
        updated, max_diff), flush=True)
    check('update_tiling_uv recomputes the marked object', updated == 1, str(updated))
    check('updated UV matches the new formula', max_diff < 1e-9,
          '{:.9f}'.format(max_diff))
    drift2, _missing2, _extra2 = uv_drift(old_layers, cube)
    check('update leaves the old layer untouched', drift2 == 0.0,
          '{:.9f}'.format(drift2))

    print('--- 5. scene scale 0.01 (100 BU = 1 m) ---', flush=True)
    clear_scene()
    big = make_cube('MB_R6Cube2mScale', 200.0)
    select_only(big)
    big_info = per_material.write_tiling_uv(big, 2.0, 0.01)
    print('scale=0.01 200 BU (=2 m) tile=2: u=[{:.6f},{:.6f}] v=[{:.6f},{:.6f}]'.format(
        big_info['u_min'], big_info['u_max'], big_info['v_min'],
        big_info['v_max']), flush=True)
    check('scale 0.01 / 2 m / tile 2 -> u 0..1',
          abs(big_info['u_min']) < 1e-9 and abs(big_info['u_max'] - 1.0) < 1e-9,
          '[{:.9f},{:.9f}]'.format(big_info['u_min'], big_info['u_max']))
    check('scale 0.01 / 2 m / tile 2 -> v 0..1',
          abs(big_info['v_min']) < 1e-9 and abs(big_info['v_max'] - 1.0) < 1e-9,
          '[{:.9f},{:.9f}]'.format(big_info['v_min'], big_info['v_max']))
    per_material.remove_tiling_uvs([big])
    small = make_cube('MB_R6Cube1mScale', 100.0)
    select_only(small)
    small_info = per_material.write_tiling_uv(small, 1.0, 0.01)
    print('scale=0.01 100 BU (=1 m) tile=1: u=[{:.6f},{:.6f}]'.format(
        small_info['u_min'], small_info['u_max']), flush=True)
    check('scale 0.01 / 1 m / tile 1 -> u 0..1',
          abs(small_info['u_min']) < 1e-9 and abs(small_info['u_max'] - 1.0) < 1e-9,
          '[{:.9f},{:.9f}]'.format(small_info['u_min'], small_info['u_max']))
    per_material.remove_tiling_uvs([small])
    bpy.context.scene.unit_settings.scale_length = 1.0

    print('--- 6. preflight errors (shared mesh / edit mode) ---', flush=True)
    clear_scene()
    shared = make_cube('MB_R6Shared', 1.0)
    second = bpy.data.objects.new('MB_R6SharedUser', shared.data)
    bpy.context.scene.collection.objects.link(second)
    shared_error = ''
    try:
        per_material.write_tiling_uv(shared, 1.0, 1.0)
    except RuntimeError as exc:
        shared_error = str(exc)
    print('shared mesh error={!r} layers={}'.format(shared_error,
                                                    [l.name for l in shared.data.uv_layers]),
          flush=True)
    check('shared mesh raises', bool(shared_error), repr(shared_error))
    check('shared mesh error names the rule',
          'single-user' in shared_error.lower(), repr(shared_error))
    bpy.data.objects.remove(second, do_unlink=True)
    select_only(shared)
    bpy.ops.object.mode_set(mode='EDIT')
    edit_error = ''
    try:
        per_material.write_tiling_uv(shared, 1.0, 1.0)
    except RuntimeError as exc:
        edit_error = str(exc)
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')
    print('edit mode error={!r}'.format(edit_error), flush=True)
    check('edit mode raises', bool(edit_error), repr(edit_error))
    check('edit mode error names the rule',
          'object mode' in edit_error.lower(), repr(edit_error))
    check('failed writes left no Tiled layer',
          shared.get(per_material.TILING_UV_MARKER) in (None, '')
          and shared.data.uv_layers.get(per_material.TILING_UV_NAME) is None,
          str([l.name for l in shared.data.uv_layers]))

    print('--- 7. user layer already called "Tiled" ---', flush=True)
    clear_scene()
    user_obj = make_quad('MB_R6UserTiled', 0.0, 1.0, 0.0, 1.0)
    select_only(user_obj)
    user_layer = user_obj.data.uv_layers.new(name='Tiled')
    for loop in user_obj.data.loops:
        user_layer.data[loop.index].uv = (0.25 + loop.index * 0.01,
                                          0.75 - loop.index * 0.01)
    sentinel = layer_data(user_obj, 'Tiled')
    bug2 = per_material.write_tiling_uv(user_obj, 1.0, 1.0)
    actual_name = user_obj.get(per_material.TILING_UV_MARKER)
    user_after = layer_data(user_obj, 'Tiled')
    print('user Tiled kept as "Tiled"; ours={} index={} user_diff={:.6f}'.format(
        actual_name, user_obj.data.uv_layers.find(actual_name),
        float(np.max(np.abs(user_after - sentinel)))), flush=True)
    check('a user "Tiled" layer forces a unique name', actual_name == 'Tiled.001',
          repr(actual_name))
    check('the unique layer is uv_layers[0]',
          user_obj.data.uv_layers[0].name == actual_name, actual_name)
    check('the user "Tiled" layer keeps its values',
          float(np.max(np.abs(user_after - sentinel))) == 0.0, 'unchanged')
    check('the user "Tiled" layer is still there',
          user_obj.data.uv_layers.get('Tiled') is not None, 'kept')
    removed = per_material.remove_tiling_uvs([user_obj])
    print('remove_tiling_uvs -> removed={} layers={} render={} marker={}'.format(
        removed, [l.name for l in user_obj.data.uv_layers],
        [l.name for l in user_obj.data.uv_layers if l.active_render],
        user_obj.get(per_material.TILING_UV_MARKER)), flush=True)
    check('remove keeps the user "Tiled" layer',
          user_obj.data.uv_layers.get('Tiled') is not None, 'kept')
    check('remove leaves the user layer data untouched',
          float(np.max(np.abs(layer_data(user_obj, 'Tiled') - sentinel))) == 0.0,
          'unchanged')
    check('remove restores the user layer as render layer',
          bool(user_obj.data.uv_layers['Tiled'].active_render), 'active_render')
    check('remove clears the tiling markers',
          user_obj.get(per_material.TILING_UV_MARKER) in (None, ''), 'cleared')

    # ================================================================ 8. repeat sampling
    print('--- 8. REPEAT sampling at UV (1.5, 0.5) vs (0.5, 0.5) ---', flush=True)
    clear_scene()
    probe_image = make_probe_image()
    repeat_plane = make_quad('MB_R6RepeatPlane', 0.0, 2.0, 0.0, 1.0)
    select_only(repeat_plane)
    per_material.write_tiling_uv(repeat_plane, 1.0, 1.0)
    repeat_material, _repeat_report = deliver_materials.build_final_material(
        MaterialStore(), 'MB_R6RepeatMaterial', {'shader.base_color': probe_image},
        tiling=tiling_payload(1.0, 1.0))
    emissionize(repeat_material)
    repeat_plane.data.materials.append(repeat_material)
    repeat_pixels = render_top(repeat_plane, size=32, center=(1.0, 0.5), span=2.0)
    row = 16
    col_a, col_b = 8, 24          # u = 0.53125 and u = 1.53125
    value_a = float(repeat_pixels[row, col_a, 0])
    value_b = float(repeat_pixels[row, col_b, 0])
    repeat_diff = abs(value_a - value_b)
    image_node = next(node for node in repeat_material.node_tree.nodes
                      if node.type == 'TEX_IMAGE')
    print('repeat: u=1.53125 -> {:.6f}  u=0.53125 -> {:.6f}  diff {:.9f}  '
          'extension={}'.format(value_b, value_a, repeat_diff,
                                image_node.extension), flush=True)
    check('image extension is REPEAT', image_node.extension == 'REPEAT',
          image_node.extension)
    check('REPEAT samples the same texel one tile apart', repeat_diff < 1e-6,
          '{:.9f}'.format(repeat_diff))
    check('both samples carry the probe value', value_a > 0.0 and value_b > 0.0,
          str((value_a, value_b)))

    # ================================================================ 9. delivered material
    print('--- 9. delivered material shape ---', flush=True)
    clear_scene()
    store = MaterialStore()
    material, report = deliver_materials.build_final_material(
        store, 'MB_R6TilingMaterial',
        {'shader.base_color': probe_image, 'shader.normal': probe_image},
        tiling=tiling_payload(2.0, 0.01))
    tree = material.node_tree
    texcoord = [node for node in tree.nodes if node.type == 'TEX_COORD']
    mapping = [node for node in tree.nodes if node.type == 'MAPPING']
    images = [node for node in tree.nodes if node.type == 'TEX_IMAGE']
    vector_links = sum(len(node.inputs['Vector'].links) for node in images)
    normal_map = next((node for node in tree.nodes if node.type == 'NORMAL_MAP'), None)
    extensions = [node.extension for node in images]
    projections = [node.projection for node in images]
    markers = {key: material.get(key) for key in deliver_materials.TILING_MARKERS}
    print('nodes: TEX_COORD={} MAPPING={} images={} vector_links={} '
          'extensions={} projections={} normal_map={}'.format(
              len(texcoord), len(mapping), len(images), vector_links,
              extensions, projections, normal_map is not None), flush=True)
    print('markers={} notes={}'.format(markers, report.notes), flush=True)
    check('no TEX_COORD node', not texcoord, str([n.name for n in texcoord]))
    check('no MAPPING node', not mapping, str([n.name for n in mapping]))
    check('no image node has a Vector link', vector_links == 0, str(vector_links))
    check('every image (normal included) is REPEAT',
          len(images) == 2 and all(item == 'REPEAT' for item in extensions),
          str(extensions))
    check('no image is BOX projected', all(item != 'BOX' for item in projections),
          str(projections))
    check('normal still goes through Normal Map', normal_map is not None, 'missing')
    check('tiling markers carry tile/scale',
          markers.get(deliver_materials.TILING_MARKER) == 1
          and abs(float(markers.get(deliver_materials.TILING_TILE_SIZE, 0.0)) - 2.0) < 1e-9
          and abs(float(markers.get(
              deliver_materials.TILING_SCALE_LENGTH, 0.0)) - 0.01) < 1e-9,
          str(markers))
    check('no normal tangent caveat is reported any more', not report.notes,
          str(report.notes))
    check('TILING_REFERENCE is gone',
          not hasattr(deliver_materials, 'TILING_REFERENCE'), 'absent')
    check('TILING_NORMAL_NOTE is gone',
          not hasattr(deliver_materials, 'TILING_NORMAL_NOTE'), 'absent')

    flat_material, _flat_report = deliver_materials.build_final_material(
        store, 'MB_R6TilingMaterial', {'shader.base_color': probe_image})
    flat_tree = flat_material.node_tree
    flat_coords = [node.type for node in flat_tree.nodes
                   if node.type in ('TEX_COORD', 'MAPPING')]
    flat_image = next(node for node in flat_tree.nodes if node.type == 'TEX_IMAGE')
    flat_markers = [key for key in deliver_materials.TILING_MARKERS
                    if key in flat_material]
    print('flat rebuild: coord_nodes={} vector_links={} markers={}'.format(
        flat_coords, len(flat_image.inputs['Vector'].links), flat_markers), flush=True)
    check('flat rebuild has no coordinate nodes', not flat_coords, str(flat_coords))
    check('flat rebuild has no Vector links',
          not flat_image.inputs['Vector'].links, 'links')
    check('flat rebuild clears every tiling marker', not flat_markers,
          str(flat_markers))

    # ================================================================ 10. real bake
    print('--- 10. real Tiling bake + original UV drift ---', flush=True)
    obj = make_scene()
    original_uvs = uv_snapshot(obj)
    original_matrix = obj.matrix_world.copy()
    out_dir = tempfile.mkdtemp(prefix='mb_r6_tiling_')
    real_settings = make_settings('TILING')
    real_plan = plan.build_plan(bpy.context, real_settings)
    real_task = real_plan.tasks[0]
    tx = transaction.SceneTransaction(bpy.context)
    cycles = backend.CyclesBackend(tx, margin=0, samples=1, device='CPU',
                                   save_directory=out_dir, save_files=True)
    started = time.perf_counter()
    bake_job = job.BakeJob(bpy.context, real_plan, real_settings, backend=cycles,
                           transaction=tx)
    bake_job.run_to_completion()
    elapsed = time.perf_counter() - started
    image = real_task.image
    pixels = np.array(image.pixels[:], dtype=float).reshape(-1, 4)
    nonempty = int(np.sum(np.max(pixels[:, :3], axis=1) > 0.01))
    exported = real_task.exported_path
    print('bake: state={} status={} seconds={:.3f} nonempty_px={} exported={}'.format(
        bake_job.state, real_task.status, elapsed, nonempty, exported), flush=True)
    check('tiling bake done', bake_job.state == 'done'
          and real_task.status == 'done', bake_job.state)
    check('tiling bake nonempty', nonempty > 0, str(nonempty))
    check('tiling exported file exists', bool(exported) and os.path.isfile(exported),
          str(exported))
    check('tiling margin forced to 0',
          any('margin forced to 0' in text for text in real_task.surgery_detail),
          str(real_task.surgery_detail))
    check('sample surface gone after success', len(tagged_surfaces()) == 0,
          str(len(tagged_surfaces())))
    drift, missing, extra = uv_drift(original_uvs, obj)
    matrix_delta = max(abs(float(original_matrix[row][col])
                           - float(obj.matrix_world[row][col]))
                       for row in range(4) for col in range(4))
    print('after bake: uv layers={} missing={} extra={} max_uv_diff={:.6f} '
          'matrix_delta={:.6f}'.format(sorted(uv_snapshot(obj)), missing, extra,
                                       drift, matrix_delta), flush=True)
    check('bake leaves every original UV layer unchanged',
          drift == 0.0 and not missing, '{:.9f} {}'.format(drift, missing))
    check('bake writes no Tiled layer by itself',
          not extra and obj.data.uv_layers.get('Tiled') is None, str(extra))
    check('object matrix untouched by the bake', matrix_delta == 0.0,
          '{:.9f}'.format(matrix_delta))

    # ================================================================ 11. GLB export
    print('--- 11. exported GLB still carries TEXCOORD_0 ---', flush=True)
    clear_scene()
    cube = make_cube('MB_R6ExportCube', 2.0)
    select_only(cube)
    per_material.write_tiling_uv(cube, 2.0, 1.0)
    tiled_name = cube.get(per_material.TILING_UV_MARKER)
    export_material, _export_report = deliver_materials.build_final_material(
        MaterialStore(), 'MB_R6ExportMaterial', {'shader.base_color': image},
        tiling=tiling_payload(2.0, 1.0))
    cube.data.materials.append(export_material)
    glb_path = os.path.join(tempfile.gettempdir(), 'mb_r6_tiling_export.glb')
    select_only(cube)
    bpy.ops.export_scene.gltf(filepath=glb_path, export_format='GLB',
                              use_selection=True, export_texcoords=True,
                              export_normals=True, export_materials='EXPORT')
    gltf, binary = read_glb(glb_path)
    primitive = gltf['meshes'][0]['primitives'][0]
    attribute_index = primitive['attributes'].get('TEXCOORD_0')
    exported_uv = glb_accessor(gltf, binary, attribute_index)
    expected_uv = loop_uv(cube, tiled_name)
    expected = np.array([(u, 1.0 - v) for u, v in expected_uv], dtype=np.float32)
    actual = np.array(exported_uv, dtype=np.float32)
    expected_sorted = expected[np.lexsort((expected[:, 1], expected[:, 0]))]
    actual_sorted = actual[np.lexsort((actual[:, 1], actual[:, 0]))]
    glb_diff = (float(np.max(np.abs(actual_sorted - expected_sorted)))
                if actual_sorted.shape == expected_sorted.shape else float('inf'))
    print('GLB: file={} exists={} attribute={} uv_count={} vs loops={}'.format(
        os.path.basename(glb_path), os.path.isfile(glb_path), attribute_index,
        actual_sorted.shape[0], expected_sorted.shape[0]), flush=True)
    print('GLB TEXCOORD_0 vs (u, 1-v) sorted: max diff {:.6f}  '
          'range u=[{:.6f},{:.6f}] v=[{:.6f},{:.6f}]'.format(
              glb_diff, float(actual_sorted[:, 0].min()),
              float(actual_sorted[:, 0].max()),
              float(actual_sorted[:, 1].min()),
              float(actual_sorted[:, 1].max())), flush=True)
    check('GLB exported', os.path.isfile(glb_path), glb_path)
    check('GLB has TEXCOORD_0', attribute_index is not None, str(attribute_index))
    check('GLB TEXCOORD_0 count matches the Tiled loops',
          actual_sorted.shape == expected_sorted.shape,
          '{} vs {}'.format(actual_sorted.shape, expected_sorted.shape))
    check('GLB TEXCOORD_0 equals (u, 1-v) of the Tiled layer',
          glb_diff < 1e-6, '{:.9f}'.format(glb_diff))
    materials_json = gltf.get('materials', [])
    exported_material = materials_json[0] if materials_json else {}
    material_keys = sorted(exported_material.keys())
    material_json_text = json.dumps(exported_material)
    print('GLB material keys={} extensionsUsed={}'.format(
        material_keys, gltf.get('extensionsUsed', [])), flush=True)
    check('GLB material uses only standard fields',
          'extensions' not in exported_material and 'KHR' not in material_json_text,
          str(material_keys))
    texture_index = (exported_material.get('pbrMetallicRoughness', {})
                     .get('baseColorTexture', {}).get('index'))
    texture = gltf.get('textures', [])[texture_index] if texture_index is not None else {}
    sampler_index = texture.get('sampler')
    sampler = gltf.get('samplers', [])[sampler_index] if sampler_index is not None else {}
    wrap_s = sampler.get('wrapS', 10497)
    wrap_t = sampler.get('wrapT', 10497)
    print('GLB sampler={} -> wrapS={} wrapT={} (10497 = REPEAT)'.format(
        sampler, wrap_s, wrap_t), flush=True)
    check('GLB sampler wraps with REPEAT (glTF default when omitted)',
          wrap_s == 10497 and wrap_t == 10497, str((wrap_s, wrap_t)))

    # ================================================================ 12. Tile Size no re-bake
    print('--- 12. Tile Size change recomputes UV, no re-bake ---', flush=True)
    file_hash_before = file_sha1(exported)
    pixel_hash_before = image_signature(image)
    old_before = {name: values for name, values in uv_snapshot(cube).items()
                  if name != tiled_name}
    tiled_before = loop_uv(cube, tiled_name)
    changed = per_material.update_tiling_uv(1.0, 1.0)
    expected = expected_tiling_uv(cube, 1.0, 1.0)
    actual = loop_uv(cube, tiled_name)
    diff = float(np.max(np.abs(actual - expected))) if actual.size else 0.0
    drift, missing, extra = uv_drift(old_before, cube)
    file_hash_after = file_sha1(exported)
    pixel_hash_after = image_signature(image)
    print('update(1.0): objects={} uv_vs_formula={:.6f} layers={} old_diff={:.6f} '
          'file_sha_same={} pixel_sha_same={}'.format(
              changed, diff, sorted(uv_snapshot(cube)), drift,
              file_hash_before == file_hash_after,
              pixel_hash_before == pixel_hash_after), flush=True)
    check('update_tiling_uv hits the marked object', changed == 1, str(changed))
    check('new Tile Size matches the formula exactly', diff < 1e-9,
          '{:.9f}'.format(diff))
    check('old layer survives the Tile Size change',
          drift == 0.0 and not missing, '{:.9f} {}'.format(drift, missing))
    check('baked image file sha1 unchanged', file_hash_before == file_hash_after,
          '{} vs {}'.format(file_hash_before, file_hash_after))
    check('baked image pixels unchanged', pixel_hash_before == pixel_hash_after,
          '{} vs {}'.format(pixel_hash_before[0], pixel_hash_after[0]))
    check('Tiled still leads the layer stack',
          cube.data.uv_layers[0].name == tiled_name, cube.data.uv_layers[0].name)

    # ================================================================ 13. apply / restore
    print('--- 13. apply_materials pins materials, restore removes Tiled ---', flush=True)
    obj = make_scene()
    old_uvs = uv_snapshot(obj)
    apply_plan = plan.build_plan(bpy.context, make_settings('TILING'))
    for item in apply_plan.tasks:
        item.status = 'done'
    # The original materials need an image node for the UV pin to have something
    # to pin (plain Principled materials have no texture coordinates at all).
    for index in range(2):
        original = bpy.data.materials.get('TilingMat{}'.format(index))
        tree = original.node_tree
        texture_node = tree.nodes.new('ShaderNodeTexImage')
        texture_node.image = probe_image
        bsdf = tree.nodes.get('Principled BSDF')
        tree.links.new(texture_node.outputs['Color'], bsdf.inputs['Base Color'])
    by_group = deliver_materials.build_for_groups(
        MaterialStore(),
        {group.name: {'shader.base_color': probe_image} for group in apply_plan.groups},
        tiling=tiling_payload(1.0, 1.0))
    original_write = per_material.write_tiling_uv
    write_calls = []

    def counted_write(target, tile, scale):
        write_calls.append(target.name)
        return original_write(target, tile, scale)

    per_material.write_tiling_uv = counted_write
    try:
        applied = per_material.apply_materials(apply_plan, by_group,
                                               tiling=tiling_payload(1.0, 1.0))
    finally:
        per_material.write_tiling_uv = original_write
    tiled_name = obj.get(per_material.TILING_UV_MARKER)
    pinned = {}
    for index in range(2):
        material = bpy.data.materials.get('TilingMat{}'.format(index))
        uv_nodes = [node for node in material.node_tree.nodes if node.type == 'UVMAP']
        pinned[index] = [node.uv_map for node in uv_nodes]
    print('apply: applied={} write_calls={} tiled={} index={} render={} '
          'pins={} slots={}'.format(
              applied, write_calls, tiled_name,
              obj.data.uv_layers.find(tiled_name),
              [l.name for l in obj.data.uv_layers if l.active_render],
              pinned, [slot.material.name for slot in obj.material_slots]), flush=True)
    check('apply_materials replaces the two slots on one object', applied == 1,
          str(applied))
    check('UV layer is written exactly once for the object', write_calls == ['TilingSource'],
          str(write_calls))
    check('Tiled exists at uv_layers[0] after apply',
          tiled_name == 'Tiled' and obj.data.uv_layers[0].name == tiled_name, tiled_name)
    check('Tiled is the render layer after apply',
          bool(obj.data.uv_layers[tiled_name].active_render), 'active_render')
    check('both original materials are pinned to the initial render layer',
          pinned[0] == ['OriginalUV'] and pinned[1] == ['OriginalUV'], str(pinned))
    check('both slots show the delivered material',
          all(slot.material.get(deliver_materials.TILING_MARKER)
              for slot in obj.material_slots),
          str([slot.material.name for slot in obj.material_slots]))

    restored = slots_mod.restore_objects([obj])
    layers_after_restore = sorted(layer.name for layer in obj.data.uv_layers)
    drift, missing, extra = uv_drift(old_uvs, obj)
    print('restore: outcomes={} layers={} render={} markers={} old_diff={:.6f}'.format(
        [str(item) for item in restored], layers_after_restore,
        [l.name for l in obj.data.uv_layers if l.active_render],
        {key: obj.get(key) for key in per_material.TILING_OBJECT_MARKERS}, drift),
        flush=True)
    check('restore succeeds', any(item.ok for item in restored),
          str([str(item) for item in restored]))
    check('restore removes the Tiled layer', 'Tiled' not in layers_after_restore,
          str(layers_after_restore))
    check('restore puts the render layer back on OriginalUV',
          bool(obj.data.uv_layers['OriginalUV'].active_render), 'active_render')
    check('restore clears every tiling object marker',
          all(obj.get(key) in (None, '') for key in per_material.TILING_OBJECT_MARKERS),
          str({key: obj.get(key) for key in per_material.TILING_OBJECT_MARKERS}))
    check('restore keeps every original UV value',
          drift == 0.0 and not missing, '{:.9f} {} {}'.format(drift, missing, extra))
    restored_again = slots_mod.restore_objects([obj])
    print('restore again: outcomes={} layers={}'.format(
        [str(item) for item in restored_again],
        sorted(layer.name for layer in obj.data.uv_layers)), flush=True)
    check('second restore is idempotent',
          'Tiled' not in [layer.name for layer in obj.data.uv_layers]
          and sorted(layer.name for layer in obj.data.uv_layers)
          == ['OriginalUV'], str([str(item) for item in restored_again]))

    # ---------------------------------------------------------------- preflight abort
    clear_scene()
    left = make_quad('MB_R6PreflightA', 0.0, 1.0, 0.0, 1.0)
    right = bpy.data.objects.new('MB_R6PreflightB', left.data)
    bpy.context.scene.collection.objects.link(right)
    left.data.uv_layers.new(name='OriginalUV')
    preflight_material, _preflight_report = deliver_materials.build_final_material(
        MaterialStore(), 'MB_R6PreflightMaterial',
        {'shader.base_color': probe_image}, tiling=tiling_payload(1.0, 1.0))
    preflight_plan = plan.BakePlan(
        groups=[scene_scan.ObjectGroup(name='G', objects=[left, right])],
        assume_all_succeeded=True)
    preflight_error = ''
    try:
        per_material.apply_materials(
            preflight_plan, {'G': (preflight_material, None)},
            tiling=tiling_payload(1.0, 1.0))
    except RuntimeError as exc:
        preflight_error = str(exc)
    print('preflight: error={!r} left_layers={} right_layers={}'.format(
        preflight_error, [l.name for l in left.data.uv_layers],
        [l.name for l in right.data.uv_layers]), flush=True)
    check('apply preflight raises for a shared mesh', bool(preflight_error),
          repr(preflight_error))
    check('apply preflight writes no UV layer before failing',
          'Tiled' not in [layer.name for layer in left.data.uv_layers],
          str([layer.name for layer in left.data.uv_layers]))

    # ================================================================ 14. session wiring
    print('--- 14. session build_materials -> apply_materials (one payload) ---', flush=True)
    clear_scene()
    delivery_obj = make_delivery_object('MB_R6SessionObject')
    select_only(delivery_obj)
    session_settings = make_settings('TILING', tile_size=2.0)
    session_plan = plan.build_plan(bpy.context, session_settings)
    for item in session_plan.tasks:
        item.status = 'done'
    group_name = session_plan.groups[0].name

    class DummyJob:
        def __init__(self, settings, current_plan):
            self.settings = settings
            self.plan = current_plan

    tiling_session = session.Session()
    tiling_session.job = DummyJob(session_settings, session_plan)
    tiling_session.imported_by_group = {
        group_name: {'shader.base_color': probe_image}}
    built, _reports = tiling_session.build_materials(ui_settings)
    payload = getattr(tiling_session, 'tiling_payload', None)
    print('session build: built={} payload={} materials={}'.format(
        built, payload, [entry[0].name
                         for entry in tiling_session.materials_by_group.values()]),
        flush=True)
    check('build_materials caches the tiling payload',
          payload is not None and abs(payload['tile_size'] - 2.0) < 1e-9, str(payload))
    added = tiling_session.apply_materials(ui_settings)
    tiled_session_name = delivery_obj.get(per_material.TILING_UV_MARKER)
    print('session apply: added={} tiled={} render={}'.format(
        added, tiled_session_name,
        [l.name for l in delivery_obj.data.uv_layers if l.active_render]), flush=True)
    check('a later Add Slot still writes the Tiled layer',
          added == 1 and tiled_session_name == 'Tiled'
          and delivery_obj.data.uv_layers[0].name == 'Tiled', repr(tiled_session_name))
    check('the delivered material came from the same run',
          all(entry[0].get(deliver_materials.TILING_MARKER)
              for entry in tiling_session.materials_by_group.values()),
          'tiling marker')
    tiling_session.close_log_file()
    slots_mod.restore_objects([delivery_obj])

    packed_settings = make_settings('PACKED')
    packed_plan = plan.build_plan(bpy.context, packed_settings)
    for item in packed_plan.tasks:
        item.status = 'done'
    packed_session = session.Session()
    packed_session.job = DummyJob(packed_settings, packed_plan)
    packed_session.imported_by_group = {
        packed_plan.groups[0].name: {'shader.base_color': probe_image}}
    packed_built, _packed_reports = packed_session.build_materials(ui_settings)
    packed_payload = getattr(packed_session, 'tiling_payload', None)
    packed_added = packed_session.apply_materials(ui_settings)
    print('packed session: built={} payload={} added={} marker={}'.format(
        packed_built, packed_payload, packed_added,
        delivery_obj.get(per_material.TILING_UV_MARKER)), flush=True)
    check('PACKED build keeps the payload None', packed_payload is None,
          str(packed_payload))
    check('PACKED apply writes no Tiled layer',
          packed_added == 1
          and delivery_obj.get(per_material.TILING_UV_MARKER) in (None, ''),
          repr(delivery_obj.get(per_material.TILING_UV_MARKER)))
    packed_session.close_log_file()
    clear_tiling_materials()

    # ================================================================ 15. Cycles comparison
    print('--- 15. baked sample plane vs procedural Tiled render (MAE) ---', flush=True)
    clear_scene()
    gradient_plane = make_plane('MB_R6GradientPlane', 1.0)
    gradient_plane.location = (0.5, 0.5, 0.0)
    bake_material = make_gradient_material('MB_R6GradientBake')
    control_material = make_gradient_material('MB_R6GradientControl', emission=True)
    gradient_plane.data.materials.append(bake_material)
    select_only(gradient_plane)
    gradient_settings = make_settings('TILING')
    gradient_plan = plan.build_plan(bpy.context, gradient_settings)
    gradient_task = gradient_plan.tasks[0]
    gradient_out = tempfile.mkdtemp(prefix='mb_r6_gradient_')
    gradient_tx = transaction.SceneTransaction(bpy.context)
    gradient_cycles = backend.CyclesBackend(
        gradient_tx, margin=0, samples=1, device='CPU',
        save_directory=gradient_out, save_files=True)
    gradient_job = job.BakeJob(bpy.context, gradient_plan, gradient_settings,
                               backend=gradient_cycles, transaction=gradient_tx)
    gradient_job.run_to_completion()
    print('gradient bake: state={} status={} size={}'.format(
        gradient_job.state, gradient_task.status,
        gradient_task.image.size[:] if gradient_task.image else None), flush=True)
    check('gradient bake succeeded', gradient_job.state == 'done'
          and gradient_task.status == 'done', gradient_job.state)

    gradient_plane.data.materials[0] = control_material
    control_pixels = render_top(gradient_plane, size=RENDER_SIZE,
                                center=(0.5, 0.5), span=1.0)

    delivered, _delivered_report = deliver_materials.build_final_material(
        MaterialStore(), 'MB_R6GradientDelivered',
        {'shader.base_color': gradient_task.image},
        tiling=tiling_payload(gradient_settings.tile_size,
                              gradient_settings.scene_scale_length))
    emissionize(delivered)
    gradient_by_group = {gradient_plan.groups[0].name: (delivered, None)}
    per_material.apply_materials(gradient_plan, gradient_by_group,
                                 tiling=tiling_payload(gradient_settings.tile_size,
                                                       gradient_settings.scene_scale_length))
    tiled_name = gradient_plane.get(per_material.TILING_UV_MARKER)
    uv_now = loop_uv(gradient_plane, tiled_name)
    print('gradient delivery: tiled={} uv=[{:.6f},{:.6f}]x[{:.6f},{:.6f}]'.format(
        tiled_name, uv_now[:, 0].min(), uv_now[:, 0].max(),
        uv_now[:, 1].min(), uv_now[:, 1].max()), flush=True)
    check('gradient plane got the Tiled layer',
          tiled_name == 'Tiled' and gradient_plane.data.uv_layers[0].name == 'Tiled',
          repr(tiled_name))
    delivered_pixels = render_top(gradient_plane, size=RENDER_SIZE,
                                  center=(0.5, 0.5), span=1.0)
    mae = float(np.mean(np.abs(delivered_pixels[:, :, :3]
                               - control_pixels[:, :, :3])))
    maximum = float(np.max(np.abs(delivered_pixels[:, :, :3]
                                 - control_pixels[:, :, :3])))
    print('baked(sample plane)->Tiled MAE={:.6f} max={:.6f} '
          'mean_baked={:.6f} mean_control={:.6f}'.format(
              mae, maximum, float(np.mean(delivered_pixels[:, :, 0])),
              float(np.mean(control_pixels[:, :, 0]))), flush=True)
    check('baked Tiled render matches the procedural render (MAE <= 0.01)',
          mae <= 0.01, '{:.9f}'.format(mae))
    check('comparison is not a blank image',
          float(np.mean(delivered_pixels[:, :, 0])) > 0.01
          and float(np.mean(control_pixels[:, :, 0])) > 0.01,
          str((float(np.mean(delivered_pixels[:, :, 0])),
               float(np.mean(control_pixels[:, :, 0])))))
    shutil.rmtree(gradient_out, ignore_errors=True)

    # ================================================================ 16. finalize / copies
    print('--- 16. finalize keeps the layers; copies keep Tiled as UVMap ---', flush=True)
    old_uvs = uv_snapshot(gradient_plane)
    finalized = per_material.finalize_materials(gradient_plan, gradient_settings)
    layers_after_finalize = sorted(layer.name for layer in gradient_plane.data.uv_layers)
    print('finalize: count={} layers={} render={}'.format(
        finalized, layers_after_finalize,
        [l.name for l in gradient_plane.data.uv_layers if l.active_render]), flush=True)
    check('Tiling finalize keeps every UV layer',
          finalized > 0 and 'Tiled' in layers_after_finalize,
          str(layers_after_finalize))
    check('Tiling finalize re-arms Tiled as render layer',
          bool(gradient_plane.data.uv_layers['Tiled'].active_render), 'active_render')
    drift, missing, extra = uv_drift(old_uvs, gradient_plane)
    check('finalize does not touch UV values',
          drift == 0.0 and not missing, '{:.9f} {}'.format(drift, missing))

    from material_bakery.deliver import objects as objects_mod
    tiled_snapshot = layer_data(gradient_plane, 'Tiled')
    collection = objects_mod.default_collection(bpy.context.scene)
    operator_session = session.Session.get()
    operator_session.job = DummyJob(gradient_settings, gradient_plan)
    operator_session.report = object()
    before_objects = set(obj.name for obj in bpy.data.objects)
    result = bpy.ops.mbakery.create_final_objects()
    created = [obj for obj in bpy.data.objects if obj.name not in before_objects]
    copy = created[0] if created else None
    copy_layers = sorted(layer.name for layer in copy.data.uv_layers) if copy else []
    copy_first = copy.data.uv_layers[0].name if copy else ''
    copy_uv = layer_data(copy, 'UVMap') if copy else None
    copy_diff = (float(np.max(np.abs(copy_uv - tiled_snapshot)))
                 if copy_uv is not None else float('inf'))
    original_layers = sorted(layer.name for layer in gradient_plane.data.uv_layers)
    print('copies: result={} created={} layers={} first={} uv_vs_tiled={:.6f} '
          'copy_tiling_marker={} original_layers={}'.format(
              result, [obj.name for obj in created], copy_layers, copy_first,
              copy_diff,
              copy.get(per_material.TILING_UV_MARKER) if copy else None,
              original_layers), flush=True)
    check('create final objects made a copy', copy is not None and result == {'FINISHED'},
          str(result))
    check('the copy keeps Tiled as its first layer, renamed UVMap',
          copy_first == 'UVMap' and 'Tiled' not in copy_layers, str(copy_layers))
    check('the copy UV data equals the Tiled layer',
          copy_diff < 1e-9, '{:.9f}'.format(copy_diff))
    check('the finalized copy carries no stale tiling marker',
          copy is not None and copy.get(per_material.TILING_UV_MARKER) in (None, ''),
          repr(copy.get(per_material.TILING_UV_MARKER) if copy else None))
    check('the original object is untouched by the copy step',
          'Tiled' in original_layers, str(original_layers))
    if copy is not None:
        bpy.data.objects.remove(copy, do_unlink=True)
    session.Session.forget()

    # ================================================================ 17. exit paths
    print('--- 17. four exit paths leave no residue ---', flush=True)
    e3_out = tempfile.mkdtemp(prefix='mb_r6_paths_')

    def residue_counts(label):
        surfaces = len(tagged_surfaces())
        scratch = sum(1 for item in bpy.data.images if item.get(per_material.SCRATCH_TAG))
        tiled_objects = [item.name for item in bpy.data.objects
                         if item.get(per_material.TILING_UV_MARKER)]
        counts = {"surfaces": surfaces, "scratch": scratch,
                  "tiled_objects": len(tiled_objects)}
        print('RESIDUE {}: SAMPLE_TAG={} scratch_images={} tiled_objects={} ({})'.format(
            label, surfaces, scratch, len(tiled_objects), tiled_objects), flush=True)
        return counts

    class PendingNullBackend(backend.NullBackend):
        synchronous = False

        def dispatch(self, task):
            self.calls.append(('dispatch', task.key))
            return False

        def poll(self):
            return True

    # 1/4 success ---------------------------------------------------------------
    success_obj = make_scene()
    success_uvs = uv_snapshot(success_obj)
    success_settings = make_settings('TILING')
    success_plan = plan.build_plan(bpy.context, success_settings)
    success_tx = transaction.SceneTransaction(bpy.context)
    success_cycles = backend.CyclesBackend(
        success_tx, margin=0, samples=1, device='CPU',
        save_directory=e3_out, save_files=True)
    success_job = job.BakeJob(bpy.context, success_plan, success_settings,
                              backend=success_cycles, transaction=success_tx)
    success_job.run_to_completion()
    after_bake = residue_counts('success/after-bake')
    success_session = session.Session()
    success_session.job = success_job
    built, added = success_session.deliver(ui_settings)
    after_delivery = residue_counts('success/after-delivery')
    success_tiled = success_obj.get(per_material.TILING_UV_MARKER)
    first_restore = slots_mod.restore_objects([success_obj])
    after_restore = residue_counts('success/after-restore')
    success_layers = sorted(layer.name for layer in success_obj.data.uv_layers)
    drift, missing, extra = uv_drift(success_uvs, success_obj)
    print('success path: build={} added={} tiled={} restore={} layers={} '
          'old_diff={:.6f}'.format(
              built, added, success_tiled, [str(item) for item in first_restore],
              success_layers, drift), flush=True)
    check('success: bake leaves no surface / scratch / Tiled',
          after_bake == {"surfaces": 0, "scratch": 0, "tiled_objects": 0},
          str(after_bake))
    check('success: delivery writes the Tiled layer',
          after_delivery['tiled_objects'] == 1 and success_tiled == 'Tiled',
          str(after_delivery))
    check('success: restore removes the Tiled layer',
          after_restore['tiled_objects'] == 0 and 'Tiled' not in success_layers,
          str(after_restore))
    check('success: restore brings back the original UV data',
          drift == 0.0 and not missing, '{:.9f} {}'.format(drift, missing))
    check('success: original render layer restored',
          bool(success_obj.data.uv_layers['OriginalUV'].active_render),
          'active_render')
    success_session.close_log_file()

    # 2/4 cancel ---------------------------------------------------------------
    clear_scene()
    cancel_obj = make_scene()
    cancel_uvs = uv_snapshot(cancel_obj)
    cancel_settings = make_settings('TILING')
    cancel_plan = plan.build_plan(bpy.context, cancel_settings)
    cancel_job = job.BakeJob(bpy.context, cancel_plan, cancel_settings,
                             backend=PendingNullBackend())
    cancel_job.start()
    cancel_job.step()
    cancel_during = residue_counts('cancel/during')
    cancel_job.cancel()
    cancel_job.step()
    cancel_after = residue_counts('cancel/after')
    cancel_drift, cancel_missing, _cancel_extra = uv_drift(cancel_uvs, cancel_obj)
    print('cancel: state={} statuses={} tiled={} old_diff={:.6f}'.format(
        cancel_job.state, [item.status for item in cancel_plan.tasks],
        cancel_obj.get(per_material.TILING_UV_MARKER), cancel_drift), flush=True)
    check('cancel: surface exists during the wait and is gone after',
          cancel_during['surfaces'] == 1 and cancel_after['surfaces'] == 0,
          '{} -> {}'.format(cancel_during, cancel_after))
    check('cancel: state is cancelled', cancel_job.state == 'cancelled',
          cancel_job.state)
    check('cancel: no Tiled layer and no UV drift',
          cancel_obj.get(per_material.TILING_UV_MARKER) in (None, '')
          and cancel_drift == 0.0, '{:.9f}'.format(cancel_drift))

    # 3/4 failure --------------------------------------------------------------
    clear_scene()
    fail_obj = make_scene()
    fail_uvs = uv_snapshot(fail_obj)
    fail_settings = make_settings('TILING')
    fail_plan = plan.build_plan(bpy.context, fail_settings)
    fail_job = job.BakeJob(
        bpy.context, fail_plan, fail_settings,
        backend=backend.NullBackend(
            fail_on=tuple(item.group_name for item in fail_plan.tasks)))
    fail_job.run_to_completion()
    fail_after = residue_counts('failure/after')
    fail_drift, _fail_missing, _fail_extra = uv_drift(fail_uvs, fail_obj)
    print('failure: state={} statuses={} errors={} tiled={} old_diff={:.6f}'.format(
        fail_job.state, [item.status for item in fail_plan.tasks],
        [item.error for item in fail_plan.tasks],
        fail_obj.get(per_material.TILING_UV_MARKER), fail_drift), flush=True)
    check('failure: every task failed and the surface is gone',
          all(item.status == 'failed' for item in fail_plan.tasks)
          and fail_after['surfaces'] == 0,
          str([item.status for item in fail_plan.tasks]))
    check('failure: no Tiled layer and no UV drift',
          fail_obj.get(per_material.TILING_UV_MARKER) in (None, '')
          and fail_drift == 0.0, '{:.9f}'.format(fail_drift))

    # 4/4 wait timeout ---------------------------------------------------------
    clear_scene()
    timeout_obj = make_scene()
    timeout_uvs = uv_snapshot(timeout_obj)
    timeout_settings = make_settings('TILING')
    timeout_plan = plan.build_plan(bpy.context, timeout_settings)
    timeout_job = job.BakeJob(bpy.context, timeout_plan, timeout_settings,
                              backend=PendingNullBackend())
    timeout_job.start()
    timeout_job.step()
    timeout_during = residue_counts('timeout/during')
    timeout_job.wait_timeout_seconds = 0.0
    timeout_job.step()
    timeout_after = residue_counts('timeout/after')
    timeout_drift, _timeout_missing, _timeout_extra = uv_drift(timeout_uvs,
                                                               timeout_obj)
    print('timeout: state={} statuses={} surfaces={} tiled={} old_diff={:.6f}'.format(
        timeout_job.state, [item.status for item in timeout_plan.tasks],
        timeout_after['surfaces'], timeout_obj.get(per_material.TILING_UV_MARKER),
        timeout_drift), flush=True)
    check('timeout: surface exists during the wait and is gone after',
          timeout_during['surfaces'] == 1 and timeout_after['surfaces'] == 0,
          '{} -> {}'.format(timeout_during, timeout_after))
    check('timeout: first task failed and the rest were skipped',
          timeout_plan.tasks[0].status == 'failed'
          and all(item.status in ('failed', 'skipped')
                  for item in timeout_plan.tasks[1:]),
          str([item.status for item in timeout_plan.tasks]))
    check('timeout: no Tiled layer and no UV drift',
          timeout_obj.get(per_material.TILING_UV_MARKER) in (None, '')
          and timeout_drift == 0.0, '{:.9f}'.format(timeout_drift))

    # 5/5 checklist rows -------------------------------------------------------
    print('--- 18. Tiling checklist rows after the normal-WARN removal ---', flush=True)
    ui_settings = bpy.context.scene.mbakery
    saved_maps = [(item.type_key, item.size_x, item.size_y, item.enabled)
                  for item in ui_settings.maps]
    try:
        ui_settings.maps.clear()
        for key in ('shader.base_color', 'shader.normal', 'standard.position',
                    'standard.uv', 'misc.pointiness', 'misc.ao', 'standard.combined'):
            item = ui_settings.maps.add()
            item.type_key = key
            item.size_x = 512
            item.size_y = 512
            item.enabled = True
        ui_settings.workflow = properties.WORKFLOW_MATERIAL
        ui_settings.per_material_layout = 'TILING'
        ui_settings.tile_size = 1.0
        bpy.context.scene.unit_settings.scale_length = 0.01
        rows = uops.checklist(bpy.context, ui_settings)
        errors = [message for level, message in rows if level == 'ERROR']
        geometry_rows = [row for row in rows
                         if row[1].startswith('These maps are baked from the 1 m')]
        normal_rows = [row for row in rows if 'Normal is still sampled' in row[1]]
        scale_rows = [row for row in rows if row[1].startswith('Scene scale length is')]
        print('Tiling checklist rows={} errors={} geometry={} normal={} scale={}'.format(
            len(rows), len(errors), len(geometry_rows), len(normal_rows),
            len(scale_rows)), flush=True)
        for row in geometry_rows + scale_rows:
            print('  {} {}'.format(row[0], row[1]), flush=True)
        check('Tiling checklist keeps the geometry WARN',
              len(geometry_rows) == 1 and geometry_rows[0][0] == 'WARN',
              str(geometry_rows[:1]))
        check('geometry WARN lists every enabled geometry map',
              geometry_rows and all(label in geometry_rows[0][1]
                                    for label in ('Position', 'UV', 'Pointiness',
                                                  'Ambient Occlusion', 'Combined')),
              str(geometry_rows[:1]))
        check('the old "normal not Boxed" WARN is gone', not normal_rows,
              str(normal_rows))
        check('scale_length 0.01 gives an OK row with BU size and px/m',
              len(scale_rows) == 1 and scale_rows[0][0] == 'OK'
              and '100 BU' in scale_rows[0][1] and '512 px/m' in scale_rows[0][1],
              str(scale_rows[:1]))
        check('Tiling checklist adds no ERROR rows', not errors, str(errors))

        ui_settings.per_material_layout = 'PACKED'
        packed_rows = uops.checklist(bpy.context, ui_settings)
        forbidden = [message for level, message in packed_rows
                     if message.startswith('These maps are baked from the 1 m')
                     or 'Normal is still sampled' in message
                     or message.startswith('Scene scale length is')]
        print('Packed checklist rows={} forbidden={}'.format(
            len(packed_rows), len(forbidden)), flush=True)
        check('Packed never shows the Tiling-only rows', not forbidden, str(forbidden))
    finally:
        bpy.context.scene.unit_settings.scale_length = 1.0
        ui_settings.per_material_layout = 'PACKED'
        ui_settings.workflow = properties.WORKFLOW_MATERIAL
        ui_settings.maps.clear()
        for key, size_x, size_y, enabled in saved_maps:
            item = ui_settings.maps.add()
            item.type_key = key
            item.size_x = size_x
            item.size_y = size_y
            item.enabled = enabled

    shutil.rmtree(out_dir, ignore_errors=True)
    shutil.rmtree(e3_out, ignore_errors=True)
finally:
    try:
        per_material.remove_tiling_uvs()
    except Exception as exc:
        print('cleanup tiling UV error: {}'.format(exc), flush=True)
    try:
        per_material.purge_scratch_surfaces(bpy.context.scene)
    except Exception as exc:
        print('cleanup error: {}'.format(exc), flush=True)
    material_bakery.unregister()

print('TOTAL FAILURES={}'.format(len(failed)), flush=True)
if failed:
    print('FAILED {} checks: {}'.format(len(failed), failed), flush=True)
    raise SystemExit(1)
print('ALL CHECKS PASSED', flush=True)

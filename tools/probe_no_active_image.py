"""实验：Blender 到底在什么情况下报 "No active image found"。

用户报某一次集合烘焙失败，错误是 RuntimeError: No active image found。
这个脚本把可能的触发条件一个个试一遍，把**每种情况下的原文**打出来 ——
不然就只能靠猜是哪一类材质出了问题。

只做实验，不碰插件代码。
用法: blender --background --factory-startup --python tools/probe_no_active_image.py
"""
import bpy

SIZE = 32


def quad(name, material):
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
    mesh.update()
    mesh.uv_layers.new(name="UVMap")
    if material is not None:
        mesh.materials.append(material)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def new_material(name, with_image_node=True, image_active=True, with_shader=True):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    tree = material.node_tree
    if with_shader:
        shader = tree.nodes.new('ShaderNodeBsdfPrincipled')
        output = tree.nodes.get("Material Output")
        tree.links.new(shader.outputs["BSDF"], output.inputs["Surface"])
    if with_image_node:
        node = tree.nodes.new('ShaderNodeTexImage')
        node.image = bpy.data.images.new(name + "Img", SIZE, SIZE)
        node.select = True
        if image_active:
            tree.nodes.active = node
    return material


def bake(objects):
    view_layer = bpy.context.view_layer
    for obj in view_layer.objects:
        obj.select_set(False)
    for obj in objects:
        obj.select_set(True)
    view_layer.objects.active = objects[0]
    try:
        result = bpy.ops.object.bake(type='EMIT', margin=2, use_clear=True,
                                     target='IMAGE_TEXTURES', save_mode='INTERNAL')
    except Exception as exc:
        return "{}: {}".format(type(exc).__name__, exc)
    return "ok {}".format(result)


bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.samples = 1
scene.render.bake.margin = 2
scene.render.bake.use_selected_to_active = False

print("=" * 74)
print("No active image found —— 触发条件实验    Blender {}".format(bpy.app.version_string))
print("=" * 74)

image_active_mat = new_material("M_ImageActive")
no_image_mat = new_material("M_NoImageNode")
inactive_image_mat = new_material("M_ImageInactive", image_active=False)
no_shader_mat = new_material("M_NoShader", with_shader=False)
empty_mat = bpy.data.materials.new("M_NoNodes")
empty_mat.use_nodes = False

obj_a = quad("A_Good", image_active_mat)
obj_b = quad("B_NoImageNode", no_image_mat)
obj_c = quad("C_ImageInactive", inactive_image_mat)
obj_d = quad("D_NoShader", no_shader_mat)
obj_e = quad("E_NoNodes", empty_mat)
obj_f = quad("F_NoMaterial", None)

print("\n1) 只有 A（正常）")
print("   ->", bake([obj_a]))

print("\n2) A + B（B 的材质里**没有**图像纹理节点）")
print("   ->", bake([obj_a, obj_b]))

print("\n3) A + C（C 有图像节点，但 active 不是它）")
print("   ->", bake([obj_a, obj_c]))

print("\n4) A + D（D 的材质没有着色器，只有输出节点）")
print("   ->", bake([obj_a, obj_d]))

print("\n5) A + E（E 的材质 use_nodes = False）")
print("   ->", bake([obj_a, obj_e]))

print("\n6) A + F（F 完全没有材质槽）")
print("   ->", bake([obj_a, obj_f]))

print("\n7) 只有 B（单选一个没有图像节点的）")
print("   ->", bake([obj_b]))

print("\n8) 只有 F（没有材质槽）")
print("   ->", bake([obj_f]))

print("\n9) A 两个槽：槽0 有图，槽1 没有图像节点")
mesh = obj_a.data
slot_mat = empty_mat.copy()
slot_mat.name = "M_SecondSlotNoImage"
slot_mat.use_nodes = True
mesh.materials.append(slot_mat)
print("   ->", bake([obj_a]))

print("\n" + "=" * 74)
print("结论要点：错误原文里通常带材质名与槽位下标 —— 报错时一定要保留原文。")
print("=" * 74)

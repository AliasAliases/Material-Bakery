"""Material Bakery — 阶段 9：逐像素对照

要证明的事情只有一句：**我们烘出来的图，和"照着同样设置手工烘一遍"的结果一模一样。**

关键在于对照组的实现必须是**独立的** —— 这个脚本里手工搭节点、手工调
bpy.ops.object.bake，完全不复用 material_bakery 的 surgery/plan/backend。
否则就是拿自己验自己，什么也证明不了。

对照项：
    shader.base_color      EMIT + 节点手术
    shader.roughness       EMIT + 节点手术
    standard.combined      Blender 自带 pass
    standard.normal        Blender 自带 pass
"""
import bpy, sys, os, shutil, glob

# 工作区 = 本文件所在目录的上一级。
# ⚠ 这里原来写死的是开发机上的绝对路径，clone 到别处每个套件都跑不起来，
#   而且公开版/私有版两份检出会互相 import 错代码。
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WS)

import material_bakery
from material_bakery import compat
from material_bakery.core import compare as cmp_mod
from material_bakery.core import plan as pl
from material_bakery.core import transaction as txmod
from material_bakery.engine import backend as bk
from material_bakery.engine import job as jb

compat.set_silent(True)

FAIL = []
def check(label, ok, extra=""):
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", label, ("  " + str(extra)) if extra else ""))
    if not ok:
        FAIL.append(label)

material_bakery.register()

ROOT = os.path.join(WS, "_probe", "parity")
NEW = os.path.join(ROOT, "new")
REF = os.path.join(ROOT, "reference")
if os.path.isdir(ROOT):
    shutil.rmtree(ROOT)
for path in (NEW, REF):
    os.makedirs(path)

SIZE = 96
MARGIN = 4
SAMPLES = 1
MAPS = ("shader.base_color", "shader.roughness", "standard.combined", "standard.normal")


# ------------------------------------------------------------------------------------
#   场景：一个四边形 + 一个球，材质里有常量也有贴图，尽量贴近真实用法
# ------------------------------------------------------------------------------------

def build_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.engine = 'CYCLES'
    scene.cycles.samples = SAMPLES
    if hasattr(scene.cycles, "use_denoising"):
        scene.cycles.use_denoising = False
    scene.render.bake.margin = MARGIN
    scene.render.bake.use_selected_to_active = False
    scene.render.bake.target = 'IMAGE_TEXTURES'

    # 一张程序化贴图接进 Base Color，避免"全是常量"让对照过于简单
    quad_mesh = bpy.data.meshes.new("QuadMesh")
    quad_mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
    quad_mesh.update()
    layer = quad_mesh.uv_layers.new(name="UVMap")
    for i, coord in enumerate(((0.02, 0.02), (0.98, 0.02), (0.98, 0.98), (0.02, 0.98))):
        layer.data[i].uv = coord

    material = bpy.data.materials.new("Tester")
    material.use_nodes = True
    tree = material.node_tree
    shader = next(n for n in tree.nodes if n.type == 'BSDF_PRINCIPLED')
    shader.inputs["Base Color"].default_value = (0.8, 0.2, 0.1, 1.0)
    shader.inputs["Roughness"].default_value = 0.37
    shader.inputs["Metallic"].default_value = 0.6
    if "IOR" in shader.inputs:
        shader.inputs["IOR"].default_value = 1.45

    # UV 上叠一层噪波，让 Base Color 不是纯常量
    tex = tree.nodes.new('ShaderNodeTexNoise')
    tex.location = (-500, 200)
    tex.inputs["Scale"].default_value = 4.0
    if "Detail" in tex.inputs:
        tex.inputs["Detail"].default_value = 2.0
    tree.links.new(tex.outputs["Fac"], shader.inputs["Base Color"])

    quad_mesh.materials.append(material)

    quad = bpy.data.objects.new("Quad", quad_mesh)
    collection = bpy.data.collections.new("Body")
    scene.collection.children.link(collection)
    collection.objects.link(quad)

    bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, radius=0.5,
                                         location=(2.0, 0.0, 0.0))
    sphere = bpy.context.active_object
    sphere.name = "Ball"
    sphere.data.uv_layers.new(name="UVMap")
    sphere.data.materials.append(material)
    scene.collection.objects.unlink(sphere)
    collection.objects.link(sphere)

    return quad, sphere, material, shader


# ------------------------------------------------------------------------------------
#   独立对照组：自己搭节点、自己烘焙
# ------------------------------------------------------------------------------------

MAP_SOCKETS = {"shader.base_color": "Base Color", "shader.roughness": "Roughness"}


def reference_bake(material, shader, objects, type_key, image):
    """手工烘一张图。完全不用 material_bakery 的代码。"""
    tree = material.node_tree
    output = next(n for n in tree.nodes if n.type == 'OUTPUT_MATERIAL')
    surface = output.inputs["Surface"]
    original_link = surface.links[0].from_socket if surface.links else None

    scratch = []
    try:
        if type_key in MAP_SOCKETS:
            # 手工版的"节点手术"：把目标插口接到 Emission 再接到输出
            emission = tree.nodes.new('ShaderNodeEmission')
            scratch.append(emission)
            source = shader.inputs[MAP_SOCKETS[type_key]]
            if source.links:
                tree.links.new(source.links[0].from_socket, emission.inputs["Color"])
            else:
                value = source.default_value
                if isinstance(value, float):
                    value = (value, value, value, 1.0)
                emission.inputs["Color"].default_value = value
            for link in list(surface.links):
                tree.links.remove(link)
            tree.links.new(emission.outputs["Emission"], surface)
            bake_pass = 'EMIT'
        elif type_key == "standard.combined":
            bake_pass = 'COMBINED'
        elif type_key == "standard.normal":
            bake_pass = 'NORMAL'
        else:
            raise ValueError("no reference recipe for {}".format(type_key))

        node = tree.nodes.new('ShaderNodeTexImage')
        scratch.append(node)
        node.image = image
        tree.nodes.active = node

        for obj in bpy.context.view_layer.objects:
            obj.select_set(False)
        for obj in objects:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = objects[0]

        kwargs = {"type": bake_pass, "margin": MARGIN, "use_clear": True,
                  "target": 'IMAGE_TEXTURES', "use_selected_to_active": False}
        if bake_pass == 'NORMAL':
            kwargs["normal_space"] = 'TANGENT'
        bpy.ops.object.bake(**kwargs)
    finally:
        for link in list(surface.links):
            tree.links.remove(link)
        if original_link is not None:
            tree.links.new(original_link, surface)
        for node in scratch:
            if tree.nodes.get(node.name) is not None:
                tree.nodes.remove(node)


def reference_image(name, is_data):
    image = bpy.data.images.new(name, width=SIZE, height=SIZE, alpha=False,
                                float_buffer=False)
    image.colorspace_settings.name = "Non-Color" if is_data else "sRGB"
    return image


def is_data_type(type_key):
    from material_bakery.core import bake_types
    return bake_types.require(type_key).channel.is_data


# ------------------------------------------------------------------------------------
#   跑

print("=== 阶段 9a：Material Bakery 烘一遍 ===")
quad, sphere, material, shader = build_scene()
objects = [quad, sphere]

settings = pl.BakeSettings(
    maps=pl.map_requests_from_pairs([(key, SIZE) for key in MAPS]),
    file_format='PNG',
    margin=MARGIN,
    pack_into_blend=False,
    fake_user=False,
    # ⚠ 对照必须关掉共享 UV 打包！
    #   打包会把一组物体的 UV 摆进互不重叠的格子里，而手工对照组是按原 UV 烘的 ——
    #   两边画的内容根本不在一个位置上，比出来的差异全是打包造成的，
    #   跟"引擎算得对不对"毫无关系。（第一版就是这么假失败的。）
    shared_textures=False,
)
plan = pl.build_plan(bpy.context, settings)
check("计划里有 4 个任务", len(plan.tasks) == 4, len(plan.tasks))

tx = txmod.SceneTransaction(bpy.context)
backend = bk.CyclesBackend(tx, margin=MARGIN, samples=SAMPLES, use_denoising=False,
                           save_directory=NEW, save_files=True)
job = jb.BakeJob(bpy.context, plan, settings, backend=backend, transaction=tx)
job.run_to_completion()
for line in job.report.details():
    print("  " + line)
check("新版 4 张全部成功", job.report.failed == 0 and job.report.done == 4,
      (job.report.done, job.report.failed,
       [r.error for r in job.report.failed_results()]))

print("\n=== 阶段 9b：手工照同样设置烘一遍 ===")
# ⚠ 先把"新版烘完之后材质是什么样"记下来。
#   下面要重建场景做对照，一 read_factory_settings 这些引用就全废了，
#   事后再去读就是 use-after-free（表现为 ReferenceError 或者直接崩）。
def material_state(material, shader_node=None):
    tree = material.node_tree
    output = next((n for n in tree.nodes if n.type == 'OUTPUT_MATERIAL'), None)
    surface = output.inputs["Surface"] if output is not None else None
    source_type = None
    if surface is not None and surface.links:
        source_type = surface.links[0].from_node.type
    return {
        "surface_source": source_type,
        "node_types": sorted(n.type for n in tree.nodes),
        "emissions": len([n for n in tree.nodes if n.type == 'EMISSION']),
        "tex_images": len([n for n in tree.nodes if n.type == 'TEX_IMAGE']),
        "base_color": tuple(round(v, 4) for v in
                            shader_node.inputs["Base Color"].default_value)
        if shader_node is not None else None,
    }

new_state = material_state(material, shader)
print("  新版烘完后:", new_state)

# 场景重建到完全相同的状态，避免上一轮留下的任何影响
quad2, sphere2, material2, shader2 = build_scene()
objects2 = [quad2, sphere2]
material2.name = "Tester"

for type_key in MAPS:
    image = reference_image("REF " + type_key, is_data_type(type_key))
    try:
        reference_bake(material2, shader2, objects2, type_key, image)
        path = os.path.join(REF, "{} - {}px.png".format(
            type_key.replace(".", "_").replace("shader_", "").replace("standard_", ""), SIZE))
        image.filepath_raw = path
        image.file_format = 'PNG'
        image.save()
        print("  手工烘好:", os.path.basename(path))
    except Exception as exc:
        check("手工烘焙 {}".format(type_key), False, str(exc).strip())

check("手工烘出了 4 张",
      len(glob.glob(os.path.join(REF, "**", "*.png"), recursive=True)) == 4,
      sorted(os.path.basename(f) for f in glob.glob(os.path.join(REF, "**", "*.png"), recursive=True)))

print("\n=== 阶段 9c：逐像素对照 ===")
# 两边的文件按"类型"配对，文件名规则不同不影响比较
def normalize(text):
    return text.lower().replace(" ", "").replace("_", "").replace("-", "")

def index_by_type(folder):
    found = {}
    for path in sorted(glob.glob(os.path.join(folder, "**", "*.png"), recursive=True)):
        stem = normalize(os.path.splitext(os.path.basename(path))[0])
        for type_key in MAPS:
            probe = normalize(type_key.split(".")[-1])
            if probe in stem:
                found.setdefault(type_key, path)
    return found

new_files = index_by_type(NEW)
ref_files = index_by_type(REF)
print("  新版:", {k: os.path.basename(v) for k, v in new_files.items()})
print("  对照:", {k: os.path.basename(v) for k, v in ref_files.items()})
check("两边四类图都齐了",
      len(new_files) == 4 and len(ref_files) == 4,
      (sorted(new_files), sorted(ref_files)))

for type_key in MAPS:
    if type_key not in new_files or type_key not in ref_files:
        check("{} 有对照文件".format(type_key), False)
        continue
    diff = cmp_mod.compare_image_files(type_key, ref_files[type_key], new_files[type_key])
    # 差异大时把两边的代表性像素打出来，免得只看一个 max 值瞎猜
    if not diff.matches(tolerance=0.0, max_diff_ratio=0.0):
        for label, path in (("对照", ref_files[type_key]), ("新版", new_files[type_key])):
            image = bpy.data.images.load(path)
            width, height = image.size
            spot = list(image.pixels[((height // 2) * width + width // 2) * 4:][:4])
            print("    {} {} 中心={} 空间={} float={} size={}".format(
                label, os.path.basename(path), [round(v, 4) for v in spot],
                image.colorspace_settings.name, image.is_float, tuple(image.size)))
            bpy.data.images.remove(image)
    print("  {}".format(diff.summary()))
    # 手工版与新版走的是同一套 Cycles 设置，结果应当**逐像素完全一致**
    check("{} 与手工烘焙逐像素一致".format(type_key),
          diff.matches(tolerance=0.0, max_diff_ratio=0.0),
          diff.summary())

print("\n=== 阶段 9d：报告与对照的接缝 ===")
from material_bakery.engine.report import STATUS_DONE, TaskResult

report_path = os.path.join(NEW, "material_bakery_report.json")
job.report.save_to(report_path)
loaded = type(job.report).load_from(report_path)
check("报告能落盘并读回", loaded.total == 4, loaded.total)
check("报告里每张图都有路径", all(r.filepath for r in loaded.results),
      [r.filepath for r in loaded.results])
check("报告里记了节点手术做了什么",
      all(r.surgery for r in loaded.results if r.type_key.startswith("shader.")),
      [(r.type_key, r.surgery) for r in loaded.results])

# 用**报告里的路径**做对照（不扫目录）——
# 新版报告的 image_name 换成型别 key，好跟对照组配对
ref_report = type(job.report)("reference")
for type_key, path in ref_files.items():
    ref_report.add(TaskResult(group="Body", type_key=type_key, type_label=type_key,
                              size=SIZE, image_name=type_key, filepath=path,
                              status=STATUS_DONE))
by_type = {r.type_key: r for r in job.report.results}
new_report = type(job.report)("new")
for type_key, result in by_type.items():
    new_report.add(TaskResult(group="Body", type_key=type_key, type_label=type_key,
                              size=SIZE, image_name=type_key,
                              filepath=new_files.get(type_key, result.filepath),
                              status=STATUS_DONE))
compared = cmp_mod.report_to_folders(ref_report, new_report)
print("  " + compared.summary())
for line in compared.details()[:6]:
    print("   " + line)
check("按报告路径对照也全部一致", compared.ok, compared.details()[:4])
check("对照了 4 张", compared.compared == 4, compared.compared)

print("\n=== 阶段 9e：材质没被两边改坏 ===")
check("新版烘完后材质回到原样（输出接 Principled）",
      new_state["surface_source"] == 'BSDF_PRINCIPLED', new_state["surface_source"])
check("新版烘完后没有残留 Emission", new_state["emissions"] == 0, new_state)
check("新版烘完后没有残留烘焙图像节点", new_state["tex_images"] == 0, new_state)
check("新版没改材质里的 Base Color 值",
      new_state["base_color"] == (0.8, 0.2, 0.1, 1.0), new_state["base_color"])

ref_state = material_state(material2, shader2)
print("  手工版烘完后:", ref_state)
check("手工版烘完后材质也回到原样",
      ref_state["surface_source"] == 'BSDF_PRINCIPLED', ref_state["surface_source"])
check("手工版烘完后没有残留 Emission", ref_state["emissions"] == 0, ref_state)
check("手工版没有残留图像节点", ref_state["tex_images"] == 0, ref_state)
check("两边烘完后的材质状态完全一致",
      new_state == ref_state, (new_state, ref_state))

print("\n=== 结果 ===")
if FAIL:
    print("FAILED {} check(s):".format(len(FAIL)))
    for name in FAIL:
        print("  -", name)
else:
    print("ALL CHECKS PASSED")

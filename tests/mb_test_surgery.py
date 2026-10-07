"""Material Bakery — 阶段 6 测试：节点手术 + 真实烘焙的像素证据

这一套回答一个最要紧的问题：**shader.* 类型到底烘出了什么？**

做法：搭一个材质，把 Base Color 明确设成纯红、Roughness 设成 0.25、Metallic 设成 1.0，
然后用 Cycles 真烘，再逐像素检查结果。如果节点手术没做对，EMIT 烘焙只会出一张全黑图。
"""
import bpy, sys, os, shutil, glob, ast

# 工作区 = 本文件所在目录的上一级。
# ⚠ 这里原来写死的是开发机上的绝对路径，clone 到别处每个套件都跑不起来，
#   而且公开版/私有版两份检出会互相 import 错代码。
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WS)

import material_bakery
from material_bakery import compat
from material_bakery.core import bake_types as bt
from material_bakery.core import plan as pl
from material_bakery.core import scene_scan as ss
from material_bakery.core import transaction as txmod
from material_bakery.engine import backend as bk
from material_bakery.engine import job as jb
from material_bakery.engine import surgery as sg

compat.set_silent(True)

FAIL = []
def check(label, ok, extra=""):
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", label, ("  " + str(extra)) if extra else ""))
    if not ok:
        FAIL.append(label)

material_bakery.register()

OUT = os.path.join(WS, "_probe", "surgery")
if os.path.isdir(OUT):
    shutil.rmtree(OUT)
os.makedirs(OUT)


# ------------------------------------------------------------------ 场景
def build_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    mesh = bpy.data.meshes.new("QuadMesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
    mesh.update()
    uv = mesh.uv_layers.new(name="UVMap")
    for i, coord in enumerate(((0, 0), (1, 0), (1, 1), (0, 1))):
        uv.data[i].uv = coord

    material = bpy.data.materials.new("Tester")
    material.use_nodes = True
    tree = material.node_tree
    shader = None
    for node in tree.nodes:
        if node.type == 'BSDF_PRINCIPLED':
            shader = node
    shader.inputs["Base Color"].default_value = (1.0, 0.0, 0.0, 1.0)
    shader.inputs["Roughness"].default_value = 0.25
    shader.inputs["Metallic"].default_value = 1.0
    if "IOR" in shader.inputs:
        shader.inputs["IOR"].default_value = 1.7
    mesh.materials.append(material)

    obj = bpy.data.objects.new("Quad", mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj, material, shader


print("=== 阶段 6a：手术的施加与还原 ===")
obj, material, shader = build_scene()
tree = material.node_tree
original_types = sorted(n.type for n in tree.nodes)
original_links = len(tree.links)

surgery = sg.NodeSurgery()
record = surgery.apply(material, bt.require("shader.base_color"))
check("shader.base_color 能施加手术", record is not None, surgery.skipped)
check("记录里写明了做了什么", record is not None and "Base Color" in record.detail,
      record.detail if record else None)
check("手术期间出现了 Emission 节点",
      any(n.type == 'EMISSION' for n in tree.nodes), [n.type for n in tree.nodes])
emission = [n for n in tree.nodes if n.type == 'EMISSION'][0]
check("连着 Principled 的 Base Color 值（纯红）",
      tuple(round(v, 3) for v in emission.inputs["Color"].default_value) == (1.0, 0.0, 0.0, 1.0),
      tuple(emission.inputs["Color"].default_value))
output = [n for n in tree.nodes if n.type == 'OUTPUT_MATERIAL'][0]
check("Material Output 现在接的是 Emission",
      output.inputs["Surface"].links[0].from_node.type == 'EMISSION')

surgery.revert_all()
check("还原后节点集合与原来完全一致",
      sorted(n.type for n in tree.nodes) == original_types,
      (original_types, sorted(n.type for n in tree.nodes)))
check("还原后连线数一致", len(tree.links) == original_links,
      (original_links, len(tree.links)))
# ⚠ 删节点会作废之前拿到的节点引用，必须重新取一遍再断言
output = [n for n in tree.nodes if n.type == 'OUTPUT_MATERIAL'][0]
check("还原后 Material Output 接回了 Principled",
      output.inputs["Surface"].links[0].from_node.type == 'BSDF_PRINCIPLED',
      output.inputs["Surface"].links[0].from_node.type)

# float 插口 -> RGBA 插口：原版在这里抛 TypeError
surgery2 = sg.NodeSurgery()
record2 = surgery2.apply(material, bt.require("shader.roughness"))
check("float 类型（Roughness）也能施加手术", record2 is not None, surgery2.skipped)
emission2 = [n for n in tree.nodes if n.type == 'EMISSION'][0]
check("float 值被正确展开成 RGBA（不抛 TypeError）",
      tuple(round(v, 3) for v in emission2.inputs["Color"].default_value) == (0.25, 0.25, 0.25, 1.0),
      tuple(emission2.inputs["Color"].default_value))
surgery2.revert_all()

# 认不出的插口要老实放弃，而不是瞎接
surgery3 = sg.NodeSurgery()
material_no_shader = bpy.data.materials.new("Empty")
material_no_shader.use_nodes = True
material_no_shader.node_tree.nodes.clear()
record3 = surgery3.apply(material_no_shader, bt.require("shader.base_color"))
check("没有 Material Output 时放弃手术", record3 is None)
check("放弃时记下了原因", bool(surgery3.skipped), surgery3.skipped)
check("放弃后没在材质里留下垃圾",
      len(material_no_shader.node_tree.nodes) == 0,
      len(material_no_shader.node_tree.nodes))

print("\n=== 阶段 6b：各种 kind 都能建立脚手架 ===")
for key in ("shader.base_color", "shader.roughness", "shader.normal", "shader.coat_tint",
            "shader.emission_color", "misc.ao", "misc.pointiness", "standard.uv",
            "misc.color_attribute", "misc.channel_packing", "misc.displacement",
            "standard.combined"):
    bake_type = bt.get(key)
    if bake_type is None:
        check("类型 {} 存在".format(key), False)
        continue
    worker = sg.NodeSurgery()
    result = worker.apply(material, bake_type, {"uv_map": "UVMap",
                                               "attribute_name": "Col"})
    if sg.needs_surgery(bake_type):
        check("{} 能施加手术".format(key), result is not None,
              worker.skipped or "no detail")
    else:
        check("{} 走标准 pass，不需要手术".format(key), result is not None)
    worker.revert_all()

check("手术全部还原后节点数回到原样",
      sorted(n.type for n in tree.nodes) == original_types,
      sorted(n.type for n in tree.nodes))

print("\n=== 阶段 6c：通道打包（原版写死下标的地方）===")
worker = sg.NodeSurgery()
record = worker.apply(material, bt.require("misc.channel_packing"),
                      {"channel_r": "shader.roughness", "channel_g": "shader.metallic",
                       "channel_b": "shader.ior"})
check("通道打包能建立脚手架", record is not None, worker.skipped)
combine = [n for n in tree.nodes if n.type == 'COMBINE_COLOR']
check("建了 Combine Color 节点", len(combine) == 1, [n.type for n in tree.nodes])
check("三个通道都接到了默认值（常量走灰度换算）",
      round(combine[0].inputs[0].default_value, 3) == 0.25,
      combine[0].inputs[0].default_value)
check("G 通道是 Metallic 的 1.0",
      round(combine[0].inputs[1].default_value, 3) == 1.0,
      combine[0].inputs[1].default_value)
check("B 通道是 IOR 的 1.7（不是 0）",
      round(combine[0].inputs[2].default_value, 3) == 1.7,
      combine[0].inputs[2].default_value)
check("说明里列出了三个来源", record is not None and record.detail.count(",") == 2,
      record.detail if record else None)
worker.revert_all()
check("通道打包还原干净",
      sorted(n.type for n in tree.nodes) == original_types)

# 默认第三个通道是 AO —— 它不是 Principled 的插口，得自己挂 AO 节点
worker = sg.NodeSurgery()
record = worker.apply(material, bt.require("misc.channel_packing"))
check("用默认 R/G/B 也能打包（含 AO）", record is not None, worker.skipped)
check("默认组合给 AO 挂了 Ambient Occlusion 节点",
      len([n for n in tree.nodes if n.type == 'AMBIENT_OCCLUSION']) == 1,
      [n.type for n in tree.nodes])
check("默认组合的说明里没有 missing",
      record is not None and "missing" not in record.detail,
      record.detail if record else None)
worker.revert_all()

# 通道源是贴图时必须先 Separate Color 只取 R（否则三个通道互相串色）
worker = sg.NodeSurgery()
image = bpy.data.images.new("Src", 8, 8)
tex = tree.nodes.new('ShaderNodeTexImage')
tex.image = image
base_socket = shader.inputs["Base Color"]
for link in list(base_socket.links):
    tree.links.remove(link)
tree.links.new(tex.outputs["Color"], base_socket)
record = worker.apply(material, bt.require("misc.channel_packing"),
                      {"channel_r": "shader.base_color", "channel_g": "shader.base_color",
                       "channel_b": "shader.base_color"})
check("通道源是贴图时也能打包", record is not None, worker.skipped)
check("为每个贴图通道建了 Separate Color",
      len([n for n in tree.nodes if n.type == 'SEPARATE_COLOR']) == 3,
      len([n for n in tree.nodes if n.type == 'SEPARATE_COLOR']))
worker.revert_all()
tree.nodes.remove(tree.nodes[tex.name])
check("还原后回到原样",
      sorted(n.type for n in tree.nodes) == original_types)

print("\n=== 阶段 6d：真机烘焙的像素证据 ===")
obj, material, shader = build_scene()
tree = material.node_tree          # ⚠ 重建场景后必须重新取，旧的已失效
settings = pl.BakeSettings(
    maps=pl.map_requests_from_pairs([
        ("shader.base_color", 64),
        ("shader.roughness", 64),
        ("shader.metallic", 64),
        ("shader.normal", 64),
        ("misc.channel_packing", 64),
    ]),
    file_format='PNG',
    pack_into_blend=False,
)
plan = pl.build_plan(bpy.context, settings)
print("  任务:", len(plan.tasks))
check("5 个任务", len(plan.tasks) == 5, len(plan.tasks))

node_types_before = sorted(n.type for n in tree.nodes)
links_before = len(tree.links)

tx = txmod.SceneTransaction(bpy.context)
backend = bk.CyclesBackend(tx, margin=2, samples=1, save_directory=OUT, save_files=True)
job = jb.BakeJob(bpy.context, plan, settings, backend=backend, transaction=tx)
job.run_to_completion()
for line in job.report.details():
    print("  " + line)
check("全部任务成功", job.report.failed == 0 and job.report.done == 5,
      (job.report.done, job.report.failed,
       [r.error for r in job.report.failed_results()]))


def sample(image, u=0.5, v=0.5):
    """取一个像素的 RGBA（UV 中心，避开边缘）"""
    width, height = image.size
    x = int(u * width)
    y = int(v * height)
    index = (y * width + x) * 4
    pixels = list(image.pixels[index:index + 4])
    return tuple(round(value, 4) for value in pixels)


by_key = {}
for task in plan.tasks:
    by_key[task.bake_type.key] = task

red = sample(by_key["shader.base_color"].image)
print("  Base Color 像素:", red)
check("Base Color 真的烘出了纯红（不是全黑）",
      red[0] > 0.9 and red[1] < 0.1 and red[2] < 0.1, red)

rough = sample(by_key["shader.roughness"].image)
print("  Roughness 像素:", rough)
check("Roughness 真的烘出了 0.25",
      abs(rough[0] - 0.25) < 0.03 and rough[0] == rough[1] == rough[2], rough)

metal = sample(by_key["shader.metallic"].image)
print("  Metallic 像素:", metal)
check("Metallic 真的烘出了 1.0", metal[0] > 0.95, metal)

normal = sample(by_key["shader.normal"].image)
print("  Normal 像素:", normal)
check("Normal 是正对相机的 (0.5,0.5,1) 附近",
      abs(normal[0] - 0.5) < 0.06 and abs(normal[1] - 0.5) < 0.06 and normal[2] > 0.9,
      normal)

def srgb_to_linear(value):
    """通道打包是 Color 通道 -> 图存的是 sRGB 编码值，比较前要解回来"""
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


packed = sample(by_key["misc.channel_packing"].image)
print("  Channel Packing 像素 (sRGB 编码, 期望 R=0.25 G=1.0 B=AO):", packed)
check("通道打包 R = Roughness 0.25（解 sRGB 后）",
      abs(srgb_to_linear(packed[0]) - 0.25) < 0.03,
      (srgb_to_linear(packed[0]), packed))
check("通道打包 G = Metallic 1.0", packed[1] > 0.95, packed)
check("通道打包 B = AO 节点（孤立四边形完全没遮挡，应为 1.0）",
      packed[2] > 0.95, packed)
check("三个通道互不相同（没有串色）",
      len({round(srgb_to_linear(v), 2) for v in packed[:3]}) == 2,
      packed)

print("\n=== 阶段 6e：材质一点没被动过 ===")
check("烘完节点集合与烘前完全一致",
      sorted(n.type for n in tree.nodes) == node_types_before,
      (node_types_before, sorted(n.type for n in tree.nodes)))
check("烘完连线数一致", len(tree.links) == links_before,
      (links_before, len(tree.links)))
check("材质里没有残留 Emission 节点",
      not [n for n in tree.nodes if n.type == 'EMISSION'])
check("材质里没有残留 Combine/Separate 节点",
      not [n for n in tree.nodes if n.type in ('COMBINE_COLOR', 'SEPARATE_COLOR')])
output = [n for n in tree.nodes if n.type == 'OUTPUT_MATERIAL'][0]
check("Material Output 接的还是 Principled",
      output.inputs["Surface"].links[0].from_node.type == 'BSDF_PRINCIPLED')
check("Base Color 的值没被改",
      tuple(round(v, 3) for v in shader.inputs["Base Color"].default_value)
      == (1.0, 0.0, 0.0, 1.0),
      tuple(shader.inputs["Base Color"].default_value))

files = sorted(os.path.basename(f) for f in glob.glob(os.path.join(OUT, "**", "*.png"), recursive=True))
print("  导出:", files)
check("5 个文件都写了", len(files) == 5, files)
check("报告里记了手术细节",
      all(result.surgery for result in job.report.results),
      [(r.type_key, r.surgery) for r in job.report.results])

print("\n=== 阶段 6f：手术失败要能被发现，不能静默出黑图 ===")
bpy.ops.wm.read_factory_settings(use_empty=True)
mesh = bpy.data.meshes.new("M")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0)], [], [(0, 1, 2)])
mesh.update()
mesh.uv_layers.new(name="UVMap")
hollow = bpy.data.materials.new("Hollow")
hollow.use_nodes = True
hollow.node_tree.nodes.clear()          # 故意没有任何节点
mesh.materials.append(hollow)
obj = bpy.data.objects.new("Tri", mesh)
bpy.context.scene.collection.objects.link(obj)

bad_settings = pl.BakeSettings(
    maps=pl.map_requests_from_pairs([("shader.base_color", 32)]))
bad_plan = pl.build_plan(bpy.context, bad_settings)
bad_job = jb.BakeJob(bpy.context, bad_plan, bad_settings,
                     backend=bk.CyclesBackend(txmod.SceneTransaction(bpy.context),
                                              margin=2, samples=1))
bad_job.run_to_completion()
print("  报告:", bad_job.report.summary())
check("材质没有节点时任务被判失败", bad_job.report.failed == 1, bad_job.report.failed)
check("失败原因说明了是节点树的问题",
      "node tree" in (bad_job.report.failed_results()[0].error if
                      bad_job.report.failed_results() else ""),
      [r.error for r in bad_job.report.failed_results()])

# 标准 pass 不需要手术，空材质也能烘
ok_settings = pl.BakeSettings(
    maps=pl.map_requests_from_pairs([("standard.combined", 32)]))
ok_plan = pl.build_plan(bpy.context, ok_settings)
ok_job = jb.BakeJob(bpy.context, ok_plan, ok_settings,
                    backend=bk.CyclesBackend(txmod.SceneTransaction(bpy.context),
                                             margin=2, samples=1))
ok_job.run_to_completion()
check("标准 pass 不依赖节点，空材质也能烘", ok_job.report.failed == 0,
      (ok_job.report.failed, [r.error for r in ok_job.report.failed_results()]))

print("\n=== 阶段 6g：静态审计 ===")
source = open(os.path.join(WS, "material_bakery", "engine", "surgery.py"),
              encoding="utf-8").read()

# 真正的检查：AST 里不许出现 inputs[数字] / outputs[数字] 这种按位次取插口的写法
# （用字符串包含判断会把"警告不要这么写"的注释也算进去）
# 唯一允许的例外：那几行必须带 `socket-index:` 说明自己为什么非按位次不可。
lines = source.splitlines()
index_access = []
for node in ast.walk(ast.parse(source)):
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
        if node.value.attr in ("inputs", "outputs"):
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int):
                # 说明可以写成多行注释，所以往前看几行
                window = "\n".join(lines[max(0, node.lineno - 6):node.lineno])
                if "socket-index" not in window:
                    index_access.append("line {}: {}".format(node.lineno, text.strip()))
check("surgery.py 里没有未加说明的按位次取插口",
      not index_access, index_access)
check("允许的例外都写了理由（socket-index 注释）",
      "socket-index" in source)
check("原地不敢用位次这件事写进了注释", "别写 outputs[0]" in source or "写死下标" in source)
check("每个 BakeKind 都有 handler 或有明确理由不处理",
      all(kind in sg.HANDLERS for kind in bt.BakeKind),
      [k for k in bt.BakeKind if k not in sg.HANDLERS])
check("needs_surgery 对标准 pass 返回假",
      not sg.needs_surgery(bt.require("standard.combined")))
check("needs_surgery 对 shader 类型返回真",
      sg.needs_surgery(bt.require("shader.base_color")))
hidden = [key for key, item in bt.REGISTRY.items()
          if item.bake_pass == "EMIT" and not sg.needs_surgery(item)
          and item.kind is not bt.BakeKind.STANDARD_PASS]
check("没有 EMIT 类型被漏掉手术", not hidden, hidden)

print("\n=== 结果 ===")
if FAIL:
    print("FAILED {} check(s):".format(len(FAIL)))
    for name in FAIL:
        print("  -", name)
else:
    print("ALL CHECKS PASSED")

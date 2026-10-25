"""Material Bakery —— 实施轮 1.6 测试：base_color **默认就带 alpha**

用户的规矩（原话）："**能不能直接把 alpha 弄进 basecolor，别分家了，用户要选 alpha
通道那就给它一张单独的 alpha 通道贴图，默认的 basecolor 就是带 alpha，就这么简单，
不会再有多的什么带不带 alpha 的话题了！**"

这一套量的是**烘完写盘之前**那一步（`core/alpha_merge.py` + `engine/backend.py`），
以及交付材质的接线：

  A. 建图时机：这一组有 `shader.alpha` 任务 → base_color 目标图 4 通道；没有 → 24 位
  B. 勾了 alpha：落盘那张 `depth == 32`、**A == alpha 图的灰度**（8 位量化容差）、
     **RGB 与"没勾 alpha"时逐像素相同**（max diff 0.000000）、**文件名就是原名**
  C. 没勾 alpha：`depth == 24`、文件名同样是原名（不加任何注明）
  D. **顺序**：alpha 排在 base_color **后面**、以及**前面**，两种顺序都要出正确的 RGBA 文件，
     而且 base_color 的 manifest 一定被写
  E. alpha 任务失败：base_color 照样落盘（纯 RGB / A 恒 1.0 = 不透明），日志点名
  F. 尺寸不一致：跳过合并（A 保持 1.0）、base_color 仍按原名落盘、说明写进报告
  G. 交付材质：base_color 自带 alpha 时它的 **Alpha 输出接到 Principled 的 Alpha**；
     有独立 alpha 图时接的是那张（base_color 的 Alpha 不接）
  H. 向后兼容：老名字 `…_NoAlpha.png` 仍能解析回 `shader.base_color`
  I. 旧机制真的没了：按钮 / 勾选框 / `file_name_for` / `_NoAlpha` 写入端

判据全部是打印出来的数字（位深、A 读数、RGB 最大逐像素差、目录文件清单）。
不硬编码版本号 / 绝对路径 / 计数。
"""
import array
import glob
import os
import shutil
import sys

import bpy

# 工作区 = 本文件所在目录的上一级。
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WS not in sys.path:
    sys.path.insert(0, WS)

import material_bakery
from material_bakery import compat
from material_bakery.core import alpha_merge as am
from material_bakery.core import imported as im
from material_bakery.core import naming
from material_bakery.core import plan as pl
from material_bakery.core import scene_scan as ss
from material_bakery.core import transaction as txmod
from material_bakery.engine import backend as bk
from material_bakery.engine import job as jb
from material_bakery.ui import ops as uops

compat.set_silent(True)

FAIL = []


def check(label, ok, extra=""):
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", label,
                               ("  " + str(extra)) if extra else ""))
    if not ok:
        FAIL.append(label)


OUT = os.path.join(WS, "_probe", "base_alpha")
shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT)

QUANT = 1.0 / 255.0          # 8 位图的量化步长
SIZE = 128


# --------------------------------------------------------------------------- 工具

def read_pixels(path):
    """把磁盘上的图读回来。返回 (image, array('f') RGBA)"""
    image = bpy.data.images.load(path)
    count = image.size[0] * image.size[1] * image.channels
    buffer = array.array('f', bytes(4 * count))
    image.pixels.foreach_get(buffer)
    return image, buffer


def max_diff(a, b, channel):
    left, right = a[channel::4], b[channel::4]
    if len(left) != len(right):
        return 999.0
    return max(abs(x - y) for x, y in zip(left, right))


def bake(maps, directory, fail_on=(), prefix="Body", save=True):
    """跑一轮真计划 + NullBackend（不发真 Cycles），返回 (job, plan, settings)

    `fail_on` 收的是**类型 key**（如 `("shader.alpha",)`），内部换算成计划里那些任务的
    完整 key —— NullBackend 是按 `task.key` / 组名匹配的。
    """
    for name in list(bpy.data.objects):
        bpy.data.objects.remove(name, do_unlink=True)
    bpy.ops.mesh.primitive_cube_add(size=1.0)
    obj = bpy.context.active_object
    obj.name = prefix
    obj.data.uv_layers.new(name="UVMap")
    material = bpy.data.materials.new(prefix + "Mat")
    material.use_nodes = True
    obj.data.materials.append(material)
    collection = bpy.data.collections.new(prefix)
    bpy.context.scene.collection.children.link(collection)
    for coll in list(obj.users_collection):
        coll.objects.unlink(obj)
    collection.objects.link(obj)

    settings = pl.BakeSettings(maps=pl.map_requests_from_pairs(maps))
    plan = pl.build_plan(bpy.context, settings)
    failing_keys = tuple(task.key for task in plan.tasks
                         if task.bake_type.key in fail_on)
    backend = bk.NullBackend(save=save, directory=directory, fail_on=failing_keys)
    job = jb.BakeJob(bpy.context, plan, settings, backend=backend,
                     transaction=txmod.SceneTransaction(bpy.context))
    job.start()
    steps = 0
    while not job.is_finished and steps < 400:
        job.step()
        steps += 1
    return job, plan, settings


def task_for(plan, type_key):
    for task in plan.tasks:
        if task.bake_type.key == type_key:
            return task
    return None


def job_dir(name):
    path = os.path.join(OUT, name)
    os.makedirs(path, exist_ok=True)
    return path


# --------------------------------------------------------------------------- A
print("=== 阶段 A：建图时机与旧机制 ===")
material_bakery.register()
settings_ui = bpy.context.scene.mbakery
check("Result 页那个按钮的 operator 已经没了",
      "export_base_color_with_alpha" not in dir(bpy.ops.mbakery),
      [n for n in dir(bpy.ops.mbakery) if "export" in n])
check("没有 merge_alpha 勾选框字段", not hasattr(settings_ui, "merge_alpha"))
check("naming 里没有 file_name_for / NO_ALPHA_SUFFIX（整套删掉）",
      not hasattr(naming, "file_name_for") and not hasattr(naming, "NO_ALPHA_SUFFIX"))
check("alpha 合并核心在 core 里（engine 不反向依赖 deliver）",
      hasattr(am, "merge_alpha_into") and hasattr(am, "AlphaStitcher"))
check("image_has_alpha 看的是位深不是 channels（channels 恒为 4）",
      am.image_has_alpha(bpy.data.images.new("probe_a", 4, 4, alpha=True)) is True
      and am.image_has_alpha(bpy.data.images.new("probe_b", 4, 4, alpha=False)) is False)

with_alpha_dir = job_dir("with_alpha")
# 用一轮真实计划来量"建图时机"：勾了 alpha → base_color 那张图 4 通道
job_a, plan_a, _ = bake([("shader.base_color", SIZE), ("shader.alpha", SIZE)],
                        with_alpha_dir)
base_task = task_for(plan_a, "shader.base_color")
alpha_task = task_for(plan_a, "shader.alpha")
check("勾了 alpha：base_color 目标图有 alpha 通道（depth 32）",
      am.image_has_alpha(base_task.image),
      getattr(base_task.image, "depth", None))
check("alpha 那张仍然是普通数据图", alpha_task.image is not None)
job_b, plan_b, _ = bake([("shader.base_color", SIZE), ("shader.roughness", SIZE)],
                        job_dir("target_without_alpha"))
base_task_b = task_for(plan_b, "shader.base_color")
check("没勾 alpha：base_color 目标图**没有** alpha 通道（depth 24）",
      not am.image_has_alpha(base_task_b.image),
      getattr(base_task_b.image, "depth", None))

# --------------------------------------------------------------------------- B
print("\n=== 阶段 B：勾了 alpha → 落盘的 base_color 带 A ===")
b_dir = job_dir("b_with_alpha")
job, plan, _ = bake([("shader.base_color", SIZE), ("shader.alpha", SIZE)], b_dir)
base_task = task_for(plan, "shader.base_color")
alpha_task = task_for(plan, "shader.alpha")
base_path = os.path.join(b_dir, base_task.image_name + ".png")
print("  文件:", sorted(os.listdir(b_dir)))
check("base_color 落盘了，而且名字**就是原名**（不带任何后缀）",
      os.path.isfile(base_path) and os.path.basename(base_path) == base_task.image_name + ".png",
      os.path.basename(base_path))
check("文件名里没有任何 alpha 注明（1.5b 的 _NoAlpha 整套没了）",
      "_NoAlpha" not in os.path.basename(base_path)
      and not os.path.basename(base_path).lower().endswith("_alpha.png"),
      os.path.basename(base_path))
image, pixels = read_pixels(base_path)
depth = image.depth
bpy.data.images.remove(image)
alpha_values = pixels[3::4]
print("  A: min={:.4f} max={:.4f}（NullBackend 把 alpha 图填成 0.5 灰）".format(
    min(alpha_values), max(alpha_values)))
check("落盘那张 depth == 32", depth == 32, depth)
check("A == alpha 图的灰度 0.5（含量化容差）",
      all(abs(v - 0.5) <= QUANT + 1e-4 for v in alpha_values),
      "最大偏差 {:.6f}".format(max(abs(v - 0.5) for v in alpha_values)))
check("RGB 不是黑的（真的烘了东西）",
      max(pixels[0::4]) > 0.05, max(pixels[0::4]))

# 对照：没勾 alpha 的那一轮，RGB 必须逐像素一样
c_dir = job_dir("c_without_alpha")
job_c, plan_c, _ = bake([("shader.base_color", SIZE), ("shader.roughness", SIZE)], c_dir)
base_task_c = task_for(plan_c, "shader.base_color")
c_path = os.path.join(c_dir, base_task_c.image_name + ".png")
image_c, pixels_c = read_pixels(c_path)
depth_c = image_c.depth
bpy.data.images.remove(image_c)
rgb_delta = max(max_diff(pixels, pixels_c, channel) for channel in (0, 1, 2))
print("  RGB 与『没勾 alpha』那轮逐像素比较: max diff = {:.6f}".format(rgb_delta))
check("RGB 逐像素**一个字节都没改**（合并只动 A）", rgb_delta <= 1e-6, rgb_delta)
check("没勾 alpha 时 depth == 24", depth_c == 24, depth_c)
check("没勾 alpha 时文件名同样是原名",
      os.path.basename(c_path) == base_task_c.image_name + ".png",
      os.path.basename(c_path))

# --------------------------------------------------------------------------- D
print("\n=== 阶段 D：顺序（alpha 排前 / 排后）===")
order_dir = job_dir("d_order")
job_d, plan_d, _ = bake([("shader.alpha", SIZE), ("shader.base_color", SIZE)], order_dir)
types = [task.bake_type.key for task in plan_d.tasks]
print("  计划顺序:", types)
check("前提：这一轮 alpha 真的排在 base_color **前面**",
      types.index("shader.alpha") < types.index("shader.base_color"), types)
base_task_d = task_for(plan_d, "shader.base_color")
d_path = os.path.join(order_dir, base_task_d.image_name + ".png")
check("alpha 排前面时 base_color 照样落盘（名字是原名）", os.path.isfile(d_path), d_path)
image_d, pixels_d = read_pixels(d_path)
depth_d = image_d.depth
bpy.data.images.remove(image_d)
check("而且 A 也是 0.5（顺序不影响结果）",
      depth_d == 32 and all(abs(v - 0.5) <= QUANT + 1e-4 for v in pixels_d[3::4]),
      (depth_d, min(pixels_d[3::4]), max(pixels_d[3::4])))
check("两轮（alpha 前 / 后）的字节内容逐像素一致",
      max(max_diff(pixels, pixels_d, c) for c in range(4)) <= 1e-6,
      max(max_diff(pixels, pixels_d, c) for c in range(4)))

manifest_path = os.path.join(order_dir, im.MANIFEST_NAME)
check("manifest 被写出来了", os.path.isfile(manifest_path), sorted(os.listdir(order_dir)))
if os.path.isfile(manifest_path):
    payload = im.read_manifest(order_dir) or {}
    keys = sorted((payload.get("textures") or {}).keys())
    print("  manifest 里的通道:", keys)
    check("manifest 里 base_color 与 alpha 两条都在",
          "shader.base_color" in keys and "shader.alpha" in keys, keys)

# --------------------------------------------------------------------------- E
print("\n=== 阶段 E：alpha 任务失败 → base_color 照样落盘 ===")
e_dir = job_dir("e_alpha_failed")
job_e, plan_e, _ = bake([("shader.base_color", SIZE), ("shader.alpha", SIZE)], e_dir,
                        fail_on=("shader.alpha",))
print("  报告:", job_e.report.summary())
base_task_e = task_for(plan_e, "shader.base_color")
e_path = os.path.join(e_dir, base_task_e.image_name + ".png")
check("alpha 失败了，base_color 仍然落盘（不许因为搭档没来就丢文件）",
      os.path.isfile(e_path), sorted(os.listdir(e_dir)))
image_e, pixels_e = read_pixels(e_path)
depth_e = image_e.depth
bpy.data.images.remove(image_e)
check("它是不透明的（A == 1.0，不会悄悄变全透明）",
      all(abs(v - 1.0) <= 1e-6 for v in pixels_e[3::4]),
      (min(pixels_e[3::4]), max(pixels_e[3::4])))
check("alpha 失败本身照原样记在报告里", job_e.report.failed >= 1, job_e.report.failed)
check("报告里 base_color 的 filepath 指向真的存在的那张图（晚到路径被补回）",
      any(r.type_key == "shader.base_color" and r.filepath
          and os.path.isfile(r.filepath) for r in job_e.report.results),
      [(r.type_key, r.filepath) for r in job_e.report.results])

# --------------------------------------------------------------------------- F
print("\n=== 阶段 F：尺寸不一致 → 跳过合并 + 点名 ===")
f_dir = job_dir("f_size_mismatch")
job_f, plan_f, _ = bake([("shader.base_color", 128), ("shader.alpha", 64)], f_dir)
base_task_f = task_for(plan_f, "shader.base_color")
f_path = os.path.join(f_dir, base_task_f.image_name + ".png")
check("base_color 仍按原名落盘", os.path.isfile(f_path), sorted(os.listdir(f_dir)))
image_f, pixels_f = read_pixels(f_path)
bpy.data.images.remove(image_f)
check("没合并：A 保持 1.0（不透明）",
      all(abs(v - 1.0) <= 1e-6 for v in pixels_f[3::4]),
      (min(pixels_f[3::4]), max(pixels_f[3::4])))
warnings = [w for w in job_f.report.warnings if "sizes differ" in w]
print("  报告里的 WARN:", warnings)
check("报告里点名了『尺寸不一致』（不静默）", bool(warnings), job_f.report.warnings)

# --------------------------------------------------------------------------- G
print("\n=== 阶段 G：交付材质把 base_color 的 Alpha 接上 ===")
from material_bakery.core.materials import MaterialStore          # noqa: E402
from material_bakery.deliver import materials as mat_mod          # noqa: E402

store = MaterialStore()
merged = bpy.data.images.new("G_BaseColor", 8, 8, alpha=True, float_buffer=False)
plain = bpy.data.images.new("G_BaseColorPlain", 8, 8, alpha=False, float_buffer=False)
alpha_img = bpy.data.images.new("G_Alpha", 8, 8, alpha=False, float_buffer=True)

material, report = mat_mod.build_final_material(
    store, "G_BaseOnly", {"shader.base_color": merged}, uv_layer="UVMap")
shader = next(n for n in material.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
image_node = next(n for n in material.node_tree.nodes if n.type == 'TEX_IMAGE')
alpha_input = next(s for s in shader.inputs if s.name in ("Alpha", "Transparency"))
print("  base_color 单独一组时的连线:", [(l.from_node.name, l.from_socket.name,
                                          l.to_socket.name)
                                         for l in material.node_tree.links])
check("base_color 自带 alpha 时，它的 **Alpha 输出**接到了 Principled 的 Alpha",
      any(link.from_node == image_node and link.from_socket.name == "Alpha"
          and link.to_socket == alpha_input for link in material.node_tree.links),
      [(l.from_socket.name, l.to_socket.name) for l in material.node_tree.links])

material2, _report2 = mat_mod.build_final_material(
    store, "G_WithAlphaMap",
    {"shader.base_color": merged, "shader.alpha": alpha_img}, uv_layer="UVMap")
shader2 = next(n for n in material2.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
alpha_node = next(n for n in material2.node_tree.nodes
                  if n.type == 'TEX_IMAGE' and n.image == alpha_img)
alpha_input2 = next(s for s in shader2.inputs if s.name in ("Alpha", "Transparency"))
check("有独立 alpha 图时，接的是**那张**（现状不变）",
      any(link.from_node == alpha_node and link.to_socket == alpha_input2
          for link in material2.node_tree.links),
      [(l.from_node.name, l.to_socket.name) for l in material2.node_tree.links])

material3, _report3 = mat_mod.build_final_material(
    store, "G_PlainOnly", {"shader.base_color": plain}, uv_layer="UVMap")
shader3 = next(n for n in material3.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
alpha_input3 = next(s for s in shader3.inputs if s.name in ("Alpha", "Transparency"))
check("base_color 没有 alpha 通道时**不接** Alpha（别接一根假的）",
      not alpha_input3.is_linked, alpha_input3.is_linked)

# --------------------------------------------------------------------------- H
print("\n=== 阶段 H：向后兼容（老名字）===")
for filename in ("Body-BaseColor-2k_NoAlpha.png",
                 "Body-BaseColor-2k_NoAlpha.1001.png",
                 "Body-BaseColor-2k.png"):
    key, size_x, _size_y, _prefix = im.parse_filename(filename)
    check("老名字 {} 仍解析成 base_color".format(filename),
          key == "shader.base_color", (key, size_x))
check("老名字不会被认成 shader.alpha",
      im.parse_filename("Body-BaseColor-2k_NoAlpha.png")[0] != "shader.alpha")

# --------------------------------------------------------------------------- I
print("\n=== 阶段 I：导出这一条路不再做任何加工 ===")
check("导出模块里已经没有合并函数了",
      not hasattr(uops, "MBAKERY_OT_ExportBaseColorWithAlpha"))
from material_bakery.deliver import textures as tex          # noqa: E402
check("deliver.textures 里没有 merge / alpha 相关的东西",
      not any(hasattr(tex, name) for name in
              ("merge_alpha_plan", "export_alpha_base_colors", "alpha_product_name",
               "alpha_image_for", "alpha_lost")),
      [n for n in dir(tex) if "alpha" in n.lower()])
check("清单化修复**保留着**（1.5 修的那个 AttributeError 不能回退）",
      hasattr(tex, "build_manifest") and hasattr(tex, "export_report")
      and hasattr(uops, "_require_exportable"))

# --------------------------------------------------------------------------- J
print("\n=== 阶段 J：导出那张带 A 的图（端到端）===")
from material_bakery.deliver import textures as tex          # noqa: E402
from material_bakery.ui import session as usession           # noqa: E402

j_dir = job_dir("j_export")
job_j, plan_j, _ = bake([("shader.base_color", SIZE), ("shader.alpha", SIZE)], j_dir)
base_task_j = task_for(plan_j, "shader.base_color")
active = usession.Session.get()
active.report = job_j.report
active.job = None
active.imported_by_group = {}
settings_ui.output_dir = j_dir
settings_ui.use_subfolders = False
settings_ui.file_format = 'PNG'
result = None
try:
    result = bpy.ops.mbakery.export_textures()
except RuntimeError as exc:
    print("      (operator 报错: {})".format(exc))
check("整排导出跑通（导的就是那张烘出来的图）", result == {'FINISHED'}, result)
exported = tex.target_path(base_task_j.image_name, 'PNG', j_dir)
check("导出的文件名就是 image_name（不再改名、不再合并）",
      os.path.isfile(exported), sorted(os.listdir(j_dir)))
image_j, pixels_j = read_pixels(exported)
depth_j = image_j.depth
bpy.data.images.remove(image_j)
check("导出的那张 depth == 32（A 是烘完就带上的，不是导出时加的）", depth_j == 32, depth_j)
check("导出的那张 A == 0.5（alpha 图的灰度）",
      all(abs(v - 0.5) <= QUANT + 1e-4 for v in pixels_j[3::4]),
      (min(pixels_j[3::4]), max(pixels_j[3::4])))

# --------------------------------------------------------------------------- K
print("\n=== 阶段 K：复用同一张图时通道数要对得上（否则重建）===")
channel = bk.bake_types.get("shader.base_color").channel
plain_image = bk.ensure_image("K_Reuse", 64, 64, channel, alpha=False)
plain_depth = plain_image.depth
check("先建一张 24 位的（这一组没有 alpha 任务）", plain_depth == 24, plain_depth)
# ⚠ 不要再碰 plain_image 了：下面这一次调用会把它**删掉重建**，那个引用随即作废
#   （读它会 ReferenceError: StructRNA of type Image has been removed）。
alpha_image = bk.ensure_image("K_Reuse", 64, 64, channel, alpha=True)
check("同一名字改要 4 通道 → 数据块被**重建**成 32 位（depth 只读，改不了）",
      alpha_image.depth == 32, alpha_image.depth)
check("重建之后同名数据块只有一个（旧的那张真被删了）",
      bpy.data.images.get("K_Reuse") == alpha_image,
      [img.name for img in bpy.data.images if img.name.startswith("K_Reuse")])
back_image = bk.ensure_image("K_Reuse", 64, 64, channel, alpha=False)
check("再要回 24 位也一样重建", back_image.depth == 24, back_image.depth)
check("重建之后仍然带我们的标记（不会被当成用户自己的图）",
      bk.IMAGE_TAG in back_image.keys())

# --------------------------------------------------------------------------- L
print("\n=== 阶段 L：存不住 alpha 的格式（JPEG）点名说明 ===")
jpeg_base = bpy.data.images.new("L_Base", 16, 16, alpha=True, float_buffer=False)
jpeg_alpha = bpy.data.images.new("L_Alpha", 16, 16, alpha=False, float_buffer=True)
jpeg_alpha.pixels.foreach_set(array.array('f', [0.5, 0.5, 0.5, 1.0] * (16 * 16)))
ok, notes = am.merge_alpha_into(jpeg_base, jpeg_alpha, 'JPEG')
levels = [level for level, _text in notes]
print("  JPEG 下的说明:", notes)
check("JPEG 下仍然合并（内存里那张、材质接线都要一致）", ok is True, ok)
check("但明确给一条 WARN 说这个格式存不住 alpha",
      "WARN" in levels and any("JPEG" in text and "PNG or TIFF" in text
                               for _level, text in notes), notes)
check("PNG 下没有那条 WARN（对照）",
      "WARN" not in [level for level, _t in am.merge_alpha_into(
          bpy.data.images.new("L_Base2", 16, 16, alpha=True, float_buffer=False),
          bpy.data.images.new("L_Alpha2", 16, 16, alpha=False, float_buffer=True),
          'PNG')[1]])

print("\n=== 结果 ===")
if FAIL:
    print("FAILED {} check(s):".format(len(FAIL)))
    for name in FAIL:
        print("  -", name)
else:
    print("ALL CHECKS PASSED")

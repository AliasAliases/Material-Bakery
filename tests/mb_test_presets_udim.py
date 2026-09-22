"""Material Bakery — 阶段 5 / 7 / 8 测试

阶段 5  UDIM：瓦片编号、多瓦片识别、按瓦片出文件
阶段 7  交付层补齐：报告存取
阶段 8  预设系统 / 暂停继续 / 抗锯齿 / 自适应边距 / 确认弹窗
"""
import bpy, sys, os, shutil, glob, json, ast

# 工作区 = 本文件所在目录的上一级。
# ⚠ 这里原来写死的是开发机上的绝对路径，clone 到别处每个套件都跑不起来，
#   而且公开版/私有版两份检出会互相 import 错代码。
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WS)

import material_bakery
from material_bakery import compat
from material_bakery.core import bake_types as bt
from material_bakery.core import plan as pl
from material_bakery.core import presets as pr
from material_bakery.core import scene_scan as ss
from material_bakery.core import udim as ud
from material_bakery.core import transaction as txmod
from material_bakery.engine import backend as bk
from material_bakery.engine import job as jb
from material_bakery.engine import report as rp
from material_bakery.ui import ops as uops
from material_bakery.ui import ops_presets as upre
from material_bakery.ui import properties as uprops
from material_bakery.ui import session as usession

compat.set_silent(True)

FAIL = []
def check(label, ok, extra=""):
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", label, ("  " + str(extra)) if extra else ""))
    if not ok:
        FAIL.append(label)

def op(operator, **kwargs):
    try:
        return operator(**kwargs)
    except RuntimeError as exc:
        print("      (operator 报错被拦: {})".format(exc))
        return {'CANCELLED'}

OUT = os.path.join(WS, "_probe", "phase578")
OUT_A = os.path.join(OUT, "reference")
OUT_B = os.path.join(OUT, "new")
for path in (OUT_A, OUT_B):
    if os.path.isdir(path):
        shutil.rmtree(path)
    os.makedirs(path)
if os.path.isdir(OUT):
    for name in os.listdir(OUT):
        full = os.path.join(OUT, name)
        if os.path.isfile(full):
            os.remove(full)
        elif os.path.isdir(full) and full not in (OUT_A, OUT_B):
            # ⚠ 按集合分文件夹之后，烘出来的图在**子目录**里（OUT/Body/...）。
            #   只清根目录的文件是不够的：命名规则一改，上一轮的旧文件名还在，
            #   数文件的断言就会把两次运行的结果一起算（这次就是这么挂的）。
            shutil.rmtree(full, ignore_errors=True)

material_bakery.register()


# ------------------------------------------------------------------ 场景
def quad(name, uv_box=(0.0, 0.0, 1.0, 1.0), color=(1.0, 0.0, 0.0, 1.0)):
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
    mesh.update()
    layer = mesh.uv_layers.new(name="UVMap")
    u0, v0, u1, v1 = uv_box
    for i, corner in enumerate(((u0, v0), (u1, v0), (u1, v1), (u0, v1))):
        layer.data[i].uv = corner
    material = bpy.data.materials.new(name + "Mat")
    material.use_nodes = True
    for node in material.node_tree.nodes:
        if node.type == 'BSDF_PRINCIPLED':
            node.inputs["Base Color"].default_value = color
    mesh.materials.append(material)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


print("=== 阶段 5a：瓦片编号 ===")
check("(0.5, 0.5) -> 1001", ud.tile_of(0.5, 0.5) == 1001, ud.tile_of(0.5, 0.5))
check("(1.5, 0.5) -> 1002", ud.tile_of(1.5, 0.5) == 1002, ud.tile_of(1.5, 0.5))
check("(0.5, 1.5) -> 1011（v 每跨 1 加 10）",
      ud.tile_of(0.5, 1.5) == 1011, ud.tile_of(0.5, 1.5))
check("(2.5, 3.5) -> 1033", ud.tile_of(2.5, 3.5) == 1033, ud.tile_of(2.5, 3.5))
check("瓦片号反解一致",
      all(ud.tile_of(*[v + 0.5 for v in ud.tile_bounds(t)[:2]]) == t
          for t in (1001, 1002, 1011, 1033, 1099, 1100)),
      [ud.tile_bounds(t) for t in (1001, 1100)])
check("1100 的列是 9 行是 9（offset 99 = 9 + 9*10）",
      ud.tile_bounds(1100)[:2] == (9.0, 9.0), ud.tile_bounds(1100))
check("1012 的列是 1 行是 1", ud.tile_bounds(1012)[:2] == (1.0, 1.0),
      ud.tile_bounds(1012))
# 边界：恰好 1.0 属于 1001（否则铺满 0..1 的普通模型会被误判成 UDIM）
check("u=1.0 仍算 1001", ud.tile_of(1.0, 0.5) == 1001, ud.tile_of(1.0, 0.5))
check("u=0.0 算 1001", ud.tile_of(0.0, 0.0) == 1001, ud.tile_of(0.0, 0.0))
check("u=1.001 才算 1002", ud.tile_of(1.001, 0.5) == 1002, ud.tile_of(1.001, 0.5))
check("负 UV 归到 1001 而不是负数瓦片", ud.tile_of(-0.2, -0.2) == 1001,
      ud.tile_of(-0.2, -0.2))

print("\n=== 阶段 5b：多瓦片识别 ===")
bpy.ops.wm.read_factory_settings(use_empty=True)
single = quad("Single")
wide = quad("Wide", uv_box=(0.0, 0.0, 2.0, 1.0))
tall = quad("Tall", uv_box=(0.0, 0.0, 1.0, 3.0))

check("单瓦片物体只报一个瓦片", ud.object_tiles(single) == [1001],
      ud.object_tiles(single))
check("横跨两列的物体报两个瓦片", ud.object_tiles(wide) == [1001, 1002],
      ud.object_tiles(wide))
check("竖跨三行的物体报三个瓦片", ud.object_tiles(tall) == [1001, 1011, 1021],
      ud.object_tiles(tall))
check("单瓦片不算 UDIM", not ud.is_udim([single]))
check("多瓦片算 UDIM", ud.is_udim([wide]))

col = bpy.data.collections.new("Body")
bpy.context.scene.collection.children.link(col)
for name in ("Single", "Wide", "Tall"):
    obj = bpy.data.objects[name]
    bpy.context.scene.collection.objects.unlink(obj)
    col.objects.link(obj)
check("整组瓦片是并集", ud.group_tiles([bpy.data.objects["Wide"],
                                        bpy.data.objects["Tall"]])
      == [1001, 1002, 1011, 1021],
      ud.group_tiles([bpy.data.objects["Wide"], bpy.data.objects["Tall"]]))

print("\n=== 阶段 5c：UDIM 命名与计划 ===")
settings = bpy.context.scene.mbakery
settings.maps.clear()
uops.add_map(settings, "shader.base_color", 64, dedupe=True)
settings.udim = False
plain = pl.build_plan(bpy.context, usession.settings_to_bake_settings(settings))
# ⚠ 断言跟着命名规则走：尺寸现在渲染成小写 "1k/2k"（用户指定），
#   连接符是 "-" 且不带空格。
check("关掉 UDIM 时用普通命名（-类型-k）",
      all("<UDIM>" not in t.image_name and "px" not in t.image_name
          and " - " not in t.image_name for t in plain.tasks),
      [t.image_name for t in plain.tasks])
check("关掉 UDIM 时没有瓦片", all(not t.udim_tiles for t in plain.tasks))

settings.udim = True
uplan = pl.build_plan(bpy.context, usession.settings_to_bake_settings(settings))
wide_task = [t for t in uplan.tasks if t.group_name == "Body"][0]
print("  UDIM 任务名:", wide_task.image_name, " 瓦片:", wide_task.udim_tiles)
check("UDIM 时命名用 {size} + 瓦片号（不带 px、不带空格外加的那个 x）",
      wide_task.image_name.startswith("Body-BaseColor-64."),
      wide_task.image_name)
check("UDIM 时每张瓦片一个任务（4 张瓦片 -> 4 个任务）",
      len(uplan.tasks) == 4, len(uplan.tasks))
check("任务名带瓦片号",
      sorted(t.image_name for t in uplan.tasks) ==
      ["Body-BaseColor-64.{}".format(t) for t in (1001, 1002, 1011, 1021)],
      sorted(t.image_name for t in uplan.tasks))
check("每个任务记了自己的瓦片号",
      sorted(t.tile for t in uplan.tasks) == [1001, 1002, 1011, 1021],
      sorted(t.tile for t in uplan.tasks))
check("UV 平移量正确（1002 -> (-1,0)，1011 -> (0,-1)）",
      [t.uv_shift for t in uplan.tasks if t.tile == 1002] == [(-1, 0)]
      and [t.uv_shift for t in uplan.tasks if t.tile == 1011] == [(0, -1)],
      [(t.tile, t.uv_shift) for t in uplan.tasks])
check("非 UDIM 任务没有 UV 平移",
      all(not t.uv_shift for t in plain.tasks),
      [(t.tile, t.uv_shift) for t in plain.tasks])
check("瓦片平移量反解正确",
      ud.tile_uv_shift(1001) == (0, 0) and ud.tile_uv_shift(1033) == (-2, -3),
      (ud.tile_uv_shift(1001), ud.tile_uv_shift(1033)))

print("\n=== 阶段 5d：真机烘 UDIM ===")
# 对照组：同一个场景、同一组物体，先按**普通**方式烘一张，确认烘焙链路本身是通的。
# 没有这个对照，UDIM 出黑图时无从判断是 UDIM 的问题还是场景/手术的问题。
control_tx = txmod.SceneTransaction(bpy.context)
control_backend = bk.CyclesBackend(control_tx, margin=2, samples=1)
control_job = jb.BakeJob(bpy.context, pl.build_plan(
    bpy.context, pl.BakeSettings(
        maps=pl.map_requests_from_pairs([("shader.base_color", 64)]))),
    pl.BakeSettings(maps=pl.map_requests_from_pairs([("shader.base_color", 64)])),
    backend=control_backend, transaction=control_tx)
control_job.run_to_completion()
control_image = control_job.plan.tasks[0].image
control_center = [round(v, 3) for v in list(
    control_image.pixels[4 * (64 * 32 + 32):][:4])]
print("  对照组（非 UDIM）中心像素:", control_center,
      " 状态:", control_job.report.summary())
check("对照组能烘出红色（烘焙链路本身是通的）",
      control_center[0] > 0.5, control_center)

tx = txmod.SceneTransaction(bpy.context)
backend = bk.CyclesBackend(tx, margin=2, samples=1,
                           save_directory=OUT, save_files=True)
job = jb.BakeJob(bpy.context, uplan, usession.settings_to_bake_settings(settings),
                 backend=backend, transaction=tx)
job.run_to_completion()
print("  报告:", job.report.summary())
check("UDIM 四个瓦片任务全部成功",
      job.report.done == 4 and job.report.failed == 0,
      (job.report.done, job.report.failed,
       [r.error for r in job.report.failed_results()]))
# UV 必须被精确还原（平移只是烘焙期间的临时手段）
expected = {
    "Single": [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)],
    "Wide": [(0.0, 0.0), (2.0, 0.0), (2.0, 1.0), (0.0, 1.0)],
    "Tall": [(0.0, 0.0), (1.0, 0.0), (1.0, 3.0), (0.0, 3.0)],
}
mismatched = {}
for name, uvs in expected.items():
    obj = bpy.data.objects.get(name)
    layer = obj.data.uv_layers.get("UVMap") if obj is not None else None
    if layer is None:
        mismatched[name] = "layer gone"
        continue
    actual = [(round(item.uv[0], 6), round(item.uv[1], 6)) for item in layer.data]
    if actual != uvs:
        mismatched[name] = actual
check("烘完 UV 被精确还原（没有留下平移）", not mismatched, mismatched)
tile_paths = sorted(glob.glob(os.path.join(OUT, "**", "*.png"), recursive=True))
files = [os.path.basename(p) for p in tile_paths]
print("  瓦片文件:", files)
check("四个瓦片各出一个文件", len(files) == 4, files)
check("文件名带瓦片号",
      all(any(str(t) in f for t in (1001, 1002, 1011, 1021)) for f in files), files)
check("任务记录了导出的瓦片",
      all(len(t.exported_tiles) == 1 and t.exported_tiles[0][0] == t.tile
          for t in uplan.tasks),
      [(t.tile, t.exported_tiles) for t in uplan.tasks])

# 每一张瓦片都真的烘出了红色（四个瓦片分别由 Single / Wide / Tall 覆盖）
# ⚠ 文件现在落在"以集合命名"的子文件夹里，所以这里用完整路径而不是拼 OUT+文件名
for tile in (1001, 1002, 1011, 1021):
    name = [p for p in tile_paths if ".{}.".format(tile) in os.path.basename(p)]
    if not name:
        check("瓦片 {} 有文件".format(tile), False, files)
        continue
    loaded = bpy.data.images.load(name[0])
    width, height = loaded.size
    center = list(loaded.pixels[((height // 2) * width + width // 2) * 4:][:4])
    check("瓦片 {} 中心真的烘出了红色".format(tile),
          center[0] > 0.5 and center[1] < 0.2,
          [round(v, 3) for v in center])
    bpy.data.images.remove(loaded)

print("\n=== 阶段 8a：预设系统 ===")
check("预设目录可用", os.path.isdir(pr.presets_directory()))
check("非法名字被拒", not pr.validate_name("")[0] and not pr.validate_name("a/b")[0])
check("合法名字通过", pr.validate_name("My Preset 1")[0])

data = pr.capture(
    [{"type": "shader.base_color", "size": 2048, "enabled": True, "options": {}},
     {"type": "shader.normal", "size": 1024, "enabled": False, "options": {}}],
    prefix="", bridge=" - ", suffix="px", template="{prefix}{type}{bridge}{size}{suffix}",
    file_format='PNG', margin=16, antialias='OFF', aa_scale=2, udim=False,
    prepare_uv=False)
check("capture 收到两个烘焙项", len(data["maps"]) == 2, data["maps"])
check("capture 不含输出路径", "output_dir" not in data, sorted(data))
check("capture 不含集合勾选", "groups" not in data and "single_collection" not in data)

path = pr.save_preset("UnitTest", data)
check("预设写盘成功", os.path.isfile(path), path)
check("预设出现在列表里", "UnitTest" in pr.list_presets(), pr.list_presets())
check("列表说明可读", "2 map" in pr.describe("UnitTest"), pr.describe("UnitTest"))

loaded, notes = pr.load_preset("UnitTest")
check("读回来的数据一致", loaded["maps"] == data["maps"], loaded["maps"])
check("带版本号", loaded["version"] == pr.PRESET_VERSION, loaded.get("version"))
check("带格式标记", loaded["format"] == pr.FORMAT_TAG)
check("v1 不需要迁移", notes == [], notes)

# v0 -> v1：原版那种人类可读的类型名
with open(pr.preset_path("Legacy"), "w", encoding="utf-8") as handle:
    json.dump({"maps": [{"type": "Base Color", "size": 512},
                        {"type": "Roughness ", "size": 512},
                        {"type": "No Such Map", "size": 512}]}, handle)
legacy, notes = pr.load_preset("Legacy")
print("  迁移说明:", notes)
check("v0 的显示名被翻译成稳定 key",
      [m["type"] for m in legacy["maps"]] == ["shader.base_color", "standard.roughness"],
      legacy["maps"])
check("带尾随空格的旧名按原版语义映射到标准 pass",
      bt.from_original_label("Roughness ") == "standard.roughness"
      and bt.from_original_label("Roughness") == "shader.roughness",
      (bt.from_original_label("Roughness "), bt.from_original_label("Roughness")))
check("认不出的类型被丢掉并记账",
      any("No Such Map" in n for n in notes), notes)
check("v0 被补齐了缺失字段",
      legacy.get("antialias") == 'OFF' and legacy.get("udim") is False, legacy)

# 未来版本要拒绝
with open(pr.preset_path("Future"), "w", encoding="utf-8") as handle:
    json.dump({"format": pr.FORMAT_TAG, "version": 99, "maps": []}, handle)
try:
    pr.load_preset("Future")
    check("拒绝未来版本的预设", False)
except pr.PresetError as exc:
    check("拒绝未来版本的预设", "newer" in str(exc), str(exc))

try:
    pr.load_preset("NoSuchPreset")
    check("不存在的预设报错", False)
except pr.PresetError:
    check("不存在的预设报错", True)

check("删除预设", pr.delete_preset("UnitTest") and "UnitTest" not in pr.list_presets())
check("删不存在的返回假", pr.delete_preset("UnitTest") is False)

print("\n=== 阶段 8b：预设接上 UI ===")
bpy.ops.wm.read_factory_settings(use_empty=True)
settings = bpy.context.scene.mbakery
uops.add_map(settings, "shader.base_color", 512, dedupe=True)
uops.add_map(settings, "shader.roughness", 512, dedupe=True)
settings.prefix = "Saved"
settings.margin = 32
settings.antialias = 'DOWNSCALE'
settings.aa_scale = 3
settings.output_dir = OUT_B
settings.groups.clear()

saved = upre.capture(settings)
pr.save_preset("RoundTrip", saved)

settings.prefix = ""
settings.margin = 16
settings.antialias = 'OFF'
settings.maps.clear()
settings.prepare_uv = True
restored, _notes = pr.load_preset("RoundTrip")
message = upre.apply_to(settings, restored)
print("  apply_to:", message)
check("UI 往返：烘焙项恢复", len(settings.maps) == 2, len(settings.maps))
check("UI 往返：类型正确",
      [m.type_key for m in settings.maps] == ["shader.base_color", "shader.roughness"],
      [m.type_key for m in settings.maps])
check("UI 往返：前缀恢复", settings.prefix == "Saved", settings.prefix)
check("UI 往返：边距恢复", settings.margin == 32, settings.margin)
check("UI 往返：抗锯齿恢复",
      settings.antialias == 'DOWNSCALE' and settings.aa_scale == 3,
      (settings.antialias, settings.aa_scale))
check("UI 往返：输出路径保持不变（预设不该覆盖它）",
      settings.output_dir == OUT_B, settings.output_dir)
check("UI 往返：Prepare UV 被预设覆盖回 False",
      settings.prepare_uv is False, settings.prepare_uv)

result = op(bpy.ops.mbakery.load_preset, name="RoundTrip")
check("Load Preset operator 可用", result == {'FINISHED'}, result)
result = op(bpy.ops.mbakery.save_preset, name="FromOperator")
check("Save Preset operator 可用", result == {'FINISHED'}, result)
check("operator 存的预设能读回来", "FromOperator" in pr.list_presets())
result = op(bpy.ops.mbakery.delete_preset, name="FromOperator")
check("Delete Preset operator 可用", result == {'FINISHED'}, result)
check("operator 删掉了预设", "FromOperator" not in pr.list_presets())

print("\n=== 阶段 8c：暂停 / 继续 / 取消 ===")
bpy.ops.wm.read_factory_settings(use_empty=True)
for i in range(3):
    quad("P{}".format(i))
col = bpy.data.collections.new("Body")
bpy.context.scene.collection.children.link(col)
for i in range(3):
    obj = bpy.data.objects["P{}".format(i)]
    bpy.context.scene.collection.objects.unlink(obj)
    col.objects.link(obj)

settings = bpy.context.scene.mbakery
settings.maps.clear()
for key in ("shader.base_color", "shader.roughness", "shader.normal"):
    uops.add_map(settings, key, 64, dedupe=True)

bake_settings = usession.settings_to_bake_settings(settings)
plan = pl.build_plan(bpy.context, bake_settings)
check("计划里有 3 个任务", len(plan.tasks) == 3, len(plan.tasks))

paused_job = jb.BakeJob(bpy.context, plan, bake_settings, backend=bk.NullBackend())
paused_job.start()
paused_job.step()                       # 只做一个任务（准备 + 派发已经交错在一起）
# ⚠ 进度现在把"准备"也算进总工作量，而且 NullBackend 是同步的，
#   所以这一步之后进度就已经是 2（1 份准备 + 1 张烘好）而不是 0。
#   这里要验的是**暂停之后不再增长**，不是"必须是 0"。
#   所以基准要取"按下暂停那一刻"的进度，而不是 prepare_index。
frozen = paused_job.progress[0]
paused_job.pause()
paused_job.step()
print("  暂停后状态:", paused_job.state, " 进度:", paused_job.progress)
check("暂停后状态为 PAUSED", paused_job.state == jb.STATE_PAUSED, paused_job.state)
check("暂停后不再推进进度", paused_job.progress[0] == frozen,
      (frozen, paused_job.progress))
check("暂停时 is_running 仍为真（还没结束）", paused_job.is_running)
check("暂停时 is_finished 为假", not paused_job.is_finished)
paused_job.step()
check("继续暂停仍然不动", paused_job.progress[0] == frozen, paused_job.progress)

paused_job.resume()
check("恢复后状态回到 BAKING", paused_job.state == jb.STATE_BAKING, paused_job.state)
paused_job.run_to_completion()
check("恢复后能跑完", paused_job.report.done == 3, paused_job.report.done)

# 暂停后取消要能正常收尾
plan2 = pl.build_plan(bpy.context, bake_settings)
job2 = jb.BakeJob(bpy.context, plan2, bake_settings, backend=bk.NullBackend())
job2.start()
job2.step()
job2.pause()
job2.step()
job2.cancel()
job2.step()
check("暂停状态下取消能生效", job2.state == jb.STATE_CANCELLED, job2.state)
check("取消的报告被标记", job2.report.cancelled)

print("\n=== 阶段 8d：抗锯齿 ===")
check("OFF 时按原尺寸烘",
      pl.render_dims(512, 512, pl.BakeSettings(antialias='OFF', aa_scale=2)) == (512, 512))
check("DOWNSCALE 时按 size×N 烘",
      pl.render_dims(512, 512, pl.BakeSettings(antialias='DOWNSCALE', aa_scale=2)) == (1024, 1024))
check("DOWNSCALE×3",
      pl.render_dims(512, 512, pl.BakeSettings(antialias='DOWNSCALE', aa_scale=3)) == (1536, 1536))
check("UPSCALE 时仍按原尺寸烘",
      pl.render_dims(512, 512, pl.BakeSettings(antialias='UPSCALE', aa_scale=2)) == (512, 512))

aa_settings = pl.BakeSettings(
    maps=pl.map_requests_from_pairs([("shader.base_color", 64)]),
    antialias='DOWNSCALE', aa_scale=2)
aa_plan = pl.build_plan(bpy.context, aa_settings)
check("超采样任务记录了两个尺寸",
      aa_plan.tasks[0].size_x == 64 and aa_plan.tasks[0].size_y == 64
      and aa_plan.tasks[0].render_size_x == 128
      and aa_plan.tasks[0].render_size_y == 128,
      (aa_plan.tasks[0].size_x, aa_plan.tasks[0].render_size_x))
aa_job = jb.BakeJob(bpy.context, aa_plan, aa_settings, backend=bk.NullBackend())
aa_job.run_to_completion()
aa_image = aa_plan.tasks[0].image
check("超采样后图像缩回了交付尺寸",
      tuple(aa_image.size) == (64, 64), tuple(aa_image.size))
check("超采样任务成功", aa_job.report.done == 1, aa_job.report.done)

up_settings = pl.BakeSettings(
    maps=pl.map_requests_from_pairs([("shader.base_color", 64)]),
    antialias='UPSCALE', aa_scale=2)
up_plan = pl.build_plan(bpy.context, up_settings)
up_job = jb.BakeJob(bpy.context, up_plan, up_settings, backend=bk.NullBackend())
up_job.run_to_completion()
check("放大模式把图放大到 size×N",
      tuple(up_plan.tasks[0].image.size) == (128, 128),
      tuple(up_plan.tasks[0].image.size))
check("放大模式会给出提示",
      any("upscaled" in w for w in up_job.report.warnings), up_job.report.warnings)

print("\n=== 阶段 8e：自适应边距 ===")
check("关闭时就是基础边距",
      pl.effective_margin(4096, 4096, pl.BakeSettings(margin=16, adaptive_margin=False)) == 16)
check("打开时 1K 与基础值取大",
      pl.effective_margin(1024, 1024, pl.BakeSettings(margin=16, adaptive_margin=True)) == 16,
      pl.effective_margin(1024, 1024, pl.BakeSettings(margin=16, adaptive_margin=True)))
check("打开时 4K 会放大边距",
      pl.effective_margin(4096, 4096, pl.BakeSettings(margin=16, adaptive_margin=True)) == 33,
      pl.effective_margin(4096, 4096, pl.BakeSettings(margin=16, adaptive_margin=True)))
check("边距有上限（不超过 64）",
      pl.effective_margin(16384, 16384, pl.BakeSettings(margin=16, adaptive_margin=True)) == 64,
      pl.effective_margin(16384, 16384, pl.BakeSettings(margin=16, adaptive_margin=True)))
check("基础边距很大时不会被自适应改小",
      pl.effective_margin(1024, 1024, pl.BakeSettings(margin=48, adaptive_margin=True)) == 48,
      pl.effective_margin(1024, 1024, pl.BakeSettings(margin=48, adaptive_margin=True)))

print("\n=== 阶段 7/8f：报告存取 + 对照 ===")
report = rp.RunReport("unit")
report.start()
report.add(rp.TaskResult(group="Body", type_key="shader.base_color",
                         type_label="Base Color", size_x=64, size_y=64,
                         image_name="BodyBaseColor - 64px", status=rp.STATUS_DONE))
report.add(rp.TaskResult(group="Body", type_key="shader.normal", type_label="Normal",
                         size_x=64, size_y=64, image_name="BodyNormal - 64px",
                         status=rp.STATUS_FAILED, error="boom"))
report.planned = 2
report.stop()

report_path = os.path.join(OUT, "report.json")
report.save_to(report_path)
check("报告写盘成功", os.path.isfile(report_path))
check("没有留下 .tmp 临时文件",
      not os.path.isfile(report_path + ".tmp"))
back = rp.RunReport.load_from(report_path)
check("报告读回来任务数一致", back.total == 2, back.total)
check("报告读回来成功/失败数一致", (back.done, back.failed) == (1, 1),
      (back.done, back.failed))
check("报告读回来错误信息还在",
      back.failed_results()[0].error == "boom", back.failed_results()[0].error)
check("报告读回来 planned 还在", back.planned == 2, back.planned)
check("摘要里含计划总数", "1 of 2" in back.summary(), back.summary())

result = op(bpy.ops.mbakery.save_report)
check("Save Report 没烘过时会被拒", result == {'CANCELLED'}, result)

# 造两张一样的图做对照
from material_bakery.core import compare as cmp_mod
image_a = bpy.data.images.new("Same", 32, 32, alpha=False)
image_a.pixels.foreach_set([0.25, 0.5, 0.75, 1.0] * (32 * 32))
image_a.filepath_raw = os.path.join(OUT_A, "Same.png")
image_a.file_format = 'PNG'
image_a.save()
image_b = bpy.data.images.new("Same2", 32, 32, alpha=False)
image_b.pixels.foreach_set([0.25, 0.5, 0.75, 1.0] * (32 * 32))
image_b.filepath_raw = os.path.join(OUT_B, "Same.png")
image_b.file_format = 'PNG'
image_b.save()
image_c = bpy.data.images.new("Diff", 32, 32, alpha=False)
image_c.pixels.foreach_set([0.9, 0.5, 0.75, 1.0] * (32 * 32))
image_c.filepath_raw = os.path.join(OUT_B, "Diff.png")
image_c.file_format = 'PNG'
image_c.save()
image_d = bpy.data.images.new("DiffRef", 32, 32, alpha=False)
image_d.pixels.foreach_set([0.25, 0.5, 0.75, 1.0] * (32 * 32))
image_d.filepath_raw = os.path.join(OUT_A, "Diff.png")
image_d.file_format = 'PNG'
image_d.save()

same = cmp_mod.compare_image_files("Same", os.path.join(OUT_A, "Same.png"),
                                  os.path.join(OUT_B, "Same.png"))
print("  " + same.summary())
check("完全相同的图判定为一致", same.matches(), same.summary())
check("最大差为 0", same.max_delta == 0.0, same.max_delta)

diff = cmp_mod.compare_image_files("Diff", os.path.join(OUT_A, "Diff.png"),
                                  os.path.join(OUT_B, "Diff.png"))
print("  " + diff.summary())
check("改过颜色的图判定为不一致", not diff.matches(), diff.summary())
check("差异比例是 100%", abs(diff.diff_ratio - 1.0) < 1e-6, diff.diff_ratio)

missing = cmp_mod.compare_image_files("Gone", os.path.join(OUT_A, "Gone.png"),
                                     os.path.join(OUT_B, "Same.png"))
check("缺文件时给出错误而不是崩", bool(missing.error) and not missing.matches(),
      missing.error)

result = cmp_mod.compare_folders("folders", OUT_A, OUT_B)
print("  " + result.summary())
for line in result.details():
    print("   ", line)
check("目录对照找到了同名图", result.compared == 2, result.compared)
check("两边文件集合一致时没有 only-in 项",
      not result.only_in_a and not result.only_in_b,
      (result.only_in_a, result.only_in_b))
check("目录对照判定不通过（Diff 不一致）", not result.ok)
check("不一致的正是 Diff",
      [d.name for d in result.mismatches()] == ["Diff"],
      [d.name for d in result.mismatches()])
check("摘要可读", "compared" in result.summary(), result.summary())

# 只在一侧出现的文件要被点名
os.remove(os.path.join(OUT_B, "Diff.png"))
extra = cmp_mod.compare_folders("extra", OUT_A, OUT_B)
check("只在一侧出现的文件被记为 only_in_a", extra.only_in_a == ["Diff"],
      (extra.only_in_a, extra.only_in_b))
check("缺文件时整体判定不通过", not extra.ok)

print("\n=== 阶段 8g：静态审计 ===")
check("预设模块不引用 PropertyGroup",
      "bpy.context.scene.mbakery" not in
      open(os.path.join(WS, "material_bakery", "core", "presets.py"),
           encoding="utf-8").read())
source_presets = open(os.path.join(WS, "material_bakery", "core", "presets.py"),
                      encoding="utf-8").read()
check("预设里没有存输出路径的字段",
      "output_dir" not in pr.FIELDS, pr.FIELDS)

panel_ops = set()
panel_source = open(os.path.join(WS, "material_bakery", "ui", "panels.py"),
                    encoding="utf-8").read()
for node in ast.walk(ast.parse(panel_source)):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "operator" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                    and "." in arg.value:
                panel_ops.add(arg.value)

declared = set()
# ⚠ 用**通配**发现 operator 文件，不要写死文件名清单。
#   写死的话每次新增一个 ops_*.py 都要回来改测试（已经改了两次了），
#   忘了改就会得到"panel 引用的 operator 没有声明"这种误导性的失败。
_ui_dir = os.path.join(WS, "material_bakery", "ui")
for path in sorted(os.listdir(_ui_dir)):
    if not (path.startswith("ops") and path.endswith(".py")):
        continue
    source = open(os.path.join(_ui_dir, path), encoding="utf-8").read()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "bl_idname":
                    if isinstance(node.value, ast.Constant):
                        declared.add(node.value.value)
print("  扫描到的 operator 文件里的 bl_idname 数量:", len(declared))
missing = sorted(name for name in panel_ops if name not in declared)
check("panel 引用的 operator 全都有声明", not missing, missing)
missing_ops = [name for name in sorted(panel_ops)
               if name.split(".")[1] not in dir(getattr(bpy.ops, name.split(".")[0]))]
check("panel 引用的 operator 全都注册了", not missing_ops, missing_ops)
check("bl_idname 唯一", len(declared) == len(set(declared)))

print("\n=== 阶段 8h：注销 ===")
material_bakery.unregister()
check("注销成功", not hasattr(bpy.context.scene, "mbakery"))
material_bakery.register()
check("能重新注册", hasattr(bpy.context.scene, "mbakery"))

print("\n=== 结果 ===")
if FAIL:
    print("FAILED {} check(s):".format(len(FAIL)))
    for name in FAIL:
        print("  -", name)
else:
    print("ALL CHECKS PASSED")

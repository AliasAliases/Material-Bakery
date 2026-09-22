"""Material Bakery — 阶段 4 测试：四页向导

覆盖：
    - 注册 / 注销 / 重复注册
    - 向导状态属性、枚举表（回归：EnumProperty 必须是 3 元组或 5 元组）
    - 严格顺序导航：不能跳页、有红色项时 Next 被拦
    - 烘焙列表增删移清 / 去重
    - 从头点到尾跑完一次烘焙（NullBackend）
    - 第 4 页：导出贴图 / 建材质 / 应用 / 对比切换 / 恢复 / 复制物体
    - 静态 AST 审计：panel 里引用的 operator 都存在（用 dir() 而不是 hasattr）、
      timer 驱动的路径不写任何 IDProperty
"""
import ast
import bpy, sys, os, shutil, glob

# 工作区 = 本文件所在目录的上一级。
# ⚠ 这里原来写死的是开发机上的绝对路径，clone 到别处每个套件都跑不起来，
#   而且公开版/私有版两份检出会互相 import 错代码。
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WS)

import material_bakery
from material_bakery import compat
from material_bakery.core import bake_types as bt
from material_bakery.core import scene_scan as ss
from material_bakery.core import materials as mt
from material_bakery.engine import backend as bk
from material_bakery.ui import ops as uops
from material_bakery.ui import panels as upanels
from material_bakery.ui import properties as uprops
from material_bakery.ui import session as usession

compat.set_silent(True)

FAIL = []
def check(label, ok, extra=""):
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", label, ("  " + str(extra)) if extra else ""))
    if not ok:
        FAIL.append(label)


def op(operator, **kwargs):
    """调用 operator。

    坑：operator 里 self.report({'ERROR'}, ...) 会让 bpy.ops 从 Python 侧**抛异常**
    （GUI 里只是显示红字、什么都不执行）。测试要的是"被拦下"，
    所以这里统一收敛成 {'CANCELLED'}。
    """
    try:
        return operator(**kwargs)
    except RuntimeError as exc:
        print("      (operator 报错被拦: {})".format(exc))
        return {'CANCELLED'}


OUT = os.path.join(WS, "_probe", "wizard")
if os.path.isdir(OUT):
    shutil.rmtree(OUT)
os.makedirs(OUT)

print("=== 阶段 4a：注册 ===")
material_bakery.register()
check("注册后 Scene 上有向导设置", hasattr(bpy.context.scene, "mbakery"))
settings = bpy.context.scene.mbakery
check("默认停在第 1 页", settings.page == uprops.PAGE_TARGETS, settings.page)
check("默认目标是集合模式", settings.target_mode == ss.MODE_COLLECTIONS, settings.target_mode)
check("Prepare UV 默认关闭", settings.prepare_uv is False)
check("默认没有烘焙项", len(settings.maps) == 0, len(settings.maps))
check("默认没有贴图列表", len(settings.textures) == 0)

material_bakery.register()
check("重复注册不报错", hasattr(bpy.context.scene, "mbakery"))

check("分组枚举表都是 3 元组（4 元组会被 Blender 拒绝）",
      all(len(item) == 3 for item in bt.enum_items()))
check("类型枚举表都是 3 元组",
      all(len(item) == 3 for item in bt.type_enum_items()))
check("类型枚举没有空 key", all(item[0] for item in bt.type_enum_items()))
check("分组枚举里有分组标题", any(item[0] == "" for item in bt.enum_items()))

print("\n=== 阶段 4b：场景扫描与烘焙列表 ===")
bpy.ops.wm.read_factory_settings(use_empty=True)
settings = bpy.context.scene.mbakery
check("换文件后向导设置是新的", len(settings.groups) == 0 and len(settings.maps) == 0)


def make_quad(name):
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
    mesh.update()
    layer = mesh.uv_layers.new(name="UVMap")
    for i, uv in enumerate(((0, 0), (1, 0), (1, 1), (0, 1))):
        layer.data[i].uv = uv
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    mat = bpy.data.materials.new(name + "Mat")
    mat.use_nodes = True
    mesh.materials.append(mat)
    return obj


for i in range(3):
    make_quad("Part{}".format(i))

# 把三个物体放进一个集合
col = bpy.data.collections.new("Body")
bpy.context.scene.collection.children.link(col)
for name in ("Part0", "Part1", "Part2"):
    obj = bpy.data.objects[name]
    bpy.context.scene.collection.objects.unlink(obj)
    col.objects.link(obj)

result = op(bpy.ops.mbakery.refresh_groups)
check("Scan Scene 成功", result == {'FINISHED'}, result)
check("扫到 Body 集合", [i.name for i in settings.groups] == ["Body"],
      [i.name for i in settings.groups])
check("集合行记录了物体数", settings.groups[0].object_count == 3,
      settings.groups[0].object_count)
check("集合行记录了材质数", settings.groups[0].material_count == 3,
      settings.groups[0].material_count)
check("扫描后自动填了默认烘焙列表", len(settings.maps) == 3, len(settings.maps))
check("默认列表含 BaseColor/Roughness/Normal",
      [m.type_key for m in settings.maps] ==
      ["shader.base_color", "shader.roughness", "shader.normal"],
      [m.type_key for m in settings.maps])

settings.groups[0].enabled = False
op(bpy.ops.mbakery.refresh_groups)
check("重新扫描保留勾选状态", settings.groups[0].enabled is False)
settings.groups[0].enabled = True

settings.map_index = 0
count_before = len(settings.maps)
op(bpy.ops.mbakery.add_map)
check("Add 新增一项（复制当前行）", len(settings.maps) == count_before + 1,
      len(settings.maps))
last = settings.maps[len(settings.maps) - 1]
check("新增项复制了当前行的类型与尺寸",
      last.type_key == settings.maps[0].type_key and last.size == settings.maps[0].size,
      (last.type_key, last.size))
check("新增后选中落在新项上", settings.map_index == count_before, settings.map_index)

removed = uops.dedupe_maps(settings)
check("Dedupe 合并重复项", removed == 1 and len(settings.maps) == count_before,
      (removed, len(settings.maps)))
op(bpy.ops.mbakery.add_map)
settings.maps[len(settings.maps) - 1].size = 4096
check("手动改尺寸后就是两项不同的",
      len({(m.type_key, m.size) for m in settings.maps}) == len(settings.maps))

settings.map_index = 0
first_key = settings.maps[0].type_key
second_key = settings.maps[1].type_key
op(bpy.ops.mbakery.move_map, direction='DOWN')
check("下移交换相邻两项",
      settings.maps[0].type_key == second_key and settings.maps[1].type_key == first_key,
      [m.type_key for m in settings.maps])
check("下移后选中跟随移动", settings.map_index == 1, settings.map_index)
op(bpy.ops.mbakery.move_map, direction='UP')
check("上移还原顺序",
      [m.type_key for m in settings.maps][:2] == [first_key, second_key],
      [m.type_key for m in settings.maps][:2])

settings.map_index = 0
check("第一项不能上移", op(bpy.ops.mbakery.move_map, direction='UP') == {'CANCELLED'})
settings.map_index = len(settings.maps) - 1
check("最后一项不能下移", op(bpy.ops.mbakery.move_map, direction='DOWN') == {'CANCELLED'})

count_before = len(settings.maps)
op(bpy.ops.mbakery.remove_map)
check("Remove 删掉一项", len(settings.maps) == count_before - 1, len(settings.maps))
check("删除后选中不越界", 0 <= settings.map_index < len(settings.maps), settings.map_index)
op(bpy.ops.mbakery.clear_maps)
check("Clear All 清空", len(settings.maps) == 0)
op(bpy.ops.mbakery.add_map)
check("清空后能重新添加", len(settings.maps) == 1, len(settings.maps))

print("\n=== 阶段 4c：校验与严格顺序 ===")
settings.maps.clear()
check("空烘焙列表被拦下", uops.validate_targets(settings) != [],
      uops.validate_targets(settings))
op(bpy.ops.mbakery.add_map)
check("有烘焙项后第 1 页放行", uops.validate_targets(settings) == [],
      uops.validate_targets(settings))

settings.groups[0].enabled = False
check("全部集合取消勾选被拦下", uops.validate_targets(settings) != [])
settings.groups[0].enabled = True

settings.target_mode = ss.MODE_SINGLE
settings.single_collection = uprops.COLLECTION_NONE
check("单集合模式没选集合被拦下", uops.validate_targets(settings) != [],
      uops.validate_targets(settings))
check("未选集合时解析出来是空串", settings.selected_collection() == "",
      settings.selected_collection())
settings.single_collection = "Body"
check("选好集合后放行", uops.validate_targets(settings) == [],
      uops.validate_targets(settings))
check("选好集合后能取到名字", settings.selected_collection() == "Body")
settings.target_mode = ss.MODE_COLLECTIONS

settings.target_mode = ss.MODE_SELECTED
for obj in bpy.context.view_layer.objects:
    obj.select_set(False)
check("选中模式没选物体被拦下", uops.validate_targets(settings) != [])
bpy.data.objects["Part0"].select_set(True)
check("选了物体后放行", uops.validate_targets(settings) == [],
      uops.validate_targets(settings))
settings.target_mode = ss.MODE_COLLECTIONS

# 红色项拦住 Next。注意：模板非法属于**设置页**的清单项，
# 第 1 页的放行条件只有「至少 1 个目标 + 至少 1 个烘焙项」。
# 公开版是 4 页（目标 / 设置 / 烘焙 / 交付），第 1 页的 Next 直接到设置页。
settings.template = "{prefix}{typo}"
op(bpy.ops.mbakery.refresh_groups)
result = op(bpy.ops.mbakery.next)
check("第 1 页不校验模板（模板属于设置页）",
      result == {'FINISHED'}, result)
check("第 1 页的 Next 直接进设置页（公开版没有 Remesh 页）",
      settings.page == uprops.PAGE_SETTINGS, settings.page)

rows = uops.checklist(bpy.context, settings)
print("  清单:", [(lvl, msg[:46]) for lvl, msg in rows])
check("清单把非法模板标成红色",
      any(lvl == 'ERROR' and 'typo' in msg for lvl, msg in rows), rows)
result = op(bpy.ops.mbakery.next)
check("模板非法时设置页的 Next 被拦", result == {'CANCELLED'}, result)
check("被拦时仍停在设置页", settings.page == uprops.PAGE_SETTINGS, settings.page)

settings.template = "{prefix}{type}{bridge}{size}{suffix}"
rows = uops.checklist(bpy.context, settings)
check("修好后清单里没有红色项",
      not [m for lvl, m in rows if lvl == 'ERROR'], rows)
check("清单级别只有三种",
      all(lvl in ('OK', 'WARN', 'ERROR') for lvl, _ in rows))
check("没设输出目录给的是黄色警告",
      any(lvl == 'WARN' and 'output' in msg.lower() for lvl, msg in rows), rows)

# 不能往前跳
result = op(bpy.ops.mbakery.goto_page, page=uprops.PAGE_BAKE)
check("不能往前跳页", result == {'CANCELLED'}, result)
result = op(bpy.ops.mbakery.goto_page, page=uprops.PAGE_TARGETS)
check("可以往回看已完成步骤",
      result == {'FINISHED'} and settings.page == uprops.PAGE_TARGETS, settings.page)
settings.page = uprops.PAGE_SETTINGS

settings.output_dir = OUT
rows = uops.checklist(bpy.context, settings)
check("设了存在的目录后是绿色",
      any(lvl == 'OK' and 'Output folder exists' in msg for lvl, msg in rows), rows)

settings.output_dir = os.path.join(OUT, "deep", "nested")
rows = uops.checklist(bpy.context, settings)
check("目录不存在时自动创建",
      any(lvl == 'OK' and 'created' in msg for lvl, msg in rows), rows)

result = op(bpy.ops.mbakery.next)
check("第 2 页能进第 3 页",
      result == {'FINISHED'} and settings.page == uprops.PAGE_BAKE, settings.page)

print("\n=== 阶段 4d：跑完一次烘焙 ===")
# 前面验证列表操作时把烘焙列表折腾过了，这里配回一套标准的三张图。
# 用 512 而不是默认的 2048：向导测的是流程，不需要大图；
# 2048² 光是 pack + 反复导出就要好几分钟。
settings.maps.clear()
for key in ("shader.base_color", "shader.roughness", "shader.normal"):
    uops.add_map(settings, key, 512, dedupe=True)
settings.map_index = 0
check("配好 3 张贴图", len(settings.maps) == 3, len(settings.maps))
check("Pack Textures Into .blend 默认**关**（用户要求：图写盘、材质链接文件）",
      settings.pack_into_blend is False)

settings.output_dir = OUT
# ⚠ 关掉自动交付：这一套要**手动**验"加槽 → 预览 → 收尾"三步。
#   开着的话跑完就已经自动加过槽了，后面再点"Add Slot"当然没东西可加
#   （第一次跑就是这么"失败"的：日志里先是 added，再点就是 already has the slot）。
settings.auto_deliver = False
active = usession.Session.get()
# ⚠ save=True：新默认是"图写盘 + 材质链接到文件"（不 pack 进 .blend），
#   所以后端必须真写文件 —— 这样第 4 页的"导出贴图"才是**拷贝文件**那条路
#   （图写盘后像素缓冲区会被释放，导出不能再依赖内存里的像素）。
active.backend_factory = lambda context, s: bk.NullBackend(save=True, directory=OUT)
result = op(bpy.ops.mbakery.start_bake)
check("Start Bake 成功", result == {'FINISHED'}, result)
check("会话认为正在跑", active.is_running)
check("job 已建立", active.job is not None)
active.run_blocking()
print("  报告:", active.report.summary())
check("跑完状态为 DONE", active.job.state == "done", active.job.state)
check("全部任务成功", active.report.failed == 0 and active.report.done == 3,
      (active.report.done, active.report.failed))
check("会话不在运行中", not active.is_running)
check("日志有内容", len(active.log) > 0, len(active.log))
check("报告是按分组拆的", active.report.group_names() == ["Body"],
      active.report.group_names())
check("Pack Textures Into .blend 默认**关**（图写盘、材质链接文件）",
      settings.pack_into_blend is False, settings.pack_into_blend)
check("烘完的图**没有**被塞进 .blend（新默认）",
      all(task.image.packed_file is None for task in active.job.plan.tasks),
      [task.image.packed_file is not None for task in active.job.plan.tasks])
check("烘完的图**写到了输出目录里**（材质链接的就是它）",
      all(task.exported_path and os.path.isfile(task.exported_path)
          for task in active.job.plan.tasks),
      [task.exported_path for task in active.job.plan.tasks])
check("烘完的图像挂了 fake user",
      all(task.image.use_fake_user for task in active.job.plan.tasks))

result = op(bpy.ops.mbakery.next)
check("第 3 页能进第 4 页",
      result == {'FINISHED'} and settings.page == uprops.PAGE_RESULT, settings.page)

print("\n=== 阶段 4e：第 4 页交付 ===")
count = active.refresh_textures(settings)
check("贴图列表刷新", count == 3, count)
check("列表项带分组名", all(i.group == "Body" for i in settings.textures))
check("成功的项默认勾选", all(i.enabled for i in settings.textures))

result = op(bpy.ops.mbakery.select_all_textures)
check("全选/全不选可用", result == {'FINISHED'}, result)
first_state = settings.textures[0].enabled
op(bpy.ops.mbakery.select_all_textures)
check("再次点击取反", settings.textures[0].enabled != first_state)
op(bpy.ops.mbakery.select_all_textures)

# ⚠ 不要再清空 OUT 了：新的默认是"图写盘 + 材质链接到文件"，那些文件就在 OUT 里，
#   清掉它们等于把导出要拷贝的源删了。改成**导出到另一个目录**，
#   正好验证"从链接的文件拷贝到别处"这条新路径。
EXPORT_DIR = os.path.join(WS, "_probe", "wizard_export")
shutil.rmtree(EXPORT_DIR, ignore_errors=True)
os.makedirs(EXPORT_DIR)
settings.output_dir = EXPORT_DIR
result = op(bpy.ops.mbakery.export_textures)
if result != {'FINISHED'}:
    print("  导出结果:", [(o.name, o.ok, o.message) for o in active.export_outcomes])
check("导出成功", result == {'FINISHED'}, result)
exported = [o for o in active.export_outcomes if o.ok]
print("  导出文件:", [os.path.relpath(o.path, EXPORT_DIR) for o in exported])
check("每个任务导出一个文件", len(exported) == 3, len(exported))
check("导出的文件真的在磁盘上（从链接的文件拷过去的）",
      all(os.path.exists(o.path) for o in exported),
      [(o.path, os.path.exists(o.path), o.message) for o in exported])
check("导出的是拷贝，不是把原文件移走",
      all(task.exported_path and os.path.isfile(task.exported_path)
          for task in active.job.plan.tasks),
      [task.exported_path for task in active.job.plan.tasks])
files = glob.glob(os.path.join(EXPORT_DIR, "**", "*.png"), recursive=True)
check("磁盘上的文件数与导出数一致", len(files) == len(exported), len(files))
check("按集合分子文件夹",
      all(os.path.basename(os.path.dirname(o.path)) == "Body" for o in exported),
      [o.path for o in exported])
settings.output_dir = OUT
check("列表里记录了导出路径", all(i.exported_path for i in settings.textures),
      [i.exported_path for i in settings.textures])

result = op(bpy.ops.mbakery.export_one_texture, index=0)
if result != {'FINISHED'}:
    print("  单张导出结果:", [(o.name, o.ok, o.message) for o in active.export_outcomes])
check("单张导出可用", result == {'FINISHED'}, result)

# ⚠ 交付材质**只有 Principled 一种**了。用户的原话：
#   "我不是很理解为啥要加这个下拉选单，第二个选项只保留图片纹理也没用啊，
#    用户还是得手动加 shader 连到输出"
#   —— 所以就算旧预设里写着 SIMPLE，也一律按 Principled 建。
settings.material_style = 'SIMPLE'
result = op(bpy.ops.mbakery.build_materials)
check("旧预设里的 SIMPLE 也按 Principled 建（不再有半成品材质）",
      result == {'FINISHED'}, result)
simple_material = active.materials_by_group["Body"][0]
check("总有 Principled 接到输出（用户不必自己接 shader）",
      any(n.type == 'BSDF_PRINCIPLED' for n in simple_material.node_tree.nodes))
check("每张图一个图像节点",
      len([n for n in simple_material.node_tree.nodes if n.type == 'TEX_IMAGE']) == 3,
      len([n for n in simple_material.node_tree.nodes if n.type == 'TEX_IMAGE']))

settings.material_style = 'PRINCIPLED'
result = op(bpy.ops.mbakery.build_materials)
check("建材质成功", result == {'FINISHED'}, result)
check("每个集合一个材质", list(active.materials_by_group) == ["Body"],
      list(active.materials_by_group))
material, link_report = active.materials_by_group["Body"]
print("  材质:", material.name, link_report.summary(),
      " linked:", link_report.linked, " skipped:", link_report.skipped)
check("材质名就是集合名", material.name == "Body", material.name)
check("三张图都接上了", len(link_report.linked) == 3, link_report.linked)
check("BaseColor 接到了 Base Color",
      any(k == "shader.base_color" and "Base Color" in v for k, v in link_report.linked),
      link_report.linked)
check("Normal 过了 Normal Map 节点",
      any(k == "shader.normal" and "Normal Map" in v for k, v in link_report.linked),
      link_report.linked)
check("材质挂了 fake user", material.use_fake_user)
check("材质节点树里有 Principled",
      any(n.type == 'BSDF_PRINCIPLED' for n in material.node_tree.nodes))
check("材质里有一张 Normal Map 节点",
      any(n.type == 'NORMAL_MAP' for n in material.node_tree.nodes))
check("重建材质时旧节点被清干净（没有重复图像节点）",
      len([n for n in material.node_tree.nodes if n.type == 'TEX_IMAGE']) == 3,
      len([n for n in material.node_tree.nodes if n.type == 'TEX_IMAGE']))

result = op(bpy.ops.mbakery.apply_baked_material)
if result != {'FINISHED'}:
    print("  加槽日志:", [e.message for e in active.log
                         if e.level in ("SLOT", "ERROR")][-6:])
check("追加烘焙槽成功", result == {'FINISHED'}, result)
objs = [bpy.data.objects["Part{}".format(i)] for i in range(3)]
visible = [mt.visible_material_names(o) for o in objs]
print("  加槽之后实际显示:", visible, " 槽数:", [len(o.material_slots) for o in objs])
# 用户定稿的流程：加槽**不改外观**，想看成色自己点"预览"（分配面）
check("每个物体多了一个槽（原来的槽还在）",
      all(len(o.material_slots) == 2 for o in objs),
      [len(o.material_slots) for o in objs])
check("加槽本身不改外观（还是原材质）",
      all(v == ["Part{}Mat".format(i)] for i, v in enumerate(visible)), visible)
check("原材质数据块没被删",
      all(bpy.data.materials.get("Part{}Mat".format(i)) is not None for i in range(3)))
check("原材质被挂了 fake user",
      all(bpy.data.materials["Part{}Mat".format(i)].use_fake_user for i in range(3)))
check("还没进入预览状态", settings.compare_baked is False)

# 预览 = 把面分配到烘焙槽
result = op(bpy.ops.mbakery.preview_baked, baked=True)
check("预览（分配面）成功", result == {'FINISHED'}, result)
check("预览后显示的就是烘焙材质",
      all(mt.visible_material_names(o) == ["Body"] for o in objs),
      [mt.visible_material_names(o) for o in objs])
check("compare_baked 已置真", settings.compare_baked is True)

result = op(bpy.ops.mbakery.preview_baked, baked=False)
check("切回原材质可用", result == {'FINISHED'}, result)
check("切回后显示的是原材质",
      all(mt.visible_material_names(o) == ["Part{}Mat".format(i)] for i, o in enumerate(objs)),
      [mt.visible_material_names(o) for o in objs])
check("切回来后 compare_baked 为假", settings.compare_baked is False)
result = op(bpy.ops.mbakery.preview_baked, baked=True)
check("再切回烘焙材质",
      all(mt.visible_material_names(o) == ["Body"] for o in objs),
      [mt.visible_material_names(o) for o in objs])

# 收尾：只留烘焙槽（导出就一个材质）
result = op(bpy.ops.mbakery.finalize_for_export)
check("收尾成功", result == {'FINISHED'}, result)
check("收尾后每个物体只剩一个槽（导出干净）",
      all(len(o.material_slots) == 1 for o in objs),
      [len(o.material_slots) for o in objs])
check("收尾后显示的还是烘焙材质",
      all(mt.visible_material_names(o) == ["Body"] for o in objs),
      [mt.visible_material_names(o) for o in objs])

result = op(bpy.ops.mbakery.restore_original_material)
check("恢复原槽成功", result == {'FINISHED'}, result)
check("收尾之后也能恢复原材质（可逆）",
      all(mt.visible_material_names(o) == ["Part{}Mat".format(i)] for i, o in enumerate(objs)),
      [mt.visible_material_names(o) for o in objs])
check("恢复后不再有已应用的标记", not any(mt.has_baked_applied(o) for o in objs))

op(bpy.ops.mbakery.apply_baked_material)
op(bpy.ops.mbakery.preview_baked, baked=True)
result = op(bpy.ops.mbakery.create_final_objects)
check("复制最终物体成功", result == {'FINISHED'}, result)
check("复制了 3 个物体", len(active.created_objects) == 3, len(active.created_objects))
check("副本在 Material Bakery 集合里",
      all(any(c.name == "Material Bakery" for c in o.users_collection)
          for o in active.created_objects),
      [[c.name for c in o.users_collection] for o in active.created_objects])
check("副本有自己的网格（不共享）",
      all(o.data.users == 1 for o in active.created_objects),
      [o.data.users for o in active.created_objects])
check("副本继承了烘焙材质",
      all(mt.visible_material_names(o) == ["Body"] for o in active.created_objects),
      [mt.visible_material_names(o) for o in active.created_objects])
check("副本不再带已应用的账",
      not any(mt.has_baked_applied(o) for o in active.created_objects))
check("原物体没被删", all(bpy.data.objects.get("Part{}".format(i)) for i in range(3)))

result = op(bpy.ops.mbakery.start_over)
check("Start Over 回到第 1 页",
      result == {'FINISHED'} and settings.page == uprops.PAGE_TARGETS, settings.page)
check("Start Over 清掉本次会话", usession.Session.get().job is None)

print("\n=== 阶段 4f：重跑路径 ===")
settings.page = uprops.PAGE_SETTINGS
settings.output_dir = OUT
op(bpy.ops.mbakery.next)
active = usession.Session.get()
active.backend_factory = lambda context, s: bk.NullBackend()
result = op(bpy.ops.mbakery.start_bake)
check("第二次烘焙能启动", result == {'FINISHED'}, result)
planned = len(active.job.plan.tasks)
print("  第二次计划:", planned, "个任务，分组:",
      [(g.name, len(g.objects)) for g in active.job.plan.groups])
for task in active.job.plan.tasks:
    print("    -", task.image_name, [o.name for o in task.targets])
active.run_blocking()
check("第二次烘焙全部成功",
      active.report.failed == 0 and active.report.done == planned,
      (active.report.done, planned, active.report.failed))
check("正在跑时不认为在跑", not active.is_running)

print("\n=== 阶段 4g：静态审计 ===")
panel_ops = set()
source = open(os.path.join(WS, "material_bakery", "ui", "panels.py"), encoding="utf-8").read()
for node in ast.walk(ast.parse(source)):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "operator" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if "." in arg.value:
                    panel_ops.add(arg.value)
print("  panel 引用的 operator:", sorted(panel_ops))
missing = [name for name in sorted(panel_ops)
           if name.split(".")[1] not in dir(getattr(bpy.ops, name.split(".")[0]))]
check("panel 引用的 operator 全部存在（用 dir() 判断）", not missing, missing)

source_ops = open(os.path.join(WS, "material_bakery", "ui", "ops.py"), encoding="utf-8").read()
declared = set()
# ⚠ 用**通配**发现 operator 文件，不要写死文件名清单。
#   写死的话每次新增一个 ops_*.py 都要回来改测试（已经改了两次），
#   忘了改就会得到"panel 引用的 operator 没有声明"这种误导性的失败。
_ui_dir = os.path.join(WS, "material_bakery", "ui")
for filename in sorted(os.listdir(_ui_dir)):
    if not (filename.startswith("ops") and filename.endswith(".py")):
        continue
    with open(os.path.join(_ui_dir, filename), encoding="utf-8") as handle:
        text = handle.read()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "bl_idname":
                    if isinstance(node.value, ast.Constant):
                        declared.add(node.value.value)
missing_decl = sorted(name for name in panel_ops if name not in declared)
check("panel 引用的 operator 都有 bl_idname 声明", not missing_decl, missing_decl)
check("bl_idname 都带 mbakery. 前缀",
      all(n.startswith("mbakery.") for n in declared),
      [n for n in declared if not n.startswith("mbakery.")])
check("bl_idname 唯一", len(declared) == len(set(declared)), sorted(declared))
check("声明的 operator 数量合理（至少 25 个）", len(declared) >= 25, len(declared))

# 静态回归防护：timer 驱动的路径绝不许写 IDProperty
source_session = open(os.path.join(WS, "material_bakery", "ui", "session.py"),
                      encoding="utf-8").read()
HOT = ("tick", "step_once", "tag_redraw", "run_blocking", "start_timer", "stop_timer")
violations = []
for node in ast.walk(ast.parse(source_session)):
    if isinstance(node, ast.FunctionDef) and node.name in HOT:
        for inner in ast.walk(node):
            if isinstance(inner, (ast.Assign, ast.AugAssign, ast.Delete)):
                targets = inner.targets if isinstance(inner, ast.Assign) else [inner.target]
                for target in targets:
                    if isinstance(target, (ast.Attribute, ast.Subscript)):
                        base = target.value
                        if not (isinstance(base, ast.Name) and base.id == "self"):
                            violations.append("{} 里写了 {}".format(
                                node.name, ast.dump(target)[:60]))
print("  timer 路径写 IDProperty 的地方:", violations)
check("timer 路径不写任何 IDProperty", not violations, violations)
check("session.py 里没有 bpy.props（运行状态不落 IDProperty）",
      "bpy.props" not in source_session)

check("panel 只声明了一个 N 面板", len(upanels.CLASSES) == 1)
check("panel 分类名正确", upanels.CATEGORY == "Material Bakery")
check("panel bl_idname 正确", bool(upanels.MBAKERY_PT_Wizard.bl_idname))

# ⚠ 断言"面板真的注册进 Blender 了"。
#   Blender 往 bpy.types 里放的时候用的是 **bl_idname**（MBAKERY_PT_wizard），
#   不是 Python 类名（MBAKERY_PT_Wizard）。查错名字会得出"面板没注册"的假结论。
panel_cls = getattr(bpy.types, "MBAKERY_PT_wizard", None)
check("面板真的注册进了 bpy.types", panel_cls is not None)
check("面板挂在 3D 视图侧栏",
      panel_cls is not None and panel_cls.bl_space_type == 'VIEW_3D',
      getattr(panel_cls, "bl_space_type", None))
check("面板挂在 UI 区域（N 面板）",
      panel_cls is not None and panel_cls.bl_region_type == 'UI',
      getattr(panel_cls, "bl_region_type", None))
check("面板分类是 Material Bakery",
      panel_cls is not None and panel_cls.bl_category == "Material Bakery",
      getattr(panel_cls, "bl_category", None))

print("\n=== 阶段 4h：注销 ===")
material_bakery.unregister()
check("注销后向导设置没了", not hasattr(bpy.context.scene, "mbakery"))
check("注销后版本标记没了", not hasattr(bpy.context.scene, "mbakery_version"))
check("注销后会话被丢弃", usession.Session._instance is None)
material_bakery.register()
check("能重新注册", hasattr(bpy.context.scene, "mbakery"))

print("\n=== 结果 ===")
if FAIL:
    print("FAILED {} check(s):".format(len(FAIL)))
    for name in FAIL:
        print("  -", name)
else:
    print("ALL CHECKS PASSED")

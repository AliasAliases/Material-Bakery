"""Material Bakery — 实施轮 1.2 回归测试

任务书 `_handoff/11_ROUND3_2_BRIEF.md` 的三件事，各一组：

    A 名字与现取      计划里保留**名字**；跨操作符边界按名字现取。
                      用户那条路径（建计划 -> 进编辑模式 -> 出来 -> Preview）
                      不许抛异常；**数据块被换掉**（旧引用变成死引用）之后
                      交付 / 预览 / 收尾 / 建副本照常工作；真没了的物体只记一句
                      "object is gone"，绝不把整页操作炸掉。
    B UV Sets 下拉    两个下拉的 items = `None` + **实际检测到的层名**；
                      旧哨兵（`__same__` / `__auto__`）当 None；stats 的三个标志
                      （nothing / same / no_pin）与显示文案对得上。
    C 法线通道        `shader.normal` 一律走原生 NORMAL pass（手术会把表面换成没有
                      Normal 输入的 Emission，Bump / Normal Map 链随之脱离求值）。
                      静态判据 + **真机三档比对**（带链 / 原生 / 断开链的几何基准），
                      **Bake 与 Re-bake 两条工作流各跑一遍**；`standard.normal` 与之等价。

判据都是打印出来的数字：不硬编码版本号 / 路径 / 计数，断言失败能指出是哪一格。

⚠ 一条踩过的坑：同一个集合 + 同一个类型 + 同一个分辨率 -> **同一个图像名**，
  后一次烘焙会把前一次的像素覆盖掉。所以每次读数都要在**下一次烘焙之前**
  取成快照（`image_stats` 返回值里的 `pixels` 就是快照），绝不回头再读 `task.image`。
"""
import bpy, sys, os, shutil

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WS not in sys.path:
    sys.path.insert(0, WS)

import material_bakery
from material_bakery import compat
from material_bakery.core import bake_types as bt
from material_bakery.core import live as live_mod
from material_bakery.core import scene_scan as ss
from material_bakery.deliver import objects as objects_mod
from material_bakery.deliver import slots as slots_mod
from material_bakery.deliver import uv_layers
from material_bakery.engine import surgery as sg
from material_bakery.engine import uv_sets as uvs
from material_bakery.ui import ops as uops
from material_bakery.ui import properties as uprops
from material_bakery.ui import session as usession

compat.set_silent(True)

FAIL = []
def check(label, ok, extra=""):
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", label,
                               ("  " + str(extra)) if extra else ""))
    if not ok:
        FAIL.append(label)


FLAT = (0.5, 0.5, 1.0)          # 切线空间里"没有扰动"：几何法线 (0,0,1)

OUT = os.path.join(WS, "_probe", "round32")
if os.path.isdir(OUT):
    shutil.rmtree(OUT)
os.makedirs(OUT)

bpy.ops.wm.read_factory_settings(use_empty=True)
material_bakery.register()
scene = bpy.context.scene
settings = scene.mbakery
settings.output_dir = OUT
settings.pack_into_blend = True
settings.margin = 0
settings.antialias = 'OFF'
settings.udim = False
settings.use_subfolders = False
settings.bake_method = 'EMISSION'
settings.auto_deliver = True
# 只烘选中的那几个：场景里有好几块测试平面，走集合模式会全部进计划
settings.target_mode = ss.MODE_SELECTED
settings.rebake_target_mode = ss.MODE_SELECTED


# ------------------------------------------------------------------------------------
#   夹具与工具

def two_layer_plane(name, source, write, offset=0.6, low=0.1, high=0.3):
    """一块平面 + 两层 UV：source 在 low..high，write 是它平移 offset 之后"""
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata([(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)], [], [(0, 1, 2, 3)])
    mesh.update()
    mesh.uv_layers.new(name=source)
    mesh.uv_layers.new(name=write)
    # ⚠ 新建层会让之前拿到的层引用失效（旧引用再迭代会直接崩）
    first = mesh.uv_layers.get(source)
    second = mesh.uv_layers.get(write)
    corners = ((low, low), (high, low), (high, high), (low, high))
    for index, (u, v) in enumerate(corners):
        first.data[index].uv = (u, v)
        second.data[index].uv = (u + offset, v + offset)
    for layer in mesh.uv_layers:
        layer.active_render = (layer.name == source)
    mesh.uv_layers.active_index = mesh.uv_layers.find(source)
    obj = bpy.data.objects.new(name, mesh)
    scene.collection.objects.link(obj)
    return obj


def gradient_image(name, resolution):
    """坐标编码图：R = u, G = v（脚本内合成，不存盘）"""
    image = bpy.data.images.new(name, resolution, resolution, alpha=False,
                                float_buffer=True)
    image.colorspace_settings.name = 'Non-Color'
    pixels = [0.0, 0.0, 0.0, 1.0] * (resolution * resolution)
    for y in range(resolution):
        for x in range(resolution):
            index = (y * resolution + x) * 4
            pixels[index] = (x + 0.5) / resolution
            pixels[index + 1] = (y + 0.5) / resolution
    image.pixels.foreach_set(pixels)
    image.update()
    return image


def textured_material(name, image):
    """Principled + 一张贴图 —— base_color 烘焙的入口"""
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    tree = material.node_tree
    texture = tree.nodes.new('ShaderNodeTexImage')
    texture.image = image
    texture.location = (-400, 0)
    principled = next(n for n in tree.nodes if n.type == 'BSDF_PRINCIPLED')
    tree.links.new(texture.outputs['Color'], principled.inputs['Base Color'])
    specular = principled.inputs.get('Specular IOR Level')
    if specular is not None:
        specular.default_value = 0.0
    return material


def bump_material(name):
    """Principled + Bump（Noise 驱动）—— 法线通道要的就是这条链"""
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    tree = material.node_tree
    principled = next(n for n in tree.nodes if n.type == 'BSDF_PRINCIPLED')
    noise = tree.nodes.new('ShaderNodeTexNoise')
    noise.location = (-600, -200)
    noise.inputs['Scale'].default_value = 12.0
    bump = tree.nodes.new('ShaderNodeBump')
    bump.location = (-300, -200)
    bump.inputs['Strength'].default_value = 1.0
    bump.inputs['Distance'].default_value = 0.2
    tree.links.new(noise.outputs['Fac'], bump.inputs['Height'])
    tree.links.new(bump.outputs['Normal'], principled.inputs['Normal'])
    return material


def tree_shape(material):
    """材质的可比较形状（节点 / 连线）—— 证明法线通道**一个节点都没动**"""
    tree = material.node_tree
    return (sorted((node.name, node.type) for node in tree.nodes),
            sorted((link.from_node.name, link.from_socket.name,
                    link.to_node.name, link.to_socket.name) for link in tree.links))


def select_only(*objects):
    wanted = [obj for obj in objects if obj is not None]
    for obj in bpy.context.view_layer.objects:
        try:
            obj.select_set(obj in wanted)
        except RuntimeError:
            pass
    if wanted:
        bpy.context.view_layer.objects.active = wanted[0]


def bake_once(workflow, type_key, size=64, from_uv=None, to_uv=None):
    """走**插件自己的**流水线跑一张图（settings_to_bake_settings -> plan -> BakeJob）"""
    settings.workflow = workflow
    settings.bake_method = 'EMISSION'
    settings.maps.clear()
    settings.rebake_maps.clear()
    if from_uv is not None:
        settings.rebake_from_uv = from_uv
    if to_uv is not None:
        settings.rebake_to_uv = to_uv
    uops.add_map(settings, type_key, size, dedupe=True)
    active = usession.Session.get()
    active.reset()
    if not active.start(bpy.context, settings):
        return None, active
    active.run_blocking()
    return active.job, active


def task_of(job, group):
    for task in job.plan.tasks:
        if task.group_name == group:
            return task
    return job.plan.tasks[0] if job.plan.tasks else None


def image_stats(image, low=0.0, high=1.0):
    """区域内的**快照**读数（下一次烘焙会覆盖同一张图，所以必须当场拷下来）

    返回：平均 RGB / 相对"无扰动"的平均与最大偏移 / 偏移>0.02 的比例 /
          不同取值个数 / 最大通道值（判"这片是不是空的"用黑色，不是用 FLAT）
    """
    if image is None:
        return None
    width, height = image.size
    buffer = [0.0] * (width * height * 4)
    image.pixels.foreach_get(buffer)
    colors = []
    for y in range(height):
        v = (y + 0.5) / height
        for x in range(width):
            u = (x + 0.5) / width
            if not (low <= u <= high and low <= v <= high):
                continue
            index = (y * width + x) * 4
            colors.append((buffer[index], buffer[index + 1], buffer[index + 2]))
    if not colors:
        return None
    mean = tuple(sum(c[k] for c in colors) / len(colors) for k in range(3))
    deviations = [max(abs(c[k] - FLAT[k]) for k in range(3)) for c in colors]
    return {
        "count": len(colors),
        "mean": mean,
        "mean_dev": sum(deviations) / len(deviations),
        "max_dev": max(deviations),
        "dev_frac": sum(1 for d in deviations if d > 0.02) / len(deviations),
        "distinct": len({tuple(round(value, 4) for value in c) for c in colors}),
        "max_value": max(max(c) for c in colors),
        "pixels": colors,
    }


def describe(label, stats):
    if stats is None:
        print("  {:<44} 没有可读的像素".format(label))
        return
    print("  {:<44} mean=({:.4f},{:.4f},{:.4f}) 平均偏移={:.4f} 最大偏移={:.4f} "
          "偏移>0.02={:.1%} 取值={} 最大通道={:.4f}".format(
              label, stats["mean"][0], stats["mean"][1], stats["mean"][2],
              stats["mean_dev"], stats["max_dev"], stats["dev_frac"],
              stats["distinct"], stats["max_value"]))


def snapshot_diff(a, b):
    """两份快照逐像素的平均绝对差（RGB）；形状不同返回 None"""
    if not a or not b:
        return None
    pa, pb = a["pixels"], b["pixels"]
    if len(pa) != len(pb):
        return None
    total = 0.0
    for ca, cb in zip(pa, pb):
        total += sum(abs(ca[k] - cb[k]) for k in range(3))
    return total / (len(pa) * 3)


def positive(stats, key, threshold):
    return bool(stats) and stats[key] > threshold


# ====================================================================================
print("=== A · 名字与现取（预览崩溃那一条）===")

live_plane = two_layer_plane("LivePlane", "UV1", "UV2")
live_plane.data.materials.append(textured_material("LiveMat",
                                                   gradient_image("LiveSource", 64)))
select_only(live_plane)

# A0：`object_name()` 对死引用是安全的（整条修法的地基）
dead_mesh = bpy.data.meshes.new("DeadMesh")
dead_object = bpy.data.objects.new("DeadObject", dead_mesh)
scene.collection.objects.link(dead_object)
bpy.data.objects.remove(dead_object)
bpy.data.meshes.remove(dead_mesh)
try:
    dead_object.name
    died, exc = False, None
except ReferenceError as error:
    died, exc = True, error
check("旧引用读 .name 真的会抛 ReferenceError（用户看到的那个错就是这个）",
      died, exc)
check("object_name(死引用) 返回空串而不是抛异常",
      ss.object_name(dead_object) == "", repr(ss.object_name(dead_object)))
check("live_object(死引用) 返回 None", live_mod.live_object(dead_object) is None)
check("live_object('不存在的名字') 返回 None",
      live_mod.live_object("NoSuchObject") is None)
check("live_object 接受名字",
      live_mod.live_object("LivePlane") is live_plane,
      live_mod.live_object("LivePlane"))

# 真跑一轮 Re-bake（顺便把交付材质与烘焙槽准备好）
job, active = bake_once(uprops.WORKFLOW_REBAKE, "shader.base_color",
                        from_uv="UV1", to_uv="UV2")
check("A 的准备轮跑完没有失败",
      active.report is not None and active.report.failed == 0
      and active.report.done == 1,
      active.report.summary() if active.report else active.error)
plan = active.job.plan

check("计划里的组带**名字**（不是只带引用）",
      plan.groups[0].object_names == ["LivePlane"], plan.groups[0].object_names)
check("计划能按名字列出全部物体", plan.object_names() == ["LivePlane"],
      plan.object_names())
check("任务里也留了目标的名字",
      task_of(job, "LivePlane").target_object_names() == ["LivePlane"],
      task_of(job, "LivePlane").target_object_names())
check("名字 -> 组名 能查回来", plan.group_name_for("LivePlane") == "LivePlane",
      plan.group_name_for("LivePlane"))
check("名字 -> 组名：不存在的名字给空串", plan.group_name_for("Ghost") == "",
      repr(plan.group_name_for("Ghost")))
check("object_all_succeeded 接受名字",
      plan.object_all_succeeded("LivePlane")
      and not plan.object_all_succeeded("Ghost"),
      (plan.object_all_succeeded("LivePlane"), plan.object_all_succeeded("Ghost")))
check("eligible_names = 本次全部成功的物体名",
      plan.eligible_names() == ["LivePlane"], plan.eligible_names())
check("目标与源的名字分开记（projects 判据按名字比）",
      task_of(job, "LivePlane").projects is False,
      task_of(job, "LivePlane").source_object_names())
check("渲染层在烘完之后回到 UV1（零残留）",
      uvs.render_layer_name(live_plane) == "UV1",
      uvs.render_layer_name(live_plane))
check("交付材质与烘焙槽准备好了（后面的预览才有的可动）",
      bool(active.materials_by_group) and slots_mod.applied_names() == ["LivePlane"],
      (sorted(active.materials_by_group), slots_mod.applied_names()))

# A1：用户那条路径 —— 建计划 -> 进编辑模式 -> 出来 -> Preview
print("  -- 用户路径：进编辑模式再按 Preview --")
select_only(live_plane)
try:
    bpy.ops.object.mode_set(mode='EDIT')
    in_edit = True
    bpy.ops.object.mode_set(mode='OBJECT')
    back = True
except Exception as error:                       # noqa: BLE001
    in_edit, back = False, error
check("进得了编辑模式、出得来（背景模式也能压这一步）", in_edit and back,
      (in_edit, back))
check("出来之后确实是物体模式", live_plane.mode == 'OBJECT', live_plane.mode)

settings.workflow = uprops.WORKFLOW_REBAKE
try:
    changed = active.preview_materials(settings, baked=True)
    crash = None
except Exception as error:                       # noqa: BLE001
    changed, crash = 0, error
check("进过编辑模式之后 Preview 不抛异常", crash is None, crash)
check("Preview 真的分配了面", changed and changed > 0, changed)
check("Preview 把渲染层切到写入层 UV2",
      uvs.render_layer_name(live_plane) == "UV2",
      uvs.render_layer_name(live_plane))
check("Preview 记下了原值（能还回去）",
      uv_layers.PROP_ORIG_RENDER_UV in live_plane, dict(live_plane.items()))
try:
    restored = active.preview_materials(settings, baked=False)
    crash = None
except Exception as error:                       # noqa: BLE001
    restored, crash = 0, error
check("Show Original 也不抛异常", crash is None, crash)
check("Show Original 还回 UV1", uvs.render_layer_name(live_plane) == "UV1",
      uvs.render_layer_name(live_plane))

# A2：数据块被换掉 —— 计划里那份引用变成死引用（撤销步就是这个效果）
print("  -- 数据块被换掉（旧引用作废）之后，交付 / 预览 / 收尾照常 --")
stale = plan.groups[0].objects[0]
stale_name = stale.name
replacement = stale.copy()                       # 自定义属性跟着复制（obj.copy 会带）
replacement.data = stale.data.copy()
scene.collection.objects.link(replacement)
bpy.data.objects.remove(stale)                   # 老数据块释放 -> 老引用作废
replacement.name = stale_name                    # 名字换回来（撤销重读就是这样）
try:
    stale.name
    died, exc = False, None
except ReferenceError as error:
    died, exc = True, error
check("计划里那份引用真的死了（等于撤销步之后的状态）", died, exc)
check("计划里的名字还在（这正是修法的意义）",
      plan.groups[0].object_names == [stale_name], plan.groups[0].object_names)
check("现取拿到的是**新的**那个物体",
      live_mod.live_object(stale_name) is replacement,
      live_mod.live_object(stale_name))
check("按名字列成功的物体照样对", plan.eligible_names() == [stale_name],
      plan.eligible_names())
check("applied_names 也按名字（死引用不会把它带崩）",
      slots_mod.applied_names() == [stale_name], slots_mod.applied_names())

try:
    changed = active.preview_materials(settings, baked=True)
    crash = None
except Exception as error:                       # noqa: BLE001
    changed, crash = 0, error
check("数据块换掉之后 Preview 不抛异常", crash is None, crash)
check("数据块换掉之后 Preview 仍然生效", changed and changed > 0, changed)
check("渲染层跟着切到 UV2", uvs.render_layer_name(replacement) == "UV2",
      uvs.render_layer_name(replacement))
try:
    restored = active.preview_materials(settings, baked=False)
    crash = None
except Exception as error:                       # noqa: BLE001
    restored, crash = 0, error
check("数据块换掉之后 Show Original 也生效", crash is None and restored > 0,
      (crash, restored))

try:
    outcomes = slots_mod.finalize_objects(plan)
    crash = None
except Exception as error:                       # noqa: BLE001
    outcomes, crash = [], error
check("收尾（材质槽）不抛异常且成功",
      crash is None and outcomes and all(o.ok for o in outcomes),
      (crash, [repr(o) for o in outcomes]))
try:
    duplicate, reason = objects_mod.create_final_object(stale_name)
    crash = None
except Exception as error:                       # noqa: BLE001
    duplicate, reason, crash = None, "", error
check("按名字建副本能拿到物体",
      crash is None and duplicate is not None, (crash, reason))
if duplicate is not None:
    print("      副本:", duplicate.name,
          "UV 层:", [layer.name for layer in duplicate.data.uv_layers])

# A3：真的没了的物体 -> 只记一句，不抛
print("  -- 物体真的没了：只记 'object is gone' --")
outcomes = slots_mod.assign_objects(plan, objects=["Ghost"], baked=True)
check("Preview 一个不存在的物体 -> 一条 object is gone 的结果",
      len(outcomes) == 1 and not outcomes[0].ok
      and outcomes[0].reason == "object is gone",
      [repr(o) for o in outcomes])
outcomes = slots_mod.finalize_objects(plan, objects=["Ghost"])
check("收尾一个不存在的物体 -> 同样只记一条",
      len(outcomes) == 1 and outcomes[0].reason == "object is gone",
      [repr(o) for o in outcomes])
outcomes = slots_mod.restore_objects(["Ghost"])
check("还原一个不存在的物体 -> 同样只记一条",
      len(outcomes) == 1 and outcomes[0].reason == "object is gone",
      [repr(o) for o in outcomes])
outcomes = slots_mod.add_baked_slots(plan, active.materials_by_group,
                                     objects=["Ghost"])
check("加槽一个不存在的物体 -> 同样只记一条",
      len(outcomes) == 1 and outcomes[0].reason == "object is gone",
      [repr(o) for o in outcomes])
created, skipped = objects_mod.create_final_objects(["Ghost"])
check("建副本：不存在的物体只报原因",
      created == [] and skipped == [("Ghost", "object is gone")],
      (len(created), skipped))
outcome = uv_layers.finalize_layers("Ghost")
check("UV 成品化：不存在的物体 -> object is gone",
      not outcome.ok and outcome.reason == "object is gone"
      and outcome.object_name == "Ghost", repr(outcome))
ok, reason = uv_layers.switch_render_layer("Ghost", "UV2")
check("切渲染层：不存在的物体 -> object is gone",
      not ok and reason == "object is gone", (ok, reason))
ok, reason = uv_layers.restore_render_layer("Ghost")
check("还渲染层：不存在的物体 -> object is gone",
      not ok and reason == "object is gone", (ok, reason))

# A4：UV 成品化（不可逆那一步）也照名字走
print("  -- UV 成品化按名字走 --")
outcome = uv_layers.finalize_layers(stale_name, "UV2")
check("成品化成功且保留了写入层", outcome.ok and outcome.kept == uv_layers.LAYER_NAME,
      repr(outcome))
check("旧层真被删掉、只剩一层",
      [layer.name for layer in replacement.data.uv_layers] == [uv_layers.LAYER_NAME],
      [layer.name for layer in replacement.data.uv_layers])
check("active_render 指着保留层",
      uvs.render_layer_name(replacement) == uv_layers.LAYER_NAME,
      uvs.render_layer_name(replacement))

# ====================================================================================
print("\n=== B · UV Sets 下拉改成 None + 检测到的层名 ===")

drop_plane = two_layer_plane("DropPlane", "Alpha", "Beta")
drop_plane.data.materials.append(
    textured_material("DropMat", gradient_image("DropSource", 16)))

select_only(drop_plane)
settings.workflow = uprops.WORKFLOW_REBAKE
from_items = uprops._rebake_from_items(settings, bpy.context)
to_items = uprops._rebake_to_items(settings, bpy.context)
print("  采样侧 items:", [(item[0], item[1]) for item in from_items])
print("  写入侧 items:", [(item[0], item[1]) for item in to_items])
check("第一项是哨兵 None（标识符不是空串 —— 空标识符会被 Blender 当分隔符丢掉）",
      from_items[0][0] == uvs.NONE_UV and uvs.NONE_UV,
      (from_items[0][0], uvs.NONE_UV))
check("第一项的标签就是 `None`", from_items[0][1] == "None", from_items[0][1])
check("两个下拉的第一项一样",
      to_items[0][0] == from_items[0][0] and to_items[0][1] == from_items[0][1],
      (to_items[0][:2], from_items[0][:2]))
check("后面跟的就是**实际检测到的层名**（排序去重）",
      [item[0] for item in from_items[1:]] == ["Alpha", "Beta"],
      [item[0] for item in from_items[1:]])
check("下拉项里没有那两个啰嗦哨兵项了",
      not any(item[0] in ("__same__", "__auto__") for item in from_items),
      [item[0] for item in from_items])
check("标签里没有 'same as write layer' 那种长句",
      not any("same as write" in item[1].lower() for item in from_items),
      [item[1] for item in from_items])
check("标签里没有 'each object' 那种长句",
      not any("each object" in item[1].lower() for item in to_items),
      [item[1] for item in to_items])
check("None 那一项的说明是**短句**（<= 80 字符）",
      all(len(item[2]) <= 80 for item in (from_items[0], to_items[0])),
      [(len(from_items[0][2]), from_items[0][2]),
       (len(to_items[0][2]), to_items[0][2])])
check("采样侧的 None 说明写的是'不钉采样层'",
      "sampling layer" in from_items[0][2], from_items[0][2])
check("写入侧的 None 说明写的是'不换渲染层'",
      "render layer" in to_items[0][2], to_items[0][2])

select_only(drop_plane, replacement)
both_items = uprops._rebake_from_items(settings, bpy.context)
check("两个物体都选中时，层名是并集且排序",
      [item[0] for item in both_items[1:]] == ["Alpha", "Beta", uv_layers.LAYER_NAME],
      [item[0] for item in both_items[1:]])
select_only(drop_plane)

# 旧哨兵：旧 .blend / 旧预设里存着的值必须当 None（不能让用户"选过却什么都不对"）
check("is_none：空串 / None 哨兵 / None / 两个旧哨兵都算'什么都不动'",
      uvs.is_none("") and uvs.is_none(uvs.NONE_UV) and uvs.is_none(None)
      and all(uvs.is_none(old) for old in uvs.LEGACY_SENTINELS),
      uvs.LEGACY_SENTINELS)
check("is_none：真层名不算", not uvs.is_none("Alpha"), uvs.is_none("Alpha"))
check("resolve_choices：旧哨兵 __same__ 当 None（写入层照旧生效）",
      uvs.resolve_choices("__same__", "UV2") == ("", "UV2"),
      uvs.resolve_choices("__same__", "UV2"))
check("resolve_choices：旧哨兵 __auto__ 当 None",
      uvs.resolve_choices("UV1", "__auto__") == ("UV1", ""),
      uvs.resolve_choices("UV1", "__auto__"))
check("resolve_choices：两侧 None -> 一个字节都不动",
      uvs.resolve_choices(uvs.NONE_UV, uvs.NONE_UV) == ("", ""),
      uvs.resolve_choices(uvs.NONE_UV, uvs.NONE_UV))
check("resolve_choices：采样侧 None + 写入层 -> 不钉采样层",
      uvs.resolve_choices(uvs.NONE_UV, "UV2") == ("", "UV2"),
      uvs.resolve_choices(uvs.NONE_UV, "UV2"))

KEYS = ("nothing", "no_pin", "same", "needs_swap")
stats = uvs.layer_stats([drop_plane], "", "")
check("stats：两侧都不动 -> nothing=True、no_pin=True",
      stats["nothing"] and stats["no_pin"] and not stats["same"],
      {key: stats[key] for key in KEYS})
stats = uvs.layer_stats([drop_plane], "Alpha", "Alpha")
check("stats：采样 == 写入 -> same=True、nothing=False",
      stats["same"] and not stats["nothing"] and not stats["no_pin"],
      {key: stats[key] for key in KEYS})
stats = uvs.layer_stats([drop_plane], "", "Beta")
check("stats：只写不钉 -> no_pin=True、same=False、needs_swap=1",
      stats["no_pin"] and not stats["same"] and stats["needs_swap"] == 1,
      {key: stats[key] for key in KEYS})
stats = uvs.layer_stats([drop_plane], "Alpha", "Beta")
check("stats：两层都指定 -> 三个标志都是 False、needs_swap=1",
      not (stats["no_pin"] or stats["same"] or stats["nothing"])
      and stats["needs_swap"] == 1,
      {key: stats[key] for key in KEYS})

# ====================================================================================
print("\n=== C · 法线通道：先量，再改 ===")
settings.auto_deliver = False

normal_plane = two_layer_plane("NormalPlane", "UV1", "UV2")
normal_material = bump_material("NormalMat")
normal_plane.data.materials.append(normal_material)
normal_socket = next(n for n in normal_material.node_tree.nodes
                     if n.type == 'BSDF_PRINCIPLED').inputs['Normal']
check("夹具：Normal 输入上真的接了 Bump 链", bool(normal_socket.links),
      normal_socket.links[0].from_node.type if normal_socket.links else None)

# C1：静态判据（共用层：类型表 + 手术判据）
check("native_pass 里 shader.normal 仍然是 NORMAL",
      bt.native_pass("shader.normal") == ("NORMAL", {}),
      bt.native_pass("shader.normal"))
check("forced_native_pass：法线通道**必须**走原生 pass",
      bt.forced_native_pass(bt.require("shader.normal")) == ("NORMAL", {})
      and bt.forced_native_pass(bt.require("shader.coat_normal")) == ("NORMAL", {}),
      (bt.forced_native_pass(bt.require("shader.normal")),
       bt.forced_native_pass(bt.require("shader.coat_normal"))))
check("forced_native_pass：值通道照旧跟着 bake_method 走",
      bt.forced_native_pass(bt.require("shader.base_color")) is None
      and bt.forced_native_pass(bt.require("shader.roughness")) is None,
      bt.forced_native_pass(bt.require("shader.base_color")))
check("needs_surgery：法线通道不要手术（shader.normal / shader.coat_normal）",
      not sg.needs_surgery(bt.require("shader.normal"))
      and not sg.needs_surgery(bt.require("shader.coat_normal")),
      (sg.needs_surgery(bt.require("shader.normal")),
       sg.needs_surgery(bt.require("shader.coat_normal"))))
check("needs_surgery：值通道**继续走手术**（别跟着一起改）",
      sg.needs_surgery(bt.require("shader.base_color"))
      and sg.needs_surgery(bt.require("shader.roughness")),
      (sg.needs_surgery(bt.require("shader.base_color")),
       sg.needs_surgery(bt.require("shader.roughness"))))
check("needs_surgery：标准 pass 一直是不做手术",
      not sg.needs_surgery(bt.require("standard.normal")), "")


def bake_normal(workflow, disconnect, label, from_uv=None, to_uv=None):
    """烘一张 shader.normal，返回 (快照, 任务明细)

    disconnect=True -> 烘之前把 Normal 输入上的链断开（= 几何基准那一档）
    """
    saved = None
    if disconnect and normal_socket.links:
        saved = normal_socket.links[0].from_socket
        normal_material.node_tree.links.remove(normal_socket.links[0])
    shape_before = tree_shape(normal_material)
    select_only(normal_plane)
    try:
        job, active = bake_once(workflow, "shader.normal", size=64,
                                from_uv=from_uv, to_uv=to_uv)
        task = task_of(job, "NormalPlane") if job else None
        detail = " | ".join(task.surgery_detail or []) if task else ""
        print("  {:<40} 明细: {}".format(label, detail))
        check("{}：跑完没有失败".format(label),
              active.report is not None and active.report.failed == 0,
              active.report.summary() if active.report else active.error)
        check("{}：材质一个节点都没被动过（法线不走手术）".format(label),
              tree_shape(normal_material) == shape_before,
              (len(shape_before[0]), len(tree_shape(normal_material)[0])))
        # ⚠ 立刻取快照：同一个组 + 同一个类型 -> 同一个图像名，下一次烘焙会覆盖它
        return image_stats(task.image) if task else None, detail
    finally:
        if saved is not None and not normal_socket.links:
            normal_material.node_tree.links.new(saved, normal_socket)


# ---- 第一遍：Bake 工作流（共享引擎；普通批量烘焙照旧走同一条路）
print("  -- Bake 工作流（默认 EMISSION）--")
bake_stats, bake_detail = bake_normal(uprops.WORKFLOW_BAKE, False, "Bake/带链")
check("Bake：明细写明走的是原生 pass",
      "native pass: NORMAL" in bake_detail, bake_detail)
check("Bake：明细写明了**为什么**不做手术（surgery + Normal）",
      "surgery" in bake_detail and "Normal" in bake_detail, bake_detail)
describe("Bake/带链（整幅）", bake_stats)

base_stats, _detail = bake_normal(uprops.WORKFLOW_BAKE, True,
                                  "Bake/断开链（几何基准）")
describe("Bake/断开链（整幅）", base_stats)


def crop(stats, low, high, size=64):
    """从**整幅**快照里切出 UV low..high 那一块（不再读图 —— 图已被后一轮覆盖）

    ⚠ 为什么必须切：UV 岛只占画面的一小块（这里 0.1–0.3 = 4%），岛外是烘焙器
      预置的中性法线 (0.5,0.5,1.0)。整幅统计会把"岛内真的有扰动"稀释成 4%，
      看起来像"bump 几乎没生效"。
    """
    if not stats or len(stats["pixels"]) != size * size:
        return None
    picked = []
    for y in range(size):
        v = (y + 0.5) / size
        for x in range(size):
            u = (x + 0.5) / size
            if low <= u <= high and low <= v <= high:
                picked.append(stats["pixels"][y * size + x])
    if not picked:
        return None
    deviations = [max(abs(c[k] - FLAT[k]) for k in range(3)) for c in picked]
    return {
        "count": len(picked),
        "mean": tuple(sum(c[k] for c in picked) / len(picked) for k in range(3)),
        "mean_dev": sum(deviations) / len(deviations),
        "max_dev": max(deviations),
        "dev_frac": sum(1 for d in deviations if d > 0.02) / len(deviations),
        "distinct": len({tuple(round(value, 4) for value in c) for c in picked}),
        "max_value": max(max(c) for c in picked),
        "pixels": picked,
    }


# 平面在 UV1 里是 0.1–0.3 那一块 —— 只统计岛内（外面是预置的中性值）
bake_island = crop(bake_stats, 0.11, 0.29)
base_island = crop(base_stats, 0.11, 0.29)
describe("Bake/带链（UV 岛内 0.11-0.29）", bake_island)
describe("Bake/断开链（UV 岛内）", base_island)
# 16bit 数据图有量化噪声（实测 1.0e-05），所以"常数"要按容差判，不能写 == 0
check("Bake：几何基准是常数 (0.5,0.5,1.0)（平面 -> 无扰动）",
      bool(base_island) and base_island["max_dev"] < 1e-4
      and base_island["distinct"] == 1,
      ((base_island or {}).get("max_dev"), (base_island or {}).get("distinct")))
check("Bake：带链之后岛内**几乎每个像素都偏离**几何基准（bump 没丢）",
      positive(bake_island, "mean_dev", 0.05)
      and positive(bake_island, "dev_frac", 0.9),
      ((bake_island or {}).get("mean_dev"), (bake_island or {}).get("dev_frac")))
check("Bake：带链的图像不是常量（真的有扰动细节）",
      positive(bake_island, "distinct", 50), (bake_island or {}).get("distinct"))
bake_vs_base = snapshot_diff(bake_island, base_island)
print("  Bake：带链 vs 几何基准（岛内）逐像素平均绝对差 = {}".format(
    "n/a" if bake_vs_base is None else "{:.6f}".format(bake_vs_base)))
check("Bake：两者逐像素差异可分辨（>= 0.05）",
      bake_vs_base is not None and bake_vs_base >= 0.05, bake_vs_base)

# ---- 第二遍：Re-bake 工作流（跨 UV），写入层在 0.7–0.9
print("  -- Re-bake 工作流（采样 UV1 -> 写进 UV2）--")
rebake_stats, rebake_detail = bake_normal(uprops.WORKFLOW_REBAKE, False,
                                          "Re-bake/带链", from_uv="UV1", to_uv="UV2")
check("Re-bake：明细里既有原生 pass，也有写入层切换",
      "native pass: NORMAL" in rebake_detail and "write layer 'UV2'" in rebake_detail,
      rebake_detail)
rebake_base_stats, _detail = bake_normal(uprops.WORKFLOW_REBAKE, True,
                                         "Re-bake/断开链（几何基准）",
                                         from_uv="UV1", to_uv="UV2")
check("Re-bake：跑完渲染层回到 UV1（脚手架零残留）",
      uvs.render_layer_name(normal_plane) == "UV1",
      uvs.render_layer_name(normal_plane))

write_region = crop(rebake_stats, 0.72, 0.88)
write_base = crop(rebake_base_stats, 0.72, 0.88)
source_region = crop(rebake_stats, 0.0, 0.3)
describe("Re-bake/带链（写入层覆盖区 0.72-0.88）", write_region)
describe("Re-bake/断开链（写入层覆盖区 0.72-0.88）", write_base)
describe("Re-bake/带链（源层那一片 0.0-0.3）", source_region)
check("Re-bake：写入层覆盖区里有内容（不是空图）",
      positive(write_region, "distinct", 50), (write_region or {}).get("distinct"))
check("Re-bake：写入层覆盖区里 bump 没丢（几乎每个像素都偏离几何基准）",
      positive(write_region, "mean_dev", 0.05)
      and positive(write_region, "dev_frac", 0.9),
      ((write_region or {}).get("mean_dev"), (write_region or {}).get("dev_frac")))
check("Re-bake：几何基准在写入层覆盖区里是常数（同一条判据成立）",
      bool(write_base) and write_base["max_dev"] < 1e-4,
      (write_base or {}).get("max_dev"))
# 源层那一片（0.0–0.3）在**写入层**里没有岛 —— 那里必须还是预置的中性值。
# 如果引擎把图画到了源层上，这两条会同时翻过来。
check("Re-bake：源层那一片没被动过（图确实写在写入层，不是源层）",
      bool(source_region) and source_region["max_dev"] < 1e-4
      and source_region["distinct"] == 1,
      ((source_region or {}).get("max_dev"), (source_region or {}).get("distinct")))
rebake_vs_base = snapshot_diff(write_region, write_base)
print("  Re-bake：带链 vs 几何基准（写入层区域）逐像素平均绝对差 = {}".format(
    "n/a" if rebake_vs_base is None else "{:.6f}".format(rebake_vs_base)))
check("Re-bake：两者逐像素差异可分辨（>= 0.05）",
      rebake_vs_base is not None and rebake_vs_base >= 0.05, rebake_vs_base)

# ---- 第三遍：standard.normal 与 shader.normal 等价（修完之后）
print("  -- standard.normal 与 shader.normal 等价 --")
check("standard.normal 的 bake_pass 也是 NORMAL",
      bt.require("standard.normal").bake_pass == "NORMAL",
      bt.require("standard.normal").bake_pass)
select_only(normal_plane)
standard_job, standard_active = bake_once(uprops.WORKFLOW_BAKE, "standard.normal",
                                          size=64)
standard_task = task_of(standard_job, "NormalPlane")
standard_stats = image_stats(standard_task.image) if standard_task else None
describe("standard.normal", standard_stats)
standard_vs_shader = snapshot_diff(standard_stats, bake_stats)
print("  standard.normal vs shader.normal 逐像素平均绝对差 = {}".format(
    "n/a" if standard_vs_shader is None else "{:.6f}".format(standard_vs_shader)))
check("两条路径烘出来的法线**逐像素相同**（等价）",
      standard_vs_shader is not None and standard_vs_shader < 1e-6,
      standard_vs_shader)

# ---- 回归：值通道仍然走手术（别把法线那条修法扩散出去）
print("  -- 值通道：base_color 仍然走节点手术 --")
color_shape_before = tree_shape(normal_material)
select_only(normal_plane)
color_job, color_active = bake_once(uprops.WORKFLOW_BAKE, "shader.base_color", size=64)
color_task = task_of(color_job, "NormalPlane")
color_detail = " | ".join(color_task.surgery_detail or []) if color_task else ""
print("  base_color 明细:", color_detail)
check("base_color 照旧走手术（明细里不是 native pass）",
      bool(color_detail) and "native pass" not in color_detail, color_detail)
check("base_color 跑完材质形状**完全还原**（手术脚手架收干净）",
      tree_shape(normal_material) == color_shape_before,
      (len(color_shape_before[0]), len(tree_shape(normal_material)[0])))
check("base_color 烘出内容了",
      color_active.report is not None and color_active.report.failed == 0,
      color_active.report.summary() if color_active.report else color_active.error)

# ---- 收尾卫生
usession.Session.forget()
bpy.ops.wm.read_factory_settings(use_empty=True)

print("\n=== 结果 ===")
if FAIL:
    print("FAILED {} check(s):".format(len(FAIL)))
    for name in FAIL:
        print("  -", name)
else:
    print("ALL CHECKS PASSED")

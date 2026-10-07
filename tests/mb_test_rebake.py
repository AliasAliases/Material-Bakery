"""Material Bakery — Re-bake（跨 UV 重烘）回归测试

覆盖（甲方在 _handoff/07_ROUND3_BRIEF.md §6 里点名的五组）：

    A 下拉与步骤表        From / To 的哨兵值、两条工作流的步骤表（数据驱动）
    B 合成平面判读        真机 Cycles：注入后写入层读到的必须是**源层坐标**；
                          不注入（对照）读到的是**写入层坐标**
    C 零残留              跑完 / 中途失败之后，节点、连线、每个 UVMap.uv_map、
                          UV 坐标校验和、active_render 全部逐项零差异
    D 边界                缺 From 层 -> WARN 且不注入；From == To -> 不注入；
                          链路里有 Mapping -> 钉在它**之前**（R2-4 的实测结论）
    E 工作流隔离          切到 Re-bake 不动 Bake 的 Maps 与目标选择
    F 曲面真机            _samples/square_bush.blend 的 BushSquare，只读；
                          shader.base_color 的 NATIVE / EMISSION 两档
                          （R2 留空的那一格），判据用 R2-5 的"唯一归属 texel"MAE 法

判据都是**打印出来的数字**：不硬编码版本号 / 路径 / 计数，断言失败能指出是哪一格。
`_samples/` 里的 .blend 只读打开，绝不存盘（跑完检查没有多出 .blend1）。

⚠ 只在 EMISSION 通道验过的东西不许写成"所有通道"（R1 的教训）：这里 B 只测
  `shader.base_color`（那正是 Re-bake 的入口通道），F 才把 NATIVE 那一档补上。
"""
import bpy, sys, os, shutil, glob

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WS not in sys.path:
    sys.path.insert(0, WS)

import material_bakery
from material_bakery import compat
from material_bakery.core import plan as pl
from material_bakery.core import scene_scan as ss
from material_bakery.core import transaction as txmod
from material_bakery.engine import backend as bk
from material_bakery.engine import job as jb
from material_bakery.engine import providers as pv
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


def near(got, want, tolerance=0.01):
    return abs(got - want) <= tolerance


def near_point(got, want, tolerance=0.01):
    return all(near(got[i], want[i], tolerance) for i in range(3))


# ------------------------------------------------------------------------------------
#   小工具

def srgb_decode(value):
    """sRGB 编码 -> 线性

    ⚠ 为什么需要：`image.pixels` 取回的是 **8bit sRGB 缓冲**而不是线性值
      （R1 实测：材质写 0.9 读回 ≈0.953、0.05 -> ≈0.247）。颜色通道
      （`shader.base_color`）的烘焙目标图就是 sRGB，读数必须先解码才对得上
      "UV 坐标"这种线性量。
    """
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def read_pixels(image):
    image.update()
    width, height = image.size
    buffer = [0.0] * (width * height * 4)
    image.pixels.foreach_get(buffer)
    return width, height, buffer


def linear_at_uv(image, u, v):
    """UV (u, v) 所在 texel 的**线性** RGB"""
    width, height, buffer = read_pixels(image)
    x = min(width - 1, max(0, int(u * width)))
    y = min(height - 1, max(0, int(v * height)))
    index = (y * width + x) * 4
    return tuple(round(srgb_decode(buffer[index + c]), 4) for c in range(3))


def gradient_image(name, resolution):
    """坐标编码图：R = u, G = v（脚本内合成，绝不存盘）

    ⚠ float buffer：误差只该来自**烘焙链路**，不该来自源图自己的 8bit 量化。
    """
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


def tree_shape(material):
    """材质的可比较形状：节点 / 连线 / 每个 UVMap.uv_map"""
    tree = material.node_tree
    return {
        "nodes": sorted((node.name, node.type) for node in tree.nodes),
        "links": sorted((link.from_node.name, link.from_socket.name,
                         link.to_node.name, link.to_socket.name)
                        for link in tree.links),
        "uvmaps": sorted((node.name, node.uv_map) for node in tree.nodes
                         if node.type == 'UVMAP'),
    }


def snapshot(materials, objects):
    """术前快照：节点 / 连线 / uv_map / UV 坐标校验和 / 当前渲染层

    逐项比，而不是"节点数一样就算还原"：删一个再建一个，节点数照样相等。
    坐标校验和用 foreach_get —— 68001 面的网格也是毫秒级。
    """
    meshes = {}
    for obj in objects:
        mesh = obj.data
        coords = {}
        for layer in mesh.uv_layers:
            buffer = [0.0] * (len(layer.data) * 2)
            layer.data.foreach_get("uv", buffer)
            coords[layer.name] = round(sum(buffer), 4)
        meshes[obj.name] = {
            "layers": [layer.name for layer in mesh.uv_layers],
            "render": uvs.render_layer_name(obj),
            "active_index": mesh.uv_layers.active_index,
            "coords": coords,
        }
    return {"trees": {m.name: tree_shape(m) for m in materials}, "meshes": meshes}


def diff_snapshot(label, before, after):
    """逐项比快照；有差异就把差异摊出来（断言失败要能指出是哪一格）"""
    bad = []
    for section in ("trees", "meshes"):
        for name in sorted(set(before[section]) | set(after[section])):
            left = before[section].get(name)
            right = after[section].get(name)
            if left != right:
                bad.append((section, name, left, right))
    check("{}：{} 项逐项零差异".format(label, len(before["trees"]) + len(before["meshes"])),
          not bad, bad[:2])


def to_bake_settings(workflow, settings=None):
    """按指定工作流编译一次 BakeSettings（用完把 workflow 放回去）

    ⚠ 必须传**真的设置对象**：settings_to_bake_settings 会读它一大堆属性，
      拿一个只有 workflow 的假对象去调是假的测试（第一版就这么错过）。
    """
    settings = settings if settings is not None else globals()["settings"]
    previous = settings.workflow
    settings.workflow = workflow
    try:
        return usession.settings_to_bake_settings(settings)
    finally:
        settings.workflow = previous


def pick_task(job, group="Plane"):
    """按组名挑任务

    ⚠ 别用 tasks[0]：场景里只要多一个组（比如默认 Cube），任务 0 就不是我们要
      读的那张图了 —— 第一版就是这么读到一张空的 Cube 贴图的。
    """
    for task in job.plan.tasks:
        if task.group_name == group:
            return task
    return job.plan.tasks[0] if job.plan.tasks else None


OUT = os.path.join(WS, "_probe", "rebake")
if os.path.isdir(OUT):
    shutil.rmtree(OUT)
os.makedirs(OUT)

# ⚠ 空场景起步：默认启动文件里有 Cube/Camera/Light，Cube 没材质也没 UV，
#   会被扫成一个组、烘出一张失败图 —— 那样"任务 0"就不是我们要读的那张了。
bpy.ops.wm.read_factory_settings(use_empty=True)
material_bakery.register()
scene = bpy.context.scene
settings = scene.mbakery
settings.output_dir = OUT
settings.pack_into_blend = True        # 图 pack 住才读得到像素（见 _secure_image）
settings.auto_deliver = False          # 交付是共享的、这一轮不测它，别让槽位变化干扰残留比对
settings.margin = 0
settings.antialias = 'OFF'
settings.udim = False
settings.use_subfolders = False
settings.fake_user = True
settings.bake_method = 'EMISSION'


# ====================================================================================
print("=== A · 两个下拉与两条步骤表（纯数据）===")

check("哨兵 `None` 不是空串（空标识符会被 Blender 当分隔符丢掉）",
      uvs.NONE_UV and uvs.NONE_UV == uvs.NONE_UV.strip(), uvs.NONE_UV)
check("旧的啰嗦哨兵只剩'兼容旧文件'这一条用途（不再出现在下拉里）",
      uvs.LEGACY_SENTINELS == ("__same__", "__auto__"), uvs.LEGACY_SENTINELS)

CHOICES = (
    ("默认：两侧都是 None -> 什么都不改", uvs.NONE_UV, uvs.NONE_UV, "", ""),
    ("To 指定一层、From 是 None -> 不钉采样层", uvs.NONE_UV, "UV2", "", "UV2"),
    ("From 指定一层、To 是 None -> 不换写入层", "UV1", uvs.NONE_UV, "UV1", ""),
    ("两层都指定", "UV1", "UV2", "UV1", "UV2"),
    ("脏数据（两个都空）", "", "", "", ""),
    ("旧哨兵 __same__ 当 None（旧 .blend 里可能存着）", "__same__", "UV2", "", "UV2"),
    ("旧哨兵 __auto__ 当 None", "UV1", "__auto__", "UV1", ""),
)
for label, from_value, to_value, want_sample, want_write in CHOICES:
    got = uvs.resolve_choices(from_value, to_value)
    check("resolve_choices · {}".format(label), got == (want_sample, want_write),
          (got, (want_sample, want_write)))

check("Bake 的步骤 = PAGES（今天的页面原样）",
      uprops.step_order(settings) == [key for key, _l, _d in uprops.PAGES],
      uprops.step_order(settings))
check("默认工作流是 Bake", settings.workflow == uprops.WORKFLOW_BAKE, settings.workflow)
check("WORKFLOWS 是数据驱动的一张表（至少两项、都是三元组）",
      len(uprops.WORKFLOWS) >= 2
      and all(len(item) == 3 for item in uprops.WORKFLOWS), uprops.WORKFLOWS)
check("每条流程的步骤条都非空，且共享的三步（Settings / Bake / Result）都在里面",
      all(len(uprops.workflow_steps(key)) >= 3 for key, _l, _d in uprops.WORKFLOWS)
      and all(uprops.PAGE_RESULT in [k for k, _l, _d in uprops.workflow_steps(key)]
              for key, _l, _d in uprops.WORKFLOWS),
      {key: [k for k, _l, _d in uprops.workflow_steps(key)]
       for key, _l, _d in uprops.WORKFLOWS})
check("Re-bake 的步骤顺序就是需求里写的那个",
      [key for key, _l, _d in uprops.workflow_steps(uprops.WORKFLOW_REBAKE)]
      == [uprops.PAGE_REBAKE_SOURCE, uprops.PAGE_REBAKE_UVS,
          uprops.PAGE_SETTINGS, uprops.PAGE_BAKE, uprops.PAGE_RESULT],
      [key for key, _l, _d in uprops.workflow_steps(uprops.WORKFLOW_REBAKE)])
check("Re-bake 里**没有** Remesh 这一步",
      getattr(uprops, "PAGE_REMESH", None) not in
      [key for key, _l, _d in uprops.workflow_steps(uprops.WORKFLOW_REBAKE)],
      [key for key, _l, _d in uprops.workflow_steps(uprops.WORKFLOW_REBAKE)])
check("page 枚举包含两条流程的全部页面",
      set(uprops.PAGES) <= set(uprops.ALL_PAGES)
      and uprops.PAGE_REBAKE_SOURCE in [item[0] for item in uprops.ALL_PAGES],
      [item[0] for item in uprops.ALL_PAGES])

settings.workflow = uprops.WORKFLOW_REBAKE
settings.page = uprops.PAGE_TARGETS
settings.sync_page()
check("切到 Re-bake 后页面被校正到该流程的第一步",
      settings.page == uprops.PAGE_REBAKE_SOURCE, settings.page)
check("Re-bake 的页码是 1/5", settings.page_index() == 0, settings.page_index())
settings.workflow = uprops.WORKFLOW_BAKE
settings.page = uprops.PAGE_TARGETS


# ====================================================================================
print("\n=== E · 工作流隔离（各记一份，共享的仍是同一份）===")

settings.maps.clear()
settings.target_mode = ss.MODE_SELECTED
uops.add_map(settings, "shader.base_color", 64)
uops.add_map(settings, "shader.roughness", 64)
check("Bake 先有两项", len(settings.maps) == 2, len(settings.maps))
check("Re-bake 那份一开是空的", len(settings.rebake_maps) == 0,
      len(settings.rebake_maps))

settings.workflow = uprops.WORKFLOW_REBAKE
check("切到 Re-bake：Bake 的 Maps 一个都没少", len(settings.maps) == 2,
      len(settings.maps))
check("切到 Re-bake：Bake 的目标模式没被动",
      settings.target_mode == ss.MODE_SELECTED, settings.target_mode)
check("Re-bake 有自己的目标模式（各记一份）",
      settings.rebake_target_mode == ss.MODE_COLLECTIONS, settings.rebake_target_mode)
uops.add_map(settings, "shader.normal", 64)
check("Re-bake 的 Add 加进自己那份",
      len(settings.rebake_maps) == 1 and len(settings.maps) == 2,
      (len(settings.rebake_maps), len(settings.maps)))
check("map_requests 跟着当前工作流走",
      [item[0] for item in settings.map_requests()] == ["shader.normal"],
      settings.map_requests())
# 共享设置：两条流程读的是**同一份**（命名模板 / 质量 / 输出都是共享的）
saved_template = settings.template
settings.template = "{prefix}_Shared_{type}_{size}"
check("共享设置（命名模板）两条流程读到的是同一份",
      to_bake_settings(uprops.WORKFLOW_BAKE).template == "{prefix}_Shared_{type}_{size}"
      and to_bake_settings(uprops.WORKFLOW_REBAKE).template
      == "{prefix}_Shared_{type}_{size}",
      (to_bake_settings(uprops.WORKFLOW_BAKE).template,
       to_bake_settings(uprops.WORKFLOW_REBAKE).template))
settings.template = saved_template

rebake_data = to_bake_settings(uprops.WORKFLOW_REBAKE)
check("Re-bake 的 BakeSettings 用自己那份 Maps",
      [item.type_key for item in rebake_data.maps] == ["shader.normal"],
      [item.type_key for item in rebake_data.maps])
check("Re-bake 的 BakeSettings 打了工作流标记",
      rebake_data.workflow == "rebake", rebake_data.workflow)
check("Re-bake 不做投影（源就是目标自己）",
      rebake_data.selected_to_active is False, rebake_data.selected_to_active)
settings.use_selected_to_active = True        # Bake 流程的开关，故意开着
check("UI 上开着投影，Re-bake 也不会走投影 provider",
      isinstance(pv.active_provider(to_bake_settings(uprops.WORKFLOW_REBAKE)),
                 pv.DefaultProvider),
      type(pv.active_provider(to_bake_settings(uprops.WORKFLOW_REBAKE))).__name__)
check("而 Bake 那份仍然走投影 provider",
      isinstance(pv.active_provider(to_bake_settings(uprops.WORKFLOW_BAKE)),
                 pv.ProjectionProvider),
      type(pv.active_provider(to_bake_settings(uprops.WORKFLOW_BAKE))).__name__)
settings.use_selected_to_active = False
settings.workflow = uprops.WORKFLOW_BAKE
check("切回 Bake 之后又读到 Bake 那份 Maps",
      [item.type_key for item in to_bake_settings(uprops.WORKFLOW_BAKE).maps]
      == ["shader.base_color", "shader.roughness"],
      [item.type_key for item in to_bake_settings(uprops.WORKFLOW_BAKE).maps])


# ====================================================================================
print("\n=== B · 合成平面判读（真机 Cycles）===")

def make_two_layer_plane(name="Plane", source="UV1", write="UV2", render=None):
    """单位平面：UV1 = 0.10–0.30（源），UV2 = 0.70–0.90（写），两层差一个 0.6 的平移"""
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata([(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)], [], [(0, 1, 2, 3)])
    mesh.update()
    mesh.uv_layers.new(name=source)
    mesh.uv_layers.new(name=write)
    # ⚠ 新建层会让之前拿到的层引用失效（R2 实测会直接崩在 MeshUVLoopLayer_data_begin）
    first = mesh.uv_layers.get(source)
    second = mesh.uv_layers.get(write)
    for i, (u, v) in enumerate(((0.1, 0.1), (0.3, 0.1), (0.3, 0.3), (0.1, 0.3))):
        first.data[i].uv = (u, v)
        second.data[i].uv = (u + 0.6, v + 0.6)
    render = render or source
    for layer in mesh.uv_layers:
        layer.active_render = (layer.name == render)
    mesh.uv_layers.active_index = mesh.uv_layers.find(render)
    return mesh


def plain_material(name, image):
    """Principled + 一张贴图（Vector **不接**东西 —— 采样层由引擎的钉子决定）"""
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    tree = material.node_tree
    for node in list(tree.nodes):
        tree.nodes.remove(node)
    output = tree.nodes.new('ShaderNodeOutputMaterial')
    shader = tree.nodes.new('ShaderNodeBsdfPrincipled')
    texture = tree.nodes.new('ShaderNodeTexImage')
    texture.image = image
    texture.extension = 'REPEAT'
    tree.links.new(texture.outputs['Color'], shader.inputs['Base Color'])
    tree.links.new(shader.outputs['BSDF'], output.inputs['Surface'])
    # NATIVE 的 Base Color 会把 ≈4% 分给镜面（R2-2 实测，见 DESIGN §35）——
    # 那是通道语义，不是采样层问题。这里量的是采样层，所以把它压到 0。
    specular = shader.inputs.get('Specular IOR Level')
    if specular is not None:
        specular.default_value = 0.0
    return material, shader, texture


plane_mesh = make_two_layer_plane()
plane = bpy.data.objects.new("Plane", plane_mesh)
scene.collection.objects.link(plane)
plane.select_set(True)
bpy.context.view_layer.objects.active = plane
plane_image = gradient_image("RebakeSource", 64)
plane_material, _shader, _texture = plain_material("RebakeMat", plane_image)
plane_mesh.materials.append(plane_material)
print("  平面 UV: {} / 渲染层 {}".format(
    [(layer.name, bool(layer.active_render)) for layer in plane_mesh.uv_layers],
    uvs.render_layer_name(plane)))

before_plane = snapshot([plane_material], [plane])


def run_rebake(from_value, to_value, size=64, bake_method='EMISSION'):
    """走**插件自己的**流水线（settings_to_bake_settings -> plan -> BakeJob）"""
    settings.workflow = uprops.WORKFLOW_REBAKE
    settings.bake_method = bake_method
    settings.rebake_from_uv = from_value
    settings.rebake_to_uv = to_value
    settings.rebake_maps.clear()
    uops.add_map(settings, "shader.base_color", size, dedupe=True)
    active = usession.Session.get()
    active.reset()
    if not active.start(bpy.context, settings):
        return None, None, active
    active.run_blocking()
    return active.job, pick_task(active.job), active

settings.rebake_from_uv = "UV1"
settings.rebake_to_uv = "UV2"
check("两个下拉被解析成 (UV1, UV2)",
      usession.rebake_uv_choices(settings) == ("UV1", "UV2"),
      usession.rebake_uv_choices(settings))
print("  统计：", usession.rebake_uv_stats(bpy.context, settings))

pinned_job, pinned_task, pinned_active = run_rebake("UV1", "UV2")
check("Re-bake（注入档）跑完没有失败",
      pinned_active.report is not None and pinned_active.report.failed == 0
      and pinned_active.report.done == 1,
      (pinned_active.report.summary() if pinned_active.report else None))
pinned_image = pinned_task.image
check("烘焙目标图存在且是 sRGB（颜色通道 -> 读数要先解码）",
      pinned_image is not None
      and pinned_image.colorspace_settings.name == 'sRGB',
      (pinned_image.size[:] if pinned_image else None,
       pinned_image.colorspace_settings.name if pinned_image else None))
pinned_center = linear_at_uv(pinned_image, 0.8, 0.8)
pinned_corner = linear_at_uv(pinned_image, 0.2, 0.2)
print("  注入档：写入层中心{} 覆盖区外{}".format(pinned_center, pinned_corner))
check("注入档：写入层中心读到的是**源层坐标** ≈ 0.2047",
      near_point(pinned_center, (0.2047, 0.2047, 0.0)), pinned_center)
check("注入档：写入层覆盖区外（0.20）没有内容", max(pinned_corner) < 0.02,
      pinned_corner)
pinned_detail = " | ".join(pinned_task.surgery_detail or [])
print("  注入档任务明细：", pinned_detail)
check("注入档：任务明细写明采样层被钉住了", "pinned on" in pinned_detail, pinned_detail)

control_job, control_task, control_active = run_rebake(uvs.NONE_UV, "UV2")
check("Re-bake（对照档）跑完没有失败",
      control_active.report is not None and control_active.report.failed == 0,
      (control_active.report.summary() if control_active.report else None))
control_center = linear_at_uv(control_task.image, 0.8, 0.8)
control_detail = " | ".join(control_task.surgery_detail or [])
print("  对照档：写入层中心{}".format(control_center))
print("  对照档任务明细：", control_detail)
check("对照档：读到的是**写入层坐标** ≈ 0.8047",
      near_point(control_center, (0.8047, 0.8047, 0.0)), control_center)
check("两档读数差 4 倍量级（判据可分辨）",
      abs(control_center[0] - pinned_center[0]) > 0.5,
      (pinned_center[0], control_center[0]))
check("对照档：任务明细里**没有**钉定记录",
      "pinned on" not in control_detail, control_detail)
check("对照档：写入层仍然被临时换过（To 生效了）",
      "write layer 'UV2'" in control_detail, control_detail)
check("写入层是 64x64（分辨率真的传到了引擎）",
      pick_task(control_job).image.size[0] == 64,
      pick_task(control_job).image.size[:])


# ====================================================================================
print("\n=== C · 零残留 ===")
diff_snapshot("两轮烘完之后", before_plane, snapshot([plane_material], [plane]))

# 派发**中途**失败：脚手架必须当场收回（不能靠"下一张任务顺手还原"）
settings.workflow = uprops.WORKFLOW_REBAKE
settings.rebake_maps.clear()
uops.add_map(settings, "shader.base_color", 16, dedupe=True)
settings.rebake_from_uv = "UV1"
settings.rebake_to_uv = "UV2"
fail_active = usession.Session.get()
fail_active.reset()
started = fail_active.start(bpy.context, settings)
check("能起一轮（为中途失败做准备）", started, fail_active.error)
observed = {}
fail_backend = fail_active.job.backend
real_run_bake = fail_backend._run_bake


def spy_run_bake(operator, kwargs):
    """在"烘焙即将发生"的那一刻取现场，然后模拟失败"""
    observed["uvmaps"] = sorted(node.uv_map for node in plane_material.node_tree.nodes
                                if node.type == 'UVMAP')
    observed["render"] = uvs.render_layer_name(plane)
    observed["nodes"] = len(plane_material.node_tree.nodes)
    raise RuntimeError("simulated failure inside the bake operator")


fail_backend._run_bake = spy_run_bake
fail_active.run_blocking()
fail_backend._run_bake = real_run_bake
print("  烘焙那一刻的现场：", observed)
check("烘焙期间：采样层的 UVMap 脚手架确实已经插上了",
      observed.get("uvmaps") == ["UV1"], observed)
check("烘焙期间：写入层确实临时换成了 To",
      observed.get("render") == "UV2", observed)
check("烘焙期间：材质里确实多了东西（不是在空跑）",
      observed.get("nodes", 0) > len(before_plane["trees"]["RebakeMat"]["nodes"]),
      (observed.get("nodes"), len(before_plane["trees"]["RebakeMat"]["nodes"])))
check("这一张被记为失败，没被吞掉",
      fail_active.report is not None and fail_active.report.failed == 1,
      (fail_active.report.summary() if fail_active.report else None))
diff_snapshot("派发中途失败之后", before_plane, snapshot([plane_material], [plane]))

# 直接量 backend 的三个状态：在位 / 还原 / "From == To 就什么都不做"
tx = txmod.SceneTransaction(bpy.context)
direct = bk.CyclesBackend(tx, margin=0, samples=1, save_directory=OUT, save_files=False)
direct.sample_uv, direct.write_uv = "UV1", "UV2"
direct._prepare_uv_layers([plane])
mid_nodes = len(plane_material.node_tree.nodes)
mid_render = uvs.render_layer_name(plane)
mid_uvmaps = sorted(node.uv_map for node in plane_material.node_tree.nodes
                    if node.type == 'UVMAP')
direct._revert_uv_layers()
direct.surgery.revert_all()
check("backend：钉上之后材质里确实多了一个 UVMap（uv_map = 采样层）",
      mid_uvmaps == ["UV1"] and mid_nodes > len(before_plane["trees"]["RebakeMat"]["nodes"]),
      (mid_uvmaps, mid_nodes))
check("backend：写入层被临时设成了 To", mid_render == "UV2", mid_render)
diff_snapshot("backend 还原之后", before_plane, snapshot([plane_material], [plane]))

direct.sample_uv, direct.write_uv = "UV1", "UV1"
direct._prepare_uv_layers([plane])
check("backend：采样层 == 写入层（且都等于当前渲染层）-> 真的一点都不改",
      uvs.render_layer_name(plane) == "UV1"
      and direct.uv_swap.swapped == 0 and not direct.uv_pin.records,
      (uvs.render_layer_name(plane), direct.uv_swap.swapped, len(direct.uv_pin.records)))
direct._revert_uv_layers()
diff_snapshot("From == To 之后", before_plane, snapshot([plane_material], [plane]))

direct.sample_uv, direct.write_uv = "UV2", "UV2"
direct._prepare_uv_layers([plane])
check("backend：采样层 == 写入层（但要写 UV2）-> 只换层、**不注入节点**",
      uvs.render_layer_name(plane) == "UV2" and not direct.uv_pin.records,
      (uvs.render_layer_name(plane), len(direct.uv_pin.records)))
direct._revert_uv_layers()
diff_snapshot("From == To == To 层之后", before_plane, snapshot([plane_material], [plane]))


# ====================================================================================
print("\n=== D · 边界 ===")

# (1) 缺 From 层：WARN 点名 + **不注入**
missing_pin = uvs.UVLayerPin()
applied = missing_pin.apply_objects([plane], "UV3")
check("缺层的物体：一个材质都没被钉", applied == 0 and not missing_pin.records,
      (applied, len(missing_pin.records)))
check("缺层被点名到物体", (plane.name, "UV3") in missing_pin.missing_layers,
      missing_pin.missing_layers)
check("缺层被点名到材质（不是静默跳过）",
      any("UV3" in reason for _name, reason in missing_pin.skipped),
      missing_pin.skipped)
diff_snapshot("缺层（不注入）之后", before_plane, snapshot([plane_material], [plane]))

stats_missing = uvs.layer_stats([plane], "UV3", "UV2")
check("layer_stats 数得清谁缺哪一层",
      stats_missing["missing_sample"] == [plane.name]
      and stats_missing["missing_write"] == []
      and stats_missing["layers"] == ["UV1", "UV2"],
      stats_missing)
check("layer_stats 知道要不要换渲染层", stats_missing["needs_swap"] == 1,
      stats_missing["needs_swap"])

# 再放一个**带着 UV3** 的物体：这样 UV3 才会出现在下拉里（动态枚举项），
# 而主平面没有它 —— 正是"From 层在某个物体上缺失"那条 WARN 的场景。
plane_b = bpy.data.objects.new("PlaneB", make_two_layer_plane("PlaneB", "UV2", "UV3",
                                                              render="UV2"))
scene.collection.objects.link(plane_b)
settings.rebake_from_uv = "UV3"
settings.rebake_to_uv = "UV2"
check("UV3 进了层名列表（下拉的枚举项跟着场景里的层走）",
      "UV3" in uvs.layer_names(usession.rebake_objects(bpy.context, settings)),
      uvs.layer_names(usession.rebake_objects(bpy.context, settings)))

rows = uops.rebake_checklist(bpy.context, settings)
check("Re-bake 清单里有 WARN 且点名缺层的物体",
      any(level == 'WARN' and plane.name in message and "UV3" in message
          for level, message in rows),
      [row for row in rows if row[0] == 'WARN'][:3])
check("Re-bake 清单里没有 Bake 流程那几条（准备 UV / 投影）",
      not any("Unwrap With SmartUV" in message or "Cage Extrusion" in message
              for _level, message in rows),
      [row for row in rows if row[0] == 'WARN'][:3])
settings.rebake_from_uv = "UV1"
settings.rebake_to_uv = "UV2"

# (2) Mapping：必须钉在它**之前**（R2-4：钉在之后会丢掉缩放）
def mapping_material(name, dangling):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    tree = material.node_tree
    for node in list(tree.nodes):
        tree.nodes.remove(node)
    output = tree.nodes.new('ShaderNodeOutputMaterial')
    shader = tree.nodes.new('ShaderNodeBsdfPrincipled')
    texture = tree.nodes.new('ShaderNodeTexImage')
    texture.image = plane_image
    mapping = tree.nodes.new('ShaderNodeMapping')
    mapping.inputs['Scale'].default_value = (2.0, 2.0, 2.0)
    tree.links.new(texture.outputs['Color'], shader.inputs['Base Color'])
    tree.links.new(shader.outputs['BSDF'], output.inputs['Surface'])
    tree.links.new(mapping.outputs['Vector'], texture.inputs['Vector'])
    if not dangling:
        uv_node = tree.nodes.new('ShaderNodeUVMap')
        uv_node.uv_map = "UV1"
        tree.links.new(uv_node.outputs['UV'], mapping.inputs['Vector'])
    return material, mapping, texture


mapped, mapping_node, mapped_texture = mapping_material("MappedMat", dangling=False)
mapped_before = tree_shape(mapped)
pin = uvs.UVLayerPin()
record = pin.apply_material(mapped, "UV2")
check("Mapping 链路：只改 uv_map，不新建节点",
      record is not None and not record.created and len(mapped.node_tree.nodes)
      == len(mapped_before["nodes"]),
      (record.created if record else None, len(mapped.node_tree.nodes)))
check("Mapping 链路：改的正是那个 UVMap 节点",
      record is not None and record.uv_maps
      == [(node.name, "UV1") for node in mapped.node_tree.nodes if node.type == 'UVMAP'],
      record.uv_maps if record else None)
check("Mapping 链路：采样是从 UV2 来的（钉子落在 Mapping **之前**）",
      any(node.type == 'UVMAP' and node.uv_map == "UV2"
          for node in mapped.node_tree.nodes), None)
pin.revert_all()
check("Mapping 链路：还原之后 uv_map 回到 UV1",
      tree_shape(mapped) == mapped_before, tree_shape(mapped))

dangling, dangling_mapping, dangling_texture = mapping_material("DanglingMat", dangling=True)
dangling_before = tree_shape(dangling)
pin = uvs.UVLayerPin()
pin.apply_material(dangling, "UV2")
new_nodes = [node for node in dangling.node_tree.nodes if node.type == 'UVMAP']
new_names = [node.name for node in new_nodes]
check("Mapping 的 Vector 悬空 -> 插的 UVMap 接在 **Mapping 的 Vector** 上（不是贴图上）",
      len(new_nodes) == 1
      and [link.from_node.name for link in dangling_mapping.inputs['Vector'].links]
      == new_names
      and [link.from_node.type for link in dangling_texture.inputs['Vector'].links]
      == ['MAPPING'],
      (new_names,
       [link.from_node.name for link in dangling_mapping.inputs['Vector'].links],
       [link.from_node.type for link in dangling_texture.inputs['Vector'].links]))
check("Mapping 的 Vector 悬空 -> 新节点的 uv_map 就是采样层",
      new_names and new_nodes[0].uv_map == "UV2", new_names and new_nodes[0].uv_map)
pin.revert_all()
check("Mapping 的 Vector 悬空 -> 还原之后逐项零差异",
      tree_shape(dangling) == dangling_before, tree_shape(dangling))

# (3) 其它坐标节点：**不动**，但要说出来
other = bpy.data.materials.new("OtherCoordMat")
other.use_nodes = True
other_tree = other.node_tree
for node in list(other_tree.nodes):
    other_tree.nodes.remove(node)
other_output = other_tree.nodes.new('ShaderNodeOutputMaterial')
other_shader = other_tree.nodes.new('ShaderNodeBsdfPrincipled')
other_texture = other_tree.nodes.new('ShaderNodeTexImage')
other_texture.image = plane_image
coord = other_tree.nodes.new('ShaderNodeTexCoord')
other_tree.links.new(other_texture.outputs['Color'], other_shader.inputs['Base Color'])
other_tree.links.new(other_shader.outputs['BSDF'], other_output.inputs['Surface'])
other_tree.links.new(coord.outputs['Object'], other_texture.inputs['Vector'])
other_before = tree_shape(other)
pin = uvs.UVLayerPin()
result = pin.apply_material(other, "UV2")
check("别的坐标节点：不钉（返回 None）且不新建节点",
      result is None and len(other.node_tree.nodes) == len(other_before["nodes"]),
      (result, len(other.node_tree.nodes)))
check("别的坐标节点：被记进 untouched（报告里说得出来）",
      any(node_type == 'TEX_COORD' for _mat, _name, node_type in pin.untouched),
      pin.untouched)
check("别的坐标节点：树形状零差异", tree_shape(other) == other_before, tree_shape(other))

# (4) 我们自己的烘焙目标节点不钉
target_only = bpy.data.materials.new("TargetOnlyMat")
target_only.use_nodes = True
target_tree = target_only.node_tree
target_node = target_tree.nodes.new('ShaderNodeTexImage')
target_node.image = plane_image
target_node.label = "MBakery Bake"
target_before = tree_shape(target_only)
pin = uvs.UVLayerPin()
check("烘焙目标节点（label = MBakery Bake）不会被钉",
      pin.apply_material(target_only, "UV2") is None and not pin.records,
      (len(pin.records), len(target_only.node_tree.nodes)))
check("烘焙目标节点：树形状零差异",
      tree_shape(target_only) == target_before, tree_shape(target_only))

# (5) 源尺寸：Re-bake 的 Maps 默认读源贴图
sized = bpy.data.images.new("Chair_BaseColor", 32, 16, alpha=False)
size_material = bpy.data.materials.new("SizeMat")
size_material.use_nodes = True
size_tree = size_material.node_tree
for node in list(size_tree.nodes):
    size_tree.nodes.remove(node)
size_output = size_tree.nodes.new('ShaderNodeOutputMaterial')
size_shader = size_tree.nodes.new('ShaderNodeBsdfPrincipled')
size_texture = size_tree.nodes.new('ShaderNodeTexImage')
size_texture.image = sized
size_tree.links.new(size_texture.outputs['Color'], size_shader.inputs['Base Color'])
size_tree.links.new(size_shader.outputs['BSDF'], size_output.inputs['Surface'])
found = uvs.source_size_for([plane], "shader.base_color")
print("  plane 的 base_color 源尺寸 ->", found)
check("source_size_for 顺着材质找得到那张贴图（64x64）",
      found is not None and found[:2] == (64, 64), found)


# ====================================================================================
print("\n=== G · 交付与预览的收尾（实施轮 1.1）===")
# 用户真机试用实施轮 1 之后的四条反馈，落在这里：
#   ① Compare 框跟 Preview / Show Original 是同一件事（删框 -> 面板套件里断言）
#   ② Re-bake 下预览要**连渲染层一起切**（用户原话："UV Maps 栏里 UV 没切过去"）
#   ③ Keep Only Baked 要真删旧 UV、新层改名 UVMap 当"正宫"（不可逆）
#   ④ Create Final Object 同样成品化，但在副本上做

from material_bakery.deliver import uv_layers as uvl


def select_only(target):
    """只选中这一个物体（交付测试必须把范围收窄，否则 preview/finalize 会作用到全场景）"""
    for obj in bpy.context.view_layer.objects:
        obj.select_set(False)
    target.select_set(True)
    bpy.context.view_layer.objects.active = target


def run_rebake_selected(target, from_value, to_value, size=32):
    """只烘一个物体的 Re-bake（MODE_SELECTED）"""
    settings.workflow = uprops.WORKFLOW_REBAKE
    settings.rebake_target_mode = ss.MODE_SELECTED
    settings.bake_method = 'EMISSION'
    settings.rebake_maps.clear()
    uops.add_map(settings, "shader.base_color", size, dedupe=True)
    select_only(target)                     # ⚠ 先选，再赋枚举（下拉的项来自选中的物体）
    settings.rebake_from_uv = from_value
    settings.rebake_to_uv = to_value
    active = usession.Session.get()
    active.reset()
    if not active.start(bpy.context, settings):
        return None, active
    active.run_blocking()
    return active, active


def op_call(operator, **kwargs):
    """调 operator；它会 report ERROR 时从 Python 侧抛异常，这里统一成 {'CANCELLED'}"""
    try:
        return operator(**kwargs)
    except RuntimeError as exc:
        print("      (operator 报错被拦: {})".format(exc))
        return {'CANCELLED'}


def layer_bounds(obj, name):
    """某一层的 UV 包围盒 —— 用来证明"留下的是新层而不是旧层\""""
    layer = obj.data.uv_layers.get(name)
    us = [item.uv[0] for item in layer.data]
    vs = [item.uv[1] for item in layer.data]
    return (min(us), min(vs), max(us), max(vs))


# ---- G1：预览连渲染层一起切（用户反馈 1 的正解）
plane_render_before = uvs.render_layer_name(plane)
g1, _job = run_rebake_selected(plane, "UV1", "UV2")
check("G1 起了一轮 Re-bake", g1 is not None and g1.report.failed == 0,
      g1.report.summary() if g1 and g1.report else None)
check("G1 会话记得住两个层名", g1.bake_layers() == ("UV1", "UV2"), g1.bake_layers())
g1_built, _reports = g1.build_materials(settings)
g1_added = g1.apply_materials(settings)
check("G1 建了交付材质并加了烘焙槽", g1_built > 0 and g1_added == 1,
      (g1_built, g1_added))
check("G1 加槽本身不动渲染层", uvs.render_layer_name(plane) == plane_render_before,
      (uvs.render_layer_name(plane), plane_render_before))

result = op_call(bpy.ops.mbakery.preview_baked, baked=True)
check("G1 Preview Baked 成功", result == {'FINISHED'}, result)
check("G1 预览后渲染层 = 写入层（To）", uvs.render_layer_name(plane) == "UV2",
      uvs.render_layer_name(plane))
check("G1 预览后 active_index 也指着写入层",
      plane.data.uv_layers.active_index == plane.data.uv_layers.find("UV2"),
      plane.data.uv_layers.active_index)
check("G1 UV Maps 栏里看到的就是新层（用户报的那条）",
      plane.data.uv_layers[plane.data.uv_layers.active_index].name == "UV2",
      plane.data.uv_layers[plane.data.uv_layers.active_index].name)

result = op_call(bpy.ops.mbakery.preview_baked, baked=False)
check("G1 Show Original 成功", result == {'FINISHED'}, result)
check("G1 Show Original 后渲染层回采样层（From）",
      uvs.render_layer_name(plane) == "UV1", uvs.render_layer_name(plane))
check("G1 Show Original 后 active_index 也回采样层",
      plane.data.uv_layers.active_index == plane.data.uv_layers.find("UV1"),
      plane.data.uv_layers.active_index)

# ---- G2：Keep Only Baked 成品化（就地、**不可逆**）
op_call(bpy.ops.mbakery.preview_baked, baked=True)
check("G2 收尾前渲染层在写入层上", uvs.render_layer_name(plane) == "UV2",
      uvs.render_layer_name(plane))
g2_done = g1.finalize_materials(settings)
check("G2 收尾成功", g2_done == 1, g2_done)
g2_names = [layer.name for layer in plane.data.uv_layers]
check("G2 收尾后只剩一层", g2_names == ["UVMap"], g2_names)
check("G2 收尾后渲染层就是那一层",
      uvs.render_layer_name(plane) == "UVMap", uvs.render_layer_name(plane))
check("G2 收尾后 active_index = 0", plane.data.uv_layers.active_index == 0,
      plane.data.uv_layers.active_index)
g2_bounds = layer_bounds(plane, "UVMap")
print("  G2 保留层的 UV 包围盒:", tuple(round(v, 3) for v in g2_bounds))
check("G2 **留下的是写入层的内容**（UV 落在 0.7–0.9，不是源层的 0.1–0.3）",
      near(g2_bounds[0], 0.7, 0.001) and near(g2_bounds[2], 0.9, 0.001),
      tuple(round(v, 3) for v in g2_bounds))
check("G2 收尾后只剩一个材质槽", len(plane.material_slots) == 1,
      [slot.material.name for slot in plane.material_slots])
check("G2 日志写明了不可逆、且 UV 层回不来",
      any("DESTRUCTIVE" in entry.message and "do NOT come back" in entry.message
          for entry in g1.log), [e.message for e in g1.log][-3:])

result = op_call(bpy.ops.mbakery.restore_original_material)
check("G2 Remove Baked Slot 成功", result == {'FINISHED'}, result)
check("G2 Remove 之后材质槽回来了（原材质一个）",
      [slot.material.name for slot in plane.material_slots] == ["RebakeMat"],
      [slot.material.name for slot in plane.material_slots])
check("G2 但 **UV 层没有回来**（测试明写这一条：收尾不可逆）",
      [layer.name for layer in plane.data.uv_layers] == ["UVMap"],
      [layer.name for layer in plane.data.uv_layers])
check("G2 被删掉的层名（UV1/UV2）一个都不在了",
      not any(plane.data.uv_layers.get(name) for name in ("UV1", "UV2")), g2_names)

# ---- G3：守卫生效 —— 材质引用了要删的旧层
guard_mesh = make_two_layer_plane("GuardMesh", "Old", "New", render="Old")
guard_obj = bpy.data.objects.new("GuardObj", guard_mesh)
scene.collection.objects.link(guard_obj)
guard_material, _gshader, guard_texture = plain_material("GuardMat", plane_image)
# 用户自己的节点：把贴图钉在**旧层**上（收尾时将被删的那一层）
guard_uvmap = guard_material.node_tree.nodes.new('ShaderNodeUVMap')
guard_uvmap.uv_map = "Old"
guard_material.node_tree.links.new(guard_uvmap.outputs['UV'],
                                   guard_texture.inputs['Vector'])
guard_mesh.materials.append(guard_material)

g3, _job = run_rebake_selected(guard_obj, "Old", "New")
check("G3 起了一轮 Re-bake", g3 is not None and g3.report.failed == 0,
      g3.report.summary() if g3 and g3.report else None)
check("G3 烘完渲染层还回 Old（本轮精确还原的那一条）",
      uvs.render_layer_name(guard_obj) == "Old", uvs.render_layer_name(guard_obj))
check("G3 用户自己那个 UVMap 节点的 uv_map 没被改坏",
      guard_uvmap.uv_map == "Old", guard_uvmap.uv_map)
g3.build_materials(settings)
g3.apply_materials(settings)
op_call(bpy.ops.mbakery.preview_baked, baked=True)
check("G3 预览把渲染层切到了新层", uvs.render_layer_name(guard_obj) == "New",
      uvs.render_layer_name(guard_obj))
g3_done = g3.finalize_materials(settings)
check("G3 收尾成功（槽那一半照旧）", g3_done == 1, g3_done)
g3_names = [layer.name for layer in guard_obj.data.uv_layers]
print("  G3 收尾后的层:", g3_names)
check("G3 守卫拦住了：被材质引用的旧层**没被删**", "Old" in g3_names, g3_names)
check("G3 保留层仍按写入层，并改名成 UVMap", sorted(g3_names) == ["Old", "UVMap"],
      g3_names)
check("G3 日志点名了是哪个材质引用的",
      any("is referenced by material GuardMat" in entry.message for entry in g3.log),
      [e.message for e in g3.log][-4:])
check("G3 材质槽收尾照旧（只剩烘焙槽）", len(guard_obj.material_slots) == 1,
      [slot.material.name for slot in guard_obj.material_slots])

# ---- G4：Create Final Object 成品化（副本路径，原物体一分不动）
copy_mesh = make_two_layer_plane("CopyMesh", "A", "B", render="A")
copy_obj = bpy.data.objects.new("CopyObj", copy_mesh)
scene.collection.objects.link(copy_obj)
copy_material, _cshader, _ctexture = plain_material("CopyMat", plane_image)
copy_mesh.materials.append(copy_material)

g4, _job = run_rebake_selected(copy_obj, "A", "B")
check("G4 起了一轮 Re-bake", g4 is not None and g4.report.failed == 0,
      g4.report.summary() if g4 and g4.report else None)
g4.build_materials(settings)
g4.apply_materials(settings)
# 先预览一次：让原物体上留下"渲染层备份"，再复制 —— 副本不该继承那份账
op_call(bpy.ops.mbakery.preview_baked, baked=True)
check("G4 原物体上确实有渲染层备份（刚预览过）",
      uvl.PROP_ORIG_RENDER_UV in copy_obj, copy_obj.get(uvl.PROP_ORIG_RENDER_UV))
orig_layers = [layer.name for layer in copy_mesh.uv_layers]
orig_render = uvs.render_layer_name(copy_obj)
orig_slots = [slot.material.name for slot in copy_obj.material_slots]
result = op_call(bpy.ops.mbakery.create_final_objects)
check("G4 复制成功", result == {'FINISHED'}, result)
check("G4 复制出 1 个副本", len(g4.created_objects) == 1,
      [o.name for o in g4.created_objects])
final_copy = g4.created_objects[0] if g4.created_objects else None
if final_copy is not None:
    copy_layers = [layer.name for layer in final_copy.data.uv_layers]
    check("G4 副本只剩一层且名字是 UVMap", copy_layers == ["UVMap"], copy_layers)
    copy_bounds = layer_bounds(final_copy, "UVMap")
    print("  G4 副本保留层的 UV 包围盒:", tuple(round(v, 3) for v in copy_bounds))
    check("G4 副本留下的是写入层的内容（UV 落在 0.7–0.9）",
          near(copy_bounds[0], 0.7, 0.001) and near(copy_bounds[2], 0.9, 0.001),
          tuple(round(v, 3) for v in copy_bounds))
    check("G4 副本的渲染层指着 UVMap",
          uvs.render_layer_name(final_copy) == "UVMap",
          uvs.render_layer_name(final_copy))
    check("G4 副本的网格是自己的（不共享）", final_copy.data is not copy_mesh,
          (final_copy.data.name, copy_mesh.name))
    check("G4 原物体的 UV 层名与层数**逐项不变**",
          [layer.name for layer in copy_mesh.uv_layers] == orig_layers,
          [layer.name for layer in copy_mesh.uv_layers])
    check("G4 原物体的渲染层不变", uvs.render_layer_name(copy_obj) == orig_render,
          (uvs.render_layer_name(copy_obj), orig_render))
    check("G4 原物体的材质槽不变",
          [slot.material.name for slot in copy_obj.material_slots] == orig_slots,
          [slot.material.name for slot in copy_obj.material_slots])
    check("G4 副本不继承原物体的渲染层备份（不会误以为还欠一次还原）",
          uvl.PROP_ORIG_RENDER_UV not in final_copy,
          final_copy.get(uvl.PROP_ORIG_RENDER_UV))
    check("G4 原物体自己的备份还在（它还在预览状态）",
          uvl.PROP_ORIG_RENDER_UV in copy_obj,
          copy_obj.get(uvl.PROP_ORIG_RENDER_UV))

# ---- G5：Bake 流程一个字都不改（预览不碰渲染层）
# 用一个**全新**的物体：副本上已经有 G4 留下的渲染层备份，拿它测"没写备份"是假的。
bake_mesh = make_two_layer_plane("BakeMesh", "A", "B", render="A")
bake_obj = bpy.data.objects.new("BakeObj", bake_mesh)
scene.collection.objects.link(bake_obj)
bake_material, _bshader, _btexture = plain_material("BakeMat", plane_image)
bake_mesh.materials.append(bake_material)

settings.workflow = uprops.WORKFLOW_BAKE
settings.maps.clear()
uops.add_map(settings, "shader.base_color", 32, dedupe=True)
settings.target_mode = ss.MODE_SELECTED
select_only(bake_obj)
g5 = usession.Session.get()
g5.reset()
check("G5 Bake 流程能起一轮", g5.start(bpy.context, settings), g5.error)
g5.run_blocking()
check("G5 Bake 流程的 bake_layers 是两个空串", g5.bake_layers() == ("", ""),
      g5.bake_layers())
g5.build_materials(settings)
g5.apply_materials(settings)
render_before_g5 = uvs.render_layer_name(bake_obj)
op_call(bpy.ops.mbakery.preview_baked, baked=True)
check("G5 Bake 流程预览**不动**渲染层",
      uvs.render_layer_name(bake_obj) == render_before_g5,
      (uvs.render_layer_name(bake_obj), render_before_g5))
check("G5 Bake 流程没写渲染层备份（一个字节都没动）",
      uvl.PROP_ORIG_RENDER_UV not in bake_obj,
      bake_obj.get(uvl.PROP_ORIG_RENDER_UV))
check("G5 Bake 流程预览后 UV 层数不变",
      len(bake_obj.data.uv_layers) == 2,
      [layer.name for layer in bake_obj.data.uv_layers])


# ====================================================================================
print("\n=== F · 曲面真机（square_bush.blend / BushSquare，只读）===")

BUSH = os.path.join(WS, "_samples", "square_bush.blend")
CURVED = os.path.join(OUT, "curved")
os.makedirs(CURVED, exist_ok=True)


def rasterize(mesh, write_layer, source_layer, resolution):
    """把写入层逐三角光栅化，算出每个 texel 的归属（R2-5 发明的判据，这里复用）

    返回 {"unique": {texel: {"write": (u,v), "source": (u,v)}}, "covered": n}
    —— 只保留**只有一个面覆盖**的 texel：重叠层上那些"多个面抢同一个格子"的
    texel 本来就没有唯一的期望值，拿它们比 MAE 等于自己骗自己。
    """
    write = [(item.uv[0], item.uv[1]) for item in write_layer.data]
    source = [(item.uv[0], item.uv[1]) for item in source_layer.data]
    counts = bytearray(resolution * resolution)
    unique = {}
    for polygon in mesh.polygons:
        loops = list(polygon.loop_indices)
        if len(loops) < 3:
            continue
        for k in range(1, len(loops) - 1):
            tri = (loops[0], loops[k], loops[k + 1])
            points = [write[i] for i in tri]
            xs = [p[0] * resolution for p in points]
            ys = [p[1] * resolution for p in points]
            min_x = max(0, int(min(xs)))
            max_x = min(resolution - 1, int(max(xs)) + 1)
            min_y = max(0, int(min(ys)))
            max_y = min(resolution - 1, int(max(ys)) + 1)
            if min_x > max_x or min_y > max_y:
                continue
            (ax, ay), (bx, by), (cx, cy) = points
            denominator = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
            if abs(denominator) < 1e-12:
                continue
            for y in range(min_y, max_y + 1):
                for x in range(min_x, max_x + 1):
                    px = (x + 0.5) / resolution
                    py = (y + 0.5) / resolution
                    wa = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / denominator
                    wb = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / denominator
                    wc = 1.0 - wa - wb
                    if wa < -1e-9 or wb < -1e-9 or wc < -1e-9:
                        continue
                    index = y * resolution + x
                    counts[index] = min(255, counts[index] + 1)
                    if counts[index] == 1:
                        weights = (wa, wb, wc)
                        unique[index] = {
                            "write": tuple(sum(weights[j] * write[tri[j]][axis]
                                               for j in range(3)) for axis in (0, 1)),
                            "source": tuple(sum(weights[j] * source[tri[j]][axis]
                                                for j in range(3)) for axis in (0, 1)),
                        }
                    else:
                        unique.pop(index, None)
    return {"unique": unique, "covered": sum(1 for value in counts if value),
            "resolution": resolution}


def measure(image, raster, label):
    """唯一归属 texel 上的 MAE：烘出来的值更像**源层坐标**还是**写入层坐标**

    源图是坐标编码图（R = u），所以"这个 texel 该读到什么"就是那一层的 u ——
    两个假设的期望值都算得出来，比 MAE 与命中率即可（R2-5 的判据）。
    """
    _width, _height, buffer = read_pixels(image)
    rows = []
    for index, data in raster["unique"].items():
        got = srgb_decode(buffer[index * 4])
        rows.append((got, data["source"][0], data["write"][0]))
    if not rows:
        return {"label": label, "unique_texels": 0}
    mae_source = sum(abs(got - source) for got, source, _w in rows) / len(rows)
    mae_write = sum(abs(got - write) for got, _s, write in rows) / len(rows)
    hits = sum(1 for got, source, _w in rows if abs(got - source) <= 0.05) / len(rows)
    hits_write = sum(1 for got, _s, write in rows if abs(got - write) <= 0.05) / len(rows)
    return {"label": label, "unique_texels": len(rows),
            "mae_vs_source": round(mae_source, 4), "mae_vs_write": round(mae_write, 4),
            "hit_rate_source": round(hits, 4), "hit_rate_write": round(hits_write, 4)}


def curved_run(target, bake_type_key, sample_uv, write_uv, bake_method, size, folder):
    """真机曲面上跑一张：**只烘这一个物体**（MODE_SELECTED + 只选它）

    ⚠ square_bush.blend 里有几十个集合、几百个物体：走 MODE_COLLECTIONS 会编译出
      一大堆任务（大部分物体没有材质、注定失败），那不是这一节要测的东西 ——
      而且"任务 0"也不再是 BushSquare。
    """
    for obj in bpy.context.view_layer.objects:
        obj.select_set(False)
    target.select_set(True)
    bpy.context.view_layer.objects.active = target
    data = pl.BakeSettings(
        target_mode=ss.MODE_SELECTED,
        maps=pl.map_requests_from_pairs([(bake_type_key, size)]),
        file_format='PNG', use_subfolders=False, pack_into_blend=True,
        fake_user=True, margin=0, antialias='OFF', bake_method=bake_method,
        workflow="rebake", sample_uv=sample_uv, write_uv=write_uv,
    )
    plan = pl.build_plan(bpy.context, data)
    tx = txmod.SceneTransaction(bpy.context)
    backend = bk.CyclesBackend(tx, margin=0, samples=1, save_directory=folder,
                               save_files=True)
    job = jb.BakeJob(bpy.context, plan, data, backend=backend, transaction=tx)
    job.run_to_completion()
    return job


if not os.path.isfile(BUSH):
    print("  跳过曲面那一节：{} 不在（那是用户样本，不进版本库）".format(BUSH))
    print("  SKIPPED (curved section: square_bush.blend not available)")
else:
    bpy.ops.wm.open_mainfile(filepath=BUSH)
    scene = bpy.context.scene
    bush = bpy.data.objects.get("BushSquare")
    check("样本里有 BushSquare", bush is not None)
    # ⚠ 只读：内存副本 + 一个存盘 API 都不调
    bush.data = bush.data.copy()
    if bush.name not in {obj.name for obj in bpy.context.view_layer.objects}:
        scene.collection.objects.link(bush)
        print("  原集合被排除在视图层外 -> 临时挂进场景集合（只改内存）")
    bush_layers = [layer.name for layer in bush.data.uv_layers]
    print("  BushSquare：{} 面 / UV 层 {}".format(len(bush.data.polygons), bush_layers))
    check("真机网格有两层 UV（跨层测试的前提）", len(bush_layers) >= 2, bush_layers)

    curved_source = bush_layers[0]
    curved_write = next((name for name in bush_layers if name != curved_source), None)
    usable = all(abs(bush.data.uv_layers.get(curved_source).data[i].uv[axis]
                     - bush.data.uv_layers.get(curved_write).data[i].uv[axis]) < 1e-6
                 for i in range(0, len(bush.data.uv_layers.get(curved_source).data), 97)
                 for axis in (0, 1))
    check("两层 UV 不是逐点相同（否则判据没有区分能力）", not usable,
          (curved_source, curved_write))

    # 材质换成"Principled + 坐标编码图"（Vector 不接东西 -> 采样层由钉子决定）
    bush.data.materials.clear()
    curved_image = gradient_image("CurvedSource", 128)
    curved_material, _cs, _ct = plain_material("CurvedMat", curved_image)
    bush.data.materials.append(curved_material)
    for polygon in bush.data.polygons:
        polygon.material_index = 0
    # 初始渲染层 = 源层（用户的现状就是"我现在按这层渲染"）
    for layer in bush.data.uv_layers:
        layer.active_render = (layer.name == curved_source)
    bush.data.uv_layers.active_index = bush.data.uv_layers.find(curved_source)

    RES = 128
    raster = rasterize(bush.data, bush.data.uv_layers.get(curved_write),
                       bush.data.uv_layers.get(curved_source), RES)
    print("  光栅化（{}²）：写入层覆盖 {} texel，唯一归属 {} 个".format(
        RES, raster["covered"], len(raster["unique"])))
    check("唯一归属 texel 够多（判据站得住）", len(raster["unique"]) > 100,
          len(raster["unique"]))

    curved_before = snapshot([curved_material], [bush])
    for bake_method in ('EMISSION', 'NATIVE'):
        for pinned in (True, False):
            label = "{}_{}".format(bake_method, "pinned" if pinned else "control")
            folder = os.path.join(CURVED, label)
            os.makedirs(folder, exist_ok=True)
            job = curved_run(bush, "shader.base_color",
                             curved_source if pinned else "",
                             curved_write, bake_method, RES, folder)
            task = pick_task(job, bush.name)
            detail = " | ".join(task.surgery_detail or [])
            image = task.image
            found = measure(image, raster, label)
            print("  {}: 失败 {} / {}  明细 {}  读数 {}".format(
                label, job.report.failed, job.report.done, detail, found))
            check("{}：只编译出 BushSquare 一个任务".format(label),
                  len(job.plan.tasks) == 1, len(job.plan.tasks))
            check("{}：烘焙成功".format(label),
                  job.report.failed == 0 and job.report.done == 1,
                  job.report.summary())
            if pinned:
                check("{}：对**源层** MAE < 0.01".format(label),
                      found.get("mae_vs_source", 9) < 0.01, found)
                check("{}：对**源层**命中率 > 95%".format(label),
                      found.get("hit_rate_source", 0) > 0.95, found)
                check("{}：明细写明采样层被钉住".format(label),
                      "pinned on" in detail, detail)
            else:
                check("{}（对照）：对**写入层** MAE < 0.01".format(label),
                      found.get("mae_vs_write", 9) < 0.01, found)
                check("{}（对照）：明细里没有钉定记录".format(label),
                      "pinned on" not in detail, detail)
    diff_snapshot("曲面真机四轮之后", curved_before, snapshot([curved_material], [bush]))

    stray = glob.glob(os.path.join(WS, "_samples", "*.blend1"))
    check("_samples/ 里没有多出 .blend1（只读打开，没存过盘）", not stray, stray)


# ====================================================================================
print("\n=== 结果 ===")
if FAIL:
    print("FAILED {} check(s):".format(len(FAIL)))
    for name in FAIL:
        print("  -", name)
else:
    print("ALL CHECKS PASSED")

"""Material Bakery — 专属参数接线 + Selected to Active（重拓补整合的缝）

这一套盯两件"宣传了但可能用不了"的事：

1. **每个烘焙项的专属参数有没有真的到引擎**
   通道打包的 R/G/B、AO 距离、UV 层名、颜色属性名、法线空间。
   只画控件不接线的话，用户改了却毫无效果 —— 比不显示控件更糟。

2. **Selected to Active 的缝是不是真的通了**
   目标（低模）和源（高模）分开、两者都被选中、use_selected_to_active 打开、
   光线距离传下去、隐藏的高模会被临时显示出来再还原。
"""
import bpy, sys, os, shutil, ast

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
from material_bakery.engine import providers as pv
from material_bakery.engine import surgery as sg
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


OUT = os.path.join(WS, "_probe", "s2a")
if os.path.isdir(OUT):
    shutil.rmtree(OUT)
os.makedirs(OUT)

material_bakery.register()


def quad(name, uv=(0.0, 0.0, 1.0, 1.0), color=(1.0, 0.0, 0.0, 1.0), link=True):
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
    mesh.update()
    layer = mesh.uv_layers.new(name="UVMap")
    u0, v0, u1, v1 = uv
    for i, corner in enumerate(((u0, v0), (u1, v0), (u1, v1), (u0, v1))):
        layer.data[i].uv = corner
    material = bpy.data.materials.new(name + "Mat")
    material.use_nodes = True
    for node in material.node_tree.nodes:
        if node.type == 'BSDF_PRINCIPLED':
            node.inputs["Base Color"].default_value = color
            node.inputs["Roughness"].default_value = 0.4
    mesh.materials.append(material)
    obj = bpy.data.objects.new(name, mesh)
    if link:
        bpy.context.scene.collection.objects.link(obj)
    return obj


print("=== 阶段 A：专属参数从 UI 一路传到引擎 ===")
bpy.ops.wm.read_factory_settings(use_empty=True)
quad("Tester")          # 计划编译需要场景里至少有一个可烘物体
settings = bpy.context.scene.mbakery
settings.maps.clear()

item = uops.add_map(settings, "misc.channel_packing", 64, dedupe=True)
check("加了通道打包项", item is not None and settings.maps[0].type_key == "misc.channel_packing")
check("默认 R/G/B 就是 ORM 那套",
      (item.channel_r, item.channel_g, item.channel_b)
      == ("shader.roughness", "shader.metallic", "misc.ao"),
      (item.channel_r, item.channel_g, item.channel_b))

item.channel_r = "shader.base_color"
item.channel_g = "shader.ao" if bt.get("shader.ao") else "shader.ior"
item.channel_b = "shader.alpha"
options = item.options()
print("  通道打包 options:", options)
check("options() 带出三个通道",
      options["channel_r"] == "shader.base_color" and options["channel_b"] == "shader.alpha",
      options)

requests = settings.map_requests()
check("map_requests 返回三元组（带参数）",
      len(requests[0]) == 4, requests)
check("参数进了 requests", requests[0][3]["channel_r"] == "shader.base_color",
      requests[0][2])

bake_settings = usession.settings_to_bake_settings(settings)
plan = pl.build_plan(bpy.context, bake_settings)
check("参数一路进了计划",
      plan.tasks[0].options.get("channel_r") == "shader.base_color",
      plan.tasks[0].options)

# UV / 颜色属性 / AO / Pointiness / 法线空间
settings.maps.clear()
uv_item = uops.add_map(settings, "standard.uv", 64, dedupe=True)
uv_item.uv_map = "MyUV"
check("UV 类型带出层名", uv_item.options() == {"uv_map": "MyUV"}, uv_item.options())

attr_item = uops.add_map(settings, "misc.color_attribute", 64, dedupe=True)
attr_item.attribute_name = "Paint"
check("颜色属性带出属性名",
      attr_item.options() == {"attribute_name": "Paint"}, attr_item.options())

ao_item = uops.add_map(settings, "misc.ao", 64, dedupe=True)
ao_item.ao_distance = 0.35
ao_item.ao_only_local = True
ao_options = ao_item.options()
check("AO 带出距离与只算自身",
      abs(ao_options["ao_distance"] - 0.35) < 1e-5 and ao_options["ao_only_local"] is True,
      ao_options)

point_item = uops.add_map(settings, "misc.pointiness", 64, dedupe=True)
point_item.pointiness_contrast = 2.0
check("Pointiness 带出对比度",
      point_item.options()["pointiness_contrast"] == 2.0, point_item.options())

normal_item = uops.add_map(settings, "standard.normal", 64, dedupe=True)
normal_item.normal_space = 'OBJECT'
check("法线带出空间", normal_item.options() == {"normal_space": "OBJECT"},
      normal_item.options())

plain_item = uops.add_map(settings, "shader.metallic", 64, dedupe=True)
check("普通类型没有多余参数", plain_item.options() == {}, plain_item.options())
check("uses_normal_space 只对法线为真",
      normal_item.uses_normal_space and not plain_item.uses_normal_space)

check("每个类型都能算出 options 而不抛异常",
      all(uops.add_map(settings, key, 32, dedupe=True).options() is not None
          for key in list(bt.REGISTRY)[:12]))

print("\n=== 阶段 B：法线空间真的传给了烘焙操作符 ===")
settings.maps.clear()
item = uops.add_map(settings, "standard.normal", 32, dedupe=True)
item.normal_space = 'OBJECT'
bake_settings = usession.settings_to_bake_settings(settings)
plan = pl.build_plan(bpy.context, bake_settings)
check("计划里记了 OBJECT 空间",
      plan.tasks[0].options.get("normal_space") == "OBJECT",
      plan.tasks[0].options)

captured = {}


class CapturingBackend(bk.CyclesBackend):
    """把传给烘焙操作符的参数截下来看。

    ⚠ 不能去 patch bpy.ops.object.bake —— bpy.ops 是**懒命名空间**，
      往里写属性不生效（读回来还是原来的操作符），第一次就是这么假失败的。
      后端为此留了 _run_bake 这个正经接缝，覆写它就行。
    """

    def _run_bake(self, operator, kwargs):
        captured.update(kwargs)
        return bk.CyclesBackend._run_bake(self, operator, kwargs)


mesh_obj = quad("Norm")
tx = txmod.SceneTransaction(bpy.context)
spy_backend = CapturingBackend(tx, margin=2, samples=1)
spy_job = jb.BakeJob(bpy.context, plan, bake_settings, backend=spy_backend,
                     transaction=tx)
spy_job.run_to_completion()
print("  传给 bake 的参数:", {k: v for k, v in captured.items()
                             if k in ("type", "normal_space", "use_selected_to_active",
                                      "margin", "max_ray_distance", "cage_extrusion")})
check("normal_space 传下去了", captured.get("normal_space") == "OBJECT",
      captured.get("normal_space"))
check("没有源时不开投影", captured.get("use_selected_to_active") is False,
      captured.get("use_selected_to_active"))
check("烘焙成功", spy_job.report.failed == 0, spy_job.report.summary())

print("\n=== 阶段 C：provider 协议 ===")
check("默认 provider 是 DefaultProvider", pv.DefaultProvider().name == "default")
check("投影 provider 存在", pv.ProjectionProvider(settings).name == "projection")
check("没注册、没开投影时用默认",
      isinstance(pv.active_provider(settings), pv.DefaultProvider))

settings.use_selected_to_active = True
check("开了投影就用投影 provider",
      isinstance(pv.active_provider(settings), pv.ProjectionProvider))
settings.use_selected_to_active = False

class DummyProvider(pv.BakeProvider):
    name = "dummy"

    def resolve_targets(self, context, settings, gate_lookup=None):
        return [], []

    def resolve_sources(self, group, settings, context):
        return []

dummy = DummyProvider()
previous = pv.set_provider(dummy)
check("第三方能注册 provider", pv.get_provider() is dummy)
check("注册后优先级最高", pv.active_provider(settings) is dummy)
pv.set_provider(previous)
check("能恢复", pv.get_provider() is previous)
check("provider 接口有默认实现（只有 resolve_targets 必须写）",
      pv.BakeProvider().resolve_sources(None, None, None) == []
      and pv.BakeProvider().extra_bake_params(None) == {})

print("\n=== 阶段 D：sources 真的被用起来 ===")
bpy.ops.wm.read_factory_settings(use_empty=True)
# 目标组（低模）在 Body 集合里；高模**不在**这个集合里 ——
# 这才是真实的重拓补场景：插件产出低模，高模是另外的物体。
low = quad("Body_low", color=(0.1, 0.2, 0.9, 1.0), link=False)
high = quad("Body_high", color=(0.9, 0.1, 0.1, 1.0), link=False)
collection = bpy.data.collections.new("Body")
bpy.context.scene.collection.children.link(collection)
collection.objects.link(low)
bpy.context.scene.collection.objects.link(high)

settings = bpy.context.scene.mbakery
settings.maps.clear()
settings.target_mode = ss.MODE_SINGLE
settings.single_collection = "Body"
settings.use_selected_to_active = True
settings.source_mode = 'PATTERN'
settings.source_pattern = "high"
settings.max_ray_distance = 0.2
settings.cage_extrusion = 0.01
uops.add_map(settings, "shader.base_color", 32, dedupe=True)

bake_settings = usession.settings_to_bake_settings(settings)
provider = pv.ProjectionProvider(settings)
plan = pl.build_plan(bpy.context, bake_settings, provider=provider)
task = plan.tasks[0]
print("  目标:", [o.name for o in task.targets], " 源:", [o.name for o in task.sources])
check("目标是低模", [o.name for o in task.targets] == ["Body_low"],
      [o.name for o in task.targets])
check("源是高模", [o.name for o in task.sources] == ["Body_high"],
      [o.name for o in task.sources])
check("任务知道自己要投影", task.projects)
check("bake_params 带上了光线距离与 cage",
      abs(task.bake_params.get("max_ray_distance", 0.0) - 0.2) < 1e-5
      and abs(task.bake_params.get("cage_extrusion", 0.0) - 0.01) < 1e-5,
      task.bake_params)

# 源模式：OTHERS
settings.source_mode = 'OTHERS'
plan2 = pl.build_plan(bpy.context, usession.settings_to_bake_settings(settings),
                      provider=pv.ProjectionProvider(settings))
check("OTHERS 模式把组外的物体都当源",
      [o.name for o in plan2.tasks[0].sources] == ["Body_high"],
      [o.name for o in plan2.tasks[0].sources])

# 源模式：PATTERN 但pattern 不匹配
settings.source_mode = 'PATTERN'
settings.source_pattern = "nothing_matches_this"
plan3 = pl.build_plan(bpy.context, usession.settings_to_bake_settings(settings),
                      provider=pv.ProjectionProvider(settings))
check("pattern 不匹配时退回自己烘（不投影）",
      not plan3.tasks[0].projects,
      ([o.name for o in plan3.tasks[0].sources], plan3.tasks[0].projects))

settings.source_pattern = "high"

print("\n=== 阶段 E：真的按投影烘，且选中/隐藏被还原 ===")
bpy.ops.wm.read_factory_settings(use_empty=True)
low = quad("Body_low", color=(0.1, 0.2, 0.9, 1.0), link=False)
high = quad("Body_high", color=(0.9, 0.1, 0.1, 1.0), link=False)
collection = bpy.data.collections.new("Body")
bpy.context.scene.collection.children.link(collection)
collection.objects.link(low)
bpy.context.scene.collection.objects.link(high)
high.hide_set(True)          # 高模通常藏着 —— 这就是最容易踩的那个坑
# ⚠ 高模必须离低模有一点距离。两个完全重合的共面四边形，
#   投影光线长度是 0，打不到任何东西 —— 烘出来全黑，而且不报错。
high.location.z = 0.1

settings = bpy.context.scene.mbakery
settings.maps.clear()
settings.target_mode = ss.MODE_SINGLE
settings.single_collection = "Body"
settings.use_selected_to_active = True
settings.source_mode = 'PATTERN'
settings.source_pattern = "high"
settings.max_ray_distance = 0.3
# ⚠ Cage Extrusion 是**必需**的。实测：只设 Ray Distance（max_ray_distance）
#   光线打不到高模，烘出来是一张纯黑图，而且不报错。
#   探针对照见本文件末尾的说明与 design 文档 §12。
settings.cage_extrusion = 0.25
settings.unhide_sources = True
uops.add_map(settings, "shader.base_color", 32, dedupe=True)

# 清单必须把"两个都是 0"这件事说出来，而不是让用户对着黑图猜
probe = bpy.context.scene.mbakery
saved_extrusion = probe.cage_extrusion
probe.cage_extrusion = 0.0
probe.max_ray_distance = 0.0
rows = uops.checklist(bpy.context, probe)
check("Cage/Ray 都是 0 时清单给出警告",
      any(level == 'WARN' and 'reach the source' in message for level, message in rows),
      [m for _l, m in rows])
probe.cage_extrusion = saved_extrusion
probe.max_ray_distance = 0.3
rows = uops.checklist(bpy.context, probe)
check("配好之后清单里没有投影相关的警告",
      not [m for level, m in rows if level == 'WARN' and 'High-Poly' in m],
      [m for _l, m in rows])

bake_settings = usession.settings_to_bake_settings(settings)
provider = pv.ProjectionProvider(settings)
plan = pl.build_plan(bpy.context, bake_settings, provider=provider)
check("隐藏的高模也被当成源（is_bakeable 只用于目标）",
      [o.name for o in plan.tasks[0].sources] == ["Body_high"],
      [o.name for o in plan.tasks[0].sources])

captured.clear()
tx = txmod.SceneTransaction(bpy.context)
spy_backend = CapturingBackend(tx, margin=2, samples=1, save_directory=OUT,
                               save_files=True)
spy_job = jb.BakeJob(bpy.context, plan, bake_settings, backend=spy_backend,
                     transaction=tx)
spy_job.run_to_completion()
for line in spy_job.report.details():
    print("  " + line)
print("  传给 bake 的参数:", {k: captured.get(k) for k in
                             ("type", "use_selected_to_active", "max_ray_distance")})
check("投影烘焙被打开", captured.get("use_selected_to_active") is True,
      captured.get("use_selected_to_active"))
check("投影烘焙成功", spy_job.report.failed == 0,
      [r.error for r in spy_job.report.failed_results()])
check("烘完高模又藏回去了", high.hide_get() is True, high.hide_get())
check("烘完选中状态被还原", not low.select_get() and not high.select_get(),
      (low.select_get(), high.select_get()))
check("UV 没被动（投影不改 UV）",
      [(round(i.uv[0], 4), round(i.uv[1], 4)) for i in low.data.uv_layers[0].data]
      == [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])

print("\n=== 阶段 F：投影确实采到了高模 ===")
# 高模是纯红、低模是蓝。投影烘出来的应当偏红 —— 证明采的是源不是自己。
baked = plan.tasks[0].image
center = list(baked.pixels[4 * (32 * 16 + 16):][:4])
print("  投影结果中心像素:", [round(v, 3) for v in center])
check("投影烘出来的是高模的颜色（偏红），不是低模的蓝",
      center[0] > center[2], [round(v, 3) for v in center])

# 对照组：关掉投影，应当烘出低模自己的蓝
settings.use_selected_to_active = False
plain_settings = usession.settings_to_bake_settings(settings)
plain_plan = pl.build_plan(bpy.context, plain_settings)
plain_job = jb.BakeJob(bpy.context, plain_plan, plain_settings,
                       backend=bk.CyclesBackend(txmod.SceneTransaction(bpy.context),
                                                margin=2, samples=1))
plain_job.run_to_completion()
plain_center = list(plain_plan.tasks[0].image.pixels[4 * (32 * 16 + 16):][:4])
print("  不投影时中心像素:", [round(v, 3) for v in plain_center])
check("不投影时烘的是低模自己的颜色（偏蓝）",
      plain_center[2] > plain_center[0], [round(v, 3) for v in plain_center])

print("\n=== 阶段 G：事务只能有一份（走过的弯路）===")
# 真实翻车现场：session.start() 给 backend 新建了一个事务，BakeJob 内部又自己建一个。
# backend 那个从没 capture 过，投影烘焙一走到"临时取消隐藏源物体"就崩：
#     AttributeError: 'SceneTransaction' object has no attribute '_hidden'
# 普通烘焙完全不碰那条路径，所以只有投影会踩；而当时的测试全都显式
# 传了同一个 transaction，正好绕开了这个 bug。
active = usession.Session.get()
active.reset()
active.backend_factory = None          # 让它自己建真的 CyclesBackend
started = active.start(bpy.context, bpy.context.scene.mbakery)
check("真的建起了 job", started and active.job is not None, started)
if active.job is not None:
    check("job 与 backend 共用同一个事务",
          active.job.tx is getattr(active.job.backend, "tx", None),
          (id(active.job.tx), id(getattr(active.job.backend, "tx", None))))
    check("事务确实快照过（capture 被调用）",
          getattr(active.job.tx, "_captured", False) is True)
active.cancel()
active.step_once()
active.reset()

# 防御性：一个从没 capture 过的事务也不能崩
fresh = txmod.SceneTransaction(bpy.context)
check("没 capture 过的事务：属性齐全",
      fresh._hidden == [] and fresh._uv_objects == [], (fresh._hidden, fresh._uv_objects))
probe_source = bpy.data.objects.get("Body")
if probe_source is not None:
    fresh.select_for_bake([low], [probe_source], unhide_sources=True)
    check("没 capture 的事务也能安全地选物体+取消隐藏", True)
    check("它顺手把自己 capture 了", fresh._captured is True)
    fresh.rollback()
    check("回滚后高模重新藏起来", probe_source.hide_get() is True)

print("\n=== 阶段 H：静态审计 ===")
source = open(os.path.join(WS, "material_bakery", "engine", "providers.py"),
              encoding="utf-8").read()
check("providers.py 不直接读 bpy.context（依赖显式传入）",
      "bpy.context" not in source, "bpy.context" in source)
check("provider 模块注释里写清了它是接入点",
      "第三方" in source or "provider" in source.lower())

# 回归防护：动态 items + 字符串 default 会被 Blender 拒绝（已踩两次）
prop_source = open(os.path.join(WS, "material_bakery", "ui", "properties.py"),
                   encoding="utf-8").read()
tree = ast.parse(prop_source)
dynamic_defaults = []
for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "EnumProperty"):
        continue
    kwargs = {kw.arg: kw.value for kw in node.keywords}
    items = kwargs.get("items")
    default = kwargs.get("default")
    if items is None or default is None:
        continue
    if isinstance(items, ast.Name) and isinstance(default, ast.Constant) \
            and isinstance(default.value, str):
        # 静态元组是可以的；只有当 items 指的是一张**回调函数表**时才违规。
        text = prop_source
        marker = "def {}(self, context)".format(items.id)
        if marker in text:
            dynamic_defaults.append("line {}: {}".format(node.lineno, items.id))
check("没有「动态 items + 字符串 default」的枚举（Blender 会直接拒绝注册）",
      not dynamic_defaults, dynamic_defaults)

check("面板的类型参数区可编辑（不是只读标签）",
      "draw_type_options" in open(
          os.path.join(WS, "material_bakery", "ui", "panels.py"),
          encoding="utf-8").read())

print("\n=== 阶段 I：注销 ===")
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

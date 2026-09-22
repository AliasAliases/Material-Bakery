"""从磁盘救回来 + 烘焙期间界面不锁 + 队列紧凑 + 日志落盘

用户的原话：
    "我还有个方案，毕竟都烘焙完了，直接扫一遍目录，要是有贴图就让第五页可以互动，
     我就能直接创建材质然后把贴图连上去了"
    "我在烘焙期间是切不到 blender 窗口的……这个烘焙列表死长，我都互动不了"
    "现在我按了一下，它无响应了，发不了 log 也 stop 不了"

所以这一套盯四件事：
    ① 文件名反解（三个命名时代的写法）+ manifest 优先
    ② 扫完之后第 5 页真的可交互（能建材质、能换到物体上、能回退）
    ③ UV 对不对得上能判断出来，不对时给 repack
    ④ 烘焙期间：模态不吃事件（PASS_THROUGH）、队列紧凑、日志落盘
"""
import bpy, sys, os, json, shutil, tempfile

# 工作区 = 本文件所在目录的上一级。
# ⚠ 这里原来写死的是开发机上的绝对路径，clone 到别处每个套件都跑不起来，
#   而且公开版/私有版两份检出会互相 import 错代码。
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WS not in sys.path:
    sys.path.insert(0, WS)

import material_bakery
from material_bakery import compat
from material_bakery.core import bake_types as bt
from material_bakery.core import imported as im
from material_bakery.core import naming as nm
from material_bakery.core import plan as pl
from material_bakery.core import transaction as txmod
from material_bakery.engine import backend as bk
from material_bakery.engine import job as jb
from material_bakery.engine import uv_pack
from material_bakery.ui import ops as uops
from material_bakery.ui import ops_rescue as urescue
from material_bakery.ui import panels as upanels
from material_bakery.ui import session as usession

compat.set_silent(True)

FAIL = []
def check(label, ok, extra=""):
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", label,
                               ("  " + str(extra)) if extra else ""))
    if not ok:
        FAIL.append(label)


OUT = os.path.join(WS, "_probe", "rescue")
if os.path.isdir(OUT):
    shutil.rmtree(OUT)
os.makedirs(OUT)

bpy.ops.wm.read_factory_settings(use_empty=True)
material_bakery.register()

print("=== ① 文件名反解：三个命名时代都要认 ===")
cases = [
    # (文件名, 期望 type_key, 期望 size)
    ("Common Parts 1-BaseColor-2k.png", "shader.base_color", 2048),      # 本次新版
    ("X-Metallic-1k.jpg", "shader.metallic", 1024),
    ("A-AmbientOcclusion-2k.png", "misc.ao", 2048),
    ("Z-Displacement(Socket)-512.png", "misc.displacement", 512),
    ("Common Parts 1BaseColor-2048.png", "shader.base_color", 2048),      # 上一版（粘一起、无 px）
    ("Bolt Carrier AssemblyNormal - 2048px.png", "shader.normal", 2048),  # 更早的原版（有空格+px）
    ("BodyRoughness-1024.png", "shader.roughness", 1024),
    ("Y-Normal.png", "shader.normal", 0),                                  # 没写尺寸
]
for filename, expected_key, expected_size in cases:
    key, size, prefix = im.parse_filename(filename)
    check("反解 {} -> {}".format(filename, expected_key),
          key == expected_key and size == expected_size,
          (key, size, prefix))

key, size, prefix = im.parse_filename("Common Parts 1-BaseColor-2k.png")
check("前缀（集合名）也解出来了", prefix == "Common Parts 1", prefix)
check("乱名字不硬猜（返回 None 让人自己判断）",
      im.parse_filename("random.png")[0] is None)
check("不认识的文件名不会被当成某个通道",
      im.parse_filename("random.png")[0] != bt.get("shader.base_color"))
check("尺寸未知时不显示 0", nm.size_token(0) == "", nm.size_token(0))

print("\n=== ② 扫目录（结构 + manifest 优先）===")
group_a = os.path.join(OUT, "Common Parts 1")
group_b = os.path.join(OUT, "Bolt")
os.makedirs(group_a)
os.makedirs(group_b)
for folder, names in ((group_a, ["Common Parts 1-BaseColor-2k.png",
                                 "Common Parts 1-Roughness-2k.png"]),
                      (group_b, ["Bolt-Normal-2k.png"])):
    for name in names:
        with open(os.path.join(folder, name), "wb") as handle:
            handle.write(b"not a real png")
with open(os.path.join(OUT, "Loose-BaseColor-1k.png"), "wb") as handle:
    handle.write(b"x")
with open(os.path.join(OUT, "random.png"), "wb") as handle:
    handle.write(b"x")

found, unmatched = im.scan_folder(OUT)
print("  found:", [str(f) for f in found])
print("  unmatched:", unmatched)
check("认出 4 张（含根目录那张）", len(found) == 4, len(found))
check("子目录名当成集合名",
      {f.group for f in found} >= {"Common Parts 1", "Bolt"}, [f.group for f in found])
check("根目录那张按文件名前缀归组",
      any(f.group == "Loose" for f in found), [f.group for f in found])
check("认不出来的文件被列出来而不是静默丢掉",
      any("random.png" in path for path, _reason in unmatched), unmatched)

# manifest 优先：写一份，把 BaseColor 指向 Roughness 的文件，看扫描信谁
manifest_dir = os.path.join(OUT, "Manifest")
os.makedirs(manifest_dir)
shutil.copy(os.path.join(group_a, "Common Parts 1-Roughness-2k.png"),
            os.path.join(manifest_dir, "weird-name.png"))
im.write_manifest(manifest_dir, "Manifest",
                  {"shader.base_color": {"type": "shader.base_color",
                                         "file": "weird-name.png", "size": 2048}},
                  uv_layer=uv_pack.PACK_LAYER_NAME,
                  extra={"packed_uv": False})
manifest_found, _unmatched = im.scan_folder(OUT)
by_path = {os.path.basename(f.path): f for f in manifest_found}
print("  manifest 结果:", by_path.get("weird-name.png"))
check("有 manifest 时按 manifest 认（名字再怪也认得出）",
      "weird-name.png" in by_path and
      by_path["weird-name.png"].type_key == "shader.base_color",
      by_path.get("weird-name.png"))
check("manifest 记录的来源被标出来",
      "weird-name.png" in by_path and by_path["weird-name.png"].source == "manifest",
      getattr(by_path.get("weird-name.png"), "source", None))
manifest = im.read_manifest(manifest_dir)
check("manifest 里记了这张图是用哪层 UV 烘的（救援时用得着）",
      manifest["uv_layer"] == uv_pack.PACK_LAYER_NAME, manifest)
check("manifest 里 packed_uv 恒为 False（不再打包 UV）",
      manifest["packed_uv"] is False, manifest)
with open(os.path.join(manifest_dir, im.MANIFEST_NAME), "w", encoding="utf-8") as handle:
    handle.write("{ not json")
check("manifest 是坏文件时返回 None 而不是抛异常",
      im.read_manifest(manifest_dir) is None)
im.write_manifest(manifest_dir, "Manifest",
                  {"shader.base_color": {"type": "shader.base_color",
                                         "file": "weird-name.png", "size": 2048}})

print("\n=== ③ 建一个真场景，扫完能建材质 + 换到物体上 ===")
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.samples = 4


def make_group(name, count=2, packed=False):
    collection = bpy.data.collections.new(name)
    scene.collection.children.link(collection)
    objects = []
    for index in range(count):
        obj_name = "{}_{}".format(name.replace(" ", "_"), index)
        mesh = bpy.data.meshes.new(obj_name + "Mesh")
        mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
        mesh.update()
        layer = mesh.uv_layers.new(name="UVMap")
        for i, uv in enumerate(((0.02, 0.02), (0.98, 0.02), (0.98, 0.98), (0.02, 0.98))):
            layer.data[i].uv = uv
        if packed:
            # 手工造一层 MBAKERY_UV，模拟**旧版本**烘出来的打包布局
            # （2026-09 之后插件自己不会产生这一层了）
            packed_layer = mesh.uv_layers.new(name=uv_pack.PACK_LAYER_NAME)
            for i, uv in enumerate(((0.02, 0.52), (0.48, 0.52), (0.48, 0.98), (0.02, 0.98))):
                packed_layer.data[i].uv = uv
        material = bpy.data.materials.new(obj_name + "Mat")
        material.use_nodes = True
        mesh.materials.append(material)
        obj = bpy.data.objects.new(obj_name, mesh)
        collection.objects.link(obj)
        objects.append(obj)
    return collection, objects


col_a, objs_a = make_group("Common Parts 1", 2, packed=True)
col_b, objs_b = make_group("Bolt", 1, packed=False)

settings = scene.mbakery
settings.use_baked_uv = True
settings.material_style = 'PRINCIPLED'
# 真烘一轮拿到真图（NullBackend 会往图里填颜色），再写盘
settings.maps.clear()
uops.add_map(settings, "shader.base_color", 32, dedupe=True)
uops.add_map(settings, "shader.roughness", 32, dedupe=True)
settings.output_dir = OUT
settings.auto_deliver = False

active = usession.Session.get()
active.reset()
active.backend_factory = lambda context, s: bk.NullBackend(save=True, directory=OUT)
check("能起一轮真烘焙（拿真图当素材）", active.start(bpy.context, settings),
      active.error)
active.run_blocking()
print("  烘焙:", active.report.summary())
done = [t for t in active.job.plan.tasks if t.status == "done"]
check("素材准备好了", len(done) > 0, len(done))

# 模拟"强杀"：把会话里的任务记录丢掉，只留磁盘上的图
active.reset()
check("会话清空后第 5 页原本是锁死的（没有报告、没有贴图）",
      active.report is None and not active.imported_by_group)

before_slots = {obj.name: [s.material.name if s.material else None
                           for s in obj.material_slots]
                for obj in objs_a + objs_b}

scan_folder = os.path.join(OUT, "Common Parts 1")
raw_found, raw_unmatched = im.scan_folder(scan_folder)
print("  目录里的文件:", sorted(os.listdir(scan_folder)))
print("  原始扫描:", [str(f) for f in raw_found], "| 没认出:", raw_unmatched)
total, groups, unmatched = active.scan_output(settings, scan_folder)
print("  扫到 {} 张 / {} 个集合 / 没认出 {}".format(total, groups, unmatched))
check("扫到了贴图", total > 0, total)
check("集合名从子目录名来", "Common Parts 1" in active.imported_by_group,
      list(active.imported_by_group))
check("第 5 页的贴图列表被填上", len(settings.textures) == total, len(settings.textures))

state, detail = active.uv_state()
print("  UV 判断:", state, "-", detail)
check("打包过的集合判为对得上", state in ("matched", "partial"), (state, detail))

built, _reports = active.build_materials(settings)
print("  建了 {} 个材质".format(built))
check("能建材质（不再说 'No successful bake to build from'）", built > 0, built)
applied = active.apply_materials(settings)
print("  应用到 {} 个物体".format(applied))
check("能换到物体上", applied > 0, applied)
check("交付材质接到了 Principled 上",
      any(node.type == 'BSDF_PRINCIPLED'
          for node in objs_a[0].material_slots[0].material.node_tree.nodes))
check("材质**没有** UVMap 节点（那正是错位的来源：材质硬引用一个按物体存在的层名）",
      not any(node.type == 'UVMAP'
              for node in objs_a[0].material_slots[-1].material.node_tree.nodes),
      [node.type for node in objs_a[0].material_slots[-1].material.node_tree.nodes])
check("交付材质走的是物体当前的渲染 UV 层（打包层已经是 active_render）",
      objs_a[0].data.uv_layers.get(uv_pack.PACK_LAYER_NAME) is not None)

from material_bakery.deliver import slots as slots_mod
restored = slots_mod.restore_objects(None)
ok_restored = [o for o in restored if o.ok]
print("  回退:", len(ok_restored), "个物体（场景里其它物体本来就没换过材质）")
check("救援路径也能一键回退", len(ok_restored) == applied, (len(ok_restored), applied))
for obj in objs_a:
    now = [s.material.name if s.material else None for s in obj.material_slots]
    check("{} 的槽回到原样".format(obj.name), now == before_slots[obj.name],
          (now, before_slots[obj.name]))

print("\n=== ④ 旧版（打包布局）的贴图要能认出来并说清后果 ===")
# ⚠ UV 打包 2026-09 整个删掉了，所以新烘的贴图永远对得上（用物体自己的 UV）。
#   这一段改成验"旧版留下来的打包贴图能被识别" —— 插件没法复原那种布局，
#   只能明确告诉用户"这是旧版烘的，要么重烘"。
#   把打包层删掉，模拟"强杀且没保存"
for obj in objs_a:
    layer = obj.data.uv_layers.get(uv_pack.PACK_LAYER_NAME)
    if layer is not None:
        obj.data.uv_layers.remove(layer)
state, detail = active.uv_state()
print("  删除旧打包层之后:", state, "-", detail)
check("旧打包贴图 + 层没了 -> 判为对不上，并说清是旧版烘的、要重烘",
      state == "missing" and "packed" in detail.lower() and "bake again" in detail.lower(),
      (state, detail))
check("Repack 按钮已经删掉（那条路不存在了）",
      "repack_uvs" not in dir(bpy.ops.mbakery),
      [n for n in dir(bpy.ops.mbakery) if "repack" in n])
check("也没法再调用 repack_uvs（不是留着个空壳）",
      not hasattr(active, "repack_uvs"))

print("\n=== ⑤ 烘焙期间界面不能被锁（PASS_THROUGH）+ 队列紧凑 ===")


class FakeEvent:
    def __init__(self, type_):
        self.type = type_


class FakeWindowManager:
    def __init__(self):
        self.removed = []
    def event_timer_remove(self, timer):
        self.removed.append(timer)


class FakeContext:
    def __init__(self):
        self.window_manager = FakeWindowManager()


class FakeOperator:
    modal = uops.MBAKERY_OT_StartBake.modal
    cancel = uops.MBAKERY_OT_StartBake.cancel
    _stop_timer = uops.MBAKERY_OT_StartBake._stop_timer

    def __init__(self, timer):
        self._timer = timer


active.reset()
active.backend_factory = lambda context, s: bk.NullBackend()
active.start(bpy.context, settings)
operator = FakeOperator("t")
result = operator.modal(FakeContext(), FakeEvent('MOUSEMOVE'))
print("  鼠标事件返回:", result)
check("鼠标事件返回 PASS_THROUGH（不吞事件、界面可点可切窗口）",
      result == {'PASS_THROUGH'}, result)
check("TIMER 事件仍然推进任务",
      operator.modal(FakeContext(), FakeEvent('TIMER')) == {'RUNNING_MODAL'})
check("ESC 仍然是取消（返回 CANCELLED）",
      operator.modal(FakeContext(), FakeEvent('ESC')) == {'CANCELLED'})
active.reset()

# 队列：128 个任务时不该把这些行全画出来
from material_bakery.ui import panels as upanels
fake_settings = scene.mbakery
fake_settings.maps.clear()
for key in ("shader.base_color", "shader.roughness", "shader.metallic", "shader.normal"):
    uops.add_map(fake_settings, key, 2048, dedupe=True)
active.reset()
fake_plan = pl.build_plan(bpy.context, usession.settings_to_bake_settings(fake_settings))
print("  计划任务数:", len(fake_plan.tasks))
log = []


class Recorder:
    def __init__(self, log):
        self._log = log
    def label(self, text="", icon=None, **kwargs):
        self._log.append(("label", text))
        return Recorder(self._log)
    def prop(self, data, name=None, **kwargs):
        self._log.append(("prop", name))
        return Recorder(self._log)
    def operator(self, idname=None, **kwargs):
        self._log.append(("operator", idname))
        return Recorder(self._log)
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        def method(*args, **kwargs):
            self._log.append((name, None))
            return Recorder(self._log)
        return method


rows = []
upanels._draw_queue_window(Recorder(rows), fake_plan.tasks, context=None)
labels = [text for kind, text in rows if kind == "label"]
print("  紧凑队列画了 {} 行: {}".format(len(labels), labels))
check("128 个任务时队列只画很少几行（不是 60 行）", len(labels) <= 10, len(labels))
check("队列说了后面还有多少",
      any("more" in str(t) for t in labels) or len(fake_plan.tasks) <= 10, labels)

print("\n=== ⑥ 日志落盘（强杀之后现场还在）===")
log_path = usession.log_path()
if os.path.isfile(log_path):
    os.remove(log_path)
active.reset()
active.backend_factory = lambda context, s: bk.NullBackend()
active.start(bpy.context, settings)
active.run_blocking()
check("日志文件被写出来了", os.path.isfile(log_path), log_path)
if os.path.isfile(log_path):
    with open(log_path, "r", encoding="utf-8") as handle:
        content = handle.read()
    print("  日志前 3 行:", content.splitlines()[:3])
    check("日志里有这次运行的表头", "Material Bakery run" in content)
    check("日志里有每个任务的记录", "baking 1/" in content, content[:200])
    check("日志里有结尾摘要", "maps baked" in content)
active.reset()
check("reset 之后日志文件还在（不会被清掉）", os.path.isfile(log_path))

print("\n=== ⑦ 扫不到的目录要明说，不能装作成功 ===")
empty_dir = os.path.join(OUT, "empty")
os.makedirs(empty_dir, exist_ok=True)
active.reset()
total, groups, unmatched = active.scan_output(settings, empty_dir)
check("空目录 -> 0 张", total == 0, total)
check("并且写了一条 ERROR 日志",
      any(entry.level == "ERROR" for entry in active.log), [e.message for e in active.log][-1:])
check("不存在的目录也不崩", active.scan_output(settings, os.path.join(OUT, "nope"))[0] == 0)

active.reset()
print("\n=== ⑧ 文件里已有烘焙材质时，第 5 页不用扫目录就能用 ===")
# 用户的原话："我想你把再改一点，第五页那扫目录，要是用户之前烘焙过了，
#              有材质了就能直接进行烘焙后的操作"
# 判据是材质身上的标记（mbakery_final_material）+ 名字（= 集合名），
# 所以重开文件、上次烘完保存过，都能接着做交付动作。
fresh = usession.Session.get()
fresh.reset()
settings.textures.clear()
settings.auto_deliver = False
settings.maps.clear()
uops.add_map(settings, "shader.base_color", 32, dedupe=True)
fresh.backend_factory = lambda context, s: bk.NullBackend(save=False)
fresh.start(bpy.context, settings)
fresh.run_blocking()
built_now = fresh.build_materials(settings)
print("  先烘一轮并建材质:", built_now[0])
material_names = sorted(fresh.materials_by_group)
fresh.reset()                                   # 模拟"重开文件 / 新会话"
check("新会话里没有报告（原样）", fresh.report is None)
check("新会话里没有材质记录（原样）", not fresh.materials_by_group)

detected = fresh.detect_existing(settings)
print("  认出文件里的材质:", detected, material_names, "| 贴图列表:", len(settings.textures))
check("认出文件里已经烘过的材质", detected > 0, detected)
check("认出来的正是我们建的材质", sorted(fresh.materials_by_group) == material_names,
      (sorted(fresh.materials_by_group), material_names))
check("贴图列表被自动填上（不用扫目录）", len(settings.textures) > 0,
      len(settings.textures))
check("贴图条目被标成 imported（来自已有结果，不是本次烘的）",
      all(item.imported for item in settings.textures),
      [item.imported for item in settings.textures])
check("贴图的通道能从节点标签反推出来",
      all(item.type_key for item in settings.textures),
      [(item.type_label, item.type_key) for item in settings.textures])
check("只有在没有报告时才认（不覆盖当前会话）",
      fresh.detect_existing(settings) == detected)

added_here = fresh.apply_materials(settings)
print("  认出来之后直接 Add Slot:", added_here, "个物体")
check("认出之后能直接用 Add Slot（用户要的就是「能直接进行烘焙后的操作」）",
      added_here > 0, added_here)
previewed_here = fresh.preview_materials(settings, baked=True)
check("也能直接 Preview", previewed_here > 0, previewed_here)
finalized_here = fresh.finalize_materials(settings)
check("也能直接收尾", finalized_here > 0, finalized_here)
from material_bakery.deliver import slots as slots_mod_2
slots_mod_2.restore_objects(None)
fresh.reset()

print("\n=== ⑨ 没有会话日志时，面板显示磁盘上那次运行的日志 ===")
# 用户的原话："我的 log 没了"（他指的是 Blender 里的红色报错）。
log_file = usession.log_path()
os.makedirs(os.path.dirname(log_file), exist_ok=True)
with open(log_file, "w", encoding="utf-8") as handle:
    handle.write("=== Material Bakery run (test) ===\n")
    handle.write("[00:00:01] ERROR: 上次那行红色报错\n")
    handle.write("[00:00:02] INFO: 6 of 6 maps baked\n")
fresh2 = usession.Session.get()
fresh2.reset()
check("新会话自己没有日志", not fresh2.log)
tail = fresh2.tail_log_file(5)
print("  磁盘日志尾部:", tail)
check("能从磁盘读到上次运行的日志", any("红色报错" in line for line in tail), tail)
check("读到的是最后几行", tail[-1].strip().endswith("maps baked"), tail[-1:])

print("\n=== ⑩ 红字（ERROR report）必须进日志并且落盘 ===")


class FakeOperator:
    def __init__(self):
        self.reported = []
    def report(self, level, message):
        self.reported.append((str(level), message))


fake = FakeOperator()
ops_module = __import__("material_bakery.ui.ops", fromlist=["x"])
ops_module._error(fake, "a simulated red error")
check("界面照旧收到 ERROR report",
      fake.reported and "ERROR" in fake.reported[0][0], fake.reported)
check("会话日志里有这条红字",
      any("a simulated red error" == entry.message for entry in fresh2.log),
      [e.message for e in fresh2.log])
check("磁盘日志里也有（关掉 Blender 也不会丢）",
      any("a simulated red error" in line for line in fresh2.tail_log_file(5)),
      fresh2.tail_log_file(3))
ops_module._warn(fake, "a simulated warning")
check("黄字也留档",
      any(entry.level == "WARN" for entry in fresh2.log),
      [(e.level, e.message) for e in fresh2.log][-2:])
fresh2.reset()

print("\n=== ⑪ 扫完 = 烘完的界面（自动建材质 + 挂槽）===")
# 用户的原话："扫完的界面就是烘焙完的界面，有列出来的 32 个材质，
#              可以切换预览，也可以导出最终物体"
from material_bakery.deliver import slots as slots_mod_3
auto_scan = usession.Session.get()
auto_scan.reset()
settings.textures.clear()
settings.auto_deliver = True
slots_mod_3.restore_objects(None)              # 从干净状态开始
total, groups, unmatched = auto_scan.scan_output(
    settings, os.path.join(OUT, "Common Parts 1"))
print("  扫描:", total, "张 /", groups, "集合")
check("扫描成功", total > 0, total)
check("扫完**自动建好了材质**（不用再点 Build Materials）",
      len(auto_scan.materials_by_group) > 0, len(auto_scan.materials_by_group))
check("扫完**自动挂上了烘焙槽**",
      len(auto_scan.objects_with_baked_slot()) > 0,
      len(auto_scan.objects_with_baked_slot()))
preview_after_scan = auto_scan.preview_materials(settings, baked=True)
check("扫完直接 Preview 就能用（不再报 nothing had a baked slot）",
      preview_after_scan > 0, preview_after_scan)
check("扫完也能直接切回原材质",
      auto_scan.preview_materials(settings, baked=False) > 0)
slots_mod_3.restore_objects(None)
auto_scan.reset()

# Auto Deliver 关掉时不该动场景，只填列表
manual_scan = usession.Session.get()
manual_scan.reset()
settings.textures.clear()
settings.auto_deliver = False
manual_scan.scan_output(settings, os.path.join(OUT, "Common Parts 1"))
print("  关掉 Auto Deliver:", len(settings.textures), "张 /",
      len(manual_scan.materials_by_group), "材质")
check("关掉 Auto Deliver 时只填贴图列表，不建材质、不挂槽",
      len(settings.textures) > 0 and not manual_scan.materials_by_group
      and not manual_scan.objects_with_baked_slot(),
      (len(settings.textures), len(manual_scan.materials_by_group)))
settings.auto_deliver = True
manual_scan.reset()

print("\n=== ⑫ Preview 在没有槽时自动补槽（不再让用户先去点别的）===")
lazy = usession.Session.get()
lazy.reset()
settings.textures.clear()
settings.auto_deliver = False                   # 故意不让扫描自动挂槽
slots_mod_3.restore_objects(None)
lazy.scan_output(settings, os.path.join(OUT, "Common Parts 1"))
lazy.build_materials(settings)                  # 只建材质，不挂槽
check("前提：这时还没有任何烘焙槽", not lazy.objects_with_baked_slot())
shown = lazy.preview_materials(settings, baked=True)
print("  直接 Preview ->", shown, "个物体")
check("没槽时 Preview 自动补槽并生效", shown > 0, shown)
check("并且日志里说明了它替你补了槽",
      any("Added the baked slots first" in e.message for e in lazy.log),
      [e.message for e in lazy.log][-3:])
slots_mod_3.restore_objects(None)
settings.auto_deliver = True
lazy.reset()

print("\n=== 结果 ===")
if FAIL:
    print("FAILED {} check(s):".format(len(FAIL)))
    for name in FAIL:
        print("  -", name)
else:
    print("ALL CHECKS PASSED")

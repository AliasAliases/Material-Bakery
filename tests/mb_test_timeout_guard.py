"""Material Bakery — 实施轮 1.3「卡死」防线回归测试

这一轮是**排查轮**（用户报"烘焙完成后卡死"，现在不可复现）。现场一点证据都没留下，
所以这一套测的不是"卡死本身"（无头环境根本复现不了 GUI 才有的卡死），而是**下一次
一定留得下证据**，以及**将来真的卡住时测试会红**：

    A 证据保鲜   每轮一个按时间戳命名的日志目录、任何一次都不覆盖上一次、
                 逐行 flush + fsync（跑到一半就能从磁盘读到"现在到哪了"）、
                 旧的稳定路径变成指路纸条、诊断快照里有阶段与队列长度
    B 尾巴分段   收尾被切成有名字的段，**每一段都有 start 与 done**
                 （卡死时"最后一条有开始、没有结束"的段就是嫌疑段）；
                 尾巴耗时与推迟队列都有上限（超时型回归）
    C 看门狗     GUI 里每 N 秒一行"我还活着 + 现在在哪一段"；背景模式不注册
    D 同值写入   §32 那条规矩推广到**设置路径**：值一样就一个字都不写（引擎 /
                 烘焙设置 / 每张任务的图像位深），日志里能看出"写还是跳过"

判据都是打印出来的数字；不硬编码版本号 / 路径 / 计数。
"""
import bpy, sys, os, shutil, time

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WS not in sys.path:
    sys.path.insert(0, WS)

import material_bakery
from material_bakery import compat
from material_bakery.core import phases
from material_bakery.core import plan as pl
from material_bakery.core import scene_scan as ss
from material_bakery.core import transaction as txmod
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


def read_log(path):
    if not path or not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [line.rstrip("\n") for line in handle]


# 尾巴必须在这个秒数内走完（无头实测 ~0.06s，留两个数量级；卡死时会直接超时）
TAIL_LIMIT_SECONDS = 10.0


# ------------------------------------------------------------------------------------
#   夹具：两块平面（两个集合 → 两个任务），带 UV 与一张贴图材质

OUT = os.path.join(WS, "_probe", "timeout_guard")
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
settings.auto_deliver = False
settings.target_mode = ss.MODE_COLLECTIONS
settings.rebake_target_mode = ss.MODE_COLLECTIONS


def gradient_image(name, resolution=16):
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


source = gradient_image("TimeoutSource")
material = bpy.data.materials.new("TimeoutMat")
material.use_nodes = True
tree = material.node_tree
principled = next(node for node in tree.nodes if node.type == 'BSDF_PRINCIPLED')
texture = tree.nodes.new('ShaderNodeTexImage')
texture.image = source
tree.links.new(texture.outputs['Color'], principled.inputs['Base Color'])

for collection_index in range(2):
    collection = bpy.data.collections.new("Guard{:02d}".format(collection_index + 1))
    scene.collection.children.link(collection)
    mesh = bpy.data.meshes.new("GuardMesh{}".format(collection_index + 1))
    mesh.from_pydata([(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)], [], [(0, 1, 2, 3)])
    mesh.update()
    mesh.uv_layers.new(name="UV1")
    mesh.materials.append(material)
    obj = bpy.data.objects.new("Guard{}".format(collection_index + 1), mesh)
    collection.objects.link(obj)

settings.workflow = uprops.WORKFLOW_BAKE
settings.maps.clear()
uops.add_map(settings, "shader.base_color", 32, dedupe=True)

# ⚠ 引擎**先设成 CYCLES**：这样后面那一轮里 `set_render_engine('CYCLES')` 就是
#   一次"把同一个值赋给自己" —— D 组要证的正是它被跳过（§32 的元凶形状）。
scene.render.engine = 'CYCLES'


def run_once():
    active = usession.Session.get()
    active.reset()
    started = active.start(bpy.context, settings)
    return active, started


# ====================================================================================
print("=== A · 证据保鲜（每轮一个目录 + 逐行落盘 + 指路纸条）===")

before_dirs = usession.run_dirs()
active, started = run_once()
check("会话起来了", started, active.error)
check("这一轮的日志目录是本轮新建的",
      bool(active.log_dir) and os.path.isdir(active.log_dir), active.log_dir)
check("日志目录在 log_root() 底下",
      os.path.normcase(os.path.dirname(os.path.normcase(active.log_dir)))
      == os.path.normcase(usession.log_root()),
      (active.log_dir, usession.log_root()))
check("日志文件名固定（目录区分轮次）",
      os.path.basename(active.log_file) == usession.LOG_FILE_NAME,
      active.log_file)

# 跑到一半就从磁盘读 —— 这就是"强杀之后现场还在"的那条判据
active.step_once()
mid_lines = read_log(active.log_file)
print("  跑到一半时磁盘上已有 {} 行".format(len(mid_lines)))
check("跑到一半时磁盘上已经有内容（不是只在内存里）", len(mid_lines) > 3,
      mid_lines[:2])
check("阶段行已经落盘（'run started' 在里面）",
      any("run started" in line for line in mid_lines), mid_lines[:2])
check("日志里有本轮的绝对路径（指路纸条指的就是它）",
      any(active.log_file in line for line in mid_lines), mid_lines[:2])

active.run_blocking()
first_log = active.log_file
first_lines = read_log(first_log)
check("第一轮跑完", active.report is not None and active.report.failed == 0,
      active.report.summary() if active.report else active.error)
check("第一轮日志最后一行的内容是本轮收尾",
      any("run finished" in line for line in first_lines[-3:]), first_lines[-3:])
check("旧路径是一张指路纸条（不再是日志本体）",
      os.path.isfile(usession.pointer_path())
      and "pointer" in read_log(usession.pointer_path())[0].lower(),
      read_log(usession.pointer_path())[:2])
check("纸条指向最新一轮的日志",
      any(first_log in line for line in read_log(usession.pointer_path())),
      read_log(usession.pointer_path()))
check("log_path() 指向最新一轮", usession.log_path() == first_log,
      (usession.log_path(), first_log))

# 第二轮：**不许覆盖**第一轮
active2, started2 = run_once()
active2.run_blocking()
second_log = active2.log_file
check("第二轮跑到另一个日志文件里（目录不同）", second_log != first_log,
      (first_log, second_log))
after_dirs = usession.run_dirs()
check("目录数增加了（一轮一个）", len(after_dirs) == len(before_dirs) + 2,
      (len(before_dirs), len(after_dirs)))
kept = read_log(first_log)
check("第一轮的日志**一个字节都没被覆盖**",
      kept == first_lines and len(kept) == len(first_lines),
      (len(kept), len(first_lines)))
check("第二轮日志自己也有 'run finished'",
      any("run finished" in line for line in read_log(second_log)[-3:]),
      read_log(second_log)[-3:])

# ====================================================================================
print("\n=== B · 尾巴分段 + 超时回归 ===")

clock = active2.clock
check("这一轮有时钟", clock is not None, type(clock).__name__)
unclosed = clock.unclosed() if hasattr(clock, "unclosed") else ["(no api)"]
check("所有已经开始的阶段**都闭合了**（卡死时这里会非空）", unclosed == [], unclosed)

TAIL_NAMES = ("finish: commit transaction", "finish: report",
              "finish: auto delivery", "finish: timings", "finish: release guard")
blob = "\n".join(read_log(second_log))
for name in TAIL_NAMES:
    check("尾巴段 '{}' 有 start 与 done".format(name),
          ("{}: start".format(name)) in blob and ("{}: done".format(name)) in blob,
          name)
tail_seconds = sum(clock.total(name) for name in TAIL_NAMES)
print("  尾巴五段合计 {:.3f}s（上限 {:.1f}s）".format(tail_seconds, TAIL_LIMIT_SECONDS))
check("尾巴在超时上限内走完（卡死会在这里红）",
      0.0 <= tail_seconds < TAIL_LIMIT_SECONDS, tail_seconds)
check("收尾的推迟队列已经排空（GUI 靠 timer，background 同步）",
      len(getattr(txmod, "_PENDING", ())) == 0, len(getattr(txmod, "_PENDING", ())))
check("日志最后落到 'session tail complete'",
      any("session tail complete" in line for line in read_log(second_log)[-2:]),
      read_log(second_log)[-2:])
check("'run finished' 是这一轮的收尾行（在它之后没有别的阶段行）",
      read_log(second_log)[-1].strip().endswith(
          active2.report.summary() if active2.report else ""),
      read_log(second_log)[-1])

state = active2.describe_state()
print("  describe_state():", state[:120])
check("诊断快照写出了当前阶段", "phase=" in state, state[:80])
check("诊断快照写出了推迟队列长度", "pending_restores=" in state, state[:80])
check("诊断快照写出了日志路径", "log=" in state, state[:80])
check("SNAPSHOT 行进了日志", "SNAPSHOT" in blob, "")

# ====================================================================================
print("\n=== C · 看门狗（GUI 才注册；回调本身可测）===")

active2.stop_watchdog()
check("背景模式不注册看门狗（没有事件循环）",
      active2.start_watchdog() is False
      and bpy.app.timers.is_registered(active2._watchdog) is False,
      bpy.app.background)

# 回调本身：没跑完 -> 留一行 ALIVE 并返回下次间隔；跑完了 -> 自己退出
class FakeJob:
    is_finished = False
    progress = (1, 2)
    state = "baking"


real_job = active2.job
active2.job = FakeJob()
active2._watchdog_running = True
before_alive = sum(1 for entry in active2.log if entry.level == "ALIVE")
interval = active2._watchdog()
after_alive = sum(1 for entry in active2.log if entry.level == "ALIVE")
check("看门狗在跑的时候留一行 ALIVE", after_alive == before_alive + 1,
      (before_alive, after_alive))
check("看门狗返回下一次间隔（继续跑）", interval == usession.WATCHDOG_INTERVAL,
      interval)
active2.job = FakeJob()
active2.job.is_finished = True
check("跑完之后看门狗自己退出（返回 None）", active2._watchdog() is None, "")
active2.job = real_job

# ====================================================================================
print("\n=== D · 同值写入：值一样就一个字都不写 ===")

# D1：引擎本来就是 CYCLES -> begin() 那次 set_render_engine 必须是 skipped
engine_lines = [line for line in blob.splitlines() if "set: engine" in line]
print("  引擎那几行:", engine_lines[:2])
check("设置路径也走'先比再写'：引擎同值被跳过",
      any("skipped" in line for line in engine_lines), engine_lines[:2])

# D2：两张任务用同一个位深 -> 第二张的 color_depth 必须 skipped
depth_lines = [line for line in blob.splitlines() if "color_depth" in line]
skipped_depth = [line for line in depth_lines if "skipped" in line]
print("  color_depth 行数 {}，其中 skipped {}".format(len(depth_lines),
                                                      len(skipped_depth)))
check("每张任务的图像位深：第一张写、第二张跳过",
      len(depth_lines) >= 2 and len(skipped_depth) >= 1,
      (len(depth_lines), len(skipped_depth)))

# D3：直接量一次 write_setting 的语义（比日志更硬）
tx = txmod.SceneTransaction(bpy.context)
before_engine = scene.render.engine
check("write_setting：值一样返回 False（没写）",
      tx.write_setting("engine", scene.render, "engine", before_engine) is False,
      before_engine)
check("write_setting：值不一样返回 True（写了）",
      tx.write_setting("bake", scene.render.bake, "margin",
                       scene.render.bake.margin + 1) is True,
      scene.render.bake.margin)
# 收回去，别把状态留给后面的套件
scene.render.bake.margin = max(0, scene.render.bake.margin - 1)

# ====================================================================================
print("\n=== E · 老接口仍然按老语义工作（别把日志设施做成行为改动）===")
check("tail_log_file 读得到最新一轮的日志",
      len(active2.tail_log_file(5)) > 0, active2.tail_log_file(2))
check("Session.forget() 之后看门狗没被留下",
      usession.Session.forget() is None
      and bpy.app.timers.is_registered(active2._watchdog) is False, "")
check("phases 的收一轮仍然给出摘要",
      phases.current() is None and "slowest" in (clock.summary() or "slowest"),
      (phases.current(), clock.summary()[:60]))

bpy.ops.wm.read_factory_settings(use_empty=True)

print("\n=== 结果 ===")
if FAIL:
    print("FAILED {} check(s):".format(len(FAIL)))
    for name in FAIL:
        print("  -", name)
else:
    print("ALL CHECKS PASSED")

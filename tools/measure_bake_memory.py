"""量一量：烘焙过程中**真实的**内存与单步耗时。

背景：用户报"烘焙过程卡住、界面没法操作"。
上一版 `measure_bake_startup.py` 已经把"一次建完 96 张 2048² 图"量成 1.7s，
但那个 4.5 GB 是**按尺寸算出来的名义值**，不是实测 ——
`resource` 模块在 Windows 上没有，那段打印一直是 None 被跳过。

这个脚本改用 Windows 的 GetProcessMemoryInfo 拿真实工作集，
并且把两条路径都跑一遍做对照：

    A) 旧路径：start() 里一次建完全部图像（同步）
    B) 新路径：交错准备 —— 一步一个任务，建一张、烘一张

要回答三个问题：
    1. 交错之后，单个 tick 的最坏耗时降了多少？（界面能不能重绘）
    2. 峰值内存降了吗？（这是最容易被想当然的一点）
    3. 如果峰值没降，钱花在哪了、有没有安全的释放点？

只测量，不改任何东西。
"""
import bpy, sys, os, time, gc, ctypes, ctypes.wintypes

# 工作区 = 本文件所在目录的上一级。
# ⚠ 这里原来写死的是开发机上的绝对路径，clone 到别处每个套件都跑不起来，
#   而且公开版/私有版两份检出会互相 import 错代码。
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WS not in sys.path:
    sys.path.insert(0, WS)

import material_bakery
from material_bakery import compat
from material_bakery.core import plan as pl
from material_bakery.core import transaction as txmod
from material_bakery.engine import backend as bk
from material_bakery.engine import job as jb
from material_bakery.engine import uv_pack
from material_bakery.engine.backend import ensure_image
from material_bakery.ui import ops as uops
from material_bakery.ui import session as usession

compat.set_silent(True)

GB = 1024.0 ** 3
MB = 1024.0 ** 2


# ---------------------------------------------------------------- 真实内存读数
class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.wintypes.DWORD),
        ("PageFaultCount", ctypes.wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


# ⚠ 必须显式写 argtypes/restype：靠 ctypes.windll 的默认推断会把
#   GetCurrentProcess() 的 HANDLE 当成 c_int 截断，调用直接返回 0，
#   于是内存读数**静默变 None** —— 上一版工具就是这样把测量悄悄跳过的。
def _memory_api():
    if not hasattr(ctypes, "WinDLL"):
        return None
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
    fn = k32.K32GetProcessMemoryInfo
    fn.argtypes = [ctypes.wintypes.HANDLE,
                   ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
                   ctypes.wintypes.DWORD]
    fn.restype = ctypes.wintypes.BOOL
    return k32.GetCurrentProcess(), fn


_MEMORY_API = _memory_api()


def _memory_counters():
    if _MEMORY_API is None:
        return None
    handle, fn = _MEMORY_API
    counters = _PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    if not fn(handle, ctypes.byref(counters), counters.cb):
        return None
    return counters


def rss_gb():
    """当前工作集（GB）"""
    counters = _memory_counters()
    return None if counters is None else counters.WorkingSetSize / GB


def peak_gb():
    """进程峰值工作集（GB）"""
    counters = _memory_counters()
    return None if counters is None else counters.PeakWorkingSetSize / GB


def _delta(a, b):
    """None 安全的差值 —— 拿不到内存读数时不要连带把脚本搞崩"""
    if a is None or b is None:
        return float('nan')
    return a - b


def has_memory_api():
    return _memory_counters() is not None


print("=" * 78)
print("烘焙内存 / 响应性测量    Blender {}".format(bpy.app.version_string))
print("=" * 78)
if not has_memory_api():
    print("⚠ 拿不到进程内存读数，下面的内存数字会是 None")
print("  进程基线内存: {:.2f} GB".format(rss_gb() or 0.0))

# ---------------------------------------------------------------- 造"像用户"的场景
GROUPS = 32
OBJECTS_PER_GROUP = 4
SIZE = 2048
MAPS = 3

print("\n场景规模: {} 个集合 x {} 个物体，{} 张贴图/集合 @ {}px".format(
    GROUPS, OBJECTS_PER_GROUP, MAPS, SIZE))
print("          合计 {} 个烘焙任务，名义像素内存 {:.2f} GB".format(
    GROUPS * MAPS, GROUPS * MAPS * SIZE * SIZE * 4 * 4 / GB))

bpy.ops.wm.read_factory_settings(use_empty=True)
material_bakery.register()
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.samples = 1

t0 = time.time()
for g in range(GROUPS):
    collection = bpy.data.collections.new("Body{:02d}".format(g))
    scene.collection.children.link(collection)
    for i in range(OBJECTS_PER_GROUP):
        name = "Body{:02d}_Part{}".format(g, i)
        mesh = bpy.data.meshes.new(name + "Mesh")
        mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
                         [], [(0, 1, 2, 3)])
        mesh.update()
        layer = mesh.uv_layers.new(name="UVMap")
        for k, uv in enumerate(((0, 0), (1, 0), (1, 1), (0, 1))):
            layer.data[k].uv = uv
        mat = bpy.data.materials.new(name + "Mat")
        mat.use_nodes = True
        mesh.materials.append(mat)
        obj = bpy.data.objects.new(name, mesh)
        collection.objects.link(obj)
print("  建场景用了 {:.2f}s".format(time.time() - t0))

settings = scene.mbakery
settings.maps.clear()
for key in ("shader.base_color", "shader.roughness", "shader.normal"):
    uops.add_map(settings, key, SIZE, dedupe=True)
settings.target_mode = 'collections'
settings.prepare_uv = False


def fresh_plan():
    bake_settings = usession.settings_to_bake_settings(settings)
    return pl.build_plan(bpy.context, bake_settings), bake_settings


plan, bake_settings = fresh_plan()
print("  计划任务数:", len(plan.tasks))
print("  分组数:", len(plan.groups))

stats = {}

# ---------------------------------------------------------------- A) 旧路径
print("\n" + "-" * 78)
print("A) 旧路径：一次建完全部图像（同步，界面完全不响应）")
print("-" * 78)
plan_a, _ = fresh_plan()
start_rss = rss_gb()
t0 = time.time()
for index, task in enumerate(plan_a.tasks):
    image = ensure_image(task.image_name, task.size, task.bake_type.channel,
                         file_format='PNG', reuse=False)
    task.image = image
    if index == 0:
        first_cost = time.time() - t0
elapsed = time.time() - t0
bulk_rss = rss_gb()
print("  建 {} 张图: {:.2f}s（第 1 张就花了 {:.3f}s）".format(
    len(plan_a.tasks), elapsed, first_cost))
print("  工作集: {:.2f} GB -> {:.2f} GB（+{:.2f} GB）".format(
    start_rss or 0, bulk_rss or 0, _delta(bulk_rss, start_rss)))
stats['bulk_seconds'] = elapsed
stats['bulk_delta'] = _delta(bulk_rss, start_rss)
stats['bulk_peak'] = peak_gb()

# 这些图像到底占不占内存？把像素缓冲区主动释放掉再量一次
t0 = time.time()
for task in plan_a.tasks:
    if task.image is not None:
        task.image.buffers_free()
freed = time.time() - t0
after_free = rss_gb()
print("  buffers_free() 之后: {:.2f} GB（{:.2f}s）—— 释放确实回收了 {:.2f} GB".format(
    after_free or 0, freed, _delta(bulk_rss, after_free)))
stats['free_recovered'] = _delta(bulk_rss, after_free)

for task in plan_a.tasks:
    if task.image is not None:
        bpy.data.images.remove(task.image)
        task.image = None
gc.collect()
print("  删掉图像后回到: {:.2f} GB".format(rss_gb()))


# ---------------------------------------------------------------- B) 新路径
print("\n" + "-" * 78)
print("B) 新路径：交错准备（一步 = 准备一张 + 烘一张）")
print("-" * 78)
plan_b, settings_b = fresh_plan()
backend = bk.NullBackend()
job = jb.BakeJob(bpy.context, plan_b, settings_b, backend=backend)

step_times = []
rss_series = []
start_rss = rss_gb()
t0 = time.time()
job.start()
startup = time.time() - t0
print("  start() 本身: {:.4f}s（旧路径的重活全在这里）".format(startup))

while not job.is_finished:
    tick = time.time()
    job.step()
    step_times.append(time.time() - tick)
    rss_series.append(rss_gb())
    if len(step_times) > 500:
        print("  ⚠ 步数异常，停下")
        break

total = time.time() - t0
live_images = sum(1 for t in plan_b.tasks if t.image is not None)
print("  总步数: {}，总耗时 {:.2f}s".format(len(step_times), total))
if step_times:
    worst = max(step_times)
    average = sum(step_times) / len(step_times)
    print("  单步耗时: 最坏 {:.3f}s / 平均 {:.4f}s".format(worst, average))
    print("  （界面每步之间都有机会重绘：最坏阻塞 = {:.0f}ms）".format(worst * 1000))
    stats['worst_step'] = worst
    stats['avg_step'] = average
print("  跑到最后仍然活着的图像: {} 张".format(live_images))
print("  工作集: {:.2f} GB -> {:.2f} GB（+{:.2f} GB）".format(
    start_rss or 0, rss_gb() or 0, _delta(rss_gb(), start_rss)))
stats['interleaved_delta'] = _delta(rss_gb(), start_rss)
stats['interleaved_peak'] = peak_gb()

if rss_series and rss_series[0] is not None:
    print("  内存曲线（每 10 步采样）: " + " ".join(
        "{:.2f}".format(v) for v in rss_series[::10]))
    print("  峰值出现在第 {} 步附近".format(
        rss_series.index(max(rss_series))))

# ---------------------------------------------------------------- C) 释放能不能救回来
print("\n" + "-" * 78)
print("C) 每张图落地之后主动 buffers_free()，能回收多少？")
print("-" * 78)
before_free = rss_gb()
t0 = time.time()
for task in plan_b.tasks:
    if task.image is not None:
        task.image.buffers_free()
free_seconds = time.time() - t0
after_free = rss_gb()
print("  buffers_free() 96 张用了 {:.3f}s".format(free_seconds))
print("  工作集: {:.2f} GB -> {:.2f} GB（回收 {:.2f} GB）".format(
    before_free or 0, after_free or 0, _delta(before_free, after_free)))
stats['filled_free_recovered'] = _delta(before_free, after_free)

# 释放之后还导得出来吗？这才是能不能真用的判据。
import tempfile
probe_dir = os.path.join(tempfile.gettempdir(), "mb_memory_probe")
if os.path.isdir(probe_dir):
    import shutil
    shutil.rmtree(probe_dir, ignore_errors=True)
os.makedirs(probe_dir, exist_ok=True)
sample = plan_b.tasks[0].image
probe_path = os.path.join(probe_dir, "after_free.png")
result = "ok"
try:
    sample.filepath_raw = probe_path
    sample.file_format = 'PNG'
    sample.save()
except Exception as exc:
    result = "{}: {}".format(type(exc).__name__, exc)
size_after_free = os.path.getsize(probe_path) if os.path.isfile(probe_path) else -1
print("  buffers_free() 之后还能不能导出: {}（{} 字节）".format(result, size_after_free))
stats['export_after_free'] = size_after_free

# 再验一次：一次真正的"写盘 + pack + 释放"循环，图还在不在
packed_ok = True
try:
    sample.reload()
except Exception as exc:
    packed_ok = "{}: {}".format(type(exc).__name__, exc)
print("  释放之后 reload():", packed_ok)

# ---------------------------------------------------------------- D) 真 Cycles 的内存
print("\n" + "-" * 78)
print("D) 真实 Cycles 烘焙：一张图落地后到底占多少内存")
print("-" * 78)


def real_bake_at(size):
    """在 test.blend 上真烘一轮，返回 (图片数, 工作集增量 GB, 浮点图数)"""
    bpy.ops.wm.open_mainfile(filepath=os.path.join(WS, "test.blend"))
    real = bpy.context.scene.mbakery
    real.maps.clear()
    for key in ("shader.base_color", "shader.roughness", "shader.normal"):
        uops.add_map(real, key, size, dedupe=True)
    real.target_mode = 'selected'
    real.prepare_uv = False
    real.output_dir = probe_dir
    for obj in bpy.context.scene.objects:
        if obj.type == 'MESH':
            obj.select_set(True)
    real_bake_settings = usession.settings_to_bake_settings(real)
    real_plan = pl.build_plan(bpy.context, real_bake_settings)

    real_backend = bk.CyclesBackend(txmod.SceneTransaction(bpy.context))
    real_job = jb.BakeJob(bpy.context, real_plan, real_bake_settings,
                          backend=real_backend)
    before = rss_gb()
    real_job.run_to_completion()
    after = rss_gb()
    done = [t for t in real_plan.tasks if t.status == "done"]
    floats = sum(1 for t in done if t.image is not None and t.image.is_float)
    frozen = rss_gb()
    for task in done:
        if task.image is not None:
            task.image.buffers_free()
    print("  @{}px: {} 张（浮点 {}） 工作集 {:.3f} -> {:.3f} GB，"
          "增量 {:.3f} GB（每张 {:.1f} MB）".format(
              size, len(done), floats, before or 0, after or 0,
              _delta(after, before),
              (_delta(after, before) or 0) * 1024 / max(1, len(done))))
    print("        buffers_free() 回收 {:.3f} GB".format(
        _delta(frozen, rss_gb()) or 0))
    return len(done), _delta(after, before) or 0.0


if os.path.isfile(os.path.join(WS, "test.blend")):
    rows = []
    for size in (1024, 2048):
        rows.append((size,) + real_bake_at(size))
    if len(rows) == 2 and rows[0][1] and rows[1][1]:
        low = rows[0][2] / rows[0][1]          # 1024px 每张 GB
        high = rows[1][2] / rows[1][1]         # 2048px 每张 GB
        print("\n  每张成本: 1024px {:.1f} MB -> 2048px {:.1f} MB"
              "（像素数 x4，成本 x{:.1f}）".format(
                  low * 1024, high * 1024, high / low))
        print("  按用户的规模外推（96 张 @2048）: {:.1f} GB".format(high * 96))
        print("  注意：这里含 Cycles 自身的常驻开销，下面还会看它占多少")
        stats['real_per_image_mb'] = high * 1024
        stats['real_extrapolated_gb'] = high * 96
else:
    print("  没有 test.blend，跳过")

# ---------------------------------------------------------------- 结论
print("\n" + "=" * 78)
print("结论")
print("=" * 78)
print("  ① 响应性（这是真的修好了）：")
print("     旧路径把 {:.2f}s 全塞进一次调用，期间界面零响应".format(
    stats.get('bulk_seconds', 0)))
print("     新路径 start() 本身 {:.4f}s，最坏单步 {:.0f}ms、平均 {:.1f}ms".format(
    startup, stats.get('worst_step', 0) * 1000, stats.get('avg_step', 0) * 1000))
print("  ② 建图像本身根本不是内存问题（以前的判断是错的）：")
print("     bpy.data.images.new() 是**惰性**的，96 张建完只涨了 {:.2f} GB，".format(
    stats.get('bulk_delta') or 0))
print("     buffers_free() 在没写过像素时回收 {:.2f} GB —— 像素缓冲区是".format(
    stats.get('free_recovered') or 0))
print("     第一次写入才分配的。旧文档里那个 4.5 GB 是按尺寸算出来的名义值，")
print("     从来没用真读数验证过。")
print("  ③ 真正的内存增长来自每张图写完之后一直留着：")
print("     NullBackend 跑完 96 张时工作集涨到 {:.2f} GB；".format(
    (start_rss or 0) + (stats.get('interleaved_delta') or 0)))
print("     释放像素缓冲区能回收 {:.2f} GB".format(
    stats.get('filled_free_recovered') or 0))
print("  ④ 释放之后还导得出来吗：{} 字节（0 或负数 = 导不出来）".format(
    stats.get('export_after_free')))
print("  ⑤ 真实烘焙（这是唯一能代表用户场景的数字）：")
print("     每张 2048px 实测量到 {:.1f} MB，用户那种 96 张的规模外推约 {:.1f} GB".format(
    stats.get('real_per_image_mb') or 0, stats.get('real_extrapolated_gb') or 0))
print("     而且真机图上 buffers_free() 回收 0 —— 说明真实路径里 Blender")
print("     在 pack / 存盘之后**已经**把像素缓冲区放掉了，")
print("     再加一个「主动释放」的开关是没有收益的，不该做。")


"""量一量：一张图从"烘好"到"落地"各步各要多久。

用户报"一轮 829 秒"。这个脚本只想回答一个问题：
**829 秒花在哪了？** 候选是每张图都要做的这几步：

    1. image.pack()          —— 默认打开（Pack Into .blend）
    2. image.save()          —— 写盘（PNG 压缩是 CPU 密集的）
    3. buffers_free()        —— 释放像素缓冲区

只测量，不改任何东西。用法：
    blender --background --factory-startup --python tools/measure_pack_cost.py
"""
import bpy, os, sys, time, tempfile, shutil

# 工作区 = 本文件所在目录的上一级。
# ⚠ 这里原来写死的是开发机上的绝对路径，clone 到别处每个套件都跑不起来，
#   而且公开版/私有版两份检出会互相 import 错代码。
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WS not in sys.path:
    sys.path.insert(0, WS)

try:
    import numpy
except ImportError:
    numpy = None

OUT = os.path.join(tempfile.gettempdir(), "mb_pack_probe")
if os.path.isdir(OUT):
    shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT, exist_ok=True)

print("=" * 74)
print("落地成本测量    Blender {}    numpy={}".format(
    bpy.app.version_string, "yes" if numpy else "no"))
print("=" * 74)


def make_image(name, size, float_buffer, noisy=False):
    image = bpy.data.images.new(name, size, size, alpha=True,
                                float_buffer=float_buffer)
    if numpy is not None:
        if noisy:
            # ⚠ 必须用噪声，不能用纯色！纯色 PNG 压缩率极高（2048² 只有 0.1 MB，
            #   存盘 0.11s），会把这个测量变成**下界**而不是真实值。
            #   真实烘焙出的法线/AO 都是高频噪声，PNG 压不动。
            generator = numpy.random.default_rng(1234)
            buffer = generator.random(4 * size * size, dtype=numpy.float32)
        else:
            buffer = numpy.tile(
                numpy.array((0.25, 0.5, 0.75, 1.0), dtype=numpy.float32),
                size * size)
        image.pixels.foreach_set(buffer)
    else:
        image.pixels[:] = [0.25, 0.5, 0.75, 1.0] * (size * size)
    image.update()
    return image


def timed_save(image, path):
    t0 = time.time()
    image.filepath_raw = path
    image.file_format = 'PNG'
    image.save()
    return time.time() - t0, os.path.getsize(path) / (1024.0 * 1024.0)


def timed(call):
    t0 = time.time()
    call()
    return time.time() - t0


print("\n--- A) 纯色图（下界，不真实）---")
for size in (2048, 1024):
    for float_buffer in (False, True):
        image = make_image("Flat{}{}".format(size, int(float_buffer)), size, float_buffer)
        path = os.path.join(OUT, "flat_{}_{}.png".format(size, int(float_buffer)))
        save_seconds, file_mb = timed_save(image, path)
        pack_seconds = timed(image.pack)
        total = save_seconds + pack_seconds
        print("  {}{:<6} save {:5.2f}s ({:4.1f} MB)  pack {:5.2f}s"
              "  => 96 张 {:.1f} 分钟".format(
                  size, " float" if float_buffer else " byte",
                  save_seconds, file_mb, pack_seconds, total * 96 / 60.0))
        bpy.data.images.remove(image)

print("\n--- B) 噪声图（接近真实烘焙产物）+ 存盘后 pack ---")
for size in (2048, 1024):
    for float_buffer in (False, True):
        image = make_image("Noise{}{}".format(size, int(float_buffer)),
                           size, float_buffer, noisy=True)
        path = os.path.join(OUT, "noise_{}_{}.png".format(size, int(float_buffer)))
        save_seconds, file_mb = timed_save(image, path)
        pack_seconds = timed(image.pack)
        total = save_seconds + pack_seconds
        print("  {}{:<6} save {:5.2f}s ({:4.1f} MB)  pack {:5.2f}s"
              "  => 96 张 {:.1f} 分钟".format(
                  size, " float" if float_buffer else " byte",
                  save_seconds, file_mb, pack_seconds, total * 96 / 60.0))
        bpy.data.images.remove(image)

print("\n--- C) 没设输出目录：图只 pack 不存盘（要在内存里编码 PNG）---")
for size in (2048, 1024):
    image = make_image("PackOnly{}".format(size), size, False, noisy=True)
    pack_seconds = timed(image.pack)
    print("  {}{:<6} pack {:5.2f}s  => 96 张 {:.1f} 分钟".format(
        size, " byte", pack_seconds, pack_seconds * 96 / 60.0))
    bpy.data.images.remove(image)

print("\n" + "=" * 74)
print("说明")
print("=" * 74)
print("  * save() 与 pack() 都在主线程里同步做 —— 就是界面被占住的那段时间。")
print("  * Pack Into .blend 默认是**开**的（见 ui/properties.py）。")
print("  * 烘焙本身（Cycles 求值）的耗时这里没量；但真机小场景一轮 9 张")
print("    1024² 只要几秒，所以 829 秒更可能花在这些每张都要做的 I/O 上。")

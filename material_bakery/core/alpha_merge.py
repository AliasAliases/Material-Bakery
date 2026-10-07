# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   base_color 默认就带 alpha（实施轮 1.6）
#
#   用户的规矩（原话）："**能不能直接把 alpha 弄进 basecolor，别分家了，用户要选 alpha
#   通道那就给它一张单独的 alpha 通道贴图，默认的 basecolor 就是带 alpha，就这么简单，
#   不会再有多的什么带不带 alpha 的话题了！**"
#
#   ⚠ 为什么必须在**写盘之前**做：Cycles 的 EMIT **烘不进图的 A 分量**（实测 A 恒 1.0000，
#     灰度落在 RGB 上，见 DESIGN §39）—— 所以"base_color 带 alpha"不可能靠烘焙本身得到，
#     只能把同组 `shader.alpha` 那张图的灰度并进 base_color 图的 A 通道。
#
#   放在 `core/` 的理由：`engine/backend`（写盘前合并、建图时机）要用它，
#   `deliver` 层也看它一眼 —— core 是两层共享的底座，**engine 绝不能反向依赖 deliver**。
#
#   实测（`_probe/probe_r36_alpha_default.py`）：
#     - `images.new(alpha=True)` → depth 32（浮点 128），**A 默认就是 1.0000**（不透明）
#     - 烘 EMIT 进一张 alpha=True 的图：A 仍然是 1.0000（连烘两次也一样）
#   → 所以"alpha 任务失败 / 没勾"时，那张 4 通道的图存出来是**不透明**的，不会悄悄变全透明。
# ------------------------------------------------------------------------------------

import array
import os

import bpy

#: 拿它的灰度写进 A 的那一类图
ALPHA_SOURCE_TYPE = "shader.alpha"
#: 被写 A 的那一类图
BASE_COLOR_TYPE = "shader.base_color"
#: 存不住 alpha 的格式（实测 8 种格式的 `depth`，只有 JPEG 是 24 位，见 DESIGN §39）
ALPHA_LOSING_FORMATS = ("JPEG",)
#: 合并成功后必说的那句（甲方逐字给的）
MERGE_RGB_NOTE = ("Only the alpha channel is written. If your source texture is "
                  "premultiplied, the RGB still needs premultiplying — this plugin "
                  "does not touch RGB.")


def alpha_lost(file_format):
    """这种格式存不住 alpha（实测，见 DESIGN §39）"""
    return file_format in ALPHA_LOSING_FORMATS


def image_has_alpha(image):
    """这张图**真的有** alpha 通道吗

    ⚠ 不能看 `image.channels` —— RNA 层面它**恒为 4**（实测）。位深才是判据：
      8 位：带 alpha = 32，不带 = 24；浮点：带 = 128，不带 = 96。
    """
    depth = int(getattr(image, "depth", 0) or 0)
    return depth in (32, 128)


def _has_pixels(image):
    """读得到像素吗

    内存里有就行；内存里没有但**磁盘上那份文件还在**也行 —— Blender 会自己读回来
    （实测：`buffers_free()` 之后 `has_data` 是 False，`foreach_get` 仍然拿到正确数值）。
    两个都没有时**绝不能**照读：那会得到一片 0，等于把贴图悄悄变透明。
    """
    if image.has_data:
        return True
    source = bpy.path.abspath(image.filepath) if image.filepath else ""
    return bool(source and os.path.isfile(source))


def _read_rgba(image):
    """整幅像素读成 `array('f')`（RNA 层面永远是 RGBA），读不到返回 None

    ⚠ 不能用 `image.pixels[:]`：2048² 会造出上千万个 Python float 对象（几百 MB）。
      `foreach_get` 写进 `array('f')` 是 48 MB，实测 2048² 读两张 0.045s。
    """
    width, height = int(image.size[0]), int(image.size[1])
    if width <= 0 or height <= 0:
        return None
    channels = int(image.channels)
    count = width * height * channels
    buffer = array.array('f', bytes(4 * count))
    try:
        image.pixels.foreach_get(buffer)
    except (RuntimeError, ValueError):
        return None
    if channels == 4:
        return buffer
    if channels not in (3, 1):
        return None
    # 不是 4 通道就摊成 RGBA（A 补 1.0）——我们的图都是 4 通道，这只是不要炸
    expanded = array.array('f', bytes(16 * width * height))
    if channels == 3:
        expanded[0::4] = buffer[0::3]
        expanded[1::4] = buffer[1::3]
        expanded[2::4] = buffer[2::3]
    else:
        expanded[0::4] = buffer
        expanded[1::4] = buffer
        expanded[2::4] = buffer
    expanded[3::4] = array.array('f', [1.0]) * (width * height)
    return expanded


def alpha_values(alpha_pixels):
    """alpha 图里的那个"灰度"取哪一路 —— 返回 (数值, 人话说明)

    ⚠ **实测**（`_probe/probe_r34_alpha.py`）：Cycles 烘 EMIT **写不进图的 A 分量** ——
      目标图的 A 恒为 `1.0000`（8 位与浮点各烘一次，RGB 分别是 0.7373 / 0.5000，
      A 一律 1.0000）。灰度落在 **RGB 三个通道**上，所以正常路径取 R。
      A 通道只有在它自己**不是常数**时才说明这张图真的自带 alpha（例如救援路径
      从磁盘捡回来的 PNG），那时候才用它 —— 恒 1 或恒 0 都当"没有信号"。
    """
    alpha = alpha_pixels[3::4]
    if min(alpha) != max(alpha):
        return alpha, "its own A channel"
    return alpha_pixels[0::4], "its R channel"


def _write_alpha(base_image, alpha_image):
    """把 alpha 图的灰度写进 base 图的 A 通道（就地改 base，RGB 原样抄回去）"""
    rgb = _read_rgba(base_image)
    if rgb is None:
        return False, "Base Color's pixels could not be read"
    gray = _read_rgba(alpha_image)
    if gray is None:
        return False, "the Alpha map's pixels could not be read"
    source, label = alpha_values(gray)
    width, height = int(base_image.size[0]), int(base_image.size[1])
    out = array.array('f', bytes(16 * width * height))
    out[0::4] = rgb[0::4]
    out[1::4] = rgb[1::4]
    out[2::4] = rgb[2::4]
    out[3::4] = source
    base_image.pixels.foreach_set(out)
    base_image.update()
    return True, label


def merge_alpha_into(base_image, alpha_image, file_format='PNG'):
    """把 alpha 图的灰度并进 base_color 的 A 通道 —— 返回 `(合并成功了吗, [(级别, 说明)])`

    ⚠ **就地写**在 base_color 那张图上（不另建临时图）：那张图现在就是按 `alpha=True`
      建的（见 `merge_target_needs_alpha`），交付材质、预览、导出看到的是同一份数据。
    ⚠ 合不了**不抛异常**：调用方照样按原名落盘（纯 RGB），并把说明写进日志与报告 ——
      "静默丢弃是最糟的一种失败"。
    """
    if base_image is None:
        return False, []
    notes = []
    if alpha_image is None:
        return False, [("WARN", "no Alpha map in this collection — written without alpha")]
    if alpha_lost(file_format):
        # 仍然合并（内存里那张、以及材质接线都要一致），只是**文件**存不住 A
        notes.append(("WARN", "{} cannot store an alpha channel — the file will have no "
                              "alpha; use PNG or TIFF".format(file_format)))
    if not image_has_alpha(base_image):
        return False, [("WARN", "the Base Color image has no alpha channel to write into")]
    if not _has_pixels(base_image):
        return False, [("WARN", "Base Color has no pixel data (and no file on disk)")]
    if not _has_pixels(alpha_image):
        return False, [("WARN", "the Alpha map has no pixel data (and no file on disk)")]
    if (int(base_image.size[0]), int(base_image.size[1])) != \
            (int(alpha_image.size[0]), int(alpha_image.size[1])):
        return False, [("WARN",
                        "the Alpha map is {}x{} and Base Color is {}x{} — sizes differ, "
                        "written without alpha".format(
                            alpha_image.size[0], alpha_image.size[1],
                            base_image.size[0], base_image.size[1]))]
    ok, detail = _write_alpha(base_image, alpha_image)
    if not ok:
        return False, [("WARN", detail)]
    notes.append(("INFO", "Alpha merged into the Base Color's A channel (from {}) — {}".format(
        detail, MERGE_RGB_NOTE)))
    return True, notes


def groups_with_alpha(tasks):
    """这一轮**哪些集合**有 `shader.alpha` 任务（决定 base_color 要不要建成 4 通道）"""
    return {task.group_name for task in tasks or ()
            if task.bake_type.key == ALPHA_SOURCE_TYPE}


def merge_target_needs_alpha(task, alpha_groups):
    """这张图要不要建成 `alpha=True`（4 通道、存出来 depth 32）

    只有"该组这一轮有 `shader.alpha` 任务"的 `base_color` 才需要 —— 没勾 alpha 的集合
    维持 `alpha=False`（纯 RGB，和 1.5 之前一样，**名字也不加任何注明**）。
    """
    return (task.bake_type.key == BASE_COLOR_TYPE
            and task.group_name in (alpha_groups or ()))


class AlphaStitcher:
    """把"base_color 带 alpha"这件事在**写盘之前**接上（逐任务顺序问题都收在这里）

    ⚠ 为什么需要它：`collect()` 是**逐个任务**跑的，而 `shader.alpha` 在 Maps 里的位置
      由用户决定 —— 它可能排在 `base_color` **后面**。规则：
        - base_color 落盘时该组的 alpha 还没 collect → **先不落盘**，挂进 `pending`
        - 同组 alpha 来了 → 立刻合并 + 把挂着的 base_color 一起交出去落盘
        - `finalize()` / `abort()` 兜底：alpha 任务失败、没排上、被取消，也要把
          base_color 落盘（纯 RGB）—— **任何路径下文件与 manifest 都必须落地**
    """

    def __init__(self, tasks=(), file_format='PNG'):
        self.file_format = file_format
        self.alpha_groups = groups_with_alpha(tasks)
        self.alpha_images = {}      # 组名 -> 已经 collect 过的 alpha 图
        self.pending = {}           # 组名 -> 等 alpha 的 base_color 任务
        self.notes = []             # [(级别, 说明), ...]（收尾时倒进日志/报告）

    def needs_alpha(self, task):
        return merge_target_needs_alpha(task, self.alpha_groups)

    def before_write(self, task):
        """轮到这个任务落盘了 —— 返回**现在就该写**的任务列表（可能不止它自己）

        - base_color 且该组 alpha 还没到 → 挂起来，返回 `[]`（这一张先不落盘）
        - alpha 到了 → 先把挂着的 base_color 合并好还给你，再还给它自己
        - 其它类型 → 原样返回自己
        """
        key = task.bake_type.key
        if key == BASE_COLOR_TYPE:
            if task.group_name in self.alpha_groups \
                    and task.group_name not in self.alpha_images:
                self.pending[task.group_name] = task
                return []
            image = self.alpha_images.get(task.group_name)
            if image is not None:
                self._merge(task, image)
            return [task]
        if key == ALPHA_SOURCE_TYPE:
            self.alpha_images[task.group_name] = task.image
            ready = []
            base = self.pending.pop(task.group_name, None)
            if base is not None:
                self._merge(base, task.image)
                ready.append(base)
            ready.append(task)
            return ready
        return [task]

    def leftovers(self):
        """收尾兜底：还在等 alpha 的 base_color（alpha 失败 / 被取消 / 没排上）"""
        tasks = list(self.pending.values())
        self.pending.clear()
        return tasks

    def drain_notes(self):
        notes = list(self.notes)
        self.notes = []
        return notes

    def _merge(self, base_task, alpha_image):
        ok, notes = merge_alpha_into(base_task.image, alpha_image, self.file_format)
        for level, text in notes:
            self.notes.append((level, "{}: {}".format(base_task.image_name, text)))
        return ok

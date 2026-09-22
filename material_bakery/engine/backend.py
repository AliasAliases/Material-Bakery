# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   烘焙后端：把「怎么烘」这件事隔离在一层协议后面
#
#   BakeBackend 协议（引擎只认这四个方法）:
#       begin(plan)            — 一次性准备（切引擎、设采样）
#       prepare(task)          — 为任务准备图像数据块，返回 image
#       dispatch(task)         — 发起烘焙；返回 True 表示已经同步完成
#       poll()                 — True 表示还在烘
#       collect(task)          — 烘焙结束后收尾（保存文件等）
#       abort()                — 中断并清理
#
#   NullBackend 让整个引擎能在没有 Cycles、甚至没有 GPU 的环境里被测试。
# ------------------------------------------------------------------------------------

import os

import bpy

from .. import compat
from ..core import bake_types
from ..core import materials as materials_mod
from ..core import naming
from ..core import phases
from ..core import plan as plan_mod
from ..core import udim as udim_mod
from . import surgery as surgery_mod
from . import uv_pack


IMAGE_TAG = "mbakery_image"


# ------------------------------------------------------------------------------------
#   图像数据块

def find_ours(name):
    """按名字找我们建过的图像（带 marker），避免抢用户自己的同名图像"""
    image = bpy.data.images.get(name)
    if image is not None and IMAGE_TAG in image.keys():
        return image
    return None


def ensure_image(name, size_x, size_y, channel, file_format='PNG', reuse=True):
    """取得（或新建）一张烘焙目标图像。

    reuse=False 时强制新建一张干净图（内容清空），这才是「重烘」该有的行为。

    ⚠ 宽高是**两条边**：2026-09 起用户在第一页分别填长和宽，不再限制正方形。
      `size_y` 为 0/None 时按正方形处理（旧数据、测试里的简写）。
    """
    width = int(size_x or 0)
    height = int(size_y or 0) or width
    image = find_ours(name) if reuse else bpy.data.images.get(name)
    if image is not None and reuse:
        if image.size[0] != width or image.size[1] != height:
            image.scale(width, height)
        return image

    if image is not None:
        bpy.data.images.remove(image)

    image = bpy.data.images.new(name, width=width, height=height, alpha=False,
                                float_buffer=channel.is_data)
    image[IMAGE_TAG] = 1
    image.file_format = file_format
    image.colorspace_settings.name = compat.safe_colorspace_name(channel.colorspace)
    return image


def image_filepath(directory, name, file_format):
    return os.path.join(directory, "{}.{}".format(name, compat.file_extension(file_format)))


# ------------------------------------------------------------------------------------
#   后端协议

class BakeBackend:
    """接口说明用的基类 —— 引擎只按这些方法调用"""

    name = "abstract"
    synchronous = False

    def begin(self, plan, settings):
        return True

    def prepare(self, task):
        raise NotImplementedError

    def dispatch(self, task):
        raise NotImplementedError

    def poll(self):
        return False

    def collect(self, task):
        return True

    def abort(self):
        pass


# ------------------------------------------------------------------------------------
#   Null 后端：不做真实烘焙，只把图像填成可预期的颜色

class NullBackend(BakeBackend):
    """测试用后端。

    - 不碰渲染引擎，不调用 bpy.ops.object.bake
    - 每个任务把图像填成按类型决定的固定颜色
    - 记录每一个调用，方便断言顺序与参数
    """

    name = "null"
    synchronous = True

    def __init__(self, save=False, directory="", fail_on=()):
        self.save = save
        self.directory = directory
        self.fail_on = set(fail_on)      # 任务 key 前缀，模拟失败
        self.calls = []
        self.prepared = []
        self.aborted = False

    def begin(self, plan, settings):
        self.calls.append(("begin", len(plan.tasks)))
        return True

    def prepare(self, task):
        self.calls.append(("prepare", task.image_name))
        image = ensure_image(task.image_name, task.size_x, task.size_y,
                             task.bake_type.channel, file_format="PNG", reuse=False)
        task.image = image
        self.prepared.append(task.image_name)
        return image

    def dispatch(self, task):
        self.calls.append(("dispatch", task.key))
        if task.key in self.fail_on or task.group_name in self.fail_on:
            raise RuntimeError("simulated bake failure for {}".format(task.key))

        image = task.image
        if image is not None:
            _fill_image(image, task.bake_type)
        task.image = image
        return True

    def poll(self):
        return False

    def collect(self, task):
        self.calls.append(("collect", task.image_name))
        if self.save and self.directory and task.image is not None:
            path = image_filepath(self.directory, task.image_name, 'PNG')
            task.image.filepath_raw = path
            task.image.file_format = 'PNG'
            task.image.save()
            task.exported_path = path
        return True

    def abort(self):
        self.calls.append(("abort",))
        self.aborted = True


_FILL_CACHE = {}


def _fill_image(image, bake_type):
    """把图像填成固定颜色。

    ⚠ 2048×2048 是 1670 万个浮点。用 Python list 乘法再 foreach_set 要 16 秒一张，
    测试会慢到没法用 —— 所以用 Blender 自带的 numpy 按尺寸缓存一个缓冲区。
    """
    width, height = image.size
    key = (width, height, bake_type.channel)
    buffer = _FILL_CACHE.get(key)
    if buffer is None:
        color = _fill(bake_type)
        try:
            import numpy
            buffer = numpy.tile(numpy.array(color, dtype=numpy.float32), width * height)
        except ImportError:
            buffer = list(color) * (width * height)
        _FILL_CACHE[key] = buffer
    image.pixels.foreach_set(buffer)
    image.update()


def _fill(bake_type):
    """按通道给一个固定颜色 —— 数据贴图用 0.5 灰，法线用 (0.5,0.5,1)"""
    from ..core.bake_types import DataChannel
    channel = bake_type.channel
    if channel is DataChannel.VECTOR:
        return (0.5, 0.5, 1.0, 1.0)
    if channel.is_data:
        return (0.5, 0.5, 0.5, 1.0)
    return (0.25, 0.5, 0.75, 1.0)


# ------------------------------------------------------------------------------------
#   Cycles 后端：真的烘

# bake_pass -> 需要显式指定的 pass_filter
PASS_FILTERS = {
    'DIFFUSE': {'DIRECT', 'INDIRECT', 'COLOR'},
    'GLOSSY': {'DIRECT', 'INDIRECT', 'COLOR'},
    'TRANSMISSION': {'DIRECT', 'INDIRECT', 'COLOR'},
}


class CyclesBackend(BakeBackend):
    """真实烘焙。

    所有对场景设置、选中状态、材质节点的改动都走 SceneTransaction，
    引擎负责在合适的时机 commit / rollback。

    兼容性：bake 操作符的参数各版本有增删，统一经 compat.call_operator 过滤。
    """

    name = "cycles"
    synchronous = False           # 由 poll() 实际判断

    def __init__(self, tx, margin=16, margin_type='ADJACENT_FACES', use_clear=True,
                 samples=None, use_denoising=None, save_mode='INTERNAL',
                 save_directory="", save_files=True, device=None,
                 sampled_samples=None):
        self.tx = tx
        self.surgery = surgery_mod.NodeSurgery()
        self.margin = margin
        self.margin_type = margin_type
        self.use_clear = use_clear
        self.samples = samples
        # 需要真实采样的通道（AO / 阴影 / 直接-间接光）用的采样数。
        # None 表示跟 self.samples 一样 —— 不给的话行为与以前完全一致。
        self.sampled_samples = sampled_samples
        self.current_samples = None     # 上一次实际设置的采样数（少一次多余的写入）
        self.use_denoising = use_denoising
        self.save_mode = save_mode
        self.save_directory = save_directory
        self.save_files = save_files
        self.device = device
        self.current = None
        self.file_format = 'PNG'      # 由 begin() 从设置里取，别问 image.file_format
        self.use_subfolders = True     # 同样由 begin() 从设置里取
        self.bake_method = 'EMISSION'  # 'EMISSION'（节点手术）或 'NATIVE'（原生 pass）
        self.settings = None           # begin() 里存下来，dispatch 时要读边距设置
        self._scratch = None           # UDIM 临时目录（没设输出目录时才用）
        self.dropped_params = []       # 这个 Blender 版本不接受的 bake 参数
        self._exclusive = None         # 已经挂过烘焙节点的材质，避免重装
        self._manifests = {}           # {集合名: {type_key: 记录}} —— 写进输出目录
        self.uv_layer_name = ""        # 供 manifest 记录（每张图用的是哪层 UV）
        self.hidden_unrelated = []     # Hide Unrelated 藏起来的物体名（仅记录）

    # -- 生命周期 ---------------------------------------------------------------

    def begin(self, plan, settings):
        if not self.tx.set_render_engine('CYCLES'):
            raise RuntimeError("Cycles is not available in this Blender build")

        self.file_format = settings.file_format
        self.use_subfolders = getattr(settings, "use_subfolders", True)
        self.bake_method = getattr(settings, "bake_method", 'EMISSION') or 'EMISSION'
        self.settings = settings

        bake = self.tx.context.scene.render.bake
        self.tx.set_bake_setting("margin", self.margin)
        self.tx.set_bake_setting("margin_type", self.margin_type)
        self.tx.set_bake_setting("use_clear", self.use_clear)
        self.tx.set_bake_setting("use_selected_to_active", False)
        self.tx.set_bake_setting("target", 'IMAGE_TEXTURES')
        if hasattr(bake, "save_mode"):
            self.tx.set_bake_setting("save_mode", self.save_mode)

        if self.samples is not None:
            self.tx.set_cycles_setting("samples", self.samples)
            self.current_samples = self.samples
        if self.use_denoising is not None:
            self.tx.set_cycles_setting("use_denoising", self.use_denoising)
        if self.device and hasattr(self.tx.context.scene.cycles, "device"):
            self.tx.set_cycles_setting("device", self.device)

        image_settings = getattr(bake, "image_settings", None)
        if image_settings is not None:
            image_settings.file_format = settings.file_format
            if settings.file_format in ('OPEN_EXR', 'OPEN_EXR_MULTILAYER'):
                image_settings.color_depth = '32'

        # 最后一步：把不在本次范围内的物体藏起来。放在这里而不是 dispatch：
        # 那是个每张图都跑一次的路径，藏/放会带来 2×任务数的视图层更新。
        self.hidden_unrelated = []
        if getattr(settings, "hide_unrelated", False):
            self.hidden_unrelated = self._hide_unrelated(plan)
        return True

    def _hide_unrelated(self, plan):
        """把不属于本次运行的网格藏起来 —— Cycles 的 BVH 和贴图就只建我们这批。

        ⚠ 只藏**网格**，不碰灯光和摄像机：Combined / Shadow 这类通道是要用光的，
          把灯藏了那几个通道会直接变黑（那是"改了没反应"里最难查的一种）。
        ⚠ 投影烘焙的**源**必须留着，否则光线打不到高模，烘出来是全黑。
        """
        view_layer = self.tx.context.view_layer
        keep = set()
        for group in plan.groups:
            for obj in group.objects:
                keep.add(obj.name)
        for task in plan.tasks:
            for obj in (task.sources or ()):
                keep.add(obj.name)
            for obj in (task.targets or ()):
                keep.add(obj.name)

        doomed = []
        for obj in view_layer.objects:
            if obj.type != 'MESH' or obj.name in keep:
                continue
            doomed.append(obj)
        hidden = self.tx.hide_objects(doomed, keep=keep)
        phases.mark("hide unrelated: {} object(s) hidden, {} kept visible".format(
            len(hidden), len(keep)))
        return hidden

    def prepare(self, task):
        # UDIM 现在也是一张普通平铺图（烘焙前把 UV 平移过来，见 engine/job.py），
        # 所以这里不再需要 tiled image 那套东西。
        image = ensure_image(task.image_name, task.render_size_x, task.render_size_y,
                             task.bake_type.channel, file_format=self.file_format,
                             reuse=True)
        task.image = image
        return image

    # -- 派发 -------------------------------------------------------------------

    def dispatch(self, task):
        targets = [o for o in task.targets if o.name in bpy.data.objects]
        if not targets:
            raise RuntimeError("no target object left in the scene")

        # 投影烘焙：源也要一起选中，目标当 active（见 SceneTransaction.select_for_bake）
        projects = task.projects
        sources = [o for o in task.sources if o.name in bpy.data.objects] if projects else []
        if projects and not sources:
            raise RuntimeError("selected-to-active was requested but no source object survived")
        self.tx.select_for_bake(targets, sources,
                               unhide_sources=getattr(self.settings, "unhide_sources", True))

        self._install_targets(targets, task.image)

        # 采样数按通道分档：确定值通道用 self.samples，需要真采样的通道
        # （AO / 阴影 / 光路类）用 self.sampled_samples。写在派发**之前**，
        # 而且走事务 —— 烘完由 commit/rollback 还回场景原来的值。
        wanted = self.samples_for(task.bake_type)
        if wanted is not None and wanted != self.current_samples:
            self.tx.set_cycles_setting("samples", wanted)
            self.current_samples = wanted

        # 烘焙方式：默认「节点手术 + EMIT」，可选「原生 pass」（见 bake_types.NATIVE_PASSES）。
        # ⚠ 原生 pass 表里没有的类型会**自动退回**手术，并把原因写进任务明细 ——
        #   静默换方法是最糟的：用户选了 Native、结果某个通道还是走了手术，却没人说。
        native = None
        if self.bake_method == 'NATIVE':
            native = bake_types.native_pass(task.bake_type.key)

        # 节点手术：把要烘的那股信号临时接到 Emission 上（EMIT 烘焙只看自发光）
        #
        # ⚠ 手术要施加在**采样源**的材质上，不是目标的。
        #   投影烘焙时 Blender 求值的是**源**（高模）那一片表面的着色，
        #   结果再写进目标的 UV。如果照目标材质做手术，源材质里没有 Emission，
        #   EMIT 烘出来就是一张纯黑图，而且不报任何错。
        #   不投影时源就是目标，两者等价。
        self.surgery.revert_all()
        if native is not None:
            task.surgery_detail = ["native pass: {} {}".format(
                native[0], " ".join(sorted(native[1].get("pass_filter", ())))).strip()]
        elif surgery_mod.needs_surgery(task.bake_type):
            shading_objects = sources if projects else targets
            materials = surgery_mod.collect_materials(shading_objects)
            done = self.surgery.apply_materials(materials, task.bake_type, task.options)
            if not done:
                raise RuntimeError("could not prepare node tree: {}".format(
                    "; ".join("{}: {}".format(name, reason)
                              for name, reason in self.surgery.skipped[:2])
                    or "no material to work on"))
            task.surgery_detail = [r.detail for r in self.surgery.records]
        elif self.bake_method == 'NATIVE':
            task.surgery_detail = ["native pass unavailable for {}, used emission".format(
                task.bake_type.label)]

        image_settings = getattr(self.tx.context.scene.render.bake, "image_settings", None)
        if image_settings is not None:
            image_settings.color_depth = task.bake_type.channel.depth

        kwargs = {
            "type": task.bake_type.bake_pass,
            "use_clear": self.use_clear,
            "margin": plan_mod.effective_margin(task.size_x, task.size_y, self.settings),
            "margin_type": self.margin_type,
            "use_selected_to_active": projects,
            "target": 'IMAGE_TEXTURES',
            "save_mode": self.save_mode,
        }
        if native is not None:
            kwargs["type"] = native[0]
            kwargs.update(native[1])
        # provider 追加的参数（光线距离 / cage 挤出顶起）
        for key, value in (task.bake_params or {}).items():
            kwargs[key] = value

        # ⚠ pass_filter 要在原生 pass 之后处理：原生 Base Color 只要 COLOR 那一路，
        #   被这里通用的 DIFFUSE 过滤器盖掉的话就会把光照也烘进去。
        if native is None:
            pass_filter = PASS_FILTERS.get(task.bake_type.bake_pass)
            if pass_filter:
                kwargs["pass_filter"] = pass_filter
        if task.bake_type.bake_pass == 'NORMAL':
            # 法线空间是可选项（切线 / 物体），默认切线
            kwargs["normal_space"] = task.options.get("normal_space", 'TANGENT')

        operator = self._bake_operator()
        self.current = task
        # ⚠ 派发的计时**不在这里** —— 它在 engine/job.py 的 _step_bake 里，
        #   这样 NullBackend / 以后的别的后端也照样有时钟（见 core/phases.py）。
        result = self._run_bake(operator, kwargs)

        if 'CANCELLED' in result:
            raise RuntimeError("bake operator returned CANCELLED")
        self.synchronous = not self.poll()
        return self.synchronous

    def _run_bake(self, operator, kwargs):
        """真正调用烘焙操作符。

        单独抽成一个方法有两个原因：
          1. 参数过滤集中在一处，被丢掉的参数会被记下来并报给用户
             （静默丢弃害过一次：S2A 的 ray_distance 参数名写错，
              用户把光线距离调到 1.0 毫无效果，烘出来全黑且不报错）
          2. 测试要能截到"到底传了什么参数"。bpy.ops 是懒命名空间，
             往里赋属性不生效，所以在方法上留一个正经的接缝。
        """
        usable, dropped = compat.supported_kwargs(operator, kwargs)
        if dropped:
            self.dropped_params = sorted(
                set(getattr(self, "dropped_params", [])) | set(dropped))
        return operator(**usable)

    def _bake_operator(self):
        """按当前选择的物体类型挑操作符（多物体烘焙在 3.x 与 4.x 上位置不同）"""
        candidates = ("bake", "bake_image")
        for name in candidates:
            if name in dir(bpy.ops.object):
                return getattr(bpy.ops.object, name)
        raise RuntimeError("no usable bake operator in this Blender build")

    def samples_for(self, bake_type):
        """这个通道该用多少采样。None = 不改（跟随场景/渲染设置）。

        ⚠ 两类通道必须分开（见 core/bake_types.SAMPLED_PASSES）：
          确定值通道烘 1 个采样就够，AO/阴影/光路类少了就是噪点。
          用户那轮 128 张 2048² 花了 1301 秒，绝大部分是确定值通道在重复算同一张图。
        """
        if bake_type is not None and bake_type.needs_sampling:
            if self.sampled_samples is not None:
                return self.sampled_samples
            return self.samples
        return self.samples

    def _install_targets(self, targets, image):
        """给目标物体的每个材质挂烘焙目标节点（同一材质只挂一次）"""
        installed = set()
        naked = []
        for obj in targets:
            if obj.type != 'MESH' or obj.data is None:
                continue
            if not any(slot.material is not None for slot in obj.material_slots):
                naked.append(obj.name)
                continue
            for slot in obj.material_slots:
                material = slot.material
                if material is None or material.name in installed:
                    continue
                installed.add(material.name)
                self.tx.install_bake_target(material, image)
        if not installed:
            raise RuntimeError("target object(s) have no material to bake into")
        # ⚠ 有点名比什么都重要：Blender 的原文是
        #   "No active image found, add a material or bake to an external file"，
        #   既不说是哪个物体，也不说为什么。计划阶段现在就会把它们剔掉，
        #   这里留一道兜底，万一还有漏网的，至少说得出名字。
        if naked:
            compat.log("烘焙目标里没有材质的物体（会被 Blender 整批拒绝）: {}".format(
                ", ".join(naked[:5])))


    def poll(self):
        """烘焙操作符在 GUI 里是异步 job；背景模式下同步完成"""
        checker = getattr(bpy.app, "is_job_running", None)
        if checker is None:
            return False
        try:
            return bool(checker('OBJECT_BAKE'))
        except (TypeError, ValueError):
            return False

    def collect(self, task):
        # 手术必须在烘焙结果落盘之前还原：Emission 那套脚手架不能留在用户的材质里
        phases.begin("collect {}".format(task.image_name))
        self.surgery.revert_all()
        image = task.image
        if image is None:
            phases.end("collect {}".format(task.image_name), "no image")
            return True
        if self.save_files and self.save_directory:
            # ⚠ 用设置里的格式，不要用 image.file_format ——
            #   pack() 会把 float 图像的 file_format 改成 OPEN_EXR，
            #   于是"导出 PNG"会莫名其妙变成 .exr 文件。
            folder = self._folder_for(task)
            path = image_filepath(folder, task.image_name, self.file_format)
            image.filepath_raw = path
            image.file_format = self.file_format
            phases.begin("save {}".format(task.image_name))
            try:
                image.save()
            finally:
                phases.end("save {}".format(task.image_name))
            task.exported_path = path
            if task.tile:
                task.exported_tiles = [(task.tile, path)]
            self._write_manifest(task, folder)
        phases.end("collect {}".format(task.image_name))
        return True

    def _write_manifest(self, task, folder):
        """把这张图记进它那个集合的 manifest

        为什么值得写：用户那次跑了 21.7 分钟、128 张图都落盘了，然后 Blender 卡住
        只能强杀 —— 于是那些图在文件夹里躺着，插件却说"这次会话什么都没烘"。
        有了 manifest，"从磁盘救回来"就是精确读取，不用靠文件名猜
        （文件名是用户可改的，manifest 是我们自己写的）。
        写法是**每张图落地就重写一次**：崩溃时已经烘好的部分照样有记录。
        """
        from ..core import imported as imported_mod

        group = task.group_name or ""
        record = self._manifests.setdefault(group, {})
        record[task.bake_type.key] = {
            "type": task.bake_type.key,
            "label": task.bake_type.label,
            "file": os.path.basename(task.exported_path),
            "size": int(max(task.size_x, task.size_y)),
            "size_x": int(task.size_x),
            "size_y": int(task.size_y),
            "tile": int(task.tile or 0),
        }
        # 记下"这张图是用哪一层 UV 烘的" —— 2026-09 之后不再打包 UV，
        # 所以这里就是物体当时的渲染层名（救援时能看出对不对得上）。
        uv_name = ""
        for target in (task.targets or []):
            uv_name = uv_pack.describe_layers(target)
            if uv_name:
                break
        imported_mod.write_manifest(
            folder, group, record,
            uv_layer=uv_name,
            extra={"blender": bpy.app.version_string, "packed_uv": False})

    def _folder_for(self, task):
        """这个任务的图片存到哪个目录。

        ⚠ 这里原来**忽略了"每集合一个子文件夹"这个设置** —— 引擎把 96 张图
          一股脑平铺在输出根目录下，而第 4 页的"导出贴图"却又按子文件夹写。
          结果是用户烘完看到的是一堆平铺文件（他报的就是这个），
          只有手动点一次导出才会出现集合文件夹。
          现在两处用同一套规则。
        """
        if not self.use_subfolders or not task.group_name:
            return self.save_directory
        return os.path.join(self.save_directory,
                            naming.sanitize_filename(task.group_name))

    def abort(self):
        self.surgery.revert_all()

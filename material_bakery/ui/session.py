# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   会话：把活动中的 BakeJob 拿在手上，并用 timer 一步步驱动它
#
#   引擎自己没有全局状态（见 engine/job.py 的说明）；这里的单例是 UI 层的东西 ——
#   Blender 只有一个界面，同一时刻只可能有一次向导会话。
#
#   ⚠ 运行期间 tick() 不写任何 IDProperty。进度、当前任务、日志全部现读现画。
# ------------------------------------------------------------------------------------

import os
import time

import bpy

from .. import compat
from ..core import guard as guard_mod
from ..core import phases
from ..core import plan as plan_mod
from ..core import scene_scan
from ..core import transaction as transaction_mod
from ..engine import backend as backend_mod
from ..engine import job as job_mod
from ..engine import report as report_mod

TICK_INTERVAL = 0.1
LOG_LIMIT = 200
# 日志同时**落盘**。为什么必须这么做：用户那次烘焙跑完时 Blender 无响应，
# 他"发不了 log 也 stop 不了"，我这边只能靠猜。写一份到临时目录，
# 强杀之后现场还在，下次不用再猜。
LOG_FILE_NAME = "material_bakery_last_run.log"


def log_path():
    import tempfile
    return os.path.join(tempfile.gettempdir(), LOG_FILE_NAME)


def _phase_sink(line):
    """core/phases 的日志落点：阶段行要进会话日志（= 落盘那份）。

    ⚠ 不能只走 compat.log —— 那只是 Blender 的控制台，GUI 里看不到，
      而"事后能查"正是加这套计时的唯一原因。
    ⚠ 每次都现取 Session 实例（模块级函数，不是绑定方法）：换过文件之后
      Session 单例会被丢掉重建，绑在旧实例上的 sink 会把阶段行写进一个
      已经作废的会话里。
    """
    instance = Session._instance
    if instance is None:
        compat.log(line)
        return
    instance.add_log("PHASE", line)


def is_alive(block):
    """这个 bpy 数据块还在不在？

    ⚠ 撤销/重做会真的把数据块删掉，而 Python 手里可能还留着引用。
      判断方式必须是"试着读一下它的属性"：`bl_rna` 是访问已释放结构时
      第一个炸的地方，直接 `if block is None` 是不够的。
    """
    if block is None:
        return False
    try:
        block.bl_rna
    except ReferenceError:
        return False
    except Exception:
        return False
    return True


class LogEntry:
    __slots__ = ("time", "level", "message")

    def __init__(self, level, message):
        self.time = time.time()
        self.level = level
        self.message = message

    def stamp(self):
        return time.strftime("%H:%M:%S", time.localtime(self.time))

    def __repr__(self):
        return "[{}] {}: {}".format(self.stamp(), self.level, self.message)


class Session:
    """一次向导会话。`Session.get()` 拿单例。"""

    _instance = None

    def __init__(self):
        self.reset()

    # ------------------------------------------------------------------ 单例

    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def forget(cls):
        """注销插件时丢掉单例（timer 也要停）"""
        if cls._instance is not None:
            cls._instance.stop_timer()
            cls._instance.reset()
        cls._instance = None

    def reset(self):
        # 运行期借走的偏好设置（全局撤销 / 自动保存）在每一条退出路径上都要还
        # —— 换文件、注销插件、重开会话都走这里。
        self.release_guard()
        self.release_hidden_objects()
        self.job = None
        self.plan = None
        self.report = None
        self.settings = None
        self.store = None
        self.transaction = None
        self.materials_by_group = {}
        self.link_reports = {}
        self.created_objects = []
        self.export_outcomes = []
        # 从磁盘救回来的贴图（见 rescue_* 方法）：{组名: {type_key: image}}
        self.imported_by_group = {}
        self.import_source = ""
        self.import_unmatched = []
        # 文件里认出来的已烘材质（detect_existing）
        self.detected_materials = 0
        self.detect_attempted = False
        self.close_log_file()
        self.backend_factory = None      # 测试注入点：callable(context, settings) -> backend
        self.log = []
        self.error = ""
        self.started_at = 0.0
        self.finished_at = 0.0
        self._timer_running = False
        self.run_count = 0

    # ------------------------------------------------------------------ 查询

    @property
    def is_running(self):
        return self.job is not None and not self.job.is_finished

    @property
    def has_run(self):
        return self.report is not None

    @property
    def succeeded(self):
        return self.report is not None and self.report.ok

    def current_task(self):
        if self.job is None:
            return None
        return self.job.current_task()

    def progress(self):
        if self.job is None:
            return 0, 0, 0.0
        return self.job.progress

    def eta(self):
        if self.job is None:
            return None
        return self.job.eta()

    def add_log(self, level, message):
        entry = LogEntry(level, message)
        self.log.append(entry)
        if len(self.log) > LOG_LIMIT:
            del self.log[:len(self.log) - LOG_LIMIT]
        self._write_log_line(entry)

    # -- 运行期的偏好设置护栏 -----------------------------------------------------

    def pause_preferences(self):
        """运行开始：暂停全局撤销与自动保存（见 core/guard.py 的来龙去脉）"""
        guard = getattr(self, "guard", None)
        if guard is None:
            guard = guard_mod.PreferenceGuard()
            self.guard = guard
        engaged = guard.engage(bpy.context)
        for level, message in guard.drain_messages():
            self.add_log(level, message)
        return engaged

    def release_guard(self):
        """把偏好设置还回去。幂等；没借过就什么都不做。"""
        guard = getattr(self, "guard", None)
        if guard is None:
            self.guard = guard_mod.PreferenceGuard()
            return False
        released = guard.release()
        for level, message in guard.drain_messages():
            self.add_log(level, message)
        return released

    def release_hidden_objects(self):
        """兜底：把 Hide Unrelated 临时藏起来的物体放回去。

        ⚠ 正常路径由事务的 commit / rollback 负责（见 core/transaction.py）。
          这里是第二条保险：万一这一轮根本没走到收尾（模态被别的模态顶掉、
          异常穿过 job 的兜底、用户中途注销插件），也不能让几百个物体永远
          藏在场景里 —— 那种场面用户根本不知道自己按了什么。
        """
        transaction = getattr(self, "transaction", None)
        if transaction is None:
            return 0
        try:
            released = transaction.unhide_run_objects()
        except Exception as exc:
            compat.log("放出临时隐藏的物体失败: {}".format(exc))
            return 0
        if released:
            self.add_log("NOTE", "Put {} object(s) that were hidden for the bake back "
                                 "into the view".format(released))
        return released

    # -- 日志落盘 ---------------------------------------------------------------

    def _write_log_line(self, entry):
        """把一条日志追加到磁盘。每行 flush —— 崩了也不丢。"""
        handle = getattr(self, "_log_handle", None)
        if handle is None:
            handle = self._open_log_file()
        if handle is None:
            return
        try:
            handle.write("[{}] {}: {}\n".format(entry.stamp(), entry.level,
                                                entry.message))
            handle.flush()
        except (OSError, ValueError):
            self._log_handle = None

    def _open_log_file(self, truncate=False):
        """打开日志文件。truncate=True 表示这是新一轮运行，从头写"""
        handle = getattr(self, "_log_handle", None)
        if handle is not None:
            try:
                handle.close()
            except (OSError, ValueError):
                pass
            self._log_handle = None
        try:
            handle = open(log_path(), "w" if truncate else "a",
                          encoding="utf-8", buffering=1)
        except OSError:
            return None
        self._log_handle = handle
        if truncate:
            handle.write("=== Material Bakery run {} (Blender {}) ===\n".format(
                time.strftime("%Y-%m-%d %H:%M:%S"), bpy.app.version_string))
            handle.flush()
        return handle

    def close_log_file(self):
        """关掉落盘句柄。

        ⚠ 用 getattr 兜住：reset() 是在 __init__ 里调用的，那时 _log_handle
          还没被赋值 —— 第一版这里直接 AttributeError，整个 Session 建不出来。
        """
        handle = getattr(self, "_log_handle", None)
        if handle is not None:
            try:
                handle.close()
            except (OSError, ValueError):
                pass
        self._log_handle = None

    # ------------------------------------------------------------------ 失效引用

    def valid_materials_by_group(self):
        """只返回**还活着**的材质。

        ⚠ 用户按 Ctrl+Z 撤销"Remove Baked Slot"之类的操作时，撤销会删掉
          在那一步之后创建的数据块 —— 也就是我们刚建好的烘焙材质。
          而 `materials_by_group` 里握着的是 Python 引用，一碰就是
          `ReferenceError: StructRNA of type Material has been removed`，
          面板 draw() 随即抛异常、整页剩下的控件全部消失（他报的就是这个）。
          所以 UI 一律走这个函数取材质，死引用直接跳过。
        """
        result = {}
        for group_name, entry in (self.materials_by_group or {}).items():
            if isinstance(entry, tuple):
                material, link_report = entry
            else:
                material, link_report = entry, None
            if is_alive(material):
                result[group_name] = (material, link_report)
        return result

    def valid_created_objects(self):
        """同上：过滤掉已被撤销/删除的物体引用"""
        return [obj for obj in (self.created_objects or []) if is_alive(obj)]

    def valid_images_by_group(self):
        result = {}
        for group_name, images in self.images_by_group().items():
            alive = {key: image for key, image in images.items() if is_alive(image)}
            if alive:
                result[group_name] = alive
        return result

    def revalidate(self):
        """撤销/重做之后调用：丢掉已经失效的引用，别让 UI 抱着死指针"""
        before = len(self.materials_by_group or {})
        self.materials_by_group = {name: entry
                                   for name, entry in self.valid_materials_by_group().items()}
        self.created_objects = self.valid_created_objects()
        self.store = None                     # 材质仓库里也全是引用，直接丢掉重建
        dropped = before - len(self.materials_by_group)
        if dropped:
            self.add_log("WARN", "Undo removed {} built material(s) — press Build "
                                 "Materials again if you need them".format(dropped))
        return dropped

    def objects_with_baked_slot(self):
        """当前有多少物体上挂着烘焙槽（面板上要显示）"""
        from ..core import materials as materials_mod
        try:
            return [obj for obj in bpy.data.objects
                    if is_alive(obj) and materials_mod.has_baked_applied(obj)]
        except ReferenceError:
            return []

    # ------------------------------------------------------------------ 认出文件里已有的烘焙材质

    def detect_existing(self, settings=None):
        """在这个 .blend 里认出"我们已经烘过的材质"，让第 5 页直接可用

        用户的原话："第五页那扫目录，要是用户之前烘焙过了，有材质了就能直接进行
        烘焙后的操作，你看能不能实现"

        判据很硬：交付材质身上带着我们自己的标记（`mbakery_final_material`），
        名字就是集合名。所以**不需要扫目录、也不需要这次会话的报告** ——
        用户上次烘完保存过、或者只是没关文件，都能立刻接着做交付动作。

        只认一次（`detect_attempted`）：有报告 / 已经有材质时不覆盖当前会话状态。
        返回认出几个材质。
        """
        if self.materials_by_group or self.report is not None:
            return len(self.materials_by_group or {})
        if self.detect_attempted:
            return 0
        self.detect_attempted = True

        from ..core import materials as materials_mod
        found = {}
        for material in bpy.data.materials:
            if not is_alive(material):
                continue
            try:
                owned = bool(material.get(materials_mod.MARKER_FINAL))
            except (AttributeError, ReferenceError):
                continue
            if owned:
                found[material.name] = (material, None)
        if not found:
            return 0

        self.materials_by_group = found
        self.detected_materials = len(found)
        if settings is not None and not settings.textures:
            self._fill_textures_from_materials(settings, found)
        slots = len(self.objects_with_baked_slot())
        self.add_log("IMPORT", "Found {} baked material(s) already in this file "
                               "({} object(s) carry a baked slot) — the last page is "
                               "ready without scanning".format(len(found), slots))
        if not slots:
            self.add_log("NOTE", "Press 'Add Slot' to put them on the objects")
        return len(found)

    def _fill_textures_from_materials(self, settings, materials_by_group):
        """从材质的图像节点反推贴图列表（第 5 页要显示）

        通道靠节点的 label 认（我们建节点时写的就是类型标签），
        尺寸靠图像本身。这样"认出已有材质"之后贴图列表也是满的。
        """
        from ..core import bake_types
        by_label = {}
        for key in bake_types.ORDER:
            bake_type = bake_types.get(key)
            by_label[bake_type.label] = key

        settings.textures.clear()
        for group_name, entry in materials_by_group.items():
            material = entry[0] if isinstance(entry, tuple) else entry
            if not is_alive(material) or material.node_tree is None:
                continue
            for node in material.node_tree.nodes:
                if node.type != 'TEX_IMAGE' or node.image is None:
                    continue
                image = node.image
                label = node.label or image.name
                type_key = by_label.get(label, "")
                row = settings.textures.add()
                row.image_name = image.name
                row.group = group_name
                row.type_key = type_key
                row.type_label = label
                row.size = image.size[0] if image.size else 0
                row.status = "imported"
                row.imported = True
                try:
                    row.exported_path = bpy.path.abspath(image.filepath) \
                        if image.filepath else ""
                except (AttributeError, ReferenceError):
                    row.exported_path = ""
        settings.texture_index = 0
        return len(settings.textures)

    # ------------------------------------------------------------------ 日志

    def tail_log_file(self, limit=8):
        """读磁盘上那份日志的最后几行（本次会话没有日志时给面板用）

        ⚠ 用户的原话："我的 log 没了" —— 他指的是 Blender 里那行红色报错。
          所以现在：ERROR 会同时写进这份文件，而且**没有会话日志时面板也会显示
          磁盘上的那份**，至少还留个现场。
        """
        path = log_path()
        if not os.path.isfile(path):
            return []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                lines = [line.rstrip("\n") for line in handle]
        except OSError:
            return []
        return lines[-limit:] if limit else lines

    # ------------------------------------------------------------------ 结果

    def succeeded_tasks(self):
        if self.job is None:
            return []
        return [t for t in self.job.plan.tasks if t.status == "done" and t.image is not None]

    def images_by_group(self):
        """{组名: {type_key: image}} —— 建最终材质要用

        优先用**从磁盘救回来的**：用户强杀过之后，内存里的任务记录没了，
        但那些图是实实在在落地了的，不该因为"这次会话没有报告"就全废。
        """
        if self.imported_by_group:
            return self.imported_by_group
        result = {}
        for task in self.succeeded_tasks():
            result.setdefault(task.group_name, {})[task.bake_type.key] = task.image
        return result

    def packed_uv_layer(self):
        """本次是否用了打包 UV 层（决定最终材质要不要指定 UV 名）

        ⚠ 要能在**没有 job** 的情况下工作（从磁盘救回来时没有计划）：
          那就扫场景里所有网格，看有没有那一层。
        """
        from ..engine import uv_pack
        names = _existing_names()
        if self.job is not None:
            candidates = [obj for group in self.job.plan.groups for obj in group.objects]
        else:
            candidates = [obj for obj in bpy.data.objects if obj.type == 'MESH']
        for obj in candidates:
            if obj.name in names and obj.data is not None:
                if obj.data.uv_layers.get(uv_pack.PACK_LAYER_NAME):
                    return uv_pack.PACK_LAYER_NAME
        return ""

    # ------------------------------------------------------------------ 从磁盘救回来

    def scan_output(self, settings, folder=""):
        """扫输出目录，把已经烘好的贴图接回第 5 页。

        用户的原话："直接扫一遍目录，要是有贴图就让第五页可以互动，
        我就能直接创建材质然后把贴图连上去了。"
        返回 (张数, 集合数, 没认出来的张数)。
        """
        from ..core import imported as imported_mod

        root = folder or (bpy.path.abspath(settings.output_dir)
                          if settings.output_dir else "")
        if not root or not os.path.isdir(root):
            self.add_log("ERROR", "Nothing to scan: '{}' is not a folder".format(root))
            return 0, 0, 0

        found, unmatched = imported_mod.scan_folder(root)
        if not found:
            self.import_unmatched = list(unmatched)
            self.add_log("ERROR", "No baked textures found in {} ({} file(s) were "
                                  "not recognised)".format(root, len(unmatched)))
            return 0, 0, len(unmatched)

        settings.textures.clear()
        self.imported_by_group = {}
        grouped = imported_mod.group_found(found)
        for entry in grouped:
            images = {}
            for type_key, item in entry["images"].items():
                image = self._load_texture(type_key, item)
                if image is None:
                    continue
                images[type_key] = image
                row = settings.textures.add()
                row.image_name = image.name
                row.group = entry["group"]
                row.type_key = type_key
                row.type_label = item.type_label
                row.size = item.size
                row.status = "imported"
                row.exported_path = item.path
                row.imported = True
            if images:
                self.imported_by_group[entry["group"]] = images
        settings.texture_index = 0
        self.import_source = root
        self.import_unmatched = list(unmatched)

        total = sum(len(v) for v in self.imported_by_group.values())
        self.add_log("IMPORT", "Scanned {}: {} texture(s) in {} collection(s)".format(
            root, total, len(self.imported_by_group)))
        for path, reason in self.import_unmatched[:5]:
            self.add_log("SKIP", "{}: {}".format(path, reason))
        state, detail = self.uv_state()
        self.add_log("IMPORT", "UV check: {} — {}".format(state, detail))
        # ⚠ 扫完就把材质建好、槽挂上 —— 也就是"直接进入烘完的界面"。
        #   用户的原话："扫完的界面就是烘焙完的界面，有列出来的 32 个材质，
        #   可以切换预览，也可以导出最终物体"。
        #   以前扫完只填了贴图列表：材质没建、槽没有 → Preview 按下去报
        #   "nothing had a baked slot yet"，界面看起来像坏的（用户实测撞过）。
        #   跟随 Auto Deliver 开关：关掉它的人还是要手动那两步。
        if total and getattr(settings, "auto_deliver", True):
            built, _reports = self.build_materials(settings)
            if built:
                added = self.apply_materials(settings)
                self.add_log("IMPORT", "Auto delivery after scan: {} material(s), "
                                       "baked slot on {} object(s)".format(built, added))
            else:
                self.add_log("WARN", "Scanned textures but no material could be built "
                                     "from them — check the collection names")
        return total, len(self.imported_by_group), len(self.import_unmatched)

    def _load_texture(self, type_key, item):
        """把磁盘上的图变成 bpy Image，并把色彩空间设对"""
        from ..core import bake_types
        bake_type = bake_types.get(type_key)
        name = os.path.splitext(item.filename)[0]
        image = bpy.data.images.get(name)
        if image is None or (image.filepath and
                             os.path.normcase(bpy.path.abspath(image.filepath))
                             != os.path.normcase(item.path)):
            try:
                image = bpy.data.images.load(item.path, check_existing=True)
            except RuntimeError as exc:
                self.add_log("ERROR", "Could not load {}: {}".format(item.filename, exc))
                return None
        image.name = name if image.name != name else image.name
        # 数据贴图必须是 Non-Color，否则法线/粗糙会被 sRGB 曲线掰弯
        if bake_type is not None and bake_type.channel.is_data:
            try:
                image.colorspace_settings.name = 'Non-Color'
            except (TypeError, AttributeError):
                pass
        return image

    def group_objects(self, group_name):
        """按集合名找回场景里的物体（救援路径要用）"""
        from ..core import scene_scan
        collection = bpy.data.collections.get(group_name)
        if collection is None:
            return []
        return [obj for obj in collection.all_objects if scene_scan.is_bakeable(obj)]

    def uv_state(self):
        """贴图和网格的 UV 对不对得上 —— 只对**旧版烘出来的**贴图有意义

        ⚠ 2026-09 删掉了 UV 打包，所以新烘的贴图**永远是对的**（用物体当前的 UV、
          一个字节都没动）。这个方法留着只为一种情况：用户拿**旧版本**烘出来的
          贴图来救援 —— 那批图是按"打包布局"烘的，只有当网格上还留着那一层时
          才能对齐。判据仍然是"网格里有没有 MBAKERY_UV"。
        """
        from ..engine import uv_pack

        groups = list(self.imported_by_group)
        if not groups:
            return "unknown", "no imported textures yet"
        checked = 0
        matched = 0
        for name in groups:
            objects = self.group_objects(name)
            if not objects:
                continue
            with_layer = [o for o in objects
                          if o.data is not None
                          and o.data.uv_layers.get(uv_pack.PACK_LAYER_NAME)]
            if len(objects) == 1 and not with_layer:
                # 单个物体不需要打包，原 UV 就是对的
                checked += 1
                matched += 1
                continue
            checked += 1
            if with_layer:
                matched += 1
        if not checked:
            return "unknown", "no matching mesh objects found in the scene"
        if matched == checked:
            return "matched", ("every collection still has the packed layer '{}' "
                               "(these textures came from an older bake)".format(
                                   uv_pack.PACK_LAYER_NAME))
        if matched:
            return "partial", "{}/{} collection(s) still have '{}' (older bake)".format(
                matched, checked, uv_pack.PACK_LAYER_NAME)
        return "missing", ("these textures were baked with a packed UV layer ('{}') that "
                           "is no longer on the meshes — the textures will NOT line up. "
                           "Bake again to get textures that use your own UVs".format(
                               uv_pack.PACK_LAYER_NAME))

    def import_plan(self):
        """没有 job 时的"假计划"：让交付那套代码（按组找物体/材质）能跑

        组的来源是**两条路合起来**：
          * `imported_by_group` —— 从磁盘扫回来的贴图；
          * `materials_by_group` —— 在这个文件里认出来的已烘材质。
        ⚠ 少了第二条时，"重开文件 → 认出材质 → 点 Add Slot"会找到 0 个物体
          （测试里当场暴露：认出 2 个材质，但 Add Slot 是 0）。
        """
        from ..core import plan as plan_mod
        from ..core import scene_scan

        names = list(self.imported_by_group)
        for name in (self.materials_by_group or {}):
            if name not in names:
                names.append(name)

        groups = []
        for name in names:
            objects = self.group_objects(name)
            if objects:
                groups.append(scene_scan.ObjectGroup(name=name, objects=list(objects)))
        plan = plan_mod.BakePlan(groups=groups, assume_all_succeeded=True)
        return plan

    def refresh_textures(self, settings):
        """把贴图列表刷成当前运行的结果（保留用户已有的勾选）"""
        previously = {item.image_name: item.enabled for item in settings.textures}
        settings.textures.clear()
        if self.job is None:
            return 0
        count = 0
        for task in self.job.plan.tasks:
            item = settings.textures.add()
            item.image_name = task.image_name
            item.group = task.group_name
            item.type_key = task.bake_type.key
            item.type_label = task.bake_type.label
            item.size = task.size
            item.enabled = previously.get(task.image_name, task.status == "done")
            item.exported_path = task.exported_path
            item.status = task.status
            count += 1
        settings.texture_index = 0
        return count

    def enabled_texture_names(self, settings):
        return {item.image_name for item in settings.textures if item.enabled}

    # ------------------------------------------------------------------ 启动

    def build_plan(self, context, settings, gate_lookup=None):
        """把 UI 设置编译成计划。顺带把冲突/跳过写进日志。"""
        from ..engine import providers
        bake_settings = settings_to_bake_settings(settings)
        provider = providers.active_provider(settings)
        if getattr(provider, "name", "") not in ("default", ""):
            self.add_log("INFO", "provider: {}".format(provider.name))
        plan = plan_mod.build_plan(context, bake_settings, gate_lookup=gate_lookup,
                                   provider=provider)
        for group, reason in plan.skipped:
            self.add_log("SKIP", "{}: {}".format(group, reason))
        for message in plan.warnings:
            self.add_log("NOTE", message)
        for obj_name, group_names in plan.conflicts:
            self.add_log("CONFLICT", "{} is in {}".format(obj_name, " + ".join(group_names)))
        projection = [t for t in plan.tasks if t.projects]
        if projection:
            self.add_log("INFO", "{} task(s) will project from high-poly sources".format(
                len(projection)))
        return bake_settings, plan

    def start(self, context, settings, backend=None):
        """开始一次烘焙。返回是否真的启动了。"""
        if self.is_running:
            self.add_log("ERROR", "A bake is already running")
            return False

        self.reset()
        self.settings = settings
        # 新一轮运行：日志文件从头写，并写一行表头（崩溃时至少知道跑的是哪一次）
        self._open_log_file(truncate=True)
        # 阶段计时从这一刻开始，而且落点换成我们自己的日志（见 _phase_sink）
        phases.set_sink(_phase_sink)
        self.clock = phases.start_run("wizard")
        # 上一次交付把材质换成了烘焙材质 —— 直接重烘会**全部失败**：
        # 交付材质（尤其是 TexImage Only 那种）的 Material Output 上没有着色器，
        # 节点手术会报 "material has no shader connected to Material Output"，
        # 6 个任务全废，而用户只是又点了一次烘焙。
        # 所以重烘之前先把原槽还回去（备份就在物体的自定义属性里）。
        self.restore_applied_materials()
        bake_settings, plan = self.build_plan(
            context, settings, gate_lookup=lambda name: _gate(settings, name))
        self.plan = plan

        if plan.is_empty:
            self.error = "Nothing to bake — check targets and the map list"
            self.add_log("ERROR", self.error)
            return False

        if backend is None:
            if self.backend_factory is not None:
                backend = self.backend_factory(context, settings)
            else:
                # ⚠ 只能有**一个**事务，而且必须同时交给 backend 和 job。
                #   以前这里给 backend 新建一个、BakeJob 内部又自己建一个：
                #   于是 backend 手里那个事务从没 capture 过，投影烘焙一走到
                #   "临时取消隐藏源物体" 就崩（AttributeError: no attribute '_hidden'）。
                #   普通烘焙看不出来 —— 只有 selected-to-active 会碰那条路径。
                transaction = transaction_mod.SceneTransaction(context)
                backend = backend_mod.CyclesBackend(
                    transaction,
                    margin=settings.margin,
                    samples=resolve_samples(context, settings),
                    sampled_samples=resolve_samples(context, settings, sampled=True),
                    save_directory=output_directory(settings, None),
                    save_files=bool(settings.output_dir))
                self.transaction = transaction

        self.job = job_mod.BakeJob(context, plan, bake_settings, backend=backend,
                                   label="wizard",
                                   transaction=getattr(self, "transaction", None),
                                   on_before_dispatch=self._before_dispatch)
        self.report = self.job.report          # 让报告只有一份，别拷来拷去
        # 偏好设置护栏：跑之前把全局撤销和自动保存停掉。放在 start() 之前 ——
        # job.start() 会 capture 事务，那之后任何设置改动都算这一轮的临时状态。
        self.pause_preferences()
        self.job.start()
        self.started_at = time.time()
        self.run_count += 1
        self.add_log("INFO", "Baking {} map(s) for {} group(s)".format(
            len(plan.tasks), len(plan.groups)))
        # ⚠ 这里**不再注册 timer**：GUI 下由 mbakery.start_bake 那个模态 operator
        #   驱动（ESC 可取消、事件有保障、和 Blender 其它长任务一致）。
        #   背景/测试环境由 run_blocking() 驱动。
        #   两个驱动同时跑会把同一个 job 推两次，所以只留一个。
        return True

    # ------------------------------------------------------------------ 驱动

    def step_once(self):
        """推进一步。背景模式与测试直接调这个。"""
        if self.job is None:
            return None
        self.job.step()
        # 引擎每一步都会产出"开始了哪张 / 在等什么 / 谁失败了"，
        # 直接搬进日志 —— 卡死的时候手上得有现场。
        for level, message in self.job.drain_events():
            self.add_log(level, message)
        if self.job.is_finished:
            self.stop_timer()
            self.finished_at = time.time()
            if self.report.fatal:
                self.error = self.report.fatal
            for result in self.report.failed_results():
                self.add_log("FAIL", "{} {}: {}".format(
                    result.group, result.type_label, result.error))
            self.add_log("INFO", self.report.summary())
            self.on_finished()
            # 收尾的顺序是有讲究的：报告 → 自动交付 → 关掉这一轮的计时 →
            # **最后**才把偏好设置还回去（交付那几步也算在墙上时间里）。
            clock = phases.end_run(self.report.summary())
            if clock is not None:
                self.add_log("INFO", "Timing: {} (whole run {:.1f}s)".format(
                    clock.summary(), clock.since_start()))
            self.release_guard()
        return self.job.state

    def _before_dispatch(self, task):
        """派发**之前**：把状态推上去并强制重绘一次

        引擎调这个回调之后立刻就会阻塞在 bpy.ops.object.bake 里（GUI 下实测
        同步返回，等于烘完才回来），主线程在那段时间一帧都画不出来。
        所以这里是画面能显示"正在烘哪一张、上一张多久"的唯一机会。

        ⚠ bpy.ops.wm.redraw_timer 在**背景模式**下 poll 不通过（实测报
          "context is incorrect"），所以先判断有没有界面，而且整段包在 try 里：
          重绘失败绝不允许影响烘焙。
        """
        self.tag_redraw()
        if bpy.app.background:
            return
        try:
            bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
        except Exception as exc:
            compat.log("强制重绘失败（不影响烘焙）: {}".format(exc))

    def run_blocking(self, max_steps=100000):
        """一口气跑完（背景 / 测试用，没有事件循环）"""
        guard = 0
        while self.is_running and guard < max_steps:
            self.step_once()
            guard += 1
        return self.report

    # ------------------------------------------------------------------ 交付

    def restore_applied_materials(self):
        """把上次交付换上去的烘焙材质还原成原材质槽

        为什么要在这里做：交付用的材质（尤其 TexImage Only）没有着色器接到
        Material Output 上，节点手术在它身上做不了 —— 用户第二次点烘焙会看到
        **每一个任务都失败**，错误是 "material has no shader connected to
        Material Output"，而他只是又点了一次烘焙而已。
        备份就在物体的自定义属性里（core/materials.py），还原是无损的。
        """
        from ..deliver import slots as slots_mod

        try:
            applied = slots_mod.applied_objects()
        except Exception as exc:
            self.add_log("WARN", "Could not check for applied materials: {}".format(exc))
            return 0
        if not applied:
            return 0
        outcomes = slots_mod.restore_objects(applied)
        ok = [o for o in outcomes if o.ok]
        for outcome in outcomes:
            if not outcome.ok:
                self.add_log("WARN", "Could not restore {}: {}".format(
                    outcome.object_name, outcome.reason))
        if ok:
            self.add_log("NOTE", "Restored the original material slots on {} object(s) "
                                 "before re-baking".format(len(ok)))
        return len(ok)

    def deliver(self, settings, auto=False):
        """把烘焙结果变成用户能用的东西：按集合建材质 → 换到物体上。

        用户的原话是"烘完自动建材质并换到物体上，但保留一键回退"，
        所以默认在跑完之后自动做一遍；做不成的事必须**说出来**，
        而不是留一个看不懂的报错（他点手动按钮时看到的是
        "No successful bake to build from"，一头雾水）。

        返回 (材质数, 应用到的物体数)。
        """
        # 交付也要计时：它和收尾同处"最后一帧之后、界面回来之前"那段黑箱里
        phases.begin("delivery: build materials")
        built, _reports = self.build_materials(settings)
        phases.end("delivery: build materials")
        if not built:
            return 0, 0
        phases.begin("delivery: add baked slots")
        added = self.apply_materials(settings)
        phases.end("delivery: add baked slots")
        return built, added

    def build_materials(self, settings):
        """按集合建最终材质（不碰物体）。返回 (材质数, link_reports)

        ⚠ 不再传 uv_layer：材质里**不加 UV Map 节点**，让它自动跟随物体当前的
          渲染 UV 层（打包层已经是 active_render）。这是修掉"表面渲染错误"的关键 ——
          详见 deliver/materials.py:build_final_material 的注释。
        """
        from ..core.materials import MaterialStore
        from ..deliver import materials as materials_mod

        images = self.images_by_group()
        if not images:
            self.add_log("NOTE", "Nothing to build from: no bake item succeeded and "
                                 "nothing was scanned from disk")
            return 0, {}

        self.store = self.store or MaterialStore()
        # 只有一种交付材质：Principled BSDF。
        # （`TexImage Only` 那档已废弃 —— 出来的材质没有着色器接输出，用户还得自己接）
        style = materials_mod.STYLE_PRINCIPLED
        self.materials_by_group = materials_mod.build_for_groups(
            self.store, images, style=style)
        self.link_reports = {name: entry[1]
                             for name, entry in self.materials_by_group.items()}
        for name, entry in self.materials_by_group.items():
            self.add_log("MATERIAL", "{}: {}".format(entry[0].name, entry[1].summary()))
        return len(self.materials_by_group), self.link_reports

    def apply_materials(self, settings):
        """给物体**追加**烘焙材质槽（不改外观）。返回成功的物体数

        用户定稿的流程：烘完物体上就是两个材质，想看成色就点"预览"（分配面）。

        ⚠ 材质还没建就**顺手建掉**：用户按了"Add Slot"却只得到一句
          "Build the materials first"（或者干脆 CANCELLED），在界面上看起来就是
          "这个按钮没用/报错"。按钮的意图是明确的，没必要让他先点另一个按钮。
        """
        from ..deliver import slots as slots_mod

        if not self.materials_by_group:
            built, _reports = self.build_materials(settings)
            if not built:
                self.add_log("ERROR", "Nothing to put in a slot — bake something (or scan "
                                      "the output folder) first")
                return 0
            self.add_log("NOTE", "Built the materials first, then added the slots")
        plan = self.job.plan if self.job is not None else self.import_plan()
        exclude = self.pack_skipped_names()
        outcomes = slots_mod.add_baked_slots(plan, self.materials_by_group,
                                             exclude=exclude)
        added = [o for o in outcomes if o.ok]
        for outcome in outcomes:
            self.add_log("SLOT", "{} {}".format(
                outcome.object_name,
                "baked slot added" if outcome.ok else "skipped: " + outcome.reason))
        skipped = len(outcomes) - len(added)
        self.add_log("INFO", "{} material(s), baked slot added to {} object(s){}".format(
            len(self.materials_by_group), len(added),
            "" if not skipped else ", {} skipped".format(skipped)))
        return len(added)

    def pack_skipped_names(self):
        """**已废弃**：以前用来排除"被 UV 打包跳过"的物体

        ⚠ 2026-09 删掉了 UV 打包，所以没有"Skipped by packing"这种物体了 ——
          每个物体都用自己现在的 UV，人人有份。保留这个函数只为一个原因：
          老调用点（apply/preview/finalize）都传了 `exclude=`，
          返回空集合即可，不必去改那几处签名。
        """
        return set()

    def preview_materials(self, settings, baked=True):
        """预览：把面分配到烘焙槽（baked=True）或切回原槽（baked=False）

        ⚠ 物体上还没有烘焙槽时**自动补上再切**。用户实测撞过这个：
          扫完目录直接点 Preview，得到一句红字
          "No object has a baked slot yet — press Add Slot first"，
          而他只是想让预览生效 —— 按钮的意图是明确的，没必要让他先点另一个按钮。
        """
        from ..deliver import slots as slots_mod

        if baked and not self.objects_with_baked_slot():
            added = self.apply_materials(settings)
            if added:
                self.add_log("NOTE", "Added the baked slots first, then previewed")
        plan = self.job.plan if self.job is not None else self.import_plan()
        outcomes = slots_mod.assign_objects(plan, baked=baked,
                                            exclude=self.pack_skipped_names())
        changed = [o for o in outcomes if o.ok]
        for outcome in outcomes:
            if not outcome.ok:
                continue
        self.add_log("PREVIEW", "{} object(s) now show the {} material{}".format(
            len(changed), "baked" if baked else "original",
            "" if changed else " (nothing has a baked slot)"))
        settings.compare_baked = bool(baked and changed)
        return len(changed)

    def finalize_materials(self, settings):
        """收尾：只留烘焙槽 + 贴图 pack 进 .blend + 设 fake user

        用户的诉求是"方便我直接导出"：导出时槽里只剩一个材质，贴图也不会丢。
        收尾**可逆** —— 原槽名与逐面索引存在物体上，`Restore Original Slots` 能重建。

        ⚠ 收尾和"移除"是**相反**的两件事，别让用户猜：
            收尾 = 删掉**旧**槽，只留烘焙材质（导出干净）
            移除 = 删掉**烘焙**槽，把原材质槽还回来（撤销）
          而且物体上还没有烘焙槽时，收尾会**先补上再加**（意图明确，不必分两步）。
        """
        from ..deliver import slots as slots_mod

        if not self.objects_with_baked_slot():
            added = self.apply_materials(settings)
            if not added:
                self.add_log("ERROR", "Nothing to finalize — nothing has a baked slot "
                                      "and nothing could be built")
                return 0
            self.add_log("NOTE", "Added the baked slots first, then finalized")
        plan = self.job.plan if self.job is not None else self.import_plan()
        outcomes = slots_mod.finalize_objects(plan, exclude=self.pack_skipped_names())
        done = [o for o in outcomes if o.ok]

        packed = 0
        pack_wanted = bool(getattr(settings, "pack_into_blend", False))
        linked = 0
        for entry in (self.materials_by_group or {}).values():
            material = entry[0] if isinstance(entry, tuple) else entry
            if material is None or material.node_tree is None:
                continue
            for node in material.node_tree.nodes:
                image = getattr(node, "image", None)
                if image is None:
                    continue
                image.use_fake_user = True
                if image.packed_file is not None:
                    continue
                # ⚠ 默认**不**把像素塞进 .blend（用户 2026-09 的要求：
                #   "默认把烘焙完的材质全都连到本地的文件，打包到文件这些让我自己搞"）。
                #   只有用户自己勾了 Pack Textures 才 pack。
                if pack_wanted:
                    try:
                        image.file_format = settings.file_format
                        image.pack()
                        packed += 1
                    except (RuntimeError, AttributeError):
                        pass
                elif image.filepath:
                    linked += 1
        self.add_log("FINALIZE", "{} object(s) now carry only the baked material; "
                                 "{} image(s) packed, {} linked to files on disk".format(
                                     len(done), packed, linked))
        if linked and not pack_wanted:
            self.add_log("NOTE", "Textures stay linked to the files in the output folder "
                                 "(turn on 'Pack Textures Into .blend' if you want them "
                                 "inside the .blend)")
        self.add_log("NOTE", "Finalize is reversible — Restore Original Slots rebuilds "
                             "the original slots from the backup stored on each object")
        return len(done)

    def on_finished(self):
        """一轮跑完之后的收尾（含自动交付）

        ⚠ 自动交付**只在有成功项时**做，而且失败/跳过一律写进日志：
          "烘完什么都没变"是最难查的一种体验 —— 用户不知道是没做还是做失败了。
        """
        settings = self.settings
        if settings is None or self.job is None:
            return
        if not getattr(settings, "auto_deliver", True):
            if self.succeeded_tasks():
                self.add_log("NOTE", "Auto delivery is off — use Build Materials / "
                                     "Apply Baked Material on the last page")
            return
        if self.report is None or self.report.cancelled or self.report.fatal:
            return
        if not self.succeeded_tasks():
            self.add_log("NOTE", "Nothing to deliver: no bake item succeeded "
                                 "({} failed)".format(self.report.failed))
            return
        try:
            built, applied = self.deliver(settings, auto=True)
        except Exception as exc:                      # 交付失败不能把报告吞掉
            self.add_log("ERROR", "Auto delivery failed: {}: {}".format(
                type(exc).__name__, exc))
            return
        if built:
            self.add_log("INFO", "Auto delivery: {} material(s) applied to {} object(s) "
                                 "— use Restore Original Slots to undo".format(
                                     built, applied))

    def cancel(self):
        if self.job is None:
            return False
        self.job.cancel()
        self.add_log("WARN", "Cancel requested")
        return True

    def pause(self):
        return self.cancel()

    def start_timer(self):
        if self._timer_running:
            return
        try:
            bpy.app.timers.register(self.tick, first_interval=TICK_INTERVAL)
            self._timer_running = True
        except (ValueError, AttributeError) as exc:
            compat.log("无法注册 timer（背景模式？）: {}".format(exc))

    def stop_timer(self):
        if not self._timer_running:
            return
        try:
            if bpy.app.timers.is_registered(self.tick):
                bpy.app.timers.unregister(self.tick)
        except (ValueError, AttributeError):
            pass
        self._timer_running = False

    def tick(self):
        """timer 回调。返回 None 表示结束；否则是下次间隔。"""
        if self.job is None:
            self._timer_running = False
            return None
        self.step_once()
        self.tag_redraw()
        if self.job.is_finished:
            self._timer_running = False
            return None
        return TICK_INTERVAL

    @staticmethod
    def tag_redraw():
        """只有画界面这件事碰 bpy.context，而且只是打重绘标记"""
        try:
            window_manager = bpy.context.window_manager
        except AttributeError:
            return
        if window_manager is None:
            return
        for window in window_manager.windows:
            screen = window.screen
            if screen is None:
                continue
            for area in screen.areas:
                if area.type == 'VIEW_3D' or area.type == 'PROPERTIES':
                    area.tag_redraw()


# ------------------------------------------------------------------------------------
#   设置转换（纯函数，方便测试）

def resolve_samples(context, settings, sampled=False):
    """烘焙要用的采样数。

    分两组（用户要求）：
      sampled=False —— 确定值通道（颜色/粗糙/金属/法线/IOR…）。默认 **1 个采样**：
        这些通道每个采样算出来都是同一个数，多烘只是白烧时间。用户那轮
        128 张 2048² 跑了 1301 秒，绝大部分就是这么烧掉的。
      sampled=True  —— 真正需要采样的通道（AO / 阴影 / Combined / Diffuse /
        Glossy / Transmission）。采样少了就是噪点，所以默认给 16。

    每组都是"低/中/高 + 自定义输入框"，'RENDER' 则沿用场景设置。
    改回去这件事**不用额外代码**：渲染设置本来就被 SceneTransaction
    快照过，commit/rollback 都会还原（见 core/transaction.py 的 _capture_cycles）。
    """
    prefix = "sampled_maps" if sampled else "samples"
    mode = getattr(settings, prefix + "_mode", 'LOW')
    custom = int(getattr(settings, prefix + "_custom", 1) or 1)

    if mode == 'CUSTOM':
        return max(1, custom)
    # 兼容旧值：'ONE' 是上一版"1 (fastest)"的键，读旧预设时会遇到。
    if mode == 'ONE':
        return 1
    table = ((16, 64, 256) if sampled else (1, 16, 64))
    if mode == 'LOW':
        return table[0]
    if mode == 'MEDIUM':
        return table[1]
    if mode == 'HIGH':
        return table[2]
    cycles = getattr(context.scene, "cycles", None)
    if cycles is not None and hasattr(cycles, "samples"):
        return cycles.samples
    return table[0]


def output_directory(settings, group_name):
    """某个分组的输出目录；没配路径就返回空串（表示不导出文件）"""
    base = getattr(settings, "output_dir", "") or ""
    if not base:
        return ""
    if not group_name or not getattr(settings, "use_subfolders", True):
        return base
    import os
    from ..core import naming
    return os.path.join(base, naming.sanitize_filename(group_name))


def settings_to_bake_settings(settings):
    """UI PropertyGroup -> core.plan.BakeSettings（纯数据）"""
    pairs = settings.map_requests()
    mode = settings.target_mode
    single = settings.selected_collection() if mode == scene_scan.MODE_SINGLE else ""
    return plan_mod.BakeSettings(
        target_mode=mode,
        single_collection=single,
        maps=plan_mod.map_requests_from_pairs(pairs),
        prefix=settings.prefix,
        bridge=settings.bridge or " ",
        suffix=settings.suffix or "",
        template=settings.template or naming.DEFAULT_TEMPLATE,
        shared_textures=True,
        file_format=settings.file_format,
        use_subfolders=getattr(settings, "use_subfolders", True),
        prepare_uv=settings.prepare_uv,
        unwrap_target=getattr(settings, "unwrap_target", "UVMap"),
        pack_into_blend=getattr(settings, "pack_into_blend", True),
        fake_user=getattr(settings, "fake_user", True),
        antialias=getattr(settings, "antialias", 'OFF'),
        aa_scale=getattr(settings, "aa_scale", 2),
        adaptive_margin=getattr(settings, "adaptive_margin", False),
        margin=getattr(settings, "margin", 16),
        bake_method=getattr(settings, "bake_method", 'EMISSION'),
        udim=getattr(settings, "udim", False),
        selected_to_active=getattr(settings, "use_selected_to_active", False),
        source_mode=getattr(settings, "source_mode", 'OTHERS'),
        source_pattern=getattr(settings, "source_pattern", ""),
        max_ray_distance=getattr(settings, "max_ray_distance", 0.0),
        cage_extrusion=getattr(settings, "cage_extrusion", 0.0),
        unhide_sources=getattr(settings, "unhide_sources", True),
        hide_unrelated=getattr(settings, "hide_unrelated", False),
    )


def _gate(settings, group_name):
    for item in settings.groups:
        if item.name == group_name:
            return bool(item.enabled)
    return True


def _existing_names():
    return {obj.name for obj in bpy.data.objects}


# ------------------------------------------------------------------------------------
#   集合列表扫描（第 1 页显示用）

def refresh_groups(context, settings, measure_overlap=False):
    """重新扫描场景，刷新集合列表。保留用户已有的勾选状态。"""
    from ..engine import uv_overlap
    from . import properties

    previously = {item.name: item.enabled for item in settings.groups}
    groups, conflicts = scene_scan.scan(context.scene)
    conflicted = {name for name, _ in conflicts}

    settings.groups.clear()
    for group in groups:
        item = settings.groups.add()
        item.name = group.name
        item.enabled = previously.get(group.name, True)
        item.object_count = len(group.objects)
        item.missing_uv = len(scene_scan.missing_uv_objects(group.objects))
        item.material_count = len({slot.material.name for obj in group.objects
                                  for slot in obj.material_slots if slot.material})
        item.has_conflict = group.name in conflicted or any(
            obj.name in conflicted for obj in group.objects)
        if measure_overlap and group.objects:
            stats = uv_overlap.scan_together(group.objects, label=group.name)
            item.overlap_ratio = stats.overlap_ratio if stats else 0.0
        else:
            item.overlap_ratio = 0.0
    settings.scanned = True
    return conflicts

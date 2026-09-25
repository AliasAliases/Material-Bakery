# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   事务：对用户数据的每一次改动都必须在一个事务里
#
#   烘焙要临时改的东西很多（渲染引擎、烘焙设置、选中状态、材质里的图像节点），
#   中途一旦抛异常或者被取消，场景必须原样回去。这个类是唯一的入口。
#
#   用法:
#       with SceneTransaction(context) as tx:
#           tx.set_render_engine('CYCLES')
#           ...
#       正常退出 -> commit()；抛异常 -> 自动 rollback()
# ------------------------------------------------------------------------------------

import bpy

from .. import compat
from . import phases


def _target_label(target):
    """给「这次赋值写在哪个对象上」取一个能读的名字（只用于日志）

    ⚠ 用 rna_type，不是 bl_rna：`scene.render` 是 **RenderSettings**，
      RenderSettings 上没有 `bl_rna`（那是 ID 数据块才有的），
      写成 bl_rna 这条日志就只剩一个兜底的类名。
    """
    try:
        name = getattr(target, "name", None)
        if isinstance(name, str) and name:
            return name
    except (AttributeError, ReferenceError):
        pass
    try:
        return str(target.rna_type.identifier)
    except (AttributeError, ReferenceError):
        return type(target).__name__


def _current_value(target, name):
    """读当前值；读不到就抛（交给调用方兜）。"""
    return getattr(target, name)


def _same_value(current, wanted):
    """值一样吗？一样就**不要写**。

    ⚠ 这是修用户那次卡死的关键。原来的还原循环无条件 `setattr`，而
      `render.engine` 的 setter 在 GUI 里是**有代价**的：它会触发一次引擎切换的
      通知/重建（EEVEE/Cycles 的视口重编译、draw manager 重建），而"赋同一个值"
      照样跑完整套。用户那次（2026-09-25 07:37）就卡死在这一行上：
          restore: engine -> RenderSettings.engine      <- 最后一行，之后没有下文
      而他的场景引擎本来就是 CYCLES（快照值就是 'CYCLES'），也就是说这里在
      把同一个值赋给自己，白白付了一次引擎重建的钱 —— 而且是在模态 operator
      还没退出的窗口里，主线程一卡就是无限期。
      同样的写法在 --background 里 0.0000 秒过，所以它只在 GUI 里咬人。

    ⚠ 只在"读过且相等"时跳过：读不到就老老实实写，不能因为读不了就把值丢了。
    """
    try:
        return current == wanted
    except Exception:
        return False


# ------------------------------------------------------------------------------------
#   推迟写入：不在模态 operator 里改引擎
#
#   ⚠ 为什么需要：上面那条"值不一样就必须写"的路径，如果值**真的**要变
#     （引擎从 CYCLES 换回 EEVEE），在 operator 里同步写就可能再卡一次。
#     而 operator 一旦返回，Blender 的事件循环就活了 —— 引擎重建再慢也能重绘、
#     能响应 ESC。所以：值不一样就排进队列，由 bpy.app.timers 在 operator
#     退出之后一条条补上。
#
#   ⚠ 队列是**先登记后执行**的：`_restore_scene` 自己不会阻塞，`commit()` 立刻返回。
#     宁可让场景晚 1 帧还原（这几项在那一帧里不影响任何画面：引擎已经确认过值了），
#     也不要让界面卡住。
# ------------------------------------------------------------------------------------

_PENDING = []


def _run_deferred_now():
    """就地、同步地把队列排空。返回应用了几条。

    ⚠ 必须有一条同步路径：**background 模式没有事件循环，timer 永远不触发**。
      只靠 timer 的话，引擎就再也不会被还原了 —— 测试里立刻就能看见
      （"渲染引擎已还原"直接变红），而 GUI 里反而不会报错，是最难查的那种不一致。
    """
    applied = 0
    while _PENDING:
        target, name, value, label = _PENDING.pop(0)
        try:
            setattr(target, name, value)
            applied += 1
            phases.mark("restore (deferred): {} -> {}.{} = {!r}".format(
                label, _target_label(target), name, value))
        except Exception as exc:
            compat.log("推迟的还原失败 {}.{}: {}".format(label, name, exc))
    return applied


def _drain_deferred():
    """timer 回调：把推迟的还原一条条补上。每帧只做一条，界面始终能响应。"""
    if not _PENDING:
        return None
    _record = _PENDING.pop(0)
    target, name, value, label = _record
    try:
        setattr(target, name, value)
        # 成功才写日志：失败时那句 ERROR 就是最后一行，比"已还原"更诚实
        phases.mark("restore (deferred): {} -> {}.{} = {!r}".format(
            label, _target_label(target), name, value))
    except Exception as exc:
        compat.log("推迟的还原失败 {}.{}: {}".format(label, name, exc))
    return 0.0 if _PENDING else None


def _defer(target, name, value, label):
    """排进队列。返回 False 表示排不进去（调用方应该就地写）。

    ⚠ 排不进去时要把刚追加的那条**拿回来**：留着它会让队列里堆一条永远没人执行的
      记录，下一次真排得进去时又会被一并执行 —— 一条"幽灵写入"，事后极难查。
    """
    _PENDING.append((target, name, value, label))
    if len(_PENDING) > 1:
        return True                       # 已经有一个 timer 在跑了
    timers = getattr(bpy.app, "timers", None)
    if timers is None:
        _PENDING.pop()
        return False
    try:
        if timers.is_registered(_drain_deferred):
            return True
        timers.register(_drain_deferred, first_interval=0.0)
        return True
    except (AttributeError, ValueError, RuntimeError) as exc:
        _PENDING.pop()
        compat.log("无法注册推迟还原的 timer（改为就地写入）: {}".format(exc))
        return False


class SceneTransaction:
    """记录场景的临时状态，失败时自动还原"""

    def __init__(self, context, label="bake"):
        self.context = context
        self.label = label
        self._snapshot = {}
        self._materials = {}      # material -> [(node_tree, node_name), ...]
        self._active_object = None
        self._selected = ()
        self._committed = False
        self._entered = False
        self._captured = False
        # ⚠ 这几个也要在 __init__ 里就位，不能只放在 capture() 里。
        #   否则一个"没走过 capture 的事务"会在用它们的时候抛
        #   AttributeError: 'SceneTransaction' object has no attribute '_hidden'
        #   —— 报错指向事务内部，跟真正的原因（两个不同的事务）毫无关系，
        #   排查起来极其费劲。实测就是这么踩到的。
        self._uv_objects = []      # 被换过渲染 UV 层的物体
        self._hidden = []          # 被临时取消隐藏的源物体名
        self._run_hidden = []      # 被"Hide Unrelated"临时藏起来的物体名

    def _ensure_captured(self):
        """任何改动场景之前，保证已经快照过。

        允许调用方忘记 capture()（比如把一个事务同时交给 job 和 backend 时
        只在一处 capture），代价不能是崩溃。
        """
        if not self._captured:
            self.capture()

    # -- 生命周期 ---------------------------------------------------------------

    def __enter__(self):
        self.capture()
        self._entered = True
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is not None:
            self.rollback()
            return False              # 异常继续往上抛
        if not self._committed:
            self.rollback()           # 没显式 commit 就当失败处理，绝不静默吞掉
        return False

    def capture(self):
        self._captured = True
        # ⚠ 开新事务之前先把上一轮**遗留**的推迟还原做掉。
        #   否则上一轮的烘焙值会被当成"用户原本的设置"快照进去 —— 那等于把临时值
        #   永久焊进场景，而且下一轮跑完会"还原"成上一轮的烘焙值，越滚越错。
        #   正常情况下队列到这时早空了（commit/rollback 都会排空），这里只是兜底。
        if _PENDING:
            _run_deferred_now()
        scene = self.context.scene
        render = scene.render
        self._snapshot = {
            "engine": render.engine,
            "bake": {
                "use_pass_direct": render.bake.use_pass_direct,
                "use_pass_indirect": render.bake.use_pass_indirect,
                "use_pass_color": render.bake.use_pass_color,
                "margin": render.bake.margin,
                "margin_type": render.bake.margin_type,
                "use_clear": render.bake.use_clear,
                "use_selected_to_active": render.bake.use_selected_to_active,
                "target": render.bake.target,
                "normal_space": render.bake.normal_space,
                "save_mode": render.bake.save_mode,
            },
            "bake_image_settings": {},
            "cycles": self._capture_cycles(scene),
            "color_management": {},
        }
        bake_image = getattr(render.bake, "image_settings", None)
        if bake_image is not None:
            self._snapshot["bake_image_settings"] = {
                "file_format": bake_image.file_format,
                "color_mode": bake_image.color_mode,
                "color_depth": bake_image.color_depth,
            }
            self._snapshot["color_management"] = {
                "view_transform": scene.view_settings.view_transform,
                "look": scene.view_settings.look,
                "exposure": scene.view_settings.exposure,
                "gamma": scene.view_settings.gamma,
            }

        self._active_object = self.context.view_layer.objects.active
        self._selected = tuple(self.context.selected_objects)
        self._materials = {}
        self._uv_objects = []          # 被换过渲染 UV 层的物体，取消时要切回去
        self._hidden = []              # [(物体名), ...] 被临时取消隐藏的源物体
        self._run_hidden = []          # 被 Hide Unrelated 藏起来的物体

    def _capture_cycles(self, scene):
        cycles = getattr(scene, "cycles", None)
        if cycles is None:
            return {}
        keys = ("samples", "use_denoising", "bake_type", "device", "use_adaptive_sampling",
                "adaptive_threshold", "max_bounces", "use_light_tree")
        return {k: getattr(cycles, k) for k in keys if hasattr(cycles, k)}

    def commit(self):
        """确认这次运行成功 —— 新建的图像/材质保留，临时状态照旧还原

        唯一不回退的是「渲染 UV 层」：打包后的布局就是交付材质要用的，
        失败时才切回用户原来那层（见 rollback）。
        """
        self._committed = True
        self._restore_scene(restore_uv=False)
        self.finish_deferred()

    def finish_deferred(self):
        """把推迟到 operator 之后的还原补上。

        ⚠ background 模式没有事件循环，timer 不会触发 —— 那里必须同步做掉，
          否则引擎/采样这些设置就永久留在烘焙值上了（GUI 不报错，测试才会红）。
        """
        if not _PENDING:
            return 0
        if getattr(bpy.app, "background", False):
            return _run_deferred_now()
        timers = getattr(bpy.app, "timers", None)
        if timers is None:
            return _run_deferred_now()
        try:
            if not timers.is_registered(_drain_deferred):
                timers.register(_drain_deferred, first_interval=0.0)
        except (AttributeError, ValueError, RuntimeError):
            return _run_deferred_now()
        return 0

    def rollback(self):
        """把场景恢复成 capture() 时的样子"""
        self._committed = False
        self._restore_scene(restore_uv=True)
        self.finish_deferred()

    def track_uv_objects(self, objects):
        """登记被换过渲染 UV 层的物体"""
        self._ensure_captured()
        for obj in objects:
            if obj not in self._uv_objects:
                self._uv_objects.append(obj)

    def hide_objects(self, objects, keep=()):
        """为了这次运行临时藏起一批物体，收尾（commit / rollback）时全部放出来。

        用途见 engine/backend.py 的 `Hide Unrelated While Baking`：把不在本次
        烘焙范围内的物体从视图层拿掉，Cycles 就不用为它们建几何和贴图。
        返回真正被藏起来的物体名列表。

        ⚠ 只管**藏起来**这件事；"哪些该藏"由调用方决定（它才知道投影源是谁）。
        ⚠ keep 里**名字或物体都收**：调用方经常手里只有名字集合（比如后端算出来的
          "本次要烘谁"），逼它去数据库里逐个查物体是没必要的负担。
        ⚠ 用户自己已经隐藏（H）的物体不算我们藏的，也不去动它 —— 收尾时把它
          放出来会凭空改变用户的场景。
        """
        self._ensure_captured()
        protected = set()
        for entry in (keep or ()):
            if isinstance(entry, str):
                protected.add(entry)
            elif entry is not None:
                protected.add(entry.name)
        hidden = []
        for obj in objects:
            if obj is None or obj.name in protected:
                continue
            try:
                already = obj.hide_get()
            except (RuntimeError, ReferenceError):     # 不在这个视图层里
                continue
            if already or obj.name in self._run_hidden:
                continue
            try:
                obj.hide_set(True)
            except (RuntimeError, ReferenceError):
                continue
            self._run_hidden.append(obj.name)
            hidden.append(obj.name)
        return hidden

    def unhide_run_objects(self):
        """把 Hide Unrelated 藏起来的物体放回去。幂等，绝不抛异常。"""
        names = self._run_hidden
        self._run_hidden = []
        released = 0
        for name in names:
            obj = bpy.data.objects.get(name)
            if obj is None:
                continue
            try:
                obj.hide_set(False)
                released += 1
            except (RuntimeError, ReferenceError):
                pass
        return released

    def _restore_scene(self, restore_uv=False):
        # ⚠ 这一段被计时**逐段**记录（见 core/phases.py）。用户那次"8/8 之后
        #   729 秒没响应"就发生在这几行里，而日志对此一片空白 —— 那就只能靠猜。
        #   现在谁慢谁自己报数，下次不用猜。
        phases.begin("restore: bake targets")
        try:
            self.remove_bake_targets()
        except Exception as exc:                     # 材质可能已被删掉
            compat.log("清理图像节点失败: {}".format(exc))
        phases.end("restore: bake targets")

        if restore_uv and self._uv_objects:
            from ..engine import uv_pack
            phases.begin("restore: render UV layers")
            try:
                uv_pack.restore_render_uv_layers(
                    [o for o in self._uv_objects if o.name in bpy.data.objects])
            except Exception as exc:
                compat.log("还原渲染 UV 层失败: {}".format(exc))
            phases.end("restore: render UV layers")

        # 把 Hide Unrelated 藏起来的物体放出来（**必须在恢复选择之前**：
        # 隐藏中的物体也能被选中，但恢复完再放出来会留下一次多余的视图层更新）
        self.unhide_run_objects()

        # 把为投影临时取消隐藏的源物体重新隐藏回去
        for obj_name in self._hidden:
            obj = bpy.data.objects.get(obj_name) if isinstance(obj_name, str) else None
            if obj is not None:
                try:
                    obj.hide_set(True)
                except (RuntimeError, ReferenceError):
                    pass
        self._hidden = []

        phases.begin("restore: engine + bake settings")
        scene = self.context.scene
        render = scene.render
        # ⚠ 每一行单独写一条日志。为什么：用户 2026-09-25 的现场日志停在
        #   "restore: engine + bake settings: start" 上，后面连 "done" 都没有 ——
        #   也就是说这二十来行赋值里有一行把主线程卡死了，而日志对此一片空白，
        #   只能靠猜（上一轮猜"是自动保存"已经猜错过一次，代价是 729 秒）。
        #   现在谁慢谁自己报数，卡死现场最后一行就是凶手。
        #   ⚠ 这些写操作**故意不加 try**：抛异常是"能看见的失败"，
        #     把它吞掉只会让场景停在半还原状态上，比报错难查得多。
        if "engine" in self._snapshot:
            self._restore_write("engine", render, "engine", self._snapshot["engine"])
        for name, value in self._snapshot.get("bake", {}).items():
            if hasattr(render.bake, name):
                self._restore_write("bake", render.bake, name, value)
        bake_image = getattr(render.bake, "image_settings", None)
        if bake_image is not None:
            for name, value in self._snapshot.get("bake_image_settings", {}).items():
                self._restore_write("bake_image_settings", bake_image, name, value)
        for name, value in self._snapshot.get("color_management", {}).items():
            self._restore_write("color_management", scene.view_settings, name, value)
        cycles = getattr(scene, "cycles", None)
        if cycles is not None:
            for name, value in self._snapshot.get("cycles", {}).items():
                self._restore_write("cycles", cycles, name, value)
        phases.end("restore: engine + bake settings")

        phases.begin("restore: selection")
        self.restore_selection()
        phases.end("restore: selection")

    def _restore_write(self, group, target, name, value):
        """一次还原赋值，并且**写一条带路径的日志**。

        三条规矩，每一条都是被现场咬出来的（2026-09-25）：
          1. 值一样 -> **一个字都不写**。`render.engine` 的 setter 在 GUI 里
             会重建引擎，赋同一个值照样付这个钱，而用户就是卡死在这一行上。
          2. 值不一样 -> 排进推迟队列，由 timer 在 operator 退出之后补上。
             在模态 operator 里同步改引擎 = 界面无限期不响应。
          3. 日志**先写**再动手：卡死时最后一行就是凶手（这一条已经立功了）。
        """
        try:
            current = _current_value(target, name)
        except Exception:
            current = None                      # 读不到就当"要写"，不回退行为
        phases.mark("restore: {} -> {}.{} (was {!r}, want {!r})".format(
            group, _target_label(target), name, current, value))
        if current is not None and _same_value(current, value):
            phases.mark("restore: {} -> {}.{} skipped (already {!r})".format(
                group, _target_label(target), name, current))
            return
        if _defer(target, name, value, group):
            return
        setattr(target, name, value)            # 兜底：排不进队列就地写（旧行为）

    # -- 渲染 / 烘焙设置 --------------------------------------------------------

    def set_render_engine(self, engine):
        self._ensure_captured()
        try:
            self.context.scene.render.engine = engine
            return True
        except TypeError:
            compat.log("本版本没有渲染引擎 {!r}".format(engine))
            return False

    def set_bake_setting(self, name, value):
        self._ensure_captured()
        if hasattr(self.context.scene.render.bake, name):
            setattr(self.context.scene.render.bake, name, value)

    def bake_setting(self, name, default=None):
        return getattr(self.context.scene.render.bake, name, default)

    def set_cycles_setting(self, name, value):
        self._ensure_captured()
        cycles = getattr(self.context.scene, "cycles", None)
        if cycles is not None and hasattr(cycles, name):
            setattr(cycles, name, value)

    # -- 选择状态 ---------------------------------------------------------------

    def select_only(self, objects):
        """烘焙操作符是拿「选中物体」干活的，这里只改视图层选中，不碰集合归属"""
        return self.select_for_bake(objects, ())

    def select_for_bake(self, targets, sources=(), unhide_sources=True):
        """选中目标（+ 投影烘焙时的源）

        ⚠ selected-to-active 要求**源和目标同时被选中**，目标还得是 active。
          只选目标的话 Blender 会当成普通烘焙，高模一点也不会被采样，
          而且不会报错 —— 只是结果不对。
        ⚠ 视口里被隐藏（H）的源会被烘焙忽略，所以投影时要临时取消隐藏，
          原状态记下来，收尾时还原。
        """
        self._ensure_captured()
        view_layer = self.context.view_layer
        objects = [o for o in list(targets) + list(sources) if o.name in bpy.data.objects]

        if unhide_sources:
            for obj in sources:
                if obj.name in bpy.data.objects and obj.hide_get():
                    # ⚠ 只存**名字**。以前存的是 (名字, True) 元组，
                    #   收尾时拿元组去 bpy.data.objects.get() 会炸
                    #   （KeyError: lib must be a string or None, not bool）。
                    if obj.name not in self._hidden:
                        self._hidden.append(obj.name)
                    obj.hide_set(False)

        for obj in view_layer.objects:
            obj.select_set(False)
        for obj in objects:
            obj.select_set(True)
        view_layer.objects.active = objects[0] if objects else None
        return objects

    def restore_selection(self):
        view_layer = self.context.view_layer
        for obj in view_layer.objects:
            obj.select_set(False)
        for obj in self._selected:
            obj.select_set(True)
        try:
            view_layer.objects.active = self._active_object
        except (ReferenceError, RuntimeError):
            view_layer.objects.active = None

    # -- 材质里的图像节点（烘焙目标） -------------------------------------------

    def install_bake_target(self, material, image, label="MBakery Bake"):
        """在材质里挂一个激活的图像纹理节点 —— 烘焙结果就写进它

        同一个材质只挂一次：换任务时只换 node.image，不堆节点。
        材质被多个物体共用时也只挂一次。
        """
        self._ensure_captured()
        tree = material.node_tree
        existing = self._materials.get(material)
        if existing:
            node = tree.nodes.get(existing[-1][1]) if tree is not None else None
            if node is not None:
                node.image = image
                node.select = True
                tree.nodes.active = node
                return node.name
            self._materials.pop(material, None)      # 节点被别人删了，重挂

        if not material.use_nodes or material.node_tree is None:
            material.use_nodes = True
        tree = material.node_tree
        node = tree.nodes.new('ShaderNodeTexImage')
        node.image = image
        node.label = label
        node.location = (-600, 300)
        node.select = True
        tree.nodes.active = node
        self._materials.setdefault(material, []).append((tree, node.name))
        return node.name

    def remove_bake_targets(self):
        for material, entries in self._materials.items():
            for tree, node_name in entries:
                node = tree.nodes.get(node_name)
                if node is not None:
                    tree.nodes.remove(node)
        self._materials = {}

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

    def rollback(self):
        """把场景恢复成 capture() 时的样子"""
        self._committed = False
        self._restore_scene(restore_uv=True)

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
        if "engine" in self._snapshot:
            try:
                render.engine = self._snapshot["engine"]
            except TypeError:
                pass
        for name, value in self._snapshot.get("bake", {}).items():
            if hasattr(render.bake, name):
                setattr(render.bake, name, value)
        bake_image = getattr(render.bake, "image_settings", None)
        if bake_image is not None:
            for name, value in self._snapshot.get("bake_image_settings", {}).items():
                setattr(bake_image, name, value)
        for name, value in self._snapshot.get("color_management", {}).items():
            setattr(scene.view_settings, name, value)
        cycles = getattr(scene, "cycles", None)
        if cycles is not None:
            for name, value in self._snapshot.get("cycles", {}).items():
                setattr(cycles, name, value)
        phases.end("restore: engine + bake settings")

        phases.begin("restore: selection")
        self.restore_selection()
        phases.end("restore: selection")

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

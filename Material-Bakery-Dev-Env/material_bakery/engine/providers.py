# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   Provider —— 第三方插件接进来的唯一入口
#
#   为什么要有这一层：烘焙里有两件事是"别人才知道"的 ——
#       **烘什么**（哪几个物体是低模、哪几个是高模）
#       **怎么烘**（要不要投影、光线打多远、要不要 cage）
#   重拓补插件知道前者，引擎知道后者。provider 就是把前者的答案翻译成
#   引擎能用的东西，而不是让引擎去猜。
#
#   ⚠ 关键设计：BakeTask 里 targets（烘进哪里）和 sources（从哪里采样）
#      从第一天就是分开的两个列表。默认它们相同；一旦 provider 给出不同的
#      sources，后端就自动切到 selected-to-active 投影。
#
#   接一个新插件要做的事：
#       写一个类实现下面的方法，然后在 register() 时 set_provider(YourProvider())
#       引擎不需要改一行。
# ------------------------------------------------------------------------------------

from ..core import scene_scan
from .. import compat


class BakeProvider:
    """provider 接口说明。

    只有 resolve_targets 是必须实现的；其余都有合理默认值。
    """

    name = "default"

    # ---------------------------------------------------------------- 必须实现

    def resolve_targets(self, context, settings, gate_lookup=None):
        """返回 (groups, conflicts)。

        groups 是 [core.scene_scan.ObjectGroup, ...]。
        默认实现：照 core.scene_scan 的三种模式走。
        """
        raise NotImplementedError

    # ---------------------------------------------------------------- 可选

    def resolve_sources(self, group, settings, context):
        """这一组物体的**采样源**（高模）。

        返回空列表表示"不投影"，就用 targets 自己烘 —— 默认行为。
        返回非空列表则启用 selected-to-active。
        """
        return []

    def extra_bake_params(self, task):
        """追加给 bpy.ops.object.bake 的参数（光线距离、cage 等）"""
        return {}

    def prepare_uv(self, context, groups):
        """展 UV。返回被展过的物体名列表；None 表示交给引擎默认逻辑。"""
        return None

    def on_before_task(self, task):
        """每个任务派发前调用（比如临时隐藏某些物体）"""
        return None

    def on_after_task(self, task):
        """每个任务结束后调用（无论成功失败）"""
        return None


# ------------------------------------------------------------------------------------
#   默认 provider：目标即源，不做投影

class DefaultProvider(BakeProvider):
    """不投影。每个物体用自己的材质自己烘 —— 也就是没有重拓补的普通用法。"""

    name = "default"

    def resolve_targets(self, context, settings, gate_lookup=None):
        return scene_scan.resolve_targets(context, settings.target_mode,
                                         settings.single_collection, gate_lookup)


# ------------------------------------------------------------------------------------
#   投影 provider：低模是目标，高模是源

class ProjectionProvider(BakeProvider):
    """selected-to-active：把别的物体的细节投到目标上。

    源的来源有三种（settings.source_mode）：
        OTHERS    场景里所有可烘的物体，扣掉目标自己
        PATTERN   名字里含某段文字的物体（重拓补工作流里最常见的约定：
                  "Body_low" / "Body_high"）
        SELECTED  当前视口选中的物体

    这个类就是给重拓补插件做范本用的 —— 换一个插件，只要换
    resolve_sources 里那几行"怎么找到高模"的逻辑。
    """

    name = "projection"

    def __init__(self, settings):
        self.settings = settings

    def resolve_targets(self, context, settings, gate_lookup=None):
        return scene_scan.resolve_targets(context, settings.target_mode,
                                         settings.single_collection, gate_lookup)

    def resolve_sources(self, group, settings, context):
        mode = getattr(settings, "source_mode", 'OTHERS')
        pattern = (getattr(settings, "source_pattern", "") or "").strip().lower()
        targets = set(group.objects)

        if mode == 'SELECTED':
            candidates = [obj for obj in context.selected_objects
                          if scene_scan.is_bakeable_source(obj)]
        elif mode == 'HIDDEN':
            # 有些流水线把高模「藏起来」而不是删掉 —— 这个模式专门对付那种做法
            candidates = [obj for obj in context.scene.objects
                          if scene_scan.is_bakeable_source(obj) and obj.hide_get()]
        else:
            candidates = [obj for obj in context.scene.objects
                          if scene_scan.is_bakeable_source(obj)]

        if mode == 'PATTERN':
            if not pattern:
                return []
            candidates = [obj for obj in candidates if pattern in obj.name.lower()]

        return [obj for obj in candidates if obj not in targets]

    def extra_bake_params(self, task):
        """追加给 bpy.ops.object.bake 的参数。

        ⚠ 参数名必须是 Blender **真的接受**的。这里踩过一个大坑：
          最早写的是 `ray_distance` —— 4.5 的 object.bake 根本没有这个参数
          （它叫 `max_ray_distance`），而 compat.call_operator 会**悄悄丢掉**
          不认识的参数。结果是：用户把"光线距离"从 0.1 调到 1.0，
          什么都没发生，也不报错，烘出来全黑。所以后端现在会把
          被丢掉的参数记进报告（见 CyclesBackend.dispatch）。
        """
        params = {}
        settings = self.settings
        max_distance = (getattr(settings, "max_ray_distance", 0.0)
                        or getattr(settings, "ray_distance", 0.0) or 0.0)
        if max_distance:
            params["max_ray_distance"] = max_distance
        extrusion = getattr(settings, "cage_extrusion", 0.0) or 0.0
        if extrusion:
            params["cage_extrusion"] = extrusion
        return params


# 第三方插件注册的 provider（见 set_provider / active_provider）。
# ⚠ 它原来住在 QuadRemesher 那一段里，剥离时被一起切掉过一次 ——
#   引擎当场全线 NameError，测试抓住了。放在这里才是对的位置。
_provider = None


def set_provider(provider):
    """给第三方插件用：注册自己的 provider"""
    global _provider
    previous = _provider
    _provider = provider
    compat.log("provider: {} -> {}".format(
        getattr(previous, "name", None), getattr(provider, "name", None)))
    return previous


def get_provider():
    return _provider


def clear_provider():
    global _provider
    _provider = None


def _projection_enabled(settings):
    """投影开了没有。

    ⚠ 这里有两个拼写：UI 的 PropertyGroup 用 `use_selected_to_active`，
      纯数据的 BakeSettings 用 `selected_to_active`。只读一个的话，另一边的调用
      会静默回退到默认 provider —— 表现是"源数量显示 0 / 不投影"，
      不报错，只是功能失灵。所以两个都读。
    """
    for name in ("use_selected_to_active", "selected_to_active"):
        value = getattr(settings, name, None)
        if value is not None:
            return bool(value)
    return False


def active_provider(settings):
    """当前该用哪个 provider

    优先级：第三方注册的 > 投影 > 默认。
    """
    if _provider is not None:
        return _provider
    if not _projection_enabled(settings):
        return DefaultProvider()
    return ProjectionProvider(settings)

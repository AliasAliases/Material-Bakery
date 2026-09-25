# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   计划编译：把设置编译成一张任务清单
#
#   这是「设置」与「引擎」之间的唯一接口。引擎不知道什么叫烘焙列表、什么叫命名结构，
#   它只拿到一串 BakeTask 并逐个执行。
#
#   BakeTask 里 targets / sources 是**两个**列表 —— 这就是给 S2A 与重拓补整合留的缝。
#   本期 sources 恒等于 targets（每个物体用自己的材质直接烘）。
# ------------------------------------------------------------------------------------

from dataclasses import dataclass, field

from . import bake_types, naming, scene_scan, udim


@dataclass
class MapRequest:
    """烘焙列表里的一项

    ⚠ 分辨率是**两条边**（用户 2026-09 要求：不再限制正方形）。
      `size_y` 省略 / 为 0 时按正方形处理（旧预设、旧报告、测试里的简写）。
    """
    type_key: str
    size_x: int
    size_y: int = 0
    options: dict = field(default_factory=dict)   # 该类型的专属参数（通道打包的 R/G/B 等）

    def __post_init__(self):
        self.size_x = int(self.size_x or 0)
        self.size_y = int(self.size_y or 0) or self.size_x

    @property
    def bake_type(self):
        return bake_types.require(self.type_key)


@dataclass
class BakeTask:
    group_name: str
    bake_type: object                 # BakeType
    size_x: int                       # 最终交付尺寸（宽）
    image_name: str
    size_y: int = 0                   # 最终交付尺寸（高）；0 = 跟宽一样（正方形）
    targets: list = field(default_factory=list)   # 烘进这些物体的 UV
    sources: list = field(default_factory=list)   # 从这些物体采样（本期 == targets）
    options: dict = field(default_factory=dict)   # 节点手术要用的参数
    # 实际烘焙尺寸（开了超采样时 != 交付尺寸）
    render_size_x: int = 0
    render_size_y: int = 0
    tile: int = 0                     # UDIM 瓦片号；0 表示不是 UDIM
    uv_shift: tuple = ()              # UDIM 烘焙前要施加的 UV 平移 (-列, -行)
    bake_params: dict = field(default_factory=dict)   # provider 追加的 bake 参数
    udim_tiles: list = field(default_factory=list)   # 兼容旧字段
    exported_tiles: list = field(default_factory=list)   # [(瓦片号, 路径), ...]

    @property
    def projects(self):
        """要不要走 selected-to-active（目标和源不是同一批物体）"""
        if not self.sources:
            return False
        return [o.name for o in self.sources] != [o.name for o in self.targets]
    surgery_detail: list = field(default_factory=list)
    status: str = "pending"           # pending / baking / done / failed / skipped
    error: str = ""
    image: object = None
    exported_path: str = ""

    def __post_init__(self):
        self.size_x = int(self.size_x or 0)
        self.size_y = int(self.size_y or 0) or self.size_x
        if not self.render_size_x:
            self.render_size_x = self.size_x
        if not self.render_size_y:
            self.render_size_y = self.render_size_x if self.render_size_x == self.size_x \
                else self.size_y

    @property
    def key(self):
        return "{}|{}|{}x{}|{}".format(self.group_name, self.bake_type.key,
                                       self.size_x, self.size_y, self.tile)

    @property
    def dims(self):
        """(宽, 高) —— 交付尺寸"""
        return self.size_x, self.size_y

    @property
    def render_dims(self):
        """(宽, 高) —— 实际烘焙尺寸"""
        return self.render_size_x, self.render_size_y

    @property
    def size_label(self):
        return naming.size_label(self.size_x, self.size_y)


@dataclass
class BakeSettings:
    """计划编译需要的全部设置 —— 纯数据，不含任何 bpy 引用

    这样 core.plan 可以在无 Blender 上下文的情况下被测试。
    """
    target_mode: str = scene_scan.MODE_COLLECTIONS
    single_collection: str = ""
    maps: tuple = ()                          # (MapRequest, ...)
    prefix: str = ""
    bridge: str = naming.DEFAULT_BRIDGE
    suffix: str = naming.DEFAULT_SUFFIX
    template: str = naming.DEFAULT_TEMPLATE
    type_names: dict = field(default_factory=dict)    # type_key -> 自定义名
    shared_textures: bool = True              # 一组物体共用一套贴图（本项目恒为真）
    file_format: str = 'PNG'
    # 每个集合一个子文件夹。引擎保存时就按这个来，不是只在"导出贴图"时才分。
    use_subfolders: bool = True
    prepare_uv: bool = False
    # 给"没有 UV"的物体展 UV 时，展到哪一层（'UVMap' 或 'MBAKERY_UV'）。
    # 只影响没有 UV 的物体；已有 UV 的永远不动。
    unwrap_target: str = "UVMap"
    # 烘完是否把图像 pack 进 .blend。
    # ⚠ 默认**关**（2026-09 用户要求）：图写到输出目录、材质链接到文件，
    #   "要不要塞进 .blend"由用户自己决定（大场景几百张图塞进去文件会非常大）。
    #   没设输出目录时图只在内存里，会明确警告。
    #   ⚠ 链接式的图**不能**用 image.save() 再导出（像素缓冲可能已被释放、
    #   报 "does not have any image data"）—— 导出层改成"没像素就直接拷文件"。
    pack_into_blend: bool = False
    fake_user: bool = True

    # 抗锯齿：'OFF' 直接按 size 烘；'DOWNSCALE' 按 size×aa_scale 烘完再缩回来
    # （超采样，真正的抗锯齿）；'UPSCALE' 按 size 烘完放大（给低清预览用）
    antialias: str = 'OFF'
    aa_scale: int = 2
    bake_method: str = 'EMISSION'      # EMISSION（节点手术）/ NATIVE（原生 pass）
    # 自适应边距：贴图越大，UV 岛之间的缝隙按比例放大，免得高分辨率下漏底色
    adaptive_margin: bool = False
    margin: int = 16
    # UDIM：跨多张瓦片的物体，一张 TILED 图一次烘完，导出时按瓦片分文件
    udim: bool = False

    # Selected to Active（投影烘焙）—— 重拓补工作流的接口
    selected_to_active: bool = False
    source_mode: str = 'OTHERS'        # OTHERS / PATTERN / SELECTED
    source_pattern: str = ""
    # ⚠ 只有 max_ray_distance 是 Blender 真接受的参数名（4.x 的 object.bake）。
    #   曾经用过 ray_distance —— 那个名字不存在，会被静默丢掉。
    max_ray_distance: float = 0.0
    cage_extrusion: float = 0.0
    unhide_sources: bool = True

    # 只把本次要烘的物体留在渲染里，其余网格在运行期间藏起来
    # （Cycles 就不必为其它 31 个集合建几何 / 加载贴图）。
    # ⚠ 默认**关**：藏起来的物体会从 AO / 阴影 / 光路类通道里消失，
    #   那些通道的语义就变了（见 ui/ops.py 的 sample_rows 警告）。
    hide_unrelated: bool = False

    def type_label(self, type_key):
        """类型在文件名里的 token：优先用自定义名，去掉所有空白"""
        custom = self.type_names.get(type_key)
        label = custom if custom else bake_types.require(type_key).label
        return naming.type_token(label)


@dataclass
class BakePlan:
    groups: list = field(default_factory=list)        # [ObjectGroup, ...]
    tasks: list = field(default_factory=list)         # [BakeTask, ...]
    conflicts: list = field(default_factory=list)
    skipped: list = field(default_factory=list)       # [(组名, 原因), ...]
    warnings: list = field(default_factory=list)
    name_preview: list = field(default_factory=list)  # [(组名, 文件名), ...]
    # 从磁盘救回来的那次没有任务记录（图是现成的），交付时把组里的物体直接当"成功"
    assume_all_succeeded: bool = False

    @property
    def is_empty(self):
        return not self.tasks

    def counts(self):
        total = len(self.tasks)
        done = sum(1 for t in self.tasks if t.status == "done")
        failed = sum(1 for t in self.tasks if t.status == "failed")
        return total, done, failed

    def object_all_succeeded(self, obj):
        """这个物体的所有任务都成功了吗 —— 材质槽交付的前提条件"""
        if self.assume_all_succeeded:
            # 从磁盘救回来的那次没有"任务"可查：图是现成的，物体直接可交付。
            return any(obj in group.objects for group in self.groups)
        related = [t for t in self.tasks if obj in t.targets]
        if not related:
            return False
        return all(t.status == "done" for t in related)


def render_dims(size_x, size_y, settings):
    """实际烘焙尺寸 (宽, 高)。

    'DOWNSCALE' 是超采样：按 size×N 烘，后面再缩回 size，边缘会明显更干净。
    贴图最贵的就是这一步，所以默认是 OFF，由用户自己开。
    ⚠ 两条边都要乘：只乘一边的话非正方形会被压扁/拉长。
    """
    scale = max(1, int(getattr(settings, "aa_scale", 2) or 1))
    mode = getattr(settings, "antialias", 'OFF')
    width, height = _dims(size_x, size_y)
    if mode == 'DOWNSCALE':
        return width * scale, height * scale
    return width, height


def _dims(size_x, size_y=None):
    """(宽, 高)。size_y 省略时按正方形处理。"""
    width = int(size_x or 0)
    height = int(size_y or 0) or width
    return width, height


def effective_margin(size_x, size_y, settings):
    """边距。开了自适应就按分辨率放大 —— 4K 图用 16px 边距会漏底色。

    ⚠ 非正方形时按**大的那条**算，这是保守的方向：边距是按像素算的，
      UV 空间里同样宽的一条缝隙，在 2048 那条边上要占 2048×g 个像素，
      比 1024 那条边多一倍。按小的算就会在长边上漏底色。
    """
    base = int(getattr(settings, "margin", 16) or 0)
    if not getattr(settings, "adaptive_margin", False):
        return base
    width, height = _dims(size_x, size_y)
    return max(base, min(64, int(round(max(width, height) * 0.008))))


# 实测常数：真机 Cycles 烘焙一张 2048² 大约占 71.6 MB 常驻内存
# （test.blend，3 物体 × 3 贴图，1024/2048 两轮对比得到斜率 17.9 字节/像素，
#  见 tools/measure_bake_memory.py）。这里面既有像素缓冲区，也有 Cycles
#  自己的常驻开销，所以不能只按 w*h*4*channels 算 —— 那样会低估一大截。
MEASURED_BYTES_PER_PIXEL = 18.0


def estimate_memory_mb(tasks):
    """按**实测**斜率估算这一轮烘焙大概要吃多少内存（MB）。

    为什么不是 w*h*4：一个 2048² 的浮点图名义上 64 MB，但真烘一轮下来
    每张的实际成本约 71.6 MB（含 Cycles 开销）；而 1024² 的实测成本是
    44.7 MB，名义值只有 16 MB —— 说明固定开销占大头，按名义值算会严重低估。
    """
    total = 0.0
    for task in tasks:
        width = int(getattr(task, "render_size_x", 0)
                    or getattr(task, "render_size", 0)
                    or getattr(task, "size_x", 0) or 0)
        height = int(getattr(task, "render_size_y", 0)
                     or getattr(task, "render_size", 0)
                     or getattr(task, "size_y", 0) or 0) or width
        total += width * height * MEASURED_BYTES_PER_PIXEL
    return total / (1024.0 * 1024.0)


def memory_warning(tasks, threshold_mb=2048.0):
    """内存估算超过阈值时给一句人话；否则返回空串。"""
    estimate = estimate_memory_mb(tasks)
    if estimate < threshold_mb:
        return ""
    return ("{} maps at this resolution will need roughly {:.1f} GB of memory "
            "while baking. Lower the resolution or bake in smaller batches "
            "if Blender runs out.".format(len(tasks), estimate / 1024.0))


def build_plan(context, settings, gate_lookup=None, provider=None):
    """把设置编译成计划。不创建任何图像，也不改场景。

    provider: engine.providers.BakeProvider。给了就用它决定目标和**采样源**
              （重拓补整合的接入点）；不给就用 core.scene_scan 的默认行为。
    """
    plan = BakePlan()

    if provider is not None:
        groups, conflicts = provider.resolve_targets(context, settings, gate_lookup)
    else:
        groups, conflicts = scene_scan.resolve_targets(
            context, settings.target_mode, settings.single_collection, gate_lookup)
    plan.conflicts = conflicts

    for group in groups:
        if group.skipped:
            plan.skipped.append((group.name, group.reason))

    usable = [g for g in groups if not g.skipped and g.objects]

    # 不在视图层里的物体：`scene_scan.scan()` 已经把它们挡在 `group.objects` 外面了
    # （见那里的 NO_VIEW_LAYER）。这里只把**部分被挡**的组说清楚 ——
    # 整组被挡的已经以"skipped + 原因"的形式进了计划，不用再说两遍。
    #
    # ⚠ 老实说：按 `scan()` 现在的形状，这段**基本到不了** —— 一个物体的集合被
    #   排除了，同组的其他物体必然也在同一个集合里，于是整组一起空掉、走的是
    #   skipped 那条路（已由 tests/mb_test_view_layer.py 钉住）。留着是因为条件
    #   本身是对的，哪天 scan 允许混合来源（比如以后支持物体级目标）它就该说话。
    #
    #   这份警告的定位是给用户的**行动指令**，不是统计：被排除的集合在 Outliner 里
    #   是灰的，用户很可能根本没意识到自己把整个集合关掉了，而这一关就是整批图全废
    #   （2026-09-25 实测：96 张里 92 张）。
    for group in usable:
        missing = [name for name, reason in group.excluded
                   if reason == scene_scan.NO_VIEW_LAYER]
        if missing:
            plan.warnings.append(
                "'{}': skipped {} object(s) that are NOT in the current view layer "
                "(their collection is excluded). Blender cannot select them, so baking "
                "them would fail with \"can't be selected because it is not in View "
                "Layer\". Tick that collection in the Outliner, then bake again: "
                "{}".format(group.name, len(missing), ", ".join(missing[:5])))

    # 没有材质的物体：**只警告，不剔除**（用户 2026-09 明确要求先跳过这一步）。
    #
    # ⚠ 这里是有代价的，必须说清楚：
    #   Blender 的烘焙要求每个被选中的物体都有材质，否则整批报
    #   "No active image found, add a material or bake to an external file"，
    #   而且不说哪个物体（实测见 tools/probe_no_active_image.py）。
    #   所以下面这条警告不是"提示一下"，它是在预告**这一组会整批失败**。
    #   曾经实现过"计划阶段剔掉这些物体"，用户要求先不要动"哪些物体参与烘焙"，
    #   于是保留行为不变、把话说在前面（第 3 页清单里也有一条黄色提醒）。
    #   想启用剔除：把这行换成 scene_scan.drop_objects_without_material(group)。
    for group in usable:
        naked = scene_scan.objects_without_material(group.objects)
        if naked:
            plan.warnings.append(
                "'{}': {} object(s) have NO material — Blender will refuse this whole "
                "collection (\"No active image found\"). Give them a material, or turn "
                "the collection off: {}".format(
                    group.name, len(naked), ", ".join(o.name for o in naked[:5])))

    # 缺 UV 的物体：默认不展，直接剔除并记账
    if not settings.prepare_uv:
        for group in usable:
            dropped = scene_scan.drop_objects_without_uv(group)
            if dropped:
                plan.warnings.append(
                    "'{}': skipped {} object(s) with no UV map.".format(group.name, dropped))
        still_usable = []
        for group in usable:
            if not group.objects:
                group.skipped = True
                group.reason = "No object has a UV map"
                plan.skipped.append((group.name, group.reason))
                continue
            still_usable.append(group)
        usable = still_usable

    plan.groups = usable

    if not usable:
        return plan
    if not settings.maps:
        return plan

    # 前缀决策：用户填了就用用户的，没填就用集合名（没有集合则用物体名）。
    # auto_mode == "没填前缀" —— select_prefix 里这就是走自动命名的分支。
    auto_mode = not settings.prefix

    def prefix_for(group):
        object_name = group.objects[0].name if group.objects else group.name
        return naming.select_prefix(settings.prefix, group.name, object_name, auto_mode)

    # 撞名消解：同一批里解析出同一前缀的组，补物体名
    collide = {}
    for group in usable:
        collide.setdefault(prefix_for(group), []).append(group.name)
    for base, names in collide.items():
        if len(names) > 1:
            plan.warnings.append(
                "{} groups resolve to the same prefix '{}'; object names will be appended.".format(
                    len(names), base))

    for group in usable:
        object_name = group.objects[0].name if group.objects else group.name
        prefix = prefix_for(group)
        if len(collide.get(prefix, [])) > 1:
            prefix = naming.disambiguate(prefix, object_name)

        # UDIM：跨多张瓦片的组，**每张瓦片一个任务**，各自烘进一张平铺图。
        # 烘之前会把 UV 临时平移，让目标瓦片落到 0..1（见 engine/uv_prep.py）。
        tiles = []
        if settings.udim:
            tiles = udim.group_tiles(group.objects)
            if len(tiles) < 2:
                tiles = []
        template = naming.DEFAULT_TEMPLATE_UDIM if tiles else settings.template
        tile_list = tiles or [0]

        # 采样源 —— 默认就是目标自己；provider 给出别的物体就变成投影烘焙
        sources = list(group.objects)
        if provider is not None:
            resolved = provider.resolve_sources(group, settings, context)
            if resolved:
                sources = list(resolved)

        for request in settings.maps:
            bake_type = request.bake_type
            render_x, render_y = render_dims(request.size_x, request.size_y, settings)
            base_name = naming.render_name(template, {
                "prefix": prefix,
                "type": settings.type_label(bake_type.key),
                "bridge": settings.bridge,
                "size": naming.size_token(request.size_x, request.size_y),
                "suffix": settings.suffix,
            })
            for tile in tile_list:
                # UDIM 的文件名就是"基础名.瓦片号"，和业界习惯一致
                image_name = "{}.{}".format(base_name, tile) if tile else base_name
                task = BakeTask(
                    group_name=group.name,
                    bake_type=bake_type,
                    size_x=request.size_x,
                    size_y=request.size_y,
                    render_size_x=render_x,
                    render_size_y=render_y,
                    image_name=image_name,
                    targets=list(group.objects),
                    sources=list(sources),          # ← 投影烘焙时这里是高模
                    options=dict(request.options or {}),
                    tile=tile,
                    uv_shift=udim.tile_uv_shift(tile),
                )
                if provider is not None:
                    task.bake_params = dict(provider.extra_bake_params(task) or {})
                plan.tasks.append(task)
                if len(plan.name_preview) < 12:
                    plan.name_preview.append((group.name, image_name))

    return plan


def map_requests_from_pairs(pairs):
    """[(type_key, size), ...] -> (MapRequest, ...)，顺带去重

    接受的形状（按元素类型区分，不靠猜）:
        (type_key, size)                     -> 正方形
        (type_key, size, options_dict)       -> 正方形 + 专属参数（旧写法）
        (type_key, size_x, size_y, options)  -> 非正方形（新写法）
    ⚠ 第 3 个元素是 dict 才算旧的三元组 —— 否则 (1024, 512) 会被当成
      "尺寸 1024 + 参数 512"。写死判据、不留歧义。
    """
    seen = set()
    requests = []
    for entry in pairs:
        type_key = entry[0]
        size_x = int(entry[1] or 0)
        size_y = 0
        options = {}
        rest = list(entry[2:])
        if rest and not isinstance(rest[0], dict):
            size_y = int(rest.pop(0) or 0)
        if rest and isinstance(rest[0], dict):
            options = rest[0]
        key = (type_key, size_x, size_y or size_x)
        if key in seen:
            continue
        seen.add(key)
        bake_types.require(type_key)          # 不认识的 key 直接抛错，别静默跳过
        requests.append(MapRequest(type_key=type_key, size_x=size_x, size_y=size_y or size_x,
                                   options=dict(options or {})))
    return tuple(requests)

# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   UI 状态（存在 .blend 里的部分）
#
#   一条硬性规则：**烘焙运行期间这个文件里的任何属性都不许写**。
#   旧引擎的崩溃就是派发 bake 之后写字符串 IDProperty，撞上 bake job 线程对
#   depsgraph 的 COW 拷贝，在 IDP_CopyProperty_ex 里 use-after-free。
#   运行时的进度 / 当前任务 / 日志全部从 ui/session.py 的活动对象里现读，
#   一个字节都不落进 IDProperty。tests/mb_test_wizard.py 里有静态断言盯着这条。
# ------------------------------------------------------------------------------------

import bpy

from ..core import bake_types, naming, scene_scan


PAGE_TARGETS = "targets"
PAGE_SETTINGS = "settings"
PAGE_BAKE = "bake"
PAGE_RESULT = "result"

PAGES = (
    (PAGE_TARGETS, "Targets & Maps", "Pick what to bake"),
    (PAGE_SETTINGS, "Settings", "Naming, output, quality"),
    (PAGE_BAKE, "Bake", "Run it"),
    (PAGE_RESULT, "Result", "Deliver"),
)

PAGE_ORDER = [key for key, _label, _desc in PAGES]


def _apply_naming_preset(settings, context):
    """命名方案下拉列表的 update 回调

    选一个方案 = 一次把 template / bridge / suffix 三件套都设好。
    用户要的是"列表"，因为手写占位符容易拼错（而拼错的后果是一个错误的文件名，
    要烘完才发现）。
    """
    applied = naming.apply_preset(settings.template_preset)
    if applied is None:
        return
    template, bridge, suffix = applied
    settings.template = template
    settings.bridge = bridge
    settings.suffix = suffix


# 采样"自定义"输入框的回调：动一下输入框就把那一组切到 Custom。
# 为什么要这样：用户说了"旁边加一个输入框，可以让用户自己输入" ——
# 如果输入框在下拉不是 Custom 时是灰的，他改了数字却什么都没发生，
# 那又是一次"改了没反应"的体验（这个项目已经被这类问题咬过好几次）。
def _use_custom_samples(settings, context):
    if settings.samples_mode != 'CUSTOM':
        settings.samples_mode = 'CUSTOM'


def _use_custom_sampled_samples(settings, context):
    if settings.sampled_maps_mode != 'CUSTOM':
        settings.sampled_maps_mode = 'CUSTOM'

FORMATS = (
    ('PNG', "PNG", ""),
    ('JPEG', "JPEG", ""),
    ('TIFF', "TIFF", ""),
    ('OPEN_EXR', "OpenEXR", ""),
    ('OPEN_EXR_MULTILAYER', "OpenEXR MultiLayer", ""),
    ('TARGA', "Targa", ""),
    ('BMP', "BMP", ""),
    ('WEBP', "WebP", ""),
)

DEFAULT_SIZE = 2048
SIZE_CHOICES = (256, 512, 1024, 2048, 4096, 8192)


TARGET_MODES = (
    (scene_scan.MODE_COLLECTIONS, "Collections",
     "One texture set per collection; objects without a collection get their own"),
    (scene_scan.MODE_SINGLE, "Single Collection", "Bake one collection only"),
    (scene_scan.MODE_SELECTED, "Selected Objects",
     "Bake only the objects selected in the viewport"),
)


# ⚠ Blender 会**丢掉**标识符为空的枚举项（当成不可选的分隔符），
#   所以"还没选集合"不能用 "" 表示 —— 赋值会直接抛 TypeError。
#   这是原版拿尾随空格当 key 的同一个坑的另一面。这里用一个显式哨兵值。
COLLECTION_NONE = "__none__"


def _collection_items(self, context):
    """动态列表：场景里现有的集合。

    ⚠ items 是函数时不能给 default（Blender 只接受整数下标）。
    """
    items = [(COLLECTION_NONE, "(pick a collection)", "")]
    scene = context.scene if context else None
    if scene is not None:
        for collection in sorted(bpy.data.collections, key=lambda c: c.name):
            if collection.library:
                continue
            items.append((collection.name, collection.name, ""))
    return items


def _build_source_type_items():
    """通道打包的通道来源：只能是"能从着色器插口取到值"的类型

    ⚠ 必须是**静态元组**。Blender 不允许 items 是函数时再给字符串 default
      （只接受整数下标），这个坑在本工程里已经踩到第二次了
      （上一次是 single_collection）。凡是能静态列出来的枚举就别用回调。
    """
    items = [("", "(none)", "")]
    for bake_type in bake_types.iterator():
        if bake_type.kind in (bake_types.BakeKind.SHADER_SOCKET,
                              bake_types.BakeKind.AO_NODE):
            items.append((bake_type.key,
                          "{} ({})".format(bake_type.label, bake_type.group), ""))
    return tuple(items)


SOURCE_TYPE_ITEMS = _build_source_type_items()


class MBAKERY_MapItem(bpy.types.PropertyGroup):
    type_key: bpy.props.EnumProperty(
        name="Map", items=bake_types.type_enum_items(), default="shader.base_color")
    # ⚠ 分辨率是**两条边**（用户 2026-09 要求：凭什么只能烘正方形）。
    #   第一页那一行就是这两个框，先宽后高。文件名里的 {size} 由
    #   naming.size_token 渲染：两边都是 1024 倍数就取**大的那条**写 k
    #   （2048×1024 -> 2k，1024×4096 -> 4k），否则写完整像素（1024x512）。
    size_x: bpy.props.IntProperty(
        name="W", default=DEFAULT_SIZE, min=16, max=16384,
        description="Texture width in pixels")
    size_y: bpy.props.IntProperty(
        name="H", default=DEFAULT_SIZE, min=16, max=16384,
        description="Texture height in pixels. Leave it the same as W for a square "
                    "map; make it different for a non-square one")
    enabled: bpy.props.BoolProperty(name="Enabled", default=True)

    # ---------------------------------------------------------------- 专属参数
    # 每个类型只会用到其中几项，面板按 kind 决定显示哪些。
    channel_r: bpy.props.EnumProperty(
        name="R", items=SOURCE_TYPE_ITEMS, default="shader.roughness")
    channel_g: bpy.props.EnumProperty(
        name="G", items=SOURCE_TYPE_ITEMS, default="shader.metallic")
    channel_b: bpy.props.EnumProperty(
        name="B", items=SOURCE_TYPE_ITEMS, default="misc.ao")

    uv_map: bpy.props.StringProperty(
        name="UV Map", description="Leave empty to use the active render UV layer")
    attribute_name: bpy.props.StringProperty(name="Attribute")

    ao_distance: bpy.props.FloatProperty(name="Distance", default=0.0, min=0.0, max=100.0)
    ao_only_local: bpy.props.BoolProperty(
        name="Only Local", default=False,
        description="Ignore other objects when computing occlusion")
    ao_samples: bpy.props.IntProperty(name="Samples", default=16, min=1, max=256)

    pointiness_contrast: bpy.props.FloatProperty(
        name="Contrast", default=1.0, min=-100.0, max=100.0)
    pointiness_brightness: bpy.props.FloatProperty(
        name="Brightness", default=0.0, min=-100.0, max=100.0)

    normal_space: bpy.props.EnumProperty(
        name="Space", default='TANGENT',
        items=(('TANGENT', "Tangent", "Standard for game engines and glTF"),
               ('OBJECT', "Object", "Object space normals")))

    @property
    def bake_type(self):
        return bake_types.get(self.type_key)

    def uses_kind(self, kind):
        bake_type = self.bake_type
        return bake_type is not None and bake_type.kind is kind

    @property
    def uses_normal_space(self):
        bake_type = self.bake_type
        return bake_type is not None and bake_type.bake_pass == "NORMAL"

    def options(self):
        """这个烘焙项的专属参数 -> 给节点手术 / 后端用的字典"""
        bake_type = self.bake_type
        if bake_type is None:
            return {}
        kind = bake_type.kind
        if kind is bake_types.BakeKind.CHANNEL_PACK:
            return {"channel_r": self.channel_r or None,
                    "channel_g": self.channel_g or None,
                    "channel_b": self.channel_b or None}
        if kind is bake_types.BakeKind.AO_NODE:
            return {"ao_distance": self.ao_distance,
                    "ao_only_local": self.ao_only_local,
                    "ao_sample": self.ao_samples}
        if kind is bake_types.BakeKind.POINTINESS:
            return {"pointiness_contrast": self.pointiness_contrast,
                    "pointiness_brightness": self.pointiness_brightness}
        if kind is bake_types.BakeKind.UV_MAP:
            return {"uv_map": self.uv_map}
        if kind is bake_types.BakeKind.COLOR_ATTR:
            return {"attribute_name": self.attribute_name}
        if bake_type.bake_pass == "NORMAL":
            return {"normal_space": self.normal_space}
        return {}


class MBAKERY_GroupItem(bpy.types.PropertyGroup):
    """集合列表里的一行。扫描时刷新，勾选状态是用户的。"""
    name: bpy.props.StringProperty()
    enabled: bpy.props.BoolProperty(default=True)
    object_count: bpy.props.IntProperty()
    missing_uv: bpy.props.IntProperty()
    material_count: bpy.props.IntProperty()
    overlap_ratio: bpy.props.FloatProperty()
    has_conflict: bpy.props.BoolProperty()


class MBAKERY_TextureItem(bpy.types.PropertyGroup):
    """第 4 页的贴图列表：烘完刷新，勾选决定导出哪些"""
    image_name: bpy.props.StringProperty()
    group: bpy.props.StringProperty()
    type_key: bpy.props.StringProperty()
    type_label: bpy.props.StringProperty()
    # 显示用：两条边都要（列表里印 "2048 × 1024"，正方形印 "2k"）
    size_x: bpy.props.IntProperty()
    size_y: bpy.props.IntProperty()
    enabled: bpy.props.BoolProperty(default=True)
    exported_path: bpy.props.StringProperty()
    status: bpy.props.StringProperty()
    # 从磁盘救回来的（不是这次会话烘出来的）
    imported: bpy.props.BoolProperty(default=False)


class MBAKERY_Settings(bpy.types.PropertyGroup):

    # ---------------------------------------------------------------- 向导
    page: bpy.props.EnumProperty(name="Step", items=PAGES, default=PAGE_TARGETS)

    # ---------------------------------------------------------------- 第 1 页
    target_mode: bpy.props.EnumProperty(
        name="Targets", items=TARGET_MODES, default=scene_scan.MODE_COLLECTIONS)
    single_collection: bpy.props.EnumProperty(name="Collection", items=_collection_items)
    groups: bpy.props.CollectionProperty(type=MBAKERY_GroupItem)
    maps: bpy.props.CollectionProperty(type=MBAKERY_MapItem)
    map_index: bpy.props.IntProperty()
    scanned: bpy.props.BoolProperty(default=False)

    # ---------------------------------------------------------------- 第 2 页
    prefix: bpy.props.StringProperty(
        name="Prefix", description="Empty = use the collection name (or object name)")
    bridge: bpy.props.StringProperty(name="Bridge", default=naming.DEFAULT_BRIDGE)
    suffix: bpy.props.StringProperty(name="Suffix", default=naming.DEFAULT_SUFFIX)
    template: bpy.props.StringProperty(name="Template", default=naming.DEFAULT_TEMPLATE)
    template_preset: bpy.props.EnumProperty(
        name="Scheme", default=naming.PRESET_DEFAULT,
        description="Pick a naming scheme — it fills in the template, bridge and suffix",
        items=naming.preset_items(), update=_apply_naming_preset)

    output_dir: bpy.props.StringProperty(
        name="Output", subtype='DIR_PATH',
        description="Folder for exported textures. Leave empty to skip exporting.")
    file_format: bpy.props.EnumProperty(name="Format", items=FORMATS, default='PNG')
    use_subfolders: bpy.props.BoolProperty(
        name="Per-collection Subfolders", default=True,
        description="Put each collection's textures in its own subfolder")
    overwrite: bpy.props.BoolProperty(
        name="Overwrite", default=True, description="Replace existing files")
    fake_user: bpy.props.BoolProperty(
        name="Fake User", default=True, description="Keep baked images when the file is closed")
    pack_into_blend: bpy.props.BoolProperty(
        # ⚠ 默认**关**（2026-09 用户要求）：烘完的图写到输出目录、材质**链接到文件**，
        #   把"要不要塞进 .blend"留给用户自己决定 —— 大场景几百张贴图塞进去
        #   会让文件变得巨大、内存也吃不消。
        #   没设输出目录时会明确警告（那种情况图只在内存里，重开文件就没了）。
        name="Pack Textures Into .blend", default=False,
        description="Store the baked pixels inside the .blend instead of linking to the "
                    "files in the output folder. Off by default: textures are written to "
                    "disk and the materials point at them")

    # 采样分两组（用户要求）：确定值通道一组、需要真采样的通道一组，
    # 每组都是"低/中/高 + 自定义输入框"。
    # ⚠ 烘完必须把场景原来的采样数还回去 —— 这件事由事务做
    #   （SceneTransaction 快照了 cycles.samples，commit/rollback 都会还原）。
    samples_mode: bpy.props.EnumProperty(
        name="Bake Quality", default='LOW',
        description="Samples for channel bakes (color, roughness, metallic, normal...). "
                    "Every sample of those gives the same value, so more samples only "
                    "cost time. The scene's own value is restored when the bake ends",
        items=(('LOW', "Low (1)", "One sample — enough for channel values"),
               ('MEDIUM', "Medium (16)", "16 samples"),
               ('HIGH', "High (64)", "64 samples"),
               ('CUSTOM', "Custom", "Use the number in the box next to this list"),
               ('RENDER', "Use Render Settings",
                "Keep whatever the scene is set to (restored afterwards either way)")))
    samples_custom: bpy.props.IntProperty(
        name="Samples", default=1, min=1, soft_max=4096,
        description="Sample count used when Bake Quality is set to Custom",
        update=_use_custom_samples)
    sampled_maps_mode: bpy.props.EnumProperty(
        # ⚠ 默认是 **Medium (64)**，不是 Low：用户 2026-09 的反馈原话是
        #   "AO 还是需要更高的采样" —— AO / 阴影这类通道 16 个采样出来就是一张
        #   雪花图，而它们在整轮里的占比很小（确定值通道才是大头）。
        #   16 仍然可选（Low），只是不再当默认。
        name="Sampled Maps", default='MEDIUM',
        description="Samples for maps that really need sampling: Ambient Occlusion, "
                    "Shadow, and light-path passes (Combined / Diffuse / Glossy / "
                    "Transmission). Too few shows up as noise",
        items=(('LOW', "Low (16)", "16 samples — visible noise on AO and shadows"),
               ('MEDIUM', "Medium (64)", "64 samples — a good default for AO and shadows"),
               ('HIGH', "High (256)", "256 samples — cleanest, slowest"),
               ('CUSTOM', "Custom", "Use the number in the box next to this list"),
               ('RENDER', "Use Render Settings",
                "Keep whatever the scene is set to (restored afterwards either way)")))
    sampled_maps_custom: bpy.props.IntProperty(
        name="Samples", default=16, min=1, soft_max=4096,
        description="Sample count used when Sampled Maps is set to Custom",
        update=_use_custom_sampled_samples)
    bake_method: bpy.props.EnumProperty(
        name="Bake Method", default='EMISSION',
        description="How each channel is fed into the bake",
        items=(('EMISSION', "Emission + Node Surgery (default)",
                "Temporarily rewire each material so the wanted socket drives Emission, "
                "then bake EMIT. Works for every channel; the material is restored "
                "afterwards"),
               ('NATIVE', "Native Render Passes",
                "Let Cycles bake its own passes (Diffuse Color for Base Color, Roughness, "
                "Normal) without touching materials. Channels that have no matching pass "
                "fall back to Emission and say so in the report")))
    margin: bpy.props.IntProperty(name="Margin", default=16, min=0, max=64)
    adaptive_margin: bpy.props.BoolProperty(
        name="Adaptive Margin", default=False,
        description="Scale the margin with the texture size — a 4K map needs a wider "
                    "margin than a 1K one or the background bleeds through")
    # Hide Unrelated While Baking（2026-09 性能一轮加的，默认关）
    #
    # 为什么值得有：他的场景是 32 个集合、200+ 个物体，而一次只烘一个集合。
    # 不在本次范围内的几何对 Cycles 来说全是白干的活（建 BVH、装贴图、占显存），
    # 在大场景里这能占掉相当一部分时间。
    #
    # ⚠ 为什么默认**关**、而且要在 Checklist 里警告：
    #   AO / 阴影 / 光路类通道是**用整个可见场景**算出来的。藏起来的东西就
    #   不再遮挡、不再投影 —— 那些通道的结果会变（不是变错，是变成另一件事）。
    #   确定值通道（颜色/粗糙/金属/法线）完全不受影响，所以这个开关对它们
    #   只有好处。这个取舍必须写在面板上，不能让用户自己发现。
    hide_unrelated: bpy.props.BoolProperty(
        name="Hide Unrelated While Baking", default=False,
        description="Hide every mesh that is not part of this run, so Cycles does not "
                    "build geometry and load textures for the rest of the scene. "
                    "Deterministic channels (color, roughness, metallic, normal) are "
                    "unaffected; AO, shadow and light-path passes will no longer see "
                    "the hidden objects. Lights and cameras are never hidden")

    antialias: bpy.props.EnumProperty(
        name="Antialiasing", default='OFF',
        items=(('OFF', "Off", "Bake straight at the target size"),
               ('DOWNSCALE', "Supersample (bake big, shrink)",
                "Bake at size x N and scale down — real antialiasing, N times slower"),
               ('UPSCALE', "Upscale (bake small, enlarge)",
                "Bake at the target size and enlarge — for low-res previews only")))
    aa_scale: bpy.props.IntProperty(name="AA Scale", default=2, min=2, max=4)

    udim: bpy.props.BoolProperty(
        name="UDIM", default=False,
        description="Objects whose UVs span several tiles get one tiled image; "
                    "exporting writes one file per tile")

    preset_name: bpy.props.StringProperty(name="Preset Name", default="")

    prepare_uv: bpy.props.BoolProperty(
        name="Unwrap With SmartUV", default=False,
        description="Run Smart UV Project on objects that have no UV map. Objects that "
                    "already have UVs are never touched")
    unwrap_target: bpy.props.EnumProperty(
        # 用户要求："给一个选择，选要不要展在默认的 UVmap 上，还是新建一个
        #            MBAKERY_UV 再展" —— 只对"没有 UV"的物体生效。
        name="Unwrap Into", default='UVMap',
        description="Which UV layer the new unwrap goes into. Only affects objects that "
                    "have no UV map at all",
        items=(('UVMap', "Default UVMap",
                "Create/use a layer named 'UVMap' — the object ends up looking like any "
                "normally unwrapped mesh"),
               ('MBAKERY_UV', "New MBAKERY_UV layer",
                "Unwrap into a separate layer called 'MBAKERY_UV' and make it the render "
                "layer — easy to spot, easy to remove later")))

    # ---------------------------------------------------------------- Selected to Active
    use_selected_to_active: bpy.props.BoolProperty(
        name="Bake From High-Poly", default=False,
        description="Project detail from other objects onto the targets. This is the "
                    "retopology workflow: the target is the low-poly, the sources are "
                    "the high-poly")
    source_mode: bpy.props.EnumProperty(
        name="Sources", default='OTHERS',
        items=(('HIDDEN', "Hidden Objects",
                "Use every hidden mesh object — some pipelines hide the high-poly"),
               ('OTHERS', "Every Other Object",
                "Use every mesh object that is not part of the target group"),
               ('PATTERN', "Name Contains",
                "Use objects whose name contains the text below"),
               ('SELECTED', "Selected Objects",
                "Use the objects currently selected in the viewport")))
    source_pattern: bpy.props.StringProperty(
        name="Pattern", default="high",
        description="Source objects are those whose name contains this text")
    max_ray_distance: bpy.props.FloatProperty(
        name="Ray Distance", default=0.0, min=0.0, max=100.0,
        description="How far along the normal to search for a source surface. "
                    "0 means the source must be found by cage extrusion only")
    cage_extrusion: bpy.props.FloatProperty(
        name="Cage Extrusion", default=0.0, min=0.0, max=100.0,
        description="Push the projection ray start outwards by this much")
    unhide_sources: bpy.props.BoolProperty(
        name="Unhide Sources", default=True,
        description="Hidden high-poly objects are temporarily shown so they can be "
                    "sampled; the hidden state is restored afterwards")

    # ---------------------------------------------------------------- 第 4 页
    # ⚠ material_style 已废弃。用户的原话：
    #   "我不是很理解为啥要加这个下拉选单，第二个选项只保留图片纹理也没用啊，
    #    用户还是得手动加 shader 连到输出"
    #   —— 他说得对：`TexImage Only` 出来的材质没有着色器接到 Material Output，
    #   根本不是能用的成品。现在**只有一种**交付材质（Principled BSDF），
    #   面板上不再出现这个下拉。属性保留只为兼容旧预设。
    material_style: bpy.props.EnumProperty(
        name="Material", default='PRINCIPLED',
        items=(('PRINCIPLED', "Principled BSDF",
                "Reconnect every baked map into a Principled BSDF"),
               ('SIMPLE', "TexImage Only (deprecated)",
                "Image nodes with no shader — not usable for rendering")))
    # ⚠ use_baked_uv 已废弃：材质里**不再加 UVMap 节点**（那正是"表面渲染错误"的
    #   来源：材质硬引用打包层名，而有些物体根本没有那一层）。
    #   属性保留只为让旧预设还能读进来（presets.FIELDS 里也留着），UI 上不再出现。
    use_baked_uv: bpy.props.BoolProperty(
        name="Use Packed UV Layer", default=True,
        description="Deprecated — the baked material now follows each object's active "
                    "render UV layer instead of naming one")

    textures: bpy.props.CollectionProperty(type=MBAKERY_TextureItem)
    texture_index: bpy.props.IntProperty()
    # 第 5 页的贴图列表按集合分组：默认折起来，只看得到"哪个集合、几张、多大"。
    # 96 张贴图全铺开会把面板淹掉（用户原话："太长太杂了"）。
    # 这里存"当前展开的集合名"，空串=全折叠，'*'=全展开。
    texture_expanded_group: bpy.props.StringProperty(default="")
    auto_deliver: bpy.props.BoolProperty(
        name="Auto Deliver", default=True,
        description="After a successful bake, build one material per collection and swap "
                    "it onto the objects. Restore Original Slots undoes it in one click")
    compare_baked: bpy.props.BoolProperty(
        name="Show Baked", default=False,
        description="Viewport shows the baked material; turn off to see the original")

    def selected_collection(self):
        """真正要烘的集合名；未选时返回空串。

        items 是函数时不能设 default，所以未赋值时拿到的是空串或哨兵值，
        两种都当作"没选"处理。
        """
        value = self.single_collection
        if not value or value == COLLECTION_NONE:
            return ""
        return value

    def map_requests(self):
        """(类型, 宽, 高, 专属参数) 四元组 —— 参数必须一路传到引擎，
        否则通道打包永远只能用默认的 R/G/B、UV 层名和法线空间也没法指定。"""
        return [(item.type_key, item.size_x, item.size_y, item.options())
                for item in self.maps if item.enabled]

    def enabled_group_names(self):
        return [item.name for item in self.groups if item.enabled]

    def page_index(self):
        try:
            return PAGE_ORDER.index(self.page)
        except ValueError:
            return 0


CLASSES = (MBAKERY_MapItem, MBAKERY_GroupItem, MBAKERY_TextureItem, MBAKERY_Settings)

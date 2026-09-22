# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   烘焙类型注册表 —— 唯一真相源
#
#   原版 Auto Bake 把"烘焙类型"这一个概念拆成了 5 张互相对齐的表：
#       bake_items（枚举，还写了 4.0+ / <4.0 两遍）
#       bake_type_info（类型 -> bake pass + label）
#       type_aliases（类型 -> socket 别名）
#       三组颜色空间集合（float / color / vector）
#       以及散落在 modal 里的每类型参数判断
#   任何一张改了别的没跟上就是静默错误。这里合成一张。
#
#   原版还有两个地雷，本表从结构上消除：
#     1. 'Displacement ' 与 'Displacement' 只靠**尾随空格**区分
#        -> 改成稳定点分 key: 'misc.displacement' / 'multires.displacement'
#     2. enum 的 key 是 node bl_idname（'ShaderNodeBsdfPrincipled'），
#        却被拿去和 node.type（'BSDF_PRINCIPLED'）比较，条件恒为真
#        -> 本模块的 key 只当 key 用，不当 Blender 标识符用
#
#   3.x / 4.x 的 socket 名差异不做版本分支 —— 别名全部内联在 sockets 里，
#   运行时按顺序匹配，天然版本无关。
# ------------------------------------------------------------------------------------

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator


class BakeKind(Enum):
    """烘焙的**做法**。决定用哪个节点手术 handler。"""
    SHADER_SOCKET = "shader_socket"    # 从 shader 的某个输入口取
    CHANNEL_PACK = "channel_pack"      # 三个灰度合成 RGB
    AO_NODE = "ao_node"                # 挂 Ambient Occlusion 节点
    POINTINESS = "pointiness"          # Geometry.Pointiness + Brightness/Contrast
    UV_MAP = "uv_map"                  # 挂 UVMap 节点
    COLOR_ATTR = "color_attr"          # 挂 Color Attribute 节点
    DISPLACEMENT = "displacement"      # 从 Material Output 的 Displacement 取
    STANDARD_PASS = "standard_pass"    # 直接用 Blender 的 bake pass，不碰节点


class DataChannel(Enum):
    """数据通道。决定**色彩空间**与**回接节点类型**。"""
    FLOAT = "float"      # 灰度 -> Float 节点
    COLOR = "color"      # 颜色 -> RGB 节点
    VECTOR = "vector"    # 向量 -> Diffuse BSDF 节点

    @property
    def is_data(self):
        """是不是数据贴图（必须 Non-Color，不能过 sRGB 变换）"""
        return self is not DataChannel.COLOR

    @property
    def colorspace(self):
        return "Non-Color" if self.is_data else "sRGB"

    @property
    def depth(self):
        """位深 —— 数据贴图 16 位，颜色 8 位"""
        return '16' if self.is_data else '8'


@dataclass(frozen=True)
class BakeType:
    key: str                            # 稳定 ID，永不变（预设/报告里用它）
    label: str                          # UI 显示名
    group: str                          # 菜单分组
    kind: BakeKind
    bake_pass: str                      # EMIT / NORMAL / COMBINED / AO / ...
    channel: DataChannel
    sockets: tuple = ()                 # 要找的 socket 名，按顺序匹配（含 3.x 别名）
    params: tuple = ()                  # 该类型暴露的设置项名字
    needs_uv: bool = True               # 目标物体是否必须有 UV
                                        # （S2A 启用后**采样源**不需要 UV，见设计文档 §4.2）

    # ---------------------------------------------------------------- 便利属性

    @property
    def is_standard(self):
        return self.kind is BakeKind.STANDARD_PASS

    @property
    def needs_normal_space(self):
        """是否显示 normal space + R/G/B 反转选项"""
        return self.bake_pass == "NORMAL"

    @property
    def needs_sampling(self):
        """这个通道要不要**真的多次采样**

        ⚠ 这是"烘焙要多久"的关键分类：
          - 确定值通道（EMIT 家族：颜色/粗糙/金属/IOR/通道打包/UV/位置/法线…）
            每一个采样算出来都是同一个数，烘 64 遍等于把同一张图算 64 遍。
            用户那轮 128 张 2048² 花了 1301 秒，绝大部分是白烧的。
          - 需要采样的通道（AO / 阴影 / 直接-间接光：COMBINED、DIFFUSE、GLOSSY、
            TRANSMISSION）每一次采样都是不同的一条光线，采样少了就是噪点。
        所以这两类走**两个不同的采样数**（UI 上是两组下拉，见第 3 页 Quality）。
        """
        # ⚠ AO 节点型（misc.ao）的 bake_pass 是 EMIT，但它内部是 Ambient
        #   Occlusion 着色器 —— 那玩意儿**真的在打光线**，采样少了就是噪点。
        #   只按 bake_pass 判会把 AO 归到"确定值"那一档（第一版就是这么错的，
        #   冒烟检查里 misc.ao 判成了 False）。
        if self.kind is BakeKind.AO_NODE:
            return True
        return self.bake_pass in SAMPLED_PASSES

    @property
    def all_params(self):
        """该类型实际要暴露的全部设置项（含派生项）"""
        extra = ("normal_space", "normal_r", "normal_g", "normal_b") if self.needs_normal_space else ()
        return tuple(self.params) + extra

    def matches_socket(self, socket_name):
        return socket_name in self.sockets


# ------------------------------------------------------------------------------------
#   类型表
#
#   字段顺序：(key, label, group, kind, bake_pass, channel, sockets, params)

_F = DataChannel.FLOAT
_C = DataChannel.COLOR
_V = DataChannel.VECTOR
_S = BakeKind.SHADER_SOCKET
_STD = BakeKind.STANDARD_PASS

_TABLE = (
    # ---------------------------------------------------------------- Shader
    ("shader.base_color", "Base Color", "Shader", _S, "EMIT", _C,
     ("Base Color", "Color", "Tint"), ()),
    ("shader.metallic", "Metallic", "Shader", _S, "EMIT", _F,
     ("Metallic",), ()),
    ("shader.roughness", "Roughness", "Shader", _S, "EMIT", _F,
     ("Roughness",), ()),
    ("shader.ior", "IOR", "Shader", _S, "EMIT", _F,
     ("IOR",), ()),
    ("shader.alpha", "Alpha", "Shader", _S, "EMIT", _F,
     ("Alpha", "Transparency"), ()),
    ("shader.normal", "Normal", "Shader", _S, "NORMAL", _V,
     ("Normal",), ()),

    # ---------------------------------------------------------------- Subsurface
    ("shader.subsurface_weight", "Subsurface Weight", "Subsurface", _S, "EMIT", _F,
     ("Subsurface Weight", "Subsurface"), ()),
    ("shader.subsurface_radius", "Subsurface Radius", "Subsurface", _S, "EMIT", _V,
     ("Subsurface Radius", "Radius"), ()),
    ("shader.subsurface_scale", "Subsurface Scale", "Subsurface", _S, "EMIT", _F,
     ("Subsurface Scale",), ()),
    ("shader.subsurface_ior", "Subsurface IOR", "Subsurface", _S, "EMIT", _F,
     ("Subsurface IOR",), ()),
    ("shader.subsurface_anisotropy", "Subsurface Anisotropy", "Subsurface", _S, "EMIT", _F,
     ("Subsurface Anisotropy", "Anisotropy"), ()),

    # ---------------------------------------------------------------- Specular
    ("shader.specular_ior_level", "Specular IOR Level", "Specular", _S, "EMIT", _F,
     ("Specular IOR Level", "Specular"), ()),
    ("shader.specular_tint", "Specular Tint", "Specular", _S, "EMIT", _C,
     ("Specular Tint",), ()),
    ("shader.anisotropic", "Anisotropic", "Specular", _S, "EMIT", _F,
     ("Anisotropic",), ()),
    ("shader.anisotropic_rotation", "Anisotropic Rotation", "Specular", _S, "EMIT", _F,
     ("Anisotropic Rotation",), ()),
    ("shader.tangent", "Tangent", "Specular", _S, "EMIT", _V,
     ("Tangent",), ()),

    # ---------------------------------------------------------------- Coat
    ("shader.coat_weight", "Coat Weight", "Coat", _S, "EMIT", _F,
     ("Coat Weight", "Clear Coat", "Clearcoat"), ()),
    ("shader.coat_roughness", "Coat Roughness", "Coat", _S, "EMIT", _F,
     ("Coat Roughness", "Clear Coat Roughness", "Clearcoat Roughness"), ()),
    ("shader.coat_ior", "Coat IOR", "Coat", _S, "EMIT", _F,
     ("Coat IOR",), ()),
    ("shader.coat_tint", "Coat Tint", "Coat", _S, "EMIT", _C,
     ("Coat Tint",), ()),
    ("shader.coat_normal", "Coat Normal", "Coat", _S, "NORMAL", _V,
     ("Coat Normal", "Clear Coat Normal", "Clearcoat Normal"), ()),

    # ---------------------------------------------------------------- 其余 shader
    ("shader.transmission_weight", "Transmission Weight", "Shader", _S, "EMIT", _F,
     ("Transmission Weight", "Transmission"), ()),
    ("shader.sheen_weight", "Sheen Weight", "Shader", _S, "EMIT", _F,
     ("Sheen Weight", "Sheen"), ()),
    ("shader.sheen_roughness", "Sheen Roughness", "Shader", _S, "EMIT", _F,
     ("Sheen Roughness",), ()),
    ("shader.sheen_tint", "Sheen Tint", "Shader", _S, "EMIT", _C,
     ("Sheen Tint",), ()),
    ("shader.emission_color", "Emission Color", "Shader", _S, "EMIT", _C,
     ("Emission Color", "Emissive Color", "Emission"), ()),
    ("shader.emission_strength", "Emission Strength", "Shader", _S, "EMIT", _F,
     ("Emission Strength", "Strength"), ()),

    # ---------------------------------------------------------------- Miscellaneous
    ("misc.channel_packing", "Channel Packing", "Miscellaneous", BakeKind.CHANNEL_PACK, "EMIT", _C,
     (), ("channel_r", "channel_g", "channel_b")),
    ("misc.color_attribute", "Color Attribute", "Miscellaneous", BakeKind.COLOR_ATTR, "EMIT", _C,
     (), ("attribute_name",)),
    ("misc.ao", "Ambient Occlusion", "Miscellaneous", BakeKind.AO_NODE, "EMIT", _F,
     (), ("ao_sample", "ao_sample_use", "ao_inside", "ao_only_local", "ao_distance")),
    ("misc.pointiness", "Pointiness", "Miscellaneous", BakeKind.POINTINESS, "EMIT", _F,
     (), ("pointiness_contrast", "pointiness_brightness")),
    ("misc.displacement", "Displacement (Socket)", "Miscellaneous", BakeKind.DISPLACEMENT, "EMIT", _F,
     (), ("displacement_source",)),

    # ---------------------------------------------------------------- Multires
    # ⚠ 原本这里有 multires.normals / multires.displacement 两个类型，**已删除**。
    #   原因：它们走的是 bpy.ops.object.bake_image(bake_type='NORMALS')，
    #   跟其它所有类型用的 object.bake 是两个操作符、两套枚举。
    #   实测在 object.bake 上传 'NORMALS' 直接 TypeError（枚举里只有 'NORMAL'），
    #   也就是说这两个类型从加进来那天起就是坏的。
    #   而它的用途（把雕刻的多级细分细节烘成法线/置换图）只在"会雕刻"的工作流里才有意义 ——
    #   本项目不做雕刻工作流，所以选择删掉，而不是维护一条无人使用的独立烘焙路径。
    #   如果以后真的要支持，需要单独接一条 bake_image 路径，
    #   并且注意 bake_image 不触发 object_bake_complete，完成判定要另做。

    # ---------------------------------------------------------------- Standard pass
    ("standard.combined", "Combined", "Standard", _STD, "COMBINED", _C,
     (), ("pass_direct", "pass_indirect", "pass_emit",
          "pass_diffuse", "pass_glossy", "pass_transmission")),
    ("standard.ao", "Ambient Occlusion", "Standard", _STD, "AO", _F,
     (), ("ao_local_only", "ao_use_normal")),
    ("standard.normal", "Normal", "Standard", _STD, "NORMAL", _V,
     (), ()),
    ("standard.roughness", "Roughness", "Standard", _STD, "ROUGHNESS", _F,
     (), ()),
    ("standard.glossy", "Glossy", "Standard", _STD, "GLOSSY", _F,
     (), ("pass_direct", "pass_indirect", "pass_color")),
    ("standard.position", "Position", "Standard", _STD, "POSITION", _V,
     (), ()),
    ("standard.shadow", "Shadow", "Standard", _STD, "SHADOW", _F,
     (), ()),
    ("standard.diffuse", "Diffuse", "Standard", _STD, "DIFFUSE", _C,
     (), ("pass_direct", "pass_indirect", "pass_color")),
    # UV 是唯一的"标准 pass + 节点手术"混合体：
    # bake_pass 用 Blender 的 UV pass，同时插入 UVMap 节点让 uv_map 参数生效
    ("standard.uv", "UV", "Standard", BakeKind.UV_MAP, "UV", _V,
     (), ("uv_map",)),
    ("standard.transmission", "Transmission", "Standard", _STD, "TRANSMISSION", _C,
     (), ("pass_direct", "pass_indirect", "pass_color")),
    ("standard.environment", "Environment", "Standard", _STD, "ENVIRONMENT", _C,
     (), ()),
    ("standard.emit", "Emit", "Standard", _STD, "EMIT", _C,
     (), ()),
)


# ------------------------------------------------------------------------------------
#   采样分类
#
#   哪些 bake pass 的结果**依赖真实采样**（光线/遮蔽），哪些是确定值。
#   确定值那批烘 1 个采样就够，多烘只是白花时间 —— 用户那轮 128 张 2048²
#   跑了 1301 秒，绝大多数就是这个浪费（见 tests/mb_test_reported_issues.py 与
#   MATERIAL_BAKERY_DESIGN.md 第 21 节）。
# ------------------------------------------------------------------------------------

SAMPLED_PASSES = frozenset(("AO", "SHADOW", "COMBINED", "DIFFUSE", "GLOSSY",
                            "TRANSMISSION"))


# ------------------------------------------------------------------------------------
#   原生 pass 对照表（Bake Method = Native Passes 时用）
#
#   为什么需要它：默认方式靠**节点手术**把要烘的那股信号接到 Emission 上，
#   然后按 EMIT 烘。它对任何材质都成立、逐像素可验证，但有两个代价：
#     1. 材质结构怪（没接 Principled、没有 Material Output）时手术会失败；
#     2. 手术过程要改用户的材质（烘完还原），期间如果崩了就会留下脚手架。
#
#   原生 pass 不改材质，让 Cycles 直接算这一路 —— 但**不是每个通道都有对应 pass**：
#     Base Color -> DIFFUSE + 只取 COLOR（纯粹的反照率，不含光照）
#     Roughness  -> ROUGHNESS
#     Normal     -> NORMAL
#     Metallic   -> 没有对应 pass（GLOSSY 的 COLOR 是镜面颜色，不等于 Metallic）
#     Alpha / IOR / Subsurface… -> 同理，没有
#   所以这张表是"能用的就列出来"，表里没有的类型在 Native 模式下会**自动退回**
#   Emission 手术，并把这件事写进任务的 surgery 明细里（不闷着）。
# ------------------------------------------------------------------------------------

NATIVE_PASSES = {
    "shader.base_color": ("DIFFUSE", {"pass_filter": {"COLOR"}}),
    "shader.roughness": ("ROUGHNESS", {}),
    "shader.normal": ("NORMAL", {}),
}


def native_pass(key):
    """这个类型有没有原生 pass；没有就返回 None（调用方退回 Emission 手术）"""
    return NATIVE_PASSES.get(key)


# ------------------------------------------------------------------------------------
#   注册表
REGISTRY = {}
for _row in _TABLE:
    _bake_type = BakeType(
        key=_row[0], label=_row[1], group=_row[2], kind=_row[3],
        bake_pass=_row[4], channel=_row[5], sockets=_row[6], params=_row[7],
    )
    assert _bake_type.key not in REGISTRY, "重复的 key: {}".format(_bake_type.key)
    REGISTRY[_bake_type.key] = _bake_type

# 显示顺序 = 建表顺序，供 UI 直接用
ORDER = tuple(REGISTRY.keys())

# 分组顺序
GROUP_ORDER = (
    "Shader", "Subsurface", "Specular", "Coat",
    "Miscellaneous", "Standard",
)

# 建表时就做一致性检查：key 必须无多余空白（原版的地雷）
for _key in REGISTRY:
    assert _key == _key.strip(), "key 不能有首尾空格: {!r}".format(_key)
    assert " " not in _key, "key 不能含空格: {!r}".format(_key)
    assert "." in _key, "key 必须是点分结构: {!r}".format(_key)


# ------------------------------------------------------------------------------------
#   查询

def get(key):
    """按 key 取类型；不存在返回 None"""
    return REGISTRY.get(key)


def require(key):
    """按 key 取类型；不存在抛 KeyError（附带可用 key 提示）"""
    bake_type = REGISTRY.get(key)
    if bake_type is None:
        raise KeyError("未知的烘焙类型 {!r}。可用的有 {} 种，例如 {}".format(
            key, len(REGISTRY), ", ".join(ORDER[:4])))
    return bake_type


def iterator():
    """按显示顺序遍历"""
    return (REGISTRY[key] for key in ORDER)


def by_group():
    """{分组: [类型, ...]}，按 GROUP_ORDER 的顺序"""
    result = {}
    for group in GROUP_ORDER:
        entries = [REGISTRY[key] for key in ORDER if REGISTRY[key].group == group]
        if entries:
            result[group] = entries
    return result


def enum_items():
    """给 EnumProperty 用：(key, label, description) 三元组，含分组标题行

    ⚠ 必须是 3 元组或 5 元组 —— 4 元组会被 Blender 直接拒绝。
    分组标题行的 key 是空串，选中它没有意义，UI 侧靠 `GROUP_HEADS` 判断。

    按 GROUP_ORDER 顺序输出，每个分组只有一个标题 ——
    原版因为建表顺序把 'Shader' 拆成了两段（中间夹着 Subsurface/Specular/Coat），
    这里把同组的类型收拢到一起。
    """
    items = []
    for group, entries in by_group().items():
        items.append(('', group, "Group -- pick a map type below"))
        for bake_type in entries:
            items.append((bake_type.key, bake_type.label, bake_type.group))
    return items


def type_enum_items():
    """只有真正的类型，没有分组标题 —— 烘焙列表里用这个"""
    return [(bake_type.key, bake_type.label, bake_type.group) for bake_type in iterator()]


def channel_of(key):
    return require(key).channel


# ------------------------------------------------------------------------------------
#   旧名迁移
#
#   原版把类型名当字符串存进 .blend 和预设里，其中有：
#     - 4.0+ 的 46 个名字（含 'Displacement ' / 'Ambient Occlusion ' / 'Normal ' / 'Roughness ' 这些尾随空格）
#     - 9 个 Blender 3.x 的旧名（Sheen / Clearcoat / Subsurface / Specular / Emission 等）
#   全部映射到新的稳定 key，供预设迁移与对照测试使用。

ORIGINAL_LABEL_TO_KEY = {
    # ---- 4.0+ 名字（注意尾随空格的那四个）
    'Base Color': 'shader.base_color',
    'Metallic': 'shader.metallic',
    'Roughness': 'shader.roughness',
    'IOR': 'shader.ior',
    'Alpha': 'shader.alpha',
    'Normal': 'shader.normal',
    'Subsurface Weight': 'shader.subsurface_weight',
    'Subsurface Radius': 'shader.subsurface_radius',
    'Subsurface Scale': 'shader.subsurface_scale',
    'Subsurface IOR': 'shader.subsurface_ior',
    'Subsurface Anisotropy': 'shader.subsurface_anisotropy',
    'Specular IOR Level': 'shader.specular_ior_level',
    'Specular Tint': 'shader.specular_tint',
    'Anisotropic': 'shader.anisotropic',
    'Anisotropic Rotation': 'shader.anisotropic_rotation',
    'Tangent': 'shader.tangent',
    'Coat Weight': 'shader.coat_weight',
    'Coat Roughness': 'shader.coat_roughness',
    'Coat IOR': 'shader.coat_ior',
    'Coat Tint': 'shader.coat_tint',
    'Coat Normal': 'shader.coat_normal',
    'Transmission Weight': 'shader.transmission_weight',
    'Sheen Weight': 'shader.sheen_weight',
    'Sheen Roughness': 'shader.sheen_roughness',
    'Sheen Tint': 'shader.sheen_tint',
    'Emission Color': 'shader.emission_color',
    'Emission Strength': 'shader.emission_strength',
    'Channel Packing': 'misc.channel_packing',
    'Color Attribute': 'misc.color_attribute',
    'Ambient Occlusion': 'misc.ao',
    'Pointiness': 'misc.pointiness',
    'Displacement ': 'misc.displacement',        # 尾随空格 = Miscellaneous 的那个
    # 'Normals' / 'Displacement'（无尾随空格）原本映射到 multires 的两个类型。
    # multires 已删除，这两个旧名现在**故意不映射** ——
    # 旧预设里带它们的项会在迁移时被丢掉并记一条说明，而不是悄悄映射到别的类型上。
    'Combined': 'standard.combined',
    'Ambient Occlusion ': 'standard.ao',         # 尾随空格 = Standard pass
    'Normal ': 'standard.normal',                # 尾随空格
    'Roughness ': 'standard.roughness',          # 尾随空格
    'Glossy': 'standard.glossy',
    'Position': 'standard.position',
    'Shadow': 'standard.shadow',
    'Diffuse': 'standard.diffuse',
    'UV': 'standard.uv',
    'Transmission': 'standard.transmission',
    'Environment': 'standard.environment',
    'Emit': 'standard.emit',

    # ---- Blender 3.x 旧名（原版 bake_type_info 里多出来的 9 条）
    'Sheen': 'shader.sheen_weight',
    'Clearcoat': 'shader.coat_weight',
    'Clearcoat Roughness': 'shader.coat_roughness',
    'Clearcoat Normal': 'shader.coat_normal',
    'Subsurface': 'shader.subsurface_weight',
    'Subsurface Color': 'shader.subsurface_weight',
    'Transmission Roughness': 'shader.roughness',
    'Specular': 'shader.specular_ior_level',
    'Emission': 'shader.emission_color',
}


def from_original_label(label):
    """把原版存的类型名迁移成新 key；不认识返回 None"""
    if label in REGISTRY:
        return label
    return ORIGINAL_LABEL_TO_KEY.get(label)


# 反查：新 key -> 原版 4.0+ 名字（对照测试要把两边设置对齐时用）
KEY_TO_ORIGINAL_LABEL = {}
for _label, _key in ORIGINAL_LABEL_TO_KEY.items():
    if _key not in KEY_TO_ORIGINAL_LABEL:
        KEY_TO_ORIGINAL_LABEL[_key] = _label

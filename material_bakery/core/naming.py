# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   命名策略 —— 纯函数，无 bpy 依赖（除文件名清洗）
#
#   原版有 3 条名称结构（普通 / UDIM / UDIM 导出），每条的格式化与校验各写一遍，
#   而且前缀决策散在 1900 行的 modal 里。这里合成：
#       render_name()       模板 -> 文件名
#       select_prefix()     前缀决策（唯一一处）
#       resolve_collisions() 撞名消解
# ------------------------------------------------------------------------------------

import re


RESERVED_CHARS = '<>:"/\\|?*'
_RESERVED_RE = re.compile("[{}]".format(re.escape(RESERVED_CHARS)))

# 模板里可用的变量。顺序即 UI 里列出的顺序。
VARIABLES = ("prefix", "type", "bridge", "size", "suffix", "udim", "uvtile")

# 只有 UDIM 导出模板能用的变量
UDIM_ONLY_VARIABLES = ("udim", "uvtile")

# ⚠ UDIM 名字里**不再补那个尾随 x**（2026-09）。
#   原来模板是 `{...}{size}x`，用意是"标一下这是 UDIM 集"。但 {size} 现在
#   可能本身就是像素形式（64x64、1024x512），补上就成 `64x64x` —— 又丑又歧义。
#   而 `.1001` 这个瓦片后缀本来就足以说明是 UDIM 集，所以直接去掉：
#       Body-BaseColor-2k.1001.png        （旧文件是 ...-2kx.1001）
#   解析端仍然容忍尾部那个 x（见 core/imported.parse_size），旧文件照样能扫回来。
DEFAULT_TEMPLATE_UDIM = "{prefix}{bridge}{type}{bridge}{size}"
DEFAULT_TEMPLATE_UDIM_EXPORT = "{prefix}{bridge}{type}{bridge}{size}.{udim}"
DEFAULT_BRIDGE = "-"
# ⚠ 默认后缀是**空**的。原来是 "px"，配上分辨率就成了 "2048px" ——
#   用户的原话："2048 还是太长了，我想缩短点，1k、2k 这样"。
#   现在 {size} 本身就渲染成 "2k"，再加个 "px" 只是多两个字符。
DEFAULT_SUFFIX = ""

# ------------------------------------------------------------------------------------
#   命名方案（UI 上的下拉列表）
#
#   用户要的是"列表"而不是每次都手写模板。改一个方案 = 一次选好
#   template/bridge/suffix 三件套，省得他自己拼占位符拼错。
#
#   ⚠ 连接符**没有空格**，分辨率**小写 k**：用户指定的目标形态是
#       Common Parts 1-BaseColor-2k
#   （他原来存的是 `Common Parts 1BaseColor-2048`：前缀和类型粘在一起、
#     后缀是空的、连接符是 "-"，所以加载时会被迁移成新写法）。
# ------------------------------------------------------------------------------------

TEMPLATE_PRESETS = (
    ("collection_type_size", "Collection-Type-Size",
     "{prefix}{bridge}{type}{bridge}{size}", "-", ""),
    ("collection_type_size_flat", "Collection_Type_Size",
     "{prefix}_{type}_{size}", "_", ""),
    ("collection_type", "Collection-Type",
     "{prefix}{bridge}{type}", "-", ""),
    ("type_size", "Type-Size",
     "{type}{bridge}{size}", "-", ""),
    ("collection_type_suffix", "Collection-Type-Suffix",
     "{prefix}{bridge}{type}{bridge}{suffix}", "-", "baked"),
)

PRESET_DEFAULT = TEMPLATE_PRESETS[0][0]

# 默认模板 = 第一个方案：`Common Parts 1-BaseColor-2k`
DEFAULT_TEMPLATE = TEMPLATE_PRESETS[0][2]


def preset_items():
    """给 EnumProperty 用：(key, label, description)"""
    return tuple((key, label, "Template: {}".format(template))
                 for key, label, template, _bridge, _suffix in TEMPLATE_PRESETS)


def apply_preset(preset_key):
    """返回 (template, bridge, suffix)；未知 key 返回 None"""
    for key, _label, template, bridge, suffix in TEMPLATE_PRESETS:
        if key == preset_key:
            return template, bridge, suffix
    return None


def _as_dims(size_x, size_y=None):
    """(宽, 高) -> (int, int)。size_y 省略时按正方形处理（旧调用点与旧数据）。

    ⚠ 这里容忍 None / 0 / 字符串：从磁盘扫回来的老报告、老 manifest 里
      可能只有一条边长，宁可退化成正方形，也不要抛异常把整页打挂。
    """
    try:
        width = int(size_x or 0)
    except (TypeError, ValueError):
        width = 0
    if size_y is None:
        return width, width
    try:
        height = int(size_y or 0)
    except (TypeError, ValueError):
        height = 0
    return width, height


def size_token(size_x, size_y=None):
    """分辨率 -> 文件名里的 `{size}` token。

    规则（用户 2026-09 定的）:
        正方形 (w == h):
            是 1024 的倍数 -> '1k' / '2k' / '4k'        2048x2048 -> '2k'
            不是           -> 就写那个数               512x512  -> '512'
        非正方形:
            两边都是 1024 的倍数 -> 取**大的那条**写 k   2048x1024 -> '2k'
                                                       1024x4096 -> '4k'
            任一边不是           -> 两边都写完整像素     1024x512  -> '1024x512'

    ⚠ 三条细节都是有意的：
      1. 正方形只写一个数/一个 k。用户给的例子 `1024x512` 是**非正方形**，
         "写完整像素"说的是"两条边都写"，不是"给正方形也补一个 x" ——
         这样老用户的文件名（`-512`、`-2k`）一个都不变。
      2. 非正方形"取大的那条"是他明确要的，代价是 2048x4096 与 1024x4096 都会
         得到 '4k'：同一集合里同类型的两档会撞文件名。规则不改，Checklist 给警告
         （见 ui/ops.filename_rows）。
      3. 小写 k：用户指定的形态就是 `BaseColor-2k`。
    """
    width, height = _as_dims(size_x, size_y)
    if not width and not height:
        # 尺寸未知（从磁盘扫回来的老文件可能没写尺寸）—— 返回空串，
        # UI 上宁可什么都不显示，也不要显示 "0"
        return ""
    if not height:
        height = width
    if not width:
        width = height
    if width == height:
        return "{}k".format(width // 1024) if width % 1024 == 0 else str(width)
    if width % 1024 == 0 and height % 1024 == 0:
        return "{}k".format(max(width, height) // 1024)
    return "{}x{}".format(width, height)


def size_label(size_x, size_y=None):
    """给 UI 列表显示的分辨率。

    正方形用短 token（'2k'）；非正方形写全 `2048 × 1024` ——
    token 会把比例信息藏掉（2048x1024 印成 '2k'、1024x4096 印成 '4k'），
    而列表里必须一眼看出真实比例。
    """
    width, height = _as_dims(size_x, size_y)
    if not width and not height:
        return ""
    if width == height:
        return size_token(width, height)
    return "{} × {}".format(width, height)

# 前缀模式的语义值（供 UI 与文档引用，不再是散落的字符串）
PREFIX_MODE_AUTO = "auto"        # 有集合用集合名，没有集合用物体名
PREFIX_MODE_NONE = "none"        # 完全不要前缀（用户填了 "" 或 ''）


def sanitize_filename(text):
    """清掉操作系统保留字符"""
    return _RESERVED_RE.sub("", str(text)).strip()


def type_token(label):
    """类型名 -> 文件名 token：去掉所有空白

    'Base Color' -> 'BaseColor'
    """
    return re.sub(r"\s+", "", str(label))


def validate_template(template, allow_udim=False):
    """校验名称结构模板。

    返回 (ok: bool, error: str)。error 为空表示通过。
    """
    if template is None or template == "":
        return False, "Template is empty"

    # 找出所有占位符
    used = set()
    for match in re.finditer(r"\{([^{}]*)\}", template):
        used.add(match.group(1))

    unknown = sorted(used - set(VARIABLES))
    if unknown:
        return False, "Unknown key: {}".format(", ".join("{{{}}}".format(k) for k in unknown))

    # 落单的花括号
    stripped = re.sub(r"\{[^{}]*\}", "", template)
    if "{" in stripped or "}" in stripped:
        return False, "Unbalanced braces"

    if not allow_udim:
        bad = sorted(used & set(UDIM_ONLY_VARIABLES))
        if bad:
            return False, "Not allowed here: {}".format(
                ", ".join("{{{}}}".format(k) for k in bad))
    else:
        if not (used & set(UDIM_ONLY_VARIABLES)):
            return False, "Must contain {udim} or {uvtile}"

    if not used:
        return False, "Template has no variables"

    return True, ""


def render_name(template, variables):
    """套模板生成名字。

    variables: dict，缺的键用空串补齐（不抛异常 —— 引擎里不该因为少一个变量就崩）
    返回已清洗的文件名。
    """
    safe = {key: "" for key in VARIABLES}
    for key, value in (variables or {}).items():
        if key in safe:
            safe[key] = "" if value is None else str(value)
    try:
        text = template.format(**safe)
    except (KeyError, IndexError, ValueError):
        # 模板非法时退化成默认结构，不让引擎崩
        text = "{}{}{}{}".format(safe["prefix"], safe["type"], safe["bridge"], safe["size"])
    return sanitize_filename(text)


def select_prefix(prefix_setting, collection_name, object_name, auto_mode):
    """前缀决策 —— 全工程唯一一处。

    prefix_setting : 用户填的 Prefix
    collection_name: 物体所属集合名（没有则空串）
    object_name    : 物体名
    auto_mode      : 是否走自动命名（多物体同烘且未开共享贴图时为 True）

    规则（与原版兼容，另加集合名优先）：
        用户填了 "" 或 ''            -> 空前缀（完全不要前缀）
        设了 Prefix 且非 auto_mode   -> 用 Prefix
        否则                         -> 有集合用集合名，没有集合用物体名
    """
    if prefix_setting in ('""', "''"):
        return ""

    if prefix_setting and not auto_mode:
        return str(prefix_setting)

    return str(collection_name) if collection_name else str(object_name)


def disambiguate(base, object_name):
    """同批撞名时的消解：集合名后补物体名

    'Body' + 'Body_Cube' -> 'Body_Body_Cube'
    """
    return "{}_{}".format(base, object_name)


def resolve_collisions(entries):
    """给一批 (标识, 前缀) 消解撞名。

    entries: [(identifier, prefix), ...]
    返回 {identifier: 唯一前缀}

    只有真的撞名时才补 —— 正常单选或每个集合只烘一个物体时名字保持干净。
    """
    counts = {}
    for _identifier, prefix in entries:
        counts[prefix] = counts.get(prefix, 0) + 1

    resolved = {}
    for identifier, prefix in entries:
        if counts.get(prefix, 0) > 1:
            resolved[identifier] = disambiguate(prefix, identifier)
        else:
            resolved[identifier] = prefix
    return resolved


def unique_name(name, taken):
    """把一个名字塞进已占用的集合里，必要时加 .001 / .002

    用于图像数据块等"必须唯一但可以自动编号"的场合。

    taken 可以是 set（会被就地更新），也可以是 list/tuple —— 后者会转成 set，
    但**不会**写回调用方，所以批量调用时请自己传一个 set 并复用。
    """
    if not hasattr(taken, "add"):
        taken = set(taken)
    if name not in taken:
        taken.add(name)
        return name
    index = 1
    while True:
        candidate = "{}.{:03d}".format(name, index)
        if candidate not in taken:
            taken.add(candidate)
            return candidate
        index += 1


def preview(template, variables, limit=6):
    """给 UI 的实况预览：返回 (行列表, 总数)

    行里含扩展名 —— 用户在点烘焙之前就能看到最终文件名。
    """
    names = []
    total = 0
    for entry in variables:
        total += 1
        if len(names) < limit:
            names.append(render_name(template, entry))
    return names, total

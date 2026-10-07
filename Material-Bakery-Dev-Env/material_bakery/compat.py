# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   版本差异集中营
#
#   目标：全工程只有这一个文件出现 bpy.app.version。
#   socket 名不在这里做分支 —— 别名直接内联在 bake_types 的 sockets 元组里，
#   3.x 与 4.x 的名字都写上，运行时按顺序匹配，天然版本无关。
# ------------------------------------------------------------------------------------

import inspect
import bpy


VERSION = bpy.app.version
VERSION_STRING = bpy.app.version_string
IS_4X = VERSION >= (4, 0, 0)
IS_3X = not IS_4X

_SILENT = False


def log(*args):
    if not _SILENT:
        print("[Material Bakery]", *args)


# ------------------------------------------------------------------------------------
#   色彩空间
#   4.0 起 Blender 换了色彩空间命名体系，旧的 Filmic/Raw 等不再适用

COLOR_SPACES_4X = (
    "ACES2065-1", "ACEScg", "AgX Base Display P3", "AgX Base Rec.1886", "AgX Base Rec.2020",
    "AgX Base sRGB", "AgX Log", "Display P3", "Filmic Log", "Filmic sRGB",
    "Linear CIE-XYZ D65", "Linear CIE-XYZ E", "Linear DCI-P3 D65", "Linear FilmLight E-Gamut",
    "Linear Rec.2020", "Linear Rec.709", "Non-Color", "Rec.1886", "Rec.2020", "sRGB",
)

COLOR_SPACES_3X = (
    "XYZ", "sRGB", "Raw", "Non-Color", "Linear ACEScg", "Linear ACES", "Linear",
    "Filmic sRGB", "Filmic Log",
)

COLOR_SPACES = COLOR_SPACES_4X if IS_4X else COLOR_SPACES_3X


def safe_colorspace_name(name):
    """返回该版本真实存在的色彩空间名；不存在则回退。

    3.x / 4.x 之间色彩空间名差异很大，用户预设里存的可能是另一版本的。
    """
    if name in COLOR_SPACES:
        return name
    fallback = "Non-Color" if name not in ("sRGB", "Linear") else name
    if fallback not in COLOR_SPACES:
        fallback = COLOR_SPACES[0]
    log("colorspace {!r} 在 Blender {} 不存在，回退为 {!r}".format(
        name, VERSION_STRING, fallback))
    return fallback


# ------------------------------------------------------------------------------------
#   图像文件格式

FILE_FORMAT_EXTENSIONS = {
    'BMP': 'bmp',
    'IRIS': 'rgb',
    'PNG': 'png',
    'JPEG': 'jpg',
    'JPEG2000': 'jp2',
    'TARGA': 'tga',
    'TARGA_RAW': 'tga',
    'CINEON': 'cin',
    'DPX': 'dpx',
    'OPEN_EXR_MULTILAYER': 'exr',
    'OPEN_EXR': 'exr',
    'HDR': 'hdr',
    'TIFF': 'tif',
    'WEBP': 'webp',
}


def file_extension(file_format):
    return FILE_FORMAT_EXTENSIONS.get(file_format, 'png')


# ------------------------------------------------------------------------------------
#   Operator 参数过滤
#
#   Blender 各版本之间 bake / 导出操作符的参数会增删。
#   直接传不存在的参数会抛 TypeError，所以按签名取交集。

def supported_kwargs(operator, kwargs):
    """只保留该操作符当前版本确实接受的参数。

    operator: bpy.ops.xxx.yyy
    kwargs:   {参数名: 值}
    返回 (可用的 dict, 被丢弃的参数名列表)
    """
    try:
        rna = operator.get_rna_type()
        accepted = {p.identifier for p in rna.properties}
    except Exception:
        return dict(kwargs), []

    usable = {}
    dropped = []
    for key, value in kwargs.items():
        if key in accepted:
            usable[key] = value
        else:
            dropped.append(key)
    if dropped:
        log("操作符 {} 在当前版本不支持这些参数，已跳过: {}".format(
            getattr(operator, "idname", lambda: "?")(), ", ".join(sorted(dropped))))
    return usable, dropped


def call_operator(operator, **kwargs):
    """安全调用：自动丢弃当前版本不支持的参数"""
    usable, _dropped = supported_kwargs(operator, kwargs)
    return operator(**usable)


# ------------------------------------------------------------------------------------
#   运行时标记（测试用）

def set_silent(silent):
    """测试时静默日志"""
    global _SILENT
    _SILENT = bool(silent)

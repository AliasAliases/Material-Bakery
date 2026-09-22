# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   从磁盘上的贴图反推回"哪个集合、哪个通道、多大" —— 纯逻辑，无 bpy 依赖
#
#   为什么需要它：用户那次烘焙跑了 21.7 分钟、128 张贴图都写进磁盘了，但 Blender
#   卡住只能强杀 —— 结果那些图在文件夹里躺着，插件却说"这次会话什么都没烘"，
#   第 5 页连按钮都是灰的。用户的原话：
#       "直接扫一遍目录，要是有贴图就让第五页可以互动，我就能直接创建材质"
#
#   两条信息来源，**优先 manifest**：
#       1. `<集合目录>/_mbakery.json` —— 我们自己写的，精确（类型、尺寸、UV 层…）
#       2. 文件名反解 —— 兼容"以前烘的、没有 manifest"的老输出（这次就是这种）
#
#   ⚠ 文件名反解一定是**启发式**的：用户手改过名字就会解析不出来。
#     解析不出来不是错误，而是"报告里说清楚哪些没认出来"，让人自己判断。
# ------------------------------------------------------------------------------------

import json
import os
import re

from . import bake_types

MANIFEST_NAME = "_mbakery.json"
MANIFEST_VERSION = 1

# 支持的图片扩展名（跟导出格式一致）
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr", ".tga",
                    ".bmp", ".webp")

# 文件名里的分隔符：类型名、尺寸、后缀一般靠这些分开
_SEPARATORS = re.compile(r"[-_\s.]+")
_ALNUM = re.compile(r"[^a-z0-9]+")
# 尺寸 token：2k / 2048 / 1K / 2048px / 512x（UDIM 的 x 也接受）
# ⚠ "px" 要认：用户上一版烘出来的文件名就是 `...-2048.png`，
#   而更早的原版是带 px 的（`... - 2048px.png`），两种都得能救。
_SIZE_TOKEN = re.compile(r"^(\d+)([kK])?(px|x)?$")


def _normalize(text):
    """归一化：只留小写字母数字，"Base Color" 和 "BaseColor" 等价"""
    return _ALNUM.sub("", str(text).lower())


def type_tokens():
    """{归一化后的类型 token: type_key}

    ⚠ 按 token 长度**从长到短**排序（AmbientOcclusion 要排在 Normal 前面），
      否则 "AmbientOcclusion" 会被短 token 抢先匹配掉。
    """
    table = {}
    for key in bake_types.ORDER:
        bake_type = bake_types.get(key)
        token = _normalize(bake_type.label)
        if token:
            table.setdefault(token, key)
    return dict(sorted(table.items(), key=lambda item: -len(item[0])))


_SIZE_PAIR = re.compile(r"^(\d+)([kK])?[xX\u00d7](\d+)([kK])?(px)?$")


def _pixels(number, suffix):
    value = int(number)
    return value * 1024 if suffix else value


def parse_size(token):
    """文件名里的尺寸 -> (宽, 高)，认不出来返回 (0, 0)。

        2k            -> (2048, 2048)
        2048          -> (2048, 2048)
        1024x512      -> (1024, 512)      ← 2026-09 起非正方形会写成这样
        2kx1k         -> (2048, 1024)
        2kx / 2048x   -> (2048, 2048)     ← UDIM 导出名尾上那个 x

    ⚠ 必须返回**两条边**：`{size}` 已经不只是单个数字了（见 naming.size_token）。
    ⚠ 单值形式的尾巴 `x` 要留着 —— UDIM 的导出模板是 `...-2kx.{瓦片号}`，
      去掉它就再也认不出那种文件名了。
    """
    text = str(token or "").strip()
    pair = _SIZE_PAIR.match(text)
    if pair:
        return _pixels(pair.group(1), pair.group(2)), _pixels(pair.group(3), pair.group(4))
    match = _SIZE_TOKEN.match(text)
    if not match:
        return 0, 0
    value = _pixels(match.group(1), match.group(2))
    return value, value


def _trailing_alnum_length(text, count):
    """从右往左数 count 个字母数字，返回需要砍掉多少字符"""
    seen = 0
    for index in range(len(text) - 1, -1, -1):
        if text[index].isalnum():
            seen += 1
            if seen == count:
                return len(text) - index
    return len(text)


def _split_glued(part, tokens):
    """处理"前缀和类型粘在一起"的老写法：`Common Parts 1BaseColor`

    ⚠ 这种文件名不是假想：用户上一版烘出来的就是 `Common Parts 1BaseColor-2048`
      （模板是 {prefix}{type}{bridge}{size}，前缀和类型之间没有分隔符）。
      光按分隔符切词是不认识它的，必须再试一次"以某个类型名结尾"。
    """
    normalized = _normalize(part)
    if not normalized:
        return None, part
    for token, type_key in tokens.items():          # tokens 已按长度从长到短
        if len(token) >= len(normalized) or not normalized.endswith(token):
            continue
        cut = len(part) - _trailing_alnum_length(part, len(token))
        prefix = part[:cut].strip(" -_.")
        return type_key, prefix
    return None, part


def parse_filename(filename, tokens=None, fallback_size=0):
    """从文件名反解 (type_key, size_x, size_y, prefix)。认不出来返回 (None, x, y, prefix)

    例：`Common Parts 1-BaseColor-2k.png`
        -> ('shader.base_color', 2048, 'Common Parts 1')
        `BodyRoughness-2048.png`   -> ('shader.roughness', 2048, 'Body')
    """
    tokens = tokens if tokens is not None else type_tokens()
    stem = os.path.splitext(os.path.basename(str(filename)))[0]
    parts = [p for p in _SEPARATORS.split(stem) if p]
    if not parts:
        return None, fallback_size, fallback_size, ""

    # 1) 从右往左扒掉尺寸 token（可能没有）
    size_x = size_y = fallback_size
    while parts and parse_size(parts[-1])[0] and (len(parts) > 1):
        size_x, size_y = parse_size(parts.pop())

    # 2) 剩下的最后 1~3 段拼起来找类型（"Base Color" 这种带空格的会被拆成两段）
    type_key = None
    consumed = 0
    for width in (3, 2, 1):
        if len(parts) < width:
            continue
        candidate = _normalize("".join(parts[-width:]))
        if candidate in tokens:
            type_key = tokens[candidate]
            consumed = width
            break

    if type_key is None:
        # 3) 老写法：前缀和类型粘在同一段里（`BodyRoughness-2048`）
        glued_key, glued_prefix = _split_glued(parts[-1], tokens)
        if glued_key is not None:
            prefix = " ".join(parts[:-1] + ([glued_prefix] if glued_prefix else []))
            return glued_key, size_x, size_y, prefix.strip()
        # 类型认不出来时**不猜**，但把可能的前缀还给调用方
        return None, size_x, size_y, " ".join(parts)

    prefix = " ".join(parts[:len(parts) - consumed]) if consumed else " ".join(parts)
    return type_key, size_x, size_y, prefix.strip()


def is_image(path):
    return os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS


def read_manifest(folder):
    """读一个目录里的 manifest；没有或坏了都返回 None（不抛）"""
    path = os.path.join(folder, MANIFEST_NAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def write_manifest(folder, group, entries, uv_layer="", extra=None):
    """写一个目录的 manifest。返回路径（失败返回空串，不抛）

    entries: {type_key: {"file": 文件名, "size": int}}
    """
    payload = {
        "format": "material_bakery/textures",
        "version": MANIFEST_VERSION,
        "group": group,
        "uv_layer": uv_layer,
        "textures": entries,
    }
    if extra:
        payload.update(extra)
    path = os.path.join(folder, MANIFEST_NAME)
    try:
        os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
    except OSError:
        return ""
    return path


class FoundTexture:
    """扫描到的一张图"""

    __slots__ = ("group", "type_key", "size_x", "size_y", "path", "source")

    def __init__(self, group, type_key, size_x, size_y=None, path="",
                 source="filename"):
        self.group = group
        self.type_key = type_key
        self.size_x = int(size_x or 0)
        self.size_y = int(size_y or 0) or self.size_x
        self.path = path
        self.source = source          # 'manifest' 或 'filename'

    @property
    def filename(self):
        return os.path.basename(self.path)

    @property
    def type_label(self):
        bake_type = bake_types.get(self.type_key) if self.type_key else None
        return bake_type.label if bake_type else ""

    def __repr__(self):
        return "<{} {} {}x{} {}>".format(self.group, self.type_key, self.size_x,
                                        self.size_y,
                                      self.filename)


def scan_folder(root, tokens=None, use_manifest=True):
    """扫一个输出目录，返回 (found, unmatched)

    found     : [FoundTexture, ...]（type_key 认出来了的）
    unmatched : [(相对路径, 原因), ...]

    结构约定（就是插件自己写的结构）：
        <root>/<集合名>/xxx.png      -> 集合名 = 子目录名
        <root>/xxx.png               -> 集合名从文件名前缀反推
    """
    root = os.path.abspath(root)
    found = []
    unmatched = []
    if not os.path.isdir(root):
        return found, [(root, "folder does not exist")]

    tokens = tokens if tokens is not None else type_tokens()

    for current, dirs, files in os.walk(root):
        # 跳过隐藏目录（Blender 的 .blend1 之类不会在这里，但保险）
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        images = [f for f in sorted(files) if is_image(f)]
        if not images:
            continue

        relative = os.path.relpath(current, root)
        group = "" if relative == "." else relative.replace(os.sep, "/")
        by_file = {}
        manifest_group = ""
        if use_manifest:
            manifest = read_manifest(current)
            if manifest and isinstance(manifest.get("textures"), dict):
                # manifest 自己记着组名 —— 用户直接把某个集合的文件夹拿来扫时，
                # 目录名就是根（相对路径 "."），组名只能从这里来。
                manifest_group = str(manifest.get("group") or "")
                # ⚠ manifest 里是 {type_key: {file: 名字, ...}}，而查找要按**文件名**。
                #   第一版这里直接拿文件名去查那个 dict，于是永远查不到、
                #   白白退回文件名反解（测试里当场暴露：weird-name.png 没被认出来）。
                for key, value in manifest["textures"].items():
                    if not isinstance(value, dict):
                        continue
                    name = value.get("file")
                    if not name and bake_types.get(key) is None:
                        name = key                  # 也接受 {文件名: {...}} 的写法
                    if name:
                        by_file[name] = value

        for filename in images:
            path = os.path.join(current, filename)
            entry = by_file.get(filename)
            if isinstance(entry, dict):
                type_key = entry.get("type")
                # manifest 里现在记 size_x/size_y；旧 manifest 只有 size
                legacy = int(entry.get("size", 0) or 0)
                size_x = int(entry.get("size_x", legacy) or legacy)
                size_y = int(entry.get("size_y", legacy) or legacy)
                if bake_types.get(type_key) is not None:
                    # ⚠ 组名三级回退：目录名 > manifest 里记的组名 > 文件名前缀。
                    #   少了中间这一级时，"直接扫某个集合的文件夹"会得到空组名 ——
                    #   于是列表里是一堆没有集合的贴图，也找不到对应物体（测试里抓到过）。
                    fallback = manifest_group or parse_filename(filename, tokens)[3]
                    found.append(FoundTexture(group or fallback, type_key, size_x, size_y,
                                              path, "manifest"))
                    continue
            if by_file:
                # 有 manifest 但这条不在里面 —— 说明是别的东西丢进来的
                unmatched.append((os.path.join(group, filename) if group else filename,
                                  "not listed in {}".format(MANIFEST_NAME)))
                continue

            type_key, size_x, size_y, prefix = parse_filename(filename, tokens)
            if type_key is None:
                unmatched.append((os.path.join(group, filename) if group else filename,
                                  "cannot tell which map this is from the name"))
                continue
            name = group or prefix
            found.append(FoundTexture(name, type_key, size_x, size_y, path, "filename"))

    return found, unmatched


def group_found(found):
    """[{group, images: {type_key: FoundTexture}}] —— 按集合聚合，保持稳定顺序"""
    groups = []
    index = {}
    for item in found:
        entry = index.get(item.group)
        if entry is None:
            entry = {"group": item.group, "images": {}}
            index[item.group] = entry
            groups.append(entry)
        # 同名同类型取第一个（重复导出时会出现 .001）
        entry["images"].setdefault(item.type_key, item)
    return groups

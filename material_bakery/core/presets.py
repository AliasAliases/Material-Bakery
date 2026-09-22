# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   预设：一份配置存成一个人能读的 JSON
#
#   存在 Blender 的预设目录里（不是插件目录，也不是 .blend 里）：
#       <user scripts>/presets/material_bakery/<名字>.mbakery.json
#
#   设计上的两条硬规矩：
#       1. **只存设置，不存场景引用**。预设里绝不出现物体名、集合名、路径 ——
#          换一个工程还能用，否则"预设"就变成了"存档"。
#       2. **版本可迁移**。文件里带 version，旧的能升级；
#          连原版 Auto Bake 存下来的类型名（"Base Color" 这种人类可读名）
#          也认得，会翻译成现在的稳定 key。
# ------------------------------------------------------------------------------------

import json
import os
import re
import time

import bpy

from .. import compat
from . import bake_types


FORMAT_TAG = "material_bakery/preset"
PRESET_VERSION = 3
SUBDIR = "material_bakery"
EXTENSION = ".mbakery.json"

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class PresetError(Exception):
    pass


# ------------------------------------------------------------------------------------
#   目录

def _writable(path):
    """这个目录真的能写吗 —— 实际写一个探针文件试试

    ⚠ 不能只看 os.path.isdir()：目录**存在**不等于**可写**。
      实测踩过：某个环境里 Roaming 下的 presets 目录之前被建出来过，
      于是 isdir() 为真、直接返回，然后 save_preset 抛 PermissionError，
      错误信息指向那个路径，跟真正的原因（只读目录）差得远。
      os.access 在 Windows 上也不可靠，所以真写一次最稳。
    """
    probe = os.path.join(path, ".mbakery_write_probe")
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("")
        os.remove(probe)
        return True
    except OSError:
        return False


def presets_directory(create=True):
    """预设目录。保证返回的目录是**可写**的（create=True 时）。

    ⚠ 绝对不要用 bpy.utils.user_resource(..., create=True)：
      它内部直接 os.makedirs，目录建不出来就抛 PermissionError 把整条调用链打断。
       实测在受限环境（以及任何 Roaming 不可写的场合）会直接让 Blender 退出。
       改成自己建、失败就换地方，并且把"换到哪了"明确返回给调用方。
    """
    candidates = []
    scripts = bpy.utils.user_resource('SCRIPTS', path='presets', create=False)
    if scripts:
        candidates.append(os.path.join(scripts, SUBDIR))
    config = bpy.utils.user_resource('CONFIG', create=False)
    if config:
        candidates.append(os.path.join(config, "presets", SUBDIR))
    # 最后的兜底：系统临时目录。至少功能可用，不会整个插件挂掉。
    import tempfile
    candidates.append(os.path.join(tempfile.gettempdir(), "material_bakery_presets"))

    errors = []
    for path in candidates:
        if not os.path.isdir(path):
            if not create:
                continue
            try:
                os.makedirs(path)
            except OSError as exc:
                errors.append("{}: {}".format(path, exc))
                continue
        if not create:
            return path
        if _writable(path):
            return path
        errors.append("{}: not writable".format(path))

    if not create:
        return ""
    raise PresetError("No writable folder for presets. " + "; ".join(errors))


def presets_directory_readonly():
    """读预设时用：**必须和写入用的是同一个目录**。

    ⚠ 这里踩过一个坑：读取时用 create=False 找"第一个存在的目录"，
      写入时（create=True）因为那个目录不可写而退到了临时目录 ——
      结果**存进去了却列不出来**，看起来像"保存失败"。
      所以读取也走同一套解析（顺便把目录建出来），保证两边永远一致。
    """
    try:
        return presets_directory(create=True)
    except PresetError:
        return ""


def validate_name(name):
    """预设名 -> (是否合法, 说明)"""
    name = (name or "").strip()
    if not name:
        return False, "Name is empty"
    if len(name) > 64:
        return False, "Name is too long (max 64)"
    if _INVALID.search(name):
        return False, 'Name cannot contain any of  < > : " / \\ | ? *'
    if name.startswith("."):
        return False, "Name cannot start with a dot"
    return True, ""


def preset_path(name):
    return os.path.join(presets_directory(), name + EXTENSION)


def list_presets():
    """所有可用预设名，按字母序"""
    directory = presets_directory_readonly()
    if not directory or not os.path.isdir(directory):
        return []
    names = []
    for entry in os.listdir(directory):
        if entry.endswith(EXTENSION):
            names.append(entry[:-len(EXTENSION)])
    return sorted(names)


def exists(name):
    return os.path.isfile(preset_path(name))


# ------------------------------------------------------------------------------------
#   迁移

def migrate(data):
    """把任何历史版本的数据升到当前版本。返回 (数据, 迁移说明列表)。"""
    notes = []
    data = dict(data)

    version = data.get("version")
    if not isinstance(version, int):
        version = 0
        notes.append("no version field — treated as v0")

    if version > PRESET_VERSION:
        raise PresetError(
            "This preset was written by a newer Material Bakery (v{} > v{})".format(
                version, PRESET_VERSION))

    if version < 1:
        data = _migrate_v0_to_v1(data, notes)
        version = 1

    if version < 2:
        data = _migrate_v1_to_v2(data, notes)
        version = 2

    if version < 3:
        data = _migrate_v2_to_v3(data, notes)
        version = 3

    data["version"] = PRESET_VERSION
    return data, notes


def _migrate_v0_to_v1(data, notes):
    """v0 -> v1

    v0 是"还没有版本号"的时期，特征：
        - maps 里的类型是显示名（'Base Color'），甚至带原版的尾随空格
        - 没有 antialias / udim 字段
    """
    maps = []
    for entry in data.get("maps", []) or []:
        if not isinstance(entry, dict):
            continue
        type_key = entry.get("type") or entry.get("type_key") or ""
        if type_key and "." not in type_key:
            resolved = bake_types.from_original_label(type_key)
            if resolved:
                notes.append("map '{}' -> '{}'".format(type_key, resolved))
                type_key = resolved
            else:
                notes.append("dropped unknown map '{}'".format(type_key))
                continue
        if bake_types.get(type_key) is None:
            notes.append("dropped unknown map '{}'".format(type_key))
            continue
        maps.append({
            "type": type_key,
            "size": int(entry.get("size", 2048)),
            "enabled": bool(entry.get("enabled", True)),
            "options": dict(entry.get("options", {}) or {}),
        })
    data["maps"] = maps
    data.setdefault("antialias", 'OFF')
    data.setdefault("aa_scale", 2)
    data.setdefault("udim", False)
    data.setdefault("adaptive_margin", False)
    return data


def _migrate_v1_to_v2(data, notes):
    """v1 -> v2：命名与采样方式变了

    v1 的默认命名是 "{prefix}{type}{bridge}{size}{suffix}"，配 suffix="px"，
    得到 "BodyBaseColor - 2048px"。用户嫌长，现在 {size} 自己就是 "2K"，
    所以把旧的 "px" 后缀摘掉；不摘的话会变成 "2Kpx"。

    同时补上新的两个字段：烘焙方式（Emission / Native）和采样策略。
    """
    suffix = data.get("suffix")
    if suffix == "px":
        data["suffix"] = ""
        notes.append("naming suffix 'px' removed — {size} now renders as 2K")
    data.setdefault("samples_mode", 'ONE')
    data.setdefault("bake_method", 'EMISSION')
    return data


# v2 时期的默认结构（前缀和类型粘在一起：`Common Parts 1BaseColor-2048`）
_V2_DEFAULT_TEMPLATES = (
    "{prefix}{type}{bridge}{size}{suffix}",
    "{prefix}{type}{bridge}{size}",
)

# 老连接符 -> 新连接符（用户要的是不带空格的 `-`）
_V2_BRIDGES = {" - ": "-", " ": "-", "": "-", "-": "-", "_": "_"}


def _migrate_v2_to_v3(data, notes):
    """v2 -> v3：命名改成 `集合名-类型-2k`（不带空格、小写 k）

    用户原话："我想要的是：BaseColor-2k，不要加空格"。
    v2 的默认模板是 "{prefix}{type}{bridge}{size}{suffix}" —— 前缀和类型之间
    没有分隔符，拼出来是 `Common Parts 1BaseColor-2048`。这里把**只匹配旧默认
    结构**的模板换成新默认（`{prefix}{bridge}{type}{bridge}{size}`），
    并把带空格的连接符收成 `-`。

    ⚠ 只动"看起来就是旧默认"的那几种写法：用户自己拼的怪模板不动，
       否则就是擅自改别人的命名规则。
    """
    template = data.get("template")
    if template in _V2_DEFAULT_TEMPLATES:
        data["template"] = "{prefix}{bridge}{type}{bridge}{size}"
        notes.append("naming template updated to '{prefix}{bridge}{type}{bridge}{size}'")
    bridge = data.get("bridge")
    if bridge in _V2_BRIDGES:
        data["bridge"] = _V2_BRIDGES[bridge]
        if bridge != data["bridge"]:
            notes.append("naming bridge '{}' -> '{}'".format(bridge, data["bridge"]))
    # 采样分成了两组：旧值 ONE 归到新方案的 Low
    if data.get("samples_mode") == 'ONE':
        data["samples_mode"] = 'LOW'
    data.setdefault("samples_mode", 'LOW')
    data.setdefault("samples_custom", 1)
    data.setdefault("sampled_maps_mode", 'MEDIUM')
    data.setdefault("sampled_maps_custom", 16)
    data.setdefault("hide_unrelated", False)
    return data


# ------------------------------------------------------------------------------------
#   读写

def save_preset(name, data):
    """写一个预设。返回文件路径。"""
    ok, message = validate_name(name)
    if not ok:
        raise PresetError(message)

    payload = dict(data)
    payload["format"] = FORMAT_TAG
    payload["version"] = PRESET_VERSION
    payload["name"] = name.strip()
    payload["blender"] = bpy.app.version_string
    payload["created"] = time.strftime("%Y-%m-%d %H:%M:%S")

    path = preset_path(name.strip())
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
    except OSError as exc:
        raise PresetError("Could not write {}: {}".format(path, exc))
    compat.log("预设已保存: {}".format(path))
    return path


def load_preset(name):
    """读一个预设。返回 (数据, 迁移说明)。"""
    path = preset_path(name)
    if not os.path.isfile(path):
        raise PresetError("No such preset: {}".format(name))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise PresetError("Could not read {}: {}".format(path, exc))
    if not isinstance(data, dict):
        raise PresetError("Preset is not a JSON object")
    tag = data.get("format")
    if tag and tag != FORMAT_TAG:
        raise PresetError("Not a Material Bakery preset (format={!r})".format(tag))
    return migrate(data)


def delete_preset(name):
    path = preset_path(name)
    if not os.path.isfile(path):
        return False
    try:
        os.remove(path)
    except OSError as exc:
        raise PresetError("Could not delete {}: {}".format(path, exc))
    return True


def describe(name):
    """列表里显示用的一行说明"""
    try:
        data, _notes = load_preset(name)
    except PresetError as exc:
        return "<broken: {}>".format(exc)
    maps = len(data.get("maps", []))
    size = 0
    sizes = [entry.get("size", 0) for entry in data.get("maps", [])]
    if sizes:
        size = max(sizes)
    return "{} map(s), up to {}px, {}".format(maps, size, data.get("file_format", "?"))


# ------------------------------------------------------------------------------------
#   数据形状（纯字典 —— core 不认识 PropertyGroup）

FIELDS = (
    "target_mode", "prefix", "bridge", "suffix", "template", "file_format",
    "use_subfolders", "overwrite", "fake_user", "pack_into_blend", "samples_mode",
    "samples_custom", "sampled_maps_mode", "sampled_maps_custom",
    "margin", "prepare_uv", "material_style", "use_baked_uv", "antialias", "aa_scale",
    "adaptive_margin", "udim", "bake_method", "auto_deliver",
    # Hide Unrelated 是"怎么烘"的一部分（而且会改变 AO/阴影的语义），
    # 所以它属于预设；输出路径、集合勾选那些才是"这个工程的事"。
    "hide_unrelated", "unwrap_target",
)


def capture(maps, **fields):
    """组装一份预设数据。

    ⚠ 只收 FIELDS 里列的东西。输出路径、集合勾选、以及任何物体/集合名
      都不进去 —— 那些是"这个工程的事"，不是"烘焙方法的事"。
    """
    data = {"maps": []}
    for entry in maps:
        data["maps"].append({
            "type": entry["type"],
            "size": int(entry["size"]),
            "enabled": bool(entry.get("enabled", True)),
            "options": dict(entry.get("options", {}) or {}),
        })
    for key in FIELDS:
        if key in fields:
            data[key] = fields[key]
    return data


def unknown_fields(data):
    """预设里有没有我们不认识的字段（提示用户版本差异，不是错误）"""
    known = set(FIELDS) | {"format", "version", "name", "blender", "created", "maps"}
    return sorted(set(data) - known)

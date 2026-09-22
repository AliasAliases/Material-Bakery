# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   预设与报告的 operator
#
#   预设的纯逻辑在 core/presets.py；这里负责 PropertyGroup <-> 字典 的搬运。
# ------------------------------------------------------------------------------------

import os

import bpy

from ..core import presets as presets_mod
from ..engine import report as report_mod
from . import properties, session


def _settings(context):
    return context.scene.mbakery


def _log(context, level, message):
    session.Session.get().add_log(level, message)


def _redraw():
    session.Session.tag_redraw()


def _preset_items(self, context):
    names = presets_mod.list_presets()
    if not names:
        return [('', "(no presets)", "")]
    return [(name, name, presets_mod.describe(name)) for name in names]


# ------------------------------------------------------------------------------------
#   PropertyGroup -> 字典

def capture(settings):
    """把当前设置收成一份预设数据。

    ⚠ 输出路径、集合勾选、物体/集合名**不进预设** —— 那是"这个工程的事"。
    """
    maps = [{"type": item.type_key, "size": item.size, "enabled": item.enabled,
             "options": _map_options(item)}
            for item in settings.maps]
    return presets_mod.capture(
        maps,
        target_mode=settings.target_mode,
        prefix=settings.prefix,
        bridge=settings.bridge,
        suffix=settings.suffix,
        template=settings.template,
        file_format=settings.file_format,
        use_subfolders=settings.use_subfolders,
        overwrite=settings.overwrite,
        fake_user=settings.fake_user,
        pack_into_blend=settings.pack_into_blend,
        samples_mode=settings.samples_mode,
        margin=settings.margin,
        adaptive_margin=settings.adaptive_margin,
        antialias=settings.antialias,
        aa_scale=settings.aa_scale,
        udim=settings.udim,
        prepare_uv=settings.prepare_uv,
        material_style=settings.material_style,
        use_baked_uv=settings.use_baked_uv,
    )


def _map_options(item):
    """烘焙项上的专属参数（目前只有通道打包用得上）"""
    from ..engine import surgery
    from ..core import bake_types
    bake_type = bake_types.get(item.type_key)
    if bake_type is None:
        return {}
    if bake_type.kind is bake_types.BakeKind.CHANNEL_PACK:
        return {"channel_r": surgery.DEFAULT_CHANNEL_SOURCES[0],
                "channel_g": surgery.DEFAULT_CHANNEL_SOURCES[1],
                "channel_b": surgery.DEFAULT_CHANNEL_SOURCES[2]}
    return {}


def apply_to(settings, data):
    """把预设数据写回设置。返回一句说明。"""
    from ..core import bake_types

    settings.maps.clear()
    dropped = 0
    for entry in data.get("maps", []):
        type_key = entry.get("type", "")
        if bake_types.get(type_key) is None:
            dropped += 1
            continue
        item = settings.maps.add()
        item.type_key = type_key
        item.size = int(entry.get("size", 2048))
        item.enabled = bool(entry.get("enabled", True))
        # options 目前不落 PropertyGroup（通道打包用默认 R/G/B），
        # 放进 item 的自定义属性里以便后续读取
        options = entry.get("options") or {}
        if options:
            item["mbakery_options"] = sorted(options.items())
    settings.map_index = 0

    for key in presets_mod.FIELDS:
        if key in ("target_mode", "udim"):
            continue                       # 目标模式和 UDIM 属于"这次要烘什么"
        if key in data and hasattr(settings, key):
            try:
                setattr(settings, key, data[key])
            except (TypeError, ValueError):
                pass

    message = "Loaded {} map(s)".format(len(settings.maps))
    if dropped:
        message += ", dropped {} unknown".format(dropped)
    return message


# ------------------------------------------------------------------------------------
#   预设 operator

class MBAKERY_OT_LoadPreset(bpy.types.Operator):
    bl_idname = "mbakery.load_preset"
    bl_label = "Load Preset"
    bl_description = "Apply a saved preset"
    bl_options = {'INTERNAL'}

    name: bpy.props.EnumProperty(name="Preset", items=_preset_items)

    def invoke(self, context, event):
        if not presets_mod.list_presets():
            _error(self, "No presets saved yet")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        self.layout.prop(self, "name", text="")

    def execute(self, context):
        settings = _settings(context)
        if not self.name:
            _error(self, "Pick a preset")
            return {'CANCELLED'}
        try:
            data, notes = presets_mod.load_preset(self.name)
        except presets_mod.PresetError as exc:
            _error(self, str(exc))
            return {'CANCELLED'}

        message = apply_to(settings, data)
        settings.preset_name = self.name
        for note in notes:
            _log(context, "PRESET", "migrated: {}".format(note))
        unknown = presets_mod.unknown_fields(data)
        if unknown:
            _log(context, "PRESET", "ignored unknown field(s): {}".format(
                ", ".join(unknown)))
        _log(context, "PRESET", "{} <- {}".format(message, self.name))
        self.report({'INFO'}, message)
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_SavePreset(bpy.types.Operator):
    bl_idname = "mbakery.save_preset"
    bl_label = "Save Preset"
    bl_description = "Save the current settings as a preset (settings only, no paths)"
    bl_options = {'INTERNAL'}

    name: bpy.props.StringProperty(name="Name", default="")

    def invoke(self, context, event):
        settings = _settings(context)
        if not self.name:
            self.name = settings.preset_name or "My Preset"
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "name", text="Name")
        settings = _settings(context)
        layout.label(text="{} bake item(s), {} format".format(
            len(settings.maps), settings.file_format), icon='INFO')
        layout.label(text="Output folder and collection ticks are not saved",
                     icon='INFO')

    def execute(self, context):
        settings = _settings(context)
        ok, message = presets_mod.validate_name(self.name)
        if not ok:
            _error(self, message)
            return {'CANCELLED'}
        try:
            path = presets_mod.save_preset(self.name, capture(settings))
        except presets_mod.PresetError as exc:
            _error(self, str(exc))
            return {'CANCELLED'}
        settings.preset_name = self.name.strip()
        _log(context, "PRESET", "saved {}".format(path))
        self.report({'INFO'}, "Saved preset '{}'".format(settings.preset_name))
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_DeletePreset(bpy.types.Operator):
    bl_idname = "mbakery.delete_preset"
    bl_label = "Delete Preset"
    bl_description = "Delete a saved preset"
    bl_options = {'INTERNAL'}

    name: bpy.props.EnumProperty(name="Preset", items=_preset_items)

    def invoke(self, context, event):
        settings = _settings(context)
        if not self.name:
            self.name = settings.preset_name
        names = presets_mod.list_presets()
        if not names:
            _error(self, "No presets saved yet")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        self.layout.prop(self, "name", text="")
        self.layout.label(text="This cannot be undone", icon='ERROR')

    def execute(self, context):
        if not self.name:
            _error(self, "Pick a preset")
            return {'CANCELLED'}
        try:
            removed = presets_mod.delete_preset(self.name)
        except presets_mod.PresetError as exc:
            _error(self, str(exc))
            return {'CANCELLED'}
        if not removed:
            _error(self, "No such preset")
            return {'CANCELLED'}
        _settings(context).preset_name = ""
        _log(context, "PRESET", "deleted {}".format(self.name))
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_OpenPresetsFolder(bpy.types.Operator):
    bl_idname = "mbakery.open_presets_folder"
    bl_label = "Open Presets Folder"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        try:
            path = presets_mod.presets_directory()
        except presets_mod.PresetError as exc:
            _error(self, str(exc))
            return {'CANCELLED'}
        bpy.ops.wm.path_open(filepath=path)
        return {'FINISHED'}


# ------------------------------------------------------------------------------------
#   报告存取

class MBAKERY_OT_SaveReport(bpy.types.Operator):
    bl_idname = "mbakery.save_report"
    bl_label = "Save Report"
    bl_description = "Write this run's report next to the textures as JSON"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        settings = _settings(context)
        active = session.Session.get()
        if active.report is None:
            _error(self, "Nothing has been baked yet")
            return {'CANCELLED'}
        directory = bpy.path.abspath(settings.output_dir) if settings.output_dir else ""
        if not directory:
            _error(self, "Set an output folder first")
            return {'CANCELLED'}
        if not os.path.isdir(directory):
            os.makedirs(directory)
        path = os.path.join(directory, "material_bakery_report.json")
        try:
            active.report.save_to(path)
        except OSError as exc:
            _error(self, str(exc))
            return {'CANCELLED'}
        _log(context, "REPORT", "saved {}".format(path))
        self.report({'INFO'}, "Report saved to {}".format(os.path.basename(path)))
        _redraw()
        return {'FINISHED'}


CLASSES = (
    MBAKERY_OT_LoadPreset,
    MBAKERY_OT_SavePreset,
    MBAKERY_OT_DeletePreset,
    MBAKERY_OT_OpenPresetsFolder,
    MBAKERY_OT_SaveReport,
)

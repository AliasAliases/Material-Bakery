# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   从磁盘救回来 —— 第 5 页的 operator
#
#   背景（用户原话）：
#       "我还有个方案，毕竟都烘焙完了，直接扫一遍目录，要是有贴图就让第五页可以互动，
#        我就能直接创建材质然后把贴图连上去了"
#   他那一轮跑了 21.7 分钟、128 张图都写盘了，然后 Blender 无响应只能强杀 ——
#   结果这些图在文件夹里躺着，插件却说"这次会话什么都没烘"，第 5 页全是灰的。
#
#   这个模块只做三件事：
#       Scan Output Folder   扫目录 -> 贴图列表 -> 第 5 页解禁（并自动建材质、挂槽）
#       Choose Folder...     换一个目录扫（贴图被挪过地方时用）
#       Clear Imported       把扫进来的结果清掉
#
#   ⚠ "Repack UVs To Match" 已经删掉：UV 打包这条路 2026-09 整个删了，
#     新烘的贴图永远用物体自己的 UV，不存在"对不上"这回事。
#     旧版烘出来的打包布局贴图没法在插件里复原，只能重烘。
# ------------------------------------------------------------------------------------

import os

import bpy

from .. import compat
from . import properties, session


def _settings(context):
    return context.scene.mbakery


def _redraw():
    session.Session.tag_redraw()


def _log(context, level, message):
    session.Session.get().add_log(level, message)
    compat.log("[{}] {}".format(level, message))


class MBAKERY_OT_ScanOutput(bpy.types.Operator):
    bl_idname = "mbakery.scan_output"
    bl_label = "Scan Output Folder"
    bl_description = ("Look in the output folder for textures that are already baked and "
                      "bring them back into the wizard, so you can build materials "
                      "without baking again")
    bl_options = {'INTERNAL'}

    folder: bpy.props.StringProperty(default="", subtype='DIR_PATH')

    def execute(self, context):
        settings = _settings(context)
        active = session.Session.get()
        total, groups, unmatched = active.scan_output(settings, self.folder)
        if not total:
            _error(self, "No baked textures found there")
            _redraw()
            return {'CANCELLED'}
        state, _detail = active.uv_state()
        message = "{} texture(s) in {} collection(s)".format(total, groups)
        if unmatched:
            message += ", {} not recognised".format(unmatched)
        if state == "missing":
            message += " — these came from an older packed bake, they will not line up"
        self.report({'INFO'}, message)
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_ChooseOutputFolder(bpy.types.Operator):
    bl_idname = "mbakery.choose_texture_folder"
    bl_label = "Pick a Folder to Scan"
    bl_options = {'INTERNAL'}

    directory: bpy.props.StringProperty(subtype='DIR_PATH')
    filter_folder: bpy.props.BoolProperty(default=True, options={'HIDDEN'})

    def invoke(self, context, event):
        # 背景模式 / 测试里没有窗口，直接拿 directory 走 execute
        if getattr(context, "window", None) is None or bpy.app.background:
            return self.execute(context)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if not self.directory:
            _error(self, "No folder picked")
            return {'CANCELLED'}
        settings = _settings(context)
        active = session.Session.get()
        total, groups, unmatched = active.scan_output(settings, self.directory)
        if not total:
            _error(self, "No baked textures found in that folder")
            _redraw()
            return {'CANCELLED'}
        self.report({'INFO'}, "{} texture(s) in {} collection(s)".format(total, groups))
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_ClearImported(bpy.types.Operator):
    bl_idname = "mbakery.clear_imported"
    bl_label = "Clear Imported Textures"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        active = session.Session.get()
        active.imported_by_group = {}
        active.import_source = ""
        active.import_unmatched = []
        _settings(context).textures.clear()
        _log(context, "IMPORT", "Cleared imported textures")
        _redraw()
        return {'FINISHED'}


class MBAKERY_OT_OpenLogFile(bpy.types.Operator):
    bl_idname = "mbakery.open_log_file"
    bl_label = "Open Session Log"
    bl_description = ("Open the log file on disk — it keeps the last run's log even if "
                      "Blender was killed")
    bl_options = {'INTERNAL'}

    def execute(self, context):
        path = session.log_path()
        if not os.path.isfile(path):
            _error(self, "No log file yet: {}".format(path))
            return {'CANCELLED'}
        bpy.ops.wm.path_open(filepath=path)
        return {'FINISHED'}


CLASSES = (
    MBAKERY_OT_ScanOutput,
    MBAKERY_OT_ChooseOutputFolder,
    MBAKERY_OT_ClearImported,
    MBAKERY_OT_OpenLogFile,
)

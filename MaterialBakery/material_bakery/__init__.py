# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 2
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   DeepSeek's Material Bakery
#
#   从 Auto Bake 的行为里提取功能，用干净、可测试、可扩展的架构重新实现。
#   设计文档见 MATERIAL_BAKERY_DESIGN.md
#
#   分层规则（硬性）：
#     ui/       只读状态、只调 operator，不写业务逻辑
#     engine/   编排：什么时候做什么
#     core/     纯逻辑与数据，不知道 modal、不知道 UI
#     deliver/  烘焙之后的动作，可独立调用
#   core/ 里不允许查找 bpy.context.scene.mbakery_*，依赖一律显式传入。
# ------------------------------------------------------------------------------------

bl_info = {
    "name": "DeepSeek's Material Bakery",
    "description": "Step-by-step texture baking: pick targets and maps, bake, then deliver "
                   "(export textures, build materials, clean up material slots)",
    "author": "DeepSeek",
    "version": (1, 0, 3),
    # 声明 4.2 起；实测通过的是 3.6.23 / 4.5.13 / 5.2.1
    # （冒烟测试 tests/mb_smoke.py 在这三个版本上跑过同一套核心路径）
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Material Bakery",
    "category": "Baking",
}

# 打包时间戳 —— 第 1 页会显示它。
#
# ⚠ 为什么需要：用户装 zip 之后**不会**自动生效（Blender 把旧模块留在内存里），
#   我们为此浪费过一整轮对话去查"为什么修复没效果"。有了这个戳，
#   "你装的是哪一版"一眼就能看出来（每次打包我都会改这里）。
__build__ = "2026-09-21 04:08"

import bpy

from . import compat
from . import deliver
from . import engine
from . import ui
from .core import bake_types, naming, materials


_registered = False


def register():
    global _registered
    if _registered:
        return
    compat.log("Material Bakery {} starting (Blender {})".format(
        ".".join(str(v) for v in bl_info["version"]), bpy.app.version_string))
    bpy.types.Scene.mbakery_version = bpy.props.StringProperty(
        name="Material Bakery Version",
        default=".".join(str(v) for v in bl_info["version"]),
    )
    ui.register()
    _registered = True


def unregister():
    global _registered
    if not _registered:
        return
    ui.unregister()
    if hasattr(bpy.types.Scene, "mbakery_version"):
        del bpy.types.Scene.mbakery_version
    _registered = False


if __name__ == "__main__":
    register()

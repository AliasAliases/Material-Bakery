# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   engine —— 编排层
#
#       backend.py    怎么烘（Cycles / Null）
#       job.py        什么时候烘（可中断的状态机）
#       report.py     烘完了记什么账
#       uv_prep.py    展 UV / 重叠预检
#
#   这一层可以碰 bpy.context，但不做业务决策 —— 决策全在 core。
# ------------------------------------------------------------------------------------

from . import backend, job, report, uv_overlap, uv_pack, uv_prep

__all__ = ["backend", "job", "report", "uv_overlap", "uv_pack", "uv_prep"]

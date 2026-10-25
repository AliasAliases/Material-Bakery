# ##### BEGIN GPL LICENSE BLOCK #####
#  GPL v2+ — see LICENSE.txt
# ##### END GPL LICENSE BLOCK #####

# ------------------------------------------------------------------------------------
#   ui —— 只读状态、只调 operator
#
#       properties.py  存在 .blend 里的设置（运行期间绝不许写）
#       session.py     活动中的 BakeJob + timer 驱动 + 日志
#       ops.py         向导导航、烘焙列表编辑、烘焙控制、交付动作
#       panels.py      N 面板的四页绘制
# ------------------------------------------------------------------------------------

import time

import bpy

from .. import compat
from ..core import phases
from . import ops, ops_presets, ops_rescue, panels, properties, session

CLASSES = (properties.CLASSES + ops.CLASSES + ops_presets.CLASSES
            + ops_rescue.CLASSES + panels.CLASSES)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)

    bpy.types.Scene.mbakery = bpy.props.PointerProperty(type=properties.MBAKERY_Settings)
    # ⚠ 会话（它握着正在跑的 job，job 又握着 bpy Image / Object 的 Python 引用）
    #   必须在**数据被释放之前**丢掉，也就是 load_pre。
    #   挂在 load_post 上已经太晚了：那时旧的 data 全被释放，session 里存的就是
    #   一堆悬空 bpy_struct，任何一次属性访问都是 use-after-free ——
    #   表现为 Blender 直接退出、连 Python traceback 都没有。
    handlers = _handler_pair() + _undo_handlers() + _save_handlers()
    for event, callback in handlers:
        getattr(bpy.app.handlers, event).append(callback)
    compat.log("UI registered ({} classes)".format(len(CLASSES)))


def _handler_pair():
    pairs = []
    if hasattr(bpy.app.handlers, "load_pre"):
        pairs.append(("load_pre", _drop_session))
    else:                                   # 老版本没有 load_pre，只能退而求其次
        pairs.append(("load_post", _drop_session))
    pairs.append(("load_post", _reset_runtime))
    return pairs


def _undo_handlers():
    """撤销/重做之后要把会话里的死引用清掉

    ⚠ 用户实测：在"Remove Baked Slot"之后按 Ctrl+Z，面板下面几块（创建最终物体、
      报告）全都消失 —— 因为撤销删掉了我们刚建的材质数据块，而会话里握着的是
      已经释放的 bpy_struct，draw() 一碰就抛，整页剩下部分不再绘制。
       现在撤销/重做之后先 revalidate()，引用失效的直接丢掉。
    """
    handlers = []
    for event in ("undo_post", "redo_post"):
        if hasattr(bpy.app.handlers, event):
            handlers.append((event, _revalidate_session))
    return handlers


def unregister():
    for event, callback in _handler_pair() + _undo_handlers() + _save_handlers():
        handlers = getattr(bpy.app.handlers, event)
        if callback in handlers:
            handlers.remove(callback)
    session.Session.forget()
    if hasattr(bpy.types.Scene, "mbakery"):
        del bpy.types.Scene.mbakery
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except (RuntimeError, ValueError) as exc:
            compat.log("注销 {} 失败: {}".format(cls.__name__, exc))


# ------------------------------------------------------------------------------------
#   写文件时的留痕
#
#   用户那次"8/8 烘完之后 Blender 729 秒没响应、内存还是平的"最像**磁盘写入**
#   （他的 .blend 打包后 6 GB，自动保存会把整份重写一遍到临时目录），
#   可我们那份日志对那 729 秒一片空白 —— 于是只能猜。
#   现在只要 Blender 写文件（手动保存 / 自动保存 / 另存为都走这条路），
#   日志里就会多两行：什么时候开始写、写在哪、写了多久。
#   即使把自动保存停掉了（见 core/guard.py），这条留痕也照样在 ——
#   用户自己按 Ctrl+S 的时候同样看得见。
#
#   ⚠ 实测（tools/probe_run_prefs.py）：这两个 handler 的参数是 (filepath, None)，
#     而且真的会被调用。异常一律吞掉：留痕绝不允许弄坏用户的一次保存。
# ------------------------------------------------------------------------------------

_SAVE_STATE = {"started": 0.0, "path": ""}


def _save_started(filepath=None, *_args, **_kwargs):
    try:
        _SAVE_STATE["started"] = time.time()
        _SAVE_STATE["path"] = str(filepath or "")
        phases.mark("file write starting: {}".format(_SAVE_STATE["path"] or "?"))
    except Exception:                                  # noqa: BLE001 - 见上面的说明
        pass


def _save_finished(filepath=None, *_args, **_kwargs):
    try:
        started = _SAVE_STATE.get("started") or 0.0
        seconds = (time.time() - started) if started else 0.0
        _SAVE_STATE["started"] = 0.0
        phases.mark("file write done in {:.2f}s: {}".format(
            seconds, filepath or _SAVE_STATE.get("path") or "?"))
    except Exception:                                  # noqa: BLE001
        pass


def _save_handlers():
    handlers = []
    for event, callback in (("save_pre", _save_started), ("save_post", _save_finished)):
        if hasattr(bpy.app.handlers, event):
            handlers.append((event, callback))
    return handlers


def _revalidate_session(_dummy=None):
    """撤销/重做之后：丢掉会话里已经失效的引用（材质、物体、图像）"""
    instance = session.Session._instance
    if instance is not None:
        instance.revalidate()


def _drop_session(_dummy=None):
    """换文件之前先把会话丢掉 —— 它手里的 bpy 引用马上就全部失效了"""
    session.Session.forget()


def _reset_runtime(_dummy=None):
    """换文件之后重置 UI 上的运行状态"""
    session.Session.forget()
    for scene in bpy.data.scenes:
        settings = getattr(scene, "mbakery", None)
        if settings is not None:
            settings.textures.clear()
            settings.compare_baked = False
    compat.log("会话已重置（文件载入）")
